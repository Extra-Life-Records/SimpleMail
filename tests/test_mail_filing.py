"""Recoverable filing protocol and restart tests. No real mail connections."""
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from mail_filing import FilingStore, mapping, move, quote, undo
from mailbox_agent import MailboxAgent


class Server:
    capabilities = (b'MOVE', b'UIDPLUS')

    def __init__(self):
        self.validities = {'INBOX': '7', 'Archive': '8'}
        self.messages = {'INBOX': {'3'}, 'Archive': set()}
        self.selected = None
        self.copyuid = None
        self.calls = []
        self.outcome = 'OK'
        self.mapping = True
        self.sticky = True
        self.logout_error = False

    def select(self, folder, readonly=True):
        self.selected = folder[1:-1]
        return ('OK', [b'1']) if self.selected in self.validities else ('NO', [])

    def response(self, name):
        if name == 'UIDVALIDITY':
            return name, [self.validities[self.selected].encode()]
        if name == 'UIDNOTSTICKY':
            return name, [None] if self.sticky else [b'']
        value, self.copyuid = self.copyuid, None
        return name, [value]

    def uid(self, command, uid, argument):
        self.calls.append((command, self.selected, uid, argument))
        if command == 'FETCH':
            return 'OK', [f'1 (UID {uid})'.encode()] if uid in self.messages[self.selected] else []
        if command != 'MOVE':
            raise AssertionError('Filing must never STORE, COPY or EXPUNGE')
        target = argument[1:-1]
        self.messages[self.selected].remove(uid)
        new_uid = str(100 + len(self.calls))
        self.messages[target].add(new_uid)
        if self.outcome == 'disconnect':
            raise ConnectionError('Acknowledgement lost')
        if self.mapping:
            self.copyuid = f'{self.validities[target]} {uid} {new_uid}'.encode()
        return self.outcome, [b'Moved']

    def logout(self):
        if self.logout_error:
            raise ConnectionError('Logout disconnected')


class FilingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mailbox.sqlite3'
        self.store = FilingStore(self.path)
        self.server = Server()
        self.account = {'id': 'one', 'label': 'One', 'email': 'one@example.com'}
        self.connector = Mock(return_value=(self.server, ['INBOX', 'Archive'], None))

    def move(self, **kwargs):
        return move(self.account, self.store, 'INBOX', '3', 'Archive', self.connector,
                    expected_validity='7', **kwargs)

    def undo(self, action):
        return undo(self.account, self.store, action['id'], self.connector)

    def moves(self):
        return [call for call in self.server.calls if call[0] == 'MOVE']

    def test_move_and_undo_keep_destination_identity_and_survive_restart(self):
        action = self.move()
        self.assertEqual(action['status'], 'moved')
        self.assertEqual(action['destination']['validity'], '8')
        self.assertEqual(self.server.messages['Archive'], {action['destination']['uid']})
        self.store = FilingStore(self.path)
        restored = self.undo(action)
        self.assertEqual(restored['status'], 'undone')
        self.assertEqual(len(self.server.messages['INBOX']), 1)
        self.assertFalse(self.server.messages['Archive'])
        self.assertEqual(len(self.moves()), 2)
        with self.assertRaises(ValueError):
            self.undo(action)

    def test_journal_is_committed_before_network_action(self):
        original = self.server.uid
        def checked(command, *args):
            if command == 'MOVE':
                actions = FilingStore(self.path).list('one')['items']
                self.assertEqual(actions[0]['status'], 'moving')
            return original(command, *args)
        self.server.uid = checked
        self.move()

    def test_retry_confirmed_request_returns_same_action_without_second_move(self):
        first = self.move(request_key='job-1')
        second = self.move(request_key='job-1')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.moves()), 1)

    def test_lost_acknowledgement_is_durable_and_never_retried(self):
        self.server.outcome = 'disconnect'
        first = self.move(request_key='job-1')
        self.assertEqual(first['status'], 'uncertain')
        self.store = FilingStore(self.path)
        self.assertEqual(self.move(request_key='job-1')['status'], 'uncertain')
        with self.assertRaises(ValueError):
            self.undo(first)
        self.assertEqual(len(self.moves()), 1)

    def test_no_and_missing_mapping_are_uncertain_without_fallback(self):
        for outcome, has_mapping in [('NO', True), ('OK', False)]:
            with self.subTest(outcome=outcome):
                self.server.messages['INBOX'].add('3')
                self.server.outcome, self.server.mapping = outcome, has_mapping
                action = self.move(request_key=f'{outcome}-{has_mapping}')
                self.assertEqual(action['status'], 'uncertain')
        self.assertTrue(all(call[0] in ('FETCH', 'MOVE') for call in self.server.calls))

    def test_logout_failure_does_not_discard_confirmed_ack(self):
        self.server.logout_error = True
        self.assertEqual(self.move()['status'], 'moved')

    def test_source_folder_recreated_blocks_move_before_journal(self):
        self.server.validities['INBOX'] = '9'
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.move()
        self.assertFalse(self.moves())
        self.assertFalse(self.store.list('one')['items'])

    def test_recreated_original_or_destination_blocks_undo(self):
        action = self.move()
        for folder in ('INBOX', 'Archive'):
            with self.subTest(folder=folder):
                old = self.server.validities[folder]
                self.server.validities[folder] = '900'
                with self.assertRaises(ValueError):
                    self.undo(action)
                self.server.validities[folder] = old
        self.assertEqual(len(self.moves()), 1)
        self.assertEqual(self.store.get('one', action['id'])['status'], 'moved')

    def test_missing_message_blocks_move_or_undo(self):
        self.server.messages['INBOX'].clear()
        with self.assertRaises(ValueError):
            self.move()
        self.server.messages['INBOX'].add('3')
        action = self.move()
        self.server.messages['Archive'].clear()
        with self.assertRaises(ValueError):
            self.undo(action)
        self.assertEqual(len(self.moves()), 1)

    def test_unsupported_capability_or_nonsticky_identity_never_moves(self):
        for caps, sticky in [((b'UIDPLUS',), True), ((b'MOVE',), True), ((b'MOVE', b'UIDPLUS'), False)]:
            self.server.capabilities, self.server.sticky = caps, sticky
            with self.assertRaises(ValueError):
                self.move()
        self.assertFalse(self.moves())

    def test_undo_lost_acknowledgement_is_not_repeatable(self):
        action = self.move()
        self.server.outcome = 'disconnect'
        self.assertEqual(self.undo(action)['status'], 'undo_uncertain')
        self.store = FilingStore(self.path)
        with self.assertRaises(ValueError):
            self.undo(action)
        self.assertEqual(len(self.moves()), 2)

    def test_restart_after_undo_claim_does_not_offer_retry(self):
        action = self.move()
        self.store.claim_undo('one', action['id'])
        self.store = FilingStore(self.path)
        with self.assertRaises(ValueError):
            self.undo(action)
        self.assertEqual(len(self.moves()), 1)

    def test_account_scope_and_request_payload_are_enforced(self):
        action = self.move(request_key='job-1')
        with self.assertRaises(ValueError):
            self.store.get('two', action['id'])
        self.assertFalse(self.store.list('two')['items'])
        self.assertIsNone(self.store.request('two', 'job-1'))
        with self.assertRaises(ValueError):
            move(self.account, self.store, 'Archive', action['destination']['uid'], 'INBOX',
                 self.connector, request_key='job-1')
        self.assertEqual(len(self.moves()), 1)

    def test_only_one_concurrent_undo_claim_succeeds(self):
        action = self.move()
        def claim(_):
            try:
                FilingStore(self.path).claim_undo('one', action['id'])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(claim, range(2))), 1)

    def test_pagination_is_account_scoped_and_complete(self):
        for index in range(30):
            self.store.begin('one', {'folder': 'INBOX', 'validity': '7', 'uid': str(index + 1)},
                             'Archive', str(index), 'owner', 'Keep for later')
        first = self.store.list('one')
        second = self.store.list('one', first['next_cursor'])
        self.assertEqual(len(first['items']), 25)
        self.assertEqual(len(second['items']), 5)
        self.assertEqual(len({item['id'] for item in first['items'] + second['items']}), 30)
        self.assertIsNone(second['next_cursor'])

    def test_guard_revocation_immediately_before_move_prevents_action(self):
        calls = []
        def guard():
            calls.append(True)
            if len(calls) == 2:
                raise ValueError('Paused')
        self.assertEqual(self.move(guard=guard)['status'], 'uncertain')
        self.assertFalse(self.moves())

    def test_agent_grant_defaults_off_and_pause_rejects_filing(self):
        cfg = Mock()
        cfg.account.return_value = self.account
        agent = MailboxAgent(cfg, self.store, 'one', self.connector)
        ref = agent.ref('INBOX', '7', '3')
        self.store.set_profile('one', True, 'File newsletters')
        with self.assertRaisesRegex(ValueError, 'not allowed'):
            agent.file(ref, 'Archive', 'work-1', 'Newsletter')
        self.store.set_profile('one', False, 'File newsletters', allow_filing=True)
        with self.assertRaisesRegex(ValueError, 'paused'):
            agent.file(ref, 'Archive', 'work-1', 'Newsletter')
        self.connector.assert_not_called()

    def test_agent_requires_full_read_and_respects_grant_changes(self):
        cfg = Mock()
        cfg.account.return_value = self.account
        agent = MailboxAgent(cfg, self.store, 'one', self.connector)
        ref = agent.ref('INBOX', '7', '3')
        self.store.set_profile('one', True, 'File newsletters', allow_filing=True)
        agent.read = Mock(return_value={'truncated': True})
        with self.assertRaisesRegex(ValueError, 'full bounded'):
            agent.file(ref, 'Archive', 'work-1', 'Newsletter')
        def read(*_):
            self.store.set_profile('one', True, 'File newsletters', allow_filing=False)
            return {'truncated': False}
        agent.read = read
        with self.assertRaisesRegex(ValueError, 'not allowed'):
            agent.file(ref, 'Archive', 'work-1', 'Newsletter')
        self.assertFalse(self.moves())

    def test_folder_quoting_rejects_injection_and_preserves_literals(self):
        self.assertEqual(quote('A"B\\C'), '"A\\"B\\\\C"')
        for name in ('', 'INBOX\r\nEXPUNGE', 'bad\x00folder', None):
            with self.assertRaises(ValueError):
                quote(name)

    def test_agent_keeps_mail_with_pending_or_uncertain_reply_available(self):
        cfg = Mock()
        cfg.account.return_value = self.account
        agent = MailboxAgent(cfg, self.store, 'one', self.connector)
        ref = agent.ref('INBOX', '7', '3')
        self.store.set_profile('one', True, 'File newsletters', allow_filing=True)
        agent.read = Mock(return_value={'truncated': False})
        draft = self.store.save_draft('one', 'reply', {'to':'person@example.com', 'subject':'Reply',
                                     'body':'Thanks', 'reason':'Respond', 'reply_ref':ref})
        for status in ('pending', 'sending', 'uncertain'):
            with self.subTest(status=status):
                with self.store.connect() as db:
                    db.execute('UPDATE drafts SET status=? WHERE id=?', (status, draft['id']))
                with self.assertRaisesRegex(ValueError, 'owner review'):
                    agent.file(ref, 'Archive', 'work-1', 'Newsletter')
        self.assertFalse(self.moves())

    def test_filing_tool_is_hidden_until_granted_and_hidden_while_paused(self):
        from agent_mcp import MCPServer
        cfg = Mock()
        cfg.account.return_value = self.account
        agent = MailboxAgent(cfg, self.store, 'one', self.connector)
        server = MCPServer(agent)
        self.store.set_profile('one', True, 'File newsletters')
        self.assertNotIn('mailbox_file', [tool['name'] for tool in server.available_tools()])
        self.store.set_profile('one', True, 'File newsletters', allow_filing=True)
        self.assertIn('mailbox_file', [tool['name'] for tool in server.available_tools()])
        self.store.set_profile('one', False, 'File newsletters')
        self.assertTrue(self.store.profile('one')['allow_filing'])
        self.assertNotIn('mailbox_file', [tool['name'] for tool in server.available_tools()])

    def test_copyuid_mapping_is_exact_for_one_message(self):
        imap = Mock()
        for value, expected in [(b'8 3 103', '103'), (b'8 3:3 103:103', '103'),
                                (b'9 3 103', None), (b'8 4 103', None), (b'8 3 0', None),
                                (b'8 3:4 103:104', None), (b'8 3,4 103,104', None)]:
            imap.response.return_value = ('COPYUID', [value])
            self.assertEqual(mapping(imap, [], '3', '8'), expected)
        imap.response.return_value = ('COPYUID', [None])
        self.assertEqual(mapping(imap, [b'[COPYUID 8 3 103] Moved'], '3', '8'), '103')


if __name__ == '__main__':
    unittest.main()
