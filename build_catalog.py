"""Generate the module catalog from published Releases; never execute module code."""
import base64
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile

MAX_ZIP = 8 * 1024 * 1024
MAX_MANIFEST = 65536
MAX_APK = 256 * 1024 * 1024


class StripRedirectCredentials(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result:
            result.remove_header('Authorization')
        return result


def package_version(data, release, asset):
    if len(data) > MAX_ZIP:
        raise ValueError('Module ZIP exceeds 8 MiB')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 128:
            raise ValueError('Duplicate or excessive ZIP entries')
        info = archive.getinfo('module.json')
        if info.file_size > MAX_MANIFEST:
            raise ValueError('Module manifest exceeds limit')
        manifest = json.loads(archive.read(info))
        if manifest.get('schema') != 1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', manifest.get('id', '')):
            raise ValueError('Invalid module identity/schema')
        if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', manifest.get('version', '')):
            raise ValueError('Stable version must be major.minor.patch')
        if manifest.get('entry') not in names:
            raise ValueError('Missing module entry')
        if release['tag_name'] != manifest['id'] + '-v' + manifest['version']:
            raise ValueError('Release tag does not match module identity/version')
    sha = hashlib.sha256(data).hexdigest()
    if asset.get('digest') and asset['digest'] != 'sha256:' + sha:
        raise ValueError('GitHub asset digest mismatch')
    return manifest, {
        'version': manifest['version'],
        'name': manifest['name'],
        'description': manifest.get('description', ''),
        'minKernel': manifest.get('minKernel', 1),
        'targets': manifest.get('targets', []),
        'releaseUrl': release['html_url'],
        'publishedAt': release['published_at'],
        'notes': release.get('body') or '',
        'download': {'url': asset['browser_download_url'], 'apiUrl': asset['url'],
                     'sha256': sha, 'size': len(data)},
    }


def app_version(metadata, data, release, asset):
    if metadata.get('schema') != 1 or metadata.get('packageName') != 'com.applet.store':
        raise ValueError('Unexpected application identity')
    version = metadata.get('version', '')
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version):
        raise ValueError('Invalid app version')
    if release['tag_name'] != 'app-v' + version:
        raise ValueError('App tag/version mismatch')
    if type(metadata.get('versionCode')) is not int or metadata['versionCode'] <= 0:
        raise ValueError('Invalid app versionCode')
    if type(metadata.get('minSdk')) is not int or metadata['minSdk'] <= 0:
        raise ValueError('Invalid app minSdk')
    sha = hashlib.sha256(data).hexdigest()
    if len(data) > MAX_APK or metadata.get('size') != len(data) or metadata.get('sha256') != sha:
        raise ValueError('APK metadata does not match bytes')
    if asset.get('digest') and asset['digest'] != 'sha256:' + sha:
        raise ValueError('GitHub APK digest mismatch')
    return {'version': version, 'versionCode': metadata['versionCode'], 'minSdk': metadata['minSdk'],
            'packageName': metadata['packageName'], 'releaseUrl': release['html_url'],
            'publishedAt': release['published_at'], 'notes': release.get('body') or '',
            'download': {'url': asset['browser_download_url'], 'apiUrl': asset['url'],
                         'sha256': sha, 'size': len(data)}}


def build_catalog(repository, releases, download):
    groups = {}
    apps = []
    for release in releases:
        if release['draft'] or release['prerelease']:
            continue
        if release['tag_name'].startswith('app-v'):
            metadata_assets = [a for a in release['assets'] if a['name'] == 'app-release.json']
            apk_assets = [a for a in release['assets'] if a['name'].endswith('.apk')]
            if len(metadata_assets) != 1 or len(apk_assets) != 1:
                raise ValueError('App release requires one APK and app-release.json')
            if metadata_assets[0]['size'] > MAX_MANIFEST or apk_assets[0]['size'] > MAX_APK:
                raise ValueError('App release exceeds limits')
            metadata = json.loads(download(metadata_assets[0]))
            apps.append(app_version(metadata, download(apk_assets[0]), release, apk_assets[0]))
            continue
        for asset in release['assets']:
            if not asset['name'].endswith('.zip'):
                continue
            if asset['size'] > MAX_ZIP:
                raise ValueError('Module asset exceeds size limit')
            manifest, version = package_version(download(asset), release, asset)
            versions = groups.setdefault(manifest['id'], [])
            if any(row['version'] == version['version'] for row in versions):
                raise ValueError('Duplicate module version')
            versions.append(version)
    modules = []
    for module_id, versions in sorted(groups.items()):
        versions.sort(key=lambda row: tuple(map(int, row['version'].split('.'))), reverse=True)
        modules.append({'id': module_id, 'name': versions[0]['name'],
                        'latest': versions[0]['version'], 'versions': versions})
    if len({v['versionCode'] for v in apps}) != len(apps):
        raise ValueError('Duplicate app versionCode')
    apps.sort(key=lambda row: row['versionCode'], reverse=True)
    result = {'schema': 1, 'repository': repository, 'modules': modules}
    if apps:
        result['app'] = {'packageName': apps[0]['packageName'], 'latestVersionCode': apps[0]['versionCode'], 'versions': apps}
    return result


def main():
    repository = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GH_TOKEN']
    base = 'https://api.github.com/repos/' + repository
    opener = urllib.request.build_opener(StripRedirectCredentials())

    def request(path, payload=None, binary=False, limit=MAX_ZIP):
        headers = {'Authorization': 'Bearer ' + token, 'User-Agent': 'Applet-Modules-Catalog',
                   'Accept': 'application/octet-stream' if binary else 'application/vnd.github+json'}
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(base + path, data=body, headers=headers,
                                     method='GET' if payload is None else 'PUT')
        with opener.open(req, timeout=45) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Response exceeds size limit')
        return data if binary else json.loads(data)

    releases = []
    for page in range(1, 101):
        batch = request('/releases?per_page=100&page=' + str(page))
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError('Release pagination exceeds limit; refusing incomplete catalog')
    catalog = build_catalog(repository, releases,
                            lambda asset: request('/releases/assets/' + str(asset['id']), binary=True,
                                                  limit=MAX_APK if asset['name'].endswith('.apk') else MAX_ZIP))
    content = (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode()
    payload = {'message': 'Update module catalog from Releases',
               'content': base64.b64encode(content).decode(), 'branch': 'main'}
    try:
        previous = request('/contents/catalog.json?ref=main')
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        if base64.b64decode(previous['content']) == content:
            print('Catalog unchanged')
            return
        payload['sha'] = previous['sha']
    request('/contents/catalog.json', payload)
    print('Published catalog:', len(catalog['modules']), 'modules')


if __name__ == '__main__':
    main()
