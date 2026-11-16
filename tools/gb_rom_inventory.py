#!/usr/bin/env python3
"""Inventory local Game Boy ROM dumps without modifying or copying them.

This reports identity/header facts and can optionally attach gbtoolsid's audio
engine fingerprint output. It does not infer track lists or prove playback.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROM_EXTENSIONS = {".gb", ".gbc"}
CARTRIDGE_TYPES = {
    0x00: "ROM ONLY", 0x01: "MBC1", 0x02: "MBC1+RAM", 0x03: "MBC1+RAM+BATTERY",
    0x05: "MBC2", 0x06: "MBC2+BATTERY", 0x08: "ROM+RAM", 0x09: "ROM+RAM+BATTERY",
    0x0B: "MMM01", 0x0C: "MMM01+RAM", 0x0D: "MMM01+RAM+BATTERY", 0x0F: "MBC3+TIMER+BATTERY",
    0x10: "MBC3+TIMER+RAM+BATTERY", 0x11: "MBC3", 0x12: "MBC3+RAM", 0x13: "MBC3+RAM+BATTERY",
    0x19: "MBC5", 0x1A: "MBC5+RAM", 0x1B: "MBC5+RAM+BATTERY", 0x1C: "MBC5+RUMBLE",
    0x1D: "MBC5+RUMBLE+RAM", 0x1E: "MBC5+RUMBLE+RAM+BATTERY", 0x20: "MBC6",
    0x22: "MBC7+SENSOR+RUMBLE+RAM+BATTERY", 0xFC: "POCKET CAMERA", 0xFD: "BANDAI TAMA5",
    0xFE: "HUC3", 0xFF: "HUC1+RAM+BATTERY",
}
ROM_SIZE_BYTES = {code: 32 * 1024 * (1 << code) for code in range(9)}
ROM_SIZE_BYTES.update({0x52: 72 * 16 * 1024, 0x53: 80 * 16 * 1024, 0x54: 96 * 16 * 1024})
RAM_SIZE_BYTES = {0x00: 0, 0x01: 2 * 1024, 0x02: 8 * 1024, 0x03: 32 * 1024,
                  0x04: 128 * 1024, 0x05: 64 * 1024}


def decode_title(rom: bytes) -> str:
    # CGB headers shorten the title field to make room for the manufacturer ID.
    end = 0x13F if rom[0x143] & 0x80 else 0x144
    raw = rom[0x134:end].split(b"\0", 1)[0]
    return raw.decode("ascii", errors="replace").strip().rstrip("\ufffd")


def header_checksum(rom: bytes) -> int:
    value = 0
    for byte in rom[0x134:0x14D]:
        value = (value - byte - 1) & 0xFF
    return value


def inspect_rom(path: Path, gbtoolsid: str | None = None) -> dict[str, Any]:
    rom = path.read_bytes()
    if len(rom) < 0x150:
        raise ValueError("file is shorter than the 0x150-byte cartridge header")

    size_code = rom[0x148]
    ram_code = rom[0x149]
    expected_header = rom[0x14D]
    record: dict[str, Any] = {
        "file": str(path),
        "title": decode_title(rom),
        "sha1": hashlib.sha1(rom).hexdigest(),
        "sha256": hashlib.sha256(rom).hexdigest(),
        "file_size_bytes": len(rom),
        "cgb_flag": f"0x{rom[0x143]:02X}",
        "cartridge_type_code": f"0x{rom[0x147]:02X}",
        "cartridge_type": CARTRIDGE_TYPES.get(rom[0x147], "UNKNOWN"),
        "rom_size_code": f"0x{size_code:02X}",
        "rom_size_bytes_from_header": ROM_SIZE_BYTES.get(size_code),
        "ram_size_code": f"0x{ram_code:02X}",
        "ram_size_bytes_from_header": RAM_SIZE_BYTES.get(ram_code),
        "header_checksum_expected": f"0x{expected_header:02X}",
        "header_checksum_actual": f"0x{header_checksum(rom):02X}",
        "header_checksum_valid": header_checksum(rom) == expected_header,
        "licensee_old_code": f"0x{rom[0x14B]:02X}",
        "licensee_new_code": rom[0x144:0x146].decode("ascii", errors="replace"),
    }
    if gbtoolsid:
        result = subprocess.run([gbtoolsid, "-oj", str(path)], check=False,
                                capture_output=True, text=True)
        record["gbtoolsid_exit_code"] = result.returncode
        record["gbtoolsid_output"] = (result.stdout + result.stderr).strip()
    return record


def find_roms(inputs: list[Path]) -> list[Path]:
    found: set[Path] = set()
    for source in inputs:
        if source.is_file() and source.suffix.lower() in ROM_EXTENSIONS:
            if not source.name.startswith("._"):
                found.add(source)
        elif source.is_dir():
            found.update(path for path in source.rglob("*")
                         if path.is_file() and not path.name.startswith("._")
                         and path.suffix.lower() in ROM_EXTENSIONS)
    return sorted(found, key=lambda path: str(path).casefold())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="ROM file(s) or directories to scan")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", type=Path, help="write report here; stdout by default")
    parser.add_argument("--gbtoolsid", help="optional path to a gbtoolsid executable")
    args = parser.parse_args()

    paths = find_roms(args.paths)
    if not paths:
        parser.error("no .gb or .gbc files found")
    records = []
    for path in paths:
        try:
            records.append(inspect_rom(path, args.gbtoolsid))
        except (OSError, ValueError) as error:
            records.append({"file": str(path), "error": str(error)})

    if args.format == "json":
        rendered = json.dumps(records, indent=2, ensure_ascii=True) + "\n"
    else:
        columns = list(dict.fromkeys(key for record in records for key in record))
        import io
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        rendered = buffer.getvalue()
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
