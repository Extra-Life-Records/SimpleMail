"""SimpleMail AWS adapter. Cognito sessions only; never stores AWS credentials."""
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler

from mail_credentials import protect


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected service redirect')


class CloudMail:
    def __init__(self, directory):
        self._path = Path(directory) / 'aws-mail.json'
        self._window = None
        self._lock = threading.RLock()
        self._access_token, self._expires = None, 0

    def _read(self):
        if not self._path.exists(): return {}
        return json.loads(self._path.read_text(encoding='utf-8'))

    def _save(self, value):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self._path.parent, delete=False) as f:
            name = f.name
            json.dump(value, f); f.flush(); os.fsync(f.fileno())
        try: os.replace(name, self._path)
        finally:
            if os.path.exists(name): os.unlink(name)

    @staticmethod
    def validate(config):
        if not re.fullmatch(r'https://[a-z0-9]+\.execute-api\.eu-west-1\.amazonaws\.com', config.get('api_url', '')):
            raise ValueError('Use the Ireland API URL supplied by your administrator')
        if not re.fullmatch(r'https://[a-z0-9-]+\.auth\.eu-west-1\.amazoncognito\.com', config.get('auth_url', '')):
            raise ValueError('Use the Ireland Cognito login URL supplied by your administrator')
        if not re.fullmatch(r'[a-z0-9]{10,100}', config.get('client_id', '')):
            raise ValueError('Invalid client ID')

    def cloud_config(self):
        return {'configured': bool(self._read().get('api_url'))}

    def cloud_configure(self, config):
        self.validate(config)
        with self._lock:
            self._save({key: config[key] for key in ('api_url', 'auth_url', 'client_id')})
            self._access_token, self._expires = None, 0
        return {'ok': True}

    @staticmethod
    def _fetch(url, method='GET', data=None, headers=None):
        req = Request(url, data=data, method=method, headers=headers or {})
        try:
            with build_opener(NoRedirect()).open(req, timeout=45) as response:
                return json.loads(response.read(5000000))
        except HTTPError as exc:
            if exc.code in (401, 403): raise ValueError('Sign in again or ask your administrator to check mailbox access') from None
            raise ValueError('Mailbox request failed. If sending, check Sent before retrying.') from None

    def _exchange(self, cfg, data):
        return self._fetch(cfg['auth_url'] + '/oauth2/token', 'POST',
                           urlencode({'client_id': cfg['client_id'], **data}).encode(),
                           {'Content-Type': 'application/x-www-form-urlencoded'})

    def cloud_login(self):
        cfg = self._read(); self.validate(cfg)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        answer = {}
        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                params = parse_qs(urlsplit(self._path).query)
                valid = (urlsplit(self._path).path == '/callback' and params.get('state') == [state]
                         and self.headers.get('Host') == 'localhost:8765' and params.get('code'))
                if valid: answer['code'] = params['code'][0]
                self.send_response(200 if valid else 400)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Cache-Control', 'no-store'); self.end_headers()
                self.wfile.write(b'Signed in. Return to SimpleMail.' if valid else b'Invalid sign-in callback.')
        with HTTPServer(('127.0.0.1', 8765), Callback) as server:
            server.timeout = 1
            webbrowser.open(cfg['auth_url'] + '/oauth2/authorize?' + urlencode({
                'response_type': 'code', 'client_id': cfg['client_id'], 'redirect_uri': 'http://localhost:8765/callback',
                'scope': 'openid email aws.cognito.signin.user.admin', 'state': state,
                'code_challenge': challenge, 'code_challenge_method': 'S256'}))
            deadline = time.monotonic() + 240
            while not answer and time.monotonic() < deadline: server.handle_request()
        if not answer: raise ValueError('Sign-in timed out. Please try again.')
        result = self._exchange(cfg, {'grant_type': 'authorization_code', 'code': answer['code'],
                                     'code_verifier': verifier, 'redirect_uri': 'http://localhost:8765/callback'})
        with self._lock:
            cfg['refresh_token'] = {'windows_dpapi': protect(result['refresh_token'])}
            self._save(cfg)
            self._access_token = result['access_token']; self._expires = time.time() + result['expires_in'] - 30
        return {'ok': True}

    def cloud_request(self, method, path, data=None):
        if method not in ('GET', 'POST', 'PATCH') or not re.fullmatch(r'/mailboxes(?:/[a-z0-9_/-]+)?(?:\?[^#\r\n]*)?', path):
            raise ValueError('Invalid mailbox request')
        with self._lock:
            cfg = self._read(); self.validate(cfg)
            if not self._access_token or time.time() >= self._expires:
                if not cfg.get('refresh_token'): raise ValueError('Sign in to your AWS inbox')
                refresh = protect(cfg['refresh_token']['windows_dpapi'], True)
                result = self._exchange(cfg, {'grant_type': 'refresh_token', 'refresh_token': refresh})
                self._access_token = result['access_token']; self._expires = time.time() + result['expires_in'] - 30
            token = self._access_token
        return self._fetch(cfg['api_url'] + path, method, json.dumps(data).encode() if data is not None else None,
                           {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})

    def cloud_logout(self):
        with self._lock:
            cfg = self._read()
            stored = cfg.pop('refresh_token', None)
            self._save(cfg); self._access_token, self._expires = None, 0
        if stored:
            self.validate(cfg)
            req = Request(cfg['auth_url'] + '/oauth2/revoke', method='POST', data=urlencode({
                'client_id': cfg['client_id'], 'token': protect(stored['windows_dpapi'], True)}).encode(),
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
            with build_opener(NoRedirect()).open(req, timeout=30): pass
        return {'ok': True}

    def cloud_save_attachment(self, file):
        import webview
        name = Path(file['name'].replace('\\', '/')).name
        payload = base64.b64decode(file['data'], validate=True)
        if len(payload) > 3 * 1024 * 1024: raise ValueError('Attachment exceeds download bound')
        if not self._window: raise ValueError('No inbox window')
        target = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
        if target:
            Path(target if isinstance(target, str) else target[0]).write_bytes(payload)
        return {'saved': bool(target)}


def open_inbox(directory):
    import webview
    api = CloudMail(directory)
    api._window = webview.create_window('Extra Life Records Mail - SimpleMail',
        str(Path(__file__).parent / 'cloud' / 'src' / 'static' / 'index.html'), js_api=api,
        width=1180, height=800, min_size=(760, 600))
    return {'ok': True}
