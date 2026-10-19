"""Restore verified server Drafts as editable local drafts without changing source mail.

Preview is default. This only writes an existing local compose/attachment store;
it has no sending path and never modifies mailbox credentials or AI grants.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from email import message_from_bytes
from email.policy import default
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mail_migration
from mail_attachments import MAX_FILE, MAX_TOTAL


def prepare(row, account):
    raw = row['path'].read_bytes()
    message = message_from_bytes(raw, policy=default)
    plain, html, attachments = [], [], []
    for index, part in enumerate(message.walk()):
        if part.is_multipart(): continue
        if part.get('Content-ID'):
            raise ValueError('Inline CID draft requires reviewed image conversion before restore')
        if part.get_filename() or part.get_content_disposition() == 'attachment':
            name = part.get_filename() or 'attachment-' + str(index)
            data = part.get_payload(decode=True) or b''
            if len(data) > MAX_FILE or len(name) > 255 or any(c in name for c in '\r\n\x00/\\'):
                raise ValueError('Draft attachment exceeds native compose limits')
            digest = hashlib.sha256(data).hexdigest()
            identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, account + ':' + name + ':' + digest))
            attachments.append((identifier, account, name, part.get_content_type(), data))
        elif part.get_content_type() == 'text/html': html.append(str(part.get_content()))
        elif part.get_content_type() == 'text/plain': plain.append(str(part.get_content()))
        else: raise ValueError('Draft contains an unsupported MIME body part; original remains archived')
    if len(attachments) > 20 or sum(len(part[4]) for part in attachments) > MAX_TOTAL:
        raise ValueError('Draft attachments exceed native compose limits')
    body_html = '\n'.join(html)
    body = '\n'.join(plain) if plain else mail_migration.service.parse(raw, maximum=0)['body']
    if any(len(value) > 500000 or '\x00' in value for value in (body, body_html)):
        raise ValueError('Draft body exceeds native compose limit; no truncation performed')
    payload = {field: str(message.get(header, '')) for field, header in (
        ('to', 'To'), ('cc', 'Cc'), ('bcc', 'Bcc'), ('subject', 'Subject'),
        ('in_reply_to', 'In-Reply-To'), ('references', 'References'))}
    payload.update(body=body, body_html=body_html, attachment_ids=list(dict.fromkeys(part[0] for part in attachments)))
    for field, maximum in (('to', 4000), ('subject', 1000), ('cc', 10000), ('bcc', 10000),
                           ('in_reply_to', 10000), ('references', 10000)):
        if len(payload[field]) > maximum or any(c in payload[field] for c in '\r\n\x00'):
            raise ValueError('Draft header exceeds native compose limits; original remains archived')
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, 'simplemail-imap-draft:' + row['id'] + ':' + account))
    return identifier, json.dumps(payload, ensure_ascii=False, sort_keys=True), attachments


def restore(database, archive_root, target_account, registry, apply=False):
    path = Path(database).resolve()
    if not path.is_file() or not re.fullmatch(r'cloud:mail-[a-z0-9-]+', target_account):
        raise ValueError('Use an existing database and a configured native target account')
    manifest, rows, _ = mail_migration.archive(archive_root)
    address = registry.get(target_account[6:], {}).get('address')
    if not address or address.lower() != manifest['username'].lower():
        raise ValueError('Archive account must match the reviewed destination registry address')
    drafts = [row for row in rows if row['present'] and row['folder'] == 'Drafts']
    prepared = [(row, *prepare(row, target_account)) for row in drafts]
    report = {'source_id': manifest['source_id'], 'target_account': target_account, 'apply': apply,
              'source_drafts': len(drafts), 'copied': [], 'existing': [], 'conflicts': [],
              'source_unchanged': True, 'sending_performed': False}
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        for row, identifier, payload, attachments in prepared:
            existing = db.execute('SELECT * FROM compose_drafts WHERE id=?', (identifier,)).fetchone()
            if existing:
                if (existing['account_id'] != target_account or existing['status'] != 'pending' or
                        existing['revision'] != 1 or existing['payload'] != payload):
                    report['conflicts'].append({'source_id': row['id'], 'reason': 'target_draft_changed_since_restore'})
                else:
                    for attachment in attachments:
                        saved = db.execute('SELECT * FROM attachments WHERE id=?', (attachment[0],)).fetchone()
                        if saved is None or tuple(saved) != attachment:
                            raise ValueError('Previously restored draft attachment differs or is missing')
                    report['existing'].append(identifier)
                continue
            if apply:
                for attachment in attachments:
                    saved = db.execute('SELECT * FROM attachments WHERE id=?', (attachment[0],)).fetchone()
                    if saved is not None and tuple(saved) != attachment:
                        raise ValueError('Existing attachment identity conflicts with restored MIME')
                    db.execute('INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?)', attachment)
                db.execute("INSERT INTO compose_drafts VALUES(?,?,1,'pending',?,?)",
                           (identifier, target_account, payload, row['received']))
            report['copied'].append(identifier)
        if apply: db.commit()
        else: db.rollback()
    except Exception:
        db.rollback(); raise
    finally: db.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--target-account', required=True)
    parser.add_argument('--registry', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    registry = json.loads(args.registry.read_text(encoding='utf-8-sig'))
    print(json.dumps(restore(args.database, args.archive, args.target_account, registry, args.apply), indent=2))


if __name__ == '__main__': main()
