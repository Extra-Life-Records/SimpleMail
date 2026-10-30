"""Who fills a folder: a saved index of senders, grouped by site.

Scanning a big Outlook.com folder takes minutes (about 8 for 34,000 emails on
one connection), so headers are read in the background over a few connections
and kept in a small SQLite cache. Later scans only fetch new mail, drop what
has gone, and refresh read/unread with one SEARCH.

Clean-up actions build on it: move a sender's mail to Trash in bulk (Undo per
message in Activity), block the site for the future (a saved sender rule), and
RFC 8058 one-click unsubscribe over HTTPS to a public address only.
"""
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email import message_from_bytes
from email.policy import compat32
from email.utils import parsedate_to_datetime
from pathlib import Path

from mail_screen import address, looks_random, text, trusted_match

BATCH = 500
WORKERS = 4
MAX_GROUPS = 400
# Second-level labels under country codes: mail.example.co.uk -> example.co.uk
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "ltd", "plc", "me", "sch", "nhs", "edu"}


def site(addr):
    """The sender's site: registrable domain of the address (or the host)."""
    host = addr.rsplit("@", 1)[-1].strip().lower().rstrip(".") if addr else ""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def one_click_url(list_unsubscribe, list_unsubscribe_post):
    """The HTTPS one-click unsubscribe URL (RFC 8058), or ''."""
    if "list-unsubscribe=one-click" not in (list_unsubscribe_post or "").lower().replace(" ", ""):
        return ""
    for url in re.findall(r"<([^>]+)>", list_unsubscribe or ""):
        url = url.strip()
        if url.lower().startswith("https://") and len(url) <= 4000:
            return url
    return ""


def _timestamp(date):
    try:
        return parsedate_to_datetime(date).timestamp()
    except Exception:
        return 0.0


def row(uid, header_bytes):
    msg = message_from_bytes(header_bytes, policy=compat32)
    raw = msg.get("From")
    addr = address(raw)
    name = re.sub(r"\s*<[^>]*>\s*$", "", text(raw)).strip().strip('"') or addr
    return {"uid": int(uid), "address": addr.lower(), "name": name[:120], "site": site(addr) or "(unknown)",
            "ts": _timestamp(text(msg.get("Date"))), "random": int(looks_random(addr)),
            "unsub": one_click_url(text(msg.get("List-Unsubscribe")), text(msg.get("List-Unsubscribe-Post")))}


class SenderIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS senders (
                    account_id TEXT NOT NULL, folder TEXT NOT NULL, uid INTEGER NOT NULL,
                    address TEXT NOT NULL, name TEXT NOT NULL, site TEXT NOT NULL, ts REAL NOT NULL,
                    seen INTEGER NOT NULL, random INTEGER NOT NULL, unsub TEXT NOT NULL,
                    PRIMARY KEY(account_id, folder, uid));
                CREATE INDEX IF NOT EXISTS senders_site ON senders(account_id, folder, site);
                CREATE TABLE IF NOT EXISTS sender_folders (
                    account_id TEXT NOT NULL, folder TEXT NOT NULL, validity TEXT NOT NULL,
                    total INTEGER NOT NULL, scanned_at REAL NOT NULL,
                    PRIMARY KEY(account_id, folder));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, account, folder):
        with self.connect() as db:
            found = db.execute("SELECT * FROM sender_folders WHERE account_id=? AND folder=?", (account, folder)).fetchone()
        return dict(found) if found else None

    def uids(self, account, folder, site_name=None):
        with self.connect() as db:
            query = "SELECT uid FROM senders WHERE account_id=? AND folder=?" + (" AND site=?" if site_name else "")
            args = (account, folder, site_name) if site_name else (account, folder)
            return {r[0] for r in db.execute(query, args)}

    def reset(self, account, folder, validity, total):
        with self.connect() as db:
            previous = db.execute("SELECT validity FROM sender_folders WHERE account_id=? AND folder=?",
                                  (account, folder)).fetchone()
            if previous and previous[0] != validity:
                db.execute("DELETE FROM senders WHERE account_id=? AND folder=?", (account, folder))
            db.execute("INSERT OR REPLACE INTO sender_folders VALUES(?,?,?,?,?)",
                       (account, folder, validity, total, time.time()))

    def add(self, account, folder, rows):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO senders VALUES(?,?,?,?,?,?,?,?,?,?)",
                           [(account, folder, r["uid"], r["address"], r["name"], r["site"], r["ts"],
                             r.get("seen", 1), r["random"], r["unsub"]) for r in rows])

    def remove(self, account, folder, uids):
        with self.connect() as db:
            db.executemany("DELETE FROM senders WHERE account_id=? AND folder=? AND uid=?",
                           [(account, folder, int(uid)) for uid in uids])

    def set_unseen(self, account, folder, unseen):
        with self.connect() as db:
            db.execute("UPDATE senders SET seen=1 WHERE account_id=? AND folder=?", (account, folder))
            db.executemany("UPDATE senders SET seen=0 WHERE account_id=? AND folder=? AND uid=?",
                           [(account, folder, int(uid)) for uid in unseen])

    def groups(self, account, folder, trusted=(), limit=MAX_GROUPS):
        """Senders by site, biggest first."""
        with self.connect() as db:
            rows = db.execute("SELECT site, address, name, ts, seen, random, unsub FROM senders "
                              "WHERE account_id=? AND folder=? ORDER BY ts DESC", (account, folder)).fetchall()
        groups = {}
        for r in rows:
            g = groups.get(r["site"])
            if g is None:
                g = groups[r["site"]] = {"site": r["site"], "name": r["name"], "count": 0, "unread": 0,
                                         "newest": r["ts"], "addresses": set(), "unsubscribe": "",
                                         "random": 0, "trusted": False}
            g["count"] += 1
            g["unread"] += 0 if r["seen"] else 1
            g["random"] += r["random"]
            g["addresses"].add(r["address"])
            if not g["unsubscribe"] and r["unsub"]:
                g["unsubscribe"] = r["unsub"]
            if not g["trusted"] and trusted_match(r["address"], trusted):
                g["trusted"] = True
        out = sorted(groups.values(), key=lambda g: (-g["count"], g["site"]))[:limit]
        for g in out:
            g["random"] = g["random"] * 2 > g["count"]
            g["can_unsubscribe"] = bool(g["unsubscribe"]) and not g["random"]
            g["addresses"] = sorted(g["addresses"])[:5]
            del g["unsubscribe"]  # the URL stays on this machine; the UI asks by site
        return out

    def unsubscribe_url(self, account, folder, site_name):
        with self.connect() as db:
            found = db.execute("SELECT unsub, random FROM senders WHERE account_id=? AND folder=? AND site=? "
                               "AND unsub != '' ORDER BY ts DESC LIMIT 1", (account, folder, site_name)).fetchone()
        return found["unsub"] if found and not found["random"] else ""


def _search(imap, criteria):
    typ, data = imap.uid("SEARCH", None, criteria)
    return [int(u) for u in data[0].split()] if typ == "OK" and data and data[0] else []


def scan(account, index, connector, folder, progress=None, workers=WORKERS):
    """Bring the index for one folder up to date. progress(done, total)."""
    from imap_fetch import fetch_groups
    from mail_filing import quote, select
    imap, _, _ = connector(account)
    try:
        validity = select(imap, folder)
        present = _search(imap, "ALL")
        unseen = _search(imap, "UNSEEN")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    index.reset(account["id"], folder, validity, len(present))
    known = index.uids(account["id"], folder)
    index.remove(account["id"], folder, known - set(present))
    index.set_unseen(account["id"], folder, unseen)
    missing = sorted(set(present) - known, reverse=True)  # newest first
    chunks = [missing[i:i + BATCH] for i in range(0, len(missing), BATCH)]
    total, done, lock = len(present), len(present) - len(missing), threading.Lock()
    unseen = set(unseen)
    if progress:
        progress(done, total)

    def work(share):
        nonlocal done
        conn, _, _ = connector(account)
        try:
            typ, _ = conn.select(quote(folder), readonly=True)
            if typ != "OK":
                raise RuntimeError("Folder unavailable")
            for chunk in share:
                typ, data = conn.uid("FETCH", ",".join(map(str, chunk)),
                                     "(UID BODY.PEEK[HEADER.FIELDS (FROM DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])")
                if typ != "OK":
                    raise RuntimeError("The mail server stopped answering")
                rows = []
                for uid, _, parts in fetch_groups(data):
                    if uid is not None and parts:
                        r = row(uid, parts[0][1])
                        r["seen"] = 0 if uid in unseen else 1
                        rows.append(r)
                index.add(account["id"], folder, rows)
                with lock:
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    if chunks:
        shares = [chunks[i::workers] for i in range(min(workers, len(chunks)))]
        with ThreadPoolExecutor(len(shares)) as pool:
            for future in [pool.submit(work, share) for share in shares]:
                future.result()
    return {"validity": validity, "total": total}


def unsubscribe(url, timeout=15):
    """RFC 8058 one-click: POST 'List-Unsubscribe=One-Click' to a public HTTPS
    address. No cookies, no redirects followed. Returns the HTTP status."""
    from mail_images import _HTTPSConnection, _destination
    parts, host, ip, port = _destination(url)
    if parts.scheme != "https":
        raise ValueError("Only HTTPS unsubscribe links are used")
    conn = _HTTPSConnection(host, ip, timeout)
    try:
        target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        body = b"List-Unsubscribe=One-Click"
        conn.request("POST", target, body=body, headers={
            "Host": host, "User-Agent": "Mozilla/5.0 SimpleMail",
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))})
        status = conn.getresponse().status
    finally:
        conn.close()
    if not 200 <= status < 300:
        raise ValueError(f"The sender's unsubscribe page answered {status}")
    return status
