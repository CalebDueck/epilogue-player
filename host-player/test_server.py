import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import MusicPlayer, Workbench, parse_info, read_gbs_header
from gb_rom_inventory import inspect_rom


def test_rom() -> bytes:
    rom = bytearray(32 * 1024)
    rom[0x134:0x13F] = b"WORKBENCH\0\0"
    rom[0x147] = rom[0x148] = rom[0x149] = 0
    check = 0
    for byte in rom[0x134:0x14D]:
        check = (check - byte - 1) & 0xFF
    rom[0x14D] = check
    return bytes(rom)


def test_gbs(track_count: int = 1) -> bytes:
    data = bytearray(112 + 16)
    data[:3] = b"GBS"
    data[3:6] = bytes((1, track_count, 1))
    data[16:26] = b"Test Song\0"
    return bytes(data)


def test_gba_rom() -> bytes:
    data = bytearray(0xC0)
    data[0xA0:0xAC] = b"WARIO LAND 4"
    data[0xAC:0xB0] = b"AW4E"
    data[0xB0:0xB2] = b"01"
    data[0xB2] = 0x96
    data[0xBC] = 1
    data[0xBD] = (-0x19 - sum(data[0xA0:0xBD])) & 0xFF
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

    def test_gba_header_selects_16_bit_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Wario Land 4.gba"
            path.write_bytes(test_gba_rom())
            info = inspect_rom(path)
            self.assertEqual(info["title"], "WARIO LAND 4")
            self.assertEqual(info["game_code"], "AW4E")
            self.assertEqual(info["presentation_mode"], "gba16")
            self.assertTrue(info["header_checksum_valid"])

    def test_gbc_flag_selects_color_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            rom = bytearray(test_rom())
            rom[0x143] = 0x80
            check = 0
            for byte in rom[0x134:0x14D]:
                check = (check - byte - 1) & 0xFF
            rom[0x14D] = check
            path = Path(temp) / "COLOR.gbc"
            path.write_bytes(rom)
            info = inspect_rom(path)
            self.assertEqual(info["presentation_mode"], "gbc-color")

    def test_gba_reader_dumps_once_and_reports_missing_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gba_dir = root / "gba"
            gba_dir.mkdir()
            (gba_dir / "Wario Land 4.gba").write_bytes(test_gba_rom())
            catalog = root / "catalog.json"
            catalog.write_text('{"games": []}')

            class FakeGBAWorkbench(Workbench):
                def __init__(self):
                    self.commands = []
                    super().__init__("throwback", root / "dumps", catalog,
                                     root / "cache", "gbsplay", gba_dir)

                def _run(self, args, timeout=90):
                    self.commands.append(args)
                    if args and args[0] == "dump-rom":
                        Path(args[1]).write_bytes(test_gba_rom())
                        return subprocess.CompletedProcess(args, 0, "", "")
                    return subprocess.CompletedProcess(args, 0,
                        "Type: GBA\nTitle: WARIOLANDE\nGame Code: AWAE\n"
                        "Header Checksum: 0xE7 (valid)\nVersion: 1\n", "")

            app = FakeGBAWorkbench()
            app.scan_once()
            state = app.snapshot()
            self.assertEqual(state["game"]["analysis"]["presentation_mode"], "gba16")
            self.assertEqual(state["player"]["status"], "UNAVAILABLE")
            self.assertIn("No compatible GSF music profile", state["player"]["message"])
            self.assertEqual(app.commands[0], ["info"])
            self.assertEqual(app.commands[1][0], "dump-rom")
            self.assertTrue(state["game"]["analysis"]["sha256"])
            preview = app.gba_preview()
            self.assertTrue(preview["available"])
            self.assertEqual(preview["game"]["game_code"], "AW4E")

    def test_donkey_kong_land_profile_is_exact_and_has_20_subsongs(self):
        root = Path(__file__).resolve().parent.parent
        catalog = root / "profiles" / "catalog.json"
        with tempfile.TemporaryDirectory() as temp:
            player = MusicPlayer(catalog, Path(temp), "gbsplay")
            profile = player.match(
                "eaf831ddd75e7cbc9990e478a139ac162034c03377670fdb79615a9aa2cc570d")
            self.assertIsNotNone(profile)
            self.assertEqual(profile["title"], "Donkey Kong Land")
            self.assertEqual(profile["verification"], "candidate")
            self.assertEqual(profile["gbs_sha256"],
                             "fa0b927cff13667f8719fc87b698fb669452a7a8698ab0f50a465d568c2fe9ad")
            self.assertEqual(len(profile["tracks"]), 20)
            self.assertEqual(profile["tracks"][0], "Track 01")
            self.assertEqual(profile["tracks"][-1], "Track 20")
            profile = dict(profile)
            fixture = test_gbs(track_count=20)
            profile["gbs_sha256"] = hashlib.sha256(fixture).hexdigest()
            (Path(temp) / profile["gbs_file"]).write_bytes(fixture)
            player.load_profile(profile)
            state = player.snapshot()
            self.assertEqual(state["status"], "CANDIDATE")
            self.assertFalse(state["supported"])
            self.assertTrue(state["playable"])
            self.assertEqual(state["track_count"], 20)
            self.assertIn("source ROM revision is unverified", state["message"])


if __name__ == "__main__":
    unittest.main()
