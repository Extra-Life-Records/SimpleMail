"""Reserved mailboxes. Every data operation checks the current assignment.

API Gateway validates Cognito access tokens. No client supplies a sender, S3 key,
or employee identity. Administrative assignment is a separate operator command.
"""
import base64
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import getaddresses, formatdate, make_msgid

import boto3
import mailboxes
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

NAMES = ('one', 'two', 'three', 'four', 'five')
FOLDERS = ('Inbox', 'Sent', 'Drafts', 'Junk', 'Trash')
MAX_RAW = 30 * 1024 * 1024
MAX_ATTACHMENT = 3 * 1024 * 1024


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def search_text(text):
    # DynamoDB's item bound is bytes, not Unicode characters.
    return text.casefold().encode('utf-8')[:180000].decode('utf-8', 'ignore')


class Denied(Exception):
    pass


def clients():
    return (boto3.resource('dynamodb').Table(os.environ['TABLE']),
            boto3.client('s3'), boto3.client('sesv2'))


def meta(table, name):
    configured = mailboxes.registry()
    if name not in configured:
        raise Denied('Unknown mailbox')
    item = table.get_item(Key={'pk': name, 'sk': 'META'}, ConsistentRead=True).get('Item')
    if not item:
        raise Denied('Mailbox has not been provisioned')
    if name not in NAMES and item.get('address') != configured[name]['address']:
        raise Denied('Mailbox identity changed; operator reconciliation required')
    return item


def mailbox_names():
    return tuple(mailboxes.registry())


def mailbox_address(name):
    return mailboxes.registry()[name]['address']


def mailbox_folders(item):
    return list(dict.fromkeys([*FOLDERS, *item.get('import_folders', [])]))


def authorize(table, name, subject):
    item = meta(table, name)
    if item['state'] == 'Disabled':
        raise Denied('Mailbox is disabled')
    # An old token cannot retain access following disable or reassignment.
    owner = table.get_item(Key={'pk': 'ADMIN', 'sk': subject}, ConsistentRead=True).get('Item')
    if not (owner and owner.get('enabled')) and not (
            item['state'] == 'Assigned' and item.get('subject') == subject):
        raise Denied('Mailbox is not assigned to this account')
    return item


def partition(item):
    # Each assignment has a separate namespace. History never follows a seat.
    return item['pk'] + '#' + item['generation']


def message(table, item, mid):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', mid):
        raise ValueError('Invalid message ID')
    row = table.get_item(Key={'pk': partition(item), 'sk': 'MSG#' + mid},
                         ConsistentRead=True).get('Item')
    if not row:
        raise Denied('Message unavailable')
    return row


def parse(raw, maximum=500000):
    msg = message_from_bytes(raw, policy=default)
    text = []
    html = []
    attachments = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        if part.get_filename() or part.get_content_disposition() == 'attachment':
            attachments.append({'part': index, 'name': part.get_filename() or 'attachment',
                                'type': part.get_content_type(),
                                'size': len(part.get_payload(decode=True) or b'')})
        elif part.get_content_type() == 'text/plain':
            text.append(str(part.get_content()))
        elif part.get_content_type() == 'text/html':
            html.append(str(part.get_content()))
    # Keep original HTML for the desktop sandboxed, sanitized reader.
    # Plain-text consumers also retain safe action URLs instead of losing links.
    if not text:
        from html.parser import HTMLParser
        class Text(HTMLParser):
            def __init__(self):
                super().__init__(); self.parts = []; self.hidden = 0
            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style'): self.hidden += 1
                if tag == 'a' and not self.hidden:
                    href = dict(attrs).get('href', '').strip()
                    from urllib.parse import urlsplit
                    try:
                        safe = urlsplit(href).scheme.lower() in ('https', 'http', 'mailto')
                    except ValueError:
                        safe = False
                    if safe: self.parts.append(' [' + href + '] ')
            def handle_endtag(self, tag):
                if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
                if tag in ('p', 'div', 'br'): self.parts.append('\n')
            def handle_data(self, data):
                if not self.hidden: self.parts.append(data)
        for part in msg.walk():
            if part.get_content_type() == 'text/html' and not part.get_filename():
                parser = Text(); parser.feed(str(part.get_content())); text.append(''.join(parser.parts))
    body = '\n'.join(text)
    return {'subject': str(msg.get('Subject', '')), 'sender': str(msg.get('From', '')),
            'to': str(msg.get('To', '')), 'cc': str(msg.get('Cc', '')),
            'reply_to': str(msg.get('Reply-To', msg.get('From', ''))),
            'date': str(msg.get('Date', '')), 'message_id': str(msg.get('Message-ID', '')),
            'body': body[:maximum] if maximum else body, 'body_truncated': bool(maximum and len(body) > maximum),
            'html': '\n'.join(html)[:500000] or None,
            'attachments': attachments}


def receive(event, context):
    table, s3, _ = clients()
    for record in event['Records']:
        notification = json.loads(record['Sns']['Message'])
        receipt = notification['receipt']
        action = receipt['action']
        if action.get('bucketName') != os.environ['BUCKET']:
            raise ValueError('Unexpected receiving bucket')
        key = action['objectKey']
        if not key.startswith('incoming/'):
            raise ValueError('Unexpected object prefix')
        # SES creates this control notification when validating an S3 receipt
        # action. The Lambda invoke policy admits only our receiving SNS topic.
        # Match its reserved marker and complete transport shape, never MIME
        # headers or a sender-controlled recipient, before skipping storage.
        if (notification.get('notificationType') == 'Received'
                and notification.get('mail', {}).get('messageId') == 'AMAZON_SES_SETUP_NOTIFICATION'
                and notification['mail'].get('destination') == ['recipient@example.com']
                and receipt.get('recipients') == ['recipient@example.com']
                and key == 'incoming/AMAZON_SES_SETUP_NOTIFICATION'
                and action.get('type') == 'S3' and action.get('objectKeyPrefix') == 'incoming/'
                and record.get('EventSource') == 'aws:sns'
                and record['Sns'].get('Type') == 'Notification'
                and re.fullmatch(r'arn:[a-z0-9-]+:sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_.-]+', record['Sns'].get('TopicArn', ''))
                and action.get('topicArn') == record['Sns']['TopicArn']):
            continue
        obj = s3.get_object(Bucket=os.environ['BUCKET'], Key=key)
        if obj['ContentLength'] > MAX_RAW:
            raise ValueError('Incoming message exceeds processing bound')
        raw = obj['Body'].read(MAX_RAW + 1)
        parsed = parse(raw)
        mid = hashlib.sha256(key.encode()).hexdigest()
        verdicts = {kind: receipt.get(kind, {}).get('status', 'UNKNOWN')
                    for kind in ('spamVerdict', 'virusVerdict', 'spfVerdict', 'dkimVerdict', 'dmarcVerdict')}
        unsafe = verdicts['virusVerdict'] != 'PASS'
        junk = unsafe or verdicts['spamVerdict'] != 'PASS'
        routes = mailboxes.routes()
        recipients = sorted({recipient.lower() for recipient in receipt['recipients']})
        # Exact envelope aliases win, then SES plus labels on an approved base.
        # MIME To/Cc never choose a mailbox; retain the labelled envelope below.
        # A mismatched SES rule must surface for retry/DLQ reconciliation.
        resolved = {recipient: mailboxes.resolve_recipient(recipient, routes) for recipient in recipients}
        if any(name is None for name in resolved.values()):
            raise ValueError('Receipt contains an unconfigured recipient')
        for name in sorted(set(resolved.values())):
            mailbox = meta(table, name)
            if mailbox['state'] == 'Disabled':
                continue
            # A persistent receipt claim pins retries to the original generation.
            generation = mailbox['generation']
            received = datetime.fromisoformat(notification['mail']['timestamp'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            if received < mailbox.get('generation_started', ''):
                history = table.query(KeyConditionExpression=Key('pk').eq(name) & Key('sk').between('GEN#', 'GEN#' + received),
                                      ScanIndexForward=False, Limit=1, ConsistentRead=True)['Items']
                # Delayed mail from before an assignment cannot enter its new inbox.
                generation = history[0]['generation'] if history else 'unassigned-archive'
            claim = {'pk': 'RECEIPT#' + name, 'sk': mid, 'generation': generation}
            try:
                table.put_item(Item=claim, ConditionExpression='attribute_not_exists(pk)')
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
                claim = table.get_item(Key={'pk': claim['pk'], 'sk': mid}, ConsistentRead=True)['Item']
            pk = name + '#' + claim['generation']
            row = {'pk': pk, 'sk': 'MSG#' + mid, 'id': mid, 'key': key,
                   'folder': 'Junk' if junk else 'Inbox', 'seen': False,
                   'received': received, 'quarantined': unsafe,
                   'recipients': [recipient for recipient in recipients if resolved[recipient] == name],
                   'verdicts': verdicts, 'subject': parsed['subject'][:1000],
                   'sender': parsed['sender'][:1000], 'date': parsed['date'][:100],
                   'search': search_text(parsed['subject'] + '\n' + parsed['sender'] + '\n' +
                                         parsed['to'] + '\n' + parsed['body'])}
            try:
                table.put_item(Item=row, ConditionExpression='attribute_not_exists(pk)')
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
    return {'ok': True}


def public(row):
    return {k: v for k, v in row.items() if k not in ('pk', 'sk', 'key', 'search', 'payload_hash')}


def read_raw(s3, row):
    obj = s3.get_object(Bucket=os.environ['BUCKET'], Key=row['key'])
    if obj['ContentLength'] > MAX_RAW: raise ValueError('Message exceeds size limit')
    return obj['Body'].read(MAX_RAW + 1)


def validate_draft(data):
    result = {}
    for key, maximum in (('to', 2000), ('cc', 2000), ('bcc', 2000), ('subject', 1000), ('body', 200000)):
        value = data.get(key, '')
        if not isinstance(value, str) or len(value) > maximum or ('\n' in value or '\r' in value) and key != 'body':
            raise ValueError('Invalid ' + key)
        result[key] = value
    for field in ('to', 'cc', 'bcc'):
        if result[field] and any(not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+', a)
                                 for _, a in getaddresses([result[field]])):
            raise ValueError('Invalid recipients')
    attachments = data.get('attachments', [])
    if not isinstance(attachments, list) or len(attachments) > 10: raise ValueError('Too many attachments')
    total = 0
    result['attachments'] = []
    for attachment in attachments:
        name = attachment.get('name', '')
        if not isinstance(name, str) or not name or len(name) > 200 or any(c in name for c in '\r\n/\\\x00'):
            raise ValueError('Invalid attachment name')
        content = base64.b64decode(attachment['data'], validate=True)
        total += len(content)
        if total > MAX_ATTACHMENT: raise ValueError('Attachments exceed 3 MB total')
        result['attachments'].append({'name': name, 'data': attachment['data']})
    return result


def send(table, s3, ses, item, subject, data, *, human=False):
    enabled = 'HUMAN_SENDING_ENABLED' if human else 'SENDING_ENABLED'
    if os.environ.get(enabled) != 'true': raise Denied('Outbound mail is not enabled yet')
    if human and mailbox_address(item['pk']) not in os.environ.get('HUMAN_SENDER_ADDRESSES', '').split(','):
        raise Denied('Sender has not been enabled for manual replies')
    if not human and item['pk'] not in NAMES:
        raise Denied('Migrated mailboxes have no enabled outbound route')
    request = data.get('request_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request): raise ValueError('A unique request ID is required')
    draft = validate_draft(data)
    if human:
        for field, maximum in (('body_html', 500000), ('in_reply_to', 2000), ('references', 10000)):
            value = data.get(field) or ''
            if not isinstance(value, str) or len(value) > maximum or (field != 'body_html' and any(c in value for c in '\r\n')):
                raise ValueError('Invalid manual message field')
            draft[field] = value
    recipients = [addr for _, addr in getaddresses([draft[k] for k in ('to', 'cc', 'bcc')]) if addr]
    if not recipients or len(recipients) > 20: raise ValueError('Specify 1 to 20 recipients')
    digest_payload = {**draft, 'reply_id': data.get('reply_id')}
    if human: digest_payload['human'] = True
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode()).hexdigest()
    pk = partition(item)
    key = {'pk': pk, 'sk': 'SEND#' + request}
    # Claim before SES. No retry of an uncertain SES outcome can double-send.
    try:
        table.put_item(Item={**key, 'status': 'uncertain', 'payload_hash': digest,
                             'created': int(time.time())}, ConditionExpression='attribute_not_exists(pk)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
        existing = table.get_item(Key=key, ConsistentRead=True)['Item']
        if existing['payload_hash'] != digest: raise ValueError('Request ID already used for different content')
        return public(existing)
    msg = EmailMessage()
    msg['From'] = mailbox_address(item['pk'])
    for name in ('to', 'cc', 'subject'):
        if draft[name]: msg[name.title()] = draft[name]
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid(domain=os.environ['DOMAIN'])
    reply = data.get('reply_id')
    if reply:
        parent = message(table, item, reply)
        original = parse(read_raw(s3, parent))
        if original['message_id']:
            msg['In-Reply-To'] = original['message_id']; msg['References'] = original['message_id']
    if human:
        from manual_body import set_message_body
        set_message_body(msg, draft['body'], draft['body_html'])
    else:
        msg.set_content(draft['body'])
    if human:
        if not reply and draft['in_reply_to']: msg['In-Reply-To'] = draft['in_reply_to']
        if not reply and draft['references']: msg['References'] = draft['references']
    for attachment in draft['attachments']:
        msg.add_attachment(base64.b64decode(attachment['data']), maintype='application',
                           subtype='octet-stream', filename=attachment['name'])
    raw = msg.as_bytes()
    object_key = 'sent/' + pk + '/' + request
    s3.put_object(Bucket=os.environ['BUCKET'], Key=object_key, Body=raw, ContentType='message/rfc822')
    row = {'pk': pk, 'sk': 'MSG#' + request, 'id': request, 'key': object_key,
           'folder': 'Sent', 'seen': True, 'subject': draft['subject'], 'sender': str(msg['From']),
           'date': str(msg['Date']), 'received': timestamp(), 'status': 'uncertain',
           'search': search_text(draft['to'] + '\n' + draft['subject'] + '\n' + draft['body'])}
    table.put_item(Item=row)
    # Recheck after uploads; don't send following a revoked assignment.
    current = authorize(table, item['pk'], subject)
    if partition(current) != pk: raise Denied('Assignment changed; send cancelled')
    try:
        options = {}
        if os.environ.get('CONFIGURATION_SET'):
            options = {'ConfigurationSetName': os.environ['CONFIGURATION_SET'], 'EmailTags': [
                {'Name': 'mailbox', 'Value': item['pk']}, {'Name': 'generation', 'Value': item['generation']},
                {'Name': 'request', 'Value': request}]}
        response = ses.send_email(FromEmailAddress=str(msg['From']),
                                  Destination={'ToAddresses': recipients},
                                  Content={'Raw': {'Data': raw}}, **options)
    except ClientError as exc:
        code = exc.response['Error']['Code']
        # Only explicit SES rejection is a definitive failed send. Everything else
        # remains uncertain until reconciled; never repeat automatically.
        if code in ('MessageRejected', 'MailFromDomainNotVerifiedException', 'AccountSuspendedException',
                    'BadRequestException', 'SendingPausedException'):
            table.update_item(Key=key, UpdateExpression='SET #s=:s',
                              ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':s': 'failed'})
            table.update_item(Key={'pk': pk, 'sk': row['sk']}, UpdateExpression='SET #s=:s',
                              ExpressionAttributeNames={'#s': 'status'}, ExpressionAttributeValues={':s': 'failed'})
        raise
    for update_key in (key, {'pk': pk, 'sk': row['sk']}):
        try:
            table.update_item(Key=update_key, UpdateExpression='SET #s=:s, provider_id=:p',
                              ConditionExpression='#s=:uncertain', ExpressionAttributeNames={'#s': 'status'},
                              ExpressionAttributeValues={':s': 'accepted', ':p': response['MessageId'], ':uncertain': 'uncertain'})
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
    return {'status': 'accepted', 'id': request, 'provider_id': response['MessageId']}


def delivery(event, context):
    table, _, _ = clients()
    states = {'Send': 'accepted', 'Delivery': 'delivered', 'Bounce': 'bounced',
              'Complaint': 'complained', 'Reject': 'rejected', 'DeliveryDelay': 'delayed'}
    for record in event['Records']:
        notice = json.loads(record['Sns']['Message'])
        state = states.get(notice.get('eventType'))
        if not state: continue
        tags = notice['mail']['tags']
        name, generation, request = (tags[k][0] for k in ('mailbox', 'generation', 'request'))
        if name not in mailbox_names() or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', generation + request):
            raise ValueError('Unexpected delivery tags')
        pk = name + '#' + generation
        for sk in ('SEND#' + request, 'MSG#' + request):
            # Delayed Send notifications cannot downgrade delivery/bounce evidence.
            condition = 'attribute_exists(pk)'
            values = {':s': state, ':p': notice['mail']['messageId']}
            if state in ('accepted', 'delayed'):
                condition += ' AND (#s=:u OR #s=:a)'
                values.update({':u': 'uncertain', ':a': 'accepted'})
            try:
                table.update_item(Key={'pk': pk, 'sk': sk}, UpdateExpression='SET #s=:s, provider_id=:p',
                                  ConditionExpression=condition, ExpressionAttributeNames={'#s': 'status'},
                                  ExpressionAttributeValues=values)
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
    return {'ok': True}


def dispatch(table, s3, ses, subject, method, path, data, query):
    if path.startswith('/ai/'):
        if os.environ.get('MCP_ENABLED') != 'true': raise Denied('AI connections are not enabled')
        from ai_access import owner_dispatch
        return owner_dispatch(table, s3, subject, method, path, data)
    if method == 'GET' and path == '/mailboxes':
        items = []
        for name in mailbox_names():
            try:
                item = authorize(table, name, subject)
                items.append({'name': name, 'address': mailbox_address(name), 'state': item['state'],
                              'folders': mailbox_folders(item)})
            except Denied: pass
        return {'mailboxes': items}
    parts = path.strip('/').split('/')
    if len(parts) < 2 or parts[0] != 'mailboxes': raise ValueError('Unknown endpoint')
    item = authorize(table, parts[1], subject)
    pk = partition(item)
    tail = parts[2:]
    if tail == ['folders'] and method == 'GET':
        return {'folders': mailbox_folders(item)}
    if len(tail) == 2 and tail[0] == 'sends' and method == 'GET':
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', tail[1]): raise ValueError('Invalid send ID')
        row = table.get_item(Key={'pk': pk, 'sk': 'SEND#' + tail[1]}, ConsistentRead=True).get('Item')
        return public(row) if row else {'status': 'not_started'}
    if tail == ['messages'] and method == 'GET': return list_messages(table, s3, pk, query)
    if tail == ['send'] and method == 'POST': return send(table, s3, ses, item, subject, data)
    if tail == ['drafts'] and method == 'POST':
        draft = validate_draft(data)
        mid = data.get('id') or uuid.uuid4().hex
        if not re.fullmatch('[a-f0-9]{32}', mid): raise ValueError('Invalid draft ID')
        revision = uuid.uuid4().hex
        object_key = 'drafts/' + pk + '/' + mid + '/' + revision
        s3.put_object(Bucket=os.environ['BUCKET'], Key=object_key, Body=json.dumps(draft).encode(), ContentType='application/json')
        old_revision = data.get('revision')
        condition = 'attribute_not_exists(pk)'
        conditions = {}
        if old_revision:
            condition = '#kind=:kind AND revision=:revision'
            conditions = {'ExpressionAttributeNames': {'#kind': 'kind'},
                          'ExpressionAttributeValues': {':kind': 'draft', ':revision': old_revision}}
        try:
            table.put_item(Item={'pk': pk, 'sk': 'MSG#' + mid, 'id': mid, 'key': object_key, 'revision': revision,
                             'kind': 'draft', 'folder': 'Drafts', 'seen': True, 'subject': draft['subject'],
                             'sender': draft['to'], 'date': formatdate(), 'received': timestamp(),
                             'search': search_text(draft['to'] + '\n' + draft['subject'] + '\n' + draft['body'])},
                           ConditionExpression=condition, **conditions)
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
            raise ValueError('Draft changed in another window; reload it before saving') from None
        return {'id': mid, 'revision': revision}
    if len(tail) >= 2 and tail[0] == 'messages':
        row = message(table, item, tail[1])
        if len(tail) == 2 and method == 'PATCH':
            folder = data.get('folder', row['folder'])
            seen = data.get('seen', row['seen'])
            if folder not in mailbox_folders(item) or not isinstance(seen, bool): raise ValueError('Invalid message update')
            table.update_item(Key={'pk': pk, 'sk': row['sk']}, UpdateExpression='SET folder=:f, seen=:s',
                              ExpressionAttributeValues={':f': folder, ':s': seen})
            return {'ok': True}
        if method == 'GET': return read_message(s3, row, tail, query)
    raise ValueError('Unknown endpoint')


def indexed_search(table, s3, pk, query, unread=False, untriaged=False):
    """Surface indexed matches before bounded full-text fallback; never scan S3 in bulk."""
    folder = query.get('folder', 'Inbox')
    needle = query['q'].casefold()
    scope = hashlib.sha256(json.dumps([pk, folder, needle, unread, untriaged]).encode()).hexdigest()
    phase, after = 'index', None
    cursor = query.get('cursor')
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048: raise ValueError()
            decoded = json.loads(base64.urlsafe_b64decode(cursor))
            if decoded['v'] != 1 or decoded['scope'] != scope or decoded['phase'] not in ('index', 'body'):
                raise ValueError()
            phase, after = decoded['phase'], decoded['after']
            if after is not None:
                if (set(after) != {'sk', 'received'} or not isinstance(after['received'], str)
                        or len(after['received']) > 40 or not re.fullmatch(r'MSG#[a-zA-Z0-9_-]{1,100}', after['sk'])):
                    raise ValueError()
        except Exception:
            raise ValueError('Search cursor changed or expired; start the search again') from None
    args = {'KeyConditionExpression': Key('pk').eq(pk), 'IndexName': 'Chronological',
            'ScanIndexForward': False, 'Limit': min(int(query.get('limit', '100')), 100 if phase == 'index' else 10)}
    if after: args['ExclusiveStartKey'] = {'pk': pk, **after}
    page = table.query(**args)
    rows = []
    for row in page['Items']:
        if folder != 'All' and row['folder'] != folder: continue
        if unread and row.get('seen'): continue
        if untriaged and row.get('triage'): continue
        indexed_match = needle in row.get('search', '')
        if phase == 'index':
            if indexed_match: rows.append(public(row))
        elif not indexed_match and not row.get('quarantined'):
            raw = read_raw(s3, row)
            content = json.loads(raw) if row.get('kind') == 'draft' else parse(raw, maximum=None)
            if needle in '\n'.join(str(content.get(k, '')) for k in ('subject', 'sender', 'to', 'cc', 'body')).casefold():
                rows.append(public(row))
    next_key = page.get('LastEvaluatedKey')
    next_phase = phase
    if not next_key and phase == 'index' and (after or page['Items']):
        next_phase = 'body'
    more = bool(next_key) or next_phase != phase
    next_cursor = None
    if more:
        position = {field: next_key[field] for field in ('sk', 'received')} if next_key else None
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {'v': 1, 'scope': scope, 'phase': next_phase, 'after': position}).encode()).decode()
    return {'messages': rows, 'cursor': next_cursor,
            'search_progress': {'phase': phase, 'scanned': len(page['Items']), 'next_phase': next_phase if more else None}}


def list_messages(table, s3, pk, query, unread=False, untriaged=False):
    folder = query.get('folder', 'Inbox')
    if not mailboxes.folder(folder): raise ValueError('Invalid folder')
    requested = query.get('limit', '10')
    if not isinstance(requested, str) or not re.fullmatch(r'[1-9][0-9]{0,2}', requested) or int(requested) > 100:
        raise ValueError('Page size must be between 1 and 100')
    if query.get('search_mode'):
        if (query['search_mode'] != 'indexed' or not isinstance(query.get('q'), str)
                or not 0 < len(query['q']) <= 1000):
            raise ValueError('Invalid search mode')
        return indexed_search(table, s3, pk, query, unread, untriaged)
    # Full-body search may read MIME from S3, so preserve its smaller work bound.
    page_size = min(int(requested), 10) if query.get('q') else int(requested)
    kwargs = {'KeyConditionExpression': Key('pk').eq(pk), 'IndexName': 'Chronological',
              'ScanIndexForward': False, 'Limit': page_size}
    cursor = query.get('cursor')
    if cursor:
        # Cursor cannot choose another partition or arbitrary DynamoDB key.
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor))
            if not re.fullmatch(r'MSG#[a-zA-Z0-9_-]{1,100}', decoded['sk']) or len(decoded['received']) > 40:
                raise ValueError('Invalid cursor')
            kwargs['ExclusiveStartKey'] = {'pk': pk, 'sk': decoded['sk'], 'received': decoded['received']}
        except Exception: raise ValueError('Invalid cursor') from None
    page = table.query(**kwargs)
    needle = query.get('q', '').casefold()[:1000]
    rows = []
    for row in page['Items']:
        if folder != 'All' and row['folder'] != folder: continue
        if unread and row.get('seen'): continue
        if untriaged and row.get('triage'): continue
        matches = needle in row.get('search', '')
        if needle and not matches and not row.get('quarantined'):
            # Search past the small DynamoDB preview index. Never silently
            # exclude long bodies; attachment binary contents are not searched.
            raw = read_raw(s3, row)
            content = json.loads(raw) if row.get('kind') == 'draft' else parse(raw, maximum=None)
            matches = needle in '\n'.join(str(content.get(k, '')) for k in
                                          ('subject', 'sender', 'to', 'cc', 'body')).casefold()
        if matches: rows.append(public(row))
    next_key = page.get('LastEvaluatedKey')
    next_cursor = base64.urlsafe_b64encode(json.dumps({'sk': next_key['sk'], 'received': next_key['received']}).encode()).decode() if next_key else None
    return {'messages': rows, 'cursor': next_cursor}


def read_message(s3, row, tail, query=None):
    """GET one message (tail = messages/<id>) or attachment (messages/<id>/attachments/<n>)."""
    if row.get('quarantined'): raise Denied('Message quarantined: virus scan did not pass')
    raw = read_raw(s3, row)
    if len(tail) == 2:
        if row.get('kind') == 'draft': return {**public(row), 'draft': json.loads(raw)}
        return {**public(row), **parse(raw)}
    if len(tail) == 4 and tail[2] == 'attachments' and tail[3].isdigit():
        return attachment(raw, int(tail[3]))
    if len(tail) == 4 and tail[2] == 'downloads' and tail[3].isdigit():
        # Auth is checked again for every chunk. No public/signed file URL and no
        # Lambda/API Gateway response larger than their buffered payload limits.
        return attachment_chunk(raw, int(tail[3]), (query or {}).get('offset', '0'))
    raise ValueError('Unknown endpoint')


def attachment(raw, wanted):
    for index, part in enumerate(message_from_bytes(raw, policy=default).walk()):
        if index == wanted and (part.get_filename() or part.get_content_disposition() == 'attachment'):
            payload = part.get_payload(decode=True) or b''
            if len(payload) > MAX_ATTACHMENT: raise ValueError('Attachment exceeds 3 MB download limit')
            return {'name': part.get_filename() or 'attachment', 'data': base64.b64encode(payload).decode()}
    raise Denied('Attachment unavailable')


def attachment_chunk(raw, wanted, offset):
    if not isinstance(offset, str) or not re.fullmatch(r'0|[1-9][0-9]{0,8}', offset):
        raise ValueError('Invalid attachment offset')
    offset = int(offset)
    for index, part in enumerate(message_from_bytes(raw, policy=default).walk()):
        if index == wanted and (part.get_filename() or part.get_content_disposition() == 'attachment'):
            payload = part.get_payload(decode=True) or b''
            if offset > len(payload): raise ValueError('Attachment offset exceeds size')
            chunk = payload[offset:offset + 1024 * 1024]
            following = offset + len(chunk)
            return {'name': part.get_filename() or 'attachment', 'data': base64.b64encode(chunk).decode(),
                    'size': len(payload), 'offset': offset, 'sha256': hashlib.sha256(payload).hexdigest(),
                    'next_offset': following if following < len(payload) else None}
    raise Denied('Attachment unavailable')


def api(event, context):
    try:
        claims = event['requestContext']['authorizer']['jwt']['claims']
        if claims.get('token_use') != 'access' or int(claims.get('exp', 0)) <= int(time.time()):
            raise Denied('Session expired; sign in again')
        if event.get('rawPath') == '/mailboxes/operational-health':
            # Keep operational/account state out of mailbox grants and MCP tools.
            # Check the app client before even reading the owner record.
            if not os.environ.get('OWNER_CLIENT_ID') or claims.get('client_id') != os.environ['OWNER_CLIENT_ID']:
                raise Denied('Open receiving health with the owner SimpleMail session')
            if event['requestContext']['http']['method'] != 'GET':
                raise Denied('Receiving health is read-only')
            table = boto3.resource('dynamodb').Table(os.environ['TABLE'])
            owner = table.get_item(Key={'pk': 'ADMIN', 'sk': claims['sub']}, ConsistentRead=True).get('Item')
            if not owner or owner.get('enabled') is not True:
                raise Denied('Receiving health is available only to the enabled owner')
            from operational_health import snapshot
            return {'statusCode': 200, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
                    'body': json.dumps(snapshot())}
        if event.get('rawPath', '').startswith('/ai/') and claims.get('client_id') != os.environ.get('OWNER_CLIENT_ID'):
            raise Denied('Open AI settings with the owner SimpleMail session')
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'): raw = base64.b64decode(raw).decode()
        if len(raw) > 4500000: raise ValueError('Request exceeds size limit')
        result = dispatch(*clients(), claims['sub'], event['requestContext']['http']['method'],
                          event['rawPath'], json.loads(raw), event.get('queryStringParameters') or {})
        status = 200
    except Denied as exc:
        status, result = 403, {'error': str(exc)}
    except (ValueError, KeyError, TypeError) as exc:
        status, result = 400, {'error': 'Invalid request: ' + str(exc)[:100]}
    except Exception:
        # Never log headers, tokens, email content, or raw provider exception payloads.
        status, result = 503, {'error': 'Service unavailable. Check send status before retrying a send.'}
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'body': json.dumps(result, default=lambda v: int(v))}
