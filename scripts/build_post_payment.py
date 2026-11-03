"""Build this project's standalone Go post-payment worker (no application startup)."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build():
    source = ROOT / 'go-post-payment'
    target = ROOT / 'bin' / ('post-payment.exe' if os.name == 'nt' else 'post-payment')
    files = sorted(p for p in source.iterdir() if p.suffix in ('.go', '.mod', '.sum'))
    digest = hashlib.sha256(b''.join(p.name.encode() + p.read_bytes() for p in files)).hexdigest()
    manifest = ROOT / '.local/go-post-payment-build.json'
    if target.is_file() and manifest.is_file():
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        if saved.get('source_sha256') == digest and saved.get('binary_sha256') == hashlib.sha256(target.read_bytes()).hexdigest():
            return target
    go = shutil.which('go')
    if not go and Path('C:/Program Files/Go/bin/go.exe').is_file():
        go = 'C:/Program Files/Go/bin/go.exe'
    if not go:
        raise RuntimeError('Go 1.25+ is required to build the post-payment worker')
    target.parent.mkdir(exist_ok=True)
    temporary = target.with_name(target.name + '.building')
    try:
        subprocess.run([go, 'build', '-trimpath', '-o', str(temporary), '.'], cwd=source,
                       env=dict(os.environ, CGO_ENABLED='0'), check=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text(json.dumps({'source_sha256': digest, 'binary_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    print('Go post-payment worker ready: ' + str(build()))
