"""Deterministic Hermes CLI stand-in exercising real stdio MCP and HTTP APIs."""
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    report = Path(os.environ['PROCESS_PROBE_REPORT'])
    report.with_suffix('.ready').touch()
    for _ in range(300):
        if report.with_suffix('.continue').exists():
            break
        await asyncio.sleep(.1)
    else:
        raise TimeoutError('probe release timed out')
    overlay = json.loads((Path(os.environ['HERMES_MANAGED_DIR']) / 'config.yaml').read_text(encoding='utf-8'))
    server = overlay['mcp_servers']['task_queue']
    params = StdioServerParameters(command=server['command'], args=server['args'],
                                  env={**os.environ, **server['env']}, cwd=server['cwd'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool, **arguments):
                response = await session.call_tool(tool, arguments)
                assert not response.isError, response
                result = json.loads(response.content[0].text)
                assert 'error' not in result, result
                return result

            inbox = await call('group_read_inbox')
            ids = [m['id'] if 'id' in m else m['message_id'] for m in inbox['messages']]
            assert ids, inbox
            blocked = await call('group_mark_seen', message_ids=ids)
            assert blocked.get('blocked') == ids, blocked
            sent = await call('group_send', text='@user process probe completed', need_reply=False)
            seen = await call('group_mark_seen', message_ids=ids)
            assert seen == {'seen': len(ids)}, seen
            report.write_text(json.dumps(dict(inbox=inbox, blocked=blocked, sent=sent, seen=seen),
                                         ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=50))
