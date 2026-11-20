import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ads  # noqa: E402
from ffmpeg_util import ffmpeg_path  # noqa: E402


def _ffmpeg_available():
    try:
        subprocess.run([ffmpeg_path(), "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def make_tone(path, seconds=0.5):
    subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=%s" % seconds, str(path)], check=True)
    return path


@unittest.skipUnless(_ffmpeg_available(), "ffmpeg yok")
class ConvertTest(unittest.TestCase):
    def test_wav_converted_and_original_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            wav = make_tone(Path(d, "a.wav"))
            out = ads.convert_to_mp3(wav)
            self.assertEqual(out, Path(d, "a.mp3"))
            self.assertTrue(out.is_file())
            self.assertTrue(ads._ffmpeg_ok(out))
            self.assertFalse(wav.exists())

    def test_name_collision_gets_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            make_tone(Path(d, "x.wav"))
            ads.convert_to_mp3(Path(d, "x.wav"))  # x.mp3 üretir (x.wav silinir)
            make_tone(Path(d, "x.wav"))           # aynı adla yeni bir wav
            out = ads.convert_to_mp3(Path(d, "x.wav"))
            self.assertEqual(out.name, "x_1.mp3")
            self.assertTrue(Path(d, "x.mp3").is_file())
            self.assertFalse(Path(d, "x.wav").exists())

    def test_broken_original_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d, "b.wav")
            bad.write_bytes(b"bu bir ses dosyasi degil" * 10)
            self.assertIsNone(ads.convert_to_mp3(bad))
            self.assertTrue(bad.exists())
            self.assertFalse(Path(d, "b.mp3").exists())

    def test_prepare_converts_and_drops_unplayable(self):
        with tempfile.TemporaryDirectory() as d:
            make_tone(Path(d, "good.wav"))
            Path(d, "bad.mp3").write_bytes(b"bozuk" * 100)
            files = ads.prepare_announcements(d)
            names = sorted(Path(f).name for f in files)
            self.assertEqual(names, ["good.mp3"])
            self.assertTrue(Path(d, "bad.mp3").exists())  # orijinal korunur
            self.assertFalse(Path(d, "good.wav").exists())

    def test_prepare_missing_folder(self):
        self.assertEqual(ads.prepare_announcements(Path(tempfile.gettempdir(), "yok_klasor_xyz")), [])


if __name__ == "__main__":
    unittest.main()
