import base64, json, tempfile, threading, unittest, urllib.parse, urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch
import mailapp
import microsoft_auth


def id_token(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"x.{body}.y"


class Reply:
    def __init__(self, data): self.data = json.dumps(data).encode()
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): return False


class MicrosoftAuthTests(unittest.TestCase):
    def setUp(self):
        for name, value in [("CLIENT_ID", "test-client"), ("_cache", {}), ("_rotated", {})]:
            p = patch.object(microsoft_auth, name, value); p.start(); self.addCleanup(p.stop)

    def test_xoauth2_format(self):
        self.assertEqual(microsoft_auth.xoauth2("a@b.com", "T"), "user=a@b.com\x01auth=Bearer T\x01\x01")

    def test_access_token_refreshes_once_then_caches_and_keeps_rotated_token(self):
        calls = []
        def opener(request, timeout):
            calls.append(urllib.parse.parse_qs(request.data.decode()))
            return Reply({"access_token": f"A{len(calls)}", "expires_in": 3600, "refresh_token": "R2"})
        self.assertEqual(microsoft_auth.access_token("Me@Live.co.uk", "R1", opener), "A1")
        self.assertEqual(microsoft_auth.access_token("me@live.co.uk", "R1", opener), "A1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["refresh_token"], ["R1"])
        self.assertEqual(calls[0]["client_id"], ["test-client"])
        self.assertEqual(microsoft_auth.latest_refresh_token("me@live.co.uk"), "R2")

    def test_expired_sign_in_is_explained(self):
        import urllib.error, io
        def opener(request, timeout):
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        with self.assertRaisesRegex(microsoft_auth.MicrosoftSignInError, "Sign in again"):
            microsoft_auth.access_token("me@live.co.uk", "R1", opener)

    def test_sign_in_round_trip_through_loopback(self):
        seen = {}
        def browser(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            seen.update(query)
            target = query["redirect_uri"][0] + "/?" + urllib.parse.urlencode({"code": "C", "state": query["state"][0]})
            threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read()).start()
        def token(form, opener=None):
            seen["form"] = form
            return {"access_token": "A", "refresh_token": "R", "expires_in": 3600,
                    "id_token": id_token({"email": "denby182@hotmail.co.uk", "name": "Denby"})}
        with patch.object(microsoft_auth, "_post_token", token):
            result = microsoft_auth.sign_in(browser, timeout=10)
        self.assertEqual(result, {"email": "denby182@hotmail.co.uk", "name": "Denby", "refresh_token": "R"})
        self.assertEqual(seen["code_challenge_method"], ["S256"])
        self.assertIn("IMAP.AccessAsUser.All", seen["scope"][0])
        self.assertEqual(seen["form"]["code"], "C")
        self.assertTrue(seen["form"]["redirect_uri"].startswith("http://localhost:"))

    def test_sign_in_rejects_wrong_state(self):
        def browser(url):
            redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
            threading.Thread(target=lambda: urllib.request.urlopen(redirect + "/?code=C&state=forged", timeout=5).read()).start()
        with patch.object(microsoft_auth, "_post_token", MagicMock()) as token:
            with self.assertRaises(microsoft_auth.MicrosoftSignInError):
                microsoft_auth.sign_in(browser, timeout=10)
            token.assert_not_called()


class MailappMicrosoftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        for name, value in [("CONFIG_DIR", Path(self.temp.name)), ("CONFIG_FILE", self.path)]:
            p = patch.object(mailapp, name, value); p.start(); self.addCleanup(p.stop)
        for name, value in [("CLIENT_ID", "test-client"), ("_cache", {}), ("_rotated", {})]:
            p = patch.object(microsoft_auth, name, value); p.start(); self.addCleanup(p.stop)
        self.acct = mailapp.normalize_account({"id": "hm", "email": "me@hotmail.co.uk", "auth": "microsoft",
                                               "password": "R1", "imap_host": "outlook.office365.com"})
        microsoft_auth._cache["me@hotmail.co.uk"] = ("TOKEN", 9e18)

    def test_imap_uses_xoauth2(self):
        imap = MagicMock(); imap.list.return_value = ("OK", [])
        with patch("imaplib.IMAP4_SSL", return_value=imap):
            mailapp.connect_imap(self.acct)
        imap.login.assert_not_called()
        mech, callback = imap.authenticate.call_args[0]
        self.assertEqual(mech, "XOAUTH2")
        self.assertEqual(callback(b""), b"user=me@hotmail.co.uk\x01auth=Bearer TOKEN\x01\x01")

    def test_folder_list_reads_bare_outlook_and_quoted_fasthosts_names(self):
        imap = MagicMock(); imap.list.return_value = ("OK", [
            b'(\HasNoChildren) "/" Inbox', b'(\HasNoChildren \Sent) "/" Sent',
            b'(\HasNoChildren) "/" "Deleted Items"', b'(\HasNoChildren \Junk) "." "INBOX.Junk"'])
        with patch("imaplib.IMAP4_SSL", return_value=imap):
            _, folders, sent = mailapp.connect_imap(self.acct)
        self.assertEqual(folders, ["Inbox", "Sent", "Deleted Items", "INBOX.Junk"])
        self.assertEqual(sent, "Sent")

    def test_smtp_uses_xoauth2_and_password_accounts_unchanged(self):
        smtp = MagicMock()
        mailapp.smtp_login(smtp, self.acct)
        mech, callback = smtp.auth.call_args[0]
        self.assertEqual((mech, callback()), ("XOAUTH2", "user=me@hotmail.co.uk\x01auth=Bearer TOKEN\x01\x01"))
        smtp.login.assert_not_called()
        plain = mailapp.normalize_account({"email": "a@b.com", "password": "pw"})
        smtp = MagicMock(); mailapp.smtp_login(smtp, plain)
        smtp.login.assert_called_once_with("a@b.com", "pw")

    def test_add_account_saves_sealed_and_survives_settings_save(self):
        self.path.write_text(json.dumps({"accounts": [mailapp.normalize_account(
            {"id": "elr", "email": "hello@extraliferecords.com", "password": "pw"})]}), encoding="utf-8")
        api = mailapp.Api(mailapp.Config())
        signed = {"email": "me@hotmail.co.uk", "name": "", "refresh_token": "SECRET-R"}
        with patch.object(microsoft_auth, "sign_in", return_value=signed):
            res = api.add_microsoft_account()
        self.assertNotIn("SECRET-R", self.path.read_text())
        public = api.get_config()
        self.assertNotIn("SECRET-R", json.dumps(public))
        added = next(a for a in public["accounts"] if a["id"] == res["account_id"])
        self.assertEqual((added["auth"], added["imap_host"]), ("microsoft", "outlook.office365.com"))
        api.save_config(public)  # settings dialog save round trip keeps the token
        reloaded = mailapp.Config().account(res["account_id"])
        self.assertEqual((reloaded["auth"], reloaded["password"]), ("microsoft", "SECRET-R"))
        self.assertEqual(mailapp.Config().account("elr")["password"], "pw")

    def test_save_persists_rotated_refresh_token(self):
        self.path.write_text(json.dumps({"accounts": [self.acct]}), encoding="utf-8")
        cfg = mailapp.Config()
        microsoft_auth._rotated["me@hotmail.co.uk"] = "R2"
        cfg.save()
        self.assertEqual(mailapp.Config().account("hm")["password"], "R2")


if __name__ == "__main__":
    unittest.main()
