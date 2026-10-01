"""Placement recovery across window states and monitor changes."""
import json
import tempfile
import unittest
from pathlib import Path

from window_state import capture_placement, fit_placement, load_placement


class WindowPlacementTests(unittest.TestCase):
    def setUp(self):
        self.placement = dict(x=2100, y=100, width=1200, height=800, maximized=False)

    def test_valid_state_round_trip_and_corrupt_state_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "window.json"
            self.assertIsNone(load_placement(path))
            path.write_text(json.dumps(self.placement))
            self.assertEqual(load_placement(path), self.placement)
            for value in ("broken", "[]", '{"x": true}', json.dumps(dict(self.placement, width=-1))):
                path.write_text(value)
                self.assertIsNone(load_placement(path))

    def test_existing_second_monitor_position_is_preserved(self):
        areas = [(0, 0, 1920, 1040), (1920, 0, 1920, 1040)]
        self.assertEqual(fit_placement(self.placement, areas), self.placement)

    def test_disconnected_monitor_returns_fully_inside_primary_work_area(self):
        actual = fit_placement(self.placement, [(0, 0, 1366, 728)])
        self.assertEqual(actual, dict(x=166, y=0, width=1200, height=728, maximized=False))

    def test_negative_monitor_coordinates_and_small_screens(self):
        state = dict(self.placement, x=-1600, width=1700)
        actual = fit_placement(state, [(0, 0, 1920, 1040), (-1600, 0, 1600, 900)])
        self.assertEqual(actual["x"], -1600)
        self.assertEqual(actual["width"], 1600)
        actual = fit_placement(state, [(0, 0, 800, 600)])
        self.assertEqual((actual["width"], actual["height"]), (800, 600))

    def test_minimized_close_preserves_previous_maximized_and_normal_bounds(self):
        maximized = capture_placement(self.placement, (200, 80, 1300, 850), "Maximized")
        self.assertTrue(maximized["maximized"])
        self.assertEqual(capture_placement(maximized, (-32000, -32000, 160, 30), "Minimized"), maximized)
        normal = capture_placement(maximized, (300, 120, 1100, 700), "Normal")
        self.assertFalse(normal["maximized"])
        self.assertEqual((normal["x"], normal["width"]), (300, 1100))


if __name__ == "__main__":
    unittest.main()
