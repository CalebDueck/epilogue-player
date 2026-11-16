import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import MusicPlayer, Workbench, parse_info, read_gbs_header


def test_rom() -> bytes:
    rom = bytearray(32 * 1024)
    rom[0x134:0x13F] = b"WORKBENCH\0\0"
    rom[0x147] = rom[0x148] = rom[0x149] = 0
    check = 0
    for byte in rom[0x134:0x14D]:
        check = (check - byte - 1) & 0xFF
    rom[0x14D] = check
    return bytes(rom)


def test_gbs() -> bytes:
    data = bytearray(112 + 16)
    data[:3] = b"GBS"
    data[3:6] = bytes((1, 1, 1))
    data[16:26] = b"Test Song\0"
    return bytes(data)


class MusicPlayerTests(unittest.TestCase):
    def test_parse_info(self):
        self.assertEqual(parse_info("Title: PM_CRYSTAL\nROM Size: 2 MB\n"),
                         {"title": "PM_CRYSTAL", "rom_size": "2 MB"})

    def test_gbs_header(self):
        header = read_gbs_header(test_gbs())
        self.assertEqual(header["title"], "Test Song")
        self.assertEqual(header["track_count"], 1)
        self.assertEqual(header["first_track"], 1)

    def test_bad_gbs_header_rejected(self):
        with self.assertRaises(ValueError):
            read_gbs_header(b"not a gbs")

    def test_profile_cache_hash_and_rom_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rom = test_rom()
            gbs = test_gbs()
            cache = root / "cache"
            cache.mkdir()
            (cache / "test.gbs").write_bytes(gbs)
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"games": [{
                "title": "Test Game", "rom_sha256": [hashlib.sha256(rom).hexdigest()],
                "gbs_file": "test.gbs", "gbs_sha256": hashlib.sha256(gbs).hexdigest(),
                "tracks": ["Test Song"],
            }]}))
            player = MusicPlayer(catalog, cache, "gbsplay")
            profile = player.match(hashlib.sha256(rom).hexdigest())
            self.assertIsNotNone(profile)
            path, header = player.prepare(profile)
            self.assertEqual(path.name, "test.gbs")
            self.assertEqual(header["track_count"], 1)
            self.assertIsNone(player.match("0" * 64))

    def test_corrupt_cached_module_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "cache"
            cache.mkdir()
            (cache / "test.gbs").write_bytes(test_gbs())
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"games": []}))
            profile = {"gbs_file": "test.gbs", "gbs_sha256": "0" * 64,
                       "tracks": ["Test Song"]}
            with self.assertRaises(ValueError):
                MusicPlayer(catalog, cache, "gbsplay").prepare(profile)


if __name__ == "__main__":
    unittest.main()
