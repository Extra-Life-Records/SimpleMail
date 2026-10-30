"""Mailboxes added on one PC (an Outlook.com / Live mailbox, say) appear on the owner's other PCs.

The account list lives in each PC's config.json, so on its own a mailbox added on the laptop never reaches the
desktop. Every couple of minutes (and at start, and after Settings change) this PC hands its accounts to Valet,
which keeps them encrypted with the vault key in the owner's Valet account, and takes in the other PCs'. Each
mailbox (by its address) carries when it last changed on the PC that changed it: the newest change wins, a removed
mailbox is removed everywhere, and nothing bounces back (what was taken in is recorded with the other PC's date).

Valet only answers SimpleMail itself, and only while it's unlocked; when it can't (locked, not installed), nothing
changes and the next round tries again. Fasthosts mailboxes aren't shared: they moved to the cloud mailboxes, which
every PC already gets from the cloud.

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
SECRETS = ("password", "smtp_password")


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
        for raw in self.ask({"op": "signins_get", "identity": IDENTITY, "pc": pc}).get("jars") or []:
            try:
                jars.append(json.loads(raw))
            except ValueError:
                pass  # one PC's set we can't read: the others still count
        return jars

    def put(self, pc, jar):
        self.ask({"op": "signins_put", "identity": IDENTITY, "pc": pc, "jar": json.dumps(jar)})


# ---- one round ---------------------------------------------------------------------------------------
_round = threading.Lock()


def sync(cfg, store, record, normalize, pc=None, now=None):
    """Share this PC's mailboxes and take in newer ones from the other PCs. Returns the addresses taken in or
    removed here (empty when nothing changed on this PC)."""
    pc = pc or pc_name()
    now = now if now is not None else time.time()
    with _round:
        mine = record.load()
        local = {key(a): a for a in cfg.accounts() if shared(a)}
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
            store.put(pc, {"pc": pc, "saved": now, "accounts": {
                k: {**r, "account": None if r.get("deleted") or k not in by_key else
                    {f: v for f, v in by_key[k].items() if not f.startswith("_")}}
                for k, r in mine.items()}})
            record.save(mine)
        return brought
