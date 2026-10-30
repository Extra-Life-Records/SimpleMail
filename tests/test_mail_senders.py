"""Sender clean-up: grouping, the saved index, bulk moves and unsubscribe links. No real mail."""
import tempfile
import unittest
from pathlib import Path

import mail_senders
from mail_filing import FilingStore, copyuid_pairs, move_many, uid_set
from mail_senders import SenderIndex, one_click_url, site


class SiteTests(unittest.TestCase):
    def test_site_keeps_the_registrable_domain(self):
        self.assertEqual(site("deals@em.secretescapes.com"), "secretescapes.com")
        self.assertEqual(site("news@mail.argos.co.uk"), "argos.co.uk")
        self.assertEqual(site("a@boohoo.com"), "boohoo.com")
        self.assertEqual(site("x@avbeefjyzqcwwr"), "avbeefjyzqcwwr")
        self.assertEqual(site("nobody"), "nobody")

    def test_saved_rules_never_match_a_whole_country(self):
        import mailapp
        self.assertEqual(mailapp.sender_domain("Argos <news@mail.argos.co.uk>"), "argos.co.uk")
        self.assertEqual(mailapp.sender_domain("Instagram <posts@mail.instagram.com>"), "instagram.com")

    def test_one_click_needs_post_header_and_https(self):
        self.assertEqual(one_click_url("<mailto:u@x.com>, <https://x.com/u?id=1>", "List-Unsubscribe=One-Click"),
                         "https://x.com/u?id=1")
        self.assertEqual(one_click_url("<https://x.com/u>", ""), "")
        self.assertEqual(one_click_url("<http://x.com/u>", "List-Unsubscribe=One-Click"), "")
        self.assertEqual(one_click_url("<mailto:u@x.com>", "List-Unsubscribe=One-Click"), "")

    def test_unsubscribe_refuses_private_addresses(self):
        with self.assertRaises(ValueError):
            mail_senders.unsubscribe("https://127.0.0.1/unsubscribe")


class UidSetTests(unittest.TestCase):
    def test_ranges_expand_in_order(self):
        self.assertEqual(uid_set("1:3,7"), ["1", "2", "3", "7"])
        self.assertEqual(uid_set("9:8"), ["8", "9"])

    def test_copyuid_pairs_ranges(self):
        class Imap:
            def response(self, name):
                return name, [b"12 4:6,9 101:104"]
        self.assertEqual(copyuid_pairs(Imap(), [], "12"), {"4": "101", "5": "102", "6": "103", "9": "104"})
        self.assertEqual(copyuid_pairs(Imap(), [], "99"), {})


HEADERS = {
    1: b"From: Boohoo <news@emailboohoo.com>\r\nDate: Tue, 27 Oct 2026 10:00:00 +0000\r\n"
       b"List-Unsubscribe: <https://emailboohoo.com/u/1>\r\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n\r\n",
    2: b"From: Boohoo <news@emailboohoo.com>\r\nDate: Wed, 28 Oct 2026 10:00:00 +0000\r\n\r\n",
    3: b"From: \"Sarah\" <sarahawarner@sky.com>\r\nDate: Thu, 29 Oct 2026 10:00:00 +0000\r\n\r\n",
    4: b"From: Casino <GM55IBND@IWAMRH9B12PQ298E.com>\r\nDate: Thu, 29 Oct 2026 11:00:00 +0000\r\n"
       b"List-Unsubscribe: <https://spam.example/u>\r\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n\r\n",
    5: b"From: Casino <LUOGRH6V@ob0a8xkqz1234.com>\r\nDate: Thu, 29 Oct 2026 12:00:00 +0000\r\n\r\n",
}


class Mailbox:
    """Fake Outlook.com: UID after the payload, MOVE with COPYUID ranges."""
    capabilities = (b"MOVE", b"UIDPLUS")

    def __init__(self, shared):
        self.shared = shared
        self.selected = None
        self.copyuid = None

    def select(self, folder, readonly=True):
        self.selected = folder.strip('"')
        return ("OK", [b"1"]) if self.selected in self.shared["validity"] else ("NO", [])

    def response(self, name):
        if name == "UIDVALIDITY":
            return name, [self.shared["validity"][self.selected].encode()]
        if name == "UIDNOTSTICKY":
            return name, [None]
        value, self.copyuid = self.copyuid, None
        return name, [value]

    def uid(self, command, *args):
        mail = self.shared["mail"][self.selected]
        if command == "SEARCH":
            criteria = args[-1]
            if criteria == "ALL":
                found = sorted(mail)
            elif criteria == "UNSEEN":
                found = sorted(u for u in mail if u in self.shared["unseen"])
            else:
                wanted = {int(u) for u in criteria.split(" ", 1)[1].split(",")}
                found = sorted(wanted & set(mail))
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            self.shared["fetches"] += 1
            out = []
            for u in map(int, args[0].split(",")):
                if u in mail:
                    out.append((b"1 (BODY[HEADER] {9}", mail[u]))
                    out.append(f" UID {u})".encode())
            return "OK", out
        if command == "MOVE":
            target = args[1].strip('"')
            moved = [int(u) for u in args[0].split(",")]
            first = self.shared["next"]
            for n, u in enumerate(moved):
                self.shared["mail"][target][first + n] = mail.pop(u)
            self.shared["next"] += len(moved)
            src = ",".join(map(str, moved))
            self.copyuid = f"{self.shared['validity'][target]} {src} {first}:{first + len(moved) - 1}".encode()
            return "OK", [b"Moved"]
        raise AssertionError(command)

    def logout(self):
        self.shared["logouts"] += 1


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = SenderIndex(Path(self.tmp.name) / "senders.sqlite3")
        self.store = FilingStore(Path(self.tmp.name) / "mail.sqlite3")
        self.shared = {"validity": {"Inbox": "7", "Deleted": "12"}, "mail": {"Inbox": dict(HEADERS), "Deleted": {}},
                       "unseen": {1, 4}, "fetches": 0, "logouts": 0, "next": 500}
        self.account = {"id": "live"}

    def tearDown(self):
        self.tmp.cleanup()

    def connector(self, _account):
        return Mailbox(self.shared), ["Inbox", "Deleted"], None

    def scan(self):
        seen = []
        mail_senders.scan(self.account, self.index, self.connector, "Inbox", lambda d, t: seen.append((d, t)), workers=2)
        return seen

    def test_scan_groups_senders_by_site(self):
        old_batch, mail_senders.BATCH = mail_senders.BATCH, 2
        try:
            progress = self.scan()
        finally:
            mail_senders.BATCH = old_batch
        self.assertEqual(progress[-1], (5, 5))
        groups = {g["site"]: g for g in self.index.groups("live", "Inbox", ["sarahawarner@sky.com"])}
        boohoo = groups["emailboohoo.com"]
        self.assertEqual((boohoo["count"], boohoo["unread"], boohoo["name"]), (2, 1, "Boohoo"))
        self.assertTrue(boohoo["can_unsubscribe"])
        self.assertNotIn("unsubscribe", boohoo)  # the link never goes to the page
        self.assertTrue(groups["sky.com"]["trusted"])
        spam = groups["iwamrh9b12pq298e.com"]
        self.assertTrue(spam["random"])
        self.assertFalse(spam["can_unsubscribe"])  # unsubscribing would confirm the address
        self.assertEqual(self.index.unsubscribe_url("live", "Inbox", "iwamrh9b12pq298e.com"), "")
        self.assertEqual(self.index.unsubscribe_url("live", "Inbox", "emailboohoo.com"), "https://emailboohoo.com/u/1")

    def test_rescan_fetches_only_new_mail_and_drops_gone_mail(self):
        self.scan()
        fetches = self.shared["fetches"]
        del self.shared["mail"]["Inbox"][3]
        self.shared["mail"]["Inbox"][6] = HEADERS[2]
        self.shared["unseen"] = {6}
        self.scan()
        self.assertEqual(self.shared["fetches"], fetches + 1)
        self.assertEqual(self.index.uids("live", "Inbox"), {1, 2, 4, 5, 6})
        groups = {g["site"]: g for g in self.index.groups("live", "Inbox")}
        self.assertEqual((groups["emailboohoo.com"]["count"], groups["emailboohoo.com"]["unread"]), (3, 1))

    def test_new_folder_identity_starts_over(self):
        self.scan()
        self.shared["validity"]["Inbox"] = "8"
        self.shared["mail"]["Inbox"] = {1: HEADERS[1]}
        self.scan()
        self.assertEqual(self.index.uids("live", "Inbox"), {1})

    def test_move_many_records_each_message_for_undo(self):
        self.scan()
        uids = self.index.uids("live", "Inbox", "emailboohoo.com") | {99}
        logouts = self.shared["logouts"]
        result = move_many(self.account, self.store, "Inbox", uids, "Deleted", self.connector, "7",
                           reason="Cleared mail from emailboohoo.com", chunk=1)
        self.assertEqual((result["moved"], result["uncertain"], result["missing"]), (2, 0, 1))
        self.assertEqual(sorted(self.shared["mail"]["Inbox"]), [3, 4, 5])
        actions = self.store.list("live")["items"]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a["status"] == "moved" and a["destination"]["folder"] == "Deleted" for a in actions))
        self.assertEqual({a["source"]["uid"] for a in actions}, {"1", "2"})
        self.assertEqual(self.shared["logouts"], logouts + 1)  # one login for the whole batch

    def test_move_many_refuses_a_changed_folder(self):
        with self.assertRaises(ValueError):
            move_many(self.account, self.store, "Inbox", [1], "Deleted", self.connector, "6")
        self.assertIn(1, self.shared["mail"]["Inbox"])


if __name__ == "__main__":
    unittest.main()
