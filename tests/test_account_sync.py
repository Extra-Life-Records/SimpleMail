import json
import tempfile
import unittest
from pathlib import Path

import account_sync
from mailapp import normalize_account


class Cfg:
    """Just what account_sync needs from mailapp.Config."""
    def __init__(self, accounts):
        self.data = {"accounts": [normalize_account(a) for a in accounts], "active_account": ""}
        self.saves = 0

    def accounts(self):
        return self.data["accounts"]

    def save(self):
        self.saves += 1


class Cloud:
    """Valet's side: one jar per PC; each PC sees the others'."""
    def __init__(self):
        self.jars = {}

    def store(self):
        cloud = self

        class Store:
            def others(self, pc):
                return [json.loads(j) for p, j in cloud.jars.items() if p != pc]

            def put(self, pc, jar):
                cloud.jars[pc] = json.dumps(jar)
        return Store()


class PC:
    def __init__(self, cloud, name, accounts):
        self.name, self.cloud = name, cloud
        self.cfg = Cfg(accounts)
        self.record = account_sync.Record(Path(tempfile.mkdtemp()) / "account-sync.json")

    def sync(self, now):
        return account_sync.sync(self.cfg, self.cloud.store(), self.record, normalize_account, pc=self.name, now=now)

    def emails(self):
        return sorted(a["email"] for a in self.cfg.accounts())

    def acct(self, email):
        return next(a for a in self.cfg.accounts() if a["email"] == email)


LIVE = {"id": "adam", "label": "Live", "email": "adam@live.co.uk", "auth": "microsoft", "password": "refresh-from-laptop",
        "imap_host": "outlook.office365.com", "smtp_host": "smtp-mail.outlook.com"}
FASTHOSTS = {"id": "extraliferecords", "email": "hello@extraliferecords.com", "password": "pw",
             "imap_host": "mail.livemail.co.uk", "smtp_host": "smtp.fasthosts.co.uk"}


class AccountSyncTests(unittest.TestCase):
    def test_a_mailbox_added_on_the_laptop_reaches_the_desktop_with_its_sign_in(self):
        cloud = Cloud()
        laptop, desktop = PC(cloud, "NIMBUS", [LIVE]), PC(cloud, "DESKTOP", [FASTHOSTS])
        laptop.sync(100)
        self.assertEqual(desktop.sync(200), ["adam@live.co.uk"])
        self.assertEqual(desktop.emails(), ["adam@live.co.uk", "hello@extraliferecords.com"])
        self.assertEqual(desktop.acct("adam@live.co.uk")["password"], "refresh-from-laptop")
        self.assertEqual(desktop.acct("adam@live.co.uk")["auth"], "microsoft")
        # Fasthosts mailboxes are never shared (they live in the cloud now)
        self.assertNotIn("hello@extraliferecords.com", cloud.jars["DESKTOP"])

    def test_nothing_bounces_back_and_tokens_alone_are_not_changes(self):
        cloud = Cloud()
        laptop, desktop = PC(cloud, "NIMBUS", [LIVE]), PC(cloud, "DESKTOP", [])
        laptop.sync(100)
        desktop.sync(200)
        desktop.acct("adam@live.co.uk")["password"] = "desktop's own newer token"   # Microsoft rotated it
        self.assertEqual(desktop.sync(300), [])
        self.assertEqual(laptop.sync(400), [])
        self.assertEqual(laptop.acct("adam@live.co.uk")["password"], "refresh-from-laptop")

    def test_an_edit_wins_by_date_and_keeps_each_pcs_own_sign_in(self):
        cloud = Cloud()
        laptop, desktop = PC(cloud, "NIMBUS", [LIVE]), PC(cloud, "DESKTOP", [])
        laptop.sync(100)
        desktop.sync(200)
        desktop.acct("adam@live.co.uk")["password"] = "desktop token"
        desktop.acct("adam@live.co.uk")["id"] = "adam-desktop"
        laptop.acct("adam@live.co.uk")["signature"] = "Adam"
        laptop.sync(300)
        self.assertEqual(desktop.sync(400), ["adam@live.co.uk"])
        a = desktop.acct("adam@live.co.uk")
        self.assertEqual((a["signature"], a["password"], a["id"]), ("Adam", "desktop token", "adam-desktop"))

    def test_removing_a_mailbox_removes_it_everywhere(self):
        cloud = Cloud()
        laptop, desktop = PC(cloud, "NIMBUS", [LIVE]), PC(cloud, "DESKTOP", [])
        laptop.sync(100)
        desktop.sync(200)
        desktop.cfg.data["accounts"] = []
        desktop.sync(300)
        self.assertEqual(laptop.sync(400), ["adam@live.co.uk"])
        self.assertEqual(laptop.emails(), [])
        self.assertEqual(desktop.sync(500), [])   # and it stays gone

    def test_a_pc_silent_for_a_month_no_longer_counts(self):
        cloud = Cloud()
        laptop, desktop = PC(cloud, "NIMBUS", [LIVE]), PC(cloud, "DESKTOP", [])
        laptop.sync(100)
        self.assertEqual(desktop.sync(100 + 31 * 86400), [])

    def test_no_secret_lands_in_the_record_on_disk(self):
        cloud = Cloud()
        laptop = PC(cloud, "NIMBUS", [LIVE, {**LIVE, "id": "x", "email": "me@outlook.com", "auth": "password",
                                              "password": "plain-password"}])
        laptop.sync(100)
        on_disk = laptop.record.path.read_text()
        self.assertNotIn("plain-password", on_disk)
        self.assertNotIn("refresh-from-laptop", on_disk)

    def test_valet_unavailable_changes_nothing(self):
        class Locked:
            def others(self, pc):
                raise account_sync.ValetUnavailable("Valet is locked")
        desktop = PC(Cloud(), "DESKTOP", [LIVE])
        with self.assertRaises(account_sync.ValetUnavailable):
            account_sync.sync(desktop.cfg, Locked(), desktop.record, normalize_account, pc="DESKTOP", now=1)
        self.assertEqual(desktop.cfg.saves, 0)
        self.assertFalse(desktop.record.path.exists())


class RetireFasthostsTests(unittest.TestCase):
    def test_a_fasthosts_copy_of_a_cloud_mailbox_goes_and_nothing_else(self):
        import mailapp
        playloudr = {**FASTHOSTS, "id": "playloudr", "email": "playloudr@extraliferecords.com",
                     "from_email": "hello@playloudr.com"}
        orphan = {**FASTHOSTS, "id": "orphan", "email": "old@example.co.uk"}
        cfg = Cfg([FASTHOSTS, playloudr, orphan, LIVE])
        cfg.data["active_account"] = "extraliferecords"
        api = mailapp.Api(cfg)
        cloud = [{"id": "cloud:hello", "email": "hello@extraliferecords.com"},
                 {"id": "cloud:playloudr", "email": "hello@playloudr.com"}]
        self.assertEqual(api._retire_fasthosts(cloud), {"extraliferecords", "playloudr"})
        self.assertEqual(sorted(a["id"] for a in cfg.accounts()), ["adam", "orphan"])
        self.assertEqual(cfg.data["active_account"], "orphan")
        self.assertEqual(api._retire_fasthosts(cloud), set())


if __name__ == "__main__":
    unittest.main()
