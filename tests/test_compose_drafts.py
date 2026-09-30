"""Human draft recovery and send guarantees; no real config or network."""
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compose_store import ComposeStore
import mailapp


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ComposeStore(Path(self.temp.name) / "state.sqlite3")
        self.id = str(uuid.uuid4())
        self.payload = {"to": "person@example.com", "subject": "Hello", "body": "Draft",
                        "body_html": "<p>Draft</p>"}
        class Config:
            def account(self, account_id):
                if account_id not in ("one", "two"):
                    raise ValueError("Unknown account")
                return {"id": account_id, "email": account_id + "@example.com", "from_email": ""}
        self.api = mailapp.Api(Config())
        self.api._compose_store = lambda: self.store

    def test_restart_recovers_html_and_incomplete_recipient(self):
        self.api.save_compose_draft("one", self.id, 0, "per", "Subject", "Body", "<b>Body</b>")
        restarted = ComposeStore(self.store.path)
        result = restarted.get("one", self.id)
        self.assertEqual(result["payload"]["to"], "per")
        self.assertEqual(result["payload"]["body_html"], "<b>Body</b>")
        self.assertEqual(len(restarted.list("one")), 1)
        self.assertEqual(restarted.list("two"), [])

    def test_edits_replace_one_draft_and_retry_is_idempotent(self):
        self.store.save("one", self.id, 0, self.payload)
        retry = self.store.save("one", self.id, 0, self.payload)
        self.assertEqual(retry["revision"], 1)
        edited = self.store.save("one", self.id, 1, {**self.payload, "body": "New"})
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(len(self.store.list("one")), 1)
        with self.assertRaises(ValueError):
            self.store.save("one", self.id, 1, self.payload)

    def test_cross_mailbox_access_and_send_rejected(self):
        self.store.save("one", self.id, 0, self.payload)
        for operation in (lambda: self.store.get("two", self.id),
                          lambda: self.store.save("two", self.id, 1, self.payload),
                          lambda: self.api.send_compose_draft("two", self.id, 1)):
            with self.assertRaises(ValueError):
                operation()

    def test_concurrent_send_claims_have_one_winner(self):
        self.store.save("one", self.id, 0, self.payload)
        def attempt(_):
            try:
                self.store.claim("one", self.id, 1)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(attempt, range(8))), 1)
        restarted = ComposeStore(self.store.path)
        with self.assertRaises(ValueError):
            restarted.claim("one", self.id, 1)

    def test_successful_send_leaves_recovery_list_and_rejects_late_save(self):
        self.store.save("one", self.id, 0, self.payload)
        with patch.object(mailapp, "send_message", return_value={"smtp_accepted": True, "sent_copy_saved": True}) as send:
            result = self.api.send_compose_draft("one", self.id, 1)
            self.assertEqual(result["status"], "sent")
            with self.assertRaises(ValueError):
                self.api.send_compose_draft("one", self.id, 1)
            send.assert_called_once()
        self.assertEqual(self.store.list("one"), [])
        with self.assertRaises(ValueError):
            self.store.save("one", self.id, 1, self.payload)

    def test_timeout_is_visible_and_blocks_retries(self):
        self.store.save("one", self.id, 0, self.payload)
        with patch.object(mailapp, "send_message", side_effect=TimeoutError()) as send:
            result = self.api.send_compose_draft("one", self.id, 1)
            self.assertEqual(result["status"], "uncertain")
            with self.assertRaises(ValueError):
                self.api.send_compose_draft("one", self.id, 1)
            send.assert_called_once()
        self.assertEqual(self.store.list("one")[0]["status"], "uncertain")

    def test_invalid_recipient_never_claims_send(self):
        self.store.save("one", self.id, 0, {**self.payload, "to": "unfinished"})
        with patch.object(mailapp, "send_message") as send:
            with self.assertRaises(ValueError):
                self.api.send_compose_draft("one", self.id, 1)
            send.assert_not_called()
        self.assertEqual(self.store.get("one", self.id)["status"], "pending")

    def test_discard_hides_draft_and_stale_discard_cannot_remove_new_edit(self):
        self.store.save("one", self.id, 0, self.payload)
        self.store.save("one", self.id, 1, {**self.payload, "body": "New"})
        with self.assertRaises(ValueError):
            self.api.discard_compose_draft("one", self.id, 1)
        self.api.discard_compose_draft("one", self.id, 2)
        self.assertEqual(self.store.list("one"), [])
        self.assertEqual(self.store.get("one", self.id)["status"], "dismissed")


if __name__ == "__main__":
    unittest.main()
