"""Single project-configuration tool; file work uses the host agent's native tools."""
import os
import asyncio
import json

import httpx
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

mcp = Server('project-assistant')


def update_project_config(changes: dict[str, str], expected_version: str) -> dict:
    """Update the bound creation draft or project configuration after a user instruction.

    Use the allowed fields and config_version from the current project context.
    A conflict returns current values and version for review before retrying.
    Document changes use the native file tools.
    """
    base, sid, token = (os.environ.get(key, '') for key in ('HUD_ENGINE_URL', 'HUD_SESSION_ID', 'HUD_TOOL_TOKEN'))
    if not all((base, sid, token)):
        raise RuntimeError('项目助手工具缺少会话配置')
    response = httpx.post(base.rstrip('/') + '/hud/' + sid + '/tool-config',
                          headers={'Authorization': 'Bearer ' + token},
                          json={'changes': changes, 'expected_version': expected_version}, timeout=60)
    body = response.json()
    if response.status_code >= 400:
        return {'saved': False, 'status': response.status_code, 'error': body.get('detail', body)}
    return body


@mcp.list_tools()
async def list_tools():
    return [types.Tool(name='update_project_config', description=update_project_config.__doc__,
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False), inputSchema={
        'type': 'object', 'properties': {
            'changes': {'type': 'object', 'additionalProperties': {'type': 'string'}},
            'expected_version': {'type': 'string', 'minLength': 64, 'maxLength': 64}},
        'required': ['changes', 'expected_version'], 'additionalProperties': False})]


@mcp.call_tool()
async def call_tool(name, arguments):
    if name != 'update_project_config':
        raise ValueError('Unknown project assistant tool')
    result = await asyncio.to_thread(update_project_config, **arguments)
    return [types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        await mcp.run(read, write, mcp.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
