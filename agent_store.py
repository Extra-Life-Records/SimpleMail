"""Durable, account-scoped agent state. Uses stdlib SQLite, shared across processes."""
import hashlib
import json
import sqlite3
import uuid
import re
import conversation_state
from email.utils import getaddresses
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class AgentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            conversation_state.initialize(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    account_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    job TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(account_id, request_key));
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS automatic_replies (
                    account_id TEXT NOT NULL, message_ref TEXT NOT NULL, draft_id TEXT NOT NULL,
                    PRIMARY KEY(account_id,message_ref));
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(profiles)")}
            if "send_mode" not in columns:
                db.execute("ALTER TABLE profiles ADD COLUMN send_mode TEXT NOT NULL DEFAULT 'draft_for_review'")
            if "allowed_recipients" not in columns:
                db.execute("ALTER TABLE profiles ADD COLUMN allowed_recipients TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _event(db, account_id, kind, detail):
        db.execute("INSERT INTO activity(account_id,kind,detail,created_at) VALUES(?,?,?,?)",
                   (account_id, kind, json.dumps(detail), now()))

    def profile(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM profiles WHERE account_id=?", (account_id,)).fetchone()
        return {"account_id": account_id, "enabled": bool(row["enabled"]) if row else False,
                "job": row["job"] if row else "", "mode": row["send_mode"] if row else "draft_for_review",
                "allowed_recipients": json.loads(row["allowed_recipients"]) if row else []}

    def set_profile(self, account_id, enabled, job, mode=None, allowed_recipients=None):
        if not isinstance(enabled, bool) or not isinstance(job, str) or len(job) > 10000:
            raise ValueError("Invalid agent settings")
        if mode is not None and mode not in ("draft_for_review", "reply_to_allowed"):
            raise ValueError("Invalid sending permission")
        if allowed_recipients is not None:
            if not isinstance(allowed_recipients, list) or len(allowed_recipients) > 100:
                raise ValueError("Provide at most 100 exact email addresses")
            if any(not isinstance(address, str) or not re.fullmatch(r'[^\s<>@,;*]+@[^\s<>@,;*]+', address)
                   for address in allowed_recipients):
                raise ValueError("Allowed recipients must be exact email addresses, without wildcards")
            allowed_recipients = sorted({address.lower() for address in allowed_recipients})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM profiles WHERE account_id=?", (account_id,)).fetchone()
            mode = mode or (old["send_mode"] if old else "draft_for_review")
            allowed = allowed_recipients if allowed_recipients is not None else (
                json.loads(old["allowed_recipients"]) if old else [])
            if mode == "reply_to_allowed" and (not allowed or not job.strip()):
                raise ValueError("Write a job and specify allowed recipients before enabling automatic replies")
            db.execute("INSERT INTO profiles(account_id,enabled,job) VALUES(?,?,?) "
                       "ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,job=excluded.job",
                       (account_id, enabled, job))
            db.execute("UPDATE profiles SET send_mode=?,allowed_recipients=? WHERE account_id=?",
                       (mode, json.dumps(allowed), account_id))
            self._event(db, account_id, "settings", {"enabled": enabled, "job": job,
                                                    "mode": mode, "allowed_recipients": allowed})
        return self.profile(account_id)

    @staticmethod
    def _draft(row):
        if row is None:
            raise ValueError("Draft not found in this mailbox")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item.pop("fingerprint")
        return item

    def get_draft(self, account_id, draft_id):
        with self.connect() as db:
            return self._draft(db.execute("SELECT * FROM drafts WHERE account_id=? AND id=?",
                                         (account_id, draft_id)).fetchone())

    def save_draft(self, account_id, request_key, payload, draft_id=None, revision=None):
        """A request key is immutable; edits require the current draft revision."""
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise ValueError("A stable request_key of at most 200 characters is required")
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if draft_id:
                row = db.execute("SELECT * FROM drafts WHERE account_id=? AND id=?",
                                 (account_id, draft_id)).fetchone()
                self._draft(row)
                if row["status"] != "pending" or row["revision"] != revision:
                    raise ValueError("Draft changed or is no longer editable; reload before editing")
                db.execute("UPDATE drafts SET payload=?,fingerprint=?,revision=revision+1,updated_at=? "
                           "WHERE id=?", (encoded, fingerprint, now(), draft_id))
                self._event(db, account_id, "draft_updated", {"draft_id": draft_id,
                            "revision": revision + 1, "reason": payload.get("reason", "")})
            else:
                row = db.execute("SELECT * FROM drafts WHERE account_id=? AND request_key=?",
                                 (account_id, request_key)).fetchone()
                if row:
                    if row["fingerprint"] != fingerprint:
                        raise ValueError("request_key already used for different content; edit the draft")
                    return self._draft(row)
                draft_id = str(uuid.uuid4())
                stamp = now()
                db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?,?)",
                           (draft_id, account_id, request_key, 1, "pending", encoded,
                            fingerprint, stamp, stamp))
                self._event(db, account_id, "draft_created", {"draft_id": draft_id,
                            "revision": 1, "reason": payload.get("reason", "")})
            return self._draft(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())

    def list_drafts(self, account_id, before=None, limit=50):
        limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            rows = db.execute("SELECT rowid AS cursor,* FROM drafts WHERE account_id=? AND status='pending' "
                              "AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                              (account_id, before, before, limit + 1)).fetchall()
        items = [self._draft(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["cursor"] if len(rows) > limit else None}

    def dismiss_draft(self, account_id, draft_id, revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE drafts SET status='dismissed',updated_at=? "
                                 "WHERE account_id=? AND id=? AND revision=? AND status='pending'",
                                 (now(), account_id, draft_id, revision)).rowcount
            if changed != 1:
                raise ValueError("Draft changed or is no longer pending")
            self._event(db, account_id, "draft_dismissed", {"draft_id": draft_id})
            conversation_state.draft_outcome(db, account_id, draft_id, "dismissed")
        return {"status": "dismissed"}

    def claim_send(self, account_id, draft_id, revision, autonomous=False):
        """Owner approval claims one exact revision before any network side effect."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM drafts WHERE account_id=? AND id=?",
                             (account_id, draft_id)).fetchone()
            draft = self._draft(row)
            if draft["status"] != "pending" or draft["revision"] != revision:
                raise ValueError("Draft changed or was already submitted; reload before sending")
            if autonomous:
                profile = db.execute("SELECT * FROM profiles WHERE account_id=?", (account_id,)).fetchone()
                if not profile or not profile["enabled"] or profile["send_mode"] != "reply_to_allowed":
                    raise ValueError("Automatic replies are not permitted; keep this draft for owner review")
                target_headers = [draft["payload"].get(key, "") for key in ("to", "cc", "bcc")]
                targets = {address.lower() for _, address in getaddresses([value for value in target_headers if value])
                           if address}
                allowed = set(json.loads(profile["allowed_recipients"]))
                if not targets or not targets <= allowed:
                    raise ValueError("Recipients are outside the owner's permission; keep this draft for review")
                if not draft["payload"].get("reply_ref"):
                    raise ValueError("Automatic sending is limited to replies to incoming mail")
                try:
                    db.execute("INSERT INTO automatic_replies VALUES(?,?,?)",
                               (account_id, draft["payload"].get("in_reply_to") or draft["payload"]["reply_ref"], draft_id))
                except sqlite3.IntegrityError:
                    raise ValueError("This incoming message already has an automatic send claim; owner review required") from None
            db.execute("UPDATE drafts SET status='sending',updated_at=? WHERE id=?", (now(), draft_id))
            self._event(db, account_id, "send_started", {"draft_id": draft_id, "revision": revision,
                        "to": draft["payload"]["to"], "subject": draft["payload"]["subject"],
                        "actor": "agent" if autonomous else "owner"})
        return draft

    def finish_send(self, account_id, draft_id, outcome, detail):
        if outcome not in ("sent", "uncertain"):
            raise ValueError("Invalid send outcome")
        with self.connect() as db:
            changed = db.execute("UPDATE drafts SET status=?,updated_at=? "
                                 "WHERE account_id=? AND id=? AND status='sending'",
                                 (outcome, now(), account_id, draft_id)).rowcount
            if changed != 1:
                raise ValueError("Send was not claimed")
            self._event(db, account_id, "send_" + outcome, {"draft_id": draft_id, **detail})
            conversation_state.draft_outcome(db, account_id, draft_id, outcome)

    def activity(self, account_id, before=None, limit=50):
        limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM activity WHERE account_id=? "
                              "AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                              (account_id, before, before, limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            item["detail"] = json.loads(item["detail"])
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}
