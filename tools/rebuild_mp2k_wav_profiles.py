#!/usr/bin/env python3
"""Re-render local MP2K WAV profiles from the exact ROMs in ~/games/gba."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "profiles" / "gba_music_catalog.json"
DEFAULT_CACHE = Path.home() / ".local" / "share" / "epilogue-player"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rebuild(catalog_path: Path, rom_dir: Path, cache_root: Path,
            gba_audio: str) -> int:
    catalog: dict[str, Any] = json.loads(catalog_path.read_text(encoding="utf-8"))
    roms = [path for path in rom_dir.glob("*.gba") if not path.name.startswith("._")]
    rom_by_hash = {sha256(path): path for path in roms}
    rebuilt = 0

    for profile in catalog.get("games", []):
        if profile.get("format") != "WAV":
            continue
        matches = [rom_by_hash[sha] for sha in profile.get("rom_sha256", [])
                   if sha in rom_by_hash]
        if not matches:
            print(f"skip {profile['title']}: no exact ROM hash in {rom_dir}")
            continue

        slots = []
        for label in profile.get("tracks", []):
            if not label.startswith("Song slot "):
                raise ValueError(f"unsupported MP2K slot label: {label}")
            slots.append(int(label.removeprefix("Song slot ")))
        track_files = profile.get("track_files", [])
        if not slots or len(slots) != len(track_files):
            raise ValueError(f"invalid WAV profile track mapping: {profile['title']}")

        relative_dir = Path(track_files[0]).parent
        if relative_dir.is_absolute() or ".." in relative_dir.parts:
            raise ValueError(f"unsafe WAV cache path for {profile['title']}")
        out_dir = cache_root / relative_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            gba_audio, "wav", str(matches[0]), "--songs",
            ",".join(str(slot) for slot in slots), "--loops", "1", "-o", str(out_dir),
        ], check=True)

        missing = [name for name in track_files if not (cache_root / name).is_file()]
        if missing:
            raise FileNotFoundError(f"{profile['title']}: renderer did not create {missing[0]}")
        profile["track_sha256"] = [sha256(cache_root / name) for name in track_files]
        rebuilt += 1

    catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    return rebuilt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gba-audio", default="gba-audio",
                        help="gba-audio-tools command")
    parser.add_argument("--rom-dir", type=Path, default=Path.home() / "games" / "gba")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    args = parser.parse_args()
    count = rebuild(args.catalog, args.rom_dir, args.cache_root, args.gba_audio)
    print(f"Rebuilt {count} exact-ROM MP2K WAV profile(s).")


if __name__ == "__main__":
    main()
