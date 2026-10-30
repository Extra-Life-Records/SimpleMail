"""Translate authenticated cloud mail into the existing desktop mailbox API.

Account IDs never select a URL or sender: discovery supplies identities, and
every message request remains subject to the server's assignment checks.
"""
import functools
import base64
import hashlib
import inspect
import re
from email.message import EmailMessage
from urllib.parse import urlencode

PREFIX = 'cloud:'
FOLDERS = ('Inbox', 'Sent', 'Drafts', 'Junk', 'Trash')


def route_cloud(method):
    @functools.wraps(method)
    def routed(self, account_id, *args, **kwargs):
        from cloud_storage_bridge import target
        bound = not str(account_id).startswith(PREFIX) and target(self.cfg.account(account_id))
        if str(account_id).startswith(PREFIX) or bound:
            self._acct(account_id)
            adapter = self._cloud_accounts()
            return getattr(adapter, method.__name__)(account_id, *args, **kwargs)
        return method(self, account_id, *args, **kwargs)
    routed.__signature__ = inspect.signature(method)
    return routed


class CloudAccounts:
    def __init__(self, transport, owner):
        self.transport, self.owner = transport, owner
        self.accounts = {}

    def discover(self):
        rows = self.transport.cloud_request('GET', '/mailboxes')['mailboxes']
        self.accounts = {PREFIX + row['name']: {
            'id': PREFIX + row['name'], 'provider': 'cloud',
            'label': row['address'], 'email': row['address'], 'identity': row['address'],
            'color': '#2563eb', 'generation': row.get('generation'),
            'signature': self.owner.cfg.data.get('cloud_signatures', {}).get(PREFIX + row['name'], ''),
            'rules': {}, 'assignment': row['state'], 'folders': row.get('folders', list(FOLDERS))}
            for row in rows if re.fullmatch(r'[a-z0-9_-]+', row['name'])}
        visible = list(self.accounts.values())
        from cloud_storage_bridge import target
        for local in self.owner.cfg.accounts():
            mailbox = target(local)
            if not mailbox:
                continue
            row = next((r for r in rows if r['name'] == mailbox), None)
            if (not row or row.get('generation') != local['cloud_generation'] or
                    row.get('address') != local['email'] or not row.get('external_sent_copy')):
                raise ValueError('Bound mailbox is unavailable or changed; no IMAP fallback')
            self.accounts[local['id']] = {**self.accounts[PREFIX + mailbox],
                'id': local['id'], 'label': local['label'], 'identity': local['from_email'],
                'email': local['from_email'], 'signature': local.get('signature', ''),
                'storage_mailbox': mailbox, 'external_sent_copy': True}
        return visible

    def request(self, account, method, path='', data=None):
        if account not in self.accounts:
            raise ValueError('Mailbox is not available to this sign-in')
        mailbox = self.accounts[account].get('storage_mailbox', account[len(PREFIX):])
        return self.transport.cloud_request(method, '/mailboxes/' + mailbox + path, data)

    @staticmethod
    def mid(uid):
        if not isinstance(uid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', uid):
            raise ValueError('Invalid message ID')
        return '/messages/' + uid

    @staticmethod
    def folder(folder):
        for value in FOLDERS:
            if value.lower() == str(folder).lower(): return value
        if isinstance(folder, str) and 0 < len(folder) <= 500 and not any(ord(c) < 32 or ord(c) == 127 for c in folder):
            return folder
        raise ValueError('Unknown mailbox folder')

    def page(self, account, folder='All', query='', cursor=None):
        # Folder views collect the whole folder. Ten-row protocol defaults make
        # an empty folder wait on dozens of round trips through unrelated mail.
        params = {'folder': folder, 'q': query, 'limit': 100}
        if query: params['search_mode'] = 'indexed'
        if cursor: params['cursor'] = cursor
        return self.request(account, 'GET', '/messages?' + urlencode(params))

    def rows(self, account, folder):
        rows, cursor = [], None
        # Follow filtered empty pages too. Never report a truncated page as all mail.
        while True:
            page = self.page(account, folder, cursor=cursor)
            rows.extend(page['messages'])
            cursor = page.get('cursor')
            if not cursor: return rows

    @staticmethod
    def envelope(row):
        return {**row, 'uid': row['id'], 'server_uid': row['id'],
                'server_folder': row['folder'], 'snippet': row['folder'],
                'subject': row.get('subject') or '(no subject)', 'sender': row.get('sender', ''),
                'date': row.get('date', row.get('received', '')), 'seen': row.get('seen', False)}

    def get_folders(self, account):
        return {'folders': [{'key': f.lower() if f in FOLDERS else f, 'name': f, 'server': f, 'unread': 0}
                            for f in self.accounts[account].get('folders', FOLDERS)], 'error': None}

    def list_messages(self, account, folder):
        if self.accounts[account].get('storage_mailbox') and self.folder(folder) == 'Sent':
            from cloud_storage_bridge import flush
            flush(self, account)
        rows = self.rows(account, self.folder(folder))
        envelopes = [self.envelope(r) for r in rows]
        unread = sum(not r['seen'] for r in envelopes)
        return {'envelopes': envelopes, 'unread': unread, 'folder_unread': unread,
                'inbox_unread': unread if self.folder(folder) == 'Inbox' else None,
                'auto_moved': 0, 'validity': None}

    def search_messages(self, account, query, cursor=None, server_folder=None):
        page = self.page(account, self.folder(server_folder) if server_folder else 'All', query, cursor)
        rows = page['messages']
        if not server_folder: rows = [r for r in rows if r['folder'] not in ('Junk', 'Trash')]
        items = [self.envelope(r) for r in rows]
        if not cursor and (not server_folder or self.folder(server_folder) == 'Drafts'):
            for draft in self.owner._compose_store().list(account):
                payload = draft['payload']
                if query.casefold() in '\n'.join(str(v) for v in payload.values()).casefold():
                    items.insert(0, {'uid': 'local:' + draft['id'], 'localDraftId': draft['id'],
                                    'sender': 'Draft · ' + payload['to'], 'subject': payload['subject'],
                                    'date': draft['updated_at'], 'seen': True, 'snippet': 'Drafts'})
        return {'items': items, 'next_cursor': page.get('cursor'),
                'search_progress': page.get('search_progress')}

    def get_message(self, account, folder, uid, validity=None):
        row = self.request(account, 'GET', self.mid(uid))
        if row.get('draft'):
            row = {**row, **row['draft']}
        self.request(account, 'PATCH', self.mid(uid), {'seen': True})
        return {**row, 'text': row.get('body', ''), 'html': row.get('html'), 'message_ref': None,
                'attachments': [{**a, 'index': a['part']} for a in row.get('attachments', []) if 'part' in a]}

    def set_seen(self, account, folder, uid, seen, validity=None):
        return self.request(account, 'PATCH', self.mid(uid), {'seen': bool(seen)})

    def mark_all_read(self, account, folder):
        rows = self.rows(account, self.folder(folder))
        count = 0
        for row in rows:
            if not row['seen']:
                self.set_seen(account, folder, row['id'], True)
                count += 1
        return {'count': count}

    def move_message(self, account, folder, uid, target, sender='', learn=False, validity=None):
        self.request(account, 'PATCH', self.mid(uid), {'folder': self.folder(target)})
        return {'status': 'moved', 'learned': None}

    def delete_message(self, account, folder, uid, validity=None):
        return self.move_message(account, folder, uid, 'Trash')

    def save_attachment(self, account, folder, uid, part_index, validity=None):
        if not str(part_index).isdigit(): raise ValueError('Invalid attachment')
        file = self.attachment_file(account, uid, part_index)
        result = self.transport.cloud_save_attachment(file)
        return {'path': result.get('path', '')}

    def attachment_file(self, account, uid, part):
        chunks, offset, identity = [], 0, None
        while True:
            file = self.request(account, 'GET', self.mid(uid) + '/downloads/' + str(part) + '?offset=' + str(offset))
            current = (file['name'], file['size'], file['sha256'])
            if identity is None: identity = current
            if current != identity or not 0 <= file['size'] <= 30 * 1024 * 1024 or file['offset'] != offset:
                raise ValueError('Attachment changed during download')
            raw = base64.b64decode(file['data'], validate=True)
            if len(raw) > 1024 * 1024 or offset + len(raw) > file['size']:
                raise ValueError('Invalid attachment chunk')
            chunks.append(raw); offset += len(raw)
            if file['next_offset'] is None:
                if offset != file['size']: raise ValueError('Incomplete attachment download')
                break
            if not raw or file['next_offset'] != offset: raise ValueError('Invalid attachment continuation')
        payload = b''.join(chunks)
        if hashlib.sha256(payload).hexdigest() != identity[2]: raise ValueError('Attachment checksum failed')
        return {'name': identity[0], 'data': base64.b64encode(payload).decode()}

    def get_compose_context(self, account, folder, uid, reply_all=False, forward=False, validity=None):
        from mail_attachments import reply_context
        row = self.request(account, 'GET', self.mid(uid))
        msg = EmailMessage()
        for field, key in [('From', 'sender'), ('To', 'to'), ('Cc', 'cc'),
                           ('Reply-To', 'reply_to'), ('Message-ID', 'message_id')]:
            if row.get(key): msg[field] = row[key]
        context = {} if forward else reply_context(msg, [self.accounts[account]['identity']], reply_all)
        attachments = []
        if forward:
            for part in row.get('attachments', []):
                file = self.attachment_file(account, uid, part['part'])
                attachments.append(self.owner._attachment_store().add_base64(account, file['name'], file['data']))
        return {**context, 'subject': row.get('subject', ''), 'sender': row.get('sender', ''),
                'date': row.get('date', ''), 'text': row.get('body', ''), 'attachments': attachments}

    def send_compose_draft(self, account, draft_id, revision):
        if self.accounts.get(account, {}).get('storage_mailbox'):
            from cloud_storage_bridge import send
            return send(self, account, draft_id, revision)
        from mailbox_agent import recipients
        import json
        if account not in self.accounts:
            raise ValueError('Mailbox is not available to this sign-in')
        store = self.owner._compose_store()
        draft = store.get(account, draft_id)
        payload = draft['payload']
        if not payload['to'].strip():
            raise ValueError('Enter a recipient before sending')
        for field in ('to', 'cc', 'bcc'):
            recipients(payload.get(field, ''))
        attachments = self.owner._attachment_store().resolve(account, payload.get('attachment_ids', []))
        if len(attachments) > 10 or sum(len(a['data']) for a in attachments) > 3 * 1024 * 1024:
            raise ValueError('Use at most 10 attachments, totalling 3 MB')
        data = {key: payload.get(key, '') for key in
                ('to', 'cc', 'bcc', 'subject', 'body', 'body_html', 'in_reply_to', 'references')}
        data['request_id'] = draft_id
        data['attachments'] = [{'name': a['name'], 'data': base64.b64encode(a['data']).decode()} for a in attachments]
        if len(json.dumps(data).encode()) > 4400000:
            raise ValueError('Message is too large. Reduce its attachments or signature images.')
        # Claim the exact reviewed revision before any network send. A crash,
        # timeout or second click must never manufacture another request ID.
        store.claim(account, draft_id, revision)
        try:
            result = self.transport.cloud_request('POST', '/human/mailboxes/' + account[len(PREFIX):] + '/send', data)
            if result.get('status') not in ('accepted', 'delivered'):
                raise ValueError('Send not confirmed')
        except Exception:
            # A lost POST response can be reconciled by a read, never a resend.
            try:
                result = self.request(account, 'GET', '/sends/' + draft_id)
            except Exception:
                result = {}
            if result.get('status') not in ('accepted', 'delivered'):
                detail = {'warning': 'Sending could not be confirmed. Check Sent before trying again. '
                          'This draft will not be sent again automatically.', 'request_id': draft_id}
                store.finish(account, draft_id, 'uncertain', detail)
                return {'status': 'uncertain', **detail}
        store.finish(account, draft_id, 'sent', {'request_id': draft_id, 'provider_id': result.get('provider_id', '')})
        return {'status': 'sent', 'sent_as': self.accounts[account]['identity'],
                'request_id': draft_id, 'provider_status': result['status']}

    def send_mail(self, account, to, subject, body, body_html=None):
        # Compatibility callers also use a durable draft, never a bare POST.
        import uuid
        draft = self.owner.save_compose_draft(account, str(uuid.uuid4()), 0, to, subject, body, body_html or '')
        return self.send_compose_draft(account, draft['id'], draft['revision'])

    def create_folder(self, account, name):
        raise ValueError('This mailbox supports Inbox, Sent, Drafts, Junk and Trash.')

