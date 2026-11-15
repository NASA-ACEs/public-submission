"""Bundles fetched at startup from RESULTS_ARCHIVE_URL: no network, the archive is served by a stub transport."""
import asyncio
import io
import os
import tarfile
import tempfile
from pathlib import Path

import httpx

os.environ['GRID_AUTO_SYNC'] = '0'
import app.archive as archive
try:
    from test_results import Base, ca_bundle, smm_bundle
except ImportError:                               # run as tests.test_archive
    from tests.test_results import Base, ca_bundle, smm_bundle

URL = 'https://files.example/aces/results.tar.gz?sig=SECRET'


def packed(make, evil=None):
    """A .tar.gz of the bundles ``make`` writes, as bytes; ``evil`` adds a member with that path."""
    with tempfile.TemporaryDirectory() as tmp:
        make(tmp)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz', compresslevel=1) as tar:
            for directory in sorted(Path(tmp).iterdir()):
                tar.add(directory, arcname=directory.name)
            if evil:
                info = tarfile.TarInfo(evil)
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
        return buffer.getvalue()


class ArchiveBase(Base):
    status, body, token = 200, b'', None

    def bundles(self, root):                      # RESULTS_DIR starts empty
        pass

    async def asyncSetUp(self):
        self.calls = []

        def serve(request):
            self.calls.append(request)
            return httpx.Response(self.status, content=self.body, headers={'etag': '"v1"'})

        self.saved = {k: os.environ.get(k) for k in ('RESULTS_ARCHIVE_URL', 'RESULTS_ARCHIVE_TOKEN')}
        os.environ['RESULTS_ARCHIVE_URL'] = URL
        os.environ.pop('RESULTS_ARCHIVE_TOKEN', None) if self.token is None else os.environ.__setitem__('RESULTS_ARCHIVE_TOKEN', self.token)
        archive.TRANSPORT, self.wait, archive.RETRY_WAIT_S = httpx.MockTransport(serve), archive.RETRY_WAIT_S, 0.01
        await super().asyncSetUp()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        archive.TRANSPORT, archive.RETRY_WAIT_S = None, self.wait
        for key, value in self.saved.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)

    async def settled(self):
        for _ in range(600):
            health = (await self.get('/health')).json()
            if health['results_archive'] != 'downloading':
                return health
            await asyncio.sleep(.05)
        self.fail('the archive never settled')


class FetchTests(ArchiveBase):
    body = packed(lambda tmp: (ca_bundle(tmp), smm_bundle(tmp)))
    token = 'TOKEN'

    async def test_bundles_arrive_and_are_served(self):
        health = await self.settled()
        self.assertEqual((health['results_archive'], health['real_results'], health['latest']), ('ready', 2, 'smm-20261101-nisar'))
        self.assertNotIn('SECRET', health['results_archive_detail'])
        self.assertEqual((await self.get(self.ca)).status_code, 200)
        self.assertEqual((await self.get(self.latest)).json()['result_id'], 'smm-20261101-nisar')
        self.assertEqual(self.calls[0].headers['authorization'], 'Bearer TOKEN')
        self.assertFalse((self.root / '.incoming').exists())

    async def test_not_fetched_again_while_the_bundles_are_there(self):
        await self.settled()
        again = archive.Fetcher(self.registry())
        again.run()
        self.assertEqual((again.status, len(self.calls)), ('ready', 1))
        (self.root / 'ca' / 'result.json').unlink()             # a bundle went missing: fetched again
        once_more = archive.Fetcher(self.registry())
        once_more.run()
        self.assertEqual(len(self.calls), 2)

    def registry(self):
        import app.main as api
        return api.app.state.results


class UnsafeArchiveTests(ArchiveBase):
    body = packed(ca_bundle, evil='../escaped.txt')

    async def test_a_path_that_leaves_the_directory_is_refused(self):
        health = await self.settled()
        self.assertEqual((health['results_archive'], health['real_results']), ('failed', 0))
        self.assertFalse((self.root.parent / 'escaped.txt').exists())
        self.assertEqual([p.name for p in self.root.iterdir() if not p.name.startswith('.')], [])


class MissingArchiveTests(ArchiveBase):
    status = 404

    async def test_a_missing_archive_fails_and_the_server_keeps_answering(self):
        health = await self.settled()
        self.assertEqual(health['results_archive'], 'failed')
        self.assertIn('HTTP 404', health['results_archive_detail'])
        self.assertNotIn('SECRET', health['results_archive_detail'])
        self.assertEqual(len(self.calls), 1)                     # a 404 is not retried
        self.assertEqual((await self.get('/v1/burnability/results/forest-demo-v1')).status_code, 200)


class FlakyHostTests(ArchiveBase):
    status = 503

    async def test_a_failing_host_is_retried_then_given_up(self):
        health = await self.settled()
        self.assertEqual((health['results_archive'], len(self.calls)), ('failed', archive.RETRIES))


class OffByDefaultTests(Base):
    async def test_no_address_no_field(self):
        self.assertNotIn('results_archive', (await self.get('/health')).json())
