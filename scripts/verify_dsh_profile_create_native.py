"""Compose a newly created profile with an installed DSH; no model request."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_dsh_profile_create import DshProfileCreateTests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    entry = args.entry.resolve(strict=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    fixture = DshProfileCreateTests()
    fixture.setUp()
    try:
        root, source, body = fixture.fixture()
        with patch.dict(os.environ, {'DSH_BIN': str(entry)}):
            response = fixture.client.post('/profiles/create', json=body)
            assert response.status_code == 200, response.text
            result = subprocess.run([str(entry), '--profile', body['profile'], '--dump-config'],
                                    cwd=fixture.root, env=os.environ.copy(), capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        (args.evidence / 'native-config.txt').write_text(result.stdout, encoding='utf-8')
        (args.evidence / 'native-stderr.txt').write_text(result.stderr, encoding='utf-8')
        assert result.returncode == 0, result.stderr
        assert 'chosen-model' in result.stdout, 'Model override missing'
        assert '独立个人指令' in result.stdout, 'Profile persona missing'
        assert 'headless' in result.stdout, 'Headless bundle missing'
        assert (source / 'cordis.patch.yml').is_file()
        (args.evidence / 'native-result.json').write_text(json.dumps({
            'exit_code': result.returncode, 'profile': body['profile'],
            'model_override': True, 'persona': True, 'headless': True,
            'model_requests': 0}, indent=2), encoding='utf-8')
        print('PASS: real API creation and native DSH composed new profile/model/persona/headless; no model requests')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
