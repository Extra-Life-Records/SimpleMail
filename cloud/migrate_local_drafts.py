"""Copy pending owner drafts to a replacement account without altering the source.

Uses the existing private SimpleMail SQLite database. Credentials and mail-grant
tables are never read or changed. Preview is the default; --apply is explicit.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path


def copy_pending(database, source_account, target_account, apply=False):
    path = Path(database).resolve()
    if not path.is_file() or not re.fullmatch(r'cloud:mail-[a-z0-9-]+', target_account) or source_account == target_account:
        raise ValueError('Use an existing database and a distinct native target account')
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    report = {'source_account': source_account, 'target_account': target_account, 'apply': apply,
              'copied': [], 'existing': [], 'conflicts': []}
    try:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        rows = db.execute("SELECT * FROM compose_drafts WHERE account_id=? AND status IN ('pending','sending','uncertain')", (source_account,)).fetchall()
        for row in rows:
            if row['status'] != 'pending':
                report['conflicts'].append({'id': row['id'], 'reason': 'source_send_outcome_requires_reconciliation'})
                continue
            payload = json.loads(row['payload']); replacements = []
            attachment_rows = []
            for identifier in payload.get('attachment_ids', []):
                attachment = db.execute('SELECT * FROM attachments WHERE id=? AND account_id=?', (identifier, source_account)).fetchone()
                if attachment is None: raise ValueError('Pending draft attachment is missing')
                digest = hashlib.sha256(attachment['data']).hexdigest()
                new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, target_account + ':' + attachment['name'] + ':' + digest))
                replacements.append(new_id); attachment_rows.append((new_id, attachment))
            payload['attachment_ids'] = replacements
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'simplemail-draft-migration:' + source_account + ':' + row['id'] + ':' + target_account))
            existing = db.execute('SELECT * FROM compose_drafts WHERE id=?', (new_id,)).fetchone()
            if existing:
                if existing['account_id'] != target_account or existing['payload'] != encoded:
                    report['conflicts'].append({'id': row['id'], 'reason': 'source_or_target_draft_changed_since_copy'})
                else: report['existing'].append(new_id)
                continue
            if apply:
                for attachment_id, attachment in attachment_rows:
                    db.execute('INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?)',
                               (attachment_id, target_account, attachment['name'], attachment['content_type'], attachment['data']))
                db.execute('INSERT INTO compose_drafts VALUES(?,?,1,?,?,?)',
                           (new_id, target_account, 'pending', encoded, row['updated_at']))
            report['copied'].append(new_id)
        if apply: db.commit()
        else: db.rollback()
        return report
    except Exception:
        db.rollback(); raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--source-account', required=True); parser.add_argument('--target-account', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(copy_pending(args.database, args.source_account, args.target_account, args.apply), indent=2))


if __name__ == '__main__':
    main()
