import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

from compose_store import ComposeStore
from mail_attachments import AttachmentStore

spec = importlib.util.spec_from_file_location('local_migration', Path(__file__).parents[1] / 'cloud' / 'migrate_local_drafts.py')
local = importlib.util.module_from_spec(spec); spec.loader.exec_module(local)


class DraftMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.drafts = ComposeStore(self.path); self.attachments = AttachmentStore(self.path)
        attachment = self.attachments.add('legacy', 'original.txt', b'unchanged bytes')
        self.identifier = str(uuid.uuid4())
        self.payload = {'to':'a@example.com','subject':'Unsent','body':'Plain','body_html':'<p>Original <b>HTML</b></p>',
                        'cc':'','bcc':'','in_reply_to':'<original@example.com>','references':'<first@example.com> <original@example.com>',
                        'attachment_ids':[attachment['id']]}
        self.drafts.save('legacy', self.identifier, 0, self.payload)

    def test_preview_and_idempotent_copy_preserve_source_html_thread_and_attachments(self):
        preview = local.copy_pending(self.path, 'legacy', 'cloud:mail-elr')
        self.assertEqual(len(preview['copied']), 1); self.assertEqual(self.drafts.list('cloud:mail-elr'), [])
        result = local.copy_pending(self.path, 'legacy', 'cloud:mail-elr', apply=True)
        again = local.copy_pending(self.path, 'legacy', 'cloud:mail-elr', apply=True)
        self.assertEqual(again['existing'], result['copied'])
        original = self.drafts.get('legacy', self.identifier)
        self.assertEqual(original['payload'], self.payload); self.assertEqual(original['status'], 'pending')
        copied = self.drafts.get('cloud:mail-elr', result['copied'][0])
        self.assertEqual(copied['payload']['body_html'], self.payload['body_html'])
        self.assertEqual(copied['payload']['references'], self.payload['references'])
        data = self.attachments.resolve('cloud:mail-elr', copied['payload']['attachment_ids'])
        self.assertEqual(data[0]['data'], b'unchanged bytes')
        with self.assertRaises(ValueError): self.attachments.resolve('legacy', copied['payload']['attachment_ids'])

    def test_uncertain_sends_are_not_copied_and_changed_target_is_not_overwritten(self):
        result = local.copy_pending(self.path, 'legacy', 'cloud:mail-elr', apply=True)
        target = result['copied'][0]
        saved = self.drafts.get('cloud:mail-elr', target)
        self.drafts.save('cloud:mail-elr', target, 1, {**saved['payload'], 'body':'Owner edit'})
        again = local.copy_pending(self.path, 'legacy', 'cloud:mail-elr', apply=True)
        self.assertEqual(again['conflicts'][0]['reason'], 'source_or_target_draft_changed_since_copy')
        self.drafts.claim('legacy', self.identifier, 1)
        blocked = local.copy_pending(self.path, 'legacy', 'cloud:mail-robots', apply=True)
        self.assertEqual(blocked['conflicts'][0]['reason'], 'source_send_outcome_requires_reconciliation')
        self.assertEqual(self.drafts.list('cloud:mail-robots'), [])


if __name__ == '__main__': unittest.main()
