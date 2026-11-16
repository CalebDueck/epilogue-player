#!/usr/bin/env python3
"""Build a hash-keyed index for all GB/GBC/GBA ROMs in the local libraries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from gb_rom_inventory import inspect_rom  # noqa: E402


def read_profiles(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("games", [])


def build(gb_dir: Path, gba_dir: Path, output: Path, gsf_dir: Path) -> dict[str, Any]:
    profiles = read_profiles(ROOT / "profiles" / "catalog.json")
    profiles += read_profiles(ROOT / "profiles" / "gba_music_catalog.json")
    by_hash = {sha: profile for profile in profiles for sha in profile.get("rom_sha256", [])}
    images: dict[str, dict[str, Any]] = {}
    ignored = []
    for folder, console in ((gb_dir, "GB/GBC"), (gba_dir, "GBA")):
        if not folder.is_dir():
            continue
        for rom in sorted(folder.iterdir()):
            if rom.name.startswith("._") or rom.suffix.lower() not in {".gb", ".gbc", ".gba"}:
                continue
            try:
                metadata = inspect_rom(rom)
            except (OSError, ValueError) as exc:
                ignored.append({"file": rom.name, "folder": console, "reason": str(exc)})
                continue
            sha = metadata["sha256"]
            entry = images.setdefault(sha, {
                "sha256": sha,
                "title": metadata.get("title") or rom.stem,
                "console": metadata.get("console_family", console),
                "presentation_mode": metadata.get("presentation_mode"),
                "rom_bytes": metadata.get("file_size_bytes"),
                "aliases": [],
                "header_checksum_valid": metadata.get("header_checksum_valid"),
            })
            entry["aliases"].append(f"{console.lower().replace('/', '-')}/{rom.name}")
            profile = by_hash.get(sha)
            if profile:
                audio_format = profile.get("format", "GBS")
                if profile.get("verification") == "unsupported":
                    playable = False
                    music_status = "unsupported-rip"
                elif audio_format == "GSF":
                    playable = all((gsf_dir / name).is_file()
                                   for name in profile.get("track_files", [])) and all(
                                       (gsf_dir / name).is_file()
                                       for name in profile.get("library_files", []))
                    music_status = "ready" if playable else "missing-local-audio-files"
                elif audio_format == "WAV":
                    playable = all((gsf_dir.parent / name).is_file()
                                   for name in profile.get("track_files", []))
                    music_status = "ready" if playable else "missing-local-audio-files"
                else:
                    playable = True
                    music_status = "ready"
                entry.update({
                    "title": profile.get("title", entry["title"]),
                    "music_status": music_status,
                    "backend": audio_format,
                    "verification": profile.get("verification", "verified"),
                    "track_count": len(profile.get("tracks", [])),
                    "tracks": profile.get("tracks", []),
                    "source": profile.get("source") or profile.get("track_names_source"),
                    "source_url": profile.get("source_url") or profile.get("archive_url"),
                })
            else:
                entry.update({
                    "music_status": "no-compatible-profile",
                    "backend": None,
                    "verification": None,
                    "track_count": 0,
                    "tracks": [],
                    "reason": "No compatible GBS, GSF, or rendered WAV profile is catalogued for this exact ROM SHA-256.",
                })
    result = {
        "schema_version": 1,
        "source_directories": {"gb_gbc": "~/games/gb", "gba": "~/games/gba"},
        "unique_rom_images": len(images),
        "source_rom_files": sum(len(item["aliases"]) for item in images.values()),
        "ignored_files": ignored,
        "games": sorted(images.values(), key=lambda game: (game["console"], game["title"].casefold(), game["sha256"])),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gb-dir", type=Path, default=Path.home() / "games" / "gb")
    parser.add_argument("--gba-dir", type=Path, default=Path.home() / "games" / "gba")
    parser.add_argument("--gsf-dir", type=Path,
                        default=Path.home() / ".local" / "share" / "epilogue-player" / "gsf")
    parser.add_argument("--output", type=Path, default=ROOT / "profiles" / "library.json")
    args = parser.parse_args()
    result = build(args.gb_dir, args.gba_dir, args.output, args.gsf_dir)
    print(f"Indexed {result['source_rom_files']} ROM files as "
          f"{result['unique_rom_images']} unique images; wrote {args.output}")


if __name__ == "__main__":
    main()
