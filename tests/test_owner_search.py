"""Complete owner search and stable result actions, with fake mail only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from email.message import EmailMessage

import mailapp
from agent_store import AgentStore
from mailbox_agent import pack


class SearchServer:
    def __init__(self):
        self.folder = None
        self.validity = {'INBOX':'7','Archive':'8','Junk':'9','Trash':'10'}
        self.calls = []
        self.readonly = []
        self.literal = None
        self.sticky = True

    def select(self, folder, readonly=True):
        self.folder = folder[1:-1]
        self.readonly.append(readonly)
        return 'OK', [b'120']

    def response(self, name):
        if name == 'UIDNOTSTICKY':
            return name, [None] if self.sticky else [b'']
        return name, [self.validity[self.folder].encode()]

    def logout(self):
        pass

    def uid(self, command, *args):
        self.calls.append((self.folder, command, args))
        if command == 'search':
            assert args == ('CHARSET', 'UTF-8', 'TEXT')
            assert self.literal == 'body-only café'.encode()
            return 'OK', [b' '.join(str(uid).encode() for uid in
                                   (range(1,121) if self.folder == 'INBOX' else [1]))]
        self.assert_peek(args[1])
        if args[1] == '(UID FLAGS)':
            return 'OK', [f'1 (UID {uid} FLAGS ())'.encode() for uid in args[0].split(',')]
        raw = b'From: sender@example.com\r\nSubject: unrelated subject\r\n\r\n'
        return 'OK', [(f'1 (UID {uid} BODY[HEADER.FIELDS] {{50}})'.encode(),raw)
                      for uid in args[0].split(',')]

    @staticmethod
    def assert_peek(spec):
        assert spec == '(UID FLAGS)' or 'BODY.PEEK' in spec


class OwnerSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name)/'mail.sqlite3')
        self.store.set_profile('one',False,'Prepare replies')
        cfg = Mock()
        def account(identifier):
            if identifier != 'one':
                raise ValueError('Unknown mailbox')
            return {'id':'one','label':'One','email':'one@example.com'}
        cfg.account.side_effect = account
        self.api = mailapp.Api(cfg)
        self.api._agent_store = lambda:self.store
        self.drafts = Mock()
        self.drafts.list.return_value = []
        self.api._compose_store = lambda:self.drafts
        self.server = SearchServer()
        self.connector = Mock(return_value=(self.server,list(self.server.validity),None))
        patcher = patch.object(mailapp,'connect_imap',self.connector)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_body_search_paginates_beyond_loaded_inbox_and_other_folder_same_uid(self):
        page = self.api.search_messages('one','body-only café')
        items = page['items'][:]
        while page['next_cursor']:
            page = self.api.search_messages('one','body-only café',page['next_cursor'])
            items.extend(page['items'])
        self.assertEqual(len(items),121)
        self.assertEqual(len({item['uid'] for item in items}),121)
        self.assertEqual({item['server_folder'] for item in items},{'INBOX','Archive'})
        self.assertEqual(sum(item['server_uid']=='1' for item in items),2)
        self.assertTrue(all(self.server.readonly))
        self.assertFalse(self.store.profile('one')['enabled'])
        self.assertFalse(any(call[1] == 'store' for call in self.server.calls))

    def test_junk_and_trash_can_be_searched_in_their_selected_folder(self):
        for folder in ('Junk','Trash'):
            page = self.api.search_messages('one','body-only café',server_folder=folder)
            self.assertEqual([item['server_folder'] for item in page['items']],[folder])

    def test_cursor_cannot_cross_query_account_or_recreated_folder(self):
        page = self.api.search_messages('one','body-only café')
        with self.assertRaises(ValueError):
            self.api.search_messages('one','other query',page['next_cursor'])
        with self.assertRaises(ValueError):
            self.api.search_messages('two','body-only café',page['next_cursor'])
        self.server.validity['INBOX']='99'
        with self.assertRaises(ValueError):
            self.api.search_messages('one','body-only café',page['next_cursor'])

    def test_local_draft_body_and_recipient_are_searchable_once(self):
        self.drafts.list.return_value = [{'id':'draft-one','updated_at':'today',
            'payload':{'to':'person@example.com','subject':'Draft','body':'body-only café'}}]
        first = self.api.search_messages('one','body-only café')
        self.assertEqual(first['items'][0]['localDraftId'],'draft-one')
        second = self.api.search_messages('one','body-only café',first['next_cursor'])
        self.assertFalse(any(item.get('localDraftId') for item in second['items']))

    def test_recreated_folder_blocks_read_flags_attachment_and_quote_before_uid_command(self):
        # Every owner result action validates its observed folder identity first.
        for action in (lambda:self.api.get_message('one','INBOX','1','99'),
                       lambda:self.api.set_seen('one','INBOX','1',False,'99'),
                       lambda:self.api.save_attachment('one','INBOX','1',0,'99'),
                       lambda:self.api.get_compose_context('one','INBOX','1',False,False,'99')):
            with self.assertRaisesRegex(ValueError,'Folder changed'):
                action()
        self.assertFalse(self.server.calls)

    def test_read_keeps_verified_reference_after_consuming_uidvalidity_response(self):
        message = EmailMessage()
        message['Subject']='A result'
        message.set_content('Body')
        server = Mock()
        server.select.return_value = ('OK',[b'1'])
        server.response.side_effect = [('UIDVALIDITY',[b'7']),('UIDNOTSTICKY',[None])]
        server.uid.side_effect = [('OK',[(b'1 (UID 1 RFC822)',message.as_bytes())]),('OK',[])]
        self.connector.return_value = (server,['INBOX'],None)
        result = self.api.get_message('one','INBOX','1','7')
        self.assertEqual(result['message_ref'],pack({'account':'one','folder':'INBOX','validity':'7','uid':'1'}))
        self.assertEqual(server.response.call_count,2)

    def test_nonsticky_server_cannot_issue_or_act_on_persistent_search_references(self):
        self.server.sticky = False
        with self.assertRaisesRegex(ValueError,'stable message'):
            self.api.search_messages('one','body-only café')
        with self.assertRaisesRegex(ValueError,'stable message'):
            self.api.get_message('one','INBOX','1','7')
        self.assertFalse(self.server.calls)


if __name__ == '__main__':
    unittest.main()
