"""Screening new Inbox mail from unknown senders. No real mail connections."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mailapp
from mail_screen import to_screen
from test_mail_senders import HEADERS, Mailbox


def env(uid, sender):
    return {"uid": str(uid), "sender": sender, "subject": "", "date": "", "seen": False, "snippet": ""}


class ToScreenTests(unittest.TestCase):
    def test_only_new_mail_from_unknown_senders(self):
        envelopes = [env(10, "Sarah <sarahawarner@sky.com>"), env(11, "Promo <deals@shop.com>"),
                     env(12, "Orders <hello@meandtherobots.com>"), env(13, "Me <Denby@Live.co.uk>"),
                     env(5, "Old <old@shop.com>"), env(14, "DistroKid <mailbot@mail.distrokid.com>")]
        moved = to_screen(envelopes, 9, {"sarahawarner@sky.com"},
                          ["hello@meandtherobots.com", "denby@live.co.uk", "distrokid.com"])
        self.assertEqual([e["uid"] for e in moved], ["11"])

    def test_screening_off_moves_nothing(self):
        # screen_from 0 is "off" in the app; to_screen with since=uid max keeps old mail
        self.assertEqual(to_screen([env(3, "x@shop.com")], 3, set(), []), [])


class ApplyScreeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shared = {"validity": {"Inbox": "7"}, "mail": {"Inbox": dict(HEADERS)},
                       "unseen": set(), "fetches": 0, "logouts": 0, "next": 900}
        self.patches = [
            mock.patch.object(mailapp, "CONFIG_DIR", Path(self.tmp.name)),
            mock.patch.object(mailapp, "connect_imap", self.connector),
            mock.patch.object(mailapp, "known_senders", lambda acct, refresh=False: {"sarahawarner@sky.com"}),
        ]
        for p in self.patches:
            p.start()
        self.cfg = mock.Mock()
        self.acct = {"id": "live", "email": "denby@live.co.uk", "from_email": "", "trusted": [], "screen_from": 2}
        self.cfg.accounts.return_value = [self.acct]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    class Server(Mailbox):
        def create(self, name):
            name = name.strip('"')
            self.shared["validity"][name] = "30"
            self.shared["mail"][name] = {}
            return "OK", [b"Created"]

    def connector(self, _acct):
        return self.Server(self.shared), list(self.shared["validity"]), None

    def test_unknown_new_mail_moves_to_screener(self):
        envelopes = [env(5, "Casino <LUOGRH6V@ob0a8xkqz1234.com>"), env(4, "Casino <GM55IBND@IWAMRH9B12PQ298E.com>"),
                     env(3, "\"Sarah\" <sarahawarner@sky.com>"), env(2, "Boohoo <news@emailboohoo.com>")]
        kept, moved = mailapp.apply_screening(self.cfg, self.acct, "Inbox", envelopes, "7")
        self.assertEqual(moved, 2)
        self.assertEqual([e["uid"] for e in kept], ["3", "2"])  # Sarah is known; uid 2 predates screening
        self.assertEqual(sorted(self.shared["mail"]["Inbox"]), [1, 2, 3])
        self.assertEqual(len(self.shared["mail"]["Screener"]), 2)

    def test_off_or_other_folders_do_nothing(self):
        envelopes = [env(5, "x@shop.com")]
        self.acct["screen_from"] = 0
        self.assertEqual(mailapp.apply_screening(self.cfg, self.acct, "Inbox", envelopes, "7"), (envelopes, 0))
        self.acct["screen_from"] = 1
        self.assertEqual(mailapp.apply_screening(self.cfg, self.acct, "Junk", envelopes, "7"), (envelopes, 0))
        self.assertNotIn("Screener", self.shared["mail"])


if __name__ == "__main__":
    unittest.main()
