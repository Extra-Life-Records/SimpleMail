import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import boto3
from moto import mock_aws
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import service
import ai_access as access


@mock_aws
class AccessTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', TABLE='mail', BUCKET='mail-testing', DOMAIN='extraliferecords.com', RECEIVE_DOMAIN='inbox.playloudr.com', EXTERNAL_SECRET='', MCP_CLIENT_IDS='clientA,clientB', AI_SENDING_ENABLED='true', SENDING_ENABLED='true', CONFIGURATION_SET='')
        env.start(); self.addCleanup(env.stop)
        boto3.client('dynamodb').create_table(TableName='mail', BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'} for k in ('pk', 'sk')], KeySchema=[{'AttributeName':'pk','KeyType':'HASH'},{'AttributeName':'sk','KeyType':'RANGE'}])
        boto3.client('s3').create_bucket(Bucket='mail-testing', CreateBucketConfiguration={'LocationConstraint':'eu-west-1'})
        self.table, self.s3, _ = service.clients()
        self.table.put_item(Item={'pk':'one','sk':'META','generation':'first','state':'Assigned','subject':'owner'})
        self.configure()
        self.ses = MagicMock(); self.ses.send_email.return_value = {'MessageId':'ses-accepted'}

    def configure(self, enabled=True, permissions=None):
        return access.owner_dispatch(self.table, self.s3, 'owner', 'POST', '/ai/connections', {'client_id':'clientA','enabled':enabled,'mailboxes':{'one': permissions or ['read','draft','send_approved']}})

    def draft(self):
        return access.save_draft(self.table, self.s3, 'owner', 'clientA', 'one', {'request_id':'unique_request_1234','to':'fan@example.com','subject':'Reply','body':'An exact approved reply.'})['reference']

    def approve(self, reference):
        snapshot = access.owner_dispatch(self.table, self.s3, 'owner', 'GET', '/ai/drafts/' + reference, {})
        return access.approve_draft(self.table, self.s3, 'owner', {'reference':reference,'digest':snapshot['digest']})

    def test_mailboxes_are_explicit_and_grants_do_not_cross_clients_or_owners(self):
        self.assertEqual(access.grant(self.table,'owner','clientA','one','read')['address'],'one@extraliferecords.com')
        for subject, client, name in [('owner','clientB','one'),('other','clientA','one'),('owner','clientA','two')]:
            with self.assertRaises(service.Denied): access.grant(self.table, subject, client, name, 'read')
        self.configure(enabled=False)
        with self.assertRaises(service.Denied): access.grant(self.table,'owner','clientA','one','read')

    def test_reassignment_invalidates_previously_granted_access(self):
        self.table.update_item(Key={'pk':'one','sk':'META'},UpdateExpression='SET generation=:v',ExpressionAttributeValues={':v':'second'})
        with self.assertRaises(service.Denied): access.grant(self.table,'owner','clientA','one','read')

    def test_drafts_are_shared_with_the_existing_mailbox_and_retries_are_idempotent(self):
        reference=self.draft(); self.assertEqual(reference,self.draft())
        row=self.table.get_item(Key={'pk':'AI#owner','sk':'DRAFT#'+reference})['Item']
        draft=service.message(self.table,service.authorize(self.table,'one','owner'),row['draft_id'])
        self.assertEqual(draft['folder'],'Drafts')
        with self.assertRaises(ValueError): access.save_draft(self.table,self.s3,'owner','clientA','one',{'request_id':'unique_request_1234','to':'different@example.com','body':'changed'})

    def test_send_requires_owner_approval_and_cannot_be_repeated(self):
        reference=self.draft()
        with self.assertRaises(service.Denied): access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)
        self.approve(reference)
        self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'accepted')
        self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'accepted')
        self.ses.send_email.assert_called_once()

    def test_changed_recipient_or_attachment_invalidates_exact_revision(self):
        reference=self.draft(); approved=self.approve(reference)
        row=self.table.get_item(Key={'pk':'AI#owner','sk':'DRAFT#'+reference})['Item']
        current=service.message(self.table,service.authorize(self.table,'one','owner'),row['draft_id'])
        service.dispatch(self.table,self.s3,self.ses,'owner','POST','/mailboxes/one/drafts',{'id':row['draft_id'],'revision':current['revision'],'to':'someoneelse@example.com','body':'Changed','attachments':[{'name':'new.txt','data':'eA=='}]}, {})
        with self.assertRaises(service.Denied): access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)
        with self.assertRaises(service.Denied): access.approve_draft(self.table,self.s3,'owner',approved)
        self.ses.send_email.assert_not_called()

    def test_network_uncertainty_never_repeats_a_send(self):
        reference=self.draft(); self.approve(reference); self.ses.send_email.side_effect=TimeoutError('lost DATA response')
        with self.assertRaises(TimeoutError): access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)
        self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'uncertain')
        self.ses.send_email.assert_called_once()

    def test_delivery_reconciles_uncertain_send_without_retry(self):
        reference=self.draft(); self.approve(reference); self.ses.send_email.side_effect=TimeoutError('lost response')
        with self.assertRaises(TimeoutError): access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)
        self.table.update_item(Key={'pk':'one#first','sk':'SEND#'+reference},UpdateExpression='SET #s=:s, provider_id=:p',ExpressionAttributeNames={'#s':'status'},ExpressionAttributeValues={':s':'delivered',':p':'receipt'})
        self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'delivered')
        listing=access.owner_dispatch(self.table,self.s3,'owner','GET','/ai/drafts',{})
        self.assertEqual(listing['drafts'][0]['state'],'delivered')
        self.ses.send_email.assert_called_once()

    def test_external_partial_acceptance_and_failed_sent_copy_never_resend(self):
        reference=self.draft(); self.approve(reference)
        row, mailbox, snapshot, current_digest = access.draft_snapshot(self.table,self.s3,'owner',reference)
        external_box={**mailbox,'external':{'smtp_host':'smtp.example.com','username':'owner','password':'test-only'}}
        smtp=MagicMock(); smtp.__enter__.return_value=smtp
        smtp.send_message.return_value={'rejected@example.com':(550,b'No mailbox')}
        with patch.object(access,'grant',return_value=external_box),patch.object(access,'draft_snapshot',return_value=(row,external_box,snapshot,current_digest)),patch.object(access.smtplib,'SMTP_SSL',return_value=smtp),patch.object(access.external,'connect',side_effect=TimeoutError('Sent append unavailable')):
            self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'partially_accepted')
            self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'partially_accepted')
            smtp.send_message.assert_called_once()

    def test_external_uncertain_data_response_is_never_repeated(self):
        reference=self.draft(); self.approve(reference)
        row, mailbox, snapshot, current_digest = access.draft_snapshot(self.table,self.s3,'owner',reference)
        external_box={**mailbox,'external':{'smtp_host':'smtp.example.com','username':'owner','password':'test-only'}}
        smtp=MagicMock(); smtp.__enter__.return_value=smtp; smtp.send_message.side_effect=TimeoutError('DATA response lost')
        with patch.object(access,'grant',return_value=external_box),patch.object(access,'draft_snapshot',return_value=(row,external_box,snapshot,current_digest)),patch.object(access.smtplib,'SMTP_SSL',return_value=smtp):
            with self.assertRaises(TimeoutError): access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)
            self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'uncertain')
            smtp.send_message.assert_called_once()

    def test_revocation_and_expiry_block_previously_approved_send(self):
        reference=self.draft(); self.approve(reference)
        access.owner_dispatch(self.table,self.s3,'owner','POST','/ai/revoke',{'reference':reference})
        self.assertEqual(access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)['state'],'revoked')
        self.approve(reference)
        self.table.update_item(Key={'pk':'APPROVAL#owner','sk':reference},UpdateExpression='SET expires=:e',ExpressionAttributeValues={':e':int(time.time())-1})
        with self.assertRaises(service.Denied): access.send_approved(self.table,self.s3,self.ses,'owner','clientA',reference)
        self.ses.send_email.assert_not_called()

    def test_ai_token_cannot_call_the_owner_approval_endpoint(self):
        with patch.dict(os.environ,OWNER_CLIENT_ID='owner-client',MCP_ENABLED='true'):
            event={'rawPath':'/ai/approve','body':'{}','requestContext':{'http':{'method':'POST'},'authorizer':{'jwt':{'claims':{'sub':'owner','client_id':'clientA','token_use':'access','exp':int(time.time())+60}}}}}
            self.assertEqual(service.api(event,None)['statusCode'],403)


class ProtocolTests(unittest.TestCase):
    def test_metadata_challenge_and_no_model_approval_tool(self):
        import remote_mcp
        with patch.dict(os.environ,MCP_RESOURCE='https://mail.example/mcp',MCP_ISSUER='https://issuer.example'):
            response=remote_mcp.api({'rawPath':'/mcp','requestContext':{'http':{'method':'POST'}}},None)
            self.assertEqual(response['statusCode'],401)
            self.assertIn('oauth-protected-resource/mcp',response['headers']['WWW-Authenticate'])
            metadata=remote_mcp.api({'rawPath':'/.well-known/oauth-protected-resource/mcp','requestContext':{'http':{'method':'GET'}}},None)
            self.assertEqual(json.loads(metadata['body'])['resource'],'https://mail.example/mcp')
            self.assertEqual(json.loads(metadata['body'])['scopes_supported'], ['https://mail.example/mcp/read', 'https://mail.example/mcp/draft', 'https://mail.example/mcp/send-approved'])
        self.assertFalse(any(tool['name'] in ('approve','mail_approve','set_permissions') for tool in remote_mcp.TOOLS))
        self.assertEqual(remote_mcp.rpc({}, {'jsonrpc':'2.0','id':1,'method':'initialize'})['result']['protocolVersion'],'2025-06-18')
        self.assertIsNone(remote_mcp.rpc({}, {'jsonrpc':'2.0','method':'notifications/initialized'}))

    def test_signed_token_requires_resource_audience_and_registered_client(self):
        import jwt
        import remote_mcp
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        claims={'iss':'https://issuer.example','aud':'https://mail.example/mcp','sub':'owner','client_id':'clientA','token_use':'access','scope':'https://mail.example/mcp/read','iat':int(time.time()),'exp':int(time.time())+60}
        mockkeys=MagicMock(); mockkeys.get_signing_key_from_jwt.return_value.key=key.public_key()
        with patch.dict(os.environ,MCP_RESOURCE=claims['aud'],MCP_ISSUER=claims['iss'],MCP_CLIENT_IDS='clientA'),patch.object(remote_mcp,'keys',return_value=mockkeys):
            self.assertEqual(remote_mcp.verify_token('Bearer '+jwt.encode(claims,key,algorithm='RS256'))['sub'],'owner')
            for update in [{'aud':'https://wrong.example'},{'client_id':'anotherAI'},{'token_use':'id'},{'exp':1},{'scope':'simplemail-ai/read'},{'scope':'https://mail.example/mcp/draft'}]:
                with self.assertRaises(service.Denied): remote_mcp.verify_token('Bearer '+jwt.encode({**claims,**update},key,algorithm='RS256'))
            # Classic Cognito hosted UI can complete OAuth without binding aud.
            unbound = {name: value for name, value in claims.items() if name != 'aud'}
            with self.assertRaises(service.Denied):
                remote_mcp.verify_token('Bearer '+jwt.encode(unbound,key,algorithm='RS256'))
