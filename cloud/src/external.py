"""Fasthosts (IMAP) mailboxes served through the robot reader.

The owner keeps their logins in one Secrets Manager secret (EXTERNAL_SECRET),
which only this function's role may read. Claude never sees a password: it calls
the reader API and AWS talks IMAP. The JSON shape is:

  {"mailboxes": [{"name": "elr", "address": "hello@extraliferecords.com",
                  "username": "hello@extraliferecords.com", "password": "...",
                  "host": "mail.livemail.co.uk", "port": 993}]}

The same rules as the AWS mailboxes apply. Reads use read-only selects and
BODY.PEEK, so nothing is marked read. Moves go between Inbox and Junk only, by
UID MOVE, never by flag-and-expunge. Drafts are new messages APPENDed to Drafts.
Nothing is sent or deleted. Triage results and draft claims live in the mail
table under external#<name>.
"""
import base64
import hashlib
import imaplib
import json
import os
import re
import time
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import formatdate, make_msgid

import boto3
from botocore.exceptions import ClientError

import service
from service import Denied

FOLDERS = ('Inbox', 'Junk', 'Sent', 'Drafts', 'Trash')
SPECIAL = {'Junk': '\\junk', 'Sent': '\\sent', 'Drafts': '\\drafts', 'Trash': '\\trash'}
FALLBACK = {'Junk': ('Junk', 'Spam', 'INBOX.Junk', 'INBOX.Spam', 'Junk E-mail', 'Junk Email'),
            'Sent': ('Sent', 'Sent Items', 'INBOX.Sent', 'Sent Messages'),
            'Drafts': ('Drafts', 'INBOX.Drafts'),
            'Trash': ('Trash', 'Deleted Items', 'INBOX.Trash', 'Deleted Messages')}
ALL = ('Inbox', 'Sent', 'Junk')
PAGE = 10
ID = re.compile(r'(inbox|junk|sent|drafts|trash)-(\d{1,20})-(\d{1,20})')
HEADERS = '(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])'
_cache = {'at': 0, 'value': {}}


class Unavailable(Exception):
    """IMAP login or network failure. Never carries server text, which can echo account details."""


def accounts():
    arn = os.environ.get('EXTERNAL_SECRET', '')
    if not arn:
        return {}
    if time.time() - _cache['at'] > 300:
        value = json.loads(boto3.client('secretsmanager').get_secret_value(SecretId=arn)['SecretString'])
        result = {}
        for row in value.get('mailboxes', []):
            name = row.get('name', '')
            if not re.fullmatch(r'[a-z]{2,10}', name) or name in service.NAMES or name in result:
                raise ValueError('Invalid external mailbox name in the secret')
            for key in ('address', 'username', 'password', 'host'):
                if not isinstance(row.get(key), str) or not row[key]:
                    raise ValueError(f'External mailbox {name} is missing {key}')
            result[name] = {**row, 'port': int(row.get('port', 993))}
        _cache.update(at=time.time(), value=result)
    return _cache['value']


def connect(account):
    try:
        imap = imaplib.IMAP4_SSL(account['host'], account['port'], timeout=20)
        imap.login(account['username'], account['password'])
        return imap
    except (imaplib.IMAP4.error, OSError):
        raise Unavailable() from None


def quote(name):
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def folders(imap):
    _, lines = imap.list()
    found, names = {'Inbox': 'INBOX'}, {}
    for line in lines or []:
        if isinstance(line, tuple):  # folder name sent as a literal
            line = line[0].rsplit(b'{', 1)[0] + b'"' + line[1].replace(b'"', b'\\"') + b'"'
        text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else str(line)
        match = re.match(r'\((?P<flags>[^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (?P<name>.+)$', text)
        if not match:
            continue
        name = match.group('name').strip()
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r'\\(.)', r'\1', name[1:-1])
        names[name.lower()] = name
        for key, flag in SPECIAL.items():
            if flag in match.group('flags').lower().split():
                found.setdefault(key, name)
    for key, options in FALLBACK.items():
        match = next((names[o.lower()] for o in options if o.lower() in names), None)
        if key not in found and match:
            found[key] = match
    return found


def select(imap, name, readonly=True):
    status, _ = imap.select(quote(name), readonly=readonly)
    if status != 'OK':
        raise Denied('Folder unavailable')
    return int(imap.response('UIDVALIDITY')[1][0])


def parse_fetch(data):
    """Split a UID FETCH response into {uid: (prefix text, body bytes)}."""
    rows = {}
    for entry in data or []:
        if not isinstance(entry, tuple):
            continue
        prefix = entry[0].decode('utf-8', 'replace')
        uid = re.search(r'UID (\d+)', prefix)
        if uid:
            rows[int(uid.group(1))] = (prefix, entry[1])
    return rows


def triage_key(message_id, headers):
    basis = message_id or '\n'.join(str(headers.get(k, '')) for k in ('From', 'Date', 'Subject'))
    return hashlib.sha256(basis.strip().encode()).hexdigest()[:32]


def summary(name, folder, validity, uid, prefix, raw_headers, table):
    headers = message_from_bytes(raw_headers or b'', policy=default)
    flags = re.search(r'FLAGS \(([^)]*)\)', prefix)
    seen = bool(flags and '\\seen' in flags.group(1).lower())
    key = triage_key(str(headers.get('Message-ID', '')), headers)
    row = {'id': f'{folder.lower()}-{validity}-{uid}', 'folder': folder, 'subject': str(headers.get('Subject', '')) or '(no subject)',
           'sender': str(headers.get('From', '')), 'to': str(headers.get('To', '')), 'date': str(headers.get('Date', '')),
           'seen': seen, 'provider': 'imap'}
    triage = table.get_item(Key={'pk': 'external#' + name, 'sk': 'TRIAGE#' + key}).get('Item')
    if triage:
        row['triage'] = triage['triage']
    return row


def list_messages(table, imap, name, query, untriaged=False):
    folder = query.get('folder', 'Inbox')
    if folder not in FOLDERS and folder != 'All':
        raise ValueError('Invalid folder')
    order = ALL if folder == 'All' else (folder,)
    position, before = 0, None
    if query.get('cursor'):
        try:
            cursor = json.loads(base64.urlsafe_b64decode(query['cursor']))
            position, before = int(cursor['p']), int(cursor['b'])
            if not 0 <= position < len(order): raise ValueError
        except Exception:
            raise ValueError('Invalid cursor') from None
    names = folders(imap)
    current = order[position]
    if current not in names:
        return {'messages': [], 'cursor': next_cursor(order, position + 1, None)}
    validity = select(imap, names[current])
    criteria = ['UNSEEN'] if query.get('unread') == '1' else ['ALL']
    needle = query.get('q', '')[:200]
    if needle:
        imap.literal = needle.encode('utf-8')
        _, found = imap.uid('SEARCH', 'CHARSET', 'UTF-8', *criteria, 'TEXT')
    else:
        _, found = imap.uid('SEARCH', *criteria)
    uids = sorted((int(u) for u in (found[0] or b'').split()), reverse=True)
    if before is not None:
        uids = [u for u in uids if u < before]
    batch, rest = uids[:PAGE], uids[PAGE:]
    rows = []
    if batch:
        _, data = imap.uid('FETCH', ','.join(map(str, batch)), HEADERS)
        fetched = parse_fetch(data)
        for uid in batch:
            if uid in fetched:
                row = summary(name, current, validity, uid, *fetched[uid], table)
                if not (untriaged and row.get('triage')):
                    rows.append(row)
    if rest:
        return {'messages': rows, 'cursor': next_cursor(order, position, batch[-1])}
    return {'messages': rows, 'cursor': next_cursor(order, position + 1, None)}


def next_cursor(order, position, before):
    if position >= len(order):
        return None
    value = {'p': position, 'b': before if before is not None else 2 ** 62}
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def locate(imap, message_id, readonly=True):
    match = ID.fullmatch(message_id or '')
    if not match:
        raise ValueError('Invalid message ID')
    folder = next(f for f in FOLDERS if f.lower() == match.group(1))
    names = folders(imap)
    if folder not in names:
        raise Denied('Message unavailable')
    if select(imap, names[folder], readonly) != int(match.group(2)):
        raise Denied('Folder changed; list the mailbox again')
    return folder, int(match.group(2)), int(match.group(3)), names


def fetch_raw(imap, uid):
    _, data = imap.uid('FETCH', str(uid), '(UID RFC822.SIZE)')
    # A size-only FETCH answers with plain lines, not (prefix, literal) pairs.
    lines = [entry[0] if isinstance(entry, tuple) else entry for entry in data or [] if entry]
    line = next((l for l in lines if re.search(rb'UID %d\b' % uid, l)), None)
    if line is None:
        raise Denied('Message unavailable')
    size = re.search(rb'RFC822\.SIZE (\d+)', line)
    if size and int(size.group(1)) > service.MAX_RAW:
        raise ValueError('Message exceeds size limit')
    _, data = imap.uid('FETCH', str(uid), '(UID BODY.PEEK[])')
    body = parse_fetch(data).get(uid)
    if not body:
        raise Denied('Message unavailable')
    return body[1]


def read(table, imap, name, message_id, tail):
    folder, validity, uid, _ = locate(imap, message_id)
    raw = fetch_raw(imap, uid)
    if len(tail) == 4:
        if tail[2] != 'attachments' or not tail[3].isdigit():
            raise ValueError('Unknown endpoint')
        return service.attachment(raw, int(tail[3]))
    parsed = service.parse(raw)
    headers = message_from_bytes(raw, policy=default)
    key = triage_key(parsed['message_id'], headers)
    result = {'id': message_id, 'folder': folder, 'provider': 'imap', **parsed}
    triage = table.get_item(Key={'pk': 'external#' + name, 'sk': 'TRIAGE#' + key}).get('Item')
    if triage:
        result['triage'] = triage['triage']
    return result


def header_key(imap, uid):
    _, data = imap.uid('FETCH', str(uid), HEADERS)
    row = parse_fetch(data).get(uid)
    if not row:
        raise Denied('Message unavailable')
    headers = message_from_bytes(row[1] or b'', policy=default)
    return headers, triage_key(str(headers.get('Message-ID', '')), headers)


def tag(table, imap, name, message_id, data):
    from reader import CATEGORIES, text
    category = data.get('category')
    if category not in CATEGORIES: raise ValueError('category must be one of ' + ', '.join(CATEGORIES))
    triage = {'category': category, 'note': text(data, 'note', 500), 'at': service.timestamp()}
    _, _, uid, _ = locate(imap, message_id)
    _, key = header_key(imap, uid)
    table.put_item(Item={'pk': 'external#' + name, 'sk': 'TRIAGE#' + key, 'triage': triage})
    return {'ok': True, 'triage': triage}


def move(table, imap, name, message_id, data):
    from reader import MOVABLE, text
    target = data.get('folder')
    if target not in MOVABLE: raise ValueError('The robot can only move mail between Inbox and Junk')
    reason = text(data, 'reason', 500)
    if 'MOVE' not in {str(c).upper() for c in getattr(imap, 'capabilities', ())}:
        raise Denied('This mail server cannot move messages safely')
    folder, _, uid, names = locate(imap, message_id, readonly=False)
    if folder not in MOVABLE: raise Denied('The robot can only move incoming mail')
    if target == folder: return {'ok': True, 'folder': target}
    if target not in names: raise Denied('No Junk folder on this mail server')
    _, key = header_key(imap, uid)
    status, _ = imap.uid('MOVE', str(uid), quote(names[target]))
    if status != 'OK': raise Unavailable()
    table.put_item(Item={'pk': 'external#' + name, 'sk': 'MOVE#' + key + '#' + service.timestamp(),
                         'from': folder, 'to': target, 'reason': reason})
    return {'ok': True, 'folder': target}


def draft(table, imap, name, account, data):
    from reader import text
    request = data.get('request_id', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,80}', request): raise ValueError('A unique request_id is required')
    reason = text(data, 'reason', 1000)
    if data.get('bcc') or data.get('attachments'): raise ValueError('Robot drafts take no BCC or attachments')
    content = service.validate_draft({key: data.get(key, '') for key in ('to', 'cc', 'subject', 'body')})
    if not content['to'].strip(): raise ValueError('A draft needs a recipient')
    names = folders(imap)
    if 'Drafts' not in names: raise Denied('No Drafts folder on this mail server')
    msg = EmailMessage()
    msg['From'] = account['address']
    for field in ('to', 'cc', 'subject'):
        if content[field]: msg[field.title()] = content[field]
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid(domain=account['address'].rpartition('@')[2])
    reply = data.get('reply_to_id')
    if reply:
        folder, _, uid, _ = locate(imap, reply)
        if folder == 'Drafts': raise ValueError('Cannot reply to a draft')
        headers, _ = header_key(imap, uid)
        parent = str(headers.get('Message-ID', ''))
        if parent:
            msg['In-Reply-To'] = parent
            msg['References'] = parent
    msg.set_content(content['body'])
    # Claim before APPEND: a retry of the same request never creates a second draft.
    # A claim stuck in "saving" (function stopped mid-request) can be retaken after 10 minutes.
    claim = {'pk': 'external#' + name, 'sk': 'DRAFT#' + hashlib.sha256(request.encode()).hexdigest()[:32]}
    stale = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    try:
        table.put_item(Item={**claim, 'status': 'saving', 'reason': reason, 'created': service.timestamp()},
                       ConditionExpression='attribute_not_exists(pk) OR #s = :failed OR (#s = :saving AND created < :stale)',
                       ExpressionAttributeNames={'#s': 'status'},
                       ExpressionAttributeValues={':failed': 'failed', ':saving': 'saving', ':stale': stale})
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
        existing = table.get_item(Key=claim, ConsistentRead=True)['Item']
        return {'id': existing.get('draft_id'), 'existing': True, 'status': existing['status']}
    try:
        status, response = imap.append(quote(names['Drafts']), '(\\Draft \\Seen)',
                                       imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if status != 'OK': raise Unavailable()
    except (imaplib.IMAP4.error, OSError, Unavailable):
        table.update_item(Key=claim, UpdateExpression='SET #s = :failed', ExpressionAttributeNames={'#s': 'status'},
                          ExpressionAttributeValues={':failed': 'failed'})
        raise Unavailable() from None
    appended = re.search(rb'APPENDUID (\d+) (\d+)', b' '.join(r for r in response or [] if isinstance(r, bytes)))
    draft_id = f'drafts-{int(appended.group(1))}-{int(appended.group(2))}' if appended else None
    table.update_item(Key=claim, UpdateExpression='SET #s = :saved, draft_id = :d', ExpressionAttributeNames={'#s': 'status'},
                      ExpressionAttributeValues={':saved': 'saved', ':d': draft_id})
    return {'id': draft_id}


def dispatch(table, account, name, method, tail, query, data, opener=None):
    imap = (opener or connect)(account)
    try:
        if method == 'GET':
            if tail == ['messages']:
                return list_messages(table, imap, name, query, untriaged=query.get('untriaged') == '1')
            if len(tail) in (2, 4) and tail[0] == 'messages':
                return read(table, imap, name, tail[1], tail)
            raise ValueError('Unknown endpoint')
        if tail == ['drafts']:
            return draft(table, imap, name, account, data)
        if len(tail) == 3 and tail[0] == 'messages' and tail[2] in ('move', 'triage'):
            return (move if tail[2] == 'move' else tag)(table, imap, name, tail[1], data)
        raise ValueError('Unknown endpoint')
    except (imaplib.IMAP4.error, OSError):
        raise Unavailable() from None
    finally:
        try:
            imap.logout()
        except Exception:
            pass
