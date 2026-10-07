"""Translate authenticated cloud mail into the existing desktop mailbox API.

Account IDs never select a URL or sender: discovery supplies identities, and
every message request remains subject to the server's assignment checks.
"""
import functools
import inspect
import re
from email.message import EmailMessage
from urllib.parse import urlencode

PREFIX = 'cloud:'
FOLDERS = ('Inbox', 'Sent', 'Drafts', 'Junk', 'Trash')


def route_cloud(method):
    @functools.wraps(method)
    def routed(self, account_id, *args, **kwargs):
        if str(account_id).startswith(PREFIX):
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
            'color': '#2563eb', 'signature': '', 'rules': {}, 'assignment': row['state']}
            for row in rows if re.fullmatch(r'[a-z0-9_-]+', row['name'])}
        return list(self.accounts.values())

    def request(self, account, method, path='', data=None):
        if account not in self.accounts:
            raise ValueError('Mailbox is not available to this sign-in')
        return self.transport.cloud_request(method, '/mailboxes/' + account[len(PREFIX):] + path, data)

    @staticmethod
    def mid(uid):
        if not isinstance(uid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', uid):
            raise ValueError('Invalid message ID')
        return '/messages/' + uid

    @staticmethod
    def folder(folder):
        for value in FOLDERS:
            if value.lower() == str(folder).lower(): return value
        raise ValueError('Unknown mailbox folder')

    def page(self, account, folder='All', query='', cursor=None):
        params = {'folder': folder, 'q': query}
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
        return {'folders': [{'key': f.lower(), 'name': f, 'server': f, 'unread': 0}
                            for f in FOLDERS], 'error': None}

    def list_messages(self, account, folder):
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
        return {'items': items, 'next_cursor': page.get('cursor')}

    def get_message(self, account, folder, uid, validity=None):
        row = self.request(account, 'GET', self.mid(uid))
        if row.get('draft'):
            row = {**row, **row['draft']}
        self.request(account, 'PATCH', self.mid(uid), {'seen': True})
        return {**row, 'text': row.get('body', ''), 'html': None, 'message_ref': None,
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
        file = self.request(account, 'GET', self.mid(uid) + '/attachments/' + str(part_index))
        result = self.transport.cloud_save_attachment(file)
        return {'path': result.get('path', '')}

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
                file = self.request(account, 'GET', self.mid(uid) + '/attachments/' + str(part['part']))
                attachments.append(self.owner._attachment_store().add_base64(account, file['name'], file['data']))
        return {**context, 'subject': row.get('subject', ''), 'sender': row.get('sender', ''),
                'date': row.get('date', ''), 'text': row.get('body', ''), 'attachments': attachments}

    def send_compose_draft(self, account, *args, **kwargs):
        # Preserve the local draft and avoid claiming a send until regional SES
        # production access and sender authentication are enabled and verified.
        raise ValueError('Sending is not enabled for this mailbox yet. Your draft is saved.')

    send_mail = send_compose_draft

    def create_folder(self, account, name):
        raise ValueError('This mailbox supports Inbox, Sent, Drafts, Junk and Trash.')

