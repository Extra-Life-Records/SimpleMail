"""Local human drafts with optimistic edits and durable send claims."""
import json
import uuid
from agent_store import AgentStore, now


class ComposeStore(AgentStore):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS compose_drafts (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("Draft not found in this mailbox")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def get(self, account_id, draft_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM compose_drafts WHERE account_id=? AND id=?",
                                         (account_id, draft_id)).fetchone())

    def save(self, account_id, draft_id, revision, payload):
        try:
            uuid.UUID(draft_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid draft ID") from None
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("Invalid draft revision")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM compose_drafts WHERE id=?", (draft_id,)).fetchone()
            if row:
                if row["account_id"] != account_id or row["status"] != "pending":
                    raise ValueError("Draft is unavailable for editing")
                if row["revision"] == revision + 1 and row["payload"] == encoded:
                    return self.decode(row)
                if row["revision"] != revision:
                    raise ValueError("Draft changed in another window; reopen it before editing")
                db.execute("UPDATE compose_drafts SET revision=revision+1,payload=?,updated_at=? WHERE id=?",
                           (encoded, now(), draft_id))
            else:
                if revision != 0:
                    raise ValueError("Draft no longer exists")
                db.execute("INSERT INTO compose_drafts VALUES(?,?,1,'pending',?,?)",
                           (draft_id, account_id, encoded, now()))
            return self.decode(db.execute("SELECT * FROM compose_drafts WHERE id=?", (draft_id,)).fetchone())

    def list(self, account_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM compose_drafts WHERE account_id=? "
                              "AND status IN ('pending','sending','uncertain') ORDER BY updated_at DESC",
                              (account_id,)).fetchall()
        return [self.decode(row) for row in rows]

    def claim(self, account_id, draft_id, revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM compose_drafts WHERE account_id=? AND id=?",
                             (account_id, draft_id)).fetchone()
            draft = self.decode(row)
            if draft["status"] != "pending" or draft["revision"] != revision:
                raise ValueError("Draft changed or was already submitted; reopen it before sending")
            db.execute("UPDATE compose_drafts SET status='sending',updated_at=? WHERE id=?", (now(), draft_id))
            self._event(db, account_id, "send_started", {"human_draft_id": draft_id,
                        "subject": draft["payload"]["subject"], "to": draft["payload"]["to"]})
            return draft

    def discard(self, account_id, draft_id, revision):
        with self.connect() as db:
            changed = db.execute("UPDATE compose_drafts SET status='dismissed',updated_at=? "
                                 "WHERE id=? AND account_id=? AND status='pending' AND revision=?",
                                 (now(), draft_id, account_id, revision)).rowcount
            if changed != 1:
                raise ValueError("Draft changed or is no longer editable")
        return {"status": "dismissed"}

    def finish(self, account_id, draft_id, status, detail):
        if status not in ("sent", "uncertain"):
            raise ValueError("Invalid send outcome")
        with self.connect() as db:
            changed = db.execute("UPDATE compose_drafts SET status=?,updated_at=? "
                                 "WHERE id=? AND account_id=? AND status='sending'",
                                 (status, now(), draft_id, account_id)).rowcount
            if changed != 1:
                raise ValueError("Send was not claimed")
            self._event(db, account_id, "send_" + status, {"human_draft_id": draft_id, **detail})
