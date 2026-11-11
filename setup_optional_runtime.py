"""Install published optional runtime archives without replacing existing folders."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
TAG = 'optional-runtimes-20261111'
REPOSITORY = 'NoneTrue/data'

def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def download(url, destination, expected=None):
    destination = Path(destination)
    if destination.exists():
        if expected and sha256(destination) == expected:
            return destination
        raise FileExistsError(f'Refusing to replace cached file: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'ChangeVision-optional-runtime'})
            with urllib.request.urlopen(request, timeout=120) as response:
                shutil.copyfileobj(response, stream)
            stream.flush()
        except Exception:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        if expected and sha256(temporary) != expected:
            raise ValueError('Downloaded archive does not match its SHA-256')
        os.rename(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination

def install_archive(archive, destination, archive_root):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f'Existing component is preserved: {destination}')
    with zipfile.ZipFile(archive) as source:
        members = [m for m in source.infolist() if not m.is_dir()]
        if not members or sum(m.file_size for m in members) > 4 * 1024**3:
            raise ValueError('Archive is empty or exceeds the extraction limit')
        paths = []
        for member in members:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in member.filename or ':' in member.filename:
                raise ValueError('Unsafe ZIP path')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('ZIP symbolic links are not supported')
            paths.append(path)
        strip = all(path.parts[0] == archive_root for path in paths)
        relative = [PurePosixPath(*path.parts[1:]) if strip else path for path in paths]
        names = [str(path).casefold() for path in relative]
        if len(set(names)) != len(names) or any(str(p) == '.' for p in relative):
            raise ValueError('Duplicate or empty ZIP paths')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.optional-', dir=destination.parent) as folder:
            staging = Path(folder) / 'payload'
            staging.mkdir()
            for member, path in zip(members, relative):
                target = staging.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as incoming, target.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
            staging.rename(destination)
    return destination

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', choices=['otb', 'geoalignai', 'all'], default='all')
    parser.add_argument('--destination', type=Path, default=ROOT / 'runtime')
    args = parser.parse_args()
    api = f'https://api.github.com/repos/{REPOSITORY}/releases/tags/{TAG}'
    with urllib.request.urlopen(urllib.request.Request(api, headers={'User-Agent': 'ChangeVision'}), timeout=60) as r:
        release = json.load(r)
    assets = {a['name']: a for a in release['assets']}
    manifest_asset = assets['runtime-manifest.json']
    with urllib.request.urlopen(manifest_asset['browser_download_url'], timeout=60) as r:
        manifest = json.load(r)
    cache = ROOT / '.optional-downloads'
    components = ['otb', 'geoalignai'] if args.component == 'all' else [args.component]
    for key in components:
        info = manifest['components'][key]
        destination = args.destination / info['directory']
        if destination.exists():
            raise FileExistsError(f'Existing component is preserved: {destination}')
        asset = assets[info['asset']]
        digest = asset.get('digest')
        if digest and digest != 'sha256:' + info['sha256']:
            raise ValueError('Release metadata checksum does not match manifest')
        archive = download(asset['browser_download_url'], cache / info['asset'], info['sha256'])
        install_archive(archive, destination, info['archive_root'])
        print(f'Installed {key}: {destination}')
        print(info['status'])
    print('These resources do not enable a new analysis backend in the current UI.')

if __name__ == '__main__':
    main()
