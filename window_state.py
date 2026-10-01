"""Local desktop window placement, separate from mailbox settings."""
import json
import logging
from pathlib import Path


def load_placement(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if any(type(value.get(k)) is not int for k in ("x", "y", "width", "height")):
            return None
        if not (200 <= value["width"] <= 32000 and 100 <= value["height"] <= 32000):
            return None
        if type(value.get("maximized")) is not bool:
            return None
        return {k: value[k] for k in ("x", "y", "width", "height", "maximized")}
    except (OSError, ValueError):
        return None


def fit_placement(placement, work_areas):
    """Keep the restored window reachable after monitor/layout changes."""
    def overlap(area):
        x, y, width, height = area
        return (max(0, min(x + width, placement["x"] + placement["width"]) - max(x, placement["x"]))
                * max(0, min(y + height, placement["y"] + placement["height"]) - max(y, placement["y"])))

    # The primary monitor is first, so disconnected monitors fall back to it.
    x, y, width, height = max(work_areas, key=overlap)
    result = dict(placement)
    result["width"] = min(width, max(980, placement["width"]))
    result["height"] = min(height, max(620, placement["height"]))
    result["x"] = max(x, min(placement["x"], x + width - result["width"]))
    result["y"] = max(y, min(placement["y"], y + height - result["height"]))
    return result


def capture_placement(previous, bounds, state):
    # Minimizing must never replace the useful normal/maximized placement.
    if state == "Minimized":
        return previous
    x, y, width, height = bounds
    return dict(x=x, y=y, width=width, height=height, maximized=state == "Maximized")


def remember_window(window, path, saved):
    """Attach on shown, when pywebview has created its native WinForms form."""
    from System import Action
    from System.Drawing import Rectangle, Size
    from System.Windows.Forms import FormWindowState, Screen, Timer

    def attach():
        form = window.native
        screens = sorted(Screen.AllScreens, key=lambda screen: not screen.Primary)
        areas = [(s.WorkingArea.X, s.WorkingArea.Y, s.WorkingArea.Width, s.WorkingArea.Height)
                 for s in screens]
        if saved:
            restored = fit_placement(saved, areas)
            form.WindowState = FormWindowState.Normal
            form.MinimumSize = Size(min(980, restored["width"]), min(620, restored["height"]))
            form.Bounds = Rectangle(restored["x"], restored["y"], restored["width"], restored["height"])
            if restored["maximized"]:
                form.WindowState = FormWindowState.Maximized

        last = saved
        timer = Timer()
        timer.Interval = 250

        def capture():
            nonlocal last
            state = str(form.WindowState)
            bounds = form.Bounds if state == "Normal" else form.RestoreBounds
            last = capture_placement(last, (bounds.X, bounds.Y, bounds.Width, bounds.Height), state)

        def save(sender=None, args=None):
            timer.Stop()
            capture()
            if last is None:
                return
            try:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps(last), encoding="utf-8")
                temporary.replace(target)
            except OSError:
                logging.getLogger(__name__).exception("Could not save window placement")

        def changed(sender, args):
            capture()
            timer.Stop()
            timer.Start()

        def closing(sender, args):
            save()
            timer.Dispose()

        timer.Tick += save
        form.LocationChanged += changed
        form.SizeChanged += changed
        form.ResizeEnd += save
        form.FormClosing += closing
        save()

    window.native.Invoke(Action(attach))
