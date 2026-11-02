"""Project configuration and navigation; files use the host agent's native tools."""
import os
import asyncio
import json

import httpx
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

mcp = Server('project-assistant')


def project_config(action='read', section='all', operation='update', changes=None, expected_version=''):
    """读取或修改当前项目数据库配置，或创建 profile；使用区域配置版本校验，返回实际保存结果。"""
    base, sid, token = (os.environ.get(key, '') for key in ('HUD_ENGINE_URL', 'HUD_SESSION_ID', 'HUD_TOOL_TOKEN'))
    if not all((base, sid, token)):
        raise RuntimeError('项目助手工具缺少会话配置')
    response = httpx.post(base.rstrip('/') + '/hud/' + sid + '/tool-settings',
        headers={'Authorization': 'Bearer ' + token}, json={'action': action, 'section': section,
        'operation': operation, 'changes': changes or {}, 'expected_version': expected_version}, timeout=120)
    body = response.json()
    return body if response.status_code < 400 else {'saved': False, 'status': response.status_code, 'error': body.get('detail', body)}


def open_project_page(page: str = 'basic') -> dict:
    """请求打开当前项目配置窗口的指定标签，保持未保存输入；返回导航请求。"""
    base, sid, token = (os.environ.get(key, '') for key in ('HUD_ENGINE_URL', 'HUD_SESSION_ID', 'HUD_TOOL_TOKEN'))
    if not all((base, sid, token)):
        raise RuntimeError('项目助手工具缺少会话配置')
    response = httpx.post(base.rstrip('/') + '/hud/' + sid + '/tool-navigation',
                         headers={'Authorization': 'Bearer ' + token}, json={'page': page}, timeout=30)
    body = response.json()
    return body if response.status_code < 400 else {'requested': False, 'status': response.status_code, 'error': body.get('detail', body)}


@mcp.list_tools()
async def list_tools():
    return [types.Tool(name='project_config', description=project_config.__doc__,
        annotations=types.ToolAnnotations(readOnlyHint=False, openWorldHint=False), inputSchema={
        'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['read', 'update'], 'default': 'read'},
            'section': {'type': 'string', 'enum': ['all', 'basic', 'roles', 'members', 'permissions', 'profiles'], 'default': 'all'},
            'operation': {'type': 'string', 'default': 'update'},
            'changes': {'type': 'object', 'additionalProperties': True},
            'expected_version': {'type': 'string'}}, 'additionalProperties': False}),
        types.Tool(name='open_project_page', description=open_project_page.__doc__,
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
            inputSchema={'type': 'object', 'properties': {'page': {'type': 'string',
                'enum': ['basic', 'agents', 'members', 'roles', 'permissions'], 'default': 'basic'}},
                'additionalProperties': False})]


@mcp.call_tool()
async def call_tool(name, arguments):
    handlers = {'project_config': project_config, 'open_project_page': open_project_page}
    if name not in handlers:
        raise ValueError('Unknown project assistant tool')
    result = await asyncio.to_thread(handlers[name], **arguments)
    return [types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        await mcp.run(read, write, mcp.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
