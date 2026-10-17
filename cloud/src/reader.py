"""Read-only robot access to the reserved mailboxes, for headless owner tools.

A separate API, authorizer and Lambda role serve these routes. The role has
DynamoDB and S3 read only and no SES, so a code defect still cannot send,
draft, move, flag or delete. The reader sees Reserved (unassigned) mailboxes
plus Assigned ones the owner names in READER_MAILBOXES; never Disabled ones.
"""
import json
import os
import re
import time

import boto3

import service
from service import Denied, NAMES

SCOPE = 'mail-reader/read'


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


def dispatch(table, s3, method, path, query):
    if method != 'GET':
        raise Denied('The reader is read-only')
    parts = path.strip('/').split('/')
    if parts[:2] != ['reader', 'mailboxes']:
        raise ValueError('Unknown endpoint')
    if len(parts) == 2:
        items = []
        for name in NAMES:
            try:
                item = visible(table, name)
            except Denied:
                continue
            items.append({'name': name, 'address': name + '@' + os.environ['DOMAIN'], 'state': item['state']})
        return {'mailboxes': items}
    item = visible(table, parts[2])
    tail = parts[3:]
    if tail == ['messages']:
        return service.list_messages(table, s3, service.partition(item), query, unread=query.get('unread') == '1')
    if len(tail) in (2, 4) and tail[0] == 'messages':
        return service.read_message(s3, service.message(table, item, tail[1]), tail)
    raise ValueError('Unknown endpoint')


def api(event, context):
    path = event.get('rawPath', '')
    try:
        claims = event['requestContext']['authorizer']['jwt']['claims']
        # Only the reader app client's machine token; never a person's session.
        if (claims.get('token_use') != 'access' or claims.get('client_id') != os.environ['READER_CLIENT_ID']
                or SCOPE not in str(claims.get('scope', '')).split()
                or int(claims.get('exp', 0)) <= int(time.time())):
            raise Denied('Reader token required')
        result = dispatch(*clients(), event['requestContext']['http']['method'], path,
                          event.get('queryStringParameters') or {})
        status = 200
    except Denied as exc:
        status, result = 403, {'error': str(exc)}
    except (ValueError, KeyError, TypeError) as exc:
        status, result = 400, {'error': 'Invalid request: ' + str(exc)[:100]}
    except Exception:
        status, result = 503, {'error': 'Service unavailable'}
    # Audit line: mailbox and endpoint kind only. Never message IDs, queries or content.
    parts = path.strip('/').split('/')
    mailbox = parts[2] if len(parts) > 2 and re.fullmatch('[a-z]{1,10}', parts[2]) else '-'
    kind = 'attachment' if 'attachments' in parts else 'message' if len(parts) == 5 else 'list'
    print(json.dumps({'reader': kind, 'mailbox': mailbox, 'status': status}))
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'body': json.dumps(result, default=lambda v: int(v))}
