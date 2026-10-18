"""Fasthosts mailboxes through the robot reader. The IMAP server is fake; no network."""
import base64
import imaplib
import io
import json
import os
import re
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
import external
import reader
import service

LOGIN = {'name': 'elr', 'address': 'hello@extraliferecords.com', 'username': 'hello@extraliferecords.com',
         'password': 'IMAP-SECRET-1', 'host': 'mail.livemail.co.uk', 'port': 993}


def mail(n, subject=None, attachment=False):
    msg = EmailMessage(); msg['From'] = f'person{n}@example.com'; msg['To'] = 'hello@extraliferecords.com'
    msg['Subject'] = subject or f'Message {n}'; msg['Message-ID'] = f'<m{n}@example.com>'; msg['Date'] = 'Fri, 17 Oct 2026 09:00:00 +0000'
    msg.set_content(f'Body {n} with needle' if n % 2 else f'Body {n}')
    if attachment: msg.add_attachment(b'file bytes', maintype='application', subtype='octet-stream', filename='f.txt')
    return msg.as_bytes()


class FakeIMAP:
    """Just enough IMAP4 for external.py, recording every command."""
    def __init__(self, move=True):
        self.capabilities = ('IMAP4REV1', 'UIDPLUS') + (('MOVE',) if move else ())
        self.boxes = {'INBOX': {'validity': 7, 'next': 1, 'mail': {}}, 'Spam': {'validity': 8, 'next': 1, 'mail': {}},
                      'Drafts': {'validity': 9, 'next': 1, 'mail': {}}, 'Sent Items': {'validity': 10, 'next': 1, 'mail': {}}}
        self.current, self.readonly, self.literal, self.commands = None, True, None, []

    def add(self, box, raw, seen=False):
        state = self.boxes[box]; uid = state['next']; state['next'] += 1
        state['mail'][uid] = {'raw': raw, 'flags': {'\\Seen'} if seen else set()}
        return uid

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "Spam"',
                      b'(\\HasNoChildren \\Drafts) "/" "Drafts"', b'(\\HasNoChildren \\Sent) "/" "Sent Items"']

    def select(self, name, readonly=False):
        self.current, self.readonly = name.strip('"'), readonly
        self.commands.append(('SELECT', self.current, readonly))
        return ('OK', [str(len(self.boxes[self.current]['mail'])).encode()]) if self.current in self.boxes else ('NO', [b''])

    def response(self, code):
        return code, [str(self.boxes[self.current]['validity']).encode()]

    def uid(self, command, *args):
        self.commands.append((command,) + args)
        box = self.boxes[self.current]
        if command == 'SEARCH':
            words = [a for a in args if a not in ('CHARSET', 'UTF-8')]
            hits = []
            for uid, item in box['mail'].items():
                if 'UNSEEN' in words and '\\Seen' in item['flags']: continue
                if 'TEXT' in words and self.literal.decode().lower() not in item['raw'].decode().lower(): continue
                hits.append(uid)
            self.literal = None
            return 'OK', [' '.join(map(str, hits)).encode()]
        if command == 'FETCH':
            uids, items = [int(u) for u in args[0].split(',')], args[1]
            data = []
            for uid in uids:
                item = box['mail'].get(uid)
                if not item: continue
                flags = ' '.join(sorted(item['flags']))
                if 'RFC822.SIZE)' in items and 'BODY' not in items:
                    data.append(f'1 (UID {uid} RFC822.SIZE {len(item["raw"])})'.encode()); continue
                body = item['raw'] if 'BODY.PEEK[]' in items else item['raw'].split(b'\n\n')[0]
                data.append((f'1 (UID {uid} FLAGS ({flags}) BODY[] {{{len(body)}}}'.encode(), body)); data.append(b')')
            return 'OK', data
        if command == 'MOVE':
            assert not self.readonly
            item = box['mail'].pop(int(args[0]))
            self.add(args[1].strip('"'), item['raw'], '\\Seen' in item['flags'])
            return 'OK', [b'Done']
        if command in ('STORE', 'EXPUNGE', 'COPY'):
            raise AssertionError('robot must not use ' + command)
        raise AssertionError(command)

    def append(self, mailbox, flags, date, message):
        self.commands.append(('APPEND', mailbox, flags))
        uid = self.add(mailbox.strip('"'), message, True)
        return 'OK', [f'[APPENDUID 9 {uid}] Append completed'.encode()]

    def logout(self):
        self.commands.append(('LOGOUT',))


@mock_aws
class ExternalTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', TABLE='mail', BUCKET='mail-testing',
                         DOMAIN='extraliferecords.com', RECEIVE_DOMAIN='inbox.playloudr.com', READER_CLIENT_ID='readerclient123', READER_MAILBOXES='')
        env.start(); self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='mail', BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'} for k in ('pk', 'sk', 'received')], KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}], GlobalSecondaryIndexes=[{'IndexName': 'Chronological', 'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'received', 'KeyType': 'RANGE'}], 'Projection': {'ProjectionType': 'ALL'}}])
        boto3.client('s3').create_bucket(Bucket='mail-testing', CreateBucketConfiguration={'LocationConstraint': 'eu-west-1'})
        arn = boto3.client('secretsmanager').create_secret(Name='simplemail/fasthosts', SecretString=json.dumps({'mailboxes': [LOGIN]}))['ARN']
        os.environ['EXTERNAL_SECRET'] = arn
        external._cache.update(at=0, value={})
        self.table, self.s3 = reader.clients()
        for name in service.NAMES:
            self.table.put_item(Item={'pk': name, 'sk': 'META', 'generation': 'first', 'state': 'Reserved'})
        self.imap = FakeIMAP()

    def call(self, path='/reader/mailboxes/elr/messages', method='GET', query=None, data=None, triage=False):
        scopes = (reader.READ, reader.TRIAGE) if triage else (reader.READ,)
        return reader.dispatch(self.table, self.s3, method, path, query or {}, data, scopes, lambda account: self.imap)

    def post(self, path, **data):
        return self.call(path, 'POST', data=data, triage=True)

    def test_secret_is_listed_without_password(self):
        boxes = self.call('/reader/mailboxes')['mailboxes']
        self.assertEqual([b['name'] for b in boxes], ['one', 'two', 'three', 'four', 'five', 'elr'])
        self.assertEqual(boxes[-1], {'name': 'elr', 'address': 'hello@extraliferecords.com', 'state': 'External', 'provider': 'imap'})
        self.assertNotIn('IMAP-SECRET-1', json.dumps(boxes))

    def test_bad_secret_entries_are_refused(self):
        for bad in ({**LOGIN, 'name': 'one'}, {**LOGIN, 'name': 'Bad Name'}, {**LOGIN, 'password': ''}):
            boto3.client('secretsmanager').put_secret_value(SecretId=os.environ['EXTERNAL_SECRET'], SecretString=json.dumps({'mailboxes': [bad]}))
            external._cache.update(at=0, value={})
            with self.assertRaises(ValueError): external.accounts()

    def test_unknown_external_name_is_refused(self):
        with self.assertRaises(service.Denied): self.call('/reader/mailboxes/other/messages')

    def test_lists_newest_first_with_paging_and_never_marks_read(self):
        for n in range(1, 13): self.imap.add('INBOX', mail(n), seen=n == 12)
        first = self.call()
        self.assertEqual([r['subject'] for r in first['messages']][:2], ['Message 12', 'Message 11'])
        self.assertEqual(len(first['messages']), 10)
        second = self.call(query={'cursor': first['cursor']})
        self.assertEqual([r['subject'] for r in second['messages']], ['Message 2', 'Message 1'])
        self.assertIsNone(second['cursor'])
        unread = self.call(query={'unread': '1'})
        self.assertNotIn('Message 12', [r['subject'] for r in unread['messages']])
        self.assertTrue(all(c[2] for c in self.imap.commands if c[0] == 'SELECT'))  # read-only selects
        self.assertTrue(all('PEEK' in str(c) for c in self.imap.commands if c[0] == 'FETCH' and 'BODY' in str(c)))
        self.assertEqual({n for n, i in self.imap.boxes['INBOX']['mail'].items() if '\\Seen' in i['flags']}, {12})

    def test_search_and_all_folders(self):
        self.imap.add('INBOX', mail(1)); self.imap.add('INBOX', mail(2)); self.imap.add('Sent Items', mail(3))
        hits = self.call(query={'q': 'needle', 'folder': 'All'})
        subjects, cursor = [r['subject'] for r in hits['messages']], hits['cursor']
        while cursor:
            page = self.call(query={'q': 'needle', 'folder': 'All', 'cursor': cursor})
            subjects += [r['subject'] for r in page['messages']]; cursor = page['cursor']
        self.assertEqual(subjects, ['Message 1', 'Message 3'])
        with self.assertRaises(ValueError): self.call(query={'cursor': 'not-a-cursor'})

    def test_read_and_attachment(self):
        uid = self.imap.add('INBOX', mail(1, attachment=True))
        message = self.call(f'/reader/mailboxes/elr/messages/inbox-7-{uid}')
        self.assertEqual((message['subject'], message['provider']), ('Message 1', 'imap'))
        self.assertIn('needle', message['body'])
        part = message['attachments'][0]['part']
        data = self.call(f'/reader/mailboxes/elr/messages/inbox-7-{uid}/attachments/{part}')
        self.assertEqual(base64.b64decode(data['data']), b'file bytes')
        with self.assertRaises(service.Denied): self.call(f'/reader/mailboxes/elr/messages/inbox-99-{uid}')  # stale UIDVALIDITY
        with self.assertRaises(ValueError): self.call('/reader/mailboxes/elr/messages/inbox-7-1;DELETE')

    def test_tag_and_untriaged(self):
        a = self.imap.add('INBOX', mail(1)); self.imap.add('INBOX', mail(2))
        self.assertEqual(len(self.call(query={'untriaged': '1'})['messages']), 2)
        self.post(f'/reader/mailboxes/elr/messages/inbox-7-{a}/triage', category='needs_fix', note='Website form is broken')
        left = self.call(query={'untriaged': '1'})['messages']
        self.assertEqual([r['subject'] for r in left], ['Message 2'])
        tagged = self.call(f'/reader/mailboxes/elr/messages/inbox-7-{a}')
        self.assertEqual(tagged['triage']['category'], 'needs_fix')

    def test_scam_moves_to_junk_by_uid_move_and_tag_follows(self):
        a = self.imap.add('INBOX', mail(1, 'Your account is locked'))
        self.post(f'/reader/mailboxes/elr/messages/inbox-7-{a}/triage', category='scam', note='Phishing')
        with self.assertRaises(ValueError): self.post(f'/reader/mailboxes/elr/messages/inbox-7-{a}/move', folder='Trash', reason='x')
        self.post(f'/reader/mailboxes/elr/messages/inbox-7-{a}/move', folder='Junk', reason='Phishing')
        self.assertEqual(self.imap.boxes['INBOX']['mail'], {})
        junk = self.call(query={'folder': 'Junk'})['messages']
        self.assertEqual((junk[0]['subject'], junk[0]['triage']['category'], junk[0]['seen']), ('Your account is locked', 'scam', False))
        self.post(f"/reader/mailboxes/elr/messages/{junk[0]['id']}/move", folder='Inbox', reason='Actually fine')
        self.assertEqual(len(self.imap.boxes['INBOX']['mail']), 1)

    def test_no_move_without_server_move_and_no_moving_sent(self):
        sent = self.imap.add('Sent Items', mail(1))
        with self.assertRaises(service.Denied): self.post(f'/reader/mailboxes/elr/messages/sent-10-{sent}/move', folder='Junk', reason='x')
        self.imap = FakeIMAP(move=False); a = self.imap.add('INBOX', mail(2))
        with self.assertRaises(service.Denied): self.post(f'/reader/mailboxes/elr/messages/inbox-7-{a}/move', folder='Junk', reason='x')
        self.assertEqual(len(self.imap.boxes['INBOX']['mail']), 1)

    def test_draft_is_appended_once_as_a_threaded_review_draft(self):
        a = self.imap.add('INBOX', mail(1))
        data = dict(to='person1@example.com', subject='Re: Message 1', body='Thanks, sorting it now.', reason='Simple reply',
                    request_id='triage-2026-10-17-elr-1', reply_to_id=f'inbox-7-{a}')
        first = self.post('/reader/mailboxes/elr/drafts', **data)
        again = self.post('/reader/mailboxes/elr/drafts', **data)
        self.assertEqual(first['id'], 'drafts-9-1'); self.assertTrue(again['existing'])
        drafts = self.imap.boxes['Drafts']['mail']
        self.assertEqual(len(drafts), 1)
        self.assertIn(('APPEND', '"Drafts"', '(\\Draft \\Seen)'), self.imap.commands)
        text = drafts[1]['raw'].decode()
        self.assertIn('In-Reply-To: <m1@example.com>', text); self.assertIn('From: hello@extraliferecords.com', text)
        for bad in ({'bcc': 'x@example.com'}, {'reason': ''}, {'to': ''}, {'reply_to_id': 'drafts-9-1'}):
            with self.assertRaises((ValueError, service.Denied)):
                self.post('/reader/mailboxes/elr/drafts', **{**data, 'request_id': 'triage-2026-10-17-elr-2', **bad})
        self.assertEqual(len(drafts), 1)
        self.assertFalse(any(c[0] in ('STORE', 'EXPUNGE') for c in self.imap.commands))

    def test_failed_append_can_be_retried(self):
        def broken(*args): raise imaplib.IMAP4.error('NO [OVERQUOTA] hello@extraliferecords.com IMAP-SECRET-1')
        self.imap.append = broken
        data = dict(to='a@example.com', subject='s', body='b', reason='r', request_id='triage-2026-10-17-elr-3')
        with self.assertRaises(external.Unavailable): self.post('/reader/mailboxes/elr/drafts', **data)
        self.imap = FakeIMAP()
        self.assertEqual(self.post('/reader/mailboxes/elr/drafts', **data)['id'], 'drafts-9-1')

    def test_stuck_draft_claim_is_retaken_only_when_stale(self):
        data = dict(to='a@example.com', subject='s', body='b', reason='r', request_id='triage-2026-10-17-elr-4')
        key = {'pk': 'external#elr', 'sk': 'DRAFT#' + external.hashlib.sha256(data['request_id'].encode()).hexdigest()[:32]}
        self.table.put_item(Item={**key, 'status': 'saving', 'created': service.timestamp()})
        self.assertTrue(self.post('/reader/mailboxes/elr/drafts', **data)['existing'])
        self.table.put_item(Item={**key, 'status': 'saving', 'created': '2026-10-01T00:00:00.000000Z'})
        self.assertEqual(self.post('/reader/mailboxes/elr/drafts', **data)['id'], 'drafts-9-1')

    def test_folder_names_sent_as_literals(self):
        self.imap.list = lambda: ('OK', [b'(\\HasNoChildren) "/" "INBOX"', (b'(\\HasNoChildren \\Junk) "/" {9}', b'Junk Mail')])
        self.assertEqual(external.folders(self.imap)['Junk'], 'Junk Mail')

    def test_read_scope_cannot_change_fasthosts_mail(self):
        a = self.imap.add('INBOX', mail(1))
        for path in ('/reader/mailboxes/elr/drafts', f'/reader/mailboxes/elr/messages/inbox-7-{a}/move'):
            with self.assertRaises(service.Denied): self.call(path, 'POST', data={'folder': 'Junk'})
        self.assertEqual(len(self.imap.boxes['INBOX']['mail']), 1)

    def test_login_failure_hides_server_text(self):
        def refuse(account): raise external.Unavailable()
        event = {'rawPath': '/reader/mailboxes/elr/messages', 'requestContext': {'http': {'method': 'GET'}, 'authorizer': {'jwt': {'claims': {
            'client_id': 'readerclient123', 'token_use': 'access', 'scope': 'mail-reader/read', 'exp': int(time.time()) + 3600}}}}}
        with patch.object(external, 'connect', refuse), redirect_stdout(io.StringIO()):
            response = reader.api(event, None)
        self.assertEqual(response['statusCode'], 503)
        self.assertNotIn('SECRET', response['body'])
        with patch.object(imaplib, 'IMAP4_SSL', side_effect=OSError('refused hello@extraliferecords.com IMAP-SECRET-1')):
            with self.assertRaises(external.Unavailable) as caught: external.connect(LOGIN)
        self.assertNotIn('SECRET', str(caught.exception))


if __name__ == '__main__': unittest.main()
