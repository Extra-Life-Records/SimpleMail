"""Actual MIME serialization, attachment recovery and recipient selection."""
import base64
import sys
import tempfile
import unittest
import uuid
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mail_attachments import AttachmentStore, reply_context, MAX_FILE
from compose_store import ComposeStore
import mailapp


class CorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.attachments = AttachmentStore(self.path)
        self.drafts = ComposeStore(self.path)
        self.acct = dict(id='one',email='one@example.com',from_email='',label='One',smtp_user='',
                         password='test',smtp_host='example.com',smtp_port=587,smtp_starttls=True)
        class Config:
            def account(inner, account):
                if account != 'one': raise ValueError('Unknown mailbox')
                return self.acct
        self.api = mailapp.Api(Config())
        self.api._attachment_store = lambda: self.attachments
        self.api._compose_store = lambda: self.drafts

    def test_reply_to_all_deduplicates_and_excludes_own_addresses(self):
        msg = EmailMessage()
        msg['From'] = 'Sender <sender@example.com>'
        msg['Reply-To'] = 'Help <help@example.com>'
        msg['To'] = 'one@example.com, customer@example.com'
        msg['Cc'] = 'CUSTOMER@example.com, help@example.com, alias@example.com, third@example.com'
        msg['Message-ID'] = '<message@example.com>'
        msg['References'] = '<earlier@example.com>'
        result = reply_context(msg, ['one@example.com','alias@example.com'], True)
        self.assertEqual(result['to'], 'Help <help@example.com>')
        self.assertEqual(result['cc'], 'customer@example.com, third@example.com')
        self.assertEqual(result['references'], '<earlier@example.com> <message@example.com>')
        self.assertEqual(reply_context(msg,['one@example.com'])['cc'],'')

    def test_sent_message_replies_to_original_recipient(self):
        msg=EmailMessage()
        msg['From']='one@example.com';msg['To']='customer@example.com';msg['Cc']='other@example.com'
        result=reply_context(msg,['one@example.com'],True)
        self.assertEqual(result['to'],'customer@example.com')
        self.assertEqual(result['cc'],'other@example.com')

    def test_attachment_bytes_survive_restart_and_cannot_cross_mailboxes(self):
        meta = self.attachments.add_base64('one','invoice.pdf',base64.b64encode(b'\x00\xffinvoice').decode())
        recovered=AttachmentStore(self.path).resolve('one',[meta['id']])
        self.assertEqual(recovered[0]['data'],b'\x00\xffinvoice')
        self.assertEqual(self.attachments.add('one','invoice.pdf',b'\x00\xffinvoice')['id'],meta['id'])
        with self.assertRaises(ValueError): self.attachments.resolve('two',[meta['id']])

    def test_invalid_oversized_and_duplicate_attachments_rejected(self):
        for name,data in [('../secret',b'abc'),('large.bin',b'x'*(MAX_FILE+1))]:
            with self.assertRaises(ValueError): self.attachments.add('one',name,data)
        with self.assertRaises(ValueError): self.attachments.add_base64('one','bad.bin','%%bad%%')
        meta=self.attachments.add('one','one.txt',b'abc')
        with self.assertRaises(ValueError): self.attachments.resolve('one',[meta['id'],meta['id']])

    def test_human_draft_recovers_recipients_headers_and_attachment_metadata(self):
        meta=self.attachments.add('one','invoice.pdf',b'PDF bytes')
        draft=self.api.save_compose_draft('one',str(uuid.uuid4()),0,'to@example.com','Reply','Body','<p>Body</p>',
            'cc@example.com','hidden@example.com','<original@example.com>','<original@example.com>',[meta['id']])
        recovered=self.api.get_compose_draft('one',draft['id'])
        self.assertEqual(recovered['attachments'][0]['name'],'invoice.pdf')
        self.assertNotIn('data',recovered['attachments'][0])
        self.assertEqual(recovered['payload']['bcc'],'hidden@example.com')
        self.assertEqual(recovered['payload']['in_reply_to'],'<original@example.com>')

    def test_actual_smtp_message_contains_file_bytes_and_thread_headers(self):
        meta=self.attachments.add('one','invoice.pdf',b'PDF bytes')
        smtp=Mock();smtp.send_message.return_value={}
        captured=[]
        smtp.send_message.side_effect=lambda msg: captured.append(msg.as_bytes()) or {}
        with patch.object(mailapp.smtplib,'SMTP') as factory:
            factory.return_value.__enter__.return_value=smtp
            mailapp.send_message(self.acct,'to@example.com','Re: Hello','Body',save_sent=False,
                cc='cc@example.com',bcc='hidden@example.com',in_reply_to='<original@example.com>',
                references='<original@example.com>',attachments=self.attachments.resolve('one',[meta['id']]))
        msg=mailapp.message_from_bytes(captured[0],policy=mailapp.email_policy)
        self.assertEqual(msg['In-Reply-To'],'<original@example.com>')
        self.assertEqual(msg['Cc'],'cc@example.com')
        attached=list(msg.iter_attachments())
        self.assertEqual(attached[0].get_filename(),'invoice.pdf')
        self.assertEqual(attached[0].get_payload(decode=True),b'PDF bytes')

    def test_missing_attachment_rejected_before_claim_or_smtp(self):
        draft=self.drafts.save('one',str(uuid.uuid4()),0,{'to':'to@example.com','subject':'Subject',
            'body':'Body','body_html':'Body','attachment_ids':['missing']})
        with patch.object(mailapp,'send_message') as send:
            with self.assertRaises(ValueError): self.api.send_compose_draft('one',draft['id'],1)
            send.assert_not_called()
        self.assertEqual(self.drafts.get('one',draft['id'])['status'],'pending')

    def test_forward_preserves_attachment_without_marking_original_read(self):
        msg=EmailMessage();msg['From']='person@example.com';msg['To']='one@example.com';msg['Subject']='Document'
        msg.set_content('See attached');msg.add_attachment(b'original file',maintype='application',subtype='pdf',filename='invoice.pdf')
        raw=msg.as_bytes();imap=Mock();imap.select.return_value=('OK',[b'1'])
        imap.uid.side_effect=[('OK',[f'1 (RFC822.SIZE {len(raw)})'.encode()]),('OK',[(b'1 BODY[]',raw)])]
        with patch.object(mailapp,'connect_imap',return_value=(imap,[],None)):
            result=self.api.get_compose_context('one','INBOX','1',False,True)
        self.assertEqual(result['attachments'][0]['name'],'invoice.pdf')
        self.assertEqual(self.attachments.resolve('one',[result['attachments'][0]['id']])[0]['data'],b'original file')
        self.assertNotIn('in_reply_to',result)
        imap.select.assert_called_once_with('"INBOX"',readonly=True)
        imap.uid.assert_any_call('fetch','1','(BODY.PEEK[])')


if __name__ == '__main__': unittest.main()
