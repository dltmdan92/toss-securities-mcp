"""Offline platform storage/lock checks; no real Keychain items or API calls."""
import asyncio
import errno
import tempfile
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from vault import Vault


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self.items[service, account] = value


@unittest.skipUnless(sys.platform == "darwin", "native macOS backend and flock tests")
class VaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'store'

    def test_macos_roundtrip_namespaces_and_no_plaintext_files(self):
        backend = FakeKeychain()
        with patch('keyring.backends.macOS.Keyring', return_value=backend):
            first, second = Vault(self.root), Vault(self.root / 'other')
            self.assertIsNone(first.read('credentials'))
            first.write('credentials', {'client_secret': 'synthetic', 'enable_trading': False})
            self.assertEqual(first.read('credentials'), {'client_secret': 'synthetic', 'enable_trading': False})
            self.assertIsNone(second.read('credentials'))
            first.write('token', {'access_token': 'synthetic-token'})
            self.assertEqual(first.read('token'), {'access_token': 'synthetic-token'})
            self.assertFalse(self.root.exists())

    def test_macos_errors_are_sanitized_and_fail_closed(self):
        with patch('keyring.backends.macOS.Keyring', side_effect=RuntimeError('synthetic-secret')):
            for action in (lambda: Vault(self.root).read('credentials'),
                           lambda: Vault(self.root).write('credentials', {'secret': 'synthetic-secret'})):
                with self.assertRaises(RuntimeError) as caught:
                    action()
                self.assertNotIn('synthetic-secret', str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
            self.assertFalse(self.root.exists())

    async def test_lock_serializes_and_releases_after_exception(self):
        vault = Vault(self.root)
        events = []
        async def follower():
            async with vault.lock('token'):
                events.append('second')
        with self.assertRaises(ValueError):
            async with vault.lock('token'):
                task = asyncio.create_task(follower())
                await asyncio.sleep(0.15)
                self.assertEqual(events, [])
                raise ValueError('synthetic')
        await asyncio.wait_for(task, 2)
        self.assertEqual(events, ['second'])

    async def test_lock_unexpected_error_does_not_wait_or_leak_message(self):
        with patch('fcntl.flock', side_effect=OSError(errno.EBADF, 'synthetic-secret')):
            with self.assertRaises(RuntimeError) as caught:
                async with Vault(self.root).lock('token'):
                    self.fail('lock acquired despite failure')
            self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_malformed_json_or_nonobject_fails_closed(self):
        for payload in ('broken JSON', '[]', 'null', '"text"'):
            backend = FakeKeychain()
            with patch('keyring.backends.macOS.Keyring', return_value=backend):
                vault = Vault(self.root)
                backend.items[vault._keychain()[1], 'credentials'] = payload
                with self.assertRaises(RuntimeError):
                    vault.read('credentials')

    async def test_new_lock_permissions_are_private(self):
        async with Vault(self.root).lock('token'):
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.root / 'token.lock').stat().st_mode & 0o777, 0o600)

    async def test_lock_timeout_is_bounded(self):
        with patch('fcntl.flock', side_effect=BlockingIOError(errno.EAGAIN, 'busy')), patch('vault.time.monotonic', side_effect=[0, 31]):
            with self.assertRaises(RuntimeError):
                async with Vault(self.root).lock('token'):
                    self.fail('lock was not available')

    async def test_cancellation_releases_lock(self):
        entered = asyncio.Event()
        async def holder():
            async with Vault(self.root).lock('token'):
                entered.set()
                await asyncio.sleep(100)
        task = asyncio.create_task(holder())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(2):
            async with Vault(self.root).lock('token'):
                pass

    async def test_lock_serializes_across_processes(self):
        code = ("import asyncio,sys; from pathlib import Path; from vault import Vault\n"
                "async def main():\n"
                " async with Vault(Path(sys.argv[1])).lock('token'):\n"
                "  print('acquired', flush=True)\n"
                "asyncio.run(main())")
        async with Vault(self.root).lock('token'):
            process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(self.root),
                                                           stdout=asyncio.subprocess.PIPE)
            reader = asyncio.create_task(process.stdout.readline())
            await asyncio.sleep(0.3)
            self.assertFalse(reader.done())
        self.assertEqual(await asyncio.wait_for(reader, 3), b'acquired\n')
        self.assertEqual(await process.wait(), 0)


class WindowsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'store'

    def test_windows_storage_still_uses_dpapi(self):
        with patch('vault.sys.platform', 'win32'), patch('vault.crypt', side_effect=lambda value, decrypt=False: value):
            vault = Vault(self.root)
            vault.write('credentials', {'enable_trading': False})
            self.assertTrue((self.root / 'credentials.dpapi').is_file())
            self.assertEqual(vault.read('credentials'), {'enable_trading': False})
