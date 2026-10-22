"""Privacy/security regressions. Standard library, synthetic fixtures, no QQ/network services."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

import ingest
import server
from backend import Store


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('PLANNER_')}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_main(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch('sys.argv', ['server.py', '--no-reminders', *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                server.main()
            except SystemExit as exc:
                return exc.code, output.getvalue(), errors.getvalue()
        return 0, output.getvalue(), errors.getvalue()

    def test_new_database_is_empty_with_or_without_legacy_flag(self):
        for flags in ([], ['--no-seed']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temp:
                data = Path(temp) / 'data'
                # Ignore any seed argument in the spy so a regression never reads a seed file.
                with mock.patch.object(server, 'Store', side_effect=lambda path, *seed: Store(path)) as create, mock.patch.object(server.LocalServer, 'serve_forever', side_effect=KeyboardInterrupt):
                    code, _, errors = self.run_main('--port', '0', '--data-dir', str(data), *flags)
                self.assertEqual(code, 0, errors)
                create.assert_called_once_with(data)
                store = Store(data)
                try:
                    self.assertEqual(store.snapshot_tasks(), [])
                finally:
                    store.close()

    def test_non_loopback_bind_is_rejected_before_storage_or_socket(self):
        for host in ('0.0.0.0', '192.0.2.1', '::', '2001:db8::1'):
            with self.subTest(host=host), mock.patch.object(server.ThreadingHTTPServer, '__init__') as bind:
                with self.assertRaisesRegex(ValueError, 'loopback'):
                    server.LocalServer((host, 0))
                bind.assert_not_called()

    def test_resolved_hostname_cannot_hide_non_loopback_address(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.1', 0)),
        ]
        with mock.patch.object(server.socket, 'getaddrinfo', return_value=addresses), mock.patch.object(server.ThreadingHTTPServer, '__init__') as bind:
            with self.assertRaisesRegex(ValueError, 'loopback'):
                server.LocalServer(('mixed.invalid', 0))
            bind.assert_not_called()

    def test_proxy_requires_password_and_explicit_https_origin(self):
        for password, origin in ((None, 'https://planner.example'), ('test-only', None), ('test-only', 'http://planner.example')):
            with self.subTest(password=bool(password), origin=origin), mock.patch.object(server.ThreadingHTTPServer, '__init__') as bind:
                with self.assertRaises(ValueError):
                    server.LocalServer(('127.0.0.1', 0), access_password=password, trust_proxy=True, public_origin=origin)
                bind.assert_not_called()

    def test_public_origin_is_exact_and_has_valid_hostname(self):
        invalid = (
            'https://planner.example/', 'https://planner.example/path',
            'https://user:pass@planner.example', 'https://@planner.example',
            'https://planner.example?x=1', 'https://planner.example?',
            'https://planner.example#', 'https://planner.example:bad',
            'https://planner.example:70000', 'https://planner.example:0',
            'https://planner.example:', 'https://planner.example\\evil',
            'https://bad host.example', 'https://-bad.example',
            'https://bad..example', 'https://[broken',
            'https://planner.example\n', 'https:///missing',
        )
        for origin in invalid:
            with self.subTest(origin=origin), mock.patch.dict(os.environ, {'PLANNER_PUBLIC_ORIGIN': origin}), mock.patch.object(server.ThreadingHTTPServer, '__init__') as bind:
                with self.assertRaises(ValueError):
                    server.LocalServer(('127.0.0.1', 0))
                bind.assert_not_called()

    def test_bad_startup_configuration_has_clean_errors_and_no_store(self):
        cases = (
            ({}, ['--bind', '0.0.0.0']),
            ({}, ['--trust-proxy']),
            ({'PLANNER_ACCESS_PASSWORD': 'test-only'}, ['--trust-proxy']),
            ({'PLANNER_PUBLIC_ORIGIN': 'https://planner.example:bad'}, []),
            ({'PLANNER_PUBLIC_ORIGIN': 'https://[broken'}, []),
            ({'PLANNER_PORT': 'not-a-port'}, []),
        )
        for env, args in cases:
            with self.subTest(env=list(env), args=args), mock.patch.dict(os.environ, env), mock.patch.object(server, 'Store') as store:
                code, _, errors = self.run_main('--port', '0', *args) if 'PLANNER_PORT' not in env else self.run_main(*args)
                self.assertNotEqual(code, 0)
                self.assertTrue(errors)
                self.assertNotIn('Traceback', errors)
                store.assert_not_called()

    def test_loopback_binds_and_https_proxy_configuration_work(self):
        for host in ('127.0.0.1', 'localhost'):
            with self.subTest(host=host):
                instance = server.LocalServer((host, 0))
                instance.server_close()
        with mock.patch.dict(os.environ, {'PLANNER_PUBLIC_ORIGIN': 'https://planner.example:8443'}):
            instance = server.LocalServer(('127.0.0.1', 0), access_password='test-only', trust_proxy=True)
            self.assertEqual(instance.public_origin, 'https://planner.example:8443')
            instance.server_close()


class ResponseTests(unittest.TestCase):
    def test_local_ui_csp_and_proxy_host_origin_checks(self):
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith('PLANNER_')}, clear=True), tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'data')
            instance = server.LocalServer(('127.0.0.1', 0), store=store, access_password='test-only', trust_proxy=True, public_origin='https://planner.example')
            thread = threading.Thread(target=instance.serve_forever, daemon=True)
            thread.start()
            try:
                def request(method, path, headers, body=None):
                    conn = http.client.HTTPConnection('127.0.0.1', instance.server_port, timeout=5)
                    try:
                        conn.request(method, path, body=body, headers=headers)
                        response = conn.getresponse()
                        payload = response.read()
                        return response.status, dict(response.getheaders()), payload
                    finally:
                        conn.close()
                status, headers, _ = request('GET', '/', {'Host': 'planner.example'})
                self.assertEqual(status, 200)
                csp = headers.get('Content-Security-Policy', '')
                for directive in ("default-src 'self'", "script-src 'self'", "connect-src 'self'", "img-src 'self' data:", "object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'"):
                    self.assertIn(directive, csp)
                self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
                self.assertEqual(request('GET', '/', {'Host': 'other.example'})[0], 403)
                login_headers = {'Host': 'planner.example', 'X-Forwarded-Proto': 'https', 'Origin': 'https://other.example', 'Content-Type': 'application/json'}
                self.assertEqual(request('POST', '/api/v1/session', login_headers, json.dumps({'accessPassword': 'test-only'}))[0], 403)
                login_headers['Origin'] = 'https://planner.example'
                status, headers, _ = request('POST', '/api/v1/session', login_headers, json.dumps({'accessPassword': 'test-only'}))
                self.assertEqual(status, 200)
                self.assertIn('; Secure', headers['Set-Cookie'])
            finally:
                instance.shutdown()
                thread.join()
                instance.server_close()
                store.close()


class ImportPrivacyTests(unittest.TestCase):
    def test_remote_https_requires_approval_for_every_host(self):
        for host in ('planner.example.com', 'example.invalid'):
            for port in ('', ':8443'):
                base = 'https://' + host + port
                with self.subTest(base=base):
                    with self.assertRaisesRegex(ValueError, 'remote_api_requires_explicit_approval'):
                        ingest.API(base)
                    self.assertEqual(ingest.API(base, allow_remote=True).base, base)

    def test_loopback_api_needs_no_approval_but_remote_http_is_refused(self):
        for host in ('localhost', '127.0.0.1', '[::1]'):
            for scheme in ('http', 'https'):
                base = scheme + '://' + host + ':8766'
                self.assertEqual(ingest.API(base).base, base)
        for allow in (False, True):
            with self.assertRaisesRegex(ValueError, 'http_allowed_loopback_only'):
                ingest.API('http://example.invalid', allow_remote=allow)

    def test_scan_requires_root_before_contacting_api_or_scanning(self):
        with mock.patch('sys.argv', ['ingest.py', 'scan']), mock.patch.object(ingest, 'API') as api, mock.patch.object(ingest, 'scan') as scan, mock.patch.object(ingest, 'log'), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit:
                ingest.main()
            self.assertEqual(exit.exception.code, 2)
            api.assert_not_called()
            scan.assert_not_called()

    def test_scan_and_submit_default_to_local_api(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'sources'
            root.mkdir()
            (root / 'notice.txt').write_text('Synthetic course notice.', encoding='utf-8')
            out = Path(temp) / 'scan'
            batch, manifest = Path(temp) / 'batch.json', Path(temp) / 'manifest.json'
            batch.write_text('{}', encoding='utf-8')
            manifest.write_text('{}', encoding='utf-8')
            for args in (['scan', '--root', str(root), '--out', str(out)], ['submit', '--batch', str(batch), '--manifest', str(manifest)]):
                with self.subTest(command=args[0]), mock.patch('sys.argv', ['ingest.py', *args]), mock.patch.object(ingest, 'API') as api, mock.patch.object(ingest, 'validate', return_value={'batchId': 'synthetic', 'files': []}), mock.patch.object(ingest, 'log', return_value=Path(temp) / 'log'), contextlib.redirect_stdout(io.StringIO()):
                    api.return_value.request.side_effect = lambda route, *a: {'files': [], 'token': 'synthetic'}
                    self.assertEqual(ingest.main(), 0)
                    self.assertEqual(api.call_args.args[0], 'http://127.0.0.1:8766')


if __name__ == '__main__':
    unittest.main()
