"""Normalize distributable GeoAlignAI text and record exact source hashes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / 'optional-runtime-sources/GeoAlignAI'
entries = []
for path in sorted(root.rglob('*')):
    if not path.is_file() or path.name == 'FILE_MANIFEST.json':
        continue
    content = path.read_text(encoding='utf-8-sig').encode('utf-8')
    path.write_bytes(content)
    entries.append({'path': path.relative_to(root).as_posix(), 'bytes': len(content),
                    'sha256': hashlib.sha256(content).hexdigest()})
(root / 'FILE_MANIFEST.json').write_text(json.dumps(entries, indent=2) + '\n', encoding='utf-8')
print(f'{len(entries)} files; {sum(entry["bytes"] for entry in entries)} bytes')
