import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import (DUCK_DEFAULT, DUCK_DOWN_SEC, DUCK_UP_SEC, SR, Mixer,  # noqa: E402
                   PcmStream, duck_gains, soft_limit)
from config import DEFAULT_CONFIG  # noqa: E402
from player import (ANNOUNCE_MIN_DEFAULT, AnnounceTimer,  # noqa: E402
                    clamp_announce_min, clamp_duck)


def const(n, v):
    return np.full((n, 2), v, np.float32)


def tone(n, freq, amp):
    t = np.arange(n) / float(SR)
    s = (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)
    return np.stack([s, s], axis=1)


def amp_at(x, freq):
    """x (n,2) içindeki sol kanal sinüs genliği; n tam sayıda periyot içermeli (1 sn pencere)."""
    t = np.arange(len(x)) / float(SR)
    return float(2.0 * abs(np.mean(x[:, 0] * np.exp(-2j * math.pi * freq * t))))


class DuckCurveTest(unittest.TestCase):
    def test_endpoints_and_midpoint(self):
        g = duck_gains(0, 100, 101, 1.0, 0.15)
        self.assertAlmostEqual(float(g[0]), 1.0, places=9)
        self.assertAlmostEqual(float(g[100]), 0.15, places=9)
        self.assertAlmostEqual(float(g[50]), 0.575, places=6)  # (1 + 0.15) / 2

    def test_monotonic_down_and_up(self):
        down = duck_gains(0, SR, SR, 1.0, 0.15)
        up = duck_gains(0, SR, SR, 0.15, 1.0)
        self.assertTrue(np.all(np.diff(down) <= 1e-12))
        self.assertTrue(np.all(np.diff(up) >= -1e-12))
        self.assertAlmostEqual(float(down[-1]), 0.15, places=3)
        self.assertAlmostEqual(float(up[-1]), 1.0, places=3)

    def test_zero_length_gives_target(self):
        g = duck_gains(0, 0, 8, 1.0, 0.15)
        self.assertTrue(np.allclose(g, 0.15, atol=1e-12))

    def test_duck_default_is_fifteen_percent(self):
        self.assertEqual(DUCK_DEFAULT, 0.15)
        self.assertEqual(DEFAULT_CONFIG["duck_level"], 0.15)


class SoftLimitTest(unittest.TestCase):
    def test_below_knee_is_unchanged(self):
        x = np.array([0.0, 0.5, -0.8, 0.9], dtype=np.float64)
        self.assertIs(soft_limit(x), x)

    def test_above_knee_is_bounded_and_monotonic(self):
        x = np.linspace(0.0, 2.0, 300)
        y = soft_limit(x)
        self.assertLess(float(np.max(y)), 1.0)
        self.assertTrue(np.all(np.diff(y) >= -1e-12))
        self.assertTrue(np.all(np.sign(soft_limit(-x)) == -np.sign(x)))


class DuckMixerTest(unittest.TestCase):
    def test_duck_down_then_back_up(self):
        m = Mixer()
        m.transition(PcmStream(frames=const(4 * SR, 0.5)), 0)
        ann = PcmStream(frames=const(2 * SR, 0.2), name="ann")
        m.render(SR // 10)  # ses akışı başlasın
        m.play_announcement(ann)
        self.assertTrue(m.announcement_active())

        out = m.render(SR // 2)            # duck iniş süresi tamamlanır
        self.assertAlmostEqual(float(out[-1, 0]), 0.5 * 0.15 + 0.2, places=3)

        out = m.render(SR // 2)            # steady duck: müzik 0.15, anons 0.2
        self.assertAlmostEqual(float(out[0, 0]), 0.5 * 0.15 + 0.2, places=4)

        m.render(SR)                       # anons biter (toplam 2 sn)
        self.assertFalse(m.announcement_active())

        m.render(int(DUCK_UP_SEC * SR))    # 1 sn içinde eski seviyeye dönüş
        out = m.render(256)
        self.assertAlmostEqual(float(out[0, 0]), 0.5, places=4)

    def test_duck_is_gradual_not_instant(self):
        m = Mixer()
        m.transition(PcmStream(frames=const(2 * SR, 0.5)), 0)
        m.render(256)
        m.play_announcement(PcmStream(frames=const(SR, 0.0), name="sessiz"))
        out = m.render(SR // 2)
        self.assertGreater(float(out[0, 0]), 0.45)        # ilk kare hâlâ neredeyse tam ses
        self.assertLess(float(out[-1, 0]), 0.5 * 0.15 + 0.01)

    def test_announcement_is_full_volume_over_ducked_music(self):
        m = Mixer()
        m.transition(PcmStream(frames=const(3 * SR, 0.4)), 0)
        m.play_announcement(PcmStream(frames=const(SR, 0.3), name="a"))
        m.render(int(DUCK_DOWN_SEC * SR))
        out = m.render(256)
        self.assertAlmostEqual(float(out[0, 0]), 0.4 * 0.15 + 0.3, places=4)

    def test_set_duck_level_applies_while_announcing(self):
        m = Mixer()
        m.transition(PcmStream(frames=const(3 * SR, 0.5)), 0)
        m.play_announcement(PcmStream(frames=const(2 * SR, 0.0), name="a"))
        m.set_duck_level(0.5)
        m.render(int(DUCK_DOWN_SEC * SR) + 1)
        out = m.render(256)
        self.assertAlmostEqual(float(out[0, 0]), 0.25, places=4)

    def test_two_channels_sum(self):
        m = Mixer()
        m.set_duck_level(1.0)  # kısma yok: saf toplam
        m.transition(PcmStream(frames=const(SR, 0.3)), 0)
        m.play_announcement(PcmStream(frames=const(SR, 0.4), name="a"))
        out = m.render(512)
        self.assertAlmostEqual(float(out[0, 0]), 0.7, places=5)

    def test_two_loud_channels_are_soft_limited(self):
        m = Mixer()
        m.set_duck_level(1.0)
        m.transition(PcmStream(frames=const(SR, 0.7)), 0)
        m.play_announcement(PcmStream(frames=const(SR, 0.7), name="a"))
        out = m.render(512)
        self.assertLess(float(np.max(out)), 1.0)
        self.assertGreater(float(out[0, 0]), 0.9)

    def test_master_volume_scales_music_and_announcement(self):
        m = Mixer()
        m.set_duck_level(1.0)
        m.transition(PcmStream(frames=const(SR, 0.3)), 0)
        m.play_announcement(PcmStream(frames=const(SR, 0.2), name="a"))
        m.set_volume(0.5)
        out = m.render(256)
        self.assertAlmostEqual(float(out[0, 0]), 0.25, places=5)

    def test_skip_music_keeps_announcement(self):
        # Müzik kanalı değişince (geçiş) anons kanalı etkilenmez
        m = Mixer()
        m.set_duck_level(1.0)
        m.transition(PcmStream(frames=const(SR, 0.0)), 0)
        ann = PcmStream(frames=const(SR, 0.2), name="a")
        m.play_announcement(ann)
        m.transition(PcmStream(frames=const(SR, 0.1)), 0.0)
        out = m.render(256)
        self.assertAlmostEqual(float(out[0, 0]), 0.3, places=5)
        self.assertIs(m._ann, ann)


class AnnounceTimerTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.timer = AnnounceTimer(10 * 60, clock=lambda: self.now)

    def run_for(self, seconds, counting, step=0.1):
        due = False
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now += step
            due = self.timer.update(counting) or due
        return due

    def test_fires_after_interval_of_playing_time(self):
        self.timer.update(True)
        self.assertFalse(self.run_for(599, True))
        self.assertTrue(self.run_for(2, True))

    def test_pause_does_not_count(self):
        self.timer.update(True)
        self.run_for(300, True)
        self.run_for(1000, False)           # duraklatma
        self.assertFalse(self.run_for(299, True))
        self.assertTrue(self.run_for(2, True))

    def test_remaining_decreases(self):
        self.timer.update(True)
        self.run_for(120, True)
        self.assertAlmostEqual(self.timer.remaining(), 480, delta=0.2)

    def test_reset_restarts_count(self):
        self.timer.update(True)
        self.run_for(590, True)
        self.timer.reset()
        self.timer.update(True)
        self.assertFalse(self.run_for(300, True))
        self.assertAlmostEqual(self.timer.remaining(), 300, delta=0.2)

    def test_interval_change_applies_immediately(self):
        self.timer.update(True)
        self.run_for(200, True)
        self.timer.set_interval(60)
        self.assertTrue(self.timer.update(True))

    def test_minute_clamp_and_default(self):
        self.assertEqual(ANNOUNCE_MIN_DEFAULT, 10)
        self.assertEqual(DEFAULT_CONFIG["announce_every_min"], 10)
        self.assertNotIn("announce_every", DEFAULT_CONFIG)
        self.assertEqual(clamp_announce_min(0), 1)
        self.assertEqual(clamp_announce_min(999), 60)
        self.assertEqual(clamp_announce_min("abc"), 10)
        self.assertEqual(clamp_announce_min(7.4), 7)

    def test_duck_clamp(self):
        self.assertEqual(clamp_duck(-1), 0.0)
        self.assertEqual(clamp_duck(3), 1.0)
        self.assertEqual(clamp_duck("x"), DUCK_DEFAULT)


class RealFfmpegDuckingTest(unittest.TestCase):
    """Gerçek ffmpeg ile üretilmiş sinüs müzik (440 Hz) ve anons (1500 Hz) ile ducking doğrulaması."""

    def _wait(self, stream, n):
        deadline = time.time() + 20
        while stream.buffered_frames() < n and not stream.eof and time.time() < deadline:
            time.sleep(0.01)

    def test_music_ducks_and_recovers_with_real_audio(self):
        import subprocess as sp
        from ffmpeg_util import ffmpeg_path
        from player import input_cmd
        try:
            exe = ffmpeg_path()
        except FileNotFoundError:
            self.skipTest("ffmpeg yok")
        with tempfile.TemporaryDirectory() as d:
            music = Path(d, "music.wav")
            ann = Path(d, "ann.wav")
            sp.run([exe, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=8", "-af", "volume=0.5", str(music)],
                   check=True)
            sp.run([exe, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=1500:duration=2", "-af", "volume=0.5", str(ann)],
                   check=True)
            m = Mixer()
            ms = PcmStream(input_cmd(str(music)), name="music")
            m.transition(ms, 0)

            def block():
                # 1 sn'lik blok: kaynak en az 1 sn tamponlanana kadar bekler (1 sn = integer periyot)
                self._wait(ms, SR)
                return m.render(SR)

            # Genlikler ffmpeg'in sine kaynağına göre değişebilir; bu yüzden göreli karşılaştırılır.
            pre = block()                          # müzik 0-1 sn: yalnızca müzik
            a_music = amp_at(pre, 440)
            self.assertGreater(a_music, 0.01)
            self.assertLess(amp_at(pre, 1500), 0.005)

            an = PcmStream(input_cmd(str(ann)), name="ann")
            self._wait(an, SR * 2)
            m.play_announcement(an)                # anons 1 sn'de başlar, şarkı kesilmez
            during = block()                       # müzik 1-2 sn: kısılma + anons 0-1 sn
            a_ann = amp_at(during, 1500)
            self.assertGreater(a_ann, 0.01)
            self.assertTrue(m.announcement_active())

            steady = block()                       # müzik 2-3 sn: duck tamam, anons 1-2 sn ve biter
            self.assertAlmostEqual(amp_at(steady, 440) / a_music, DUCK_DEFAULT, delta=0.02)
            self.assertAlmostEqual(amp_at(steady, 1500) / a_ann, 1.0, delta=0.02)  # anons tam sesle
            self.assertFalse(m.announcement_active())

            up = block()                           # müzik 3-4 sn: 1 sn içinde eski sese dönüş
            self.assertLess(amp_at(up, 440), a_music * 0.9)
            back = block()                         # müzik 4-5 sn: tam ses
            self.assertAlmostEqual(amp_at(back, 440) / a_music, 1.0, delta=0.02)
            self.assertLess(amp_at(back, 1500), 0.005)
            m.close()


if __name__ == "__main__":
    unittest.main()
