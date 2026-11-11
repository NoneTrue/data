"""Copy and verify required resources. Unused weights/datasets are not packaged."""
import hashlib,json,shutil,argparse
from pathlib import Path


def stage(destination,include_otb=False):
    root=Path(__file__).resolve().parent;destination=Path(destination).resolve()
    if destination==root or root in destination.parents and 'runtime' in destination.relative_to(root).parts:
        raise ValueError('Build destination must be separate from source runtime.')
    source=root/'runtime';manifest=[];files=list((source/'icons').glob('*'))
    files.extend(p for p in (source/'GeoAlignAI').rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts)
    if include_otb:
        otb=source/'OTB-9.1.0-Win64'
        for directory in ('bin','lib','share'):
            files.extend(p for p in (otb/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
        files.extend(p for p in (otb/'LICENSE',otb/'README',otb/'otbenv.bat',otb/'otbenv.ps1') if p.is_file())
    for p in files:
        if not p.is_file():continue
        relative=p.relative_to(source);target=destination/'runtime'/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,target)
        def digest(path):
            with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
        original=digest(p);assert original==digest(target)
        manifest.append(dict(path=relative.as_posix(),sha256=original,bytes=p.stat().st_size))
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'runtime_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('destination');parser.add_argument('--include-otb',action='store_true')
    args=parser.parse_args();print('Verified resource copies:',len(stage(args.destination,args.include_otb)))
