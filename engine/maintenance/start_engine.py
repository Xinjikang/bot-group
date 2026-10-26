"""Start the engine independently of the launcher's console lifetime."""

from pathlib import Path
import os
import subprocess
import sys

_ROOT = Path(__file__).resolve().parents[2]


def start_detached(command: list[str], cwd: Path, log_path: Path, *, env=None) -> int:
    options = {}
    if os.name == "nt":
        # DETACHED_PROCESS disconnects the service from the batch/Git Bash console.
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab", buffering=0) as output:
        child = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, close_fds=True, env=env, **options,
        )
    return child.pid


def main() -> None:
    from engine import config
    root = _ROOT
    executable = Path(sys.executable)
    if os.name == "nt":
        executable = executable.with_name("pythonw.exe")
    # 启动日志落数据目录（req#122 程序/数据分离，与引擎日志同目录）
    env = dict(os.environ)
    env.pop('GROUP_RESTART_INSTANCE', None)
    env.pop('GROUP_SERVICE_MAINTENANCE', None)
    pid = start_detached(
        [str(executable), "-m", "engine.main"], root,
        config.logs_dir() / "engine-startup.log", env=env,
    )
    print(f"[start-engine] Background process created: {pid}")


if __name__ == "__main__":
    main()
