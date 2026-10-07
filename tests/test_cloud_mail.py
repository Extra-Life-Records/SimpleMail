import json
import tempfile
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from cloud_mail import CloudMail, NoRedirect


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
