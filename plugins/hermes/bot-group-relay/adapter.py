"""bot-group relay 平台适配器（req#168 task#316）。

数据面为 no-op（引擎会话回复经随包 MCP 的群聊工具走引擎 HTTP API，
不经平台 send）；平台角色 = 随 gateway 加载时启动/停止一条出站 WSS。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import config as relay_config
from . import relay as relay_mod

logger = logging.getLogger(__name__)


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
        self._client: relay_mod.RelayClient | None = None
        self._cfg = relay_config.load(config)
        if self._cfg is None:
            logger.warning("bot-group relay: configuration missing; adapter idle")

    # ---------- 生命周期（gateway 调用） ----------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._cfg is None:
            return False
        if self._client is None:
            self._client = relay_mod.RelayClient(self._cfg, on_status=self._on_status)
            self._client.start()
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        if self._client is not None:
            self._client.stop()
            self._client = None
        self._mark_disconnected()

    def _on_status(self, state: str) -> None:
        logger.info("bot-group relay member=%s status=%s",
                    self._cfg.member if self._cfg else "?", state)

    # ---------- 数据面 no-op ----------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None):
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Relay adapter has no data plane")

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": f"bot-group relay ({chat_id})"}

    def enforces_own_access_policy(self) -> bool:
        return True
