import io
import json
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from cloud_mail import (CloudMail, NoRedirect, CloudSignInRequired, CloudAccessDenied,
                        CloudConfigurationError, CloudServiceError, CloudRequestError)


class CloudAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.adapter=CloudMail(self.temp.name)
        self.config={'api_url':'https://abcdefghij.execute-api.eu-west-1.amazonaws.com',
                     'auth_url':'https://mail-test.auth.eu-west-1.amazoncognito.com','client_id':'abcdefghijklmno'}
        self.adapter.cloud_configure(self.config)
    def test_cannot_redirect_bearer_to_arbitrary_server(self):
        with self.assertRaises(ValueError):self.adapter.cloud_configure({**self.config,'api_url':'https://attacker.example.com'})
        with self.assertRaises(ValueError):NoRedirect().redirect_request(None,None,302,'',{},'https://attacker.example.com')
    def test_public_config_does_not_return_sessions(self):
        self.adapter._save({**self.config,'refresh_token':{'windows_dpapi':'encrypted-secret'}})
        self.assertEqual(self.adapter.cloud_config(),{'configured':True})
    def test_request_refresh_stays_in_backend(self):
        self.adapter._save({**self.config,'refresh_token':{'windows_dpapi':'cipher'}})
        with patch('cloud_mail.protect',return_value='refresh-secret') as unlock,patch.object(self.adapter,'_exchange',return_value={'access_token':'access-secret','expires_in':300}),patch.object(self.adapter,'_fetch',return_value={'mailboxes':[]}) as fetch:
            self.assertEqual(self.adapter.cloud_request('GET','/mailboxes'),{'mailboxes':[]})
            self.assertEqual(fetch.call_args.args[3]['Authorization'],'Bearer access-secret')
            unlock.assert_called_once_with('cipher',True)
        self.assertNotIn('access-secret',self.adapter._path.read_text())

    def http_failure(self, status, body):
        error = HTTPError('https://fixture.invalid/secret', status, 'private provider detail', {},
                          io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode()))
        opener = Mock()
        opener.open.side_effect = error
        return opener, error

    def test_expired_refresh_grant_requires_reconnect_and_preserves_saved_session(self):
        self.adapter._save({**self.config, 'refresh_token': {'windows_dpapi': 'protected-refresh'}})
        before = self.adapter._path.read_bytes()
        opener, error = self.http_failure(400, {'error': 'invalid_grant', 'error_description': 'private token detail'})
        with patch('cloud_mail.protect', return_value='refresh-secret'), \
             patch('cloud_mail.build_opener', return_value=opener):
            with self.assertRaises(CloudSignInRequired) as result:
                self.adapter.cloud_request('GET', '/mailboxes/one/messages?folder=Inbox')
        self.assertIn('expired', str(result.exception))
        self.assertIn('Settings', str(result.exception))
        self.assertNotIn('private', str(result.exception))
        self.assertNotIn('refresh-secret', str(result.exception))
        self.assertNotIn('sending', str(result.exception))
        self.assertEqual(self.adapter._path.read_bytes(), before)
        self.assertIsNone(self.adapter._access_token)
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(error.closed)

    def test_oauth_client_rejection_is_configuration_failure(self):
        for code in ('invalid_client', 'unauthorized_client', 'invalid_request'):
            with self.subTest(code=code):
                opener, _ = self.http_failure(400, {'error': code, 'error_description': 'private detail'})
                with patch('cloud_mail.build_opener', return_value=opener):
                    with self.assertRaises(CloudConfigurationError) as result:
                        self.adapter._exchange(self.config, {'grant_type': 'refresh_token', 'refresh_token': 'secret'})
                self.assertIn('administrator', str(result.exception))
                self.assertNotIn('expired', str(result.exception))
                self.assertNotIn('private', str(result.exception))

    def test_mailbox_api_errors_distinguish_session_permission_and_bad_request(self):
        for status, expected in ((401, CloudSignInRequired), (403, CloudAccessDenied), (400, CloudRequestError)):
            with self.subTest(status=status):
                opener, _ = self.http_failure(status, {'error': 'invalid_grant', 'error_description': 'private detail'})
                with patch('cloud_mail.build_opener', return_value=opener):
                    with self.assertRaises(expected) as result:
                        self.adapter._fetch(self.config['api_url'] + '/mailboxes')
                self.assertNotIn('private', str(result.exception))
                if status == 403:
                    self.assertIn('assignment', str(result.exception))
                    self.assertNotIn('expired', str(result.exception))

    def test_transient_send_failure_is_sanitized_and_never_retried(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                self.adapter._access_token, self.adapter._expires = 'access-secret', float('inf')
                opener, _ = self.http_failure(status, {'error': 'private provider detail'})
                with patch('cloud_mail.build_opener', return_value=opener):
                    with self.assertRaises(CloudServiceError) as result:
                        self.adapter.cloud_request('POST', '/mailboxes/one/send', {'body': 'private message'})
                self.assertIn('check Sent', str(result.exception))
                self.assertNotIn('private', str(result.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_network_errors_do_not_disclose_provider_details_or_look_like_expiry(self):
        for failure in (URLError('private host or credential detail'), TimeoutError('private timeout detail')):
            with self.subTest(failure=type(failure).__name__):
                opener = Mock()
                opener.open.side_effect = failure
                with patch('cloud_mail.build_opener', return_value=opener):
                    with self.assertRaises(CloudServiceError) as result:
                        self.adapter._exchange(self.config, {'grant_type': 'refresh_token'})
                self.assertIn('connection', str(result.exception))
                self.assertNotIn('expired', str(result.exception))
                self.assertNotIn('private', str(result.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_oauth_error_body_is_bounded_and_malformed_body_is_not_exposed(self):
        for body in (b'private non-JSON response', b' ' * 8193 + b'{"error":"invalid_grant"}'):
            with self.subTest(length=len(body)):
                opener, error = self.http_failure(400, body)
                with patch('cloud_mail.build_opener', return_value=opener):
                    with self.assertRaises(CloudConfigurationError) as result:
                        self.adapter._exchange(self.config, {'grant_type': 'refresh_token'})
                self.assertNotIn('private', str(result.exception))
                self.assertTrue(error.closed)

    def test_oauth_transient_error_does_not_request_reauthentication(self):
        for status, code in ((429, 'slow_down'), (503, 'invalid_grant'), (400, 'temporarily_unavailable')):
            with self.subTest(status=status, code=code):
                opener, _ = self.http_failure(status, {'error': code})
                with patch('cloud_mail.build_opener', return_value=opener):
                    with self.assertRaises(CloudServiceError) as result:
                        self.adapter._exchange(self.config, {'grant_type': 'refresh_token'})
                self.assertNotIn('Reconnect', str(result.exception))
    def test_path_escape_and_nonmail_routes_rejected(self):
        for path in ('https://attacker.example.com','//attacker.example.com','/mailboxes/../admin','/admin'):
            with self.assertRaises(ValueError):self.adapter.cloud_request('GET',path)
    def test_desktop_login_receives_real_http_callback(self):
        callbacks = []
        def launch(url):
            state = parse_qs(urlsplit(url).query)['state'][0]
            def callback():
                with urlopen('http://localhost:8765/callback?state=' + state + '&code=fixture-code', timeout=5) as response:
                    callbacks.append(response.status)
            worker = threading.Thread(target=callback)
            worker.start()
            self.addCleanup(worker.join, 6)
        with patch('cloud_mail.webbrowser.open', side_effect=launch), patch('cloud_mail.protect', return_value='protected'), patch.object(self.adapter, '_exchange', return_value={'refresh_token':'refresh','access_token':'access','expires_in':300}) as exchange:
            self.assertEqual(self.adapter.cloud_login(), {'ok':True})
            self.assertEqual(exchange.call_args.args[1]['code'], 'fixture-code')
            self.assertEqual(self.adapter._read()['refresh_token'], {'windows_dpapi':'protected'})
        self.assertEqual(callbacks, [200])

    def test_reconfigure_drops_old_session(self):
        self.adapter._save({**self.config,'refresh_token':{'windows_dpapi':'cipher'}})
        self.adapter._access_token='old-token';self.adapter.cloud_configure(self.config)
        self.assertIsNone(self.adapter._access_token);self.assertNotIn('refresh_token',json.loads(self.adapter._path.read_text()))

if __name__=='__main__':unittest.main()
