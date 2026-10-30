"""Mailboxes added on one PC (an Outlook.com / Live mailbox, say) appear on the owner's other PCs.

The account list lives in each PC's config.json, so on its own a mailbox added on the laptop never reaches the
desktop. Every couple of minutes (and at start, and after Settings change) this PC hands its accounts to Valet,
which keeps them encrypted with the vault key in the owner's Valet account, and takes in the other PCs'. Each
mailbox (by its address) carries when it last changed on the PC that changed it: the newest change wins, a removed
mailbox is removed everywhere, and nothing bounces back (what was taken in is recorded with the other PC's date).

Valet only answers SimpleMail itself, and only while it's unlocked; when it can't (locked, not installed), nothing
changes and the next round tries again. Fasthosts mailboxes aren't shared: they moved to the cloud mailboxes, which
every PC gets from the cloud, and the sign-in to those travels here too (CloudSignIn), so a new PC needs only Valet.

A mailbox's sign-in (its password, or the Microsoft refresh token in "password") travels with it so the other PC
can read it straight away, but changing only a sign-in isn't a change: Microsoft hands each PC a new token as it's
used, and a changed password is typed in again on each PC.
"""
import hashlib
import json
import os
import struct
import subprocess
import threading
import time
from pathlib import Path

IDENTITY = "simplemail:accounts"   # Valet keeps it beside Surf's sign-ins; only SimpleMail may ask for it
FASTHOSTS = ("livemail.co.uk", "fasthosts.co.uk")
STALE_DAYS = 30                    # a PC that hasn't shared anything for a month (retired) no longer counts
SECRETS = ("password", "smtp_password", "refresh_token")
CLOUD = "~cloud"                   # the cloud mailboxes' sign-in travels as one more entry under this key
TRUSTED = "simplemail:trusted"     # the cloud mailboxes' trusted senders (sync_trusted): a set of its own


def pc_name():
    return os.environ.get("SIMPLEMAIL_PC") or os.environ.get("COMPUTERNAME") or "this-pc"


def key(acct):
    return (acct.get("email") or "").strip().lower()


def on_fasthosts(acct):
    host = (acct.get("imap_host") or "").lower()
    return any(host == h or host.endswith("." + h) for h in FASTHOSTS)


def shared(acct):
    return bool(key(acct)) and not on_fasthosts(acct)


def fingerprint(acct):
    """What counts as a change: everything the owner set except the id (each PC keeps its own) and the secrets (a
    Microsoft token changes on every use, and a password's hash has no business in a file on disk)."""
    body = {k: v for k, v in acct.items() if k != "id" and k not in SECRETS and not k.startswith("_")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


# ---- this PC's record: when each mailbox last changed (no secrets) -------------------------------------
class Record:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("accounts", {})
        except (OSError, ValueError):
            return {}

    def save(self, accounts):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"accounts": accounts}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


# ---- Valet: the same link its browser add-on and Surf use ------------------------------------------------
class ValetUnavailable(Exception):
    pass


class ValetStore:
    HOST_KEY = r"Software\Microsoft\Edge\NativeMessagingHosts\com.valet.browser"

    def __init__(self, identity=IDENTITY):
        self.identity = identity

    def _program(self):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.HOST_KEY) as k:
                manifest = winreg.QueryValue(k, None)
            return json.loads(Path(manifest).read_text(encoding="utf-8"))["path"]
        except (OSError, ValueError, KeyError, ImportError):
            raise ValetUnavailable("Valet isn't set up on this PC") from None

    def ask(self, request, timeout=60):
        program = self._program()
        cmd = [program] if program.lower().endswith(".exe") else ["cmd.exe", "/c", program]
        body = json.dumps(request).encode("utf-8")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out, _ = proc.communicate(struct.pack("<I", len(body)) + body, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValetUnavailable(f"couldn't reach Valet ({e})") from None
        if len(out) < 4:
            raise ValetUnavailable("Valet closed without answering")
        n = struct.unpack("<I", out[:4])[0]
        reply = json.loads(out[4:4 + n].decode("utf-8"))
        if not reply.get("ok"):
            raise ValetUnavailable(reply.get("detail") or "Valet said no")
        return reply

    def others(self, pc):
        jars = []
        for raw in self.ask({"op": "signins_get", "identity": self.identity, "pc": pc}).get("jars") or []:
            try:
                jars.append(json.loads(raw))
            except ValueError:
                pass  # one PC's set we can't read: the others still count
        return jars

    def put(self, pc, jar):
        self.ask({"op": "signins_put", "identity": self.identity, "pc": pc, "jar": json.dumps(jar)})


# ---- the cloud mailboxes' sign-in (aws-mail.json, written by cloud_mail.CloudMail) ---------------------
class CloudSignIn:
    """So a new PC sees the cloud mailboxes too without signing in to them: the sign-in travels like a mailbox.
    A PC that already has its own keeps it; signing out on one PC doesn't sign the others out."""

    def __init__(self, directory, protector=None):
        self.path = Path(directory) / "aws-mail.json"
        self.protector = protector

    def _protect(self, value, decrypt=False):
        if self.protector:
            return self.protector(value, decrypt)
        from model_control import protect_secret
        return protect_secret(value, decrypt)

    def read(self):
        """{api_url, auth_url, client_id, refresh_token} when this PC is signed in, else None."""
        try:
            cfg = json.loads(self.path.read_text(encoding="utf-8"))
            token = self._protect(cfg["refresh_token"]["windows_dpapi"], True)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return {k: cfg.get(k, "") for k in ("api_url", "auth_url", "client_id")} | {"refresh_token": token}

    def write(self, value):
        from cloud_mail import CloudMail
        CloudMail.validate(value)   # only the real service's addresses, whatever arrives
        cfg = {k: value[k] for k in ("api_url", "auth_url", "client_id")}
        cfg["refresh_token"] = {"windows_dpapi": self._protect(value["refresh_token"])}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg), encoding="utf-8")
        os.replace(tmp, self.path)


# ---- one round ---------------------------------------------------------------------------------------
_round = threading.Lock()


def sync(cfg, store, record, normalize, pc=None, now=None, cloud=None):
    """Share this PC's mailboxes (and its cloud sign-in, given `cloud`) and take in newer ones from the other
    PCs. Returns the addresses taken in or removed here (empty when nothing changed on this PC)."""
    pc = pc or pc_name()
    now = now if now is not None else time.time()
    with _round:
        mine = record.load()
        local = {key(a): a for a in cfg.accounts() if shared(a)}
        signed_in = cloud.read() if cloud else None
        if signed_in:
            local[CLOUD] = signed_in
        changed = False
        for k in set(local) | set(mine):
            ours = mine.get(k) or {}
            if k in local:
                h = fingerprint(local[k])
                if ours.get("hash") != h or ours.get("deleted"):
                    mine[k] = {"changed": now, "hash": h, "deleted": False}
                    changed = True
            elif not ours.get("deleted"):
                mine[k] = {"changed": now, "hash": "", "deleted": True}
                changed = True

        accounts = [dict(a) for a in cfg.accounts()]
        brought = []
        for other in store.others(pc):
            if now - float(other.get("saved") or 0) > STALE_DAYS * 86400:
                continue
            for k, theirs in (other.get("accounts") or {}).items():
                ours = mine.get(k)
                if ours and (theirs["changed"] <= ours["changed"] or
                             (theirs["hash"] == ours["hash"] and bool(theirs.get("deleted")) == bool(ours.get("deleted")))):
                    continue
                if k == CLOUD:
                    # Taken only by a PC not signed in to the same service; a sign-out isn't copied.
                    if not cloud or theirs.get("deleted") or not theirs.get("account"):
                        continue
                    if not signed_in or signed_in.get("api_url") != theirs["account"].get("api_url"):
                        cloud.write(theirs["account"])
                        signed_in = local[CLOUD] = theirs["account"]
                    mine[k] = {"changed": theirs["changed"], "hash": theirs["hash"], "deleted": False}
                    brought.append(k)
                    changed = True
                    continue
                current = next((a for a in accounts if key(a) == k), None)
                if theirs.get("deleted"):
                    accounts = [a for a in accounts if key(a) != k]
                elif theirs.get("account"):
                    incoming = normalize(theirs["account"])
                    if current is not None:
                        incoming["id"] = current["id"]
                        for field in SECRETS:   # this PC's own sign-in keeps working: never swap it out
                            if current.get(field) and current.get("auth") == incoming.get("auth"):
                                incoming[field] = current[field]
                        current.clear()
                        current.update(incoming)
                    else:
                        ids = {a["id"] for a in accounts}
                        while incoming["id"] in ids:
                            incoming["id"] += "-2"
                        accounts.append(incoming)
                mine[k] = {"changed": theirs["changed"], "hash": theirs["hash"], "deleted": bool(theirs.get("deleted"))}
                brought.append(k)
                changed = True

        if brought:
            cfg.data = {**cfg.data, "accounts": accounts}
            if not any(a["id"] == cfg.data.get("active_account") for a in accounts) and accounts:
                cfg.data["active_account"] = accounts[0]["id"]
            cfg.save()
        if changed:
            by_key = {key(a): a for a in accounts if shared(a)}
            if signed_in:
                by_key[CLOUD] = signed_in
            store.put(pc, {"pc": pc, "saved": now, "accounts": {
                k: {**r, "account": None if r.get("deleted") or k not in by_key else
                    {f: v for f, v in by_key[k].items() if not f.startswith("_")}}
                for k, r in mine.items()}})
            record.save(mine)
        return brought



# ---- the cloud mailboxes' trusted senders ----------------------------------------------------------------
# A cloud mailbox keeps no settings of its own on this PC, so the senders trusted there ("Always for this sender")
# sit in config.json under cloud_trusted, by mailbox address. They travel as a set of their own (TRUSTED), not
# inside the mailbox list, which older SimpleMail builds would read as a mailbox. Trusting is never undone, so the
# PCs simply pool their lists.
_shared_trusted = {}   # pc -> what this PC last handed Valet, so an unchanged list isn't sent every round


def sync_trusted(cfg, store, pc=None, now=None):
    """Take in the other PCs' trusted senders and share this PC's. Returns True when something new came in."""
    pc = pc or pc_name()
    now = now if now is not None else time.time()
    with _round:
        lists = {str(a).lower(): [str(e).lower() for e in v] for a, v in (cfg.data.get("cloud_trusted") or {}).items()
                 if isinstance(v, list)}
        grew = False
        for other in store.others(pc):
            if now - float(other.get("saved") or 0) > STALE_DAYS * 86400:
                continue
            for mailbox, entries in (other.get("trusted") or {}).items():
                if not isinstance(entries, list):
                    continue
                have = lists.setdefault(str(mailbox).lower(), [])
                for entry in entries:
                    entry = str(entry).strip().lower()
                    if entry and entry not in have:
                        have.append(entry)
                        grew = True
        if grew:
            cfg.data["cloud_trusted"] = lists
            cfg.save()
        snapshot = json.dumps(lists, sort_keys=True)
        if lists and _shared_trusted.get(pc) != snapshot:
            store.put(pc, {"pc": pc, "saved": now, "trusted": lists})
            _shared_trusted[pc] = snapshot
        return grew
