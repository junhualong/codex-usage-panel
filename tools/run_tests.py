"""Run only logic tests; desktop/process integration scripts are opt-in."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
names = ["test_usage", "test_follow", "test_speed", "test_autostart", "test_lifecycle", "test_runtime", "test_panel_view", "test_appearance", "test_icons"]
result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
raise SystemExit(0 if result.wasSuccessful() else 1)
