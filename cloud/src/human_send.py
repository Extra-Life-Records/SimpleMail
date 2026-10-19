"""Separate owner-session-only manual route; no MCP grants or SMTP credentials."""
import base64
import json
import os
import re
import time

import boto3
import service


def dispatch(table, s3, ses, claims, path, data):
    if (claims.get('token_use') != 'access' or claims.get('client_id') != os.environ.get('OWNER_CLIENT_ID') or
            not os.environ.get('OWNER_CLIENT_ID') or int(claims.get('exp', 0)) <= time.time() or
            'openid' not in claims.get('scope', '').split()):
        raise service.Denied('Use the owner SimpleMail sign-in for manual mail')
    if os.environ.get('HUMAN_SENDING_ENABLED') != 'true':
        raise service.Denied('Manual replacement sending remains disabled')
    match = re.fullmatch(r'/human/mailboxes/([a-z0-9-]+)/send', path)
    if not match: raise ValueError('Unknown manual mail endpoint')
    subject = claims['sub']
    admin = table.get_item(Key={'pk': 'ADMIN', 'sk': subject}, ConsistentRead=True).get('Item')
    if not admin or not admin.get('enabled'): raise service.Denied('Owner access required')
    item = service.authorize(table, match[1], subject)
    return service.send(table, s3, ses, item, subject, data, human=True)


def api(event, context):
    try:
        if event.get('requestContext', {}).get('http', {}).get('method') != 'POST':
            raise ValueError('Use POST for manual sending')
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'): raw = base64.b64decode(raw).decode()
        if len(raw) > 4500000: raise ValueError('Manual message exceeds request bound')
        table = boto3.resource('dynamodb').Table(os.environ['TABLE'])
        result = dispatch(table, boto3.client('s3'), boto3.client('sesv2', region_name='eu-west-2'),
                          event['requestContext']['authorizer']['jwt']['claims'], event['rawPath'], json.loads(raw))
        status = 200
    except service.Denied:
        status, result = 403, {'error': 'Manual sending is disabled or this session is not authorized'}
    except (ValueError, KeyError, TypeError):
        status, result = 400, {'error': 'Invalid manual message request'}
    except Exception:
        status, result = 503, {'error': 'Check the existing send reference before any retry'}
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
            'body': json.dumps(result, default=lambda value: int(value))}
