"""Find the real mail buried in Junk and rescue it.

Each message is judged by its sender ADDRESS, never the display name (spam
fakes those: "TVLicensing UK" from a random domain). People the owner has
written to and senders he has trusted are real. Replies in a conversation and
personal addresses are probably real, unless the address looks machine-made.
Everything else stays where it is. Rescued mail goes to its own folder, so it
cannot get lost again in a busy Inbox, and every move has Undo in Activity.
"""
import re
from email import message_from_bytes
from email.policy import compat32
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr

RESCUE_FOLDER = "Rescued"
SCAN_LIMIT = 1000
BATCH = 300
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com",
    "live.co.uk", "msn.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "mac.com",
    "sky.com", "btinternet.com", "virginmedia.com", "talktalk.net", "aol.com", "protonmail.com",
    "proton.me",
}


def text(value):
    """A header as readable text (RFC 2047 words decoded, folds removed)."""
    if value is None:
        return ""
    try:
        value = str(make_header(decode_header(str(value))))
    except Exception:
        value = str(value)
    return re.sub(r"\s+", " ", value).strip()


def address(from_header):
    addr = parseaddr(text(from_header))[1].strip()
    return addr if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr) else ""


def domain(addr):
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def looks_random(addr):
    """Throwaway sender addresses (case as sent): emoji or non-ASCII domains, long vowel-less
    or digit-stuffed names (avbeefjyzqcwwr.com, 0b4a1rd70yepbxb7l.com) and
    shouty 8-character mailbox names (GM55IBND@...)."""
    if "@" not in addr:
        return True
    local, host = addr.rsplit("@", 1)
    host = host.lower()
    if not host.isascii() or not local.isascii():
        return True
    labels = host.split(".")
    if len(labels) < 2:
        return True
    for label in labels[:-1]:
        letters = [c for c in label if c.isalpha()]
        digits = sum(c.isdigit() for c in label)
        vowels = sum(c in "aeiou" for c in letters)
        if len(label) >= 12 and (digits >= 2 or (letters and vowels / len(letters) < 0.25)):
            return True
        if len(label) >= 9 and digits >= 3:
            return True
    return bool(re.fullmatch(r"[A-Z0-9]{8}", local) and any(c.isdigit() for c in local)
                and re.search(r"[A-Z]", local))


def trusted_match(addr, trusted):
    """trusted holds addresses (sarah@sky.com) and domains (distrokid.com);
    a domain also covers its subdomains (mail.distrokid.com)."""
    addr, host = addr.lower(), domain(addr)
    for entry in trusted:
        entry = str(entry).strip().lower()
        if not entry:
            continue
        if "@" in entry:
            if addr == entry:
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def classify(addr, subject="", in_reply_to="", list_unsubscribe="", known=(), trusted=()):
    """('real' | 'maybe' | None, reason). Strong signals only; spam never wins
    on its display name or subject."""
    if not addr:
        return None, ""
    if addr.lower() in known:
        return "real", "You've emailed them"
    if trusted_match(addr, trusted):
        return "real", "A sender you trust"
    if looks_random(addr):
        return None, ""
    if in_reply_to and re.match(r"\s*(re|aw|sv)\s*:", subject or "", re.I):
        return "maybe", "Reply in a conversation"
    if domain(addr) in PERSONAL_DOMAINS and not list_unsubscribe:
        return "maybe", "Personal email address"
    return None, ""


def _quote(folder):
    from mail_filing import quote
    return quote(folder)


def _headers(imap, uids, fields):
    """[(uid, message)] for uids. Outlook.com puts the UID after the header
    payload, so responses are grouped by imap_fetch rather than read inline."""
    from imap_fetch import fetch_groups
    out = []
    for start in range(0, len(uids), BATCH):
        chunk = b",".join(uids[start:start + BATCH])
        typ, data = imap.uid("FETCH", chunk, f"(UID BODY.PEEK[HEADER.FIELDS ({fields})])")
        if typ != "OK":
            continue
        for uid, _, parts in fetch_groups(data):
            if uid is not None and parts:
                out.append((str(uid), message_from_bytes(parts[0][1], policy=compat32)))
    return out


def known_senders(imap, sent_folder, limit=3000):
    """Everyone the owner has written to, from the Sent folder."""
    known = set()
    if not sent_folder:
        return known
    typ, _ = imap.select(_quote(sent_folder), readonly=True)
    if typ != "OK":
        return known
    typ, data = imap.uid("SEARCH", None, "ALL")
    uids = data[0].split()[-limit:] if typ == "OK" and data and data[0] else []
    for _, msg in _headers(imap, uids, "TO CC BCC"):
        # One field per call: an empty or malformed field makes Python's strict
        # parser drop every address it was given alongside it.
        values = [text(msg.get(field)) for field in ("To", "Cc", "Bcc")]
        for _, addr in (pair for value in values if value for pair in getaddresses([value])):
            addr = addr.strip().lower()
            if "@" in addr:
                known.add(addr)
    return known


def scan(imap, folder, known, trusted, limit=SCAN_LIMIT, own=()):
    """Real and maybe-real messages among the newest `limit` in folder.
    Mail claiming to be from the mailbox itself (`own`) is never rescued:
    "we hacked your account" spam forges exactly that.
    Returns (validity, items), newest first."""
    own = {a.lower() for a in own}
    from mail_filing import select
    validity = select(imap, folder)
    typ, data = imap.uid("SEARCH", None, "ALL")
    uids = data[0].split()[-limit:] if typ == "OK" and data and data[0] else []
    items = []
    for uid, msg in _headers(imap, uids, "FROM SUBJECT DATE IN-REPLY-TO LIST-UNSUBSCRIBE"):
        addr = address(msg.get("From"))
        if addr.lower() in own:
            continue
        subject = text(msg.get("Subject")) or "(no subject)"
        verdict, reason = classify(addr, subject, text(msg.get("In-Reply-To")),
                                   text(msg.get("List-Unsubscribe")), known, trusted)
        if verdict:
            items.append({"uid": uid, "sender": text(msg.get("From")), "address": addr.lower(),
                          "subject": subject, "date": text(msg.get("Date")),
                          "verdict": verdict, "reason": reason})
    items.sort(key=lambda item: int(item["uid"]), reverse=True)
    return validity, items


class _Shared:
    """One IMAP session reused by every move in a rescue (mail_filing.move
    logs out after each message, which would mean a new login per email)."""

    def __init__(self, imap):
        self.imap = imap

    def __getattr__(self, name):
        if name == "logout":
            return lambda: None
        return getattr(self.imap, name)


def ensure_folder(imap, folders, name):
    if any(f.lower() == name.lower() for f in folders):
        return next(f for f in folders if f.lower() == name.lower())
    typ, data = imap.create(_quote(name))
    if typ != "OK":
        raise RuntimeError(f"Could not create the {name} folder")
    return name


def rescue(account, store, connector, folder, uids, validity, target=RESCUE_FOLDER):
    """Move each uid from folder into target, recoverably (Undo in Activity).
    Returns {"target", "moved": [uid...], "failed": [{uid, error}...]}."""
    from mail_filing import move
    if not validity:
        raise ValueError("Refresh the folder before rescuing mail")
    uids = [str(uid) for uid in uids]
    if not uids:
        return {"target": target, "moved": [], "failed": []}
    imap, folders, _ = connector(account)
    moved, failed = [], []
    try:
        target = ensure_folder(imap, folders, target)
        shared = _Shared(imap)
        for uid in uids:
            try:
                result = move(account, store, folder, uid, target, lambda _a: (shared, folders, None),
                              expected_validity=validity, reason=f"Rescued from {folder}")
                if result["status"] == "moved":
                    moved.append(uid)
                else:
                    failed.append({"uid": uid, "error": result.get("warning") or "Needs checking"})
            except Exception as exc:
                failed.append({"uid": uid, "error": str(exc)})
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return {"target": target, "moved": moved, "failed": failed}


SCREEN_FOLDER = "Screener"


def to_screen(envelopes, since_uid, known, trusted):
    """Inbox envelopes that screening moves aside: arrived after screening
    was turned on (uid > since_uid) and from nobody the owner has written to
    or trusted. `trusted` should include the owner's own addresses."""
    out = []
    for envelope in envelopes:
        if not str(envelope.get("uid", "")).isdigit() or int(envelope["uid"]) <= since_uid:
            continue
        addr = address(envelope.get("sender", "")).lower()
        if addr and (addr in known or trusted_match(addr, trusted)):
            continue
        out.append(envelope)
    return out
