"""Read-only environment connector tests. All mail connections are fake."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env_mail_mcp import EnvConfig, ReadOnlyMailbox, ReadOnlyServer, main
from mailbox_agent import unpack

ENV = {"SIMPLEMAIL_EMAIL": "owner@example.com", "SIMPLEMAIL_PASSWORD": "SECRET"}


class EnvMailTests(unittest.TestCase):
    def setUp(self):
        self.imap = Mock()
        self.imap.select.return_value = ("OK", [b'3'])
        self.imap.response.return_value = ("UIDVALIDITY", [b'7'])
        self.connector = Mock(return_value=(self.imap, ["INBOX", "Sent"], None))
        self.mailbox = ReadOnlyMailbox(EnvConfig(ENV), self.connector)
        self.server = ReadOnlyServer(self.mailbox)
        self.call({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.call({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, request):
        return self.server.handle(request)

    def tool(self, name, **arguments):
        return self.call({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})

    def test_config_requires_login_and_defaults_to_fasthosts(self):
        with self.assertRaises(ValueError):
            EnvConfig({}).account("env")
        with self.assertRaises(ValueError):
            EnvConfig({**ENV, "SIMPLEMAIL_IMAP_PORT": "imap"}).account("env")
        account = EnvConfig(ENV).account("env")
        self.assertEqual((account["imap_host"], account["imap_port"]), ("mail.livemail.co.uk", 993))
        with self.assertRaises(ValueError):
            EnvConfig(ENV).account("other")

    def test_only_read_tools_are_listed_or_callable(self):
        names = {item["name"] for item in self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertIn("mailbox_unread", names)
        self.assertIn("mailbox_read", names)
        for blocked in ("mailbox_draft", "mailbox_send", "mailbox_file", "mailbox_work_next", "mailbox_attach"):
            self.assertNotIn(blocked, names)
            response = self.tool(blocked)
            self.assertEqual(response["error"]["code"], -32602)
        self.connector.assert_not_called()

    def test_identity_is_read_only_without_credentials(self):
        result = self.tool("mailbox_identity")["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["mode"], "read_only")
        self.assertFalse(result["structuredContent"]["allow_filing"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unread_searches_unseen_inbox_without_marking_read(self):
        def uid(command, *args):
            if command == "search":
                return "OK", [b'4 9']
            ids = args[0].split(',')
            return "OK", [(f'1 (UID {n} FLAGS () BODY[HEADER.FIELDS] {{30}})'.encode(),
                           f'From: person@example.com\r\nSubject: Message {n}\r\n'.encode()) for n in ids]
        self.imap.uid.side_effect = uid
        result = self.tool("mailbox_unread", limit=1)["result"]["structuredContent"]
        self.assertEqual(self.imap.uid.call_args_list[0].args, ("search", None, "UNSEEN"))
        self.imap.select.assert_called_with('"INBOX"', readonly=True)
        self.assertEqual([item["uid"] for item in result["items"]], ["9"])
        self.assertTrue(unpack(result["next_cursor"])["unseen"])
        page = self.mailbox.unread(cursor=result["next_cursor"], limit=1)
        self.assertEqual([item["uid"] for item in page["items"]], ["4"])
        with self.assertRaises(ValueError):  # an unread cursor cannot page an all-mail search
            self.mailbox.search(cursor=result["next_cursor"], limit=1)
        self.assertFalse(any(call.args[0] == "store" for call in self.imap.uid.call_args_list))

    def test_unread_validates_arguments_and_hides_connection_errors(self):
        self.assertEqual(self.tool("mailbox_unread", limit=0)["error"]["code"], -32602)
        self.assertEqual(self.tool("mailbox_unread", folder="Sent")["error"]["code"], -32602)
        self.connector.side_effect = OSError("login failed for owner@example.com SECRET")
        result = self.tool("mailbox_unread")["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_cli_fails_closed_without_environment(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["check"]), 1)


if __name__ == "__main__":
    unittest.main()
