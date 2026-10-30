"""Junk rescue: who counts as real, and recoverable moves. No real mail connections."""
import tempfile
import unittest
from pathlib import Path

import mail_screen
from mail_filing import FilingStore
from mail_screen import address, classify, looks_random, text, trusted_match
from test_mail_filing import Server


class ClassifyTests(unittest.TestCase):
    known = {"sarahawarner@sky.com", "support@distrokid.com", "hello@meandtherobots.com"}

    def test_people_he_has_written_to_are_real(self):
        self.assertEqual(classify("sarahawarner@sky.com", "Job spec", known=self.known)[0], "real")
        self.assertEqual(classify("Support@DistroKid.com", "[DistroKid] Re: Music Issue", known=self.known),
                         ("real", "You've emailed them"))

    def test_trusted_domains_cover_subdomains(self):
        self.assertTrue(trusted_match("mailbot@mail.distrokid.com", ["distrokid.com"]))
        self.assertFalse(trusted_match("mailbot@notdistrokid.com", ["distrokid.com"]))
        self.assertTrue(trusted_match("Sarah@Sky.com", ["sarah@sky.com"]))
        self.assertEqual(classify("mailbot@distrokid.com", trusted=["distrokid.com"])[0], "real")

    def test_display_names_never_count(self):
        # "TVLicensing UK" and "Apple" names on unknown addresses stay in Junk
        self.assertEqual(classify(address("TVLicensing UK <tv-licensing@renewals-uk.net>"), "Renew")[0], None)
        self.assertEqual(classify(address('"hello@meandtherobots.com" <x@spam.biz>'), "", known=self.known)[0], None)

    def test_conversation_replies_and_personal_addresses_are_maybe(self):
        self.assertEqual(classify("zayn@helpdesk-mail.com", "Re: Music Issue", "<abc@x>")[0], "maybe")
        self.assertEqual(classify("someone@gmail.com", "Hello")[0], "maybe")
        self.assertEqual(classify("someone@gmail.com", "Sale", list_unsubscribe="<https://x>")[0], None)
        self.assertEqual(classify("news@shop.com", "Re: your basket", "")[0], None)

    def test_machine_made_addresses_are_never_rescued(self):
        for addr in ["no-reply@LmCu5ov.\U0001f979\U0001faf6", "GM55IBND@IWAMRH9B12PQ298E.com",
                     "x@0b4a1rd70yepbxb7l.com", "a@avbeefjyzqcwwr.com", "LUOGRH6V@ob0a8.com", "nobody"]:
            self.assertTrue(looks_random(addr), addr)
            self.assertIsNone(classify(addr, "Re: win", "<a@b>")[0], addr)
        for addr in ["sarahawarner@sky.com", "support@distrokid.com", "mailbot@mail.distrokid.com",
                     "hello@meandtherobots.com", "deals@em.secretescapes.com", "info@argos.co.uk"]:
            self.assertFalse(looks_random(addr), addr)

    def test_headers_decode(self):
        self.assertEqual(text("=?UTF-8?Q?Wave_3_is_coming_=E2=80=94_only?=\r\n =?UTF-8?Q?_300?="),
                         "Wave 3 is coming — only 300")
        self.assertEqual(address("=?UTF-8?Q?Sarah_Warner?= <sarahawarner@sky.com>"), "sarahawarner@sky.com")


class FolderTests(unittest.TestCase):
    def test_outlook_deleted_folder_is_trash(self):
        import mailapp
        folders = mailapp.map_folders(["Inbox", "Junk", "Deleted", "Deleted/Newsletters", "Sent"])
        self.assertEqual({f["key"]: f["server"] for f in folders}["trash"], "Deleted")


class Junk(Server):
    """Fake Outlook: Inbox, Junk and Sent with headers; creates folders."""

    def __init__(self):
        super().__init__()
        self.validities = {"Inbox": "7", "Junk": "9", "Sent": "5"}
        self.messages = {"Inbox": set(), "Junk": {"11", "12", "13"}, "Sent": {"2", "3"}}
        self.headers = {
            ("Junk", "11"): b"From: Sarah Warner <sarahawarner@sky.com>\r\nSubject: Job spec\r\n\r\n",
            ("Junk", "12"): b"From: Free Spins <GM55IBND@IWAMRH9B12PQ298E.com>\r\nSubject: Re: Win\r\nIn-Reply-To: <a@b>\r\n\r\n",
            ("Junk", "13"): b"From: Zayn <zayn@desk.distrokid.com>\r\nSubject: Re: Music Issue\r\nIn-Reply-To: <a@b>\r\n\r\n",
            ("Sent", "2"): b"To: Sarah <sarahawarner@sky.com>\r\nCc: friend@gmail.com\r\n\r\n",
            ("Sent", "3"): b"To: DistroKid <support@distrokid.com>\r\n\r\n",  # no Cc/Bcc
        }
        self.created = []
        self.logouts = 0
        self.uid_after = False

    def create(self, name):
        name = name[1:-1]
        self.created.append(name)
        self.validities[name] = "12"
        self.messages[name] = set()
        return "OK", [b"Created"]

    def uid(self, command, uid, argument=None, *rest):
        if command == "SEARCH":
            return "OK", [" ".join(sorted(self.messages[self.selected], key=int)).encode()]
        if command == "FETCH" and argument.startswith("(UID BODY.PEEK"):
            out = []
            for one in uid.decode().split(","):
                if one in self.messages[self.selected]:
                    if self.uid_after:  # Outlook.com: UID follows the payload
                        out.append((b"1 (BODY[HEADER] {9}", self.headers[(self.selected, one)]))
                        out.append(f" UID {one})".encode())
                    else:
                        out.append((f"1 (UID {one} BODY[HEADER] {{9}}".encode(), self.headers[(self.selected, one)]))
                        out.append(b")")
            return "OK", out
        return super().uid(command, uid, argument)

    def logout(self):
        self.logouts += 1


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FilingStore(Path(self.tmp.name) / "mail.sqlite3")
        self.server = Junk()
        self.account = {"id": "live"}

    def tearDown(self):
        self.tmp.cleanup()

    def connector(self, _account):
        return self.server, ["Inbox", "Junk", "Sent"], "Sent"

    def test_scan_finds_real_and_maybe_but_not_spam(self):
        known = mail_screen.known_senders(self.server, "Sent")
        self.assertEqual(known, {"sarahawarner@sky.com", "friend@gmail.com", "support@distrokid.com"})
        validity, items = mail_screen.scan(self.server, "Junk", known, [])
        self.assertEqual(validity, "9")
        self.assertEqual([(i["uid"], i["verdict"]) for i in items], [("13", "maybe"), ("11", "real")])
        self.assertEqual(items[1]["subject"], "Job spec")

    def test_scan_reads_outlook_uid_after_payload(self):
        self.server.uid_after = True
        known = mail_screen.known_senders(self.server, "Sent")
        self.assertIn("sarahawarner@sky.com", known)
        _, items = mail_screen.scan(self.server, "Junk", known, [])
        self.assertEqual([(i["uid"], i["verdict"]) for i in items], [("13", "maybe"), ("11", "real")])

    def test_mail_forged_from_the_mailbox_itself_is_never_rescued(self):
        self.server.headers[("Junk", "12")] = b"From: Me <Denby@Live.co.uk>\r\nSubject: Re: hacked\r\nIn-Reply-To: <a@b>\r\n\r\n"
        _, items = mail_screen.scan(self.server, "Junk", {"denby@live.co.uk", "sarahawarner@sky.com"},
                                    ["denby@live.co.uk"], own=["denby@live.co.uk"])
        self.assertNotIn("12", [i["uid"] for i in items])
        self.assertIn("11", [i["uid"] for i in items])

    def test_rescue_creates_folder_moves_with_undo_and_logs_in_once(self):
        result = mail_screen.rescue(self.account, self.store, self.connector, "Junk", ["11", "13"], "9")
        self.assertEqual(result, {"target": "Rescued", "moved": ["11", "13"], "failed": []})
        self.assertEqual(self.server.created, ["Rescued"])
        self.assertEqual(self.server.messages["Junk"], {"12"})
        self.assertEqual(len(self.server.messages["Rescued"]), 2)
        self.assertEqual(self.server.logouts, 1)
        actions = self.store.list("live")["items"]
        self.assertEqual({a["status"] for a in actions}, {"moved"})
        self.assertTrue(all(a["reason"] == "Rescued from Junk" for a in actions))

    def test_rescue_reuses_existing_folder_and_reports_missing_mail(self):
        self.server.validities["rescued"] = "12"
        self.server.messages["rescued"] = set()
        connector = lambda _a: (self.server, ["Inbox", "Junk", "rescued"], None)
        result = mail_screen.rescue(self.account, self.store, connector, "Junk", ["11", "99"], "9")
        self.assertEqual(self.server.created, [])
        self.assertEqual((result["target"], result["moved"]), ("rescued", ["11"]))
        self.assertEqual(result["failed"][0]["uid"], "99")

    def test_rescue_refuses_a_changed_folder(self):
        result = mail_screen.rescue(self.account, self.store, self.connector, "Junk", ["11"], "8")
        self.assertEqual(result["moved"], [])
        self.assertIn("changed", result["failed"][0]["error"])
        with self.assertRaises(ValueError):
            mail_screen.rescue(self.account, self.store, self.connector, "Junk", ["11"], None)


if __name__ == "__main__":
    unittest.main()
