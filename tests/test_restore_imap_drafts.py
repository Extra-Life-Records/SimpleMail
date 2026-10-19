import importlib.util
import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from compose_store import ComposeStore
from mail_attachments import AttachmentStore

spec = importlib.util.spec_from_file_location('restore_imap', Path(__file__).parents[1] / 'cloud' / 'restore_imap_drafts.py')
restore = importlib.util.module_from_spec(spec); spec.loader.exec_module(restore)


class ImapDraftRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.database = self.root / 'drafts.sqlite3'
        self.store = ComposeStore(self.database); self.attachments = AttachmentStore(self.database)
        self.account = 'cloud:mail-robots'; self.registry = {'mail-robots': {'address': 'hello@example.com'}}
        self.manifest = {'source_id': 'a' * 64, 'username': 'hello@example.com'}

    def row(self, message, identity='b' * 64):
        path = self.root / (identity + '.eml'); path.write_bytes(message.as_bytes())
        return {'id': identity, 'path': path, 'present': True, 'folder': 'Drafts', 'received': '2026-10-19T10:00:00Z'}

    def run_restore(self, rows, apply=True):
        with patch.object(restore.mail_migration, 'archive', return_value=(self.manifest, rows, ['Drafts'])):
            return restore.restore(self.database, self.root, self.account, self.registry, apply)

    def test_html_only_empty_recipient_draft_is_editable_and_repeated_restore_is_safe(self):
        message = EmailMessage(); message['Subject'] = 'Not addressed yet'
        message.set_content('<p>Retain <b>formatting</b></p>', subtype='html')
        row = self.row(message); original = row['path'].read_bytes()
        preview = self.run_restore([row], apply=False)
        self.assertEqual(len(preview['copied']), 1); self.assertEqual(self.store.list(self.account), [])
        result = self.run_restore([row]); draft = self.store.get(self.account, result['copied'][0])
        self.assertEqual(draft['payload']['to'], '')
        self.assertEqual(draft['payload']['body_html'], message.get_content())
        self.assertIn('Retain formatting', draft['payload']['body'])
        self.assertEqual(self.run_restore([row])['existing'], result['copied'])
        changed = {**draft['payload'], 'body_html': '<p>Owner edit</p>'}
        self.store.save(self.account, draft['id'], draft['revision'], changed)
        self.assertEqual(len(self.run_restore([row])['conflicts']), 1)
        self.assertEqual(self.store.get(self.account, draft['id'])['payload'], changed)
        self.assertEqual(row['path'].read_bytes(), original)

    def test_headers_and_attachment_bytes_keep_account_scope(self):
        message = EmailMessage()
        for header, value in {'To': 'to@example.com', 'Cc': 'cc@example.com', 'Bcc': 'bcc@example.com',
                              'Subject': 'Reply', 'In-Reply-To': '<original@example.com>',
                              'References': '<first@example.com> <original@example.com>'}.items(): message[header] = value
        message.set_content('Plain'); message.add_alternative('<p>Formatted</p>', subtype='html')
        message.add_attachment(b'attachment unchanged', maintype='application', subtype='octet-stream', filename='file.bin')
        result = self.run_restore([self.row(message)])
        draft = self.store.get(self.account, result['copied'][0])['payload']
        self.assertEqual(draft['references'], str(message['References']))
        self.assertEqual(draft['bcc'], 'bcc@example.com')
        self.assertEqual(draft['in_reply_to'], '<original@example.com>')
        attachments = self.attachments.resolve(self.account, draft['attachment_ids'])
        self.assertEqual(attachments[0]['data'], b'attachment unchanged')
        with self.assertRaises(ValueError): self.attachments.resolve('cloud:mail-elr', draft['attachment_ids'])
        self.assertEqual(len(self.run_restore([self.row(message)])['existing']), 1)

    def test_identity_mismatch_and_unsupported_cid_fail_before_writing(self):
        message = EmailMessage(); message.set_content('<p>Body</p>', subtype='html')
        row = self.row(message)
        self.registry['mail-robots']['address'] = 'other@example.com'
        with self.assertRaisesRegex(ValueError, 'match'): self.run_restore([row])
        self.registry['mail-robots']['address'] = 'hello@example.com'
        cid = EmailMessage(); cid.set_content('<img src="cid:image">', subtype='html')
        cid.add_related(b'pixels', maintype='image', subtype='png', cid='<image>')
        with self.assertRaisesRegex(ValueError, 'CID'):
            self.run_restore([row, self.row(cid, 'c' * 64)])
        self.assertEqual(self.store.list(self.account), [])

    def test_plain_body_and_headers_must_remain_editable_without_truncation(self):
        message = EmailMessage(); message.set_content('x' * 500001)
        with self.assertRaisesRegex(ValueError, 'body exceeds'):
            self.run_restore([self.row(message)])
        message = EmailMessage(); message['Subject'] = 'x' * 1001; message.set_content('Small body')
        with self.assertRaisesRegex(ValueError, 'header exceeds'):
            self.run_restore([self.row(message)])
        self.assertEqual(self.store.list(self.account), [])


if __name__ == '__main__': unittest.main()
