"""Durable single-message filing and owner Undo. Never use folder-wide EXPUNGE."""
import json
import re
import uuid
import hashlib
from agent_store import AgentStore, now


def quote(folder):
    if not isinstance(folder, str) or not folder or len(folder) > 500 or any(c in folder for c in '\r\n\x00'):
        raise ValueError('Invalid folder')
    return '"' + folder.replace('\\', '\\\\').replace('"', '\\"') + '"'


def select(imap, folder, readonly=True):
    result, _ = imap.select(quote(folder), readonly=readonly)
    if result != 'OK':
        raise ValueError('Folder unavailable')
    _, values = imap.response('UIDVALIDITY')
    value = values[0] if values else None
    if not isinstance(value, bytes) or not value.isdigit() or int(value) < 1:
        raise ValueError('Server did not identify this folder safely')
    _, sticky = imap.response('UIDNOTSTICKY')
    if sticky and any(value is not None for value in sticky):
        raise ValueError('This folder does not support stable message identities')
    return value.decode('ascii')


def mapping(imap, data, source_uid, target_validity):
    _, responses = imap.response('COPYUID')
    candidates = [value for value in responses or [] if isinstance(value, bytes)]
    candidates += [match.group(1) for value in data or [] if isinstance(value, bytes)
                   for match in re.finditer(rb'\[COPYUID ([^\]]+)\]', value)]
    for value in candidates:
        match = re.fullmatch(rb'\s*(\d+) (\d+)(?::\2)? (\d+)(?::\3)?\s*', value)
        if match and match[1].decode() == target_validity and match[2].decode() == source_uid and int(match[3]) > 0:
            return match[3].decode()
    return None


class FilingStore(AgentStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS filing_actions (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, request_key TEXT NOT NULL,
                source TEXT NOT NULL, target TEXT NOT NULL, destination TEXT,
                status TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, rule_domain TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(account_id,request_key))''')

    @staticmethod
    def item(row):
        result = dict(row)
        result['source'] = json.loads(result['source'])
        result['destination'] = json.loads(result['destination']) if result['destination'] else None
        result['action_id'] = result['id']
        if result['status'] in ('moving', 'uncertain', 'undoing', 'undo_uncertain'):
            result['warning'] = 'Filing needs checking. Refresh the folders; do not repeat the operation automatically.'
        return result

    def get(self, account, identifier):
        with self.connect() as db:
            row = db.execute('SELECT * FROM filing_actions WHERE account_id=? AND id=?', (account, identifier)).fetchone()
        if not row:
            raise ValueError('Filing action is unavailable for this mailbox')
        return self.item(row)

    def request(self, account, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM filing_actions WHERE account_id=? AND request_key=?', (account, key)).fetchone()
        return self.item(row) if row else None

    def list(self, account, before=None):
        with self.connect() as db:
            rows = db.execute('SELECT rowid AS cursor,* FROM filing_actions WHERE account_id=? '
                              'AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT 26', (account, before, before)).fetchall()
        items = [self.item(row) for row in rows[:25]]
        return {'items': items, 'next_cursor': items[-1]['cursor'] if len(rows) > 25 else None}

    def begin(self, account, source, target, request_key, actor, reason, rule_domain=''):
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise ValueError('Give a stable filing request key')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError('Explain why this message should be filed')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM filing_actions WHERE account_id=? AND request_key=?', (account, request_key)).fetchone()
            if row:
                existing = self.item(row)
                if existing['source'] != source or existing['target'] != target:
                    raise ValueError('This filing request key already belongs to another operation')
                return existing, False
            identifier, stamp = str(uuid.uuid4()), now()
            db.execute('INSERT INTO filing_actions VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?)',
                       (identifier, account, request_key, json.dumps(source), target, 'moving', actor, reason, rule_domain, stamp, stamp))
            self._event(db, account, 'filing_started', {'action_id': identifier, 'note': reason, 'target': target})
        return self.get(account, identifier), True

    def finish(self, account, identifier, status, destination=None, expected='moving'):
        with self.connect() as db:
            changed = db.execute('UPDATE filing_actions SET status=?,destination=COALESCE(?,destination),updated_at=? '
                                 'WHERE account_id=? AND id=? AND status=?',
                                 (status, json.dumps(destination) if destination else None, now(), account, identifier, expected)).rowcount
            if changed != 1:
                raise ValueError('Filing action changed; refresh before acting')
            self._event(db, account, 'filing_' + status, {'action_id': identifier})
        return self.get(account, identifier)

    def claim_undo(self, account, identifier):
        with self.connect() as db:
            changed = db.execute("UPDATE filing_actions SET status='undoing',updated_at=? WHERE account_id=? AND id=? AND status='moved'",
                                 (now(), account, identifier)).rowcount
            if changed != 1:
                raise ValueError('This move has already changed; refresh before undoing')


def move(account, store, folder, uid, target, connector, expected_validity=None,
         request_key=None, actor='owner', reason='Filed by owner', guard=None, rule_domain=''):
    uid = str(uid)
    if not uid.isdigit() or int(uid) < 1:
        raise ValueError('Invalid message UID')
    quote(folder); quote(target)
    if folder == target:
        raise ValueError('Message is already in that folder')
    imap, _, _ = connector(account)
    try:
        capabilities = {value.decode().upper() if isinstance(value, bytes) else str(value).upper() for value in imap.capabilities}
        if not {'MOVE', 'UIDPLUS'} <= capabilities:
            raise ValueError('This server does not support recoverable filing; no message was moved')
        target_validity = select(imap, target)
        validity = select(imap, folder, False)
        if expected_validity is not None and validity != expected_validity:
            raise ValueError('Folder identity changed; search again before filing')
        source = {'folder': folder, 'validity': validity, 'uid': uid}
        key = request_key or actor + '-' + hashlib.sha256(json.dumps([source, target], sort_keys=True).encode()).hexdigest()
        existing = store.request(account['id'], key)
        if existing:
            if existing['source'] != source or existing['target'] != target:
                raise ValueError('This filing request key already belongs to another operation')
            return existing
        # Check that the exact UID exists without changing its read state.
        result, data = imap.uid('FETCH', uid, '(UID)')
        if result != 'OK' or not any(re.search(rb'\bUID ' + uid.encode() + rb'\b',
                                              part[0] if isinstance(part, tuple) else part)
                                     for part in data or [] if isinstance(part, (bytes, tuple))):
            raise ValueError('Message is unavailable; refresh the folder')
        if guard:
            guard()
        action, fresh = store.begin(account['id'], source, target, key, actor, reason, rule_domain)
        if not fresh:
            return action
        imap.response('COPYUID')  # discard any earlier response
        try:
            if guard:
                guard()
            result, data = imap.uid('MOVE', uid, quote(target))
            destination_uid = mapping(imap, data, uid, target_validity) if result == 'OK' else None
        except Exception:
            destination_uid = None
        if destination_uid is None:
            return store.finish(account['id'], action['id'], 'uncertain')
        return store.finish(account['id'], action['id'], 'moved',
                            {'folder': target, 'validity': target_validity, 'uid': destination_uid})
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def undo(account, store, identifier, connector):
    action = store.get(account['id'], identifier)
    if action['status'] != 'moved' or not action['destination']:
        raise ValueError('Only a confirmed move can be undone; check uncertain actions in the mailbox')
    source, destination = action['source'], action['destination']
    imap, _, _ = connector(account)
    try:
        capabilities = {value.decode().upper() if isinstance(value, bytes) else str(value).upper() for value in imap.capabilities}
        if not {'MOVE', 'UIDPLUS'} <= capabilities:
            raise ValueError('This server does not support recoverable filing')
        if select(imap, source['folder']) != source['validity']:
            raise ValueError('Original folder was recreated; choose a destination manually')
        if select(imap, destination['folder'], False) != destination['validity']:
            raise ValueError('Destination folder changed; search for the message before acting')
        result, data = imap.uid('FETCH', destination['uid'], '(UID)')
        if result != 'OK' or not any(re.search(rb'\bUID ' + destination['uid'].encode() + rb'\b',
                                              part[0] if isinstance(part, tuple) else part)
                                     for part in data or [] if isinstance(part, (bytes, tuple))):
            raise ValueError('Filed message moved or disappeared; search before restoring it')
        store.claim_undo(account['id'], identifier)
        imap.response('COPYUID')
        try:
            result, data = imap.uid('MOVE', destination['uid'], quote(source['folder']))
            restored = mapping(imap, data, destination['uid'], source['validity']) if result == 'OK' else None
        except Exception:
            restored = None
        status = 'undone' if restored else 'undo_uncertain'
        return store.finish(account['id'], identifier, status, expected='undoing')
    finally:
        try:
            imap.logout()
        except Exception:
            pass
