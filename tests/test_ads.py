import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ads import AnnouncementPicker, list_announcements  # noqa: E402


class AdsTest(unittest.TestCase):
    def test_empty_returns_none(self):
        p = AnnouncementPicker([], rng=random.Random(1))
        self.assertIsNone(p.pick())
        self.assertEqual(p.probabilities(), [])

    def test_single_file_always_plays(self):
        p = AnnouncementPicker(["a.mp3"], rng=random.Random(1))
        for _ in range(20):
            self.assertEqual(p.pick(), "a.mp3")
        self.assertAlmostEqual(p.probabilities()[0][1], 100.0)

    def test_no_consecutive_repeat_two_files(self):
        p = AnnouncementPicker(["a.mp3", "b.mp3"], rng=random.Random(2))
        prev = None
        for _ in range(2000):
            cur = p.pick()
            self.assertNotEqual(cur, prev)
            prev = cur

    def test_no_consecutive_repeat_three_files(self):
        p = AnnouncementPicker(["a.mp3", "b.mp3", "c.mp3"], rng=random.Random(3))
        prev = None
        for _ in range(2000):
            cur = p.pick()
            self.assertNotEqual(cur, prev)
            prev = cur

    def test_all_files_get_played(self):
        files = ["a.mp3", "b.mp3", "c.mp3"]
        p = AnnouncementPicker(files, rng=random.Random(4))
        seen = {p.pick() for _ in range(300)}
        self.assertEqual(seen, set(files))

    def test_last_played_has_zero_probability_and_sum_is_100(self):
        p = AnnouncementPicker(["a.mp3", "b.mp3", "c.mp3"], rng=random.Random(5))
        last = p.pick()
        probs = {f: pct for f, pct, _ in p.probabilities()}
        self.assertEqual(probs[last], 0.0)
        self.assertAlmostEqual(sum(probs.values()), 100.0, places=6)

    def test_least_recently_played_gets_more_weight(self):
        p = AnnouncementPicker(["a.mp3", "b.mp3", "c.mp3"], rng=random.Random(6))
        p._record("a.mp3")
        p._record("b.mp3")
        probs = {f: pct for f, pct, _ in p.probabilities()}
        # c hiç çalmadı, a ondan eski; b son çalan (0)
        self.assertEqual(probs["b.mp3"], 0.0)
        self.assertGreater(probs["c.mp3"], probs["a.mp3"])
        self.assertGreater(probs["a.mp3"], 0.0)

    def test_list_announcements_filters_mp3(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "1.mp3").write_bytes(b"x")
            Path(d, "notes.txt").write_text("x", encoding="utf-8")
            files = list_announcements(d)
            self.assertEqual([Path(f).name for f in files], ["1.mp3"])
        self.assertEqual(list_announcements(Path(d, "yok")), [])


if __name__ == "__main__":
    unittest.main()
