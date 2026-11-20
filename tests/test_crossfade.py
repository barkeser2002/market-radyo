import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import SR, Mixer, PcmStream  # noqa: E402


def const(n, v):
    return np.full((n, 2), v, np.float32)


def sine(n, freq, amp):
    t = np.arange(n) / float(SR)
    s = (amp * np.sin(2 * math.pi * freq * t)).astype(np.float32)
    return np.stack([s, s], axis=1)


class CrossfadeTest(unittest.TestCase):
    def test_equal_power_midpoint(self):
        # Sabit 1.0 iki sinyal: doğrusal geçişte orta nokta 1.0, eşit güçte √2 olmalı
        m = Mixer()
        m.transition(PcmStream(frames=const(SR, 1.0)), 0)
        m.transition(PcmStream(frames=const(SR, 1.0)), 1.0)
        out = np.concatenate([m.render(SR // 2), m.render(SR // 2)])
        mid = SR // 2
        self.assertAlmostEqual(float(out[mid, 0]), math.sqrt(2.0), places=3)
        self.assertAlmostEqual(float(out[0, 0]), 1.0, places=4)
        self.assertAlmostEqual(float(out[-1, 0]), 1.0, places=3)

    def test_sine_mix_matches_formula(self):
        n = SR  # 1 sn
        fade = SR // 2
        a = sine(n, 440, 0.5)
        b = sine(n, 660, 0.5)
        m = Mixer()
        m.transition(PcmStream(frames=a), 0)
        m.transition(PcmStream(frames=b), fade / float(SR))
        out = np.concatenate([m.render(n // 2), m.render(n - n // 2)])
        t = np.arange(n)
        r = np.clip(t / float(fade), 0.0, 1.0)[:, None]
        expected = a * np.cos(r * math.pi / 2) + b * np.sin(r * math.pi / 2)
        self.assertLess(float(np.max(np.abs(out - expected))), 1e-5)

    def test_transition_finishes_and_closes_old(self):
        m = Mixer()
        a = PcmStream(frames=const(SR, 0.2))
        b = PcmStream(frames=const(SR, 0.3))
        m.transition(a, 0)
        m.transition(b, 0.1)
        self.assertIs(m.incoming(), b)
        self.assertTrue(m.fading())
        m.render(SR // 5)  # 0.2 sn > 0.1 sn geçiş
        self.assertIs(m.current(), b)
        self.assertIsNone(m.incoming())
        self.assertFalse(m.fading())
        self.assertTrue(a.closed)

    def test_fade_out_only_ends_silent(self):
        m = Mixer()
        m.transition(PcmStream(frames=const(SR, 0.5)), 0)
        m.transition(None, 0.1)
        m.render(SR // 5)
        self.assertIsNone(m.current())
        self.assertFalse(m.fading())

    def test_paused_is_silent_and_volume_scales(self):
        m = Mixer()
        m.transition(PcmStream(frames=const(SR, 0.5)), 0)
        m.paused = True
        self.assertTrue(np.all(m.render(256) == 0))
        m.paused = False
        m.set_volume(0.5)
        self.assertAlmostEqual(float(m.render(256)[0, 0]), 0.25, places=5)  # 0.5 * 0.5

    def test_pcm_stream_pull_and_remaining(self):
        s = PcmStream(frames=const(100, 0.25))
        out, got = s.pull(60)
        self.assertEqual(got, 60)
        self.assertEqual(out.shape, (60, 2))
        self.assertEqual(s.remaining_frames(), 40.0)
        out, got = s.pull(60)
        self.assertEqual(got, 40)
        self.assertTrue(np.all(out[40:] == 0))  # tampon bitince sessizlik
        self.assertTrue(s.is_finished())


class RealFfmpegStreamTest(unittest.TestCase):
    def test_wav_decodes_to_pcm_through_ffmpeg(self):
        import tempfile
        import subprocess
        from ffmpeg_util import ffmpeg_path
        from player import input_cmd
        try:
            exe = ffmpeg_path()
        except FileNotFoundError:
            self.skipTest("ffmpeg yok")
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d, "tone.wav")
            subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", str(wav)], check=True)
            s = PcmStream(input_cmd(str(wav)), name="tone")
            total = 0
            while True:
                _, got = s.pull(4410)
                total += got
                if got == 0 and s.is_finished():
                    break
                if got == 0:
                    s.cv.acquire(); s.cv.wait(0.05); s.cv.release()
            s.close()
        self.assertFalse(s.error)
        self.assertAlmostEqual(total, SR, delta=SR // 50)


if __name__ == "__main__":
    unittest.main()
