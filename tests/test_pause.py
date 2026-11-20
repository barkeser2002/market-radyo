import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import DUCK_UP_SEC, SR, Mixer, PcmStream, eqp_gains  # noqa: E402
from config import DEFAULT_CONFIG  # noqa: E402
from player import (FADE_DEFAULT, FADE_MAX, clamp_announce_mode, clamp_fade,  # noqa: E402
                    input_cmd)

BLOCK = 441  # 0,01 sn


def const(n, v):
    return np.full((n, 2), v, np.float32)


def run(m, seconds):
    """seconds sn ses üretir (BLOCK'lık parçalarla); birleşik dizi döner."""
    total = int(round(seconds * SR))
    parts = []
    done = 0
    while done < total:
        n = min(BLOCK, total - done)
        parts.append(m.render(n))
        done += n
    return np.concatenate(parts) if parts else np.zeros((0, 2), np.float32)


class FadeCurveTest(unittest.TestCase):
    def test_fade_out_is_cos_and_fade_in_is_sin(self):
        n = 1000
        # n+1 kare: son kare r = 1 (geçişin tam sonu)
        g = eqp_gains(0, n, n + 1, 1.0, 0.0)
        h = eqp_gains(0, n, n + 1, 0.0, 1.0)
        self.assertAlmostEqual(float(g[0]), 1.0, places=9)
        self.assertAlmostEqual(float(g[-1]), 0.0, places=9)
        self.assertAlmostEqual(float(h[0]), 0.0, places=9)
        self.assertAlmostEqual(float(h[-1]), 1.0, places=9)
        mid = n // 2
        self.assertAlmostEqual(float(g[mid]), math.cos(mid / float(n) * math.pi / 2), places=9)
        # equal-power: karelerin hepsinde g^2 + h^2 = 1
        self.assertTrue(np.allclose(g ** 2 + h ** 2, 1.0, atol=1e-9))

    def test_fade_is_monotonic(self):
        g = eqp_gains(0, SR, SR, 1.0, 0.0)
        h = eqp_gains(0, SR, SR, 0.0, 1.0)
        self.assertTrue(np.all(np.diff(g) <= 1e-12))
        self.assertTrue(np.all(np.diff(h) >= -1e-12))

    def test_zero_length_is_immediate_target(self):
        self.assertTrue(np.allclose(eqp_gains(0, 0, 4, 1.0, 0.0), 0.0))
        self.assertTrue(np.allclose(eqp_gains(0, 0, 4, 0.0, 1.0), 1.0))

    def test_fade_clamp_and_mode_clamp(self):
        self.assertEqual(DEFAULT_CONFIG["announce_mode"], "duck")
        self.assertEqual(DEFAULT_CONFIG["announce_fade_sec"], FADE_DEFAULT)
        self.assertEqual(FADE_DEFAULT, 2.0)
        self.assertEqual(FADE_MAX, 3.0)
        self.assertEqual(clamp_fade(10), 3.0)
        self.assertEqual(clamp_fade(-1), 0.0)
        self.assertEqual(clamp_fade("x"), FADE_DEFAULT)
        self.assertEqual(clamp_announce_mode("PAUSE"), "pause")
        self.assertEqual(clamp_announce_mode("xyz"), "duck")
        self.assertEqual(clamp_announce_mode(None), "duck")


class ResumeCommandTest(unittest.TestCase):
    def test_no_seek_without_start(self):
        cmd = input_cmd("http://x/a.m4a")
        self.assertNotIn("-ss", cmd)

    def test_seek_is_before_input(self):
        cmd = input_cmd("http://x/a.m4a", {"User-Agent": "ua"}, start=73.25)
        self.assertIn("-ss", cmd)
        i = cmd.index("-ss")
        self.assertEqual(cmd[i + 1], "73.250")
        self.assertLess(i, cmd.index("-i"))

    def test_seek_zero_or_negative_ignored(self):
        self.assertNotIn("-ss", input_cmd("x.mp3", start=0))
        self.assertNotIn("-ss", input_cmd("x.mp3", start=-5))


class StreamPositionTest(unittest.TestCase):
    def test_position_includes_start_offset(self):
        s = PcmStream(frames=const(SR * 4, 0.1), duration=100.0, start=30.0)
        self.assertAlmostEqual(s.position_sec(), 30.0)
        s.pull(SR)
        self.assertAlmostEqual(s.position_sec(), 31.0, places=4)

    def test_remaining_respects_start(self):
        # süresi 100 sn, 40. saniyeden başlayan akış: kalan 60 sn (tampon küçük)
        s = PcmStream(frames=const(SR, 0.1))
        s.eof = False
        s.duration = 100.0
        s.start = 40.0
        self.assertAlmostEqual(s.remaining_frames() / SR, 60.0, places=3)

    def test_resumable_rules(self):
        ok = PcmStream(frames=const(SR, 0.1), duration=None)
        self.assertTrue(ok.resumable())
        ok.error = True
        self.assertFalse(ok.resumable())
        whole = PcmStream(frames=const(SR * 10, 0.1), duration=10.0)
        self.assertTrue(whole.resumable())   # tamamı tamponda: yarım değil
        short = PcmStream(frames=const(SR * 3, 0.1), duration=10.0)
        self.assertFalse(short.resumable())  # 10 sn'lik parçanın 3 sn'i var: yarım kalmış


class DeadProcessTest(unittest.TestCase):
    def test_exited_ffmpeg_is_not_resumable_even_before_reader_notices(self):
        import subprocess
        import sys as _sys
        import time as _t
        # Süreç hata koduyla çıkar; okuma iş parçacığı tamponlu olduğu için henüz bayrak koymamış olabilir
        s = PcmStream(cmd=[_sys.executable, "-c", "import sys; sys.exit(3)"], name="olu")
        s.proc.wait(timeout=10)
        _t.sleep(0.2)
        with s.cv:
            s.error = False
        self.assertFalse(s.resumable())
        s.close()
        self.assertIsInstance(s.proc, subprocess.Popen)


class PauseModeMixerTest(unittest.TestCase):
    """Müzik 1.0, anons 0.3 (sabit). Anons 3 sn, geçiş 0.5 sn."""

    def setUp(self):
        self.m = Mixer()
        self.music = PcmStream(frames=const(SR * 60, 1.0), name="music", duration=60.0)
        self.m.transition(self.music, 0)
        run(self.m, 0.1)  # müzik çalıyor

    def tearDown(self):
        self.m.close()

    def test_music_ducks_out_then_is_held_at_exact_position(self):
        ann = PcmStream(frames=const(SR * 3, 0.3), name="ann")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.5)
        self.assertEqual(self.m.hold_state(), "fading")
        run(self.m, 0.5)
        self.assertEqual(self.m.hold_state(), "held")
        self.assertIsNone(self.m.current())
        held = self.m.held_stream()
        self.assertIs(held, self.music)
        self.assertFalse(held.closed)                         # kapatılmadı
        # konum: ilk 0.1 sn + 0.5 sn geçiş = 0.6 sn
        self.assertAlmostEqual(held.position_sec(), 0.6, places=3)

    def test_announcement_full_volume_while_music_held(self):
        ann = PcmStream(frames=const(SR * 3, 0.3), name="ann")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.5)
        run(self.m, 0.6)
        out = run(self.m, 0.2)
        self.assertAlmostEqual(float(out[-1, 0]), 0.3, places=4)  # müzik yok, anons tam sesle

    def test_resume_continues_from_held_position_with_fade_in(self):
        ann = PcmStream(frames=const(SR * 2, 0.3), name="ann")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.5)
        run(self.m, 2.0)                                      # anons tam 2 sn'de biter
        self.assertFalse(self.m.announcement_active())
        self.assertIsNone(self.m.hold_state())
        pos_before = self.music.position_sec()
        self.assertAlmostEqual(pos_before, 0.6, places=3)     # kaldığı yer korunmuş
        out = run(self.m, 0.5)                                # geri dönüş geçişi
        self.assertLess(float(out[0, 0]), 0.05)               # sessizlikten başlar
        self.assertGreater(float(out[-1, 0]), 0.95)           # ~tam sese yükselir
        run(self.m, 0.05)
        self.assertIs(self.m.current(), self.music)
        self.assertGreater(self.music.position_sec(), pos_before)  # şarkı kaldığı yerden devam eder

    def test_duck_level_is_not_used_in_pause_mode(self):
        self.m.set_duck_level(0.15)
        ann = PcmStream(frames=const(SR * 3, 0.0), name="sessiz")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.5)
        run(self.m, 0.6)
        self.m.set_duck_level(0.5)                            # yalnızca duck modunda uygulanır
        self.assertEqual(self.m.hold_state(), "held")

    def test_broken_held_stream_asks_engine_to_reopen(self):
        ann = PcmStream(frames=const(SR * 1, 0.3), name="ann")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.2)
        run(self.m, 0.3)
        self.music.error = True                               # bağlantı koptu
        run(self.m, 1.0)                                      # anons biter
        self.assertTrue(self.m.resume_wanted())
        self.assertEqual(self.m.hold_state(), "held")
        self.assertLess(float(np.max(np.abs(run(self.m, 0.1)))), 1e-6)  # bu arada sessiz

        fresh = PcmStream(frames=const(SR * 60, 1.0), name="fresh", duration=60.0, start=0.6)
        self.assertTrue(self.m.resume_held(fresh))
        self.assertTrue(self.music.closed)                    # eski akış kapatıldı
        self.assertFalse(self.m.resume_wanted())
        run(self.m, 0.3)
        self.assertIs(self.m.current(), fresh)

    def test_resume_held_without_held_returns_false(self):
        fresh = PcmStream(frames=const(SR, 1.0))
        self.assertFalse(self.m.resume_held(fresh))

    def test_drop_hold_closes_held_song(self):
        ann = PcmStream(frames=const(SR * 3, 0.3), name="ann")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.2)
        run(self.m, 0.3)
        self.assertEqual(self.m.hold_state(), "held")
        self.m.drop_hold()
        self.assertTrue(self.music.closed)
        self.assertIsNone(self.m.hold_state())
        self.assertIsNone(self.m.held_stream())
        self.assertTrue(self.m.announcement_active())         # anons etkilenmez
        run(self.m, 3.0)
        self.assertFalse(self.m.announcement_active())
        self.assertIsNone(self.m.current())                   # şarkı yok: motor sıradakini başlatır

    def test_announcement_ends_before_fade_cancels_hold(self):
        ann = PcmStream(frames=const(SR // 10, 0.3), name="kisa")
        self.m.play_announcement(ann, mode="pause", fade_sec=0.5)
        run(self.m, 0.2)
        self.assertIsNone(self.m.hold_state())
        self.assertIs(self.m.current(), self.music)
        out = run(self.m, 0.5)
        self.assertGreater(float(out[-1, 0]), 0.9)            # şarkı tam sesle sürer

    def test_pause_without_music_plays_announcement_only(self):
        m = Mixer()
        ann = PcmStream(frames=const(SR, 0.3), name="tek")
        m.play_announcement(ann, mode="pause", fade_sec=0.5)
        self.assertIsNone(m.hold_state())
        out = run(m, 0.6)
        self.assertGreater(float(out[-1, 0]), 0.29)
        m.close()

    def test_duck_mode_still_keeps_song_playing(self):
        ann = PcmStream(frames=const(SR * 2, 0.0), name="duck")
        self.m.play_announcement(ann, mode="duck")
        run(self.m, 1.0)
        self.assertIs(self.m.current(), self.music)
        self.assertIsNone(self.m.hold_state())
        self.assertTrue(self.m.announcement_active())
        self.assertEqual(DUCK_UP_SEC, 1.0)


if __name__ == "__main__":
    unittest.main()
