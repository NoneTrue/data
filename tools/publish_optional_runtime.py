"""Publish validated optional assets using the workflow's repository token."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = 'NoneTrue/data'
TAG = 'optional-runtimes-20261111'

def request(method, url, payload=None, binary=False):
    headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
               'Accept': 'application/vnd.github+json', 'User-Agent': 'ChangeVision-runtime-publisher',
               'X-GitHub-Api-Version': '2022-11-28'}
    if binary:
        body = payload
        headers['Content-Type'] = 'application/octet-stream'
    else:
        body = json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers['Content-Type'] = 'application/json'
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=300) as r:
        return json.load(r)

def main(folder):
    folder = Path(folder)
    manifest = json.loads((folder / 'runtime-manifest.json').read_text())
    if manifest['validation'].get('otb_windows_launcher') != 'PASS':
        raise ValueError('A successful Windows OTB smoke test is required before publication')
    api = f'https://api.github.com/repos/{REPOSITORY}/releases'
    try:
        release = request('GET', api + '/tags/' + TAG)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        release = request('POST', api, {'tag_name': TAG, 'target_commitish': os.environ['GITHUB_SHA'],
                          'name': 'Optional runtimes: OTB 9.1.0 and GeoAlignAI redistributable sources',
                          'draft': True, 'prerelease': False,
                          'body': 'OTB is the unchanged official Windows archive, with a successful CLI smoke test. '
                                  'GeoAlignAI contains LoFTR/YACS sources only. Magic Leap SuperGlue/SuperPoint '
                                  'and their weights are not redistributed. See OPTIONAL_RUNTIME.md and '
                                  'fetch_restricted_geoalign.py for owner-source download instructions. '
                                  'These components do not activate a new ChangeVision analysis backend.'})
    files = [folder / c['asset'] for c in manifest['components'].values()]
    files += [folder / 'runtime-manifest.json', folder / 'SHA256SUMS.txt', folder / 'OTB-smoke.log']
    existing = {a['name']: a for a in release['assets']}
    for path in files:
        data = path.read_bytes()
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        if path.name in existing:
            asset = existing[path.name]
        else:
            if not release['draft']:
                raise ValueError('Published release is immutable; refusing to add/replace assets')
            url = f'https://uploads.github.com/repos/{REPOSITORY}/releases/{release["id"]}/assets?name=' + urllib.parse.quote(path.name)
            asset = request('POST', url, data, binary=True)
        if asset['size'] != len(data) or asset.get('digest') != digest:
            raise ValueError('GitHub asset checksum mismatch: ' + path.name)
        print(json.dumps({'asset': path.name, 'bytes': len(data), 'digest': digest}), flush=True)
    if release['draft']:
        release = request('PATCH', api + '/' + str(release['id']), {'draft': False})
    print('PUBLISHED ' + release['html_url'], flush=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder')
    main(parser.parse_args().folder)
