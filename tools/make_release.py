"""构建发布归档并随包产出 release_manifest.json（受管理发布清单）与 VERSION（版本标识）。

node 1089 缺陷3：release_manifest.json 是区分「受管程序文件」与「用户文件」的
唯一权威依据（文件级完整相对路径，见 docs/guides/RELEASE-GUIDE.md），
必须由打包侧生成并随包分发——不能依赖升级后 upgrade._write_release_manifest 补写
（缺清单环境的升级已被 fail-closed 拒绝，运行时无从建立清单基准）。

node 215：包内 VERSION 文件记录版本号（upgrade 自动发现「当前版本」口径）。

用法（在项目根执行）：
    python tools/make_release.py [--out dist] [--name bot-group] [--version <ver>]

行为：
1. 收集受管程序文件（默认清单 = 引擎/脚本/前端/文档/入口脚本等，排除数据项与
   运行期产物，见 _MANAGED_TOP 与 _EXCLUDE_PATTERNS）；
2. 在暂存目录写入 release_manifest.json（{"program_files": [文件级相对路径…]}）；
3. 打 zip 归档（含清单）到 <out>/<name>-<version>.zip，打印归档路径与 SHA256
   （SHA256 供 do_upgrade 的 expected_sha256 校验）。
清单生成后随包分发；升级端 _collect_release_manifest 读取该文件识别受管边界。
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 受管顶层（发布内容）；数据/运行期项绝不入包
# node 1094：templates/ 是运行时模板资源（project_templates.TEMPLATE_ROOT 读
# templates/projects/<id>/manifest.json + .tmpl + references），漏收会导致解包后
# catalogue() 抛 invalid_template、/projects/templates 入口 400。
_MANAGED_TOP = (
    "engine", "tools", "frontend", "doc", "docs", "mcp", "templates",
    "requirements.txt", "requirements.lock", "install.py", "install.sh",
    "install.bat", "bootstrap.py", "bootstrap.sh", "bootstrap.bat",
    "start.bat", "start.sh", "stop.bat", "stop.sh", "upgrade.py",
    "AGENTS.md", "README.md", "README.zh-CN.md", "LICENSE", "install.ps1", "upgrade.bat",
)

# 受管目录内的排除模式（测试/缓存/运行期产物不入清单不入包）
_EXCLUDE_PARTS = {"__pycache__", "node_modules", ".venv", "venv", ".git",
                  "dist", "build", ".pytest_cache", ".mypy_cache"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".bak", ".tmp", ".orig", ".rej", "~")
_EXCLUDE_NAMES = {"install.json", "release_manifest.json", "group.db",
                  "group.db-wal", "group.db-shm", ".env"}


def _legacy_release_files(files: list[str]) -> dict[str, str]:
    """为 v0.1.x 升级进程生成旧路径转发入口，随清单接受校验与回滚管理。"""
    bridges = {}
    if "tools/install.py" in files:
        bridges["scripts/install.py"] = (
            '"""Compatibility entry for v0.1.x upgrades."""\n'
            'from pathlib import Path\n'
            'import runpy\n'
            'import sys\n'
            'root = Path(__file__).resolve().parents[1]\n'
            'sys.path.insert(0, str(root))\n'
            'if __name__ == "__main__":\n'
            '    runpy.run_path(str(root / "tools" / "install.py"), run_name="__main__")\n'
        )
    if "tools/service.py" in files:
        bridges["scripts/service.py"] = (
            '"""Environment loader used by v0.1.x rollback requests."""\n'
            'from tools.service import _load_env\n'
        )
    return bridges


def _managed_files(root: Path) -> list[str]:
    """枚举受管程序文件（文件级相对路径，正斜杠分隔，排除数据/测试/运行期产物）。"""
    files: list[str] = []
    for top in _MANAGED_TOP:
        p = root / top
        if not p.exists():
            continue
        if p.is_file():
            if p.name not in _EXCLUDE_NAMES:
                files.append(top)
            continue
        for cur, dirs, names in os.walk(p):
            dirs[:] = [d for d in dirs
                       if d not in _EXCLUDE_PARTS and not d.startswith(".")]
            for name in sorted(names):
                if name.startswith('.') or name in _EXCLUDE_NAMES:
                    continue
                if name.lower().endswith(_EXCLUDE_SUFFIXES) or ".bak_" in name.lower():
                    continue
                if name.startswith("test_") or name.endswith("_test.py"):
                    continue  # 测试用例随仓不随包
                rel = (Path(cur) / name).relative_to(root)
                files.append(rel.as_posix())
    return sorted(set(files))


def make_release(root: Path, out_dir: Path, name: str, version: str, base_url: str = "") -> int:
    # 限制输出文件名，版本正式性由 --formal 入口验证。
    if any(c in name + version for c in '/\\:') or name in ('.', '..') or version in ('.', '..'):
        raise ValueError("发布名称和版本须为单个文件名段")
    files = _managed_files(root)
    if not files:
        print("[make_release] 未收集到任何受管文件，中止（避免产出空清单归档）",
              file=sys.stderr)
        return 1
    # VERSION 属于受管文件，覆盖、正向删除与回滚均走相同的文件归属规则。
    bridges = _legacy_release_files(files)
    files = sorted(set(files) | {"VERSION"} | bridges.keys())
    manifest = {"version": version, "program_files": files}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{name}-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        # 清单先写：升级端解包后 _collect_release_manifest 从源码根读它
        z.writestr("release_manifest.json", manifest_bytes)
        z.writestr("VERSION", version)
        for rel in files:
            if rel in bridges:
                z.writestr(rel, bridges[rel])
            elif rel != "VERSION":
                z.write(root / rel, rel)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest_path = out_dir / "release_manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    checksums = f"{sha}  {archive.name}\n{manifest_sha}  {manifest_path.name}\n"
    bootstrap = root / "tools" / "bootstrap.py"
    installer_assets = {}
    if bootstrap.is_file():
        bootstrap_bytes = bootstrap.read_bytes()
        (out_dir / "bootstrap.py").write_bytes(bootstrap_bytes)
        checksums += f"{hashlib.sha256(bootstrap_bytes).hexdigest()}  bootstrap.py\n"
        installer_assets = {"bootstrap": base_url + "bootstrap.py",
                            "bootstrap_sha256": hashlib.sha256(bootstrap_bytes).hexdigest()}
    installer = root / "install.ps1"
    if installer.is_file():
        installer_bytes = installer.read_bytes()
        (out_dir / "install.ps1").write_bytes(installer_bytes)
        checksums += f"{hashlib.sha256(installer_bytes).hexdigest()}  install.ps1\n"
    (out_dir / "SHA256SUMS").write_text(
        checksums, encoding="utf-8")
    (out_dir / "releases.json").write_text(json.dumps({"releases": [{
        "version": version, "prerelease": False, "draft": False,
        "source": base_url + archive.name, "sha256": sha,
        "manifest": base_url + manifest_path.name, "manifest_sha256": manifest_sha,
        **installer_assets,
    }]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[make_release] 归档：{archive}")
    print(f"[make_release] 受管文件 {len(files)} 项（文件级清单已随包）")
    print(f"[make_release] SHA256：{sha}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构建发布归档并随包产出 release_manifest.json")
    parser.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    parser.add_argument("--name", default="bot-group", help="归档名（默认 bot-group）")
    parser.add_argument("--version", default="dev", help="版本号（默认 dev）")
    parser.add_argument("--formal", action="store_true", help="正式发布，版本必须符合 v1.2.3")
    parser.add_argument("--base-url", default="", help="固定版本资产 URL 前缀，以 / 结尾")
    args = parser.parse_args(argv)
    if args.base_url and (not args.base_url.startswith("https://") or not args.base_url.endswith("/")):
        parser.error("--base-url 必须为以 / 结尾的 HTTPS 地址")
    if args.formal:
        from tools import releases
        if releases.version_key(args.version) is None:
            parser.error("正式版本格式为 v1.2.3，预发布请使用独立目录与索引")
    root = Path(__file__).resolve().parent.parent
    return make_release(root, Path(args.out), args.name, args.version, args.base_url)


if __name__ == "__main__":
    sys.exit(main())
