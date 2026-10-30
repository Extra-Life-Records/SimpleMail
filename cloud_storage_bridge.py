"""Explicit, owner-configured Playloudr storage binding; no IMAP fallback."""
import base64
import hashlib
from email.policy import SMTP
from email.utils import getaddresses


def target(account):
    mailbox = account.get('cloud_mailbox', '')
    if not mailbox:
        return None
    if (mailbox != 'mail-playloudr' or account.get('email') != 'playloudr@extraliferecords.com' or
            account.get('from_email') != 'hello@playloudr.com' or
            account.get('smtp_host') != 'smtp.resend.com' or
            not account.get('smtp_starttls') or not account.get('cloud_generation')):
        raise ValueError('Cloud storage binding needs review; old IMAP storage will not be used')
    return mailbox


class SentCopies:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS external_sent_copies (
                account_id TEXT NOT NULL, request_id TEXT NOT NULL, mailbox TEXT NOT NULL,
                generation TEXT NOT NULL, raw BLOB NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY (account_id, request_id))''')

    def prepare(self, account, request, mailbox, generation, raw):
        with self.store.connect() as db:
            db.execute("INSERT INTO external_sent_copies VALUES(?,?,?,?,?,'prepared')",
                       (account, request, mailbox, generation, raw))

    def advance(self, account, request, before, after):
        with self.store.connect() as db:
            changed = db.execute('UPDATE external_sent_copies SET state=? WHERE account_id=? AND request_id=? AND state=?',
                                 (after, account, request, before)).rowcount
            if changed != 1:
                raise ValueError('Sent copy state changed; reconcile before continuing')

    def pending(self, account):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM external_sent_copies WHERE account_id=? AND state='accepted'", (account,)).fetchall()]


def flush(provider, account):
    """Retry storage only for SMTP-confirmed copies. Never calls SMTP."""
    copies = SentCopies(provider.owner._compose_store())
    binding = provider.accounts[account]
    pending = copies.pending(account)
    if pending:
        provider.discover()
        binding = provider.accounts[account]
    for row in pending:
        if (row['mailbox'] != binding['storage_mailbox'] or
                row['generation'] != binding['generation']):
            raise ValueError('Pending Sent copy belongs to a different assignment')
        raw = bytes(row['raw'])
        result = provider.transport.cloud_request('POST', '/human/mailboxes/' + row['mailbox'] + '/sent-copy',
                    {'request_id': row['request_id'], 'generation': row['generation'],
                     'raw': base64.b64encode(raw).decode()})
        if (result.get('status') != 'stored' or result.get('sha256') != hashlib.sha256(raw).hexdigest() or
                result.get('generation') != row['generation']):
            raise ValueError('Cloud Sent copy was not verified')
        copies.advance(account, row['request_id'], 'accepted', 'stored')
    return len(pending)


def send(provider, account, draft_id, revision):
    import smtplib
    from mailapp import build_message, smtp_credentials, from_address
    from mailbox_agent import recipients
    acct = provider.owner.cfg.account(account)
    mailbox = target(acct)
    provider.discover()  # Revalidate the pinned assignment before SMTP submission.
    binding = provider.accounts[account]
    if mailbox != binding.get('storage_mailbox') or not binding.get('external_sent_copy'):
        raise ValueError('Cloud Sent storage is not ready')
    store = provider.owner._compose_store()
    payload = store.get(account, draft_id)['payload']
    if not payload['to'].strip():
        raise ValueError('Enter a recipient before sending')
    for field in ('to', 'cc', 'bcc'):
        recipients(payload.get(field, ''))
    attachments = provider.owner._attachment_store().resolve(account, payload.get('attachment_ids', []))
    msg = build_message(acct, payload['to'], payload['subject'] or '(no subject)', payload['body'],
                        payload.get('body_html'), cc=payload.get('cc', ''), bcc=payload.get('bcc', ''),
                        in_reply_to=payload.get('in_reply_to', ''), references=payload.get('references', ''),
                        attachments=attachments)
    envelope = [addr for _, addr in getaddresses([str(msg.get(k, '')) for k in ('To', 'Cc', 'Bcc')])]
    if not 1 <= len(envelope) <= 20:
        raise ValueError('Specify 1 to 20 recipients')
    if 'Bcc' in msg:
        del msg['Bcc']
    raw = msg.as_bytes(policy=SMTP)
    if len(base64.b64encode(raw)) > 4000000:
        raise ValueError('Message exceeds cloud Sent storage bound; reduce attachments or signature images')
    copies = SentCopies(store)
    store.claim(account, draft_id, revision)
    copies.prepare(account, draft_id, mailbox, binding['generation'], raw)
    try:
        user, password = smtp_credentials(acct)
        with smtplib.SMTP(acct['smtp_host'], int(acct['smtp_port']), timeout=30) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo(); smtp.login(user, password)
            refused = smtp.sendmail(from_address(acct), envelope, raw)
        copies.advance(account, draft_id, 'prepared', 'accepted')
    except Exception:
        detail = {'warning': 'Delivery could not be confirmed. The original is preserved locally. '
                             'This message will not be sent again automatically.'}
        store.finish(account, draft_id, 'uncertain', detail)
        return {'status': 'uncertain', **detail}
    detail = {'smtp_accepted': True, 'refused_recipients': list(refused or {}),
              'message_id': str(msg['Message-ID']), 'sent_copy_saved': False}
    store.finish(account, draft_id, 'sent', detail)
    try:
        flush(provider, account)
        detail['sent_copy_saved'] = True
    except Exception:
        detail['warning'] = 'Sent through Resend. Its copy is safely stored locally and will sync to cloud Sent; do not resend.'
    return {'status': 'sent', 'sent_as': from_address(acct), **detail}
