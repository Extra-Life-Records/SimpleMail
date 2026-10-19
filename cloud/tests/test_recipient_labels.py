import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import service


@mock_aws
class RecipientLabelTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            'one': {'aliases': ['dizzyteapot@extraliferecords.com']},
            'mail-elr': {'address': 'hello@extraliferecords.com'},
            'mail-robots': {'address': 'hello@meandtherobots.com'},
            'mail-label': {'address': 'label-target@example.com',
                           'aliases': ['hello+vip@extraliferecords.com']},
        }
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', TABLE='labels',
                         BUCKET='label-fixture', DOMAIN='extraliferecords.com',
                         RECEIVE_DOMAIN='inbox.playloudr.com', MAILBOX_REGISTRY=json.dumps(self.config),
                         SENDING_ENABLED='false', AI_SENDING_ENABLED='false', HUMAN_SENDING_ENABLED='false')
        env.start(); self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='labels', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'} for key in ('pk', 'sk')],
            KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}])
        self.table = boto3.resource('dynamodb').Table('labels')
        self.s3 = boto3.client('s3')
        self.s3.create_bucket(Bucket='label-fixture', CreateBucketConfiguration={'LocationConstraint': 'eu-west-1'})
        self.ses = Mock()
        clients = patch.object(service, 'clients', return_value=(self.table, self.s3, self.ses))
        clients.start(); self.addCleanup(clients.stop)
        for name, spec in service.mailboxes.registry().items():
            self.table.put_item(Item={'pk': name, 'sk': 'META', 'address': spec['address'],
                'state': 'Reserved', 'generation': 'original', 'generation_started': '2026-01-01T00:00:00Z'})
        self.table.put_item(Item={'pk': 'ADMIN', 'sk': 'owner', 'enabled': True})
        self.table.put_item(Item={'pk': 'GRANT#owner', 'sk': 'existing', 'permissions': ['read', 'draft']})
        self.protected = self.protected_rows()

    def protected_rows(self):
        return sorted(json.dumps(row, sort_keys=True) for row in self.table.scan()['Items']
                      if row['pk'].startswith(('ADMIN', 'GRANT#')))

    def tearDown(self):
        self.assertEqual(self.protected_rows(), self.protected)
        self.assertEqual(self.ses.mock_calls, [])

    def event(self, recipients, key='incoming/label-test', timestamp='2026-10-19T10:00:00Z'):
        # A misleading MIME recipient must never control the destination.
        raw = b'From: sender@example.net\r\nTo: five@inbox.playloudr.com\r\nSubject: Fixture\r\n\r\nBody'
        self.s3.put_object(Bucket='label-fixture', Key=key, Body=raw)
        notice = {'mail': {'timestamp': timestamp}, 'receipt': {
            'action': {'bucketName': 'label-fixture', 'objectKey': key},
            'recipients': recipients, 'virusVerdict': {'status': 'PASS'}, 'spamVerdict': {'status': 'PASS'}}}
        return {'Records': [{'Sns': {'Message': json.dumps(notice)}}]}

    def messages(self):
        return [row for row in self.table.scan()['Items'] if row['sk'].startswith('MSG#')]

    def test_approved_bases_domains_and_aliases_preserve_full_envelope(self):
        recipients = ['HELLO+Receipt@ExtraLifeRecords.com', 'hello+robot@meandtherobots.com',
                      'one+form@inbox.playloudr.com', 'dizzyteapot+social+source@extraliferecords.com']
        self.assertEqual(service.receive(self.event(recipients), None), {'ok': True})
        by_box = {row['pk']: row for row in self.messages()}
        self.assertEqual(set(by_box), {'mail-elr#original', 'mail-robots#original', 'one#original'})
        self.assertEqual(by_box['mail-elr#original']['recipients'], ['hello+receipt@extraliferecords.com'])
        self.assertEqual(by_box['mail-robots#original']['recipients'], ['hello+robot@meandtherobots.com'])
        self.assertEqual(by_box['one#original']['recipients'],
            ['dizzyteapot+social+source@extraliferecords.com', 'one+form@inbox.playloudr.com'])

    def test_exact_label_mapping_wins_over_base_and_first_plus_is_deliberate(self):
        service.receive(self.event(['hello+vip@extraliferecords.com',
                                    'hello+vip+more@extraliferecords.com']), None)
        by_box = {row['pk']: row['recipients'] for row in self.messages()}
        self.assertEqual(by_box, {'mail-label#original': ['hello+vip@extraliferecords.com'],
                                 'mail-elr#original': ['hello+vip+more@extraliferecords.com']})

    def test_exact_disabled_label_never_falls_back_to_enabled_base(self):
        self.table.update_item(Key={'pk': 'mail-label', 'sk': 'META'}, UpdateExpression='SET #s=:s',
                               ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':s': 'Disabled'})
        service.receive(self.event(['hello+vip@extraliferecords.com']), None)
        self.assertEqual(self.messages(), [])
        self.assertFalse(any(row['pk'].startswith('RECEIPT#') for row in self.table.scan()['Items']))

    def test_disabled_base_does_not_block_an_explicit_enabled_label(self):
        self.table.update_item(Key={'pk': 'mail-elr', 'sk': 'META'}, UpdateExpression='SET #s=:s',
                               ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':s': 'Disabled'})
        service.receive(self.event(['hello+vip@extraliferecords.com', 'hello+other@extraliferecords.com']), None)
        self.assertEqual([row['pk'] for row in self.messages()], ['mail-label#original'])

    def test_unknown_base_domain_and_empty_label_fail_before_any_mailbox_write(self):
        unknowns = ['unknown+tag@extraliferecords.com', 'hello+tag@other.example',
                    'hello+tag@sub.extraliferecords.com', 'hello+@extraliferecords.com',
                    '+tag@extraliferecords.com', 'not-an-address']
        original = self.table.scan()['Items']
        for index, address in enumerate(unknowns):
            with self.subTest(address=address):
                event = self.event(['hello@extraliferecords.com', address], key=f'incoming/unknown-{index}')
                with self.assertRaisesRegex(ValueError, 'unconfigured recipient'): service.receive(event, None)
                self.assertCountEqual(self.table.scan()['Items'], original)

    def test_explicit_empty_label_is_an_exact_alias_only(self):
        configured = copy.deepcopy(self.config)
        configured['mail-label']['aliases'].append('hello+@extraliferecords.com')
        with patch.dict(os.environ, MAILBOX_REGISTRY=json.dumps(configured)):
            service.receive(self.event(['hello+@extraliferecords.com']), None)
        self.assertEqual([(row['pk'], row['recipients']) for row in self.messages()],
                         [('mail-label#original', ['hello+@extraliferecords.com'])])

    def test_base_alias_and_labels_deduplicate_and_retry_preserves_owner_state_and_generation(self):
        event = self.event(['one@inbox.playloudr.com', 'one+tag@inbox.playloudr.com',
                            'dizzyteapot+tag@extraliferecords.com', 'one+tag@inbox.playloudr.com'])
        service.receive(event, None)
        rows = self.messages(); self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['recipients']), 3)
        key = {'pk': rows[0]['pk'], 'sk': rows[0]['sk']}
        self.table.update_item(Key=key, UpdateExpression='SET seen=:s, folder=:f',
                              ExpressionAttributeValues={':s': True, ':f': 'Archive'})
        self.table.update_item(Key={'pk': 'one', 'sk': 'META'}, UpdateExpression='SET generation=:g',
                              ExpressionAttributeValues={':g': 'reassigned'})
        service.receive(event, None)
        self.assertEqual(len(self.messages()), 1)
        row = self.table.get_item(Key=key)['Item']
        self.assertTrue(row['seen']); self.assertEqual(row['folder'], 'Archive')
        claim = self.table.get_item(Key={'pk': 'RECEIPT#one',
                    'sk': hashlib.sha256(b'incoming/label-test').hexdigest()})['Item']
        self.assertEqual(claim['generation'], 'original')

    def test_delayed_label_is_kept_in_original_generation(self):
        self.table.put_item(Item={'pk': 'one', 'sk': 'GEN#2026-01-01T00:00:00.000000Z', 'generation': 'original'})
        self.table.update_item(Key={'pk': 'one', 'sk': 'META'},
            UpdateExpression='SET generation=:g, generation_started=:t',
            ExpressionAttributeValues={':g': 'reassigned', ':t': '2026-10-20T00:00:00.000000Z'})
        service.receive(self.event(['one+late@inbox.playloudr.com']), None)
        self.assertEqual([row['pk'] for row in self.messages()], ['one#original'])


if __name__ == '__main__': unittest.main()
