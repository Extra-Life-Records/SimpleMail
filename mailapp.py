#!/usr/bin/env python3
"""
SimpleMail - a minimal, beautiful IMAP/SMTP mail client for Fasthosts.

Architecture: Python backend (IMAP/SMTP, stdlib) + WebView2 frontend
(pywebview + Pico CSS). Runs on Windows x64 AND Windows ARM64.

Servers (Fasthosts mailboxes are provisioned on the livemail platform):
    IMAP: mail.livemail.co.uk:993   (SSL/TLS)
    SMTP: smtp.fasthosts.co.uk:587 (STARTTLS)

Usage:
    python mailapp.py            # launch the GUI
    python mailapp.py --check    # test connectivity from CLI (no GUI)
"""

import json
import os
import re
import smtplib
import subprocess
import sys
import threading
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as email_policy
from pathlib import Path
import html as html_lib
from html.parser import HTMLParser
from mail_credentials import unlock_account, write_config
from window_state import load_placement, remember_window
from cloud_accounts import CloudAccounts, route_cloud
from cloud_mail import (CloudSignInRequired, CloudAccessDenied, CloudConfigurationError,
                        CloudServiceError, CloudRequestError)

# ---------------------------------------------------------------------------
# pythonnet / pywebview environment (must be set BEFORE importing webview)
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent

if os.name == "nt":
    # Force pythonnet to use the .NET Core WindowsDesktop runtime (contains
    # WinForms, which pywebview needs). Without this, pythonnet loads the
    # console runtime and System.Windows.Forms is missing.
    os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
    _rc = _BASE_DIR / "runtimeconfig.json"
    if _rc.exists():
        os.environ.setdefault("PYTHONNET_CORECLR_RUNTIME_CONFIG", str(_rc))

try:
    import webview
except Exception as e:  # pragma: no cover
    webview = None
    _WEBVIEW_IMPORT_ERROR = e
else:
    _WEBVIEW_IMPORT_ERROR = None

APP_NAME = "SimpleMail"
APP_VERSION = "1.9.4"
APP_REPO = "Extra-Life-Records/SimpleMail"  # owner/repo for auto-updates
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "accounts": [],          # list of account dicts (see ACCOUNT_DEFAULTS)
    "active_account": "",    # id of the last-used account
    "max_messages": 100,
    "ui_scale": "default",  # compact | default | large
}

# One entry per mailbox. IDENTITY ISOLATION IS THE POINT of this schema:
# every account carries its own servers, credentials, From address, signature
# and learned rules, and the backend only ever derives the From address from
# the account that owns the mailbox - never from anything the UI sends.
ACCOUNT_DEFAULTS = {
    "id": "",                # stable slug, never shown
    "label": "",             # human name shown in the sidebar / From display name
    "color": "#2563eb",      # account accent so mailboxes are visually unmistakable
    "email": "",             # IMAP login, and the default identity
    "password": "",          # IMAP login password (and SMTP unless overridden)
    "from_email": "",        # identity override (e.g. hello@playloudr.com while
                             # the mailbox itself lives on another domain)
    "imap_host": "mail.livemail.co.uk",
    "imap_port": 993,
    "smtp_host": "smtp.fasthosts.co.uk",
    "smtp_port": 587,
    "smtp_starttls": True,
    "smtp_user": "",         # SMTP login override (e.g. Resend's "resend")
    "smtp_password": "",     # SMTP password override (e.g. a Resend API key)
    "signature": "",
    "rules": {},             # learned sender-domain -> folder moves, per account
}

# Legacy (v1.0.x) single-account keys, migrated into accounts[0] on first load.
_LEGACY_KEYS = ("email", "password", "signature", "imap_host", "imap_port",
                "smtp_host", "smtp_port", "smtp_starttls", "smtp_user", "smtp_password", "from_email", "rules")


def _slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")
    return s or "account"


def normalize_account(raw):
    """Fill an account dict with defaults; never mutates the input."""
    acct = dict(ACCOUNT_DEFAULTS)
    acct["rules"] = {}
    for key in ACCOUNT_DEFAULTS:
        if key in (raw or {}):
            acct[key] = raw[key]
    if not acct["id"]:
        acct["id"] = _slugify(acct["email"].split("@")[-1].split(".")[0] or acct["label"])
    if not acct["label"]:
        acct["label"] = acct["email"] or acct["id"]
    return acct


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass
        self._migrate_legacy()
        raw_accounts = self.data.get("accounts", [])
        self.data["accounts"] = [unlock_account(normalize_account(a)) for a in raw_accounts]
        if any(isinstance(a.get(field), str) and a.get(field)
               for a in raw_accounts for field in ("password", "smtp_password")):
            self.save()

    def _migrate_legacy(self):
        """v1.0.x kept a single account's fields at the top level."""
        if self.data.get("accounts"):
            return
        legacy_email = self.data.get("email", "")
        if not legacy_email:
            return
        acct = normalize_account({k: self.data[k] for k in _LEGACY_KEYS if k in self.data})
        domain = legacy_email.split("@")[-1].split(".")[0]
        acct["label"] = domain.replace("-", " ").title() if domain else legacy_email
        self.data["accounts"] = [acct]
        self.data["active_account"] = acct["id"]
        for k in _LEGACY_KEYS:
            self.data.pop(k, None)
        try:
            self.save()
        except OSError:
            pass  # migration re-runs next boot; nothing is lost

    def save(self):
        write_config(CONFIG_FILE, self.data)

    # -- accounts ----------------------------------------------------------
    def accounts(self):
        return self.data.get("accounts", [])

    def account(self, account_id):
        """The account dict for an id. Raises on an unknown id rather than
        falling back to another account - a silent fallback is exactly how a
        reply would leave from the wrong address."""
        for acct in self.accounts():
            if acct["id"] == account_id:
                return acct
        raise KeyError(f"Unknown account: {account_id!r}")

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value


# ---------------------------------------------------------------------------
# Identity helpers - the single choke points for who a message is "from" and
# how SMTP authenticates. Everything that sends goes through these.
# ---------------------------------------------------------------------------

def from_address(acct):
    return (acct.get("from_email") or acct["email"]).strip()


def smtp_credentials(acct):
    user = (acct.get("smtp_user") or acct["email"]).strip()
    password = acct.get("smtp_password") or acct["password"]
    return user, password


# ---------------------------------------------------------------------------
# Mail plumbing (pure logic - also used by --check)
# ---------------------------------------------------------------------------

def connect_imap(cfg):
    """Open an authenticated IMAP connection. Returns (imap, folder_list)."""
    import imaplib
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg["imap_port"]), timeout=30)
    imap.login(cfg["email"], cfg["password"])
    folders = []
    sent_folder = None
    try:
        typ, data = imap.list()
        if typ == "OK":
            for line in data:
                text = line.decode("utf-8", "replace")
                m = re.search(r'"([^"]+)"\s*$', text)
                if m:
                    name = m.group(1)
                    folders.append(name)
                    if r"\Sent" in text and sent_folder is None:
                        sent_folder = name
    except Exception:
        pass
    return imap, folders, sent_folder


def pick_sent_folder(folders, sent_flag_folder):
    """Best folder for saving sent copies: the one flagged \\Sent, else by name."""
    if sent_flag_folder:
        return sent_flag_folder
    for f in folders:
        if f.lower() in ("sent messages", "sent", "sent items"):
            return f
    return "Sent"


FOLDER_ORDER = ["inbox", "sent", "drafts", "junk", "trash"]
FOLDER_LABELS = {
    "inbox": "Inbox", "sent": "Sent", "drafts": "Drafts",
    "junk": "Junk", "trash": "Trash",
}


def map_folders(server_folders):
    """Map server folder names to logical keys. Returns [{key,name,server}]."""
    lower = {f.lower(): f for f in server_folders}
    picks = {}
    if "inbox" in lower:
        picks["inbox"] = lower["inbox"]
    for key, candidates in {
        "sent": ["sent messages", "sent", "sent items"],
        "drafts": ["drafts"],
        "junk": ["junk", "junk email", "spam"],
        "trash": ["trash", "deleted items", "deleted messages"],
    }.items():
        for c in candidates:
            if c in lower:
                picks[key] = lower[c]
                break
    out = []
    for key in FOLDER_ORDER:
        if key in picks:
            out.append({"key": key, "name": FOLDER_LABELS[key], "server": picks[key]})
    # include any leftover server folders so nothing is unreachable
    known = set(picks.values())
    used_keys = set(picks.keys())
    for f in server_folders:
        key = f.lower().replace(" ", "-")
        if f not in known and key not in used_keys:
            out.append({"key": key, "name": f, "server": f})
            used_keys.add(key)
    return out


def selected_validity(imap):
    try:
        _, values = imap.response('UIDVALIDITY')
        value = values[0] if values else None
        return value.decode('ascii') if isinstance(value, bytes) and value.isdigit() and int(value) > 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def fetch_envelopes(imap, folder, limit):
    """Return list of dicts: uid, sender, subject, date, seen, snippet."""
    import imaplib
    imap.select(folder, readonly=True)
    typ, data = imap.uid("search", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()[-limit:]
    uidlist = b",".join(uids)

    # Pass 1: flags - this server drops FLAGS when combined with BODY.PEEK,
    # so it must be a separate (batched) fetch.
    flags_map = {}
    try:
        typ, fdata = imap.uid("fetch", uidlist, "(UID FLAGS)")
        for item in fdata:
            line = item if isinstance(item, bytes) else item[0]
            m = re.search(rb"UID (\d+) FLAGS \(([^)]*)\)", line)
            if m:
                flags_map[m.group(1).decode()] = m.group(2).decode("utf-8", "replace")
    except Exception:
        pass

    # Pass 2: headers + 200-byte body peek, batched. Response items alternate
    # (desc_line, payload) tuples per message part. Content-Type and
    # Content-Transfer-Encoding ride along so single-part bodies can be
    # decoded (quoted-printable receipts showed raw =0A codes in previews).
    envelopes = []
    try:
        typ, fdata = imap.uid(
            "fetch", uidlist,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE"
            " CONTENT-TYPE CONTENT-TRANSFER-ENCODING)]"
            " BODY.PEEK[TEXT]<0.2000>)",
        )
        cur = None  # current envelope being assembled
        cur_cte = cur_ctype = ""
        for item in fdata:
            if not isinstance(item, tuple):
                continue
            desc = item[0].decode("utf-8", "replace")
            payload = item[1]
            if "BODY[HEADER.FIELDS" in desc:
                m = re.search(r"UID (\d+)", desc)
                if not m:
                    continue
                msg = message_from_bytes(payload, policy=email_policy)
                cur_cte = str(msg.get("Content-Transfer-Encoding", ""))
                cur_ctype = str(msg.get("Content-Type", ""))
                cur = {
                    "uid": m.group(1),
                    "sender": str(msg.get("From", "")),
                    "subject": str(msg.get("Subject", "(no subject)")),
                    "date": str(msg.get("Date", "")),
                    "seen": r"\Seen" in flags_map.get(m.group(1), ""),
                    "snippet": "",
                }
                envelopes.append(cur)
            elif "BODY[TEXT]" in desc and cur is not None:
                snippet = decode_snippet(payload, cur_cte, cur_ctype)
                cur["snippet"] = snippet[:180]
    except Exception:
        pass
    envelopes.reverse()
    return envelopes


def count_unread(imap, folder):
    """Unread count for the WHOLE folder, straight from the server.

    The envelope list is only the most recent page of messages, so counting
    unseen ones in it undercounts a busy folder. Both the sidebar badge and
    the folder header use this instead, so they can never disagree.
    Returns None if the server would not say.
    """
    try:
        imap.select(folder, readonly=True)
        typ, data = imap.uid("search", None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return 0
        return len(data[0].split())
    except Exception:
        return None


def decode_transfer(raw, cte, ctype=""):
    """Undo the Content-Transfer-Encoding on a raw body peek.

    The peek is truncated at 2000 bytes, so it can end mid quoted-printable
    escape or mid base64 quantum - decode what is whole, never raise.
    """
    cte = (cte or "").strip().lower()
    data = raw
    if cte == "quoted-printable":
        import quopri
        # drop a trailing partial escape ("=" or "=C") cut off by the peek
        clipped = re.sub(rb"=[0-9A-Fa-f]?$", b"", raw)
        try:
            data = quopri.decodestring(clipped)
        except Exception:
            data = raw
    elif cte == "base64":
        import base64
        b = re.sub(rb"\s+", b"", raw)
        b = b[: len(b) - (len(b) % 4)]  # truncated peek: drop partial quantum
        try:
            data = base64.b64decode(b)
        except Exception:
            data = raw
    charset = "utf-8"
    m = re.search(r'charset="?([\w.-]+)"?', ctype or "", re.I)
    if m:
        charset = m.group(1)
    try:
        return data.decode(charset, "replace")
    except Exception:
        return data.decode("utf-8", "replace")


def decode_snippet(raw, cte="", ctype=""):
    """Turn a raw BODY[TEXT] peek into a clean one-line preview.

    The peek is the *start of the MIME structure* for multipart messages:
    possibly a leading blank line, a boundary line, Content-* headers, then
    the body (quoted-printable or base64). We extract the real boundary
    name, re-wrap it as a multipart message, and pick the longest readable
    text part - so previews are real sentences, not MIME scaffolding.

    Single-part messages have no such scaffolding: their body arrives still
    wearing the transfer encoding declared in the top-level headers, so it
    is undone here (regression: quoted-printable receipts previewed as
    "=0A=0A =0A ..." until v1.1.7).
    """
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return ""
    text = text.lstrip("\r\n")  # some peeks start with a blank line
    m = re.match(r"^(--[^\r\n]+)", text)
    if not m:
        # some messages start with a short preamble ("This is a multi-part
        # message in MIME format.") before the first boundary - find it
        m2 = re.search(r"\r?\n(--[^\r\n]+)", text)
        if m2:
            m = m2
    if m:
        boundary = m.group(1)
        try:
            from email import message_from_string
            from email.policy import default as _pol
            wrapped = (
                f"Content-Type: multipart/mixed; boundary=\"{boundary[2:]}\"\r\n\r\n"
                + text
            )
            msg = message_from_string(wrapped, policy=_pol)
            best = ""
            for part in msg.walk():
                ct = part.get_content_type()
                try:
                    body = part.get_content()
                except Exception:
                    continue
                if not body or not body.strip():
                    continue
                if ct == "text/plain":
                    cand = collapse_snippet(body)
                elif ct == "text/html":
                    cand = collapse_snippet(html_to_text(body))
                else:
                    continue
                if len(cand) > len(best):
                    best = cand
            if best:
                return best
        except Exception:
            pass
    # not multipart scaffolding - a single-part body. Binary types (DMARC
    # reports are a bare application/zip body) have nothing readable to
    # preview: decoding them spills raw zip bytes ("PK..") into the inbox.
    main_type = (ctype or "").split(";")[0].strip().lower()
    if main_type and not main_type.startswith(("text/", "message/")):
        name = re.search(r'name="?([^";\r\n]+)"?', ctype or "", re.I)
        return f"({name.group(1)})" if name else "(attachment)"
    # undo the declared transfer encoding, then flatten (html or plain)
    text = decode_transfer(raw, cte, ctype)
    if "<" in text:
        return collapse_snippet(html_to_text(text))
    return collapse_snippet(text)


def collapse_snippet(s):
    """Flatten whitespace/newlines into a single readable line."""
    # binary residue (control chars, undecodable-byte marks) never belongs
    # in a preview, whatever the declared content type claimed
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f�]+", " ", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def select_message_folder(imap, folder, expected_validity=None, readonly=False):
    from mail_filing import quote
    typ, _ = imap.select(quote(folder), readonly=readonly)
    if typ != 'OK':
        raise ValueError('Folder unavailable')
    if expected_validity is not None and selected_validity(imap) != expected_validity:
        raise ValueError('Folder changed; search again before using this message')
    if expected_validity is not None:
        kind, sticky = imap.response('UIDNOTSTICKY')
        if kind == 'UIDNOTSTICKY' and sticky and any(value is not None for value in sticky):
            raise ValueError('This folder does not support stable message identities')


def fetch_message(imap, folder, uid, expected_validity=None):
    """Return dict with parsed message; marks it \\Seen."""
    import imaplib
    select_message_folder(imap, folder, expected_validity)
    typ, data = imap.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or data[0] is None:
        raise RuntimeError("Could not fetch message")
    raw = data[0][1]
    msg = message_from_bytes(raw, policy=email_policy)
    try:
        imap.uid("store", uid, "+FLAGS", r"(\Seen)")
    except Exception:
        pass
    text, html = extract_bodies(msg)
    attachments = []
    for idx, part in enumerate(msg.walk()):
        fn = part.get_filename()
        if fn:
            try:
                size = len(part.get_payload(decode=True) or b"")
            except Exception:
                size = 0
            attachments.append({
                "index": idx,
                "name": fn,
                "size": size,
                "content_type": part.get_content_type(),
            })
    return {
        "subject": str(msg.get("Subject", "(no subject)")),
        "sender": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "date": str(msg.get("Date", "")),
        "text": text,
        "html": html,
        "attachments": attachments,
    }


def extract_bodies(msg):
    """Return (plain_text, html) - prefer plain, fall back to html -> text."""
    text, html = None, None
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_disposition() in ("attachment", "inline") and ct != "text/html":
            continue
        if ct == "text/plain" and text is None:
            try:
                text = part.get_content()
            except Exception:
                pass
        elif ct == "text/html" and html is None:
            try:
                html = part.get_content()
            except Exception:
                pass
    if html and not text:
        text = html_to_text(html)
    return text or "(no readable text body)", html


class _TE(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "blockquote"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html):
    p = _TE()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or "(empty html message)"


def send_message(acct, to, subject, body, body_html=None, save_sent=True,
                 cc="", bcc="", in_reply_to="", references="", message_id=None, attachments=None):
    """Send via the ACCOUNT'S OWN SMTP as the ACCOUNT'S OWN identity, then
    append a copy to that same account's Sent folder.

    The From address is derived here from the account and nowhere else -
    callers cannot supply one, so a message can never leave a mailbox under
    another account's identity."""
    from email.utils import formataddr, formatdate, make_msgid

    msg = EmailMessage()
    sender = from_address(acct)
    msg["From"] = formataddr((acct.get("label") or "", sender)) if acct.get("label") else sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if body_html:
        msg.set_content(body or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body or "")
    for attachment in attachments or []:
        maintype, subtype = attachment["content_type"].split("/", 1)
        msg.add_attachment(attachment["data"], maintype=maintype, subtype=subtype, filename=attachment["name"])
    smtp_user, smtp_password = smtp_credentials(acct)
    with smtplib.SMTP(acct["smtp_host"], int(acct["smtp_port"]), timeout=30) as smtp:
        smtp.ehlo()
        if acct["smtp_starttls"]:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(smtp_user, smtp_password)
        refused = smtp.send_message(msg)
    # SMTP strips Bcc on transmission; the Sent copy must also omit it.
    if "Bcc" in msg:
        del msg["Bcc"]
    sent_copy_saved = False
    if save_sent:
        try:
            imap, folders, sent_flag = connect_imap(acct)
            try:
                sent = pick_sent_folder(folders, sent_flag)
                typ, _ = imap.append(sent, r"(\Seen)", None, msg.as_bytes())
                sent_copy_saved = typ == "OK"
            finally:
                imap.logout()
        except Exception:
            pass  # sent copy is best-effort; the mail itself went out
    return {"smtp_accepted": True, "sent_copy_saved": sent_copy_saved,
            "refused_recipients": list(refused or {}), "message_id": str(msg["Message-ID"])}


def save_draft_message(acct, to, subject, body, body_html=None):
    import imaplib
    msg = EmailMessage()
    msg["From"] = from_address(acct)
    msg["To"] = to
    msg["Subject"] = subject
    if body_html:
        msg.set_content(body or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body or "")
    imap, folders, _ = connect_imap(acct)
    try:
        drafts = next((f for f in folders if f.lower() in ("drafts",)), "Drafts")
        imap.append(drafts, r"(\Draft)", None, msg.as_bytes())
    finally:
        imap.logout()


def mark_all_read(cfg, folder):
    """Mark every message in a folder as \\Seen. Returns count marked."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        imap.select(folder, readonly=False)
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return 0
        uids = data[0].split()
        imap.store(b",".join(uids), "+FLAGS", r"(\Seen)")
        return len(uids)
    finally:
        imap.logout()


def set_seen(cfg, folder, uid, seen, expected_validity=None):
    """Mark a single message read (seen=True) or unread (seen=False)."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        select_message_folder(imap, folder, expected_validity)
        imap.uid("store", uid, "+FLAGS" if seen else "-FLAGS", r"(\Seen)")
    finally:
        imap.logout()


def save_attachment(cfg, folder, uid, part_index, expected_validity=None):
    """Save one attachment part to ~/Downloads/SimpleMail/. Returns path."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        select_message_folder(imap, folder, expected_validity, readonly=True)
        typ, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            raise RuntimeError("Could not fetch message")
        msg = message_from_bytes(data[0][1], policy=email_policy)
        parts = list(msg.walk())
        part = parts[part_index]
        payload = part.get_payload(decode=True)
        if payload is None:
            raise RuntimeError("Attachment is not decodable")
        name = part.get_filename() or f"attachment-{part_index}"
        out_dir = Path.home() / "Downloads" / "SimpleMail"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / os.path.basename(name)
        out.write_bytes(payload)
        return str(out)
    finally:
        imap.logout()


def delete_message(cfg, folder, uid, expected_validity=None):
    if not expected_validity:
        raise ValueError("Refresh the folder before moving this message")
    imap, folders, _ = connect_imap(cfg)
    try:
        trash = next((item['server'] for item in map_folders(folders) if item['key'] == 'trash'), None)
    finally:
        imap.logout()
    if not trash:
        raise ValueError('No Trash folder is available; choose a folder to move this message')
    if folder == trash:
        raise ValueError('Message is already in Trash; move it to another folder to restore it')
    return move_message(cfg, folder, uid, trash, reason='Moved to Trash by owner', expected_validity=expected_validity)


def move_message(cfg, folder, uid, target, actor='owner', reason='Filed by owner', rule_domain='', expected_validity=None):
    if not expected_validity:
        raise ValueError('Refresh the folder before moving this message')
    from mail_filing import FilingStore, move
    return move(cfg, FilingStore(CONFIG_DIR / 'agent' / 'mailbox.sqlite3'), folder, uid, target,
                connect_imap, actor=actor, reason=reason, rule_domain=rule_domain, expected_validity=expected_validity)


def create_folder(cfg, name):
    """Create an IMAP folder."""
    import imaplib
    imap, _, _ = connect_imap(cfg)
    try:
        typ, data = imap.create(name)
        if typ != "OK":
            raise RuntimeError(data[-1].decode("utf-8", "replace") if data else "create failed")
    finally:
        imap.logout()


def short_sender(sender):
    """'Instagram <posts-recaps@mail.instagram.com>' -> 'Instagram' (or address)."""
    m = re.search(r"^([^<]+?)\s*<.*>", sender)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return sender or "Unknown"


def sender_domain(sender):
    """'Instagram <posts-recaps@mail.instagram.com>' -> 'instagram.com'."""
    m = re.search(r"<([^>]+)>", sender)
    addr = m.group(1) if m else sender
    if "@" not in addr:
        return None
    host = addr.rsplit("@", 1)[1].lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def learn_rule(cfg, account_id, domain, target_folder):
    """Remember: mail from this domain goes to target_folder - for ONE account.
    Rules are per-mailbox so a routing habit on one account can never move
    another account's mail."""
    acct = cfg.account(account_id)
    acct.setdefault("rules", {})[domain] = target_folder
    cfg.save()


def remove_rule(cfg, account_id, domain):
    acct = cfg.account(account_id)
    rules = acct.get("rules", {})
    if domain in rules:
        del rules[domain]
        cfg.save()
        return True
    return False


def apply_rules(cfg, account_id, server_folder, envelopes, validity=None):
    """Move any envelope whose sender domain has a learned rule targeting a
    different folder. Returns (kept_envelopes, moved_count)."""
    acct = cfg.account(account_id)
    rules = acct.get("rules", {})
    if not rules:
        return envelopes, 0
    if server_folder.upper() != 'INBOX':
        return envelopes, 0
    kept, moved = [], 0
    for envelope in envelopes:
        domain = sender_domain(envelope['sender'])
        target = rules.get(domain) if domain else None
        if not target or target == server_folder:
            kept.append(envelope)
            continue
        try:
            result = move_message(acct, server_folder, envelope['uid'], target, actor='rule',
                                  reason='Filed by a saved sender rule', rule_domain=domain, expected_validity=validity)
            if result['status'] == 'moved':
                moved += 1
            else:
                kept.append(envelope)
        except Exception:
            kept.append(envelope)
    return kept, moved


def check_connection(acct):
    """Returns list of (ok: bool, line: str)."""
    results = []
    try:
        imap, folders, _ = connect_imap(acct)
        results.append((True, f"IMAP login OK ({acct['imap_host']}:{acct['imap_port']})"))
        results.append((True, f"Folders: {', '.join(folders[:8]) or '(none listed)'}"))
        imap.logout()
    except Exception as e:
        results.append((False, f"IMAP failed: {e}"))
    try:
        smtp_user, smtp_password = smtp_credentials(acct)
        with smtplib.SMTP(acct["smtp_host"], int(acct["smtp_port"]), timeout=30) as s:
            s.ehlo()
            if acct["smtp_starttls"]:
                s.starttls()
                s.ehlo()
            s.login(smtp_user, smtp_password)
            results.append((True, f"SMTP login OK ({acct['smtp_host']}:{acct['smtp_port']}) as {smtp_user}"))
    except Exception as e:
        results.append((False, f"SMTP failed: {e}"))
    return results


# ---------------------------------------------------------------------------
# Auto-update (GitHub releases)
# ---------------------------------------------------------------------------

def current_arch():
    """Return 'x64' or 'arm64' for release-asset selection."""
    import platform
    m = platform.machine().lower()
    if "arm" in m or "aarch" in m:
        return "arm64"
    return "x64"


def parse_version(s):
    """'v1.2.3' -> (1, 2, 3). Returns None for junk."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(s or ""))
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def check_for_update(timeout=15):
    """Query GitHub for the latest release. Returns dict or None.

    Fields: version (tuple), tag, url, asset_url, asset_name, notes.
    Raises on network/parse errors so the caller can fail quietly.
    """
    import json as _json
    import urllib.request

    url = f"https://api.github.com/repos/{APP_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode("utf-8"))

    tag = data.get("tag_name", "")
    version = parse_version(tag)
    if not version:
        return None

    arch = current_arch()
    asset = None
    for a in data.get("assets", []):
        name = (a.get("name") or "").lower()
        # The console agent shares the release but is never a desktop update.
        if not name.startswith("simplemail-") or not name.endswith(".exe"):
            continue
        if arch == "arm64" and "arm64" in name:
            asset = a
            break
        if arch == "x64" and ("x64" in name or "amd64" in name):
            asset = a
            break

    return {
        "version": version,
        "tag": tag,
        "url": data.get("html_url", ""),
        "asset_url": asset["browser_download_url"] if asset else None,
        "asset_name": asset["name"] if asset else None,
        "notes": data.get("body", "")[:2000],
        "arch": arch,
    }


def download_file(url, dest, timeout=120):
    """Download url to dest with a progress callback (bytes_so_far, total)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)


def _frozen_exe_path():
    """Path of the running .exe when frozen with PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def _staged_exe_path(target):
    """Where the downloaded update is staged, next to the app."""
    return target.with_name(target.stem + ".new" + target.suffix)


def _update_log(target, msg):
    """Append a line to _update_log.txt next to the exe (best effort)."""
    try:
        with open(target.with_name("_update_log.txt"), "a", encoding="utf-8") as fh:
            import datetime
            fh.write(f"{datetime.datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def cleanup_update_leftovers(target=None):
    """Delete staged .new exes and legacy _update.ps1 next to the app.

    Called at startup: after a successful update the staged copy (which ran
    as the helper) can only be removed once it has exited, i.e. by the
    relaunched app. Failures are ignored - retried on the next launch.
    """
    if target is None:
        target = _frozen_exe_path()
    if target is None:
        return
    for leftover in (_staged_exe_path(target), target.with_name("_update.ps1")):
        try:
            leftover.unlink()
        except OSError:
            pass


def apply_update(asset_url):
    """Download the new exe next to the running one and hand over to it.

    DESIGN RULE (learned across v1.0.6-v1.1.4): the OLD app's update code is
    frozen in every already-shipped exe, so any bug in it strands those
    installs forever - they can never update past it. Therefore the old side
    does only the bare minimum (download, sanity-check, launch, confirm) and
    the actual swap is performed by the NEW exe via --finish-update, whose
    code ships fresh with every release and is therefore always fixable.
    """
    import subprocess
    import time

    exe_path = _frozen_exe_path()
    if exe_path is None:
        raise RuntimeError("Updates only work when running the packaged app")

    target = exe_path
    new_exe = _staged_exe_path(target)
    cleanup_update_leftovers(target)

    download_file(asset_url, new_exe)

    # Sanity: a real Windows exe, not a truncated download or an HTML error
    # page saved to disk. MZ magic + a size no onefile build could be under.
    try:
        with open(new_exe, "rb") as fh:
            magic = fh.read(2)
        if magic != b"MZ" or new_exe.stat().st_size < 1_000_000:
            raise RuntimeError(
                f"Downloaded update is not a valid app ({new_exe.stat().st_size} bytes)")
    except Exception:
        try:
            new_exe.unlink()
        except OSError:
            pass
        raise

    # Hand over to the NEW exe. NOTE: DETACHED_PROCESS breaks child console
    # processes in this context (v1.1.4 regression - silently never ran);
    # CREATE_NEW_PROCESS_GROUP alone is enough to survive our os._exit().
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            [str(new_exe), "--finish-update", str(target), str(os.getpid())],
            close_fds=True, creationflags=creationflags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        try:
            new_exe.unlink()
        except OSError:
            pass
        raise RuntimeError(f"Could not launch the downloaded update: {e}")

    # Confirm the helper survived its first seconds before we die: if the
    # download is broken enough to crash on boot, stay alive and report it
    # instead of exiting into nothing.
    time.sleep(3)
    if proc.poll() is not None:
        try:
            new_exe.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"Update helper exited early (code {proc.returncode})")

    _update_log(target, f"handover to {new_exe.name} ok, exiting for swap")
    os._exit(0)


def finish_update(target, old_pid=None, self_path=None, sleep=None, timeout=120):
    """Run inside the NEW exe (--finish-update): swap ourselves in.

    Waits for the old app to release its exe (copying over a running exe
    raises PermissionError - that IS the wait condition, and unlike the old
    Get-Process polling it works whatever the exe file is named), copies this
    binary over it and relaunches. If the swap never succeeds, relaunch the
    old exe so the user is never left with no app at all.
    """
    import shutil
    import subprocess
    import time as _time

    sleep = sleep or _time.sleep
    target = Path(target)
    me = Path(self_path or sys.executable)

    swapped = False
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            shutil.copyfile(me, target)
            swapped = True
            break
        except OSError:
            sleep(1)
    _update_log(target, "swap ok" if swapped
                else f"swap FAILED after {timeout}s (old pid {old_pid} still alive?)")

    # Relaunch. The old app's WebView2 children can outlive it by a couple
    # of seconds and hold the browser-profile lock; an instant relaunch then
    # crashes on startup (seen live on v1.1.5's first field swap). Give them
    # time to let go, and retry if the app dies right after starting.
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    for attempt in range(3):
        sleep(3)
        proc = subprocess.Popen(
            [str(target)], close_fds=True, creationflags=creationflags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        alive = True
        for _ in range(6):
            sleep(1)
            if proc.poll() is not None:
                alive = False
                break
        if alive:
            break
        _update_log(target, f"relaunch attempt {attempt + 1} exited "
                            f"code {proc.returncode}, retrying")
    os._exit(0 if swapped else 1)


# ---------------------------------------------------------------------------
# pywebview JS bridge
# ---------------------------------------------------------------------------

_API_WINDOW = None  # set in main(); kept module-level so pywebview's
# attribute walker doesn't expose it to JS (it would recurse into Window)


class Api:
    def load_message_images(self, account_id, folder, uid, validity=None):
        from mail_images import load_remote_images
        message = self.get_message(account_id, folder, uid, validity)
        return load_remote_images(message.get("html") or "")

    def _cloud_accounts(self):
        if not hasattr(self, '_cloud_provider'):
            from cloud_mail import CloudMail
            self._cloud_provider = CloudAccounts(CloudMail(CONFIG_DIR), self)
        self._cloud_provider.transport._window = _API_WINDOW
        return self._cloud_provider

    def connect_cloud_accounts(self):
        self._cloud_accounts().transport.cloud_login()
        return self._cloud_accounts().discover()

    def __init__(self, cfg):
        self.cfg = cfg
        self._log_path = _BASE_DIR / "api_debug.log"

    def _log(self, msg):
        if not os.environ.get("SIMPLEMAIL_DEBUG"):
            return
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{msg}\n")
        except Exception:
            pass

    def _acct(self, account_id):
        """Resolve an account id to its config. Raises on unknown ids -
        NO fallback account, ever. A fallback is how mail crosses accounts."""
        if str(account_id).startswith('cloud:'):
            provider = self._cloud_accounts()
            if account_id not in provider.accounts:
                provider.discover()
            if account_id not in provider.accounts:
                raise KeyError('Mailbox is not available to this sign-in')
            return provider.accounts[account_id]
        return self.cfg.account(account_id)

    def get_config(self):
        self._log("CALL get_config")
        cfg = self.cfg
        accounts = []
        for acct in cfg.accounts():
            accounts.append({
                "id": acct["id"],
                "label": acct["label"],
                "color": acct.get("color") or "#2563eb",
                "email": acct["email"],
                "password": "",
                "has_password": bool(acct["password"] or "password" in acct.get("_locked_credentials", {})),
                "credential_error": bool(acct.get("_locked_credentials")),
                "password_error": "password" in acct.get("_locked_credentials", {}),
                "smtp_password_error": "smtp_password" in acct.get("_locked_credentials", {}),
                "from_email": acct.get("from_email", ""),
                "identity": from_address(acct),
                "imap_host": acct["imap_host"],
                "imap_port": acct["imap_port"],
                "smtp_host": acct["smtp_host"],
                "smtp_port": acct["smtp_port"],
                "smtp_starttls": acct["smtp_starttls"],
                "smtp_user": acct.get("smtp_user", ""),
                "smtp_password": "",
                "has_smtp_password": bool(acct.get("smtp_password") or "smtp_password" in acct.get("_locked_credentials", {})),
                "signature": acct.get("signature", ""),
                "rules": acct.get("rules", {}),
            })
        cloud_error = None
        cloud_configured = self._cloud_accounts().transport.cloud_config()['configured']
        if cloud_configured:
            try:
                accounts.extend(self._cloud_accounts().discover())
            except (CloudSignInRequired, CloudAccessDenied, CloudConfigurationError,
                    CloudServiceError, CloudRequestError) as exc:
                cloud_error = str(exc)
            except Exception:
                cloud_error = 'Employee mailboxes are unavailable. Please try again.'
        return {
            "cloud_configured": cloud_configured,
            "cloud_error": cloud_error,
            "accounts": accounts,
            "active_account": cfg.data.get("active_account", ""),
            "ui_scale": cfg["ui_scale"],
            "version": APP_VERSION,
            "arch": current_arch(),
        }

    @route_cloud
    def get_folders(self, account_id):
        """Folder list + unread badges for ONE account."""
        self._log(f"CALL get_folders {account_id}")
        acct = self._acct(account_id)
        folders = []
        error = None
        if acct["email"] and acct["password"]:
            try:
                imap, server_folders, _ = connect_imap(acct)
                try:
                    folders = map_folders(server_folders)
                    for f in folders:
                        f["unread"] = count_unread(imap, f["server"]) or 0
                finally:
                    imap.logout()
            except Exception as e:
                folders = [{"key": "inbox", "name": "Inbox", "server": "INBOX", "unread": 0}]
                error = str(e)
        if not any(folder["key"] == "drafts" for folder in folders):
            folders.insert(min(2, len(folders)), {"key": "drafts", "name": "Drafts", "server": None, "unread": 0})
        return {"folders": folders, "error": error}

    def set_active_account(self, account_id):
        self._log(f"CALL set_active_account {account_id}")
        self._acct(account_id)  # validate
        self.cfg["active_account"] = account_id
        self.cfg.save()
        return {"ok": True}

    def save_config(self, data):
        self._log("CALL save_config")
        incoming = data.get("accounts", [])
        # Preserve learned rules across saves: the settings UI doesn't edit
        # them, so merge each account's stored rules back in by id.
        existing_rules = {a["id"]: a.get("rules", {}) for a in self.cfg.accounts()}
        accounts = []
        seen_ids = set()
        for raw in incoming:
            if str(raw.get("id", "")).startswith("cloud:") or raw.get("provider") == "cloud":
                continue
            acct = normalize_account(raw)
            while acct["id"] in seen_ids:  # keep ids unique
                acct["id"] += "-2"
            seen_ids.add(acct["id"])
            acct = self._retain_credentials(acct, raw)
            if "rules" not in raw or not raw.get("rules"):
                acct["rules"] = existing_rules.get(acct["id"], {})
            accounts.append(acct)
        active = data.get("active_account", "")
        if not any(a["id"] == active for a in accounts):
            active = accounts[0]["id"] if accounts else ""
        updated = dict(self.cfg.data)
        updated["accounts"] = accounts
        updated["active_account"] = active
        if data.get("ui_scale") in ("compact", "default", "large"):
            updated["ui_scale"] = data["ui_scale"]
        previous = self.cfg.data
        self.cfg.data = updated
        try:
            self.cfg.save()
        except Exception:
            self.cfg.data = previous
            raise
        return {"ok": True, "accounts": [a["id"] for a in accounts], "active_account": active}

    def test_connection(self, data):
        self._log("CALL test_connection")
        acct = self._retain_credentials(normalize_account(data or {}), data or {})
        results = check_connection(acct)
        ok = all(r[0] for r in results)
        return {"ok": ok, "error": None if ok else "; ".join(l for okk, l in results if not okk)}

    def _retain_credentials(self, acct, raw):
        for field in ("password", "smtp_password"):
            if not isinstance(acct[field], str) or not isinstance(raw.get("clear_" + field, False), bool):
                raise ValueError("Invalid password input")
        try:
            stored = self.cfg.account(acct["id"])
        except KeyError:
            return acct
        locked = {}
        for field, connection in (("password", ("email", "imap_host", "imap_port")),
                                  ("smtp_password", ("smtp_host", "smtp_port", "smtp_user"))):
            if raw.get("clear_" + field):
                acct[field] = ""
                continue
            if acct[field]:
                continue
            if any(acct.get(key) != stored.get(key) for key in connection) and (
                    stored.get(field) or field in stored.get("_locked_credentials", {})):
                raise ValueError("Enter a new password after changing the mailbox or server")
            acct[field] = stored.get(field, "")
            if field in stored.get("_locked_credentials", {}):
                locked[field] = stored["_locked_credentials"][field]
        if locked:
            acct["_locked_credentials"] = locked
        return acct

    @route_cloud
    def list_messages(self, account_id, server_folder):
        self._log(f"CALL list_messages {account_id} {server_folder}")
        acct = self._acct(account_id)
        imap, _, _ = connect_imap(acct)
        try:
            envelopes = fetch_envelopes(imap, server_folder, int(self.cfg["max_messages"]))
            validity = selected_validity(imap)
            folder_unread = count_unread(imap, server_folder)
            inbox_unread = folder_unread if server_folder.upper() == "INBOX" else count_unread(imap, "INBOX")
        finally:
            imap.logout()
        envelopes, moved = apply_rules(self.cfg, account_id, server_folder, envelopes, validity)
        if moved:
            # re-open a fresh connection; apply_rules used its own
            imap2, _, _ = connect_imap(acct)
            try:
                envelopes = fetch_envelopes(imap2, server_folder, int(self.cfg["max_messages"]))
                validity = selected_validity(imap2)
                envelopes, _ = apply_rules(self.cfg, account_id, server_folder, envelopes, validity)
                folder_unread = count_unread(imap2, server_folder)
                inbox_unread = folder_unread if server_folder.upper() == "INBOX" else count_unread(imap2, "INBOX")
            finally:
                imap2.logout()
        unread = sum(1 for e in envelopes if not e["seen"])
        return {
            "envelopes": envelopes,
            "unread": unread,                  # unseen among the loaded page
            "folder_unread": folder_unread,    # whole folder, per the server
            "inbox_unread": inbox_unread,
            "auto_moved": moved, "validity": validity,
        }

    @route_cloud
    def search_messages(self, account_id, query, cursor=None, server_folder=None):
        from mailbox_agent import MailboxAgent, unpack
        self._acct(account_id)
        owner = MailboxAgent(self.cfg, self._agent_store(), account_id, owner_access=True)
        page = (owner.search(server_folder, query, cursor, 25) if server_folder else
                owner.search_all(query, cursor, 25))
        items = []
        for item in page['items']:
            ref = unpack(item['message_ref'])
            items.append({**item, 'uid': 'search:' + item['message_ref'],
                          'server_uid': ref['uid'], 'server_folder': ref['folder'],
                          'validity': ref['validity'], 'snippet': ref['folder']})
        # Human drafts are local, so IMAP cannot find them. Include them once.
        if not cursor and (server_folder is None or server_folder.lower() == 'drafts'):
            needle = query.casefold().strip()
            for draft in self._compose_store().list(account_id):
                payload = draft['payload']
                if needle and needle not in '\n'.join(str(payload.get(key, '')) for key in
                                                     ('to', 'cc', 'bcc', 'subject', 'body')).casefold():
                    continue
                items.insert(0, {'uid':'local:' + draft['id'], 'localDraftId':draft['id'],
                                  'sender':'Draft · ' + (payload.get('to') or 'No recipient'),
                                  'subject':payload.get('subject') or '(no subject)', 'date':draft['updated_at'],
                                  'seen':True, 'snippet':'Drafts · Saved on this device'})
        return {**page, 'items':items}

    @route_cloud
    def get_message(self, account_id, server_folder, uid, validity=None):
        self._log(f"CALL get_message {account_id} {server_folder} {uid}")
        acct = self._acct(account_id)
        imap, _, _ = connect_imap(acct)
        try:
            result = fetch_message(imap, server_folder, uid, validity)
            result["message_ref"] = None
            try:
                from mailbox_agent import pack
                folder_identity = validity or selected_validity(imap)
                if folder_identity:
                    result["message_ref"] = pack({"account": account_id, "folder": server_folder,
                                                  "validity": folder_identity, "uid": str(uid)})
            except (ValueError, TypeError):
                pass
            return result
        finally:
            imap.logout()

    def get_conversation(self, account_id, message_ref, cursor=None):
        from mailbox_agent import MailboxAgent
        self._acct(account_id)
        owner = MailboxAgent(self.cfg, self._agent_store(), account_id, owner_access=True)
        return owner.thread(message_ref, cursor=cursor, limit=5, max_chars=10000)

    @route_cloud
    def send_mail(self, account_id, to, subject, body, body_html=None):
        """Send as the given account. The From identity comes from the
        account config alone - there is deliberately no from parameter."""
        self._log(f"CALL send_mail {account_id} to={to} subject={subject[:40]}")
        acct = self._acct(account_id)
        result = send_message(acct, to, subject, body, body_html=body_html)
        return {"ok": True, "sent_as": from_address(acct), **result}

    def _agent_store(self):
        from agent_store import AgentStore
        return AgentStore(CONFIG_DIR / "agent" / "mailbox.sqlite3")

    def _compose_store(self):
        from compose_store import ComposeStore
        return ComposeStore(CONFIG_DIR / "agent" / "mailbox.sqlite3")

    def _attachment_store(self):
        from mail_attachments import AttachmentStore
        return AttachmentStore(CONFIG_DIR / "agent" / "mailbox.sqlite3")

    def add_compose_attachment(self, account_id, name, encoded):
        self._acct(account_id)
        return self._attachment_store().add_base64(account_id, name, encoded)

    @route_cloud
    def get_compose_context(self, account_id, server_folder, uid, reply_all=False, forward=False, validity=None):
        from mail_attachments import reply_context, MAX_TOTAL
        from mailbox_agent import clean_string
        acct = self._acct(account_id)
        clean_string(server_folder, "folder", 500)
        if not isinstance(uid, str) or not uid.isdigit():
            raise ValueError("Invalid message UID")
        imap, _, _ = connect_imap(acct)
        try:
            select_message_folder(imap, server_folder, validity, readonly=True)
            typ, data = imap.uid("fetch", uid, "(RFC822.SIZE)")
            size = re.search(rb'RFC822.SIZE (\d+)', b' '.join(item for item in data or [] if isinstance(item, bytes)))
            if typ != "OK" or not size or int(size[1]) > 30 * 1024 * 1024:
                raise ValueError("Message is unavailable or too large to quote (30 MB limit)")
            typ, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((item[1] for item in data or [] if isinstance(item, tuple)), None)
            if typ != "OK" or raw is None or len(raw) > 30 * 1024 * 1024:
                raise ValueError("Message could not be read")
            msg = message_from_bytes(raw, policy=email_policy)
        finally:
            imap.logout()
        text, _ = extract_bodies(msg)
        attachments = []
        if forward:
            parts = [part for part in msg.walk() if part.get_filename()]
            contents = [(part, part.get_payload(decode=True) or b'') for part in parts]
            if sum(len(content) for _, content in contents) > MAX_TOTAL:
                raise ValueError("Forwarded attachments exceed the 20 MB limit")
            store = self._attachment_store()
            for part, content in contents:
                name = (part.get_filename() or 'attachment').replace('\\', '/').rsplit('/', 1)[-1]
                attachments.append(store.add(account_id, name, content))
        context = {} if forward else reply_context(msg, [acct["email"], from_address(acct)], reply_all)
        return {**context, "subject": str(msg.get("Subject", "")), "sender": str(msg.get("From", "")),
                "date": str(msg.get("Date", "")), "text": text, "attachments": attachments}

    def save_compose_draft(self, account_id, draft_id, revision, to, subject, body, body_html,
                           cc="", bcc="", in_reply_to="", references="", attachment_ids=None):
        from mailbox_agent import clean_string
        self._acct(account_id)
        clean_string(to, "recipients", 4000)
        clean_string(subject, "subject", 1000)
        for value, name in ((cc, "CC"), (bcc, "BCC"), (in_reply_to, "reply header"), (references, "references")):
            clean_string(value, name, 10000)
        attachment_ids = attachment_ids or []
        if attachment_ids:
            self._attachment_store().resolve(account_id, attachment_ids)
        if any(not isinstance(value, str) or len(value) > 500000 or '\x00' in value
               for value in (body, body_html)):
            raise ValueError("Invalid draft body")
        return self._compose_store().save(account_id, draft_id, revision,
                {"to": to, "subject": subject, "body": body, "body_html": body_html,
                 "cc": cc, "bcc": bcc, "in_reply_to": in_reply_to, "references": references,
                 "attachment_ids": attachment_ids})

    def list_compose_drafts(self, account_id):
        self._acct(account_id)
        return self._compose_store().list(account_id)

    def get_compose_draft(self, account_id, draft_id):
        self._acct(account_id)
        draft = self._compose_store().get(account_id, draft_id)
        items = self._attachment_store().resolve(account_id, draft["payload"].get("attachment_ids", []))
        draft["attachments"] = [{"id": item["id"], "name": item["name"], "size": len(item["data"])} for item in items]
        return draft

    def discard_compose_draft(self, account_id, draft_id, revision):
        self._acct(account_id)
        return self._compose_store().discard(account_id, draft_id, revision)

    @route_cloud
    def send_compose_draft(self, account_id, draft_id, revision):
        from mailbox_agent import recipients
        acct = self._acct(account_id)
        store = self._compose_store()
        payload = store.get(account_id, draft_id)["payload"]
        if not payload["to"].strip():
            raise ValueError("Enter a recipient before sending")
        recipients(payload["to"])
        recipients(payload.get("cc", ""))
        recipients(payload.get("bcc", ""))
        attachments = self._attachment_store().resolve(account_id, payload["attachment_ids"]) if payload.get("attachment_ids") else []
        draft = store.claim(account_id, draft_id, revision)
        payload = draft["payload"]
        try:
            result = send_message(acct, payload["to"], payload["subject"] or "(no subject)",
                                  payload["body"], body_html=payload["body_html"], cc=payload.get("cc", ""),
                                  bcc=payload.get("bcc", ""), in_reply_to=payload.get("in_reply_to", ""),
                                  references=payload.get("references", ""), attachments=attachments)
        except Exception:
            detail = {"warning": "Delivery could not be confirmed. Check the mailbox before sending again. "
                      "This draft will not be retried automatically."}
            store.finish(account_id, draft_id, "uncertain", detail)
            return {"status": "uncertain", **detail}
        store.finish(account_id, draft_id, "sent", result)
        return {"status": "sent", "sent_as": from_address(acct), **result}

    def get_agent_state(self, account_id):
        self._acct(account_id)
        store = self._agent_store()
        drafts = self.list_agent_drafts(account_id)
        return {"profile": store.profile(account_id), "drafts": drafts,
                "activity": store.activity(account_id), "model": self.get_model_state(account_id),
                "reviews": self.list_agent_reviews(account_id), "filings": self.list_filing_actions(account_id)}

    def _review_queue(self):
        from work_queue import WorkQueue
        return WorkQueue(CONFIG_DIR / "agent" / "mailbox.sqlite3")

    def list_agent_reviews(self, account_id, cursor=None):
        self._acct(account_id)
        return self._review_queue().owner_reviews(account_id, cursor)

    def get_agent_review(self, account_id, work_id):
        from mailbox_agent import MailboxAgent
        self._acct(account_id)
        queue = self._review_queue()
        item = queue.owner_work(account_id, work_id)
        mailbox = MailboxAgent(self.cfg, queue, account_id, owner_access=True)
        try:
            message = mailbox.read(item['message_ref'], 50000)
        except Exception:
            # Moved/deleted mail and connection failures must still be resolvable by the owner.
            message = {**item['headers'], 'text': 'Message unavailable. Check the mailbox connection or whether it moved.',
                       'unavailable': True, 'truncated': False}
        return {"work": item, "message": message,
                "conversation": queue.conversation(account_id, item['message_ref'])}

    def resolve_agent_review(self, account_id, work_id, updated_at, action):
        self._acct(account_id)
        return self._review_queue().owner_resolve(account_id, work_id, updated_at, action)

    def _model_control(self):
        from model_control import ModelControl
        return ModelControl(CONFIG_DIR / "agent" / "mailbox.sqlite3")

    def get_model_state(self, account_id):
        self._acct(account_id)
        return self._model_control().state(account_id)

    def save_model_connection(self, account_id, endpoint, model, api, key="", clear_key=False):
        self._acct(account_id)
        return self._model_control().save(account_id, endpoint, model, api, key, clear_key)

    def start_model_worker(self, account_id):
        self._acct(account_id)
        return self._model_control().start(account_id)

    def save_agent_settings(self, account_id, enabled, job, mode=None, allowed_recipients=None, allow_filing=None):
        self._acct(account_id)
        if enabled and not job.strip():
            raise ValueError("Write the agent's job first")
        profile = self._agent_store().set_profile(account_id, enabled, job, mode, allowed_recipients, allow_filing)
        if not enabled:
            self._model_control().stop(account_id)
        return profile

    def list_agent_drafts(self, account_id, cursor=None):
        self._acct(account_id)
        drafts = self._agent_store().list_drafts(account_id, cursor)
        for draft in drafts["items"]:
            ids = draft["payload"].get("attachment_ids", [])
            items = self._attachment_store().resolve(account_id, ids) if ids else []
            draft["attachments"] = [{"name": item["name"], "size": len(item["data"])} for item in items]
            if draft['payload'].get('reply_ref'):
                draft['conversation'] = self._review_queue().conversation(account_id, draft['payload']['reply_ref'])
        return drafts

    def list_agent_activity(self, account_id, cursor=None):
        self._acct(account_id)
        return self._agent_store().activity(account_id, cursor)

    def edit_agent_draft(self, account_id, draft_id, revision, to, subject, body, cc="", bcc=""):
        from mailbox_agent import clean_string, recipients
        self._acct(account_id)
        store = self._agent_store()
        draft = store.get_draft(account_id, draft_id)
        payload = draft["payload"]
        payload.update(to=recipients(to), subject=clean_string(subject, "subject", 1000),
                       cc=recipients(cc), bcc=recipients(bcc))
        if not isinstance(body, str) or len(body) > 200000 or '\x00' in body:
            raise ValueError("Invalid message body")
        payload["body"] = body
        return store.save_draft(account_id, draft["request_key"], payload, draft_id, revision)

    def dismiss_agent_draft(self, account_id, draft_id, revision):
        self._acct(account_id)
        return self._agent_store().dismiss_draft(account_id, draft_id, revision)

    def send_agent_draft(self, account_id, draft_id, revision):
        """Owner-only GUI action. Never exposed through the MCP tool server."""
        from email.utils import make_msgid
        acct = self._acct(account_id)
        store = self._agent_store()
        candidate = store.get_draft(account_id, draft_id)
        if not candidate["payload"]["to"].strip():
            raise ValueError("Enter a recipient before sending")
        ids = candidate["payload"].get("attachment_ids", [])
        attachments = self._attachment_store().resolve(account_id, ids) if ids else []
        draft = store.claim_send(account_id, draft_id, revision)
        payload = draft["payload"]
        message_id = make_msgid()
        try:
            result = send_message(acct, payload["to"], payload["subject"], payload["body"],
                                  cc=payload.get("cc", ""), bcc=payload.get("bcc", ""),
                                  in_reply_to=payload.get("in_reply_to", ""),
                                  references=payload.get("references", ""), message_id=message_id, attachments=attachments)
        except Exception:
            detail = {"message_id": message_id, "warning": "Delivery could not be confirmed. "
                      "Check the mailbox before sending again; this draft will not be retried automatically."}
            store.finish_send(account_id, draft_id, "uncertain", detail)
            return {"status": "uncertain", **detail}
        store.finish_send(account_id, draft_id, "sent", result)
        return {"status": "sent", "sent_as": from_address(acct), **result}

    def save_draft(self, account_id, to, subject, body, body_html=None):
        self._log(f"CALL save_draft {account_id} subject={subject[:40]}")
        acct = self._acct(account_id)
        save_draft_message(acct, to, subject, body, body_html=body_html)
        return {"ok": True}

    @route_cloud
    def mark_all_read(self, account_id, server_folder):
        self._log(f"CALL mark_all_read {account_id} {server_folder}")
        count = mark_all_read(self._acct(account_id), server_folder)
        return {"count": count}

    @route_cloud
    def set_seen(self, account_id, server_folder, uid, seen, validity=None):
        self._log(f"CALL set_seen {account_id} {server_folder} {uid} seen={seen}")
        set_seen(self._acct(account_id), server_folder, uid, bool(seen), validity)
        return {"ok": True}

    @route_cloud
    def save_attachment(self, account_id, server_folder, uid, part_index, validity=None):
        self._log(f"CALL save_attachment {account_id} {server_folder} {uid} part={part_index}")
        path = save_attachment(self._acct(account_id), server_folder, uid, int(part_index), validity)
        return {"path": path}

    def pick_image(self):
        """Open a file dialog for an image; return {name, data_uri} or None."""
        self._log("CALL pick_image")
        import base64
        import mimetypes
        global _API_WINDOW
        win = _API_WINDOW
        if win is None:
            raise RuntimeError("No window available")
        result = win.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp)",),
        )
        if not result:
            return None
        path = Path(result[0])
        if not path.exists():
            raise RuntimeError("Selected file not found")
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        data_uri = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return {"name": path.name, "data_uri": data_uri}

    @route_cloud
    def delete_message(self, account_id, server_folder, uid, validity=None):
        self._log(f"CALL delete_message {account_id} {server_folder} {uid}")
        return delete_message(self._acct(account_id), server_folder, uid, validity)

    @route_cloud
    def move_message(self, account_id, server_folder, uid, target, sender="", learn=False, validity=None):
        self._log(f"CALL move_message {account_id} {server_folder} {uid} -> {target} learn={learn}")
        domain = sender_domain(sender) if learn and sender else None
        result = move_message(self._acct(account_id), server_folder, uid, target, rule_domain=domain or "", expected_validity=validity)
        learned = None
        if learn and result["status"] == "moved":
            dom = domain
            if dom:
                learn_rule(self.cfg, account_id, dom, target)
                learned = dom
        return {**result, "learned": learned}

    def _filing_store(self):
        from mail_filing import FilingStore
        return FilingStore(CONFIG_DIR / 'agent' / 'mailbox.sqlite3')

    def list_filing_actions(self, account_id, cursor=None):
        self._acct(account_id)
        return self._filing_store().list(account_id, cursor)

    def undo_filing(self, account_id, action_id):
        from mail_filing import undo
        account = self._acct(account_id)
        store = self._filing_store()
        action = store.get(account_id, action_id)
        if action['status'] != 'moved':
            raise ValueError('Only a confirmed move can be undone')
        domain = action['rule_domain']
        if domain and account.get('rules', {}).get(domain) == action['target']:
            remove_rule(self.cfg, account_id, domain)
        return undo(account, store, action_id, connect_imap)

    @route_cloud
    def create_folder(self, account_id, name):
        self._log(f"CALL create_folder {account_id} {name}")
        create_folder(self._acct(account_id), name)
        return {"ok": True}

    def remove_rule(self, account_id, domain):
        self._log(f"CALL remove_rule {account_id} {domain}")
        removed = remove_rule(self.cfg, account_id, domain)
        return {"ok": removed}

    def learn_rule(self, account_id, domain, target):
        self._log(f"CALL learn_rule {account_id} {domain} -> {target}")
        learn_rule(self.cfg, account_id, domain, target)
        return {"ok": True}

    def check_update(self):
        """Return {available, version, notes, asset_name, url} or error."""
        self._log("CALL check_update")
        try:
            info = check_for_update()
        except Exception as e:
            return {"available": False, "error": str(e)}
        if not info or not info["asset_url"]:
            arch = info["arch"] if info else current_arch()
            return {"available": False,
                    "error": f"No {arch} build published for this release yet"}
        local = parse_version(APP_VERSION)
        remote = info["version"]
        available = bool(local and remote and remote > local)
        return {
            "available": available,
            "local_version": APP_VERSION,
            "new_version": info["tag"],
            "notes": info.get("notes", ""),
            "asset_name": info.get("asset_name"),
            "url": info.get("url"),
            "arch": info.get("arch"),
        }

    def apply_update(self):
        """Download the new exe and self-replace (process exits)."""
        self._log("CALL apply_update")
        info = check_for_update()
        if not info or not info["asset_url"]:
            raise RuntimeError("No update available")
        local = parse_version(APP_VERSION)
        if not (local and info["version"] > local):
            raise RuntimeError("Already up to date")
        apply_update(info["asset_url"])
        return {"ok": True}


# ---------------------------------------------------------------------------
# Native Windows notifications (toasts, like Outlook)
# ---------------------------------------------------------------------------

def _xml_escape(s):
    return (
        str(s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def show_toast(title, body):
    """Fire a native Windows toast notification via the WinRT API.

    Uses PowerShell (always present on Windows, ARM64-native) so we need no
    extra Python deps. The toast appears in the Action Center exactly like
    Outlook's - clickable, respects Do Not Disturb, etc.
    """
    if os.name != "nt":
        return
    title = _xml_escape(title)[:120]
    body = _xml_escape(body)[:300]
    xml = (
        '<toast activationType="foreground">'
        '<visual><binding template="ToastGeneric">'
        f"<text>{title}</text><text>{body}</text>"
        "</binding></visual></toast>"
    )
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        f"$xml.LoadXml('{xml}'); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('SimpleMail.App').Show("
        "[Windows.UI.Notifications.ToastNotification]::new($xml))"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


class MailPoller(threading.Thread):
    """Poll the inbox for new mail and raise native toasts.

    Runs every POLL_SECONDS; tracks the highest UID seen so only *new*
    arrivals notify. Skips senders that learned rules would auto-route
    elsewhere (they're not "new business mail").
    """

    POLL_SECONDS = 30
    LAST_UID_KEY = "poll_last_uid"

    def __init__(self, acct):
        super().__init__(daemon=True)
        self.acct = acct
        self._last_uid = None
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        # initial pass: just record where we are - don't notify for the
        # existing backlog
        self._snapshot_uid()
        while not self._stop_event.wait(self.POLL_SECONDS):
            try:
                if self._last_uid is None:
                    self._snapshot_uid()
                else:
                    self._check_once()
            except Exception:
                pass  # transient IMAP/network errors are fine

    def _snapshot_uid(self):
        try:
            imap, _, _ = connect_imap(self.acct)
            try:
                imap.select("INBOX", readonly=True)
                typ, data = imap.uid("search", None, "ALL")
                if typ == "OK":
                    self._last_uid = max((int(uid) for uid in (data[0] or b"").split()), default=0) if data else 0
            finally:
                imap.logout()
        except Exception:
            pass

    def _check_once(self):
        imap, _, _ = connect_imap(self.acct)
        try:
            imap.select("INBOX", readonly=True)
            if self._last_uid is None:
                return  # not armed yet
            typ, data = imap.uid("search", None, f"UID {self._last_uid + 1}:*")
            if typ != "OK" or not data or not data[0]:
                return
            # IMAP ranges are inclusive in either direction: n:* can return
            # the previous maximum UID even when nothing new has arrived.
            new_uids = [int(x) for x in data[0].split() if int(x) > self._last_uid]
            if not new_uids:
                return
            uid_list = ",".join(str(u) for u in new_uids)
            typ, fdata = imap.uid(
                "fetch", uid_list, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)] FLAGS)"
            )
            if typ != "OK" or not fdata:
                return
            seen_uids = set()
            for item in fdata:
                if not isinstance(item, tuple):
                    continue
                desc = item[0].decode("utf-8", "replace")
                payload = item[1]
                m = re.search(r"UID (\d+)", desc)
                if not m:
                    continue
                uid = int(m.group(1))
                seen_uids.add(uid)
                if uid <= self._last_uid:
                    continue
                msg = message_from_bytes(payload, policy=email_policy)
                sender = str(msg.get("From", "Unknown"))
                subject = str(msg.get("Subject", "(no subject)"))
                domain = sender_domain(sender)
                rule_target = self.acct.get("rules", {}).get(domain)
                if rule_target and rule_target != "INBOX":
                    continue  # learned rule routes this elsewhere
                label = self.acct.get("label") or self.acct.get("email") or ""
                show_toast(f"[{label}] {subject}" if label else subject, short_sender(sender))
            if seen_uids:
                self._last_uid = max(seen_uids)
        finally:
            imap.logout()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def main():
    # Update helper mode: this process IS the freshly downloaded exe, asked
    # by the old app to swap itself in. Must run before config/GUI setup.
    if len(sys.argv) >= 3 and sys.argv[1] == "--finish-update":
        finish_update(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        return 0  # unreachable - finish_update never returns

    # Normal start: clear leftovers from a completed (or failed) update.
    cleanup_update_leftovers()

    cfg = Config()

    if "--check" in sys.argv:
        print(f"Config: {CONFIG_FILE}")
        if not cfg.accounts():
            print("Account: (not set - run the app and enter credentials)")
            return 1
        all_ok = True
        for acct in cfg.accounts():
            print(f"Account: {acct['label']} <{from_address(acct)}> (mailbox {acct['email']})")
            results = check_connection(acct)
            for ok, line in results:
                print(("  OK   " if ok else "  FAIL ") + line)
            all_ok = all_ok and all(ok for ok, _ in results)
        return 0 if all_ok else 1

    # Taskbar identity: without an AppUserModelID the icon won't pin/group
    # properly from a PyInstaller onefile build.
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SimpleMail.App")
        except Exception:
            pass

    if webview is None:
        print("pywebview is not available:", _WEBVIEW_IMPORT_ERROR)
        print("Install it with:  py -3 -m pip install pywebview==5.3.2 pythonnet==3.0.5")
        return 1

    # Serve the frontend over localhost (avoids file:// quirks in WebView2)
    try:
        import bottle
    except ImportError:
        bottle = None

    index_html = (_BASE_DIR / "web" / "index.html").resolve()
    web_dir = (_BASE_DIR / "web").resolve()

    if bottle is not None:
        @bottle.get("/")
        def _index():
            return bottle.static_file("index.html", root=str(web_dir))

        @bottle.get("/<filename:path>")
        def _static(filename):
            return bottle.static_file(filename, root=str(web_dir))

        server = bottle.Bottle()
        # re-register on the bottle instance (module-level decorators attach to default app)
        server.route("/", "GET", _index)
        server.route("/<filename:path>", "GET", _static)
        port = 17591
        threading.Thread(
            target=lambda: server.run(host="127.0.0.1", port=port, quiet=True),
            daemon=True,
        ).start()
        url = f"http://127.0.0.1:{port}"
    else:
        url = str(index_html)

    api = Api(cfg)
    icon_path = _BASE_DIR / "assets" / "icon.ico"
    placement_file = CONFIG_DIR / "window.json"
    placement = load_placement(placement_file)

    # Native inbox notifications (like Outlook): one quiet poller per account,
    # each toast labelled with its mailbox so arrivals are never ambiguous.
    pollers = []
    for acct in cfg.accounts():
        if acct["email"] and acct["password"]:
            poller = MailPoller(acct)
            poller.start()
            pollers.append(poller)

    # GUI startup can fail transiently right after an update swap: the old
    # instance's WebView2 children hold the browser-profile lock for a couple
    # of seconds after it exits. Retry once instead of dying into the
    # "Unhandled exception in script" dialog.
    import time as _time
    try:
        for attempt in (1, 2):
            try:
                webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
                window = webview.create_window(
                    f"SimpleMail v{APP_VERSION}",
                    url=url,
                    js_api=api,
                    text_select=True,
                    width=1240,
                    height=800,
                    maximized=placement["maximized"] if placement else True,
                    min_size=(980, 620),
                    background_color="#f6f8fb",
                )
                global _API_WINDOW
                _API_WINDOW = window  # lets Api.pick_image open a file dialog
                if os.name == "nt":
                    window.events.shown += lambda: remember_window(window, placement_file, placement)
                # Title-bar icon (pywebview 5.x has no icon kwarg; set it on
                # the native form)
                if os.name == "nt" and icon_path.exists():
                    try:
                        from System.Drawing import Icon as NetIcon  # type: ignore
                        window.native.Icon = NetIcon(str(icon_path))
                    except Exception:
                        pass
                webview.start()
                break
            except Exception:
                if attempt == 2:
                    raise
                _time.sleep(4)
    finally:
        for poller in pollers:
            poller.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
