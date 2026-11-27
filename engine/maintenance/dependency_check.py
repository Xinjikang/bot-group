"""Verify the active distribution versions in a target virtual environment."""
from importlib import metadata
from pathlib import Path
import sys

from pip._vendor.packaging.requirements import InvalidRequirement, Requirement


def problems(path: Path, *, locked: bool):
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            yield f"{path.name} 第 {number} 行依赖声明无效"
            continue
        if req.marker and not req.marker.evaluate():
            continue
        specs = list(req.specifier)
        if req.url or req.extras:
            yield f"{path.name} 第 {number} 行的 {req.name} 需要按来源或扩展项核验"
            continue
        if locked and (len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version):
            yield f"{path.name} 中的 {req.name} 缺少精确锁定版本"
            continue
        try:
            version = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            yield f"缺少 {req.name}{req.specifier}"
            continue
        if not req.specifier.contains(version, prereleases=True):
            yield f"{req.name} 已安装 {version}，要求 {req.specifier}"


if __name__ == "__main__":
    try:
        errors = [*problems(Path(sys.argv[1]), locked=False), *problems(Path(sys.argv[2]), locked=True)]
    except (OSError, UnicodeError) as exc:
        print(f"依赖清单读取失败：{exc}")
        sys.exit(1)
    if errors:
        print("；".join(dict.fromkeys(errors)))
        sys.exit(1)
