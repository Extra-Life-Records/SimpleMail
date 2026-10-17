"""AWS robot-reader connector tests. All HTTP is fake; no real mail service is called."""
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_reader import CloudReader, CloudReaderServer
from env_mail_mcp import main

API = "https://abc123.execute-api.eu-west-1.amazonaws.com"
AUTH = "https://playloudr-mail-1.auth.eu-west-1.amazoncognito.com"
ENV = {"SIMPLEMAIL_CLOUD_API_URL": API, "SIMPLEMAIL_CLOUD_AUTH_URL": AUTH,
       "SIMPLEMAIL_CLOUD_CLIENT_ID": "readerclient123"}


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


class FakeOpener:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def open(self, request, timeout):
        self.requests.append(request)
        url = urlsplit(request.full_url)
        if url.path == "/oauth2/token":
            return Response(json.dumps({"access_token": "token-1", "expires_in": 3600}).encode())
        query = parse_qs(url.query)
        key = request.get_method() + " " + url.path + ("?unread" if query.get("unread") else "") + \
            ("?untriaged" if query.get("untriaged") else "")
        value = self.pages[key]
        if isinstance(value, int):
            raise HTTPError(request.full_url, value, "denied", {}, io.BytesIO(b'{"error": "Mailbox is not available"}'))
        return Response(json.dumps(value).encode())


PAGES = {
    "POST /reader/mailboxes/one/messages/m1/triage": {"ok": True},
    "POST /reader/mailboxes/one/messages/m1/move": {"ok": True, "folder": "Junk"},
    "POST /reader/mailboxes/one/drafts": {"id": "d1", "revision": "r1"},
    "GET /reader/mailboxes/one/messages?untriaged": {"messages": [{"id": "m1"}], "cursor": None},
    "GET /reader/mailboxes/five/messages?untriaged": {"messages": [], "cursor": None},
    "GET /reader/mailboxes": {"mailboxes": [{"name": "one", "address": "one@extraliferecords.com", "state": "Reserved"},
                                        {"name": "five", "address": "five@extraliferecords.com", "state": "Reserved"}]},
    "GET /reader/mailboxes/one/messages?unread": {"messages": [{"id": "m1", "subject": "Hi", "seen": False}], "cursor": "c1"},
    "GET /reader/mailboxes/five/messages?unread": {"messages": [], "cursor": None},
    "GET /reader/mailboxes/one/messages": {"messages": [{"id": "m1"}], "cursor": None},
    "GET /reader/mailboxes/one/messages/m1": {"id": "m1", "subject": "Hi", "body": "x" * 500, "html": "<p>x</p>",
                                          "body_truncated": False},
    "GET /reader/mailboxes/two/messages/m1": 403,
}


class CloudReaderTests(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener(PAGES)
        self.reader = CloudReader(ENV, self.opener)
        self.server = CloudReaderServer(self.reader)
        self.init = self.call({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, request):
        return self.server.handle(request)

    def tool(self, name, **arguments):
        return self.call({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})

    def test_settings_must_be_the_stack_outputs(self):
        for changes in ({"SIMPLEMAIL_CLOUD_API_URL": "https://evil.example.com"},
                        {"SIMPLEMAIL_CLOUD_AUTH_URL": "http://playloudr-mail-1.auth.eu-west-1.amazoncognito.com"},
                        {"SIMPLEMAIL_CLOUD_CLIENT_ID": ""}):
            with self.assertRaises(ValueError):
                CloudReader({**ENV, **changes}, self.opener).mailboxes()
        self.assertEqual(self.opener.requests, [])

    def test_token_relies_on_network_secret_and_is_reused(self):
        self.reader.mailboxes(); self.reader.mailboxes()
        token_requests = [r for r in self.opener.requests if r.full_url.endswith("/oauth2/token")]
        self.assertEqual(len(token_requests), 1)
        self.assertIsNone(token_requests[0].get_header("Authorization"))
        self.assertEqual(parse_qs(token_requests[0].data.decode()),
                         {"grant_type": ["client_credentials"], "client_id": ["readerclient123"],
                          "scope": ["mail-reader/read mail-reader/triage"]})
        self.assertEqual(self.opener.requests[-1].get_header("Authorization"), "Bearer token-1")

    def test_local_secret_uses_basic_auth(self):
        reader = CloudReader({**ENV, "SIMPLEMAIL_CLOUD_CLIENT_SECRET": "s3cret"}, self.opener)
        reader.mailboxes()
        self.assertTrue(self.opener.requests[0].get_header("Authorization").startswith("Basic "))

    def test_only_read_tools_exist(self):
        self.assertEqual(self.init["result"]["serverInfo"]["name"], "simplemail-aws-reader")
        names = {item["name"] for item in self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(names, {"mailbox_list", "mailbox_unread", "mailbox_search", "mailbox_read", "mailbox_attachment",
                                 "mailbox_untriaged", "mailbox_tag", "mailbox_move", "mailbox_draft"})
        for blocked in ("mailbox_send", "mailbox_file", "mailbox_identity", "mailbox_delete"):
            self.assertEqual(self.tool(blocked)["error"]["code"], -32602)
        self.assertEqual(self.opener.requests, [])

    def test_unread_across_every_mailbox(self):
        result = self.tool("mailbox_unread")["result"]["structuredContent"]
        self.assertEqual([(box["mailbox"], len(box["items"]), box["next_cursor"]) for box in result["mailboxes"]],
                         [("one", 1, "c1"), ("five", 0, None)])
        self.assertTrue(self.tool("mailbox_unread", cursor="c1")["result"]["isError"])
        self.reader.unread("one", "c1")
        self.assertEqual(parse_qs(urlsplit(self.opener.requests[-1].full_url).query),
                         {"folder": ["Inbox"], "unread": ["1"], "cursor": ["c1"]})

    def test_read_trims_body_and_drops_html(self):
        value = self.tool("mailbox_read", mailbox="one", message_id="m1", max_chars=100)["result"]["structuredContent"]
        self.assertEqual((len(value["body"]), value["truncated"]), (100, True))
        self.assertNotIn("html", value)

    def test_bad_names_never_reach_the_service(self):
        for arguments in ({"mailbox": "../one", "message_id": "m1"}, {"mailbox": "one", "message_id": "m1/../x"}):
            self.assertTrue(self.tool("mailbox_read", **arguments)["result"]["isError"])
        self.assertTrue(self.tool("mailbox_search", mailbox="one", folder="Secret")["result"]["isError"])
        self.assertEqual(self.opener.requests, [])

    def test_refusal_is_reported_plainly(self):
        result = self.tool("mailbox_read", mailbox="two", message_id="m1")["result"]
        self.assertTrue(result["isError"])
        self.assertIn("403", result["content"][0]["text"])
        self.assertNotIn("token-1", json.dumps(result))

    def body(self):
        request = self.opener.requests[-1]
        return request.get_method(), urlsplit(request.full_url).path, json.loads(request.data)

    def test_untriaged_across_every_mailbox(self):
        result = self.tool("mailbox_untriaged")["result"]["structuredContent"]
        self.assertEqual([(box["mailbox"], len(box["items"])) for box in result["mailboxes"]], [("one", 1), ("five", 0)])

    def test_tag_move_and_draft_send_the_right_requests(self):
        self.assertFalse(self.tool("mailbox_tag", mailbox="one", message_id="m1", category="scam", note="Fake invoice")["result"]["isError"])
        self.assertEqual(self.body(), ("POST", "/reader/mailboxes/one/messages/m1/triage", {"category": "scam", "note": "Fake invoice"}))
        self.tool("mailbox_move", mailbox="one", message_id="m1", folder="Junk", reason="Fake invoice")
        self.assertEqual(self.body(), ("POST", "/reader/mailboxes/one/messages/m1/move", {"folder": "Junk", "reason": "Fake invoice"}))
        self.tool("mailbox_draft", mailbox="one", to="fan@example.com", subject="Re: Hi", body="Thanks!",
                  reason="Thank-you", request_id="triage-2026-10-17-1", reply_to_id="m1")
        method, path, data = self.body()
        self.assertEqual((method, path, data["reply_to_id"], data["request_id"]), ("POST", "/reader/mailboxes/one/drafts", "m1", "triage-2026-10-17-1"))

    def test_bad_tag_or_move_never_reaches_the_service(self):
        before = len(self.opener.requests)
        self.assertTrue(self.tool("mailbox_tag", mailbox="one", message_id="m1", category="urgent", note="x")["result"]["isError"])
        self.assertTrue(self.tool("mailbox_move", mailbox="one", message_id="m1", folder="Trash", reason="x")["result"]["isError"])
        self.assertTrue(self.tool("mailbox_draft", mailbox="one", to="a@example.com", subject="s", body="b", reason="r",
                                  request_id="triage-2026-10-17-1", reply_to_id="../m1")["result"]["isError"])
        self.assertEqual(len(self.opener.requests), before)

    def test_cli_uses_cloud_mode_when_configured(self):
        with patch.dict(os.environ, {**ENV, "SIMPLEMAIL_CLOUD_API_URL": "https://bad.example.com"}, clear=True):
            self.assertEqual(main(["check"]), 1)


if __name__ == "__main__":
    unittest.main()
