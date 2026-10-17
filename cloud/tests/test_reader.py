import base64
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
import boto3
from moto import mock_aws
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import reader
import service

CLIENT = 'readerclient123'


@mock_aws
class ReaderTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', TABLE='mail', BUCKET='mail-testing',
                         DOMAIN='extraliferecords.com', RECEIVE_DOMAIN='inbox.playloudr.com',
                         READER_CLIENT_ID=CLIENT, READER_MAILBOXES='')
        env.start(); self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='mail', BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'} for k in ('pk', 'sk', 'received')], KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}], GlobalSecondaryIndexes=[{'IndexName': 'Chronological', 'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'received', 'KeyType': 'RANGE'}], 'Projection': {'ProjectionType': 'ALL'}}])
        boto3.client('s3').create_bucket(Bucket='mail-testing', CreateBucketConfiguration={'LocationConstraint': 'eu-west-1'})
        self.table, self.s3 = reader.clients()
        for name in service.NAMES:
            self.table.put_item(Item={'pk': name, 'sk': 'META', 'generation': 'first', 'state': 'Reserved'})

    def deliver(self, key='incoming/test', subject='Private subject', virus='PASS'):
        msg = EmailMessage(); msg['From'] = 'fan@example.com'; msg['To'] = 'one@extraliferecords.com'
        msg['Subject'] = subject; msg.set_content('Body with needle')
        msg.add_attachment(b'attachment bytes', maintype='application', subtype='octet-stream', filename='a.txt')
        self.s3.put_object(Bucket='mail-testing', Key=key, Body=msg.as_bytes())
        notification = {'mail': {'timestamp': '2026-10-07T00:00:00Z'}, 'receipt': {'action': {'bucketName': 'mail-testing', 'objectKey': key}, 'recipients': ['one@inbox.playloudr.com'], 'spamVerdict': {'status': 'PASS'}, 'virusVerdict': {'status': virus}}}
        service.receive({'Records': [{'Sns': {'Message': json.dumps(notification)}}]}, None)

    def call(self, path='/reader/mailboxes/one/messages', method='GET', query=None):
        return reader.dispatch(self.table, self.s3, method, path, query or {})

    def assign(self, name, state='Assigned'):
        self.table.put_item(Item={'pk': name, 'sk': 'META', 'generation': 'second', 'state': state, 'subject': 'employee'})

    def event(self, claims, path='/reader/mailboxes', method='GET'):
        return {'rawPath': path, 'requestContext': {'http': {'method': method}, 'authorizer': {'jwt': {'claims': claims}}}}

    def claims(self, **changes):
        return {'sub': CLIENT, 'client_id': CLIENT, 'token_use': 'access', 'scope': 'mail-reader/read',
                'exp': int(time.time()) + 3600, **changes}

    def test_sees_unassigned_and_named_inboxes_only(self):
        self.assign('two'); self.assign('three'); self.assign('four', 'Disabled')
        names = [row['name'] for row in self.call('/reader/mailboxes')['mailboxes']]
        self.assertEqual(names, ['one', 'five'])
        with patch.dict(os.environ, READER_MAILBOXES='three, four,unknown'):
            names = [row['name'] for row in self.call('/reader/mailboxes')['mailboxes']]
            self.assertEqual(names, ['one', 'three', 'five'])  # Disabled stays hidden even when named
            with self.assertRaises(service.Denied): self.call('/reader/mailboxes/two/messages')
            with self.assertRaises(service.Denied): self.call('/reader/mailboxes/four/messages')

    def test_assignment_removes_access_immediately(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        self.assign('one')
        for path in ('/reader/mailboxes/one/messages', f'/reader/mailboxes/one/messages/{mid}',
                     f'/reader/mailboxes/one/messages/{mid}/attachments/2'):
            with self.assertRaises(service.Denied): self.call(path)

    def test_lists_reads_searches_and_downloads(self):
        self.deliver(); rows = self.call()['messages']
        self.assertEqual(len(rows), 1); mid = rows[0]['id']
        self.assertEqual(self.call(f'/reader/mailboxes/one/messages/{mid}')['subject'], 'Private subject')
        attachment = self.call(f'/reader/mailboxes/one/messages/{mid}/attachments/2')
        self.assertEqual(base64.b64decode(attachment['data']), b'attachment bytes')
        self.assertEqual(len(self.call(query={'q': 'needle'})['messages']), 1)
        self.assertEqual(self.call('/reader/mailboxes/two/messages', query={'q': 'needle'})['messages'], [])

    def test_unread_filter_and_reading_never_marks_read(self):
        self.deliver('incoming/a'); self.deliver('incoming/b', 'Second')
        rows = self.call(query={'unread': '1'})['messages']; self.assertEqual(len(rows), 2)
        self.call('/reader/mailboxes/one/messages/' + rows[0]['id'])
        self.assertEqual(len(self.call(query={'unread': '1'})['messages']), 2)
        self.table.update_item(Key={'pk': 'one#first', 'sk': 'MSG#' + rows[0]['id']}, UpdateExpression='SET seen=:s', ExpressionAttributeValues={':s': True})
        self.assertEqual([r['id'] for r in self.call(query={'unread': '1'})['messages']], [rows[1]['id']])

    def test_quarantined_mail_stays_closed(self):
        self.deliver(virus='FAIL'); rows = self.call(query={'folder': 'Junk'})['messages']
        with self.assertRaises(service.Denied): self.call('/reader/mailboxes/one/messages/' + rows[0]['id'])

    def test_every_write_is_refused(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        for method, path in (('POST', '/reader/mailboxes/one/send'), ('POST', '/reader/mailboxes/one/drafts'),
                             ('PATCH', f'/reader/mailboxes/one/messages/{mid}'),
                             ('DELETE', f'/reader/mailboxes/one/messages/{mid}'), ('PUT', '/reader/mailboxes')):
            with self.assertRaises(service.Denied): self.call(path, method)
        for path in ('/reader/mailboxes/one/send', '/reader/mailboxes/one/drafts', '/reader/mailboxes/one/sends/request-123456789',
                     '/mailboxes/one/messages', '/reader/other'):
            with self.assertRaises((ValueError, service.Denied)): self.call(path)
        row = self.table.get_item(Key={'pk': 'one#first', 'sk': 'MSG#' + mid})['Item']
        self.assertEqual((row['folder'], row['seen']), ('Inbox', False))

    def test_only_the_reader_machine_token_is_accepted(self):
        for claims in (self.claims(client_id='desktopclient'), self.claims(scope='openid email'),
                       self.claims(token_use='id'), self.claims(exp=0), {'sub': 'owner', 'token_use': 'access'}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(reader.api(self.event(claims), None)['statusCode'], 403)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(reader.api(self.event(self.claims(), method='POST'), None)['statusCode'], 403)

    def test_api_returns_mail_and_audit_log_has_no_content(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        log = io.StringIO()
        with redirect_stdout(log):
            response = reader.api(self.event(self.claims(), f'/reader/mailboxes/one/messages/{mid}'), None)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body'])['subject'], 'Private subject')
        self.assertEqual(json.loads(log.getvalue()), {'reader': 'message', 'mailbox': 'one', 'status': 200})


if __name__ == '__main__': unittest.main()
