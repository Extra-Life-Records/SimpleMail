import unittest
import unittest.mock
from unittest.mock import MagicMock
import mailapp
from imap_fetch import fetch_groups

FASTHOSTS = [(b'1 (UID 11 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {30}', b'From: a@b.com\r\nSubject: One\r\n\r\n'),
             (b' BODY[TEXT]<0> {5}', b'hello'), b')',
             (b'2 (UID 12 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {30}', b'From: c@d.com\r\nSubject: Two\r\n\r\n'),
             (b' BODY[TEXT]<0> {5}', b'there'), b')']
OUTLOOK = [(b'33740 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {30}', b'From: a@b.com\r\nSubject: One\r\n\r\n'),
           (b' BODY[TEXT]<0> {5}', b'hello'), b' UID 164725)',
           (b'33741 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {30}', b'From: c@d.com\r\nSubject: Two\r\n\r\n'),
           (b' BODY[TEXT]<0> {5}', b'there'), b' UID 164727)']


class FetchGroupTests(unittest.TestCase):
    def test_uid_before_or_after_payloads(self):
        for data, uids in ((FASTHOSTS, [11, 12]), (OUTLOOK, [164725, 164727])):
            groups = fetch_groups(data)
            self.assertEqual([g[0] for g in groups], uids)
            self.assertEqual([len(g[2]) for g in groups], [2, 2])

    def test_flags_in_either_order(self):
        groups = fetch_groups([b'33740 (UID 164725 FLAGS (\Seen))', b'5 (FLAGS (\Recent) UID 9)'])
        self.assertEqual([(g[0], g[1]) for g in groups], [(164725, '\Seen'), (9, '\Recent')])

    def test_envelopes_from_outlook_shaped_response(self):
        imap = MagicMock()
        imap.uid.side_effect = [("OK", [b"164725 164727"]),
                                ("OK", [b'33740 (UID 164725 FLAGS (\Seen))', b'33741 (UID 164727 FLAGS ())']),
                                ("OK", OUTLOOK)]
        envelopes = mailapp.fetch_envelopes(imap, "INBOX", 100)
        self.assertEqual([(e["uid"], e["subject"], e["seen"], e["snippet"]) for e in envelopes],
                         [("164727", "Two", False, "there"), ("164725", "One", True, "hello")])


class ThreadedTests(unittest.TestCase):
    def read(self, headers):
        imap = MagicMock()
        imap.uid.return_value = ("OK", [(b"1 (RFC822 {1}", b"From: a@b.com\r\nSubject: Hi\r\n" + headers + b"\r\nbody")])
        with unittest.mock.patch.object(mailapp, "select_message_folder"):
            return mailapp.fetch_message(imap, "INBOX", "5")

    def test_only_replies_are_threaded(self):
        # the reader offers Conversation history only on replies
        self.assertFalse(self.read(b"")["threaded"])
        self.assertTrue(self.read(b"In-Reply-To: <x@y>\r\n")["threaded"])
        self.assertTrue(self.read(b"References: <x@y> <z@y>\r\n")["threaded"])


if __name__ == "__main__":
    unittest.main()
