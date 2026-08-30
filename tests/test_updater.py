#!/usr/bin/env python3
"""SimpleMail release-critical tests.

Run from the repo root:   PYTHONPATH= py -3 tests/test_updater.py
Pre-release gate:         PYTHONPATH= py -3 tests/test_updater.py --live

Covers the paths that bit real releases:
  T1  apply_update full flow never raises (frozen-exe simulated, download
      mocked). History: v1.1.0 shipped a str.format() KeyError that stranded
      every install forever - the update code that RUNS is always the OLD
      exe's, so the old side must stay minimal and the swap now happens in
      the NEW exe (--finish-update), whose code is fixable by shipping.
  T2  old-side handover details: staged .new exe written next to the app,
      helper launched as `new.exe --finish-update <target> <pid>` with
      CREATE_NEW_PROCESS_GROUP (NOT DETACHED_PROCESS - v1.1.4 regression:
      detached children silently never ran); bad/truncated downloads are
      rejected BEFORE the app exits; a helper that dies on boot aborts the
      update with the app still running.
  TF  finish_update (new-exe side): waits on the exe FILE lock, not on a
      hard-coded process name (regression: installs named SimpleMail-x64.exe
      never matched `Get-Process -Name SimpleMail`), swaps, relaunches; if
      the swap never succeeds it still relaunches the old exe.
  T4  check_for_update arch/asset selection with mocked GitHub payloads
  T5  check_update error surfacing (arch-named error, exceptions surfaced)
  T6  live GitHub check: latest release must exist and carry an asset for
      THIS machine's arch (regression: v1.1.0 shipped x64-only)
  T7  web/app.js startup update-check ordering + checkForUpdates behaviour
      (node vm harness; skipped if node is unavailable)

SAFETY RULES (learned the hard way):
  * Never launch a real app instance from a test - a launched instance reads
    and rewrites the user's real config (%APPDATA%/SimpleMail/config.json).
    If a test ever must launch one, point APPDATA at a temp dir first:
    CONFIG_DIR honours os.environ["APPDATA"].
  * All fixtures live under a tempfile scratch dir that is removed at exit.
  * Tests never read or write the real config.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIVE = "--live" in sys.argv
FAILS = []
CHECKS = 0


def check(name, cond, detail=""):
    global CHECKS
    CHECKS += 1
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def load_app():
    """Import mailapp.py headless (pywebview is not importable in tests)."""
    spec = importlib.util.spec_from_file_location("mailapp", REPO / "mailapp.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["webview"] = type(sys)("fake_webview")
    spec.loader.exec_module(m)
    return m


m = load_app()
SCRATCH = Path(tempfile.mkdtemp(prefix="sm-test-"))

# ---------------------------------------------------------------------------
# T1 + T2: apply_update old-side handover (frozen simulation, isolated scratch)
# ---------------------------------------------------------------------------

target = SCRATCH / "SimpleMail-x64.exe"   # renamed installs must work too
target.write_bytes(b"OLD-EXE")
new_exe = SCRATCH / "SimpleMail-x64.new.exe"
popen_calls = []
GOOD_EXE = b"MZ" + b"\x00" * 1_100_000

m._frozen_exe_path = lambda: target
m.download_file = lambda url, dest: Path(dest).write_bytes(GOOD_EXE)
m.os._exit = lambda code: (_ for _ in ()).throw(SystemExit(code))
orig_popen = subprocess.Popen
orig_sleep = time.sleep


class FakeProc:
    def __init__(self, exit_code=None):
        self._exit = exit_code
        self.returncode = exit_code

    def poll(self):
        return self._exit


helper_exit = [None]  # None = helper alive after boot


def fake_popen(args, **kw):
    popen_calls.append((list(args), kw))
    return FakeProc(helper_exit[0])


subprocess.Popen = fake_popen
time.sleep = lambda s: None
try:
    m.apply_update("https://example.invalid/SimpleMail-x64.exe")
    check("T1: apply_update completed without raising", False, "os._exit never reached")
except SystemExit as e:
    check("T1: apply_update completed without raising", e.code == 0, str(e))
except Exception as e:
    check("T1: apply_update completed without raising", False, f"{type(e).__name__}: {e}")
finally:
    subprocess.Popen = orig_popen
    time.sleep = orig_sleep

check("T2: new exe staged next to app", new_exe.exists() and new_exe.read_bytes() == GOOD_EXE)
check("T2: handover launches NEW exe with --finish-update <target> <pid>",
      len(popen_calls) == 1
      and popen_calls[0][0] == [str(new_exe), "--finish-update", str(target), str(os.getpid())],
      str(popen_calls))
check("T2: CREATE_NEW_PROCESS_GROUP set",
      bool(popen_calls[0][1].get("creationflags", 0) & 0x00000200), str(popen_calls))
check("T2: NOT DETACHED_PROCESS (v1.1.4 regression: detached child never ran)",
      not (popen_calls[0][1].get("creationflags", 0) & 0x00000008), str(popen_calls))

# T2-bad: corrupt/truncated download must abort BEFORE the app exits
popen_calls.clear()
new_exe.unlink()
m.download_file = lambda url, dest: Path(dest).write_bytes(b"<html>error page</html>")
subprocess.Popen = fake_popen
try:
    m.apply_update("https://example.invalid/SimpleMail-x64.exe")
    check("T2-bad: corrupt download rejected", False, "did not raise")
except SystemExit:
    check("T2-bad: corrupt download rejected", False, "app exited on corrupt download")
except Exception:
    check("T2-bad: corrupt download rejected", True)
finally:
    subprocess.Popen = orig_popen
check("T2-bad: staged file cleaned up, helper never launched",
      not new_exe.exists() and not popen_calls, str(popen_calls))

# T2-dead: helper that dies on boot aborts the update with the app alive
popen_calls.clear()
m.download_file = lambda url, dest: Path(dest).write_bytes(GOOD_EXE)
helper_exit[0] = 1
subprocess.Popen = fake_popen
time.sleep = lambda s: None
try:
    m.apply_update("https://example.invalid/SimpleMail-x64.exe")
    check("T2-dead: dead helper -> update aborted, app stays alive", False, "did not raise")
except SystemExit:
    check("T2-dead: dead helper -> update aborted, app stays alive", False, "app exited blind")
except RuntimeError as e:
    check("T2-dead: dead helper -> update aborted, app stays alive", "exited early" in str(e), str(e))
finally:
    subprocess.Popen = orig_popen
    time.sleep = orig_sleep
    helper_exit[0] = None
check("T2-dead: staged file cleaned up", not new_exe.exists())

# ---------------------------------------------------------------------------
# T2b: the launch form actually EXECUTES a child that survives (regression:
# v1.1.2-v1.1.3 DETACHED_PROCESS made the child silently never run)
# ---------------------------------------------------------------------------

launch_log = SCRATCH / "launch-log.txt"
child = subprocess.Popen(
    [sys.executable, "-c",
     f"open(r'{launch_log}', 'w').write('ran')"],
    close_fds=True,
    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.time() + 15
while time.time() < deadline and not launch_log.exists():
    time.sleep(0.5)
check("T2b: child launched with handover flags actually executes",
      launch_log.exists() and launch_log.read_text().strip() == "ran",
      "<no log>" if not launch_log.exists() else launch_log.read_text())

# ---------------------------------------------------------------------------
# TF: finish_update (new-exe side) - waits on the FILE lock, swaps, relaunches
# ---------------------------------------------------------------------------

f_target = SCRATCH / "app" / "SimpleMail-x64.exe"
f_target.parent.mkdir()
f_target.write_bytes(b"OLD-EXE")
f_self = SCRATCH / "app" / "SimpleMail-x64.new.exe"
f_self.write_bytes(GOOD_EXE)

real_copyfile = shutil.copyfile
deny = [2]  # PermissionError for the first N attempts (old app still running)


def locked_copyfile(src, dst):
    if deny[0] > 0:
        deny[0] -= 1
        raise PermissionError("file in use")
    return real_copyfile(src, dst)


popen_calls.clear()
shutil.copyfile = locked_copyfile
subprocess.Popen = fake_popen
try:
    m.finish_update(str(f_target), old_pid="12345", self_path=str(f_self),
                    sleep=lambda s: None)
    check("TF: finish_update exits", False, "os._exit never reached")
except SystemExit as e:
    check("TF: swap waits out the file lock then exits 0", e.code == 0, str(e))
finally:
    shutil.copyfile = real_copyfile
    subprocess.Popen = orig_popen
check("TF: target replaced with the new binary", f_target.read_bytes() == GOOD_EXE)
check("TF: app relaunched from the target path",
      len(popen_calls) == 1 and popen_calls[0][0] == [str(f_target)], str(popen_calls))

# TF-stuck: swap never succeeds -> still relaunch the OLD exe (never no app)
f_target.write_bytes(b"OLD-EXE")
deny[0] = 10 ** 9
popen_calls.clear()
shutil.copyfile = locked_copyfile
subprocess.Popen = fake_popen
try:
    m.finish_update(str(f_target), self_path=str(f_self), sleep=lambda s: None,
                    timeout=0)
    check("TF-stuck: finish_update exits", False, "os._exit never reached")
except SystemExit as e:
    check("TF-stuck: failed swap exits 1", e.code == 1, str(e))
finally:
    shutil.copyfile = real_copyfile
    subprocess.Popen = orig_popen
check("TF-stuck: old exe untouched and still relaunched",
      f_target.read_bytes() == b"OLD-EXE"
      and len(popen_calls) == 1 and popen_calls[0][0] == [str(f_target)], str(popen_calls))

# TF-dispatch: `exe --finish-update <target> <pid>` reaches finish_update
# before any config/GUI work (the helper must never open a window)
orig_argv, orig_finish = sys.argv, m.finish_update
dispatch = []
m.finish_update = lambda t, pid=None, **kw: dispatch.append((t, pid))
sys.argv = ["SimpleMail-x64.new.exe", "--finish-update", str(f_target), "999"]
try:
    m.main()
finally:
    sys.argv, m.finish_update = orig_argv, orig_finish
check("TF-dispatch: --finish-update routes to finish_update",
      dispatch == [(str(f_target), "999")], str(dispatch))

# ---------------------------------------------------------------------------
# T4: check_for_update arch/asset selection (mocked GitHub payloads)
# ---------------------------------------------------------------------------

RELEASE_JSON = {
    "tag_name": "v9.9.9",
    "html_url": "https://example.invalid/releases/tag/v9.9.9",
    "body": "release notes",
    "assets": [
        {"name": "SimpleMail-x64.exe", "browser_download_url": "https://example.invalid/x64"},
        {"name": "SimpleMail-arm64.exe", "browser_download_url": "https://example.invalid/arm64"},
    ],
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload

    def decode(self, _enc):
        return self._payload.decode()


orig_urlopen = urllib.request.urlopen
orig_arch = m.current_arch


def fake_urlopen(payload):
    def _open(req, timeout=None):
        return FakeResponse(payload)
    return _open


urllib.request.urlopen = fake_urlopen(RELEASE_JSON)
try:
    m.current_arch = lambda: "arm64"
    info = m.check_for_update()
    check("T4: arm64 picks the arm64 asset", info and info["asset_name"] == "SimpleMail-arm64.exe",
          str(info and info["asset_name"]))
    m.current_arch = lambda: "x64"
    info = m.check_for_update()
    check("T4: x64 picks the x64 asset", info and info["asset_name"] == "SimpleMail-x64.exe",
          str(info and info["asset_name"]))

    urllib.request.urlopen = fake_urlopen({**RELEASE_JSON, "assets": [RELEASE_JSON["assets"][0]]})
    m.current_arch = lambda: "arm64"
    info = m.check_for_update()
    check("T4: missing arch asset -> asset_url None (v1.1.0 regression)",
          info and info["asset_url"] is None)

    urllib.request.urlopen = fake_urlopen({**RELEASE_JSON, "tag_name": "not-a-version"})
    info = m.check_for_update()
    check("T4: junk tag -> None", info is None)
finally:
    urllib.request.urlopen = orig_urlopen
    m.current_arch = orig_arch  # leak broke T6: it must test THIS machine's arch

# ---------------------------------------------------------------------------
# T5: check_update error surfacing
# ---------------------------------------------------------------------------

orig_check = m.check_for_update
m.check_for_update = lambda: {"version": (9, 9, 9), "tag": "v9.9.9", "url": "",
                              "asset_url": None, "asset_name": None, "notes": "", "arch": "arm64"}
try:
    res = m.Api(None).check_update()
    check("T5: missing asset -> available=False + error names arch",
          res.get("available") is False and "arm64" in res.get("error", ""), str(res))
finally:
    m.check_for_update = orig_check

m.check_for_update = lambda: (_ for _ in ()).throw(RuntimeError("network boom"))
try:
    res = m.Api(None).check_update()
    check("T5: exception -> error field surfaced", res.get("error") == "network boom", str(res))
finally:
    m.check_for_update = orig_check

# ---------------------------------------------------------------------------
# T6: live GitHub check - latest release must carry this machine's arch asset
# ---------------------------------------------------------------------------

try:
    info = m.check_for_update()
    if info is None:
        check("T6: live latest release found", False, "check_for_update returned None")
    else:
        check("T6: live latest release found", True, info["tag"])
        have_asset = info["asset_url"] is not None
        check(f"T6: release {info['tag']} has a {info['arch']} asset", have_asset)
        if have_asset:
            req = urllib.request.Request(info["asset_url"], method="HEAD",
                                         headers={"User-Agent": "SimpleMail/tests"})
            with urllib.request.urlopen(req, timeout=30) as r:
                check("T6: asset URL downloads (200)", r.status == 200, str(r.status))
except Exception as e:
    if LIVE:
        check("T6: live GitHub check", False, str(e))
    else:
        print(f"SKIP  T6: live GitHub check (offline? {e}) - rerun with --live")

# ---------------------------------------------------------------------------
# TF-live (--live only): REAL locked-file swap - a child process holds the
# target exe open the way a running app locks its own binary, and
# finish_update must wait it out and then swap.
# ---------------------------------------------------------------------------

if LIVE:
    staged = SCRATCH / "staged"
    staged.mkdir()
    tgt = staged / "SimpleMail.exe"
    new = staged / "SimpleMail.new.exe"
    tgt.write_bytes(b"OLD")
    new.write_bytes(b"NEW-PAYLOAD")
    # Hold an exclusive handle on the target for ~4s (os.O_TEMPORARY-free
    # exclusive open via msvcrt locking is overkill: on Windows a second
    # writer is enough to collide with copyfile's open('wb') only when the
    # holder denies sharing, so emulate the app lock with a child that maps
    # the file via exclusive CreateFile through PowerShell).
    holder = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"$f=[System.IO.File]::Open('{tgt}','Open','Read','None');"
         "Start-Sleep -Seconds 4; $f.Close()"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)  # let the child take the handle first
    popen_calls.clear()
    subprocess.Popen = fake_popen
    t0 = time.time()
    try:
        m.finish_update(str(tgt), self_path=str(new), timeout=30)
    except SystemExit as e:
        check("TF-live: swap exits 0 after real lock released", e.code == 0, str(e))
    finally:
        subprocess.Popen = orig_popen
        holder.wait(timeout=30)
    check("TF-live: waited for the lock (not instant)", time.time() - t0 >= 2,
          f"{time.time() - t0:.1f}s")
    check("TF-live: target replaced once unlocked", tgt.read_bytes() == b"NEW-PAYLOAD")
    check("TF-live: relaunched from target", popen_calls and popen_calls[-1][0] == [str(tgt)],
          str(popen_calls))

# ---------------------------------------------------------------------------
# T7: web/app.js - startup update check ordering + checkForUpdates behaviour
# ---------------------------------------------------------------------------

js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
mfn = re.search(r"async function checkForUpdates\(silent = false\) \{(.*?)\n\}", js, re.S)
fn_src = "async function checkForUpdates(silent = false) {" + mfn.group(1) + "\n}"
start = js.index("  try {\n    const data = await api.get_config();")
catch_i = js.index("  } catch (e) {", start)
end = js.index("\n  }\n", catch_i) + len("\n  }\n")
init_src = js[start:end]

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const fnSrc = fs.readFileSync(process.argv[2], 'utf8');
const initSrc = fs.readFileSync(process.argv[3], 'utf8');
const calls = [];
const mkStubs = () => ({
  toast: (...a) => calls.push(['toast', ...a]),
  openSettings: () => calls.push(['openSettings']),
  applyScale: () => calls.push(['applyScale']),
  selectAccount: async () => calls.push(['selectAccount']),
  state: {},
  checkForUpdates: async () => calls.push(['checkForUpdates']),
  escapeHtml: s => String(s),
  document: { getElementById: () => ({ innerHTML: '' }) },
  $: id => ({ textContent: '', innerHTML: '', style: {},
               classList: { add: () => calls.push(['show', id]) } }),
});
let ok = true;
const t = (name, cond, detail = '') => {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond ? '' : '  [' + detail + ']'));
  if (!cond) ok = false;
};
(async () => {
  // startup ordering
  let ctx = vm.createContext(Object.assign({ api: { get_config: async () => ({ accounts: [] }) } }, mkStubs()));
  vm.runInContext('(async () => { ' + initSrc + ' })()', ctx);
  await new Promise(r => setImmediate(r));
  const iCheck = calls.findIndex(c => c[0] === 'checkForUpdates');
  const iOpen = calls.findIndex(c => c[0] === 'openSettings');
  t('T7: logged-out app still checks for updates first', iCheck >= 0 && iCheck < iOpen, JSON.stringify(calls));
  calls.length = 0;
  ctx = vm.createContext(Object.assign({ api: { get_config: async () => ({
    accounts: [{ id: 'a1' }], active_account: 'a1', ui_scale: 'default' }) } }, mkStubs()));
  vm.runInContext('(async () => { ' + initSrc + ' })()', ctx);
  await new Promise(r => setImmediate(r));
  t('T7: logged-in app checks exactly once',
    calls.filter(c => c[0] === 'checkForUpdates').length === 1, JSON.stringify(calls));

  // checkForUpdates behaviour (available / error / silent / up-to-date)
  ctx = vm.createContext(Object.assign({ api: { check_update: async () => ({
    available: true, new_version: 'v9.9.9', local_version: '1.0.0', notes: '' }) } }, mkStubs()));
  vm.runInContext(fnSrc, ctx);
  calls.length = 0;
  const r1 = await vm.runInContext('checkForUpdates(false)', ctx);
  t('T7: available -> returns true + backdrop', r1 === true && calls.some(c => c[0] === 'show'),
    JSON.stringify(calls));
  ctx.api = { check_update: async () => ({ available: false,
    error: 'No arm64 build published for this release yet' }) };
  calls.length = 0;
  const r2 = await vm.runInContext('checkForUpdates(false)', ctx);
  t('T7: error -> honest toast, never "latest" lie',
    r2 === false && calls.length === 1 && calls[0][1].startsWith('Update check failed:'),
    JSON.stringify(calls));
  calls.length = 0;
  await vm.runInContext('checkForUpdates(true)', ctx);
  t('T7: error + silent -> no toast', calls.length === 0, JSON.stringify(calls));
  ctx.api = { check_update: async () => ({ available: false, local_version: '1.1.3' }) };
  calls.length = 0;
  await vm.runInContext('checkForUpdates(false)', ctx);
  t('T7: genuinely up to date -> "latest" message', calls.length === 1 &&
    calls[0][1].includes('latest version'), JSON.stringify(calls));
  console.log(ok ? 'NODE-JS OK' : 'NODE-JS FAILED');
  process.exit(ok ? 0 : 1);
})();
"""

if shutil.which("node"):
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="sm-harness-", delete=False,
                                     dir=os.environ.get("TEMP")) as fh:
        fh.write(HARNESS)
        harness_path = fh.name
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="sm-fn-", delete=False,
                                     dir=os.environ.get("TEMP")) as fh:
        fh.write(fn_src)
        fn_path = fh.name
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="sm-init-", delete=False,
                                     dir=os.environ.get("TEMP")) as fh:
        fh.write(init_src)
        init_path = fh.name
    try:
        p = subprocess.run(["node", harness_path, fn_path, init_path], capture_output=True,
                           text=True, timeout=90, cwd=REPO / "web")
        print(p.stdout.strip())
        if p.stderr.strip():
            print("node stderr:", p.stderr.strip()[:300])
        check("T7: node vm checks passed", p.returncode == 0, p.stderr.strip()[:200])
    finally:
        for f in (harness_path, fn_path, init_path):
            try:
                os.unlink(f)
            except OSError:
                pass
else:
    print("SKIP  T7: node not on PATH")

# ---------------------------------------------------------------------------
shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print(f"{CHECKS} checks, {len(FAILS)} failures")
if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("ALL CHECKS PASSED")
