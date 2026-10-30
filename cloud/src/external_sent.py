"""Owner-only storage of an already submitted Resend message. Never sends mail."""
import base64
import hashlib
import os
import re
from email import message_from_bytes
from email.policy import default
from email.utils import getaddresses

from botocore.exceptions import ClientError
import service


def save(table, s3, item, subject, data):
    if (item['pk'] != 'mail-playloudr' or
            service.mailbox_address(item['pk']) != 'playloudr@extraliferecords.com' or
            data.get('generation') != item['generation']):
        raise service.Denied('Storage assignment differs')
    request = data.get('request_id', '')
    if not isinstance(request, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request):
        raise ValueError('Invalid storage request ID')
    encoded = data.get('raw', '')
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4000000:
        raise ValueError('Sent copy exceeds request bound')
    raw = base64.b64decode(encoded, validate=True)
    msg = message_from_bytes(raw, policy=default)
    if (len(msg.get_all('From', [])) != 1 or
            [address for _, address in getaddresses(msg.get_all('From', []))] != ['hello@playloudr.com'] or
            len(msg.get_all('Message-ID', [])) != 1 or not str(msg['Message-ID']).strip() or
            msg.get_all('Bcc') or msg.get_all('Resent-From')):
        raise ValueError('Unexpected Sent identity or headers')
    digest = hashlib.sha256(raw).hexdigest()
    pk = service.partition(item)
    claim = {'pk': pk, 'sk': 'EXTSENT#' + request}
    try:
        table.put_item(Item={**claim, 'sha256': digest}, ConditionExpression='attribute_not_exists(pk)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        old = table.get_item(Key=claim, ConsistentRead=True).get('Item', {})
        if old.get('sha256') != digest:
            raise ValueError('Storage request already used for different content')
    current = service.authorize(table, item['pk'], subject)
    if service.partition(current) != pk:
        raise service.Denied('Storage assignment changed')
    mid = 'ext-' + request
    key = 'sent/' + pk + '/' + mid
    # All retries for this key have the same digest. Never overwrite a mail row:
    # a later retry must preserve owner moves, flags, and deletions.
    s3.put_object(Bucket=os.environ['BUCKET'], Key=key, Body=raw, ContentType='message/rfc822')
    parsed = service.parse(raw)
    row = {'pk': pk, 'sk': 'MSG#' + mid, 'id': mid, 'key': key,
           'folder': 'Sent', 'seen': True, 'subject': str(msg.get('Subject', '')),
           'sender': str(msg['From']), 'date': str(msg.get('Date', '')),
           'received': service.timestamp(), 'status': 'stored', 'transport': 'external',
           'sha256': digest, 'search': service.search_text(str(msg.get('To', '')) + '\n' +
                                                        str(msg.get('Subject', '')) + '\n' + parsed.get('body', ''))}
    try:
        table.put_item(Item=row, ConditionExpression='attribute_not_exists(pk)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        old = table.get_item(Key={'pk': pk, 'sk': row['sk']}, ConsistentRead=True).get('Item', {})
        if old.get('sha256') != digest:
            raise ValueError('Sent row conflicts with preserved content')
    return {'status': 'stored', 'id': mid, 'sha256': digest, 'generation': item['generation']}
