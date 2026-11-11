"""Build redistributable optional assets and smoke-test OTB on Windows."""
from __future__ import annotations
import argparse
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setup_optional_runtime import download, install_archive, sha256

OTB_URL = 'https://www.orfeo-toolbox.org/packages/OTB-9.1.0-Win64.zip'
OTB_SHA256 = '48e604c72bb09bcd592a098a7a8fff06f082f3eddaf8fddc193922a78abd833e'

def build(output, archive=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    otb = output / 'OTB-9.1.0-Win64.zip'
    if archive:
        if otb.exists():
            raise FileExistsError(otb)
        shutil.copy2(archive, otb)
        if sha256(otb) != OTB_SHA256:
            raise ValueError('OTB source archive checksum mismatch')
    else:
        download(OTB_URL, otb, OTB_SHA256)
    source = ROOT / 'optional-runtime-sources/GeoAlignAI'
    manifest = json.loads((source / 'FILE_MANIFEST.json').read_text(encoding='utf-8'))
    for entry in manifest:
        path = source / entry['path']
        if sha256(path) != entry['sha256']:
            raise ValueError('GeoAlignAI source checksum mismatch: ' + entry['path'])
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    geo = output / 'GeoAlignAI-redistributable-sources-20261111.zip'
    with zipfile.ZipFile(geo, 'x', compression=zipfile.ZIP_DEFLATED) as archive_file:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                archive_file.write(path, 'GeoAlignAI/' + path.relative_to(source).as_posix())
    validation = output / 'validation'
    otb_root = install_archive(otb, validation / 'OTB-9.1.0-Win64', 'OTB-9.1.0-Win64')
    geo_root = install_archive(geo, validation / 'GeoAlignAI', 'GeoAlignAI')
    assert (geo_root / 'repos/LoFTR/LICENSE').is_file()
    assert not (geo_root / 'repos/SuperGlue').exists()
    assert not (geo_root / 'repos/SuperPoint').exists()
    smoke = {'otb_archive_sha256': sha256(otb), 'geoalignai_source_files': len(manifest),
             'geoalignai_syntax': 'PASS', 'restricted_components_republished': False}
    if sys.platform == 'win32':
        env = os.environ.copy()
        env['PATH'] = os.pathsep.join([str(otb_root / 'bin'), str(otb_root / 'lib'),
                                       os.path.join(os.environ['WINDIR'], 'System32')])
        env['OTB_APPLICATION_PATH'] = str(otb_root / 'lib/otb/applications')
        env['GDAL_DATA'] = str(otb_root / 'share/data')
        env['PROJ_DATA'] = env['PROJ_LIB'] = str(otb_root / 'share/proj')
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
        test_image = validation / 'otb-smoke.pgm'
        test_image.write_bytes(b'P5\n2 2\n255\n\x00\x40\x80\xff')
        process = subprocess.run([str(otb_root / 'bin/otbApplicationLauncherCommandLine.exe'),
                                  'ReadImageInfo', '-in', str(test_image)], env=env, capture_output=True,
                                 text=True, errors='replace', timeout=90,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (output / 'OTB-smoke.log').write_text(process.stdout + process.stderr, encoding='utf-8')
        if process.returncode != 0 or 'ReadImageInfo' not in process.stdout + process.stderr:
            raise RuntimeError('OTB launcher failed: ' + (process.stdout + process.stderr)[-3000:])
        smoke['otb_windows_launcher'] = 'PASS'
    components = {
        'otb': {'asset': otb.name, 'directory': 'OTB-9.1.0-Win64', 'archive_root': 'OTB-9.1.0-Win64',
                'sha256': sha256(otb), 'bytes': otb.stat().st_size, 'source': OTB_URL,
                'status': 'Complete official OTB 9.1.0 Windows archive, unchanged; CLI smoke tested.'},
        'geoalignai': {'asset': geo.name, 'directory': 'GeoAlignAI', 'archive_root': 'GeoAlignAI',
                      'sha256': sha256(geo), 'bytes': geo.stat().st_size,
                      'status': 'Redistributable LoFTR/YACS sources only. SuperGlue/SuperPoint and their weights are not republished.'}}
    (output / 'runtime-manifest.json').write_text(json.dumps({'components': components, 'validation': smoke}, indent=2), encoding='utf-8')
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{sha256(p)}  {p.name}\n' for p in [otb, geo, output / 'runtime-manifest.json']), encoding='utf-8')
    print(json.dumps(smoke, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--otb-archive', type=Path)
    args = parser.parse_args()
    build(args.output, args.otb_archive)
