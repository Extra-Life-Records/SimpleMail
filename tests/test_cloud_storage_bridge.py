import base64
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch
import mailapp
from cloud_accounts import CloudAccounts
from cloud_storage_bridge import SentCopies, flush


class StorageBridgeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        for attr,value in [('CONFIG_DIR',Path(temp.name)),('CONFIG_FILE',Path(temp.name)/'config.json')]:
            p=patch.object(mailapp,attr,value);p.start();self.addCleanup(p.stop)
        self.cfg=mailapp.Config()
        self.cfg.data['accounts']=[mailapp.normalize_account({'id':'playloudr','label':'Playloudr',
            'email':'playloudr@extraliferecords.com','from_email':'hello@playloudr.com',
            'smtp_host':'smtp.resend.com','smtp_user':'resend','smtp_password':'fixture',
            'cloud_mailbox':'mail-playloudr','cloud_generation':'pinned','signature':'Original'})]
        self.api=mailapp.Api(self.cfg);self.transport=Mock();self.transport.cloud_config.return_value={'configured':True}
        self.provider=CloudAccounts(self.transport,self.api);self.api._cloud_provider=self.provider
        self.row={'name':'mail-playloudr','address':'playloudr@extraliferecords.com','state':'Reserved',
                  'generation':'pinned','external_sent_copy':True}
        self.requests=[];self.fail_copy=False
        self.transport.cloud_request.side_effect=self.request
        self.provider.discover()

    def request(self, method, path, data=None):
        self.requests.append((method,path,data))
        if path=='/mailboxes':return {'mailboxes':[self.row]}
        if path.endswith('/sent-copy'):
            if self.fail_copy:raise TimeoutError('fixture')
            return {'status':'stored','generation':data['generation'],'sha256':hashlib.sha256(base64.b64decode(data['raw'])).hexdigest()}
        return {'messages':[],'cursor':None}

    def draft(self):
        return self.api.save_compose_draft('playloudr',str(uuid.uuid4()),0,'owner@example.com','Fixture','Body','',
                                          bcc='private@example.com')

    def test_read_routes_to_existing_cloud_mailbox_and_never_imap(self):
        with patch('mailapp.connect_imap',side_effect=AssertionError('IMAP forbidden')):
            self.api.list_messages('playloudr','Inbox')
        self.assertTrue(any(p.startswith('/mailboxes/mail-playloudr/messages?') for _,p,_ in self.requests))
        self.assertEqual(self.api._acct('playloudr')['identity'],'hello@playloudr.com')

    def test_send_keeps_resend_identity_and_bcc_private_with_durable_copy(self):
        draft=self.draft()
        with patch('smtplib.SMTP') as smtp,patch('mailapp.connect_imap',side_effect=AssertionError('IMAP forbidden')):
            conn=smtp.return_value.__enter__.return_value;conn.sendmail.return_value={}
            result=self.api.send_compose_draft('playloudr',draft['id'],draft['revision'])
            args=conn.sendmail.call_args.args
            self.assertEqual(args[0],'hello@playloudr.com');self.assertIn('private@example.com',args[1])
            self.assertNotIn(b'Bcc:',args[2]);self.assertIn(b'hello@playloudr.com',args[2])
            self.assertTrue(result['sent_copy_saved'])
            with self.assertRaises(ValueError):self.api.send_compose_draft('playloudr',draft['id'],draft['revision'])
            conn.sendmail.assert_called_once()

    def test_lost_storage_response_retries_copy_not_smtp_after_restart(self):
        draft=self.draft();self.fail_copy=True
        with patch('smtplib.SMTP') as smtp:
            conn=smtp.return_value.__enter__.return_value;conn.sendmail.return_value={}
            result=self.api.send_compose_draft('playloudr',draft['id'],draft['revision'])
        self.assertEqual(result['status'],'sent');self.assertFalse(result['sent_copy_saved'])
        self.fail_copy=False
        with patch('smtplib.SMTP',side_effect=AssertionError('Must not resend')):
            flush(self.provider,'playloudr')
        self.assertEqual(SentCopies(self.api._compose_store()).pending('playloudr'),[])

    def test_uncertain_smtp_is_preserved_and_not_retried_or_uploaded(self):
        draft=self.draft()
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.sendmail.side_effect=TimeoutError()
            result=self.api.send_compose_draft('playloudr',draft['id'],draft['revision'])
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(flush(self.provider,'playloudr'),0)
        self.assertFalse(any(p.endswith('/sent-copy') for _,p,_ in self.requests))

    def test_settings_and_drafts_keep_original_id_credentials_and_binding(self):
        draft=self.draft();cfg=self.api.get_config()
        row=next(a for a in cfg['accounts'] if a['id']=='playloudr');row['signature']='Updated'
        row['from_email']='spoof@example.com';self.api.save_config(cfg)
        account=self.cfg.account('playloudr')
        self.assertEqual(account['smtp_password'],'fixture');self.assertEqual(account['from_email'],'hello@playloudr.com')
        self.assertEqual(account['cloud_generation'],'pinned');self.assertEqual(account['signature'],'Updated')
        self.assertEqual(self.api.get_compose_draft('playloudr',draft['id'])['status'],'pending')

    def test_generation_change_blocks_sending_before_smtp_and_no_imap_fallback(self):
        draft=self.draft();self.row['generation']='reassigned'
        with patch('smtplib.SMTP',side_effect=AssertionError('Must not send')):
            with self.assertRaises(ValueError):self.api.send_compose_draft('playloudr',draft['id'],draft['revision'])
        with self.assertRaises(ValueError):mailapp.connect_imap(self.cfg.account('playloudr'))
