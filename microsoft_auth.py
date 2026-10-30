"""Sign in with Microsoft for Outlook.com / Hotmail / Live / Microsoft 365 mailboxes.

Microsoft turned off password logins for Outlook.com IMAP and SMTP, so these
accounts use OAuth2: the owner signs in once in their browser, we keep only
the refresh token (DPAPI-sealed in the account's "password" field, like any
other mailbox secret) and swap it for short-lived access tokens that log in
to IMAP/SMTP with SASL XOAUTH2.

The sign-in is a standard authorization-code flow with PKCE and a loopback
redirect (http://localhost:<random port>), so no client secret exists.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Public client id of the "SimpleMail" app registration (not a secret).
CLIENT_ID = os.environ.get("SIMPLEMAIL_MS_CLIENT_ID", "cb0a1543-1e17-406f-91fb-32b8807f50e2")
AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
MAIL_SCOPES = ("https://outlook.office.com/IMAP.AccessAsUser.All "
               "https://outlook.office.com/SMTP.Send offline_access")
SIGN_IN_SCOPES = "openid profile email " + MAIL_SCOPES

# Outlook.com servers (Microsoft 365 work mailboxes accept the same hosts).
IMAP_HOST, IMAP_PORT = "outlook.office365.com", 993
SMTP_HOST, SMTP_PORT = "smtp-mail.outlook.com", 587

_lock = threading.Lock()
_cache = {}      # email -> (access_token, expires_at)
_rotated = {}    # email -> newest refresh token Microsoft handed back


class MicrosoftSignInError(Exception):
    pass


def _client_id():
    if not CLIENT_ID:
        raise MicrosoftSignInError("Microsoft sign-in is not set up in this build")
    return CLIENT_ID


def _post_token(form, opener=urllib.request.urlopen):
    body = urllib.parse.urlencode({"client_id": _client_id(), **form}).encode()
    request = urllib.request.Request(AUTHORITY + "/token", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {}
        if detail.get("error") == "invalid_grant":
            raise MicrosoftSignInError("Microsoft sign-in expired. Sign in again in Settings.") from None
        raise MicrosoftSignInError(detail.get("error_description", "").split("\r\n")[0]
                                   or f"Microsoft sign-in failed ({exc.code})") from None


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def _remember(email, result):
    with _lock:
        _cache[email.lower()] = (result["access_token"], time.time() + int(result.get("expires_in", 3600)))
        if result.get("refresh_token"):
            _rotated[email.lower()] = result["refresh_token"]


def sign_in(open_browser=None, timeout=300):
    """Run the browser sign-in. Returns {"email", "name", "refresh_token"}."""
    import webbrowser
    open_browser = open_browser or webbrowser.open
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    received = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in query.items()})
            ok = "code" in received and received.get("state") == state
            page = ("<h2>Signed in. You can close this tab and go back to SimpleMail.</h2>" if ok else
                    "<h2>Sign-in did not finish. Close this tab and try again in SimpleMail.</h2>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body style='font-family:sans-serif;padding:40px'>{page}</body></html>".encode())
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://localhost:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        open_browser(AUTHORITY + "/authorize?" + urllib.parse.urlencode({
            "client_id": _client_id(), "response_type": "code", "redirect_uri": redirect,
            "response_mode": "query", "scope": SIGN_IN_SCOPES, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
        }))
        if not done.wait(timeout):
            raise MicrosoftSignInError("Microsoft sign-in timed out")
    finally:
        server.shutdown()
        server.server_close()
    if received.get("state") != state:
        raise MicrosoftSignInError("Microsoft sign-in was interrupted")
    if "code" not in received:
        raise MicrosoftSignInError(received.get("error_description") or "Microsoft sign-in was cancelled")
    result = _post_token({"grant_type": "authorization_code", "code": received["code"],
                          "redirect_uri": redirect, "code_verifier": verifier, "scope": SIGN_IN_SCOPES})
    claims = _jwt_claims(result.get("id_token", ""))
    email = (claims.get("email") or claims.get("preferred_username") or "").strip()
    if "@" not in email or not result.get("refresh_token"):
        raise MicrosoftSignInError("Microsoft did not return a mailbox address")
    _remember(email, result)
    return {"email": email, "name": claims.get("name", ""), "refresh_token": _rotated[email.lower()]}


def access_token(email, refresh_token, opener=urllib.request.urlopen):
    """A valid access token, refreshed when within 5 minutes of expiry."""
    key = email.lower()
    with _lock:
        token, expires = _cache.get(key, ("", 0))
        refresh_token = _rotated.get(key) or refresh_token
    if token and expires - time.time() > 300:
        return token
    if not refresh_token:
        raise MicrosoftSignInError("Sign in with Microsoft again in Settings")
    result = _post_token({"grant_type": "refresh_token", "refresh_token": refresh_token,
                          "scope": MAIL_SCOPES}, opener)
    _remember(email, result)
    return result["access_token"]


def latest_refresh_token(email):
    """The newest refresh token issued this session, if Microsoft rotated it."""
    with _lock:
        return _rotated.get(email.lower())


def xoauth2(email, token):
    return f"user={email}\x01auth=Bearer {token}\x01\x01"
