"""Entry point embedded in the Linux Hermes relay installer."""
import argparse
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def unpack(installer, stage):
    with zipfile.ZipFile(installer) as bundle:
        payload = bundle.read('relay.tar.gz')
        expected = bundle.read('relay.sha256').decode('ascii')
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('Relay archive checksum mismatch')
    files = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        for entry in archive:
            path = Path(entry.name)
            if (not entry.isfile() or path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] in ('state', 'venv') or any(p.startswith('.') for p in path.parts)):
                raise ValueError(f'Unexpected relay archive entry: {entry.name}')
            dest = stage / path
            dest.parent.mkdir(exist_ok=True, parents=True)
            with dest.open('xb') as output:
                shutil.copyfileobj(archive.extractfile(entry), output)
            dest.chmod(entry.mode & 0o755)
            files.append(path)
    manifest = json.loads((stage / 'manifest.json').read_text(encoding='utf-8'))
    version = (stage / 'VERSION').read_text().strip()
    if not version or manifest['plugin'] != 'bot-group-relay' or manifest['version'] != version:
        raise ValueError('Invalid relay manifest or version')
    return version, files


def prepare_runtime(stage, storage):
    lock = stage / 'requirements.lock'
    key = hashlib.sha256(lock.read_bytes()).hexdigest()
    runtime = storage / f'py{sys.version_info.major}.{sys.version_info.minor}-{key}'
    python = runtime / 'venv/bin/python'
    ready = runtime / 'ready'
    if not (ready.is_file() and python.is_file()):
        runtime.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lock, runtime / lock.name)
        shutil.copy2(stage / 'install.sh', runtime / 'install.sh')
        subprocess.run([sys.executable, '-m', 'venv', str(runtime / 'venv')], check=True)
        subprocess.run(['sh', str(runtime / 'install.sh')], check=True)
        if not python.is_file():
            raise ValueError('The bundled MCP environment has no Python interpreter')
        ready.touch()
    return python.parents[1]


def deploy(stage, backup, plugin, files, hermes):
    # Replace only package-owned entries. The active gateway can keep writing its pairing state.
    with ExitStack() as rollback:
        for relative in files:
            target = plugin / relative
            if target.parent.resolve() != target.parent:
                raise ValueError(f'Unexpected symlink in plugin installation path: {target.parent}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                if target.is_dir() and relative != Path('venv'):
                    raise ValueError(f'Expected a plugin file: {target}')
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                target.rename(saved)
                rollback.callback(saved.rename, target)
            (stage / relative).replace(target)
            rollback.callback(target.unlink)
        subprocess.run([*hermes, 'plugins', 'enable', 'bot-group-relay'], check=True)
        rollback.pop_all()


def main():
    parser = argparse.ArgumentParser(description='Install the bundled bot-group relay into a Linux Hermes profile')
    parser.add_argument('--profile', help='Hermes profile (defaults to the active CLI profile)')
    args = parser.parse_args()
    hermes = ['hermes', '-p', args.profile] if args.profile else ['hermes']
    config = Path(subprocess.check_output([*hermes, 'config', 'path'], text=True).strip())
    if not config.is_absolute() or config.name != 'config.yaml' or not config.parent.is_dir():
        raise ValueError('Hermes did not return the config path of an existing profile')
    home = config.parent.resolve()
    storage = home / '.bot-group-relay'
    storage.mkdir(mode=0o700, exist_ok=True)
    import fcntl
    with (storage / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plugins = home / 'plugins'
        plugins.mkdir(exist_ok=True)
        plugin = plugins.resolve() / 'bot-group-relay'
        if plugin.is_symlink():
            raise ValueError(f'Expected a plugin directory, found a symlink: {plugin}')
        with tempfile.TemporaryDirectory(prefix='.relay-install-', dir=plugins) as scratch:
            stage = Path(scratch) / 'package'
            stage.mkdir()
            version, files = unpack(sys.argv[0], stage)
            runtime = prepare_runtime(stage, storage)
            (stage / 'venv').symlink_to(runtime, target_is_directory=True)
            plugin.mkdir(exist_ok=True)
            deploy(stage, Path(scratch) / 'previous', plugin, [*files, Path('venv')], hermes)
    print(f'Installed bot-group-relay {version} in {plugin}')
    print('Reload this profile with: ' + shlex.join([*hermes, 'gateway', 'restart']))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, tarfile.TarError, zipfile.BadZipFile) as error:
        sys.exit(f'Relay installation failed: {error}')
