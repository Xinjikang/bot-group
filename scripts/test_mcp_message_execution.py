"""Real stdio MCP -> isolated HTTP API -> atomic execution link."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import auth, config, db, signing_keys
from scripts.unified_test_support import isolated_api, isolated_database, server_parameters


async def main():
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    with isolated_database() as path, \
         patch.object(config, 'data_dir', return_value=Path(path).parent), \
         patch.object(signing_keys, '_active_path', None), \
         patch.dict(os.environ, {'GROUP_ENGINE_KEY': ''}), isolated_api() as url:
        conn = db.get_connection()
        signing_keys.initialize(conn)
        conn.execute("INSERT INTO wake_runs(session,started_at,profile,project,runtime) "
                     "VALUES('default-alpha-work',100,'default','alpha','hermes')")
        mid = conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply) "
            "VALUES('alpha',?,'user','user','@default reply','[\"default\"]',1)", (db.now_iso(),)).lastrowid
        conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'default','pending')", (mid,))
        conn.commit()
        params = server_parameters(path, url)
        params.env['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps(dict(
            profile='default', project='alpha', thread_id=None,
            session='default-alpha-work', wake_started_at=100))
        params.env['GROUP_ENGINE_TOKEN'] = auth.execution_token(
            'default', json.loads(params.env['XDG_GROUP_ENGINE_EXECUTION']))
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools if t.name == 'group_send')
                    assert set(tool.inputSchema['properties']) == {'text', 'need_reply'}, tool.inputSchema
                    async def call(name, arguments):
                        response = await session.call_tool(name, arguments)
                        assert not response.isError, response
                        return json.loads(response.content[0].text)
                    assert (await call('group_read_inbox', {}))['pending_total'] == 1
                    assert (await call('group_mark_seen', {'message_ids': [mid]}))['blocked'] == [mid]
                    response = await session.call_tool('group_send', {'text': '@user response', 'need_reply': False})
                    assert not response.isError, response
                    result = json.loads(response.content[0].text)
                    assert 'message_id' in result, result
                    row = conn.execute('SELECT m.project,m.thread_id,e.session,e.wake_started_at FROM messages m '
                                       'JOIN message_executions e ON e.message_id=m.id WHERE m.id=?',
                                       (result['message_id'],)).fetchone()
                    assert tuple(row) == ('alpha', None, 'default-alpha-work', 100), tuple(row)
                    assert (await call('group_mark_seen', {'message_ids': [mid]})) == {'seen': 1}
                    assert (await call('group_read_inbox', {}))['pending_total'] == 0
                    conn.execute("UPDATE wake_runs SET ended_at=101 WHERE session='default-alpha-work'")
                    conn.commit()
                    expired = await call('group_read_inbox', {})
                    assert 'engine 401' in expired.get('error', ''), expired
                    print('PASS: real stdio MCP with persistent-generation Hermes CLI credentials; '
                          'read/reply/seen, execution provenance and terminal 401')
        finally:
            conn.close()


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=30))
