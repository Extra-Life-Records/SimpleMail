import base64
import hashlib
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
import human_send
import service


@mock_aws
class ExternalSentTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', TABLE='storage', BUCKET='storage-test',
                         DOMAIN='extraliferecords.com', RECEIVE_DOMAIN='inbox.playloudr.com',
                         MAILBOX_REGISTRY=json.dumps({'mail-playloudr': {'address': 'playloudr@extraliferecords.com'}}),
                         HUMAN_SENDING_ENABLED='true', OWNER_CLIENT_ID='owner-client', SENDING_ENABLED='false')
        env.start(); self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='storage', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'} for k in ('pk', 'sk')],
            KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}])
        self.table = boto3.resource('dynamodb').Table('storage'); self.s3 = boto3.client('s3'); self.ses = Mock()
        self.s3.create_bucket(Bucket='storage-test', CreateBucketConfiguration={'LocationConstraint':'eu-west-1'})
        self.table.put_item(Item={'pk':'ADMIN','sk':'owner','enabled':True})
        self.table.put_item(Item={'pk':'mail-playloudr','sk':'META','state':'Reserved','generation':'pinned',
                                 'address':'playloudr@extraliferecords.com'})
        self.claims={'sub':'owner','client_id':'owner-client','token_use':'access','scope':'openid','exp':time.time()+300}
        msg=EmailMessage(); msg['From']='Playloudr <hello@playloudr.com>'; msg['To']='owner@example.com'
        msg['Message-ID']='<fixture@example.com>'; msg['Subject']='Fixture'; msg.set_content('Exact body')
        self.raw=msg.as_bytes()
        self.data={'request_id':'0123456789abcdef','generation':'pinned','raw':base64.b64encode(self.raw).decode()}
        self.path='/human/mailboxes/mail-playloudr/sent-copy'

    def save(self, **changes):
        return human_send.dispatch(self.table,self.s3,self.ses,self.claims,self.path,{**self.data,**changes})

    def test_retries_preserve_exact_bytes_and_owner_moves_without_sending(self):
        result=self.save(); self.assertEqual(result['sha256'],hashlib.sha256(self.raw).hexdigest())
        key={'pk':'mail-playloudr#pinned','sk':'MSG#'+result['id']}
        self.table.update_item(Key=key,UpdateExpression='SET folder=:f, seen=:s',ExpressionAttributeValues={':f':'Trash',':s':False})
        self.assertEqual(self.save(),result)
        row=self.table.get_item(Key=key)['Item']; self.assertEqual(row['folder'],'Trash'); self.assertFalse(row['seen'])
        self.assertEqual(self.s3.get_object(Bucket='storage-test',Key=row['key'])['Body'].read(),self.raw)
        self.assertEqual(self.ses.mock_calls,[])

    def test_changed_payload_rejected_and_original_retained(self):
        self.save()
        with self.assertRaises(ValueError): self.save(raw=base64.b64encode(self.raw+b'changed').decode())

    def test_failure_between_claim_and_upload_is_retryable_without_sending(self):
        with patch.object(self.s3,'put_object',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError): self.save()
        self.assertEqual(self.save()['status'],'stored'); self.assertEqual(self.ses.mock_calls,[])

    def test_wrong_client_assignment_sender_and_disabled_manual_are_denied(self):
        with patch.dict(self.claims,client_id='ai-client'):
            with self.assertRaises(service.Denied): self.save()
        with self.assertRaises(service.Denied): self.save(generation='new-generation')
        with self.assertRaises(ValueError): self.save(raw=base64.b64encode(self.raw.replace(b'hello@playloudr.com',b'other@example.com')).decode())
        with patch.dict(os.environ,HUMAN_SENDING_ENABLED='false'):
            with self.assertRaises(service.Denied): self.save()
        self.assertEqual(self.ses.mock_calls,[])

    def test_bcc_duplicate_sender_and_oversized_payload_are_rejected(self):
        for raw in [b'Bcc: private@example.com\n'+self.raw,b'From: other@example.com\n'+self.raw]:
            with self.assertRaises(ValueError): self.save(raw=base64.b64encode(raw).decode())
        with self.assertRaises(ValueError): self.save(raw='A'*4000004)
