"""Offline HTTP setup checks; synthetic values never reach Keychain or APIs."""
from http.client import HTTPConnection
from http.server import HTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import configure_web


class SetupFormTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        saved = self.saved

        class FakeVault:
            def __init__(self, root):
                pass

            def write(self, name, payload):
                saved.append((name, payload))

        for name, value in [('Vault', FakeVault), ('NONCE', 'synthetic-nonce'),
                            ('PATH', '/setup/synthetic-path')]:
            patcher = patch.object(configure_web, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = HTTPServer(('127.0.0.1', 0), configure_web.Handler)
        self.server.expected_host = f'127.0.0.1:{self.server.server_port}'
        self.origin = 'http://' + self.server.expected_host
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, method='GET', origin=None, nonce='synthetic-nonce', host=None,
                path='/setup/synthetic-path'):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        headers = {'Host': host or self.server.expected_host}
        body = None
        if method == 'POST':
            body = urlencode({'csrf': nonce, 'client_id': 'synthetic-id',
                              'client_secret': 'synthetic-secret', 'account_seq': '123'})
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            if origin is not None:
                headers['Origin'] = origin
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_form_preserves_origin_on_same_origin_post(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        # no-referrer makes browsers send Origin:null on a non-CORS form POST.
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn(b"method='post'", body)
        self.assertIn(b"name='csrf' value='synthetic-nonce'", body)

    def test_same_origin_post_saves_credentials_and_stops_one_use_server(self):
        status, _, body = self.request('POST', origin=self.origin)
        self.assertEqual(status, 200)
        self.assertIn(b'SAVED', body)
        self.assertEqual(self.saved, [('credentials', {
            'client_id': 'synthetic-id', 'client_secret': 'synthetic-secret',
            'account_seq': 123, 'enable_trading': False})])
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_cross_origin_null_and_missing_origin_cannot_save(self):
        for origin in ('https://attacker.example', 'null', None):
            with self.subTest(origin=origin):
                status, _, _ = self.request('POST', origin=origin)
                self.assertEqual(status, 403)
                self.assertEqual(self.saved, [])

    def test_wrong_nonce_cannot_save_even_with_same_origin(self):
        status, _, _ = self.request('POST', origin=self.origin, nonce='wrong-nonce')
        self.assertEqual(status, 403)
        self.assertEqual(self.saved, [])

    def test_wrong_host_or_path_cannot_save_even_with_same_origin(self):
        for options in ({'host': 'attacker.example'}, {'path': '/setup/wrong-path'}):
            with self.subTest(options=options):
                status, _, _ = self.request('POST', origin=self.origin, **options)
                self.assertEqual(status, 403)
                self.assertEqual(self.saved, [])


if __name__ == '__main__':
    unittest.main()
