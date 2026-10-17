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

    def call(self, path='/reader/mailboxes/one/messages', method='GET', query=None, data=None, scopes=(reader.READ,)):
        return reader.dispatch(self.table, self.s3, method, path, query or {}, data, scopes)

    def post(self, path, **data):
        return self.call(path, 'POST', data=data, scopes=(reader.READ, reader.TRIAGE))

    def row(self, mid, pk='one#first'):
        return self.table.get_item(Key={'pk': pk, 'sk': 'MSG#' + mid})['Item']

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

    def test_read_scope_alone_cannot_change_anything(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        for path in ('/reader/mailboxes/one/drafts', f'/reader/mailboxes/one/messages/{mid}/move',
                     f'/reader/mailboxes/one/messages/{mid}/triage'):
            with self.assertRaises(service.Denied): self.call(path, 'POST', data={'folder': 'Junk'})
        row = self.row(mid); self.assertEqual((row['folder'], 'triage' in row), ('Inbox', False))

    def test_triage_scope_still_cannot_send_delete_or_mark_read(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        both = (reader.READ, reader.TRIAGE)
        for method, path in (('PATCH', f'/reader/mailboxes/one/messages/{mid}'),
                             ('DELETE', f'/reader/mailboxes/one/messages/{mid}'), ('PUT', '/reader/mailboxes')):
            with self.assertRaises(service.Denied): self.call(path, method, data={'folder': 'Trash', 'seen': True}, scopes=both)
        for path in ('/reader/mailboxes/one/send', f'/reader/mailboxes/one/messages/{mid}', '/reader/mailboxes',
                     f'/reader/mailboxes/one/messages/{mid}/seen'):
            with self.assertRaises((ValueError, service.Denied)):
                self.call(path, 'POST', data={'to': 'a@example.com', 'body': 'x', 'seen': True}, scopes=both)
        with self.assertRaises(ValueError): self.post(f'/reader/mailboxes/one/messages/{mid}/move', folder='Trash', reason='tidy')
        row = self.row(mid); self.assertEqual((row['folder'], row['seen']), ('Inbox', False))
        for path in ('/reader/mailboxes/one/send', '/reader/mailboxes/one/drafts', '/reader/mailboxes/one/sends/request-123456789',
                     '/mailboxes/one/messages', '/reader/other'):
            with self.assertRaises((ValueError, service.Denied)): self.call(path)
        row = self.table.get_item(Key={'pk': 'one#first', 'sk': 'MSG#' + mid})['Item']
        self.assertEqual((row['folder'], row['seen']), ('Inbox', False))

    def test_scam_moves_to_junk_and_back_with_reason(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        with self.assertRaises(ValueError): self.post(f'/reader/mailboxes/one/messages/{mid}/move', folder='Junk')
        self.post(f'/reader/mailboxes/one/messages/{mid}/move', folder='Junk', reason='Fake invoice')
        row = self.row(mid)
        self.assertEqual((row['folder'], row['seen'], row['moved_by']['from'], row['moved_by']['reason']),
                         ('Junk', False, 'Inbox', 'Fake invoice'))
        self.assertEqual(self.call()['messages'], [])
        self.post(f'/reader/mailboxes/one/messages/{mid}/move', folder='Inbox', reason='Not a scam')
        self.assertEqual(self.row(mid)['folder'], 'Inbox')

    def test_robot_cannot_move_trash_or_drafts(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        self.table.update_item(Key={'pk': 'one#first', 'sk': 'MSG#' + mid}, UpdateExpression='SET folder=:f',
                               ExpressionAttributeValues={':f': 'Trash'})
        with self.assertRaises(service.Denied): self.post(f'/reader/mailboxes/one/messages/{mid}/move', folder='Inbox', reason='restore')
        made = self.post('/reader/mailboxes/one/drafts', to='a@example.com', subject='Hi', body='Hello', reason='Reply',
                         request_id='request-0000000001')
        with self.assertRaises(service.Denied): self.post(f"/reader/mailboxes/one/messages/{made['id']}/move", folder='Junk', reason='x')

    def test_tag_and_untriaged_listing(self):
        self.deliver('incoming/a'); self.deliver('incoming/b', 'Second')
        rows = self.call(query={'untriaged': '1'})['messages']; self.assertEqual(len(rows), 2)
        first = f"/reader/mailboxes/one/messages/{rows[0]['id']}/triage"
        with self.assertRaises(ValueError): self.post(first, category='urgent', note='x')
        with self.assertRaises(ValueError): self.post(first, category='important', note='')
        self.post(first, category='important', note='Label wants the masters by Friday')
        self.assertEqual([r['id'] for r in self.call(query={'untriaged': '1'})['messages']], [rows[1]['id']])
        tagged = next(r for r in self.call()['messages'] if r['id'] == rows[0]['id'])
        self.assertEqual(tagged['triage']['category'], 'important')
        self.assertFalse(self.row(rows[0]['id'])['seen'])

    def test_robot_draft_is_a_review_draft_and_retry_safe(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        data = dict(to='fan@example.com', subject='Re: Private subject', body='Thanks!', reason='Simple thank-you',
                    request_id='triage-2026-10-17-one-1', reply_to_id=mid)
        first = self.post('/reader/mailboxes/one/drafts', **data)
        again = self.post('/reader/mailboxes/one/drafts', **{**data, 'body': 'Changed on retry'})
        self.assertEqual((again['id'], again.get('existing')), (first['id'], True))
        drafts = self.call(query={'folder': 'Drafts'})['messages']
        self.assertEqual(len(drafts), 1)
        self.assertEqual((drafts[0]['author'], drafts[0]['reply_to_id'], drafts[0]['reason']), ('robot', mid, 'Simple thank-you'))
        self.assertEqual(self.call(f"/reader/mailboxes/one/messages/{first['id']}")['draft']['body'], 'Thanks!')
        for bad in ({'bcc': 'x@example.com'}, {'attachments': [{'name': 'a', 'data': ''}]}, {'reason': ''},
                    {'request_id': 'short'}, {'to': ''}, {'to': 'not an address'}, {'reply_to_id': 'missing'}):
            with self.assertRaises((ValueError, service.Denied)):
                self.post('/reader/mailboxes/one/drafts', **{**data, 'request_id': 'triage-2026-10-17-one-2', **bad})
        self.assertEqual(len(self.call(query={'folder': 'Drafts'})['messages']), 1)
        self.assertEqual(self.s3.list_objects_v2(Bucket='mail-testing', Prefix='sent/').get('KeyCount', 0), 0)

    def test_only_the_reader_machine_token_is_accepted(self):
        for claims in (self.claims(client_id='desktopclient'), self.claims(scope='openid email'),
                       self.claims(token_use='id'), self.claims(exp=0), {'sub': 'owner', 'token_use': 'access'}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(reader.api(self.event(claims), None)['statusCode'], 403)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(reader.api(self.event(self.claims(), '/reader/mailboxes/one/drafts', 'POST'), None)['statusCode'], 403)
            self.assertEqual(reader.api(self.event(self.claims(scope='mail-reader/triage'), '/reader/mailboxes'), None)['statusCode'], 403)

    def test_api_tags_with_triage_scope_and_logs_no_content(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        event = self.event(self.claims(scope='mail-reader/read mail-reader/triage'),
                           f'/reader/mailboxes/one/messages/{mid}/triage', 'POST')
        event['body'] = json.dumps({'category': 'scam', 'note': 'Private subject looks fake'})
        log = io.StringIO()
        with redirect_stdout(log):
            response = reader.api(event, None)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(log.getvalue()), {'reader': 'triage', 'mailbox': 'one', 'status': 200})

    def test_api_returns_mail_and_audit_log_has_no_content(self):
        self.deliver(); mid = self.call()['messages'][0]['id']
        log = io.StringIO()
        with redirect_stdout(log):
            response = reader.api(self.event(self.claims(), f'/reader/mailboxes/one/messages/{mid}'), None)
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body'])['subject'], 'Private subject')
        self.assertEqual(json.loads(log.getvalue()), {'reader': 'message', 'mailbox': 'one', 'status': 200})


if __name__ == '__main__': unittest.main()
