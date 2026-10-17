"""Robot access to the reserved mailboxes, for headless owner tools such as triage.

A separate API, authorizer and Lambda role serve these routes. The role cannot
use SES, cannot delete, may write only mail rows (never mailbox assignments or
owner records) and may upload only drafts. So whatever the code does, the robot
can never send. Scope mail-reader/read reads. Scope mail-reader/triage also lets
it move mail between Inbox and Junk, tag a message with a triage result, and
save new drafts for the owner to review. It never changes read state.

The robot sees Reserved (unassigned) mailboxes plus Assigned ones the owner
names in READER_MAILBOXES; never Disabled ones.
"""
import base64
import hashlib
import json
import os
import re
import time

import boto3
from botocore.exceptions import ClientError

import service
from service import Denied, NAMES

READ, TRIAGE = 'mail-reader/read', 'mail-reader/triage'
CATEGORIES = ('important', 'scam', 'needs_fix', 'reply', 'fyi')
MOVABLE = ('Inbox', 'Junk')


def clients():
    return boto3.resource('dynamodb').Table(os.environ['TABLE']), boto3.client('s3')


def named():
    return {name.strip() for name in os.environ.get('READER_MAILBOXES', '').split(',')} & set(NAMES)


def visible(table, name):
    item = service.meta(table, name)
    # Re-read on every request: assigning an employee removes their inbox at once.
    if item['state'] == 'Reserved' or (item['state'] == 'Assigned' and name in named()):
        return item
    raise Denied('Mailbox is not available to the reader')


def text(data, key, maximum, required=True):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError('Invalid ' + key)
    return value


def move(table, row, data):
    folder = data.get('folder')
    if folder not in MOVABLE: raise ValueError('The robot can only move mail between Inbox and Junk')
    if row.get('kind') == 'draft' or row['folder'] not in MOVABLE:
        raise Denied('The robot can only move incoming mail')
    text(data, 'reason', 500)
    table.update_item(Key={'pk': row['pk'], 'sk': row['sk']}, UpdateExpression='SET folder=:f, moved_by=:m',
                      ConditionExpression='folder IN (:inbox, :junk)',
                      ExpressionAttributeValues={':f': folder, ':m': {'by': 'robot', 'from': row['folder'], 'reason': data['reason'], 'at': service.timestamp()},
                                                 ':inbox': 'Inbox', ':junk': 'Junk'})
    return {'ok': True, 'folder': folder}


def tag(table, row, data):
    category = data.get('category')
    if category not in CATEGORIES: raise ValueError('category must be one of ' + ', '.join(CATEGORIES))
    triage = {'category': category, 'note': text(data, 'note', 500), 'at': service.timestamp()}
    table.update_item(Key={'pk': row['pk'], 'sk': row['sk']}, UpdateExpression='SET triage=:t',
                      ConditionExpression='attribute_exists(pk)', ExpressionAttributeValues={':t': triage})
    return {'ok': True, 'triage': triage}


def draft(table, s3, item, data):
    """Create a new review draft. Never edits an existing draft, never sends."""
    request = data.get('request_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request): raise ValueError('A unique request_id is required')
    reason = text(data, 'reason', 1000)
    if data.get('bcc') or data.get('attachments'): raise ValueError('Robot drafts take no BCC or attachments')
    content = service.validate_draft({key: data.get(key, '') for key in ('to', 'cc', 'subject', 'body')})
    if not content['to'].strip(): raise ValueError('A draft needs a recipient')
    pk = service.partition(item)
    reply = data.get('reply_to_id')
    if reply:
        if service.message(table, item, reply).get('kind') == 'draft': raise ValueError('Cannot reply to a draft')
    # The same request always names the same draft, so a retry cannot duplicate it.
    mid = hashlib.sha256((pk + '#' + request).encode()).hexdigest()[:32]
    existing = table.get_item(Key={'pk': pk, 'sk': 'MSG#' + mid}, ConsistentRead=True).get('Item')
    if existing: return {'id': mid, 'revision': existing['revision'], 'existing': True}
    revision = hashlib.sha256(mid.encode()).hexdigest()[:32]
    object_key = 'drafts/' + pk + '/' + mid + '/' + revision
    s3.put_object(Bucket=os.environ['BUCKET'], Key=object_key, Body=json.dumps(content).encode(), ContentType='application/json')
    row = {'pk': pk, 'sk': 'MSG#' + mid, 'id': mid, 'key': object_key, 'revision': revision,
           'kind': 'draft', 'folder': 'Drafts', 'seen': True, 'subject': content['subject'],
           'sender': content['to'], 'date': service.formatdate(), 'received': service.timestamp(),
           'author': 'robot', 'reason': reason,
           'search': service.search_text(content['to'] + '\n' + content['subject'] + '\n' + content['body'])}
    if reply: row['reply_to_id'] = reply
    try:
        table.put_item(Item=row, ConditionExpression='attribute_not_exists(pk)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
    return {'id': mid, 'revision': revision}


def dispatch(table, s3, method, path, query, data=None, scopes=(READ,)):
    data = data or {}
    if method not in ('GET', 'POST'):
        raise Denied('Not available to the robot')
    if method == 'POST' and TRIAGE not in scopes:
        raise Denied('The reader is read-only')
    parts = path.strip('/').split('/')
    if parts[:2] != ['reader', 'mailboxes']:
        raise ValueError('Unknown endpoint')
    if len(parts) == 2 and method == 'GET':
        items = []
        for name in NAMES:
            try:
                item = visible(table, name)
            except Denied:
                continue
            items.append({'name': name, 'address': name + '@' + os.environ['DOMAIN'], 'state': item['state']})
        return {'mailboxes': items}
    if len(parts) < 4: raise ValueError('Unknown endpoint')
    item = visible(table, parts[2])
    tail = parts[3:]
    if method == 'GET':
        if tail == ['messages']:
            return service.list_messages(table, s3, service.partition(item), query,
                                         unread=query.get('unread') == '1', untriaged=query.get('untriaged') == '1')
        if len(tail) in (2, 4) and tail[0] == 'messages':
            return service.read_message(s3, service.message(table, item, tail[1]), tail)
        raise ValueError('Unknown endpoint')
    if tail == ['drafts']:
        return draft(table, s3, item, data)
    if len(tail) == 3 and tail[0] == 'messages' and tail[2] in ('move', 'triage'):
        row = service.message(table, item, tail[1])
        return move(table, row, data) if tail[2] == 'move' else tag(table, row, data)
    raise ValueError('Unknown endpoint')


def api(event, context):
    path = event.get('rawPath', '')
    method = event.get('requestContext', {}).get('http', {}).get('method', '')
    try:
        claims = event['requestContext']['authorizer']['jwt']['claims']
        scopes = str(claims.get('scope', '')).split()
        # Only the reader app client's machine token; never a person's session.
        if (claims.get('token_use') != 'access' or claims.get('client_id') != os.environ['READER_CLIENT_ID']
                or READ not in scopes or int(claims.get('exp', 0)) <= int(time.time())):
            raise Denied('Reader token required')
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'): raw = base64.b64decode(raw).decode()
        if len(raw) > 300000: raise ValueError('Request exceeds size limit')
        data = json.loads(raw)
        if not isinstance(data, dict): raise ValueError('Body must be an object')
        result = dispatch(*clients(), method, path, event.get('queryStringParameters') or {}, data, scopes)
        status = 200
    except Denied as exc:
        status, result = 403, {'error': str(exc)}
    except (ValueError, KeyError, TypeError) as exc:
        status, result = 400, {'error': 'Invalid request: ' + str(exc)[:100]}
    except Exception:
        status, result = 503, {'error': 'Service unavailable'}
    # Audit line: action and mailbox only. Never message IDs, queries, recipients or content.
    parts = path.strip('/').split('/')
    mailbox = parts[2] if len(parts) > 2 and re.fullmatch('[a-z]{1,10}', parts[2]) else '-'
    if method == 'POST':
        kind = parts[-1] if parts[-1] in ('drafts', 'move', 'triage') else 'post'
    else:
        kind = 'attachment' if 'attachments' in parts else 'message' if len(parts) == 5 else 'list'
    print(json.dumps({'reader': kind, 'mailbox': mailbox, 'status': status}))
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'body': json.dumps(result, default=lambda v: int(v))}
