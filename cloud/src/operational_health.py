"""Bounded owner diagnostics. Never reads mail, queue bodies or billing data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os

import boto3
from botocore.config import Config

PATH = '/mailboxes/operational-health'
CLIENT_CONFIG = Config(connect_timeout=2, read_timeout=3, retries={'total_max_attempts': 1})


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def flag(value):
    return {'true': 'enabled', 'false': 'disabled'}.get(value, 'unknown')


def snapshot(client_factory=None):
    """No cache: each observation is fresh or explicitly unknown."""
    started = stamp()
    try:
        config = json.loads(os.environ.get('OPERATIONAL_HEALTH_CONFIG', '{}'))
        assert isinstance(config, dict)
        assert len(config.get('queues', [])) == 2 and len(config.get('alarms', [])) == 2
        assert 1 <= len(config.get('domains', [])) <= 10 and 1 <= len(config.get('rules', [])) <= 10
        assert config.get('rule_set') and config.get('mcp_function') and config.get('topic_arn')
        assert all(isinstance(row, dict) and row.get('label') and row.get('url') for row in config['queues'])
        assert all(isinstance(row, dict) and row.get('label') and row.get('name') for row in config['alarms'])
        assert all(isinstance(name, str) and name for name in config['domains'])
        assert all(isinstance(row, dict) and row.get('name') and isinstance(row.get('recipients'), list) and row['recipients'] for row in config['rules'])
    except (ValueError, TypeError, KeyError, AssertionError):
        return {'status': 'unknown', 'checked_at': started, 'expires_after_seconds': 600,
                'checks': [], 'reason': 'Operational checks are not configured.', 'delivery_verified': False}
    factory = client_factory or (lambda name: boto3.client(name, config=CLIENT_CONFIG))
    clients = {name: factory(name) for name in ('sqs', 'cloudwatch', 'ses', 'sesv2', 'lambda')}
    checks = []

    def observe(kind, name, read):
        try:
            return {'kind': kind, 'name': name, 'checked_at': stamp(), **read()}
        except Exception:
            # Provider exceptions can contain raw configuration; never return them.
            return {'kind': kind, 'name': name, 'checked_at': stamp(), 'status': 'unknown',
                    'detail': 'Check unavailable. Try refreshing.'}

    def queue(row):
        attrs = clients['sqs'].get_queue_attributes(QueueUrl=row['url'], AttributeNames=[
            'ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible',
            'ApproximateNumberOfMessagesDelayed'])['Attributes']
        counts = {name: int(attrs[key]) for name, key in (
            ('visible', 'ApproximateNumberOfMessages'), ('in_flight', 'ApproximateNumberOfMessagesNotVisible'),
            ('delayed', 'ApproximateNumberOfMessagesDelayed'))}
        return {'status': 'attention' if any(counts.values()) else 'clear', 'counts': counts}

    def alarm(row):
        items = clients['cloudwatch'].describe_alarms(AlarmNames=[row['name']])['MetricAlarms']
        item = next(item for item in items if item['AlarmName'] == row['name'])
        state = item['StateValue']
        return {'status': {'OK': 'clear', 'ALARM': 'attention'}.get(state, 'unknown'), 'state': state}

    def domain(name):
        item = clients['sesv2'].get_email_identity(EmailIdentity=name)
        verification, dkim = item.get('VerificationStatus'), item.get('DkimAttributes', {}).get('Status')
        status = 'clear' if verification == dkim == 'SUCCESS' else 'pending'
        if verification in ('FAILED', 'TEMPORARY_FAILURE') or dkim in ('FAILED', 'TEMPORARY_FAILURE'): status = 'attention'
        known = ('SUCCESS', 'PENDING', 'NOT_STARTED', 'FAILED', 'TEMPORARY_FAILURE')
        if verification not in known or dkim not in known: status = 'unknown'
        return {'status': status, 'verification': verification, 'dkim': dkim}

    def rules():
        result = clients['ses'].describe_active_receipt_rule_set()
        if result.get('Metadata', {}).get('Name') != config['rule_set']:
            return {'status': 'attention', 'detail': 'The expected receiving rule set is not active.'}
        if [row['Name'] for row in result.get('Rules', [])] != [row['name'] for row in config['rules']]:
            return {'status': 'attention', 'detail': 'Receiving rules or their order differ from the reviewed setup.'}
        actual = {row['Name']: row for row in result.get('Rules', [])}
        states = []
        for expected in config['rules']:
            rule = actual.get(expected['name'], {})
            actions = rule.get('Actions', [])
            s3 = actions[0].get('S3Action', {}) if len(actions) == 1 else {}
            matches = (set(rule.get('Recipients', [])) == set(expected['recipients']) and
                       rule.get('TlsPolicy') == 'Require' and rule.get('ScanEnabled') is True and
                       s3.get('BucketName') == os.environ.get('BUCKET') and
                       s3.get('ObjectKeyPrefix') == 'incoming/' and s3.get('TopicArn') == config['topic_arn'])
            state = 'attention' if not matches else ('clear' if rule.get('Enabled') is True else 'pending')
            states.append({'name': expected.get('label', expected['name']), 'enabled': rule.get('Enabled'), 'status': state,
                           'migration': expected.get('migration') is True,
                           'recipient_count': len(expected['recipients'])})
        status = 'attention' if any(s['status'] == 'attention' for s in states) else (
            'pending' if any(s['status'] == 'pending' for s in states) else 'clear')
        result = {'status': status, 'rules': states}
        if any(r['migration'] and r['enabled'] is not True for r in states):
            result['migration_notice'] = 'Migration receiving is not activated. Imported history does not confirm new mail delivery.'
        return result

    def sending():
        variables = clients['lambda'].get_function_configuration(FunctionName=config['mcp_function']).get('Environment', {}).get('Variables', {})
        flags = {'general': flag(os.environ.get('SENDING_ENABLED')),
                 'manual': flag(os.environ.get('HUMAN_SENDING_ENABLED')),
                 'ai': flag(variables.get('AI_SENDING_ENABLED')),
                 'ai_transport': flag(variables.get('SENDING_ENABLED'))}
        status = 'attention' if 'enabled' in flags.values() else ('unknown' if 'unknown' in flags.values() else 'clear')
        return {'status': status, 'flags': flags}

    jobs = [('queue', row['label'], lambda row=row: queue(row)) for row in config['queues']]
    jobs += [('alarm', row['label'], lambda row=row: alarm(row)) for row in config['alarms']]
    jobs += [('domain', name, lambda name=name: domain(name)) for name in config['domains']]
    jobs += [('rules', 'Receiving routes', rules), ('sending', 'Cloud sending', sending)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        checks = list(pool.map(lambda job: observe(*job), jobs))
    states = {item['status'] for item in checks}
    overall = next((state for state in ('attention', 'unknown', 'pending') if state in states), 'clear')
    return {'status': overall, 'checked_at': started, 'expires_after_seconds': 600,
            'checks': checks, 'delivery_verified': False}
