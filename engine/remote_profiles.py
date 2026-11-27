"""Route profile edits to the authenticated link for the selected roster member."""
from fastapi import HTTPException

from . import relay_control, relay_protocol, relay_server

_FIELDS = ('description', 'soul', 'env', 'model', 'provider', 'reasoning_effort', 'revision')


def _link(member):
    link = relay_server.active_link(member)
    if link is None or link.closed:
        raise HTTPException(409, '远程节点离线，请连接后重新打开编辑窗口')
    if link.protocol_version < 3:
        raise HTTPException(409, '请升级远端 bot-group-relay 插件并重启 Hermes 网关，再打开编辑窗口')
    return link


def _request(link, spec):
    saving = spec['action'] == 'save'
    uncertain = '远端保存结果待确认，请重新打开编辑窗口核对配置'
    try:
        response = relay_control.request(link, kind='profile_request', field='profile', spec=spec, timeout=25,
                                         timeout_message=uncertain if saving else '读取远程配置超时，请重试')
    except HTTPException as exc:
        if saving and (link.closed or link is not relay_server.active_link(link.member) or exc.status_code == 504):
            raise HTTPException(504, uncertain) from None
        raise
    if 'failure' in response:
        raise HTTPException(response.get('status', 400), response['failure'])
    return response


def read(member):
    link = _link(member)
    result = _request(link, {'action': 'read', 'include_env': link.profile_tls})
    fields = relay_protocol.profile_fields(result['fields'], decode=True)
    editable = link.profile_tls and 'env' in fields
    if not editable:
        fields.pop('env', None)
        fields['capabilities'] = {'env': {'available': False, 'reason': '编辑 .env 需要加密的 WSS 连接'}}
    return dict(fields, env_editable=editable, remote_profile=True)


def save(member, values):
    link = _link(member)
    if 'env' in values and not link.profile_tls:
        raise HTTPException(409, '编辑 .env 需要加密的 WSS 连接，请重开编辑窗口')
    from .profile_config import _MAX_BYTES
    selected = {k: values[k] for k in _FIELDS if values.get(k) is not None}
    if any(len(value.encode('utf-8')) > _MAX_BYTES for value in selected.values()):
        raise HTTPException(400, '单个配置文件不能超过 2 MB')
    fields = relay_protocol.profile_fields(selected)
    return _request(link, {'action': 'save', 'fields': fields})
