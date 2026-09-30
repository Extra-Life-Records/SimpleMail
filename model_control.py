"""Owner-only provider setup and managed workers. Never exposed through MCP."""
import base64
import ctypes
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from agent_store import AgentStore


def protect_secret(value, decrypt=False):
    """Windows DPAPI, scoped to the current Windows user, without UI prompts."""
    if os.name != "nt":
        raise ValueError("Protected model keys require Windows")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value, validate=True) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("Windows could not protect or unlock the model key")
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        kernel.LocalFree(target.data)


class ModelControl(AgentStore):
    def __init__(self, path, protector=protect_secret, clock=time.time):
        super().__init__(path)
        self.protector, self.clock = protector, clock
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS model_connections (
                    account_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
                    model TEXT NOT NULL, api TEXT NOT NULL, secret TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_runtime (
                    account_id TEXT PRIMARY KEY, token TEXT NOT NULL,
                    desired INTEGER NOT NULL, heartbeat REAL NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
            """)

    def state(self, account):
        with self.connect() as db:
            connection = db.execute("SELECT * FROM model_connections WHERE account_id=?", (account,)).fetchone()
            runtime = db.execute("SELECT * FROM model_runtime WHERE account_id=?", (account,)).fetchone()
        active = bool(runtime and runtime["status"] not in ("stopped", "failed") and
                      self.clock() - runtime["heartbeat"] < 90)
        status = runtime["status"] if runtime else "stopped"
        if runtime and not active and status not in ("stopped", "failed"):
            status = "interrupted"
        return {"endpoint": connection["endpoint"] if connection else "",
                "model": connection["model"] if connection else "",
                "api": connection["api"] if connection else "responses",
                "has_key": bool(connection and connection["secret"]),
                "configured": bool(connection), "active": active,
                "status": status, "detail": runtime["detail"] if runtime else ""}

    def connection(self, account):
        with self.connect() as db:
            row = db.execute("SELECT * FROM model_connections WHERE account_id=?", (account,)).fetchone()
        if not row:
            raise ValueError("Connect a model first")
        return {"endpoint": row["endpoint"], "model": row["model"], "api": row["api"],
                "key": self.protector(row["secret"], True) if row["secret"] else ""}

    def save(self, account, endpoint, model, api, key="", clear_key=False):
        from model_worker import HTTPModel
        if not all(isinstance(item, str) for item in (endpoint, model, api, key)) or len(key) > 4096:
            raise ValueError("Invalid model connection")
        endpoint, model = endpoint.strip(), model.strip()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            runtime = db.execute("SELECT * FROM model_runtime WHERE account_id=?", (account,)).fetchone()
            if runtime and runtime["status"] not in ("stopped", "failed") and self.clock()-runtime["heartbeat"] < 90:
                raise ValueError("Pause the worker and wait for it to stop before changing its model")
            old = db.execute("SELECT * FROM model_connections WHERE account_id=?", (account,)).fetchone()
            # A saved key must never silently follow a changed provider endpoint.
            secret = old["secret"] if old and old["endpoint"] == endpoint and not clear_key else ""
            if key:
                secret = self.protector(key)
            unlocked = self.protector(secret, True) if secret else ""
            HTTPModel(endpoint, model, api, key=unlocked)
            db.execute("INSERT OR REPLACE INTO model_connections VALUES(?,?,?,?,?)",
                       (account, endpoint, model, api, secret))
        return self.state(account)

    def claim(self, account):
        self.connection(account)  # Unlock and validate before reserving a launch.
        if not self.profile(account)["enabled"]:
            raise ValueError("Save a job and enable agent access first")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM model_runtime WHERE account_id=?", (account,)).fetchone()
            if row and row["status"] not in ("stopped", "failed") and self.clock()-row["heartbeat"] < 90:
                raise ValueError("This mailbox already has a worker running or stopping")
            token = str(uuid.uuid4())
            db.execute("INSERT OR REPLACE INTO model_runtime VALUES(?,?,1,?,'starting','')",
                       (account, token, self.clock()))
        return token

    def check(self, account, token):
        with self.connect() as db:
            row = db.execute("SELECT desired,token FROM model_runtime WHERE account_id=?", (account,)).fetchone()
        if not row or row["token"] != token or not row["desired"]:
            raise ValueError("Worker paused by owner")

    def heartbeat(self, account, token, status=None, detail=""):
        with self.connect() as db:
            if status:
                db.execute("UPDATE model_runtime SET heartbeat=?,status=?,detail=? WHERE account_id=? AND token=?",
                           (self.clock(), status, detail, account, token))
            else:
                db.execute("UPDATE model_runtime SET heartbeat=? WHERE account_id=? AND token=?",
                           (self.clock(), account, token))

    def stop(self, account):
        with self.connect() as db:
            db.execute("UPDATE model_runtime SET desired=0,status='stopping' WHERE account_id=? "
                       "AND status NOT IN ('stopped','failed')", (account,))
        return self.state(account)

    def start(self, account, launcher=subprocess.Popen):
        token = self.claim(account)
        if getattr(sys, "frozen", False):
            agent = Path(sys.executable).parent / "SimpleMailAgent.exe"
            command = [str(agent)]
        else:
            command = [sys.executable, str(Path(__file__).parent / "agent_mcp.py")]
        command += ["managed", "--account", account, "--token", token]
        try:
            process = launcher(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, cwd=str(Path(__file__).parent),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=process.wait, daemon=True).start()
        except Exception:
            self.heartbeat(account, token, "failed", "Worker could not start; check the console agent installation")
            raise ValueError("Worker could not start; check the console agent installation") from None
        return self.state(account)


def run_managed(config, store_path, account, token):
    """Background entry point; the desktop may exit while this process keeps running."""
    from mailbox_agent import MailboxAgent
    from model_worker import HTTPModel, ModelWorker
    from work_queue import WorkQueue
    control = ModelControl(store_path)
    done = threading.Event()
    control.check(account, token)
    def pulse():
        while not done.wait(3):
            control.heartbeat(account, token)
    heartbeat = threading.Thread(target=pulse, daemon=True)
    heartbeat.start()
    try:
        selected = control.connection(account)
        model = HTTPModel(selected["endpoint"], selected["model"], selected["api"], key=selected["key"])
        queue = WorkQueue(store_path)
        mailbox = MailboxAgent(config, queue, account)
        guard = lambda: control.check(account, token)
        worker = ModelWorker(mailbox, queue, model, owner_guard=guard)
        while True:
            guard()
            control.heartbeat(account, token, "running")
            result = worker.run(once=True)
            control.heartbeat(account, token, result["status"],
                              "Check mailbox connection settings" if result["status"] == "unavailable" else "")
            for _ in range(30 if result["status"] in ("idle", "paused", "unavailable") else 1):
                guard()
                done.wait(1)
    except Exception:
        try:
            control.check(account, token)
        except ValueError:
            control.heartbeat(account, token, "stopped")
        else:
            control.heartbeat(account, token, "failed", "Worker stopped; check the model and mailbox connection")
    finally:
        done.set()
        heartbeat.join(timeout=5)
