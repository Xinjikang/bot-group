"""Real unified MCP + isolated API + one group.db; python scripts/test_mcp_e2e.py."""
import asyncio
import json
import os
import sqlite3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.unified_test_support import REQUIREMENT_FIELDS, confirmed_requirement_fields, role_id_of
from engine import auth, db
from scripts.unified_test_support import isolated_api, isolated_database, server_parameters


async def main(tasks_only=False):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    with isolated_database() as path, isolated_api() as url:
        conn = db.get_connection()
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile) VALUES(?,?,?,?)",
                     ('default-alpha-work', 100, 'alpha', 'default'))
        conn.commit()
        conn.close()
        params = server_parameters(path, url)
        params.env['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps({'profile': 'default', 'project': 'alpha',
            'thread_id': None, 'session': 'default-alpha-work', 'wake_started_at': 100})
        params.env['GROUP_ENGINE_TOKEN'] = auth.execution_token(
            'default', json.loads(params.env['XDG_GROUP_ENGINE_EXECUTION']))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, **arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, response
                    result = json.loads(response.content[0].text)
                    assert 'error' not in result, result
                    return result

                req = await call('req_create', project='alpha', title='Unified MCP regression', **confirmed_requirement_fields('alpha'))
                rid = req['requirement_id']
                updated = await call('req_update', project='alpha', requirement_id=rid, background='Updated goal')
                assert updated['background_goal'] == 'Updated goal'
                invalid = await session.call_tool('req_create', {'project': 'alpha', 'title': 'Missing fields'})
                assert json.loads(invalid.content[0].text)['code'] == 'requirement_fields_invalid'
                created = await call('task_create', project='alpha', requirement_id=rid,
                                     tasks=[{'title': 'work', 'next_bot': 'default',
                                             'description': 'Implement isolated workflow',
                                             'design_basis': 'Approved integration test contract',
                                             'acceptance_criteria': 'Claim returns full requirement and complete task',
                                             'prerequisites': [], 'draft': True}])
                tid = created['task_ids'][0]
                assert not (await call('task_claim', project='alpha'))['claimed']
                await call('task_update', project='alpha', task_id=tid, publish=True)
                claimed = await call('task_claim', project='alpha')
                assert claimed['task']['id'] == tid and claimed['task']['project'] == 'alpha'
                packet = await call('task_get', project='alpha', task_id=tid)
                assert packet['requirement']['id'] == rid
                assert packet['requirement']['background_goal'] == 'Updated goal'
                assert packet['task']['description'] == 'Implement isolated workflow'
                for fields in ({}, {'result': ' \t\n'}):
                    rejected = await session.call_tool('task_log', dict(project='alpha', task_id=tid,
                        type='dev', next_bot='default', expected_role_id=role_id_of('alpha', 'default'), **fields))
                    assert json.loads(rejected.content[0].text)['code'] == 'task_result_required'
                    assert await call('task_get', project='alpha', task_id=tid) == packet
                await call('task_log', project='alpha', task_id=tid, type='dev', next_bot='default',
                           result='Verified isolated MCP workflow and complete task context',
                           expected_role_id=role_id_of('alpha', 'default'))
                await call('task_complete', project='alpha', task_id=tid)
                await call('req_complete', project='alpha', requirement_id=rid)
                # Keep the same MCP process while the API switches to a relocated database.
                moved = str(Path(path).with_name("relocated.db"))
                old = db.get_connection()
                destination = sqlite3.connect(moved)
                try:
                    old.backup(destination)
                finally:
                    destination.close()
                    old.close()
                os.environ["GROUP_DB"] = moved
                await call('req_update', project='alpha', requirement_id=rid, background='After relocation')
                old = sqlite3.connect(path)
                try:
                    assert old.execute("SELECT background_goal FROM requirements WHERE id=?", (rid,)).fetchone()[0] == 'Updated goal'
                finally:
                    old.close()
                assert (await call('task_get', project='alpha', task_id=tid))['requirement']['background_goal'] == 'After relocation'
                print('PASS: running MCP follows API database relocation without configuration changes')
                denied = await session.call_tool('req_list', {'project': 'beta'})
                assert '403' in json.loads(denied.content[0].text)['error']
                async with stdio_client(server_parameters(path, url)) as (other_read, other_write):
                    async with ClientSession(other_read, other_write) as other:
                        await other.initialize()
                        response = await other.call_tool('req_create', {
                            'project': 'beta', 'title': 'Separate project, shared table',
                            **confirmed_requirement_fields('beta')})
                        assert 'requirement_id' in json.loads(response.content[0].text)
                if tasks_only:
                    print('PASS: real MCP draft gate, publish, full claim/task_get context, handoff and completion')
                    return

                conn = db.get_connection()
                conn.execute("UPDATE project_members SET role='质量把关人' WHERE project='alpha' AND profile='dev'")
                conn.commit()
                conn.close()
                roster = await call('group_list_members', role='质量把关人')
                assert [m['profile'] for m in roster['members']] == ['dev'], roster
                topic = await call('group_create_thread', title='Unified group tools',
                                   members=['default', 'dev'], opening='Shared database context')
                thread = await call('group_get_thread', thread_id=topic['thread_id'])
                assert any(m['content'] == 'Shared database context' for m in thread['messages'])
                conn = db.get_connection()
                try:
                    assert {r[0] for r in conn.execute('SELECT project FROM requirements')} == {'alpha', 'beta'}
                    assert conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'done'
                    assert conn.execute('SELECT COUNT(*) FROM messages WHERE thread_id=?', (topic['thread_id'],)).fetchone()[0] == 1
                finally:
                    conn.close()
                print('PASS: one MCP serves tasks and group tools against one isolated group.db')


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(tasks_only='--tasks-only' in sys.argv), timeout=60))
