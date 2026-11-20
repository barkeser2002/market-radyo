"""Ses aygıtı yokken / koptuğunda motor davranışı. sounddevice sahte modülle değiştirilir."""

import logging
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import player  # noqa: E402
from audio import SR, Mixer, PcmStream  # noqa: E402
from player import AUDIO_RETRY_SEC, RadioEngine  # noqa: E402


class FakeDevice:
    """Sahte PortAudio: fail True iken çıkış açılamaz (aygıt yok)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.opened = []

    def output_stream(self, samplerate, channels, dtype, callback):
        if self.fail:
            raise RuntimeError("Error querying device 999")
        out = FakeOut(callback)
        self.opened.append(out)
        return out


class FakeOut:
    def __init__(self, callback):
        self.callback = callback
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


def fake_sounddevice(device):
    mod = types.ModuleType("sounddevice")
    mod.OutputStream = device.output_stream
    mod._terminate = mock.MagicMock(name="_terminate")
    mod._initialize = mock.MagicMock(name="_initialize")
    return mod


def const(n, v):
    return np.full((n, 2), v, np.float32)


class MixerDeviceTest(unittest.TestCase):
    def test_missing_device_does_not_raise_and_logs_warning_without_traceback(self):
        dev = FakeDevice(fail=True)
        m = Mixer()
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sounddevice(dev)}):
            with self.assertLogs("radyo.audio", level="WARNING") as cm:
                self.assertFalse(m.open())
        self.assertFalse(m.audio_ok())
        self.assertTrue(all(r.levelno == logging.WARNING for r in cm.records))
        self.assertTrue(all(r.exc_info is None for r in cm.records))
        m.close()

    def test_device_appears_later_then_opens(self):
        dev = FakeDevice(fail=True)
        m = Mixer()
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sounddevice(dev)}):
            self.assertFalse(m.open())
            dev.fail = False
            self.assertTrue(m.open())
        self.assertTrue(m.audio_ok())
        self.assertEqual(len(dev.opened), 1)
        self.assertTrue(dev.opened[0].active)
        m.close()

    def test_device_drop_is_detected_and_closed(self):
        dev = FakeDevice()
        m = Mixer()
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sounddevice(dev)}):
            self.assertTrue(m.open())
            self.assertTrue(m.check_device())
            dev.opened[0].active = False          # aygıt çıkarıldı
            self.assertFalse(m.check_device())
        self.assertFalse(m.audio_ok())
        self.assertTrue(dev.opened[0].closed)

    def test_stalled_callback_counts_as_drop(self):
        dev = FakeDevice()
        m = Mixer()
        with mock.patch.dict(sys.modules, {"sounddevice": fake_sounddevice(dev)}):
            self.assertTrue(m.open())
            m._last_cb = time.monotonic() - 10.0  # geri çağırma 10 sn'dir gelmiyor
            self.assertFalse(m.check_device())
        self.assertFalse(m.audio_ok())

    def test_check_device_without_stream_is_false(self):
        self.assertFalse(Mixer().check_device())


class EngineDeviceTest(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDevice(fail=True)
        self.fake = fake_sounddevice(self.dev)
        self.patch = mock.patch.dict(sys.modules, {"sounddevice": self.fake})
        self.patch.start()
        self.library = mock.MagicMock()
        self.announcer = mock.MagicMock()
        self.announcer.files = []
        # Ağ çağrısı yapmasın: ön yükleme iş parçacığı devre dışı.
        self.prefetch = mock.patch.object(RadioEngine, "_prefetch", lambda self_, track: None)
        self.prefetch.start()
        self.engine = None

    def tearDown(self):
        if self.engine is not None:
            self.engine.stop()
        self.prefetch.stop()
        self.patch.stop()

    def make_engine(self):
        self.engine = RadioEngine({}, self.library, self.announcer)
        return self.engine

    def test_engine_starts_without_device(self):
        eng = self.make_engine()
        snap = eng.snapshot()
        self.assertFalse(snap["audio_ok"])
        self.assertIsNone(eng.mixer.current())

    def test_tick_without_device_does_not_pick_or_play(self):
        eng = self.make_engine()
        eng._tick()
        eng._tick()
        self.library.pick.assert_not_called()
        self.assertIsNone(eng.mixer.current())
        self.assertIsNone(eng.mixer.incoming())

    def test_retry_waits_interval_then_plays_when_device_appears(self):
        eng = self.make_engine()
        track = SimpleNamespace(id="v1", title="Parça", playlist_id="p")
        self.library.pick.return_value = track
        stream = PcmStream(frames=const(SR * 5, 0.1), duration=5.0, name="Parça")
        eng._open_item = lambda item: stream

        # Aralık dolmadan yeniden deneme yok
        eng._tick()
        self.assertFalse(eng.snapshot()["audio_ok"])
        self.assertEqual(self.dev.opened, [])

        # Süre doldu, aygıt hâlâ yok: sürücü yeniden başlatılır, açılamaz
        eng._audio_next_try = 0.0
        eng._tick()
        self.assertFalse(eng.snapshot()["audio_ok"])
        self.assertTrue(self.fake._terminate.called)
        self.assertTrue(self.fake._initialize.called)

        # Aygıt geldi: bir sonraki denemede açılır ve şarkı başlar
        self.dev.fail = False
        eng._audio_next_try = 0.0
        eng._tick()
        snap = eng.snapshot()
        self.assertTrue(snap["audio_ok"])
        self.assertEqual(len(self.dev.opened), 1)
        self.assertEqual(snap["id"], "v1")
        self.assertIsNotNone(eng.mixer.incoming() or eng.mixer.current())
        self.assertGreater(AUDIO_RETRY_SEC, 0)

    def test_outage_during_playback_resumes_from_position(self):
        self.dev.fail = False
        eng = self.make_engine()
        self.assertTrue(eng.snapshot()["audio_ok"])
        track = SimpleNamespace(id="v2", title="Uzun", playlist_id="p")
        item = ("track", track)
        # 60 sn'lik şarkı, 2 sn tamponlu ve kısmen okunmuş (eof: kaynak kesildi)
        stream = PcmStream(frames=const(SR * 2, 0.1), duration=60.0, name="Uzun")
        eng.mixer.transition(stream, 0.0)
        eng._cur_item = item
        eng.mixer.render(SR // 2)  # 0.5 sn çalındı

        # Aygıt koptu
        self.dev.opened[0].active = False
        self.assertFalse(eng._audio_tick())
        self.assertFalse(eng.snapshot()["audio_ok"])

        # Aygıt geri geldi: bozuk akış aynı konumdan (0.5 sn) yeniden açılmalı
        reopened = []

        def fake_open_at(it, pos):
            reopened.append((it, pos))
            return PcmStream(frames=const(SR * 10, 0.2), duration=60.0, name="Uzun", start=pos)

        eng._open_at = fake_open_at
        self.dev.fail = False
        eng._audio_next_try = 0.0
        self.assertTrue(eng._audio_tick())
        self.assertTrue(eng.snapshot()["audio_ok"])
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0][0], item)
        self.assertAlmostEqual(reopened[0][1], 0.5, places=3)
        self.assertTrue(stream.closed)
        self.assertIsNotNone(eng.mixer.incoming() or eng.mixer.current())

    def test_healthy_stream_continues_without_reopen(self):
        self.dev.fail = False
        eng = self.make_engine()
        track = SimpleNamespace(id="v3", title="Tam", playlist_id="p")
        stream = PcmStream(frames=const(SR * 60, 0.1), duration=60.0, name="Tam")
        eng.mixer.transition(stream, 0.0)
        eng._cur_item = ("track", track)
        self.dev.opened[0].active = False
        self.assertFalse(eng._audio_tick())

        eng._open_at = mock.MagicMock(side_effect=AssertionError("yeniden açılmamalı"))
        eng._audio_next_try = 0.0
        self.assertTrue(eng._audio_tick())
        eng._open_at.assert_not_called()
        self.assertIs(eng.mixer.current(), stream)


if __name__ == "__main__":
    unittest.main()
