"""Read-only application version and formal release discovery."""
from .maintenance import releases, upgrade
from .version import version_info


def check_update() -> dict:
    result = version_info()
    from .app_upgrade import blocked_reason
    reason = blocked_reason(result)
    result.update(can_upgrade=not reason, upgrade_blocked_reason=reason)
    try:
        target = upgrade.discover_release()
    except Exception:
        return {**result, "status": "unavailable", "update_available": False}
    current = releases.version_key(result["current_version"] or "")
    latest = releases.version_key(target.version or "")
    return {**result, "latest_version": target.version,
            "status": "ok" if current and latest else "development" if result.get('version_source') == 'git' else "unknown",
            "update_available": bool(current and latest and latest > current)}
