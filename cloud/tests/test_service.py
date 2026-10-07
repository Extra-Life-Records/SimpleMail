import base64
import json
import os
import sys
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch
import boto3
from moto import mock_aws
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import service

@mock_aws
class MailTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,AWS_DEFAULT_REGION='eu-west-1',TABLE='mail',BUCKET='mail-testing',DOMAIN='playloudr.com',RECEIVE_DOMAIN='inbox.playloudr.com',SENDING_ENABLED='true')
        env.start();self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='mail',BillingMode='PAY_PER_REQUEST',AttributeDefinitions=[{'AttributeName':k,'AttributeType':'S'} for k in ('pk','sk','received')],KeySchema=[{'AttributeName':'pk','KeyType':'HASH'},{'AttributeName':'sk','KeyType':'RANGE'}],GlobalSecondaryIndexes=[{'IndexName':'Chronological','KeySchema':[{'AttributeName':'pk','KeyType':'HASH'},{'AttributeName':'received','KeyType':'RANGE'}],'Projection':{'ProjectionType':'ALL'}}])
        boto3.client('s3').create_bucket(Bucket='mail-testing',CreateBucketConfiguration={'LocationConstraint':'eu-west-1'})
        self.table,self.s3,self.ses=service.clients()
        for name in service.NAMES:self.table.put_item(Item={'pk':name,'sk':'META','generation':'first','state':'Reserved'})
        self.table.put_item(Item={'pk':'ADMIN','sk':'owner','enabled':True})
    def deliver(self,virus='PASS',body='Original body'):
        msg=EmailMessage();msg['From']='tester@example.com';msg['To']='unrelated@example.com';msg['Subject']='Mail test';msg.set_content(body)
        msg.add_attachment(b'private attachment',maintype='application',subtype='octet-stream',filename='sample.txt')
        self.s3.put_object(Bucket='mail-testing',Key='incoming/test',Body=msg.as_bytes())
        notification={'mail':{'timestamp':'2026-10-07T00:00:00Z'},'receipt':{'action':{'bucketName':'mail-testing','objectKey':'incoming/test'},'recipients':['one@inbox.playloudr.com'],'spamVerdict':{'status':'PASS'},'virusVerdict':{'status':virus}}}
        event={'Records':[{'Sns':{'Message':json.dumps(notification)}}]};service.receive(event,None);return event
    def call(self,subject='owner',method='GET',path='/mailboxes/one/messages',data=None,query=None):
        return service.dispatch(self.table,self.s3,self.ses,subject,method,path,data or {},query or {})
    def assign(self,name,subject,generation='first'):
        self.table.put_item(Item={'pk':name,'sk':'META','generation':generation,'state':'Assigned','subject':subject})
    def test_reserved_visible_only_to_owner(self):
        self.assertEqual(len(self.call(path='/mailboxes')['mailboxes']),5)
        self.assertEqual(self.call(subject='employee',path='/mailboxes')['mailboxes'],[])
        with self.assertRaises(service.Denied):self.call(subject='employee')
    def test_duplicate_and_attachment_isolation(self):
        event=self.deliver();service.receive(event,None);messages=self.call()['messages'];self.assertEqual(len(messages),1)
        mid=messages[0]['id'];self.assign('one','alice');self.assign('two','bob')
        attachment=self.call('alice',path=f'/mailboxes/one/messages/{mid}/attachments/2')
        self.assertEqual(base64.b64decode(attachment['data']),b'private attachment')
        for path in (f'/mailboxes/one/messages/{mid}',f'/mailboxes/one/messages/{mid}/attachments/2','/mailboxes/one/messages'):
            with self.assertRaises(service.Denied):self.call('bob',path=path)
        with self.assertRaises(service.Denied):self.call('bob',path=f'/mailboxes/two/messages/{mid}')
    def test_reassignment_isolates_history_including_retry(self):
        event=self.deliver();self.assign('one','new-person','second');service.receive(event,None)
        self.assertEqual(self.call('new-person')['messages'],[])
        with self.assertRaises(service.Denied):self.call('old-person')
    def test_quarantine_cannot_download(self):
        self.deliver(virus='FAIL');rows=self.call(query={'folder':'Junk'})['messages'];self.assertEqual(len(rows),1)
        with self.assertRaises(service.Denied):self.call(path='/mailboxes/one/messages/'+rows[0]['id'])
    def test_disabled_revokes_current_session(self):
        self.assign('one','alice');self.table.update_item(Key={'pk':'one','sk':'META'},UpdateExpression='SET #s=:s',ExpressionAttributeNames={'#s':'state'},ExpressionAttributeValues={':s':'Disabled'})
        with self.assertRaises(service.Denied):self.call('alice')
        with self.assertRaises(service.Denied):self.call('owner')
    def test_search_cursor_cannot_cross_mailbox(self):
        self.deliver(body='needle in body');self.assertEqual(len(self.call(query={'q':'needle'})['messages']),1)
        self.assertEqual(self.call(path='/mailboxes/two/messages',query={'q':'needle'})['messages'],[])
        with self.assertRaises(ValueError):self.call(query={'cursor':'two#first'})
    def test_send_is_not_repeated_after_timeout(self):
        ses=Mock();ses.send_email.side_effect=TimeoutError('uncertain')
        data={'to':'person@example.com','subject':'Hello','body':'Hi','request_id':'request-123456789'};item=service.authorize(self.table,'one','owner')
        with self.assertRaises(TimeoutError):service.send(self.table,self.s3,ses,item,'owner',data)
        result=service.send(self.table,self.s3,ses,item,'owner',data)
        self.assertEqual(result['status'],'uncertain');self.assertEqual(ses.send_email.call_count,1)
        with self.assertRaises(ValueError):service.send(self.table,self.s3,ses,item,'owner',{**data,'body':'different'})
    def test_sender_from_assignment_and_bcc_absent_from_raw(self):
        ses=Mock();ses.send_email.return_value={'MessageId':'provider-1'}
        data={'to':'person@example.com','bcc':'secret@example.com','subject':'Hello','body':'Hi','request_id':'request-123456789','from':'attacker@example.com'}
        result=service.send(self.table,self.s3,ses,service.authorize(self.table,'one','owner'),'owner',data)
        self.assertEqual(result['status'],'accepted');sent=ses.send_email.call_args.kwargs
        self.assertEqual(sent['FromEmailAddress'],'one@playloudr.com');self.assertNotIn(b'Bcc:',sent['Content']['Raw']['Data'])
        self.assertIn('secret@example.com',sent['Destination']['ToAddresses'])
    def test_expired_and_id_tokens_rejected(self):
        for claims in ({'sub':'owner','token_use':'access','exp':0},{'sub':'owner','token_use':'id','exp':int(time.time())+3600}):
            self.assertEqual(service.api({'requestContext':{'authorizer':{'jwt':{'claims':claims}}}},None)['statusCode'],403)
    def test_draft_roundtrip_and_trash(self):
        draft=self.call(method='POST',path='/mailboxes/one/drafts',data={'to':'a@example.com','body':'Saved'});path='/mailboxes/one/messages/'+draft['id']
        self.assertEqual(self.call(path=path)['draft']['body'],'Saved');self.call(method='PATCH',path=path,data={'folder':'Trash'})
        self.assertEqual(self.call(query={'folder':'Drafts'})['messages'],[]);self.assertEqual(len(self.call(query={'folder':'Trash'})['messages']),1)
    def test_long_body_search_beyond_index_and_unicode_bound(self):
        self.deliver(body='music '*40000+'deep-search-needle')
        self.assertEqual(len(self.call(query={'q':'deep-search-needle'})['messages']),1)
        self.assertLessEqual(len(service.search_text('\U0001f3b5'*180000).encode()),180000)
    def test_delayed_first_processing_cannot_enter_new_assignment(self):
        self.table.put_item(Item={'pk':'one','sk':'META','state':'Assigned','subject':'new-person','generation':'new','generation_started':'2026-10-08T00:00:00.000000Z'})
        self.deliver();self.assertEqual(self.call('new-person')['messages'],[])
    def test_delivery_reconciles_timeout_and_send_event_does_not_downgrade(self):
        ses=Mock();ses.send_email.side_effect=TimeoutError('lost response')
        item=service.authorize(self.table,'one','owner');data={'to':'a@example.com','body':'Hi','request_id':'request-123456789'}
        with self.assertRaises(TimeoutError):service.send(self.table,self.s3,ses,item,'owner',data)
        def event(kind):return {'Records':[{'Sns':{'Message':json.dumps({'eventType':kind,'mail':{'messageId':'provider-123','tags':{'mailbox':['one'],'generation':['first'],'request':['request-123456789']}}})}}]}
        service.delivery(event('Delivery'),None);service.delivery(event('Send'),None)
        self.assertEqual(self.call(path='/mailboxes/one/sends/request-123456789')['status'],'delivered')
        self.assertEqual(self.call(query={'folder':'Sent'})['messages'][0]['status'],'delivered')
    def test_explicit_rejection_is_a_failed_send(self):
        from botocore.exceptions import ClientError
        ses=Mock();ses.send_email.side_effect=ClientError({'Error':{'Code':'MessageRejected','Message':'Rejected'}},'SendEmail')
        data={'to':'a@example.com','body':'Hi','request_id':'request-123456789'}
        with self.assertRaises(ClientError):service.send(self.table,self.s3,ses,service.authorize(self.table,'one','owner'),'owner',data)
        self.assertEqual(self.call(path='/mailboxes/one/sends/request-123456789')['status'],'failed')
    def test_stale_draft_cannot_overwrite_newer_content(self):
        first=self.call(method='POST',path='/mailboxes/one/drafts',data={'body':'First'})
        second=self.call(method='POST',path='/mailboxes/one/drafts',data={**first,'body':'Second'})
        with self.assertRaises(ValueError):self.call(method='POST',path='/mailboxes/one/drafts',data={**first,'body':'Stale'})
        row=self.call(path='/mailboxes/one/messages/'+first['id'])
        self.assertEqual(row['draft']['body'],'Second');self.assertEqual(row['revision'],second['revision'])

if __name__=='__main__':unittest.main()
