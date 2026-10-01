"""Native Windows check: run maximized, restore-maximized, restore-normal in order."""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mailapp  # configure the same WindowsDesktop runtime as the app
import webview
from window_state import load_placement, remember_window

stage = sys.argv[1]
path = Path(tempfile.gettempdir()) / 'simplemail-window-smoke.json'
saved = load_placement(path)
window = webview.create_window('SimpleMail placement verification', html='<p>Checking window placement</p>',
                              width=1240, height=800, maximized=saved['maximized'] if saved else True)
window.events.shown += lambda: remember_window(window, path, saved)
errors = []

def verify():
    time.sleep(1)
    from System import Action
    from System.Drawing import Rectangle
    from System.Windows.Forms import FormWindowState
    def native():
        try:
            form = window.native
            if stage == 'maximized':
                form.WindowState = FormWindowState.Normal
                form.Bounds = Rectangle(180, 100, 1320, 840)
                form.WindowState = FormWindowState.Maximized
                form.WindowState = FormWindowState.Minimized
            elif stage == 'restore-maximized':
                assert str(form.WindowState) == 'Maximized', str(form.WindowState)
                form.WindowState = FormWindowState.Normal
                b = form.Bounds
                assert (b.X, b.Y, b.Width, b.Height) == (180, 100, 1320, 840), str(b)
                form.Bounds = Rectangle(240, 140, 1280, 820)
            else:
                assert str(form.WindowState) == 'Normal', str(form.WindowState)
                b = form.Bounds
                assert (b.X, b.Y, b.Width, b.Height) == (240, 140, 1280, 820), str(b)
        except Exception as error:
            errors.append(str(error))
        form.Close()
    window.native.Invoke(Action(native))

webview.start(verify)
if errors:
    raise AssertionError(errors)
actual = load_placement(path)
if stage == 'maximized':
    assert actual == dict(x=180, y=100, width=1320, height=840, maximized=True), actual
else:
    assert actual == dict(x=240, y=140, width=1280, height=820, maximized=False), actual
print('PASS', stage, actual)
