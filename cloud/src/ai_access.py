"""Mailbox-scoped AI access and exact-draft approval. Mail text is data, never authority."""
import base64
import hashlib
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import getaddresses, formatdate, make_msgid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
import service
import reader
import external
from service import Denied

PERMISSIONS = ('read', 'draft', 'send_approved')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise ValueError('Invalid identifier')
    return value


def owner_mailbox(table, subject, name):
    if name in service.mailbox_names():
        item = service.authorize(table, name, subject)
        return {'name': name, 'address': service.mailbox_address(name), 'generation': item['generation'], 'item': item}
    admin = table.get_item(Key={'pk': 'ADMIN', 'sk': subject}, ConsistentRead=True).get('Item')
    account = external.accounts().get(name) if admin and admin.get('enabled') else None
    if not account: raise Denied('Mailbox is not assigned to this account')
    # Changing the mailbox address or IMAP principal revokes older grants.
    generation = digest({k: account[k] for k in ('address', 'username', 'host')})
    return {'name': name, 'address': account['address'], 'generation': generation, 'external': account}


def grant(table, subject, client, name, permission):
    row = table.get_item(Key={'pk': 'GRANT#' + subject, 'sk': identifier(client)}, ConsistentRead=True).get('Item')
    access = (row or {}).get('mailboxes', {}).get(name, {})
    if not row or not row.get('enabled') or permission not in access.get('permissions', []):
        raise Denied('This AI connection does not have that mailbox permission')
    mailbox = owner_mailbox(table, subject, name)
    if access.get('generation') != mailbox['generation']: raise Denied('Mailbox assignment changed; renew its AI permission')
    return mailbox


def read(table, s3, mailbox, tail, query=None):
    if 'external' in mailbox:
        return external.dispatch(table, mailbox['external'], mailbox['name'], 'GET', tail, query or {}, {})
    if tail == ['messages']:
        return service.list_messages(table, s3, service.partition(mailbox['item']), query or {}, unread=(query or {}).get('unread') == '1')
    return service.read_message(s3, service.message(table, mailbox['item'], tail[1]), tail)


def save_draft(table, s3, subject, client, name, data):
    mailbox = grant(table, subject, client, name, 'draft')
    request = identifier(data.get('request_id'))
    if len(request) < 16: raise ValueError('Use a unique request_id of at least 16 characters')
    content = service.validate_draft(data)
    # The existing robot-draft writers deliberately limit drafts to To/Cc and text.
    if content['bcc'] or content['attachments']: raise ValueError('AI draft creation supports To, Cc and text; add attachments in SimpleMail before approval')
    reference = digest([client, name, request])[:32]
    key = {'pk': 'AI#' + subject, 'sk': 'DRAFT#' + reference}
    payload_hash = digest({**content, 'reply_to_id': data.get('reply_to_id')})
    row = {**key, 'id': reference, 'client': client, 'mailbox': name, 'generation': mailbox['generation'], 'state': 'saving', 'payload_hash': payload_hash, 'created': int(time.time())}
    try:
        table.put_item(Item=row, ConditionExpression='attribute_not_exists(pk)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
        existing = table.get_item(Key=key, ConsistentRead=True)['Item']
        if existing['payload_hash'] != payload_hash: raise ValueError('Request ID already used for different draft content')
        return {'reference': reference, 'state': existing['state'], 'draft_id': existing.get('draft_id')}
    # Claim stays saving if APPEND loses its response. Never repeat an uncertain append.
    values = {**content, 'request_id': reference, 'reason': 'AI draft for owner review', 'reply_to_id': data.get('reply_to_id')}
    if 'external' in mailbox:
        saved = external.dispatch(table, mailbox['external'], name, 'POST', ['drafts'], {}, values)
    else:
        saved = reader.draft(table, s3, mailbox['item'], values)
    if not saved.get('id'): return {'reference': reference, 'state': 'saving', 'message': 'Draft append needs reconciliation; do not repeat with a new request ID'}
    table.update_item(Key=key, UpdateExpression='SET #s=:s, draft_id=:d, reply_id=:r',
                      ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':s': 'draft', ':d': saved['id'], ':r': data.get('reply_to_id') or ''})
    return {'reference': reference, 'draft_id': saved['id'], 'state': 'draft'}


def draft_snapshot(table, s3, subject, reference):
    row = table.get_item(Key={'pk': 'AI#' + subject, 'sk': 'DRAFT#' + identifier(reference)}, ConsistentRead=True).get('Item')
    if not row or not row.get('draft_id'): raise Denied('Draft is unavailable or still needs reconciliation')
    mailbox = owner_mailbox(table, subject, row['mailbox'])
    if row['generation'] != mailbox['generation']: raise Denied('Mailbox assignment changed')
    if 'external' in mailbox:
        imap = external.connect(mailbox['external'])
        try:
            folder, _, uid, _ = external.locate(imap, row['draft_id'])
            if folder != 'Drafts': raise Denied('Message is no longer a draft')
            raw = external.fetch_raw(imap, uid)
        finally:
            try: imap.logout()
            except Exception: pass
        msg = message_from_bytes(raw, policy=default)
        content = {field: str(msg.get(field, '')) for field in ('to', 'cc', 'bcc', 'subject')}
        body = msg.get_body(preferencelist=('plain',))
        if body is None: raise ValueError('Review and save a plain-text draft in SimpleMail first')
        content['body'] = body.get_content()
        content['attachments'] = [{'name': part.get_filename() or 'attachment', 'data': base64.b64encode(part.get_payload(decode=True) or b'').decode()} for part in msg.iter_attachments()]
        revision = hashlib.sha256(raw).hexdigest()
        reply_header = str(msg.get('In-Reply-To', ''))
    else:
        current = service.message(table, mailbox['item'], row['draft_id'])
        if current.get('kind') != 'draft' or current.get('folder') != 'Drafts': raise Denied('Message is no longer a draft')
        content = json.loads(service.read_raw(s3, current)); revision = current['revision']; reply_header = ''
    content = service.validate_draft(content)
    snapshot = {'reference': reference, 'mailbox': row['mailbox'], 'generation': row['generation'], 'sender': mailbox['address'], 'draft_id': row['draft_id'], 'revision': revision, 'content': content, 'reply_id': row.get('reply_id', ''), 'reply_header': reply_header}
    return row, mailbox, snapshot, digest(snapshot)


def approve_draft(table, s3, subject, data):
    row, _, snapshot, current_digest = draft_snapshot(table, s3, subject, data.get('reference'))
    if data.get('digest') != current_digest: raise Denied('Draft changed; review the current recipients, text and attachments')
    recipients = getaddresses([snapshot['content'][key] for key in ('to', 'cc', 'bcc')])
    if not 1 <= len(recipients) <= 20: raise ValueError('Review a message with 1 to 20 recipients before approving')
    grant(table, subject, row['client'], row['mailbox'], 'send_approved')
    key = {'pk': 'APPROVAL#' + subject, 'sk': row['id']}
    try:
        table.put_item(Item={**key, 'digest': current_digest, 'revision': snapshot['revision'], 'client': row['client'], 'mailbox': row['mailbox'], 'state': 'approved', 'expires': int(time.time()) + 86400},
                       ConditionExpression='attribute_not_exists(pk) OR #s=:approved OR #s=:revoked', ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':approved': 'approved', ':revoked': 'revoked'})
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
        raise Denied('This draft has already entered sending; check its outcome') from None
    return {'approved': True, 'reference': row['id'], 'digest': current_digest}


def approval_status(table, subject, reference, approval, mailbox):
    """Reconcile native provider receipts without ever repeating the send."""
    if approval and approval['state'] not in ('approved', 'revoked') and 'external' not in mailbox:
        receipt = table.get_item(Key={'pk': service.partition(mailbox['item']), 'sk': 'SEND#' + reference}, ConsistentRead=True).get('Item')
        if receipt:
            approval = {**approval, 'state': receipt['status'], 'provider_id': receipt.get('provider_id', '')}
    return approval


def send_approved(table, s3, ses, subject, client, reference):
    key = {'pk': 'APPROVAL#' + subject, 'sk': identifier(reference)}
    approval = table.get_item(Key=key, ConsistentRead=True).get('Item')
    if not approval or approval['client'] != client: raise Denied('Owner approval required')
    mailbox = grant(table, subject, client, approval['mailbox'], 'send_approved')
    approval = approval_status(table, subject, reference, approval, mailbox)
    if approval['state'] != 'approved': return {'state': approval['state'], 'provider_id': approval.get('provider_id'), 'refused': approval.get('refused', [])}
    if approval['expires'] <= time.time(): raise Denied('Approval expired; review the draft again')
    _, mailbox, snapshot, current_digest = draft_snapshot(table, s3, subject, reference)
    if current_digest != approval['digest']: raise Denied('Draft changed after approval; review it again')
    if os.environ.get('AI_SENDING_ENABLED') != 'true': raise Denied('Approved AI sending is not enabled')
    if 'external' in mailbox and not mailbox['external'].get('smtp_host'): raise Denied('Configure this mailbox SMTP host before sending')
    if 'external' not in mailbox and os.environ.get('SENDING_ENABLED') != 'true': raise Denied('Outbound mail is not enabled')
    try:
        table.update_item(Key=key, UpdateExpression='SET #s=:sending', ConditionExpression='#s=:approved AND digest=:digest AND expires>:now', ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':sending': 'uncertain', ':approved': 'approved', ':digest': current_digest, ':now': int(time.time())})
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
        return {'state': table.get_item(Key=key, ConsistentRead=True)['Item']['state']}
    # Recheck grants after the durable claim. Revocation never permits an automatic retry.
    grant(table, subject, client, approval['mailbox'], 'send_approved')
    if 'external' not in mailbox:
        result = service.send(table, s3, ses, mailbox['item'], subject, {**snapshot['content'], 'request_id': reference, 'reply_id': snapshot['reply_id'] or None})
        state = result['status']; provider_id = result.get('provider_id', '')
        refused = []
    else:
        content = snapshot['content']; account = mailbox['external']; msg = EmailMessage()
        msg['From'] = snapshot['sender']; msg['Date'] = formatdate(); msg['Message-ID'] = make_msgid(domain=snapshot['sender'].split('@')[1])
        for field in ('to', 'cc', 'subject'):
            if content[field]: msg[field.title()] = content[field]
        if snapshot['reply_header']: msg['In-Reply-To'] = snapshot['reply_header']; msg['References'] = snapshot['reply_header']
        msg.set_content(content['body'])
        for attachment in content['attachments']: msg.add_attachment(base64.b64decode(attachment['data']), maintype='application', subtype='octet-stream', filename=attachment['name'])
        recipients = [address for _, address in getaddresses([content[k] for k in ('to', 'cc', 'bcc')])]
        if not 1 <= len(recipients) <= 20: raise ValueError('Specify 1 to 20 recipients')
        # TLS-only SMTP, to an owner-configured host. A lost DATA response stays uncertain.
        with smtplib.SMTP_SSL(account['smtp_host'], int(account.get('smtp_port', 465)), timeout=20, context=ssl.create_default_context()) as smtp:
            smtp.login(account['username'], account['password'])
            refused = list(smtp.send_message(msg, from_addr=snapshot['sender'], to_addrs=recipients).keys())
        state = 'partially_accepted' if refused else 'accepted'; provider_id = str(msg['Message-ID'])
        # Save acceptance before attempting the optional Sent copy. APPEND failure is not a failed send.
        table.update_item(Key=key, UpdateExpression='SET #s=:s, provider_id=:p, refused=:r', ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':s': state, ':p': provider_id, ':r': refused})
        imap = None
        try:
            imap = external.connect(account); names = external.folders(imap)
            if 'Sent' in names: imap.append(external.quote(names['Sent']), '(\\Seen)', imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        except Exception: pass
        finally:
            if imap:
                try: imap.logout()
                except Exception: pass
    table.update_item(Key=key, UpdateExpression='SET #s=:s, provider_id=:p, refused=:r', ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':s': state, ':p': provider_id, ':r': refused})
    return {'state': state, 'provider_id': provider_id, 'refused': refused}


def owner_dispatch(table, s3, subject, method, path, data):
    tail = path.removeprefix('/ai/').split('/')
    if tail == ['connections'] and method == 'GET':
        rows = table.query(KeyConditionExpression=Key('pk').eq('GRANT#' + subject), ConsistentRead=True)['Items']
        return {'connections': [{k: v for k, v in row.items() if k != 'pk'} for row in rows], 'client_ids': os.environ.get('MCP_CLIENT_IDS', '').split(',')}
    if tail == ['connections'] and method == 'POST':
        client = identifier(data.get('client_id'))
        if client not in os.environ.get('MCP_CLIENT_IDS', '').split(','): raise ValueError('Choose a configured OAuth client')
        mailboxes = {}
        for name, permissions in data.get('mailboxes', {}).items():
            mailbox = owner_mailbox(table, subject, name)
            if not isinstance(permissions, list) or not permissions or any(p not in PERMISSIONS for p in permissions): raise ValueError('Invalid permissions')
            mailboxes[name] = {'permissions': permissions, 'generation': mailbox['generation']}
        table.put_item(Item={'pk': 'GRANT#' + subject, 'sk': client, 'enabled': data.get('enabled') is True, 'mailboxes': mailboxes})
        return {'saved': True}
    if tail == ['mailboxes'] and method == 'GET':
        items = []
        for name in [*service.mailbox_names(), *external.accounts().keys()]:
            try:
                box = owner_mailbox(table, subject, name); items.append({'name': name, 'address': box['address']})
            except Denied: pass
        return {'mailboxes': items}
    if tail == ['drafts'] and method == 'GET':
        rows = table.query(KeyConditionExpression=Key('pk').eq('AI#' + subject) & Key('sk').begins_with('DRAFT#'), ConsistentRead=True)['Items']
        visible = []
        for row in rows:
            try:
                mailbox = owner_mailbox(table, subject, row['mailbox'])
                if row['generation'] != mailbox['generation']: continue
            except Denied: continue
            approval = table.get_item(Key={'pk': 'APPROVAL#' + subject, 'sk': row['id']}, ConsistentRead=True).get('Item')
            approval = approval_status(table, subject, row['id'], approval, mailbox)
            visible.append({**{k: v for k, v in row.items() if k not in ('pk', 'payload_hash')},
                            'state': approval['state'] if approval else row['state']})
        return {'drafts': sorted(visible, key=lambda row: row['created'], reverse=True)}
    if len(tail) == 2 and tail[0] == 'drafts' and method == 'GET':
        _, _, snapshot, current_digest = draft_snapshot(table, s3, subject, tail[1])
        return {**snapshot, 'digest': current_digest}
    if tail == ['approve'] and method == 'POST': return approve_draft(table, s3, subject, data)
    if tail == ['revoke'] and method == 'POST':
        key = {'pk': 'APPROVAL#' + subject, 'sk': identifier(data.get('reference'))}
        try:
            table.update_item(Key=key, UpdateExpression='SET #s=:revoked', ConditionExpression='#s=:approved', ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':revoked': 'revoked', ':approved': 'approved'})
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
            raise Denied('This approval was already used or is not active') from None
        return {'revoked': True}
    raise ValueError('Unknown AI settings endpoint')
