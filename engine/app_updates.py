"""Read-only application version and formal release discovery."""
from scripts import releases, upgrade


def version_info() -> dict:
    try:
        version = upgrade.current_version()
    except (OSError, UnicodeError):
        version = None
    return {"current_version": version,
            "version_label": version or "开发版 / 版本未知"}


def check_update() -> dict:
    result = version_info()
    try:
        target = upgrade.discover_release()
    except Exception:
        return {**result, "status": "unavailable", "update_available": False}
    current = releases.version_key(result["current_version"] or "")
    latest = releases.version_key(target.version or "")
    return {**result, "latest_version": target.version,
            "status": "ok" if current and latest else "unknown",
            "update_available": bool(current and latest and latest > current)}
