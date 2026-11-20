import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import SR, PcmStream  # noqa: E402
from istek import RequestQueue, parse_link  # noqa: E402
from player import RadioEngine  # noqa: E402
from playlists import Track  # noqa: E402


def const(n, v):
    return np.full((n, 2), v, np.float32)


class ParseLinkTest(unittest.TestCase):
    def test_video_links(self):
        self.assertEqual(parse_link("https://www.youtube.com/watch?v=5NV6Rdv1a3I"),
                         ("video", "5NV6Rdv1a3I"))
        self.assertEqual(parse_link("https://youtu.be/5NV6Rdv1a3I?si=abc"),
                         ("video", "5NV6Rdv1a3I"))
        self.assertEqual(parse_link("https://www.youtube.com/shorts/5NV6Rdv1a3I"),
                         ("video", "5NV6Rdv1a3I"))

    def test_playlist_links(self):
        self.assertEqual(parse_link("https://www.youtube.com/playlist?list=PLabc_-12"),
                         ("playlist", "PLabc_-12"))
        self.assertEqual(parse_link("https://www.youtube.com/watch?v=5NV6Rdv1a3I&list=PLxyz"),
                         ("playlist", "PLxyz"))

    def test_plain_text_is_not_a_link(self):
        self.assertIsNone(parse_link("daft punk get lucky"))
        self.assertIsNone(parse_link(""))
        self.assertIsNone(parse_link(None))


class RequestQueueTest(unittest.TestCase):
    def setUp(self):
        self.q = RequestQueue()

    def _pl(self, pid, n, mode="seq"):
        tracks = [{"id": "%s%d" % (pid, i), "title": "%s-%d" % (pid, i)} for i in range(n)]
        return self.q.make_playlist(pid, "PL " + pid, tracks, mode=mode)

    def test_empty_queue_returns_none(self):
        self.assertTrue(self.q.is_empty())
        self.assertIsNone(self.q.pop_next())

    def test_tracks_are_fifo(self):
        self.q.add(self.q.make_track("a" * 11, "A"))
        self.q.add(self.q.make_track("b" * 11, "B"))
        t1, _ = self.q.pop_next()
        t2, _ = self.q.pop_next()
        self.assertEqual((t1.title, t2.title), ("A", "B"))
        self.assertIsNone(self.q.pop_next())
        self.assertTrue(self.q.is_empty())

    def test_playlist_plays_in_order_then_is_removed(self):
        req = self._pl("P", 3)
        self.q.add(req)
        ids = [self.q.pop_next()[0].id for _ in range(3)]
        self.assertEqual(ids, ["P0", "P1", "P2"])
        self.assertIsNone(self.q.pop_next())

    def test_playlist_progress_in_snapshot(self):
        req = self._pl("P", 4)
        self.q.add(req)
        self.q.pop_next()
        self.q.pop_next()
        snap = self.q.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual((snap[0]["done"], snap[0]["total"]), (2, 4))
        self.assertEqual(snap[0]["kind"], "playlist")

    def test_shuffle_keeps_every_track_once(self):
        req = self._pl("S", 30, mode="shuffle")
        self.q.add(req)
        ids = [self.q.pop_next()[0].id for _ in range(30)]
        self.assertEqual(sorted(ids), sorted("S%d" % i for i in range(30)))
        self.assertEqual(req.mode, "shuffle")

    def test_remove_drops_pending_item(self):
        a = self._pl("A", 2)
        b = self.q.make_track("b" * 11, "B")
        self.q.add(a)
        self.q.add(b)
        self.assertTrue(self.q.remove(a.id))
        self.assertFalse(self.q.remove(a.id))
        self.assertEqual(self.q.pop_next()[1], b.id)

    def test_advance_skips_one_song_and_removes_when_done(self):
        req = self._pl("X", 2)
        self.q.add(req)
        self.assertTrue(self.q.advance(req.id))
        self.assertEqual(self.q.pop_next()[0].id, "X1")
        self.assertIsNone(self.q.pop_next())
        self.assertFalse(self.q.advance(req.id))

    def test_version_changes_on_mutation(self):
        v0 = self.q.version
        req = self.q.make_track("c" * 11, "C")
        self.q.add(req)
        self.assertGreater(self.q.version, v0)

    def test_make_playlist_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.q.make_playlist("E", "E", [])


class FakeLibrary:
    def __init__(self, ids):
        self.tracks = [Track(i, "pool-" + i, "POOL") for i in ids]
        self.calls = 0

    def pick(self, exclude=()):
        self.calls += 1
        for t in self.tracks:
            if t.id not in exclude:
                return t
        return self.tracks[0] if self.tracks else None

    def count(self):
        return len(self.tracks)


class FakeAnnouncer:
    files = []

    def pick(self):
        return None


def fake_open(self):
    """Sahte ses aygıtı: gerçek çıkış açılmaz, aygıt varmış gibi davranılır."""
    self._audio_ok = True
    return True


def make_engine(pool_ids):
    cfg = {"crossfade_sec": 0, "announce_enabled": False}
    with mock.patch("audio.Mixer.open", fake_open):
        eng = RadioEngine(cfg, FakeLibrary(pool_ids), FakeAnnouncer())
    return eng


class EngineRequestTest(unittest.TestCase):
    """Motor mantığı: gerçek ses çıkışı ve ağ yok (akışlar bellekte, kısa)."""

    def setUp(self):
        self.patches = [
            mock.patch.object(RadioEngine, "_prefetch", lambda self, t: None),
            mock.patch("audio.Mixer.check_device", lambda self: True),
            mock.patch.object(RadioEngine, "_open_item",
                              lambda self, item: PcmStream(frames=const(SR * 6 // 10, 0.5),
                                                           name=item[1].title)),
        ]
        for p in self.patches:
            p.start()
        self.eng = make_engine(["p1", "p2", "p3"])

    def tearDown(self):
        self.eng.mixer.close()
        for p in self.patches:
            p.stop()

    def play_cycle(self, ticks=1):
        """Bir tick + 0,1 sn ses üretimi (akışları tüketir)."""
        for _ in range(ticks):
            self.eng._tick()
            self.eng.mixer.render(SR // 10)

    def now_title(self):
        return self.eng._cur_item[1].title if self.eng._cur_item else None

    def test_requests_play_before_pool_then_pool_resumes(self):
        eng = self.eng
        q = eng.requests
        eng.add_request(q.make_track("r1" * 6, "R1"))
        eng.add_request(q.make_track("r2" * 6, "R2"))
        seen = []
        for _ in range(30):
            self.play_cycle()
            if not seen or seen[-1] != self.now_title():
                seen.append(self.now_title())
        self.assertEqual(seen[:3], ["R1", "R2", "pool-p1"])
        self.assertEqual(eng._cur_item[0], "track")
        snap = eng.snapshot()
        self.assertNotEqual(snap["kind"], "request")

    def test_request_kind_is_reported(self):
        eng = self.eng
        eng.add_request(eng.requests.make_track("r1" * 6, "R1"))
        self.play_cycle(2)
        self.assertEqual(eng.snapshot()["kind"], "request")
        self.assertEqual(eng._cur_item[0], "request")

    def test_playlist_request_plays_all_tracks_in_order(self):
        eng = self.eng
        tracks = [{"id": "x%d" % i, "title": "X%d" % i} for i in range(3)]
        eng.add_request(eng.requests.make_playlist("PL", "Liste", tracks))
        seen = []
        for _ in range(25):
            self.play_cycle()
            if self.now_title() not in seen:
                seen.append(self.now_title())
        self.assertEqual(seen[:4], ["X0", "X1", "X2", "pool-p1"])

    def test_remove_before_playing_skips_it(self):
        eng = self.eng
        a = eng.requests.make_track("a1" * 6, "A")
        b = eng.requests.make_track("b1" * 6, "B")
        eng.add_request(a)
        eng.add_request(b)
        eng.remove_request(a.id)
        seen = []
        for _ in range(12):
            self.play_cycle()
            if self.now_title() not in seen:
                seen.append(self.now_title())
        self.assertEqual(seen[0], "B")
        self.assertNotIn("A", seen)

    def test_skip_item_advances_queued_playlist(self):
        eng = self.eng
        first = eng.requests.make_track("f1" * 6, "F")
        pl = eng.requests.make_playlist("PL", "Liste", [{"id": "y%d" % i, "title": "Y%d" % i}
                                                         for i in range(3)])
        eng.add_request(first)
        eng.add_request(pl)
        self.play_cycle()                  # F çalıyor; Y0 planlandı
        self.assertEqual(self.now_title(), "F")
        eng.skip_request_item(pl.id)       # planlanan Y0 atlanır, Y1 planlanır
        seen = []
        for _ in range(12):
            self.play_cycle()
            if self.now_title() not in seen:
                seen.append(self.now_title())
        self.assertIn("Y1", seen)
        self.assertNotIn("Y0", seen)

    def test_skip_item_on_playing_request_moves_to_next_song(self):
        eng = self.eng
        pl = eng.requests.make_playlist("PL", "Liste", [{"id": "z%d" % i, "title": "Z%d" % i}
                                                         for i in range(3)])
        eng.add_request(pl)
        self.play_cycle()
        self.assertEqual(self.now_title(), "Z0")
        eng.skip_request_item(pl.id)
        self.play_cycle()
        self.assertEqual(self.now_title(), "Z1")

    def test_request_added_to_idle_queue_switches_now(self):
        eng = self.eng
        self.play_cycle(2)
        self.assertEqual(eng._cur_item[0], "track")  # havuz şarkısı çalıyor
        eng.add_request(eng.requests.make_track("n1" * 6, "NOW"))
        self.play_cycle(2)
        self.assertEqual(self.now_title(), "NOW")
        self.assertEqual(eng._cur_item[0], "request")

    def test_second_request_waits_for_playing_request(self):
        eng = self.eng
        eng.add_request(eng.requests.make_track("a1" * 6, "A"))
        self.play_cycle(2)
        self.assertEqual(self.now_title(), "A")
        eng.add_request(eng.requests.make_track("b1" * 6, "B"))
        self.play_cycle(1)
        self.assertEqual(self.now_title(), "A")  # çalan istek kesilmez
        seen = []
        for _ in range(30):
            self.play_cycle()
            if self.now_title() not in seen:
                seen.append(self.now_title())
        self.assertEqual(seen[:2], ["A", "B"])   # B havuzdan önce, A'nın hemen ardından


if __name__ == "__main__":
    unittest.main()
