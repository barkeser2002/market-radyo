"""Gerçek ağ testleri: YouTube araması ve bir istek şarkısının PCM akışı.

İnternet yoksa atlanır. Cookie kullanılmaz, hiçbir şey indirilmez (yalnızca ses akışı çözülür).
"""

import sys
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audio import SR, PcmStream  # noqa: E402
from istek import search_all  # noqa: E402
from player import input_cmd, internet_ok, resolve_stream  # noqa: E402

ONLINE = internet_ok()


@unittest.skipUnless(ONLINE, "internet yok")
class LiveSearchTest(unittest.TestCase):
    def test_search_returns_songs_and_playlists(self):
        data = search_all("daft punk get lucky")
        self.assertEqual(len(data["songs"]), 5)
        self.assertGreaterEqual(len(data["playlists"]), 1)
        for s in data["songs"]:
            self.assertTrue(s["id"])
            self.assertTrue(s["title"])
        for p in data["playlists"]:
            self.assertTrue(p["id"])
            self.assertTrue(p["title"])

    def test_request_song_pcm_arrives(self):
        song = search_all("daft punk get lucky")["songs"][0]
        url, headers, duration = resolve_stream(song["id"])
        s = PcmStream(input_cmd(url, headers), duration=duration, name=song["title"])
        try:
            deadline = time.time() + 60
            while s.buffered_frames() < SR and not s.eof and time.time() < deadline:
                time.sleep(0.05)
            self.assertFalse(s.error)
            self.assertGreaterEqual(s.buffered_frames(), SR)
            out, got = s.pull(SR)
            self.assertEqual(got, SR)
            self.assertGreater(float(np.abs(out).mean()), 1e-4)  # sessiz değil
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
