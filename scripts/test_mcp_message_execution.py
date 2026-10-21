"""Real stdio MCP -> isolated HTTP API -> atomic execution link."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import db
from scripts.unified_test_support import isolated_api, isolated_database, server_parameters


async def main():
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    with isolated_database() as path, isolated_api() as url:
        conn = db.get_connection()
        conn.execute("INSERT INTO wake_runs(session,started_at,profile,project) VALUES('default-alpha-work',100,'default','alpha')")
        conn.commit()
        params = server_parameters(path, url)
        params.env['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps(dict(
            profile='default', project='alpha', thread_id=None,
            session='default-alpha-work', wake_started_at=100))
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tool = next(t for t in (await session.list_tools()).tools if t.name == 'group_send')
                    assert set(tool.inputSchema['properties']) == {'text', 'need_reply'}, tool.inputSchema
                    response = await session.call_tool('group_send', {'text': '@dev response', 'need_reply': False})
                    assert not response.isError, response
                    result = json.loads(response.content[0].text)
                    assert 'message_id' in result, result
                    row = conn.execute('SELECT m.project,m.thread_id,e.session,e.wake_started_at FROM messages m '
                                       'JOIN message_executions e ON e.message_id=m.id WHERE m.id=?',
                                       (result['message_id'],)).fetchone()
                    assert tuple(row) == ('alpha', None, 'default-alpha-work', 100), tuple(row)
                    print('PASS: real MCP schema exposes only text/need_reply; execution metadata fills HTTP request and persisted link')
        finally:
            conn.close()


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=30))
