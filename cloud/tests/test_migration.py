import base64
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import ai_access
import human_send
import mailboxes
import reader
import service

spec = importlib.util.spec_from_file_location('migration', Path(__file__).parents[1] / 'mail_migration.py')
migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)

CONFIG = {'one': {'aliases': ['dizzyteapot@extraliferecords.com']},
          'mail-elr': {'address': 'hello@extraliferecords.com', 'aliases': ['contact@extraliferecords.com']},
          'mail-robots': {'address': 'hello@meandtherobots.com'}}


def fixture(root, folder='INBOX', flags=None, attachment=b'original attachment'):
    host, username = 'mail.livemail.co.uk', 'hello@extraliferecords.com'
    source_id = hashlib.sha256((host + '\n' + username).encode()).hexdigest()
    path = 'folders/fixture/42'
    (root / path / 'messages').mkdir(parents=True)
    rows = []
    for uid in (1, 2):
        message = EmailMessage(); message['Subject'] = 'Unicode café'; message['Message-ID'] = '<same@example.com>'
        message['From'] = 'writer@example.com'; message['To'] = username
        message['Date'] = 'Mon, 01 Jan 2024 10:00:00 +0000'
        message.set_content('Body ' + str(uid))
        message.add_attachment(attachment, maintype='application', subtype='octet-stream', filename='source.dat')
        raw = message.as_bytes(); relative = f'{path}/messages/{uid}.eml'
        (root / relative).write_bytes(raw)
        rows.append({'uid': uid, 'uidvalidity': 42, 'flags': flags or ['\\Seen', '\\Flagged'],
                     'internal_date': '2024-01-01T11:00:00+01:00', 'size': len(raw),
                     'sha256': hashlib.sha256(raw).hexdigest(), 'relative_path': relative, 'present': True})
    info = {'path': path, 'wire_name_b64': base64.b64encode(folder.encode()).decode(),
            'name': folder, 'uidvalidity': 42, 'complete': True, 'message_count': 2}
    (root / path / 'folder.json').write_text(json.dumps({**info, 'attributes': [], 'initial_uids': [1, 2], 'final_uids': [1, 2]}))
    (root / path / 'messages.jsonl').write_text('\n'.join(json.dumps(row) for row in rows))
    (root / 'mailbox.json').write_text(json.dumps({'schema_version': 1, 'source_id': source_id, 'host': host,
                                                'username': username, 'complete': True, 'folders': [info], 'message_count': 2}))
    return rows


@mock_aws
class MigrationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', TABLE='migration', BUCKET='migration-fixture',
                         DOMAIN='extraliferecords.com', RECEIVE_DOMAIN='inbox.playloudr.com',
                         MAILBOX_REGISTRY=json.dumps(CONFIG), SENDING_ENABLED='false', AI_SENDING_ENABLED='false',
                         HUMAN_SENDING_ENABLED='false', OWNER_CLIENT_ID='owner-client',
                         HUMAN_SENDER_ADDRESSES='hello@extraliferecords.com', CONFIGURATION_SET='', EXTERNAL_SECRET='')
        env.start(); self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='migration', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'} for key in ('pk', 'sk', 'received')],
            KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
            GlobalSecondaryIndexes=[{'IndexName': 'Chronological', 'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'received', 'KeyType': 'RANGE'}], 'Projection': {'ProjectionType': 'ALL'}}])
        boto3.client('s3').create_bucket(Bucket='migration-fixture', CreateBucketConfiguration={'LocationConstraint': 'eu-west-1'})
        self.table, self.s3, self.ses = service.clients()
        self.table.put_item(Item={'pk': 'ADMIN', 'sk': 'owner', 'enabled': True})
        for name in service.NAMES:
            self.table.put_item(Item={'pk': name, 'sk': 'META', 'state': 'Reserved', 'generation': 'legacy'})
        self.generation = migration.provision(self.table, ['mail-elr', 'mail-robots'], 'owner', apply=True)[0]['generation']
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.rows = fixture(self.root)

    def run_import(self, **kwargs):
        return migration.import_archive(self.table, self.s3, 'migration-fixture', 'mail-elr', self.generation,
                                        self.root, **kwargs)

    def test_alias_registry_preserves_old_transport_and_rejects_collision(self):
        self.assertEqual(mailboxes.routes()['one@inbox.playloudr.com'], 'one')
        self.assertEqual(mailboxes.routes()['dizzyteapot@extraliferecords.com'], 'one')
        self.assertEqual(mailboxes.routes()['hello@meandtherobots.com'], 'mail-robots')
        with patch.dict(os.environ, MAILBOX_REGISTRY=json.dumps({**CONFIG, 'mail-robots': {'address': 'hello@extraliferecords.com'}})):
            with self.assertRaisesRegex(ValueError, 'more than one'): mailboxes.registry()
        with patch.dict(os.environ, MAILBOX_REGISTRY=json.dumps({'ADMIN': {'address': 'admin@example.com'}})):
            with self.assertRaises(ValueError): mailboxes.registry()

    def test_provision_and_import_do_not_grant_ai_or_touch_reserved_generations(self):
        self.run_import(apply=True)
        for name in service.NAMES: self.assertEqual(service.meta(self.table, name)['generation'], 'legacy')
        with self.assertRaises(service.Denied): reader.visible(self.table, 'mail-elr')
        with self.assertRaises(service.Denied): ai_access.grant(self.table, 'owner', 'clientA', 'mail-elr', 'read')
        with self.assertRaises(service.Denied): service.authorize(self.table, 'mail-elr', 'stranger')
        with patch.dict(os.environ, MAILBOX_REGISTRY=json.dumps({'mail-elr': {'address': 'changed@example.com'}})):
            with self.assertRaises(service.Denied): service.meta(self.table, 'mail-elr')

    def test_verified_import_restart_keeps_bytes_dates_folders_and_distinct_uids(self):
        first = self.run_import(apply=True); again = self.run_import(apply=True)
        self.assertEqual(first['created'], 2); self.assertEqual(again['unchanged'], 2)
        self.assertEqual(again['created'], 0)
        result = migration.reconcile_archive(self.table, self.s3, 'migration-fixture', 'mail-elr', self.generation, self.root)
        self.assertTrue(result['storage_reconciled']); self.assertEqual(result['verified_messages'], 2)
        self.assertFalse(result['ready_for_cutover'])
        boxes = service.dispatch(self.table, self.s3, self.ses, 'owner', 'GET', '/mailboxes', {}, {})['mailboxes']
        self.assertEqual(next(box['address'] for box in boxes if box['name'] == 'mail-elr'), 'hello@extraliferecords.com')
        rows = service.list_messages(self.table, self.s3, 'mail-elr#' + self.generation, {})['messages']
        self.assertEqual(len(rows), 2); self.assertEqual(rows[0]['received'], '2024-01-01T10:00:00.000000Z')
        other = service.meta(self.table, 'mail-robots')
        with self.assertRaises(service.Denied): service.message(self.table, other, rows[0]['id'])

    def test_dry_run_never_writes_and_hash_mismatch_blocks_every_write(self):
        result = self.run_import()
        self.assertEqual(result['created'], 2)
        self.assertNotIn('Contents', self.s3.list_objects_v2(Bucket='migration-fixture'))
        (self.root / self.rows[1]['relative_path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'SHA256'): self.run_import(apply=True)
        self.assertNotIn('Contents', self.s3.list_objects_v2(Bucket='migration-fixture'))

    def test_partial_archive_and_missing_uid_are_rejected(self):
        path = self.root / 'mailbox.json'; manifest = json.loads(path.read_text())
        manifest['complete'] = False; path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'complete'): migration.archive(self.root)
        manifest['complete'] = True; path.write_text(json.dumps(manifest))
        path = self.root / 'folders/fixture/42/folder.json'; value = json.loads(path.read_text())
        value['final_uids'].append(3); path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'inventory'): migration.archive(self.root)

    def test_nonselectable_hierarchy_container_retains_inventory_without_message_path(self):
        path = self.root / 'mailbox.json'; manifest = json.loads(path.read_text())
        container = {'selectable':False,'complete':True,'message_count':0,'name':'Projects', 'wire_name_b64':'UHJvamVjdHM='}
        manifest['folders'].append(container); path.write_text(json.dumps(manifest))
        result = migration.inspect(self.root)
        self.assertEqual(result['messages'], 2)
        self.assertEqual(result['nonselectable_containers'], ['Projects'])
        self.assertEqual(self.run_import(apply=True)['created'], 2)
        container['message_count'] = 1; path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Nonselectable'): migration.archive(self.root)

    def test_archive_paths_cannot_escape_and_uidvalidity_is_part_of_identity(self):
        with self.assertRaises(ValueError): migration.private_file(self.root, '../outside.eml')
        self.assertNotEqual(migration.source_key('source', 'folder', 1, 1), migration.source_key('source', 'folder', 2, 1))
        self.assertNotEqual(migration.source_key('source', 'folder', 1, 1), migration.source_key('source2', 'folder', 1, 1))

    def test_catchup_refreshes_source_flags_but_preserves_owner_edits(self):
        self.run_import(apply=True)
        path = self.root / 'folders/fixture/42/messages.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['flags'] = []; path.write_text('\n'.join(json.dumps(row) for row in rows))
        self.assertEqual(self.run_import(apply=True)['metadata_refreshed'], 1)
        _, archived, _ = migration.archive(self.root)
        key = {'pk': 'mail-elr#' + self.generation, 'sk': 'MSG#' + archived[1]['id']}
        self.table.update_item(Key=key, UpdateExpression='SET seen=:s', ExpressionAttributeValues={':s': False})
        result = self.run_import(apply=True)
        self.assertEqual(result['conflicts'][0]['reason'], 'destination_changed_since_import')
        self.assertFalse(self.table.get_item(Key=key)['Item']['seen'])

    def test_reassignment_during_upload_never_imports_into_new_generation(self):
        original = migration.safe_put
        def raced(*args):
            original(*args)
            self.table.update_item(Key={'pk': 'mail-elr', 'sk': 'META'}, UpdateExpression='SET generation=:g', ExpressionAttributeValues={':g': 'new-owner'})
        with patch.object(migration, 'safe_put', side_effect=raced):
            with self.assertRaisesRegex(ValueError, 'generation'): self.run_import(apply=True)
        rows = self.table.scan()['Items']
        self.assertFalse(any(row['sk'].startswith('MSG#') for row in rows))

    def test_envelope_routes_multi_domain_and_aliases_once_without_cross_delivery(self):
        raw = (self.root / self.rows[0]['relative_path']).read_bytes()
        self.s3.put_object(Bucket='migration-fixture', Key='incoming/test', Body=raw)
        notice = {'mail': {'timestamp': '2027-01-01T00:00:00Z'}, 'receipt': {
            'action': {'bucketName': 'migration-fixture', 'objectKey': 'incoming/test'},
            'recipients': ['hello@extraliferecords.com', 'contact@extraliferecords.com', 'hello@meandtherobots.com'],
            'virusVerdict': {'status': 'PASS'}, 'spamVerdict': {'status': 'PASS'}}}
        event = {'Records': [{'Sns': {'Message': json.dumps(notice)}}]}
        service.receive(event, None); service.receive(event, None)
        for name in ('mail-elr', 'mail-robots'):
            pk = service.partition(service.meta(self.table, name))
            self.assertEqual(len(service.list_messages(self.table, self.s3, pk, {})['messages']), 1)
        self.assertEqual(service.list_messages(self.table, self.s3, 'one#legacy', {})['messages'], [])
        notice['receipt']['recipients'] = ['unknown@example.com']
        with self.assertRaisesRegex(ValueError, 'unconfigured'):
            service.receive({'Records': [{'Sns': {'Message': json.dumps(notice)}}]}, None)

    def test_large_attachment_chunking_is_bounded_and_reassembles_exactly(self):
        message = EmailMessage(); message.set_content('Body')
        data = b'file' * (1024 * 1024)
        message.add_attachment(data, maintype='application', subtype='octet-stream', filename='large.bin')
        raw = message.as_bytes(); chunks = []; offset = '0'
        while True:
            result = service.attachment_chunk(raw, 2, offset)
            chunk = base64.b64decode(result['data']); self.assertLessEqual(len(chunk), 1024 * 1024)
            chunks.append(chunk)
            if result['next_offset'] is None: break
            offset = str(result['next_offset'])
        self.assertEqual(b''.join(chunks), data)
        with self.assertRaises(ValueError): service.attachment_chunk(raw, 2, '-1')

    def test_manual_send_gate_rejects_mcp_clients_and_disabled_route(self):
        claims = {'sub': 'owner', 'client_id': 'owner-client', 'token_use': 'access', 'scope': 'openid', 'exp': int(time.time()) + 300}
        data = {'to': 'recipient@example.com', 'body': 'Draft', 'request_id': 'manual-fixture-1234'}
        ses = Mock()
        with self.assertRaises(service.Denied):
            human_send.dispatch(self.table, self.s3, ses, claims, '/human/mailboxes/mail-elr/send', data)
        with patch.dict(os.environ, HUMAN_SENDING_ENABLED='true'):
            with self.assertRaises(service.Denied):
                human_send.dispatch(self.table, self.s3, ses, {**claims, 'client_id': 'mcp-client'}, '/human/mailboxes/mail-elr/send', data)
            with self.assertRaises(service.Denied):
                human_send.dispatch(self.table, self.s3, ses, claims, '/human/mailboxes/mail-robots/send', data)
            with self.assertRaises(service.Denied):
                service.send(self.table, self.s3, ses, service.meta(self.table, 'mail-elr'), 'owner', data)
        ses.send_email.assert_not_called()

    def test_manual_send_uses_pinned_identity_and_never_retries_uncertainty(self):
        claims = {'sub': 'owner', 'client_id': 'owner-client', 'token_use': 'access', 'scope': 'openid', 'exp': int(time.time()) + 300}
        data = {'from': 'spoof@example.com', 'to': 'recipient@example.com', 'body': 'Draft', 'request_id': 'manual-fixture-1234'}
        ses = Mock(); ses.send_email.side_effect = TimeoutError('lost response')
        with patch.dict(os.environ, HUMAN_SENDING_ENABLED='true'):
            with self.assertRaises(TimeoutError): human_send.dispatch(self.table, self.s3, ses, claims, '/human/mailboxes/mail-elr/send', data)
            result = human_send.dispatch(self.table, self.s3, ses, claims, '/human/mailboxes/mail-elr/send', data)
        self.assertEqual(result['status'], 'uncertain'); self.assertEqual(ses.send_email.call_count, 1)
        self.assertEqual(ses.send_email.call_args.kwargs['FromEmailAddress'], 'hello@extraliferecords.com')

    def test_pre_migration_send_claim_remains_retry_compatible(self):
        data = {'to': 'recipient@example.com', 'body': 'Legacy', 'request_id': 'legacy-fixture-1234'}
        old_digest = hashlib.sha256(json.dumps({**service.validate_draft(data), 'reply_id': None}, sort_keys=True).encode()).hexdigest()
        self.table.put_item(Item={'pk':'one#legacy','sk':'SEND#legacy-fixture-1234','status':'uncertain','payload_hash':old_digest})
        ses = Mock()
        with patch.dict(os.environ, SENDING_ENABLED='true'):
            result = service.send(self.table, self.s3, ses, service.meta(self.table, 'one'), 'owner', data)
        self.assertEqual(result['status'], 'uncertain'); ses.send_email.assert_not_called()

    def test_manual_numbered_mailbox_and_html_reply_with_inline_image(self):
        from email import message_from_bytes
        claims = {'sub':'owner','client_id':'owner-client','token_use':'access','scope':'openid','exp':int(time.time())+300}
        data = {'to':'recipient@example.com','bcc':'private@example.com','body':'Reply',
                'body_html':'<p>Reply</p><img src="data:image/png;base64,YWJj">',
                'in_reply_to':'<original@example.com>','references':'<original@example.com>',
                'attachments':[{'name':'sample.txt','data':'dGVzdA=='}], 'request_id':'manual-numbered-12345'}
        ses = Mock(); ses.send_email.return_value = {'MessageId':'ses-accepted'}
        with patch.dict(os.environ, HUMAN_SENDING_ENABLED='true', HUMAN_SENDER_ADDRESSES='one@extraliferecords.com'):
            result = human_send.dispatch(self.table,self.s3,ses,claims,'/human/mailboxes/one/send',data)
        self.assertEqual(result['status'],'accepted')
        msg = message_from_bytes(ses.send_email.call_args.kwargs['Content']['Raw']['Data'])
        self.assertEqual(msg['From'],'one@extraliferecords.com')
        self.assertEqual(msg['In-Reply-To'],'<original@example.com>')
        self.assertIsNone(msg['Bcc'])
        self.assertEqual(len([p for p in msg.walk() if p.get('Content-ID')]),1)
        self.assertIn('private@example.com',ses.send_email.call_args.kwargs['Destination']['ToAddresses'])

    def test_stack_guard_blocks_each_replacement_send_switch(self):
        session = Mock()
        baseline = {'SendingEnabled':'false','AiSendingEnabled':'false','HumanSendingEnabled':'false',
                    'Domain':'extraliferecords.com','ReceiveDomain':'inbox.playloudr.com'}
        for key in ('SendingEnabled', 'AiSendingEnabled', 'HumanSendingEnabled'):
            values = {**baseline, key:'true'}
            session.client.return_value.describe_stacks.return_value = {'Stacks':[{'Parameters':[
                {'ParameterKey':name,'ParameterValue':value} for name,value in values.items()],
                'StackStatus':'UPDATE_COMPLETE','Outputs':[]}]}
            with self.assertRaisesRegex(ValueError, 'sending controls'): migration.check_stack(session, 'fixture')


if __name__ == '__main__': unittest.main()
