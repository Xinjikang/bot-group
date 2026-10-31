"""Capture the DSH plugin-status payload for the member editor integration test.

区分证据层级：本脚本用**合成配置**（临时 DSH_HOME 的 cordis.patch.yml）+ **真实读取接口**
（dsh_profile_config.read）得到静态 plugins 载荷，供前端浏览器用例驱动。它**不是原生加载
证据**——bundle 插件在 asar 内、真实运行时加载失败由 266 启动探测补足，此处只验证编辑时
可用能力的判定（disabled/load_failed/继承 ok）。
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_profile_config  # noqa: E402

PATCH = """\
- id: system-prompt
  disabled: true
  config:
    personaPrefix: "探针人格"
- id: agent-default-model
  name: "@deepseek-ai/custom-not-installed"
  config:
    model: deepseek-flash
    provider: deepseek-account
    reasoningEffort: max
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / 'dsh'
        profile = 'desktop'
        folder = home / 'profiles' / profile
        folder.mkdir(parents=True)
        (folder / 'cordis.patch.yml').write_text(PATCH, encoding='utf-8')
        (home / 'AGENTS.md').write_text('真实指令链', encoding='utf-8')
        (home / '.env').write_text('DEEPSEEK_API_KEY=fixture\n', encoding='utf-8')
        (folder / '.group-engine-description').write_text('真实描述', encoding='utf-8')
        view = dsh_profile_config.read({'config_root': str(home), 'profile': profile})
        evidence = Path(args.evidence)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({
            'kind': 'synthetic-config + real read interface (not native load evidence)',
            'profile': profile,
            'home': str(home),
            'plugins': view['plugins'],
            'config': view,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        states = {field: state['state'] for field, state in view['plugins'].items()}
        assert states['persona'] == 'disabled', states
        assert states['instructions'] == 'ok', states  # 未覆盖 → 继承 bundle 默认
        assert states['model'] == states['provider'] == states['reasoning_effort'] == 'load_failed', states
        assert states['description'] == states['env'] == 'ok', states
        print(f'PASS: dsh plugin status {states}; evidence={evidence.resolve()}')


if __name__ == '__main__':
    main()
