"""req#138 task#269 返修：真实 DSH overlay.yml 加载 + 真实 PUT 保存回读证据。

两段（各自独立，可独立复跑）：
  --segment put      真实 HTTP PUT（TestClient）保存 persona/instructions（无 soul）回读一致
  --segment overlay  真实 DSH --patch overlay.yml 加载：!!js 求值 + 本地 insert 插件 + persona
运行：python -X utf8 -B scripts/verify_dsh_rework_live.py --entry <dsh.cmd绝对路径> \\
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径> \\
  [--segment put|overlay|all]
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_auth, dsh_profile_config, native_runtime  # noqa: E402


def fingerprint(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _sanitize(text):
    import re
    return re.sub(r'(?i)(sk-[A-Za-z0-9_-]{6,}|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)',
                  '[REDACTED]', str(text))[:600]


def _segment_put(test_dir: Path) -> dict:
    """真实 PUT 保存 persona/instructions（无 soul）+ 回读一致。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engine import api, auth, db

    root = Path(tempfile.mkdtemp(prefix="dsh_put_", dir=test_dir))
    home = root / "home"
    (home / "profiles" / "desktop").mkdir(parents=True)
    (home / "profiles" / "desktop" / "cordis.patch.yml").write_text(
        "- id: agent-default-model\n  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
        encoding="utf-8")
    (home / ".env").write_text("", encoding="utf-8")
    (home / "AGENTS.md").write_text("", encoding="utf-8")
    entry = root / "dsh.cmd"
    entry.write_text("@echo off\n", encoding="utf-8")

    db_path = root / "group.db"
    os.environ["GROUP_DB"] = str(db_path)
    os.environ["GROUP_ADMIN_PASSWORD"] = "test-password"
    os.environ["GROUP_SECRET"] = "test-secret"

    conn = db.get_connection()
    db.init_schema(conn)
    cur = conn.execute(
        "INSERT INTO bots(host_type,profile,config_root,runtime,runtime_entry,display_name,role) "
        "VALUES('dsh','desktop',?,'dsh',?, 'dsh-bot','')", (str(home), str(entry)))
    bot_id = cur.lastrowid
    conn.execute("INSERT INTO members(profile,display_name,role,bot_id) "
                 "VALUES('dsh-bot','dsh-bot','dsh-bot',?)", (bot_id,))
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(api.router_api)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + auth.login("test-password")["token"]}
    body = {
        "description": "一个 DSH 助手",
        "persona": "我是「PUT_PERSONA」人格",
        "instructions": "先思考再回答",
        "env": "",
        "model": "deepseek-flash",
        "provider": "deepseek-account",
        "reasoning_effort": "medium",
        "auto_review": "off",
        "revision": None,
    }
    # 先读 revision
    read0 = client.get("/members/dsh-bot/config", headers=headers)
    result = {"read_status": read0.status_code}
    if read0.status_code != 200:
        result["passed"] = False
        return result
    body["revision"] = read0.json()["revision"]
    put = client.put("/members/dsh-bot/config", headers=headers, json=body)
    result["put_status"] = put.status_code
    read1 = client.get("/members/dsh-bot/config", headers=headers).json()
    result["persona_readback"] = read1.get("persona")
    result["instructions_readback"] = read1.get("instructions")
    result["persona_consistent"] = read1.get("persona") == "我是「PUT_PERSONA」人格"
    result["instructions_consistent"] = read1.get("instructions") == "先思考再回答"
    result["passed"] = (put.status_code == 200 and result["persona_consistent"]
                        and result["instructions_consistent"])
    client.close()
    shutil.rmtree(root, ignore_errors=True)
    return result


def _segment_overlay(entry: Path, source_home: Path, test_dir: Path) -> dict:
    """真实 DSH --patch overlay.yml：!!js 求值 + 本地 insert 插件 + persona。"""
    private = Path(tempfile.mkdtemp(prefix="dsh_ovl_", dir=test_dir))
    home = Path(tempfile.mkdtemp(prefix="dsh_home_", dir=test_dir))
    (home / "profiles" / "desktop").mkdir(parents=True)
    # 本地 insert 插件：加载即写 marker（证明 insert 生效）。
    marker = private / "insert-marker.txt"
    plugin = private / "rework-probe.mjs"
    plugin.write_text(
        'import { writeFileSync } from "node:fs";\n'
        'export const name = "rework-probe-plugin";\n'
        'export function apply(ctx) { writeFileSync(' + json.dumps(str(marker)) + ', "loaded\\n"); }\n',
        encoding="utf-8")
    # persona 用 !!js 动态表达式产出指令（只有原生求值才会命中 REWORK_JS_PERSONA）。
    persona_js = ('"You are a test persona named REWORK_JS_PERSONA. '
                  'Always state your name as REWORK_JS_PERSONA in your reply."')
    patch = home / "profiles" / "desktop" / "cordis.patch.yml"
    patch.write_text(
        '- id: system-prompt\n'
        '  config:\n'
        "    personaPrefix: !!js '" + persona_js + "'\n"
        '- id: agent-default-model\n'
        '  config:\n'
        '    provider: deepseek-account\n'
        '    model: deepseek-flash\n'
        '- insert:\n'
        '    - id: rework-probe\n'
        '      name: ' + str(plugin) + '\n',
        encoding="utf-8")
    # 凭据副本（账号路由）+ 限权
    cred = dsh_auth.copy_credentials(str(source_home), str(private))
    bot = {"runtime": "dsh", "config_root": str(home), "profile": "desktop"}
    overlay = native_runtime.dsh_overlay(bot, credentials_path=cred)
    yaml_text = native_runtime.overlay_yaml(overlay)
    overlay_path = private / "overlay.yml"
    overlay_path.write_text(yaml_text, encoding="utf-8")
    result = {
        "overlay_preserves_js": "!!js" in yaml_text and "[expression]" not in yaml_text,
        "overlay_preserves_persona": "REWORK_JS_PERSONA" in yaml_text,
        "overlay_preserves_insert": "rework-probe" in yaml_text,
    }
    # 真实 DSH 经 --patch overlay.yml 运行（stdin 提示词，同 native_runtime.dsh）。
    cmd = [str(entry), "--profile", "headless", "--patch", str(overlay_path), "--json", "-"]
    env = os.environ.copy()
    env.update(DSH_HOME=str(home))
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("DSH_PROFILE", None)
    env.pop("DSH_BIN", None)
    prompt = "你是谁？用一句话自我介绍，必须报出你的角色名。"
    p = subprocess.Popen(cmd, cwd=str(private), env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace",
                         creationflags=subprocess.CREATE_NO_WINDOW)
    timed_out = False
    try:
        out, err = p.communicate(input=prompt, timeout=180)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out, err = p.communicate(timeout=10)
    events = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                events.append(e)
        except ValueError:
            pass
    final = next((e for e in reversed(events) if e.get("type") == "final"), {})
    answer = final.get("text", "")
    result["exit_code"] = p.returncode
    result["timed_out"] = timed_out
    result["persona_hit"] = "REWORK_JS_PERSONA" in answer
    result["insert_marker_written"] = marker.is_file()
    result["answer"] = answer if "REWORK_JS_PERSONA" in answer else "[unexpected output omitted]"
    result["stderr_tail"] = _sanitize(err[-400:] if err else "")
    result["passed"] = (result["overlay_preserves_js"] and result["overlay_preserves_persona"]
                        and result["overlay_preserves_insert"] and result["exit_code"] == 0
                        and result["persona_hit"] and result["insert_marker_written"]
                        and not timed_out)
    shutil.rmtree(private, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True, help="dsh.cmd 绝对路径")
    ap.add_argument("--source-home", type=Path, required=True, help="源 DSH_HOME 绝对路径")
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--segment", choices=("put", "overlay", "all"), default="all")
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")
    if not args.test_directory.is_dir():
        raise ValueError("--test-directory 必须是已分配的隔离目录")
    segments = ("put", "overlay") if args.segment == "all" else (args.segment,)
    result = {}
    for seg in segments:
        result[seg] = (_segment_put(args.test_directory) if seg == "put"
                       else _segment_overlay(args.entry, args.source_home, args.test_directory))
    result["passed"] = all(result[s].get("passed") for s in segments)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
