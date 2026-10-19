import copy
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import operational_health as health
import service


CONFIG = {'queues': [{'label': 'Receiver failures', 'url': 'queue-1'}, {'label': 'Notification failures', 'url': 'queue-2'}],
          'alarms': [{'label': 'Receiver alarm', 'name': 'alarm-1'}, {'label': 'Notification alarm', 'name': 'alarm-2'}],
          'domains': ['example.com'], 'rule_set': 'rules', 'topic_arn': 'topic', 'mcp_function': 'mcp', 'delivery_function': 'delivery',
          'rules': [{'name': 'original', 'recipients': ['one@example.com']}]}


class OwnerIsolation(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, OWNER_CLIENT_ID='owner-client', TABLE='mail')
        env.start(); self.addCleanup(env.stop)
        self.resource = Mock(); self.table = self.resource.Table.return_value
        self.table.get_item.return_value = {'Item': {'enabled': True}}

    def event(self, client='owner-client', subject='owner', method='GET', expired=False):
        return {'rawPath': health.PATH, 'requestContext': {'http': {'method': method}, 'authorizer': {'jwt': {'claims': {
            'client_id': client, 'sub': subject, 'token_use': 'access', 'exp': 0 if expired else int(time.time()) + 900}}}}}

    def test_only_owner_client_and_current_enabled_admin_can_read(self):
        with patch.object(service.boto3, 'resource', return_value=self.resource), patch.object(health, 'snapshot', return_value={'status': 'clear'}) as snapshot:
            self.assertEqual(service.api(self.event(), None)['statusCode'], 200)
            self.table.get_item.assert_called_with(Key={'pk': 'ADMIN', 'sk': 'owner'}, ConsistentRead=True)
            self.table.get_item.return_value = {'Item': {'enabled': False}}
            self.assertEqual(service.api(self.event(), None)['statusCode'], 403)
            self.table.get_item.return_value = {}
            self.assertEqual(service.api(self.event(subject='assigned-employee'), None)['statusCode'], 403)
            self.assertEqual(snapshot.call_count, 1)

    def test_ai_clients_mutations_and_expired_tokens_cannot_read(self):
        with patch.object(service.boto3, 'resource', return_value=self.resource) as resource, patch.object(health, 'snapshot') as snapshot:
            for event in (self.event(client='codex-client'), self.event(client='claude-client'),
                          self.event(method='POST'), self.event(expired=True)):
                self.assertEqual(service.api(event, None)['statusCode'], 403)
            resource.assert_not_called(); snapshot.assert_not_called()

    def test_missing_owner_client_fails_closed(self):
        with patch.dict(os.environ, OWNER_CLIENT_ID=''), patch.object(health, 'snapshot') as snapshot:
            self.assertEqual(service.api(self.event(client=''), None)['statusCode'], 403)
            snapshot.assert_not_called()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, OPERATIONAL_HEALTH_CONFIG=json.dumps(CONFIG), BUCKET='mail-bucket',
                         SENDING_ENABLED='false', HUMAN_SENDING_ENABLED='false')
        env.start(); self.addCleanup(env.stop)
        self.clients = {name: Mock() for name in ('sqs', 'cloudwatch', 'ses', 'sesv2', 'lambda')}
        self.clients['sqs'].get_queue_attributes.return_value = {'Attributes': {
            'ApproximateNumberOfMessages': '0', 'ApproximateNumberOfMessagesNotVisible': '0', 'ApproximateNumberOfMessagesDelayed': '0'}}
        self.clients['cloudwatch'].describe_alarms.side_effect = lambda AlarmNames: {'MetricAlarms': [
            {'AlarmName': AlarmNames[0], 'StateValue': 'OK', 'AlarmActions': ['private-subscriber']} ]}
        self.clients['sesv2'].get_email_identity.return_value = {'VerificationStatus': 'SUCCESS', 'DkimAttributes': {'Status': 'SUCCESS'}}
        self.rules = {'Metadata': {'Name': 'rules'}, 'Rules': [{'Name': 'original', 'Enabled': True, 'TlsPolicy': 'Require',
            'ScanEnabled': True, 'Recipients': ['one@example.com'], 'Actions': [{'S3Action': {
            'BucketName': 'mail-bucket', 'ObjectKeyPrefix': 'incoming/', 'TopicArn': 'topic'}}]}]}
        self.clients['ses'].describe_active_receipt_rule_set.return_value = self.rules
        self.clients['lambda'].get_function_configuration.return_value = {'Environment': {'Variables': {
            'AI_SENDING_ENABLED': 'false', 'SENDING_ENABLED': 'false', 'HUMAN_SENDING_ENABLED': 'false', 'SECRET': 'credential-must-not-leak'}}}

    def snapshot(self):
        return health.snapshot(self.clients.__getitem__)

    def test_clear_reads_are_bounded_sanitized_and_never_mutate(self):
        result = self.snapshot()
        self.assertEqual(result['status'], 'clear'); self.assertFalse(result['delivery_verified'])
        self.assertEqual(len(result['checks']), 7)
        encoded = json.dumps(result)
        for secret in ('private-subscriber', 'credential-must-not-leak', 'queue-1', 'topic'):
            self.assertNotIn(secret, encoded)
        allowed = {'sqs': {'get_queue_attributes'}, 'cloudwatch': {'describe_alarms'}, 'ses': {'describe_active_receipt_rule_set'},
                   'sesv2': {'get_email_identity'}, 'lambda': {'get_function_configuration'}}
        for name, client in self.clients.items():
            self.assertTrue(all(call[0] in allowed[name] for call in client.method_calls))
        for call in self.clients['sqs'].get_queue_attributes.call_args_list:
            self.assertNotIn('All', call.kwargs['AttributeNames'])

    def test_queue_and_alarm_failures_are_attention(self):
        self.clients['sqs'].get_queue_attributes.return_value['Attributes']['ApproximateNumberOfMessagesDelayed'] = '1'
        self.assertEqual(self.snapshot()['status'], 'attention')
        self.clients['sqs'].get_queue_attributes.return_value['Attributes']['ApproximateNumberOfMessagesDelayed'] = '0'
        self.clients['cloudwatch'].describe_alarms.side_effect = lambda AlarmNames: {'MetricAlarms': [{'AlarmName': AlarmNames[0], 'StateValue': 'ALARM'}]}
        self.assertEqual(self.snapshot()['status'], 'attention')

    def test_failed_read_and_missing_alarm_are_unknown_without_raw_exception(self):
        self.clients['sqs'].get_queue_attributes.side_effect = RuntimeError('secret raw provider details')
        self.clients['cloudwatch'].describe_alarms.side_effect = lambda AlarmNames: {'MetricAlarms': []}
        result = self.snapshot()
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))
        self.assertTrue(all(r['status'] == 'unknown' for r in result['checks'] if r['kind'] in ('queue', 'alarm')))

    def test_pending_domain_and_disabled_rule_are_not_delivery_success(self):
        self.rules['Rules'][0]['Enabled'] = False
        self.clients['sesv2'].get_email_identity.return_value['VerificationStatus'] = 'PENDING'
        config = copy.deepcopy(CONFIG); config['rules'][0]['migration'] = True
        with patch.dict(os.environ, OPERATIONAL_HEALTH_CONFIG=json.dumps(config)):
            result = self.snapshot()
        self.assertEqual(result['status'], 'pending'); self.assertFalse(result['delivery_verified'])
        self.assertIn('Migration receiving is not activated', next(r for r in result['checks'] if r['kind'] == 'rules')['migration_notice'])

    def test_rule_drift_is_attention(self):
        for mutate in (lambda: self.rules['Rules'][0].update(Recipients=['other@example.com']),
                       lambda: self.rules['Rules'][0].update(TlsPolicy='Optional'),
                       lambda: self.rules['Rules'].insert(0, {'Name': 'unreviewed-rule'})):
            original = copy.deepcopy(self.rules); mutate()
            self.assertEqual(self.snapshot()['status'], 'attention')
            self.rules.clear(); self.rules.update(original)

    def test_sending_enabled_or_unknown_is_explicit(self):
        variables = self.clients['lambda'].get_function_configuration.return_value['Environment']['Variables']
        variables['AI_SENDING_ENABLED'] = 'true'
        self.assertEqual(self.snapshot()['status'], 'attention')
        del variables['AI_SENDING_ENABLED']
        result = self.snapshot(); self.assertEqual(result['status'], 'unknown')
        self.assertEqual(next(r for r in result['checks'] if r['kind'] == 'sending')['flags']['ai'], 'unknown')

    def test_manual_switch_reads_delivery_runtime_not_an_api_copy(self):
        self.clients['lambda'].get_function_configuration.side_effect = lambda FunctionName: {'Environment': {'Variables': (
            {'HUMAN_SENDING_ENABLED': 'true'} if FunctionName == 'delivery' else {'AI_SENDING_ENABLED': 'false', 'SENDING_ENABLED': 'false'})}}
        result = self.snapshot()
        self.assertEqual(result['status'], 'attention')
        self.assertEqual(next(r for r in result['checks'] if r['kind'] == 'sending')['flags']['manual'], 'enabled')

    def test_unconfigured_or_invalid_config_never_calls_aws(self):
        for value in ('{}', '[]', '{broken', json.dumps({**CONFIG, 'queues': [None, None]})):
            with patch.dict(os.environ, OPERATIONAL_HEALTH_CONFIG=value):
                factory = Mock()
                self.assertEqual(health.snapshot(factory)['status'], 'unknown')
                factory.assert_not_called()


if __name__ == '__main__': unittest.main()
