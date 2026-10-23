"""可替换发布源：读取正式版本索引，固定本次下载地址与摘要。仅依赖标准库。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import urllib.parse
import urllib.request

DEFAULT_INDEX_URL = "https://github.com/Xinjikang/bot-group/releases/latest/download/releases.json"


@dataclass(frozen=True)
class Release:
    version: str | None
    source: str
    sha256: str
    manifest: str = ""
    manifest_sha256: str = ""


def version_key(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    return tuple(map(int, match.groups())) if match else None


def read_bytes(url: str) -> bytes:
    if urllib.parse.urlsplit(url).scheme not in ("https", "http", "file"):
        raise RuntimeError("发布资产地址必须使用 HTTPS、HTTP 或 file URL")
    request = urllib.request.Request(url, headers={"User-Agent": "bot-group-upgrade"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_index(url: str) -> list[dict]:
    if not url:
        raise RuntimeError("请在数据目录 .env 配置 RELEASE_INDEX_URL，再执行 upgrade")
    data = json.loads(read_bytes(url).decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
        raise RuntimeError('Release 索引格式错误：需要 {"releases": [...]}')
    result = []
    for entry in data["releases"]:
        if isinstance(entry, dict):
            entry = dict(entry)
            for key in ("source", "manifest"):
                if isinstance(entry.get(key), str) and entry[key].strip():
                    entry[key] = urllib.parse.urljoin(url, entry[key])
            result.append(entry)
    return result


def pick_formal(entries: list[dict]) -> dict:
    candidates = [r for r in entries if r.get("prerelease", False) is False
                  and r.get("draft", False) is False
                  and isinstance(r.get("version"), str) and version_key(r["version"])]
    if not candidates:
        raise RuntimeError("Release 索引中无可用正式版本（版本格式为 v1.2.3）")
    return max(candidates, key=lambda r: version_key(r["version"]))


def resolve(entry: dict) -> Release:
    for key in ("version", "source", "sha256", "manifest", "manifest_sha256"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise RuntimeError(f"最新正式 Release 缺少必需资产字段：{key}")
    for key in ("sha256", "manifest_sha256"):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", entry[key]):
            raise RuntimeError(f"Release {key} 需要 64 位十六进制 SHA256")
    return Release(**{k: entry[k] for k in
                     ("version", "source", "sha256", "manifest", "manifest_sha256")})


def checked_manifest(target: Release) -> dict:
    raw = read_bytes(target.manifest)
    if hashlib.sha256(raw).hexdigest() != target.manifest_sha256.lower():
        raise RuntimeError("Release 清单 SHA256 校验失败")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != target.version:
        raise RuntimeError("Release 清单版本与固定目标不一致")
    return manifest
