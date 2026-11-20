import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import updater  # noqa: E402
from playlists import Library  # noqa: E402
from updater import (  # noqa: E402
    Updater, build_update_script, is_newer, parse_sha256, parse_version, verify_sha256,
)

REPO = "barkeser2002/market-radyo"
CFG = {"update_repo": REPO, "update_branch": "main"}
EXE_BYTES = b"MZ-fake-exe-payload" * 50


class FakeResp:
    def __init__(self, json_data=None, text="", chunks=(), status=200):
        self._json = json_data
        self.text = text
        self._chunks = list(chunks)
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return self._json

    def iter_content(self, chunk_size):
        for c in self._chunks:
            yield c

    def close(self):
        pass


def release(tag, exe_url="https://dl/exe", sha_url="https://dl/sha", names=None):
    assets = [
        {"name": "MarketRadyo.exe", "browser_download_url": exe_url},
        {"name": "MarketRadyo.exe.sha256", "browser_download_url": sha_url},
    ]
    if names is not None:
        assets = [a for a in assets if a["name"] in names]
    return {"tag_name": tag, "assets": assets}


def fake_get_factory(rel, sha_text, exe_bytes=EXE_BYTES):
    def fake_get(url, **kw):
        if url == updater.API_URL % REPO:
            return FakeResp(json_data=rel)
        if url == "https://dl/sha":
            return FakeResp(text=sha_text)
        if url == "https://dl/exe":
            return FakeResp(chunks=[exe_bytes[:100], exe_bytes[100:]])
        raise AssertionError("beklenmeyen istek: " + url)
    return fake_get


class VersionTest(unittest.TestCase):
    def test_parse_tag_and_plain(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("1.2.3\n"), (1, 2, 3))
        self.assertEqual(parse_version("V2.0"), (2, 0, 0))

    def test_parse_invalid(self):
        self.assertIsNone(parse_version("abc"))
        self.assertIsNone(parse_version(""))
        self.assertIsNone(parse_version(None))

    def test_numeric_not_lexical(self):
        self.assertTrue(is_newer("v1.10.0", "1.9.0"))
        self.assertFalse(is_newer("v1.9.0", "1.10.0"))

    def test_newer_equal_older(self):
        self.assertTrue(is_newer("v1.0.1", "1.0.0"))
        self.assertFalse(is_newer("v1.0.0", "1.0.0"))
        self.assertFalse(is_newer("v0.9.9", "1.0.0"))

    def test_unreadable_is_not_newer(self):
        self.assertFalse(is_newer("garbage", "1.0.0"))
        self.assertFalse(is_newer("v1.0.1", ""))


class Sha256Test(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.exe = self.dir / "MarketRadyo.exe"
        self.exe.write_bytes(b"hello market radyo")
        self.digest = hashlib.sha256(b"hello market radyo").hexdigest()

    def tearDown(self):
        self._tmp.cleanup()

    def test_parse_sha_formats(self):
        self.assertEqual(parse_sha256(self.digest), self.digest)
        self.assertEqual(parse_sha256("%s  MarketRadyo.exe\n" % self.digest.upper()), self.digest)
        self.assertIsNone(parse_sha256("deadbeef  MarketRadyo.exe"))
        self.assertIsNone(parse_sha256(""))

    def test_match_passes(self):
        self.assertTrue(verify_sha256(self.exe, "%s  MarketRadyo.exe" % self.digest))

    def test_wrong_hash_fails(self):
        other = hashlib.sha256(b"baska").hexdigest()
        self.assertFalse(verify_sha256(self.exe, "%s  MarketRadyo.exe" % other))

    def test_tampered_file_fails(self):
        sha = "%s  MarketRadyo.exe" % self.digest
        self.exe.write_bytes(b"hello market radyo!")
        self.assertFalse(verify_sha256(self.exe, sha))

    def test_missing_or_garbage_sha_fails(self):
        self.assertFalse(verify_sha256(self.exe, "not a hash"))
        self.assertFalse(verify_sha256(self.dir / "yok.exe", self.digest))


class UpdateScriptTest(unittest.TestCase):
    def test_script_contents(self):
        script = build_update_script(4242, r"C:\Radyo Dizini\MarketRadyo.exe",
                                     r"C:\Radyo Dizini\MarketRadyo.exe.new",
                                     r"C:\Radyo Dizini\MarketRadyo.exe.old")
        self.assertIn('set "PID=4242"', script)
        self.assertIn('set "EXE=C:\\Radyo Dizini\\MarketRadyo.exe"', script)
        self.assertIn('move /Y "%NEW%" "%EXE%"', script)
        self.assertIn('start "" "%EXE%"', script)
        self.assertIn('del /F /Q "%~f0"', script)
        self.assertIn("goto wait", script)
        self.assertTrue(script.endswith("\r\n"))
        self.assertNotIn("\n\n", script.replace("\r\n", "\n"))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        (self.base / "version.txt").write_text("1.0.0\n", encoding="utf-8")
        self.exe = self.base / "MarketRadyo.exe"
        self.digest = hashlib.sha256(EXE_BYTES).hexdigest()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, cfg=CFG):
        return Updater(self.base, cfg, exe=self.exe)

    def test_disabled_without_repo(self):
        with mock.patch("updater.requests.get") as get:
            res = self.make({"update_repo": "OWNER/market-radyo"}).check()
        self.assertEqual(res.status, "disabled")
        get.assert_not_called()

    def test_uptodate(self):
        rel = release("v1.0.0")
        with mock.patch("updater.requests.get", side_effect=fake_get_factory(rel, self.digest)):
            res = self.make().check()
        self.assertEqual(res.status, "uptodate")

    def test_source_install_only_reports(self):
        rel = release("v1.1.0")
        with mock.patch("updater.is_frozen", return_value=False), \
                mock.patch("updater.requests.get", side_effect=fake_get_factory(rel, self.digest)):
            up = self.make()
            res = up.check()
        self.assertEqual(res.status, "available")
        self.assertEqual(res.version, "1.1.0")
        self.assertIsNone(up.pending_version())
        self.assertFalse((self.base / "MarketRadyo.exe.new").exists())

    def test_frozen_stages_verified_exe(self):
        rel = release("v1.1.0")
        sha_text = "%s  MarketRadyo.exe" % self.digest
        with mock.patch("updater.is_frozen", return_value=True), \
                mock.patch("updater.requests.get", side_effect=fake_get_factory(rel, sha_text)):
            up = self.make()
            res = up.check()
        self.assertEqual(res.status, "staged")
        self.assertEqual(up.pending_version(), "1.1.0")
        staged = self.base / "MarketRadyo.exe.new"
        self.assertEqual(staged.read_bytes(), EXE_BYTES)
        self.assertFalse((self.base / "MarketRadyo.exe.new.part").exists())

    def test_frozen_bad_sha_rejected(self):
        rel = release("v1.1.0")
        bad = "%s  MarketRadyo.exe" % hashlib.sha256(b"baska").hexdigest()
        with mock.patch("updater.is_frozen", return_value=True), \
                mock.patch("updater.requests.get", side_effect=fake_get_factory(rel, bad)):
            up = self.make()
            res = up.check()
        self.assertEqual(res.status, "error")
        self.assertIsNone(up.pending_version())
        self.assertFalse((self.base / "MarketRadyo.exe.new").exists())
        self.assertFalse((self.base / "MarketRadyo.exe.new.part").exists())

    def test_missing_assets_is_error(self):
        rel = release("v1.1.0", names=["MarketRadyo.exe"])
        with mock.patch("updater.is_frozen", return_value=True), \
                mock.patch("updater.requests.get", side_effect=fake_get_factory(rel, self.digest)):
            res = self.make().check()
        self.assertEqual(res.status, "error")

    def test_bad_tag_is_error(self):
        rel = release("surum-bir")
        with mock.patch("updater.requests.get", side_effect=fake_get_factory(rel, self.digest)):
            res = self.make().check()
        self.assertEqual(res.status, "error")

    def test_network_failure_is_error(self):
        with mock.patch("updater.requests.get", side_effect=OSError("ag yok")):
            res = self.make().check()
        self.assertEqual(res.status, "error")

    def test_apply_without_staged_does_nothing(self):
        self.assertFalse(self.make().apply_pending())


class PlaylistMergeTest(unittest.TestCase):
    def test_remote_adds_but_keeps_local_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Library(Path(tmp), {})
            a = {"id": "PLAAAAAAAAAAAAAAAA", "title": "A", "enabled": True}
            self.assertEqual(lib.merge_remote([a]), 1)
            lib.set_enabled(a["id"], False)  # kullanıcı kapattı
            b = {"id": "PLBBBBBBBBBBBBBBBB", "title": "B", "enabled": True}
            remote = [dict(a, enabled=True), b]  # uzakta A hâlâ açık görünüyor
            self.assertEqual(lib.merge_remote(remote), 1)
            self.assertFalse(lib.get(a["id"])["enabled"])
            self.assertTrue(lib.get(b["id"])["enabled"])
            self.assertEqual(lib.merge_remote("bozuk"), 0)


if __name__ == "__main__":
    unittest.main()
