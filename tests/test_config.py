import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DEFAULT_CONFIG, load_config, save_setting  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_config(self.base), DEFAULT_CONFIG)

    def test_save_keeps_other_keys(self):
        (self.base / "config.json").write_text(
            json.dumps({"update_repo": "a/b", "volume": 55}), encoding="utf-8")
        save_setting(self.base, "crossfade_sec", 7.5)
        data = json.loads((self.base / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(data, {"update_repo": "a/b", "volume": 55, "crossfade_sec": 7.5})
        self.assertEqual(load_config(self.base)["crossfade_sec"], 7.5)

    def test_save_creates_missing_file(self):
        save_setting(self.base, "crossfade_sec", 2.0)
        self.assertEqual(json.loads((self.base / "config.json").read_text(encoding="utf-8")),
                         {"crossfade_sec": 2.0})

    def test_broken_file_is_not_overwritten(self):
        path = self.base / "config.json"
        path.write_text("{bozuk", encoding="utf-8")
        with self.assertRaises(ValueError):
            save_setting(self.base, "crossfade_sec", 1.0)
        self.assertEqual(path.read_text(encoding="utf-8"), "{bozuk")


if __name__ == "__main__":
    unittest.main()
