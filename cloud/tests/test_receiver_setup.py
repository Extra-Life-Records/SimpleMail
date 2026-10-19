import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import service


class SetupNotificationTests(unittest.TestCase):
    def setUp(self):
        self.topic = 'arn:aws:sns:eu-west-1:123456789012:receiving'
        self.notification = {'notificationType': 'Received',
            'mail': {'messageId': 'AMAZON_SES_SETUP_NOTIFICATION', 'destination': ['recipient@example.com']},
            'receipt': {'recipients': ['recipient@example.com'], 'action': {'type': 'S3',
                'bucketName': 'receiving-fixture', 'objectKeyPrefix': 'incoming/',
                'objectKey': 'incoming/AMAZON_SES_SETUP_NOTIFICATION', 'topicArn': self.topic}}}
        self.record = {'EventSource': 'aws:sns', 'Sns': {'Type': 'Notification', 'TopicArn': self.topic}}
        self.table, self.s3, self.ses = Mock(), Mock(), Mock()
        self.s3.get_object.side_effect = RuntimeError('ordinary receive path')
        env = patch.dict(os.environ, BUCKET='receiving-fixture')
        env.start(); self.addCleanup(env.stop)
        clients = patch.object(service, 'clients', return_value=(self.table, self.s3, self.ses))
        clients.start(); self.addCleanup(clients.stop)

    def run_event(self, notification=None, record=None):
        record = copy.deepcopy(record or self.record)
        record['Sns']['Message'] = json.dumps(notification or self.notification)
        return service.receive({'Records': [record]}, None)

    def test_exact_provider_setup_is_acknowledged_without_storage_or_sending(self):
        self.assertEqual(self.run_event(), {'ok': True})
        self.assertEqual(self.table.mock_calls, [])
        self.assertEqual(self.s3.mock_calls, [])
        self.assertEqual(self.ses.mock_calls, [])

    def test_marker_lookalikes_remain_on_ordinary_failure_path(self):
        changes = [(('notificationType',), 'Delivery'), (('mail', 'messageId'), 'real-message'),
            (('mail', 'destination'), ['one@example.com']), (('receipt', 'recipients'), ['one@example.com']),
            (('receipt', 'action', 'objectKey'), 'incoming/real-message'),
            (('receipt', 'action', 'type'), 'Lambda'), (('receipt', 'action', 'objectKeyPrefix'), 'other/'),
            (('receipt', 'action', 'topicArn'), self.topic + '-other')]
        for path, value in changes:
            with self.subTest(path=path):
                item = copy.deepcopy(self.notification); target = item
                for part in path[:-1]: target = target[part]
                target[path[-1]] = value
                with self.assertRaisesRegex(RuntimeError, 'ordinary receive path'):
                    self.run_event(item)
        for key, value in [('EventSource', 'aws:s3'), ('Type', 'SubscriptionConfirmation'), ('TopicArn', '')]:
            with self.subTest(transport=key):
                record = copy.deepcopy(self.record)
                (record if key == 'EventSource' else record['Sns'])[key] = value
                with self.assertRaisesRegex(RuntimeError, 'ordinary receive path'):
                    self.run_event(record=record)
        self.assertEqual(self.table.mock_calls, [])
        self.assertEqual(self.ses.mock_calls, [])

    def test_setup_record_does_not_skip_following_real_mail(self):
        setup = copy.deepcopy(self.record)
        setup['Sns']['Message'] = json.dumps(self.notification)
        ordinary = copy.deepcopy(self.notification)
        ordinary['mail']['messageId'] = 'real-message'
        ordinary['receipt']['action']['objectKey'] = 'incoming/real-message'
        second = copy.deepcopy(self.record)
        second['Sns']['Message'] = json.dumps(ordinary)
        with self.assertRaisesRegex(RuntimeError, 'ordinary receive path'):
            service.receive({'Records': [setup, second]}, None)
        self.s3.get_object.assert_called_once_with(Bucket='receiving-fixture', Key='incoming/real-message')

    def test_wrong_bucket_or_prefix_is_never_acknowledged(self):
        for key, value in [('bucketName', 'another-bucket'), ('objectKey', 'other/AMAZON_SES_SETUP_NOTIFICATION')]:
            item = copy.deepcopy(self.notification); item['receipt']['action'][key] = value
            with self.assertRaises(ValueError): self.run_event(item)
        self.assertEqual(self.s3.mock_calls, [])

    def test_real_unknown_recipient_still_fails_without_mailbox_writes(self):
        item = copy.deepcopy(self.notification)
        item['mail']['messageId'] = 'real-message'
        item['receipt']['action']['objectKey'] = 'incoming/real-message'
        raw = b'From: sender@example.com\r\nTo: recipient@example.com\r\n\r\nActual message'
        self.s3.get_object.side_effect = None
        self.s3.get_object.return_value = {'ContentLength': len(raw), 'Body': io.BytesIO(raw)}
        with patch.object(service.mailboxes, 'routes', return_value={}):
            with self.assertRaisesRegex(ValueError, 'unconfigured recipient'): self.run_event(item)
        self.assertEqual(self.table.mock_calls, [])
        self.assertEqual(self.ses.mock_calls, [])


if __name__ == '__main__': unittest.main()
