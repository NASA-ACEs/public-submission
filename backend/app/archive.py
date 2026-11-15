"""Result bundles fetched at startup, for a host with no way to upload files (``RESULTS_ARCHIVE_URL``).

Off unless ``RESULTS_DIR`` and ``RESULTS_ARCHIVE_URL`` are set. Each address is a ``.tar.gz`` (or ``.tar``) whose top
level holds bundle directories. A background thread downloads it into ``RESULTS_DIR/.incoming``, unpacks it there,
moves the bundles into ``RESULTS_DIR`` and rescans; the server answers meanwhile and keeps running if it fails.
An archive is not fetched again while the bundles it delivered are on disk (``RESULTS_DIR/.archives``).
"""
import hashlib
import json
import os
import shutil
import tarfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

RETRIES = 4
RETRY_WAIT_S = 5.0
TRANSPORT = None                    # tests put an httpx transport here


class ArchiveError(Exception):
    pass


class Fetcher:
    def __init__(self, registry):
        self.registry = registry
        self.urls = [u.strip() for u in os.getenv('RESULTS_ARCHIVE_URL', '').split(',') if u.strip()]
        self.token = os.getenv('RESULTS_ARCHIVE_TOKEN', '').strip()
        self.limit = int(float(os.getenv('RESULTS_ARCHIVE_MAX_MB', '4096')) * 1024 * 1024)
        self.status, self.detail = 'idle', ''
        self.thread = None

    @property
    def enabled(self):
        return bool(self.urls) and self.registry.enabled

    def start(self):
        if self.enabled:
            self.status = 'downloading'
            self.thread = threading.Thread(target=self.run, name='results-archive', daemon=True)
            self.thread.start()

    def run(self):
        root, done, failed = self.registry.root, [], []
        for index, url in enumerate(self.urls, 1):
            name = f'archive {index} ({Path(urlsplit(url).path).name or "unnamed"})'      # never the address: it may carry a secret
            try:
                root.mkdir(parents=True, exist_ok=True)
                done += self.fetch(url, root)
                self.registry.scan(force=True)
            except Exception as error:      # the demo and /v1/grid keep working
                reason = str(error) if isinstance(error, ArchiveError) else type(error).__name__
                failed.append(f'{name}: {reason}')
                print(f'[results-archive] {name} failed: {reason}', flush=True)
            finally:
                shutil.rmtree(root / '.incoming', ignore_errors=True)
        self.detail = '; '.join(failed) if failed else f'{len(done)} bundle(s) from {len(self.urls)} archive(s)'
        self.status = 'failed' if failed else 'ready'
        print(f'[results-archive] {self.status}: {self.detail}', flush=True)

    def fetch(self, url, root):
        """Names of the bundles this archive holds, after making sure they are in ``root``."""
        if urlsplit(url).scheme not in ('http', 'https'):
            raise ArchiveError('only http and https addresses are fetched')
        marker = root / '.archives' / (hashlib.sha256(url.encode()).hexdigest()[:16] + '.json')
        if marker.exists():
            known = json.loads(marker.read_text()).get('bundles', [])
            if known and all((root / b / 'result.json').exists() for b in known):
                return known
        work = root / '.incoming'
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        size, etag = self.download(url, work / 'archive')
        bundles = self.unpack(work / 'archive', work / 'unpacked', root)
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(json.dumps(dict(bytes=size, etag=etag, bundles=bundles, fetched_at=int(time.time()))))
        print(f'[results-archive] {size / 1e6:.0f} MB, bundles: {", ".join(bundles)}', flush=True)
        return bundles

    def download(self, url, target):
        headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
        for attempt in range(1, RETRIES + 1):
            try:
                with httpx.Client(transport=TRANSPORT, follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as client, \
                        client.stream('GET', url, headers=headers) as response:
                    if response.status_code >= 500 or response.status_code == 429:
                        raise httpx.TransportError(f'HTTP {response.status_code}')
                    if response.status_code != 200:
                        raise ArchiveError(f'HTTP {response.status_code}')
                    if int(response.headers.get('content-length') or 0) > self.limit:
                        raise ArchiveError('larger than RESULTS_ARCHIVE_MAX_MB')
                    size = 0
                    with open(target, 'wb') as out:
                        for chunk in response.iter_bytes(1 << 20):
                            size += len(chunk)
                            if size > self.limit:
                                raise ArchiveError('larger than RESULTS_ARCHIVE_MAX_MB')
                            out.write(chunk)
                    return size, response.headers.get('etag')
            except httpx.TransportError as error:
                if attempt == RETRIES:
                    raise ArchiveError(f'download failed after {RETRIES} attempts ({error if "HTTP" in str(error) else type(error).__name__})')
                time.sleep(RETRY_WAIT_S * attempt)

    def unpack(self, archive, work, root):
        try:
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                if any(m.issym() or m.islnk() or not (m.isfile() or m.isdir()) for m in members):
                    raise ArchiveError('the archive holds links or special files')
                if sum(m.size for m in members) > self.limit:
                    raise ArchiveError('unpacks to more than RESULTS_ARCHIVE_MAX_MB')
                tar.extractall(work, filter='data')              # refuses absolute paths and paths that leave the directory
        except (tarfile.TarError, OSError) as error:
            raise ArchiveError(f'not a usable tar archive ({type(error).__name__})')
        bundles = sorted(p.parent.name for p in work.glob('*/result.json'))
        if not bundles:
            raise ArchiveError('no bundle directory with a result.json at the top level')
        for name in bundles:
            if not (root / name).exists():                       # a bundle already there is kept
                os.replace(work / name, root / name)
        return bundles
