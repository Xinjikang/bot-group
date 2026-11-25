"""bot-group relay 平台适配器（req#168 task#316）。

数据面为 no-op（引擎会话回复经随包 MCP 的群聊工具走引擎 HTTP API，
不经平台 send）；适配器随 gateway 加载和停止各引擎的连接。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from . import config as relay_config
from . import relay as relay_mod

def create_adapter(config):
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter

    class GatewayRelayAdapter(RelayAdapter, BasePlatformAdapter):
        """Use gateway-owned handler registration and runtime state."""

    return GatewayRelayAdapter(config, Platform("bot-group-relay"))


class RelayAdapter:
    """Relay transport lifecycle for the Hermes platform adapter."""

    def __init__(self, config: Any, platform: Any = None):
        super().__init__(config, platform)
        self._platform_config = config
        self._clients = {}
        self._watcher = None

    # ---------- 生命周期（gateway 调用） ----------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._refresh()
        if not self._clients:
            return False
        if self._watcher is None:
            self._watcher = asyncio.create_task(self._watch())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except asyncio.CancelledError:
                pass
            self._watcher = None
        await asyncio.gather(*(asyncio.to_thread(relay_mod.stop_gateway_client, client)
                               for client in self._clients.values()))
        self._clients.clear()
        self._mark_disconnected()

    def _refresh(self):
        for peer, cfg in relay_config.peer_configs(self._platform_config, reserve=True).items():
            if peer not in self._clients:
                self._clients[peer] = relay_mod.gateway_client(cfg, peer)

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            self._refresh()

    # ---------- 数据面 no-op ----------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None):
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Relay adapter has no data plane")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"bot-group relay ({chat_id})"}

    def enforces_own_access_policy(self) -> bool:
        return True
