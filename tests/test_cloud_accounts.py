import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mailapp
from cloud_accounts import CloudAccounts
from cloud_mail import CloudSignInRequired, CloudServiceError


class UnifiedInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = patch.object(mailapp, 'CONFIG_DIR', Path(self.tmp.name))
        self.directory.start(); self.addCleanup(self.directory.stop)
        self.file = patch.object(mailapp, 'CONFIG_FILE', Path(self.tmp.name) / 'config.json')
        self.file.start(); self.addCleanup(self.file.stop)
        self.cfg = mailapp.Config()
        self.cfg.data['accounts'] = [mailapp.normalize_account({'id':'hello', 'email':'hello@example.com'})]
        self.api = mailapp.Api(self.cfg)
        self.transport = Mock()
        self.transport.cloud_config.return_value = {'configured': True}
        self.provider = CloudAccounts(self.transport, self.api)
        self.api._cloud_provider = self.provider
        self.transport.cloud_request.return_value = {'mailboxes':[
            {'name': n, 'address': n+'@extraliferecords.com', 'state':'Reserved'}
            for n in ['one','two','three','four','five']]}
        self.provider.discover()
        self.transport.reset_mock()

    def test_all_accounts_share_config_without_persisting_cloud_as_imap(self):
        cfg = self.api.get_config()
        self.assertEqual(len(cfg['accounts']), 6)
        self.api.save_config(cfg)
        self.assertEqual([a['id'] for a in self.cfg.accounts()], ['hello'])
        self.assertNotIn('refresh_token', str(cfg))

    def test_imap_routing_unchanged_and_bridge_signature_preserved(self):
        with patch('mailapp.connect_imap', side_effect=ValueError('imap fixture')) as connect:
            with self.assertRaisesRegex(ValueError, 'imap fixture'):
                self.api.list_messages('hello', 'INBOX')
            connect.assert_called_once()
        self.assertEqual(inspect.getfullargspec(self.api.get_message).args,
                         ['self','account_id','server_folder','uid','validity'])

    def test_filtered_empty_pages_are_followed(self):
        self.transport.cloud_request.side_effect = [
            {'messages':[], 'cursor':'next'},
            {'messages':[{'id':'abc','folder':'Inbox','sender':'sender@example.com','seen':False}], 'cursor':None}]
        rows = self.api.list_messages('cloud:one', 'Inbox')
        self.assertEqual(rows['envelopes'][0]['uid'], 'abc')
        self.assertEqual(rows['folder_unread'], 1)
        self.assertIn('cursor=next', self.transport.cloud_request.call_args.args[1])

    def test_read_and_attachment_stay_in_selected_mailbox(self):
        self.transport.cloud_request.side_effect = [
            {'body':'fixture body', 'html':'<a href="https://example.com/invite">Accept invite</a>', 'attachments':[{'part':2,'name':'a.txt','size':1}]}, {'ok':True}]
        row = self.api.get_message('cloud:two', 'Inbox', 'abc')
        self.assertEqual(row['text'], 'fixture body')
        self.assertIn('https://example.com/invite', row['html'])
        self.assertEqual(row['attachments'][0]['index'], 2)
        for call in self.transport.cloud_request.call_args_list:
            self.assertTrue(call.args[1].startswith('/mailboxes/two/messages/abc'))
        self.transport.cloud_request.side_effect = None
        self.transport.cloud_request.return_value = {'name':'a.txt','data':'YQ=='}
        self.transport.cloud_save_attachment.return_value = {'path':'selected/a.txt'}
        self.assertEqual(self.api.save_attachment('cloud:two','Inbox','abc',2)['path'], 'selected/a.txt')

    def test_invalid_message_path_never_reaches_transport(self):
        self.transport.reset_mock()
        with self.assertRaises(ValueError): self.api.get_message('cloud:one','Inbox','../two')
        self.transport.cloud_request.assert_not_called()

    def test_cloud_draft_recovery_is_account_scoped_and_send_gate_preserves_it(self):
        draft = self.api.save_compose_draft('cloud:one', '12345678-1234-1234-1234-123456789abc', 0, 'a@example.com','Test','Body','Body')
        self.assertEqual(len(self.api.list_compose_drafts('cloud:one')), 1)
        self.assertEqual(self.api.list_compose_drafts('cloud:two'), [])
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            self.api.send_compose_draft('cloud:one', draft['id'], draft['revision'])
        self.assertEqual(self.api.get_compose_draft('cloud:one', draft['id'])['status'], 'pending')

    def test_discovery_failure_does_not_remove_existing_imap_accounts(self):
        self.transport.cloud_request.side_effect = ValueError('expired session')
        cfg = self.api.get_config()
        self.assertEqual([a['id'] for a in cfg['accounts']], ['hello'])
        self.assertIn('unavailable', cfg['cloud_error'])

    def test_discovery_reports_safe_expiry_and_service_errors_separately(self):
        for error in (CloudSignInRequired('Reconnect employee mailboxes in Settings.'),
                      CloudServiceError('Mailbox service is temporarily unavailable. Please try again.')):
            with self.subTest(error=type(error).__name__):
                self.transport.cloud_request.side_effect = error
                cfg = self.api.get_config()
                self.assertEqual(cfg['cloud_error'], str(error))
                self.assertEqual([a['id'] for a in cfg['accounts']], ['hello'])

    def test_unexpected_discovery_error_does_not_disclose_private_details(self):
        self.transport.cloud_request.side_effect = RuntimeError('private provider detail')
        self.assertNotIn('private', self.api.get_config()['cloud_error'])

    def test_reply_all_excludes_selected_identity(self):
        self.transport.cloud_request.return_value = {
            'sender':'Sender <sender@example.com>', 'to':'two@extraliferecords.com',
            'cc':'one@extraliferecords.com', 'body':'Hello', 'message_id':'<original@example.com>'}
        context = self.api.get_compose_context('cloud:two','Inbox','abc',True)
        self.assertIn('sender@example.com', context['to'])
        self.assertEqual(context['cc'], 'one@extraliferecords.com')
        self.assertEqual(context['in_reply_to'], '<original@example.com>')


if __name__ == '__main__': unittest.main()
