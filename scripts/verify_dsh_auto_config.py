"""Verify forced Auto composition with an installed DSH, without model requests."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import dsh_auto, native_runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    entry = args.entry.resolve(strict=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='dsh-auto-config-') as tmp:
        root = Path(tmp)
        profile = root / 'profiles' / 'desktop'
        profile.mkdir(parents=True)
        (profile / 'cordis.patch.yml').write_text(
            '- id: auto-review\n  disabled: true\n', encoding='utf-8')
        (profile / '.group-engine-auto-review').write_text('off', encoding='utf-8')
        bot = dict(config_root=tmp, profile='desktop', runtime_entry=str(entry))
        assert dsh_auto.plugin_available(bot), 'Installed runtime Auto package was not detected'
        adapter = root / dsh_auto.ADAPTER_FILE
        adapter.write_text(dsh_auto.adapter_source(), encoding='utf-8')
        overlay = native_runtime.dsh_overlay(bot, auto_review={'adapter_path': str(adapter)})
        path = root / 'overlay.yml'
        path.write_text(native_runtime.overlay_yaml(overlay), encoding='utf-8')
        env = native_runtime.environment()
        env['DSH_HOME'] = tmp
        result = subprocess.run([str(entry), '--profile', 'headless', '--patch', str(path), '--dump-config'],
                                cwd=tmp, env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        (args.evidence / 'native-config.txt').write_text(result.stdout, encoding='utf-8')
        (args.evidence / 'native-stderr.txt').write_text(result.stderr, encoding='utf-8')
        assert result.returncode == 0, result.stderr
        from ruamel.yaml import YAML
        entries = YAML(typ='rt').load(result.stdout)
        autos = [item for item in entries if item.get('id') == 'auto-review']
        assert len(autos) == 1 and autos[0].get('disabled') is False, autos
        adapters = [item for item in entries if item.get('id') == 'auto-enable-adapter']
        assert len(adapters) == 1, adapters
        (args.evidence / 'result.json').write_text(json.dumps({
            'detected': True, 'auto_forced_on': True, 'adapter_present': True,
            'exit_code': result.returncode, 'model_requests': 0}, indent=2), encoding='utf-8')
    print('PASS: native DSH detected Auto and composed enabled plugin plus adapter; zero model requests')


if __name__ == '__main__':
    main()
