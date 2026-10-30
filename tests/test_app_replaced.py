"""An exe swapped in while the app is open is noticed, and restart relaunches it cleanly."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mailapp


class ReplacedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.exe = Path(temp.name) / "Simple Mail's.exe"
        self.exe.write_bytes(b"MZ old build")
        self.started = mailapp._exe_stamp(self.exe)

    def test_unchanged_exe_is_not_replaced(self):
        self.assertFalse(mailapp.exe_replaced(self.exe, self.started))

    def test_new_build_on_disk_is_replaced(self):
        self.exe.write_bytes(b"MZ new build, a different size")
        self.assertTrue(mailapp.exe_replaced(self.exe, self.started))

    def test_same_size_rewrite_is_replaced(self):
        os.utime(self.exe, ns=(0, self.started[1] + 10**9))
        self.assertTrue(mailapp.exe_replaced(self.exe, self.started))

    def test_missing_exe_is_replaced(self):
        self.exe.unlink()
        self.assertTrue(mailapp.exe_replaced(self.exe, self.started))

    def test_running_from_source_is_never_replaced(self):
        self.assertFalse(mailapp.exe_replaced(None, None))
        self.assertEqual(mailapp.Api.app_replaced(None), {"replaced": False})

    def test_restart_waits_for_this_process_then_starts_fresh(self):
        calls, exits = [], []
        mailapp.restart_app(self.exe, pid=4321, popen=lambda *a, **k: calls.append((a, k)),
                            exit_=exits.append)
        (args,), kwargs = calls[0]
        script = args[-1]
        self.assertEqual(args[0], "powershell")
        self.assertLess(script.index("Wait-Process -Id 4321"), script.index("Start-Process"))
        self.assertIn("'" + str(self.exe).replace("'", "''") + "'", script)
        self.assertIn("foreach ($n in 1..3)", script)   # retried while WebView2 holds its profile
        self.assertIn("if (-not $p.HasExited) { break }", script)
        self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(exits, [0])

    def test_open_drafts_are_handed_to_the_next_start_once(self):
        drafts = [{"id": "d1", "account_id": "one", "revision": 0, "payload": {"body": "Hi — unsaved"}}]
        with patch.object(mailapp, "CONFIG_DIR", self.exe.parent / "config"):
            mailapp.restart_app(self.exe, pid=1, popen=lambda *a, **k: None, exit_=lambda code: None, drafts=drafts)
            self.assertEqual(json.loads(mailapp.restart_drafts_file().read_text(encoding="utf-8")), drafts)
            self.assertEqual(mailapp.Api.take_restart_drafts(None), drafts)
            self.assertEqual(mailapp.Api.take_restart_drafts(None), [])

    def test_bad_drafts_stop_the_restart(self):
        with self.assertRaises(ValueError):
            mailapp.restart_app(self.exe, popen=lambda *a, **k: self.fail("launched"), exit_=self.fail,
                                drafts=["not a draft"], drafts_file=self.exe.parent / "d.json")

    def test_restart_refuses_outside_the_packaged_app(self):
        with self.assertRaises(RuntimeError):
            mailapp.restart_app(None, popen=lambda *a, **k: self.fail("launched"), exit_=self.fail)


if __name__ == "__main__":
    unittest.main()
