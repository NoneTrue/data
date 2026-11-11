"""Download Magic Leap components from their owners after explicit license acceptance."""
import argparse
from pathlib import Path
import shutil
from setup_optional_runtime import ROOT, download, install_archive

SOURCES = {
    'SuperGlue': ('magicleap/SuperGluePretrainedNetwork', 'ddcf11f42e7e0732a0c4607648f9448ea8d73590'),
    'SuperPoint': ('magicleap/SuperPointPretrainedNetwork', '1fda796addba9b6f8e79d586a3699700a86b1cea'),
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accept-magicleap-license', action='store_true',
                        help='Confirm you reviewed and may use the owners\' noncommercial research licenses')
    parser.add_argument('--destination', type=Path, default=ROOT / 'runtime/GeoAlignAI')
    args = parser.parse_args()
    if not args.accept_magicleap_license:
        parser.error('Review the owners\' licenses first. Redistribution is not authorized by this script. '
                     'Use --accept-magicleap-license only if you may use these components under their terms.')
    for name, (repository, commit) in SOURCES.items():
        destination = args.destination / 'repos' / name
        if destination.exists():
            raise FileExistsError(f'Existing component is preserved: {destination}')
        archive_name = repository.rsplit('/', 1)[1]
        url = f'https://github.com/{repository}/archive/{commit}.zip'
        archive = download(url, ROOT / '.optional-downloads' / f'{archive_name}-{commit}.zip')
        install_archive(archive, destination, archive_name + '-' + commit)
        print(f'Downloaded from owner: {repository}@{commit}')
    models = args.destination / 'models'
    models.mkdir(exist_ok=True)
    for name in ['superpoint_v1.pth', 'superglue_outdoor.pth']:
        target = models / name
        if target.exists():
            raise FileExistsError(f'Existing model is preserved: {target}')
        source = args.destination / 'repos/SuperGlue/models/weights' / name
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming, outgoing)
    print('Downloaded for permitted local use only. No redistribution permission is granted.')
    print('PyTorch/LoFTR dependencies and backend integration are not installed or enabled.')

if __name__ == '__main__':
    main()
