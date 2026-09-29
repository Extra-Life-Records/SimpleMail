"""Isolated mail-arrival regression tests: py -3 -m unittest discover -s tests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mailapp


class MailPollerTests(unittest.TestCase):
    def setUp(self):
        self.imap = Mock()
        self.connection = patch.object(mailapp, 'connect_imap', return_value=(self.imap, [], None))
        self.connection.start()
        self.addCleanup(self.connection.stop)
        self.toast = patch.object(mailapp, 'show_toast').start()
        self.addCleanup(patch.stopall)
        self.poller = mailapp.MailPoller({'id': 'test', 'label': 'Test', 'rules': {}})

    def test_first_arrival_in_initially_empty_inbox_notifies_once(self):
        self.imap.uid.return_value = ('OK', [b''])
        self.poller._snapshot_uid()
        self.assertEqual(self.poller._last_uid, 0)
        self.imap.uid.side_effect = [
            ('OK', [b'1']),
            ('OK', [(b'1 (UID 1 FLAGS ())', b'From: Person <person@example.com>\r\nSubject: Hello\r\n')]),
            ('OK', [b'1']),
        ]
        self.poller._check_once()
        self.poller._check_once()
        self.toast.assert_called_once_with('[Test] Hello', 'Person')
        self.assertEqual(self.poller._last_uid, 1)

    def test_startup_network_failure_retries_baseline_then_checks_mail(self):
        def snapshot():
            if self.poller._snapshot_uid.call_count > 1:
                self.poller._last_uid = 3
        self.poller._snapshot_uid = Mock(side_effect=snapshot)
        self.poller._check_once = Mock()
        self.poller._stop_event = Mock()
        self.poller._stop_event.wait.side_effect = [False, False, True]
        self.poller.run()
        self.assertEqual(self.poller._snapshot_uid.call_count, 2)
        self.poller._check_once.assert_called_once()

    def test_failed_fetch_does_not_consume_arrival(self):
        self.poller._last_uid = 4
        self.imap.uid.side_effect = [('OK', [b'5']), ('NO', [None])]
        self.poller._check_once()
        self.assertEqual(self.poller._last_uid, 4)
        self.toast.assert_not_called()

    def test_learned_rules_keep_notifications_quiet(self):
        self.poller._last_uid = 1
        self.poller.acct['rules'] = {'example.com': 'Marketing'}
        self.imap.uid.side_effect = [
            ('OK', [b'2']),
            ('OK', [(b'2 (UID 2)', b'From: Offers <offers@example.com>\r\nSubject: Sale\r\n')]),
        ]
        self.poller._check_once()
        self.toast.assert_not_called()
        self.assertEqual(self.poller._last_uid, 2)

    def test_stop_keeps_thread_join_usable(self):
        self.poller.stop()
        self.poller._snapshot_uid = Mock()
        self.poller.start()
        self.poller.join(timeout=2)
        self.assertFalse(self.poller.is_alive())

    def test_listing_sent_includes_current_inbox_count_on_same_connection(self):
        # Api needs only a dict-style max_messages setting and account lookup.
        class Config(dict):
            def account(self, account_id):
                return {}
        with patch.object(mailapp, 'fetch_envelopes', return_value=[]), \
             patch.object(mailapp, 'count_unread', side_effect=[0, 9]), \
             patch.object(mailapp, 'apply_rules', return_value=([], 0)):
            result = mailapp.Api(Config(max_messages=100)).list_messages('test', 'Sent')
        self.assertEqual(result['folder_unread'], 0)
        self.assertEqual(result['inbox_unread'], 9)
        self.imap.logout.assert_called_once()


if __name__ == '__main__':
    unittest.main()
