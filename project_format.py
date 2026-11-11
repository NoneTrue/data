"""Version-tolerant, compressed JSON CVP serialization."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from storage_manager import atomic_write


GZIP_MAGIC = b'\x1f\x8b'


def _walk_paths(node, pointer=''):
    if isinstance(node, dict):
        for key, value in node.items():
            escaped = str(key).replace('~', '~0').replace('/', '~1')
            child = pointer + '/' + escaped
            if isinstance(value, str) and (key in ('image1', 'image2') or any(tag in key.lower() for tag in ('path', 'image', 'file'))):
                yield child, value
            else:
                yield from _walk_paths(value, child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_paths(value, pointer + '/' + str(index))


def _set_pointer(node, pointer, value):
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer.strip('/').split('/')]
    parent = node
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    if parent is not None:
        if isinstance(parent, list):
            parent[int(parts[-1])] = value
        else:
            parent[parts[-1]] = value


def write_project(path, data, *, include_assets=False):
    """Write compressed CVP JSON, or a ZIP bundle with selected external assets."""
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')
    if not include_assets:
        compressed = gzip.compress(payload, compresslevel=6, mtime=0)
        atomic_write(path, lambda temp: temp.write_bytes(compressed), overwrite=True)
        return len(payload), len(compressed)

    document = json.loads(payload.decode('utf-8'))
    embedded = {}
    assets = {}
    for pointer, raw_path in _walk_paths(document):
        candidate = Path(raw_path).expanduser()
        if not candidate.is_file():
            continue
        resolved = str(candidate.resolve())
        member = assets.get(resolved)
        if member is None:
            digest = hashlib.sha256(resolved.encode('utf-8', errors='surrogatepass')).hexdigest()[:16]
            member = f'assets/{digest}_{candidate.name}'
            assets[resolved] = member
        embedded[pointer] = member
    document['embedded_assets'] = embedded
    payload = json.dumps(document, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')
    def write(temp):
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr('project.json', payload)
            for source, member in assets.items():
                archive.write(source, member)
    atomic_write(path, write, overwrite=True)
    return len(payload) + sum(Path(source).stat().st_size for source in assets), Path(path).stat().st_size


def read_project(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            data = json.loads(archive.read('project.json').decode('utf-8-sig'))
            embedded = data.pop('embedded_assets', {})
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
            cache = Path(tempfile.gettempdir()) / 'ChangeVision2026_CVP' / digest
            cache.mkdir(parents=True, exist_ok=True)
            for pointer, member in embedded.items():
                if member not in archive.namelist():
                    raise ValueError(f'أصل مضمّن مفقود في CVP: {member}')
                output = cache / Path(member).name
                if not output.is_file():
                    temporary = output.with_name(output.name + '.tmp')
                    with archive.open(member) as src, temporary.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(temporary, output)
                _set_pointer(data, pointer, str(output))
        return data
    raw = path.read_bytes()
    if raw.startswith(GZIP_MAGIC):
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('ملف المشروع لا يحتوي كائن بيانات صالحًا.')
    return data
