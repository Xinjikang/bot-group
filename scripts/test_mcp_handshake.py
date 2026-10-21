"""Discover both tool groups on the real unified MCP: python scripts/test_mcp_handshake.py."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.unified_test_support import isolated_api, isolated_database, server_parameters
from engine import db
import json
from scripts.unified_test_support import REQUIREMENT_FIELDS



async def main():
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    with isolated_database() as path, isolated_api() as url:
        async with stdio_client(server_parameters(path, url)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                from engine.wake import _MCP_TOOLS
                assert names <= set(_MCP_TOOLS), names - set(_MCP_TOOLS)
                schemas = {tool.name: tool.inputSchema['properties'] for tool in tools}
                log_tool = next(tool for tool in tools if tool.name == 'task_log')
                for field in ('type', 'meta', 'result', 'comment'):
                    assert field + '：' in log_tool.description, field
                example = json.loads(log_tool.description[log_tool.description.index('{"project":'):].strip())
                from jsonschema import validate
                validate(example, log_tool.inputSchema)
                assert {'commit', 'artifacts', 'tests', 'report'} <= example['meta'].keys()
                assert '未验证' in example['result'] and example['comment']
                print('PASS: task_log four-field description and complete schema-valid example exposed by real MCP')
                for name in ('task_log', 'task_update'):
                    assert 'next_bot' in schemas[name] and 'next_role' not in schemas[name]
                assert 'role' in schemas['group_list_members']
                required = {'project_list', 'req_create', 'task_create', 'task_claim',
                            'task_complete', 'group_send', 'group_read_inbox',
                            'group_create_thread', 'group_get_thread'}
                assert required <= names, required - names
                assert not {'work_on', 'work_off'} & names
                print(f'PASS: unified MCP exposes both tool groups ({len(names)} tools)')
        for mode in ('0', '1'):
            params = server_parameters(path, url)
            params.env.update({'TASK_QUEUE_ROLE': 'arbitrary-profile', 'HERMES_PROFILE': 'arbitrary-profile', 'HERMES_CHAT_MODE': mode})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert {'req_create', 'task_update', 'req_cancel', 'task_cancel', 'req_complete',
                            'req_resume', 'task_resume'} <= names
                    from engine.wake import _MCP_TOOLS
                    assert {'req_update', 'req_resume', 'task_resume'} <= set(_MCP_TOOLS)
                    # req#117 #199：task_create 双入口开放；领取/开发/交付工具仍仅 WORK 注册。
                    assert 'task_create' in names
                    for name in ('task_claim', 'task_log', 'task_complete', 'task_block'):
                        assert (name in names) == (mode == '0'), (mode, name)
                    response = await session.call_tool('req_create', {'project': 'alpha', 'title': 'denied'})
                    assert json.loads(response.content[0].text)['code'] == 'permission_denied'
                    conn = db.get_connection()
                    conn.execute("INSERT OR IGNORE INTO members(profile,display_name) VALUES('arbitrary-profile','Arbitrary')")
                    conn.execute("INSERT OR IGNORE INTO project_members(project,profile,active) VALUES('alpha','arbitrary-profile',1)")
                    conn.execute("INSERT INTO project_permissions VALUES('alpha','arbitrary-profile','req.create')")
                    conn.commit()
                    response = await session.call_tool('req_create', {'project': 'alpha', 'title': 'granted', **REQUIREMENT_FIELDS})
                    assert 'requirement_id' in json.loads(response.content[0].text)
                    rid = json.loads(response.content[0].text)['requirement_id']
                    conn.execute("UPDATE requirements SET status='blocked' WHERE id=?", (rid,))
                    tid = conn.execute("INSERT INTO tasks(project,requirement_id,title,status,next_bot,created_at) "
                                       "VALUES('alpha',?,'resume test','blocked','arbitrary-profile',?)",
                                       (rid, db.now_iso())).lastrowid
                    conn.commit()
                    for tool, key, args in (
                            ('req_resume', 'req.resume', {'project': 'alpha', 'requirement_id': rid}),
                            ('task_resume', 'task.resume', {'project': 'alpha', 'task_id': tid})):
                        response = await session.call_tool(tool, args)
                        assert json.loads(response.content[0].text)['code'] == 'permission_denied'
                        conn.execute("INSERT INTO project_permissions VALUES('alpha','arbitrary-profile',?)", (key,))
                        conn.commit()
                        response = await session.call_tool(tool, args)
                        assert json.loads(response.content[0].text)['resumed'], response
                    response = await session.call_tool('req_create', {'project': 'beta', 'title': 'wrong project'})
                    assert json.loads(response.content[0].text)['code'] == 'permission_denied'
                    conn.execute("DELETE FROM project_permissions WHERE profile='arbitrary-profile'")
                    conn.commit()
                    conn.close()
                    response = await session.call_tool('req_create', {'project': 'alpha', 'title': 'revoked'})
                    assert json.loads(response.content[0].text)['code'] == 'permission_denied'
        # 环境标志遗漏时仍通过执行上下文拒绝，数据库所有变更计数保持不变。
        params = server_parameters(path, url)
        params.env['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps({
            'profile': 'default', 'project': 'alpha', 'thread_id': 99,
            'session': 'default-alpha-chat-99', 'wake_started_at': 100})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                conn = db.get_connection()
                before = list(conn.iterdump())
                response = await session.call_tool('task_claim', {'project': 'alpha'})
                result = json.loads(response.content[0].text)
                assert result['error'] == 'PERMISSION_DENIED', result
                assert result['reason'] == 'CHAT_MODE_RESTRICTED', result
                assert result['action'] == 'task_claim', result
                assert 'scan:req' in result['next_step'], result
                assert list(conn.iterdump()) == before
                conn.close()
        print('PASS: arbitrary profiles, live grants, CHAT registration and runtime denial with zero writes')


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=45))
