"""Outbound relay retry policy shared by the engine and the installed gateway."""
from collections import Counter
import ssl
import time

from . import relay_protocol


DELAYS = (1, 2, 4, 8, 16) + (30,) * 19


def rejection(exc):
    """Return a safe, definitive rejection; transport timeouts remain retryable."""
    closed = getattr(exc, "rcvd", None)
    code = getattr(closed, "code", None)
    if code == 4003:
        return "protocol", relay_protocol.refusal(closed.reason)
    if isinstance(exc, relay_protocol.ProtocolMismatch):
        return "protocol", "认证回执中的通信协议不兼容"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls", "对端证书校验失败"
    return {
        4001: ("auth", "连接凭据已被拒绝"),
        4002: ("mcp", "MCP 兼容性校验失败"),
        4004: ("auth", "连接来源未获授权"),
        4005: ("direction", "两端拨号方向冲突，请核对连接方向"),
    }.get(code)


def transient_reason(stage, exc):
    if isinstance(exc, TimeoutError):
        return "等待认证回执超时" if stage == "auth" else "连接超时"
    return type(exc).__name__


class RetryRound:
    def __init__(self):
        self.started = time.monotonic()
        self.attempts = 0
        self.reasons = Counter()

    def summary(self, result):
        return {"result": result, "attempts": self.attempts,
                "offline_seconds": round(time.monotonic() - self.started, 1),
                "reasons": dict(self.reasons)}

    def finish(self, logger, event, member, result):
        summary = self.summary(result)
        logger.info("%s member=%s result=%s attempts=%d offline_seconds=%.1f reasons=%s",
                    event, member, result, self.attempts, summary["offline_seconds"], summary["reasons"])
        return summary
