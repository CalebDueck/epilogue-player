#!/usr/bin/env python3
"""Local GB Operator cartridge music player (GBS profile prototype)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from gb_rom_inventory import inspect_rom  # noqa: E402

DEFAULT_PROFILE = ROOT / "profiles" / "catalog.json"


def parse_info(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            info[key.strip().lower().replace(" ", "_")] = value.strip()
    return info


def read_gbs_header(data: bytes) -> dict[str, Any]:
    if len(data) < 112 or data[:3] != b"GBS":
        raise ValueError("not a valid GBS module (expected GBS header)")

    def field(start: int) -> str:
        return data[start:start + 32].split(b"\0", 1)[0].decode("latin-1").strip()

    count = data[4]
    first = data[5]
    if count == 0 or first == 0 or first + count > 256:
        raise ValueError("invalid GBS subsong range")
    return {
        "title": field(16), "author": field(48), "copyright": field(80),
        "track_count": count, "first_track": first,
    }


class MusicPlayer:
    def __init__(self, catalog: Path, cache_dir: Path, gbsplay: str,
                 gsf_dir: Path | None = None, gsfplay: str = "playgsf",
                 gba_catalog: Path | None = None) -> None:
        self.catalog_path = catalog
        self.gba_catalog_path = gba_catalog or ROOT / "profiles" / "gba_music_catalog.json"
        self.cache_dir = cache_dir
        self.gbsplay = gbsplay
        self.gsfplay = gsfplay
        self.gsf_dir = gsf_dir or (Path.home() / ".local" / "share" /
                                  "epilogue-player" / "gsf")
        self.lock = threading.RLock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.paused = False
        self.profile: dict[str, Any] | None = None
        self.gbs_file: Path | None = None
        self.track_index = 0
        self.first_track = 1
        self.shuffle = False
        self.repeat = False
        self.history: list[int] = []
        self.status = "STOPPED"
        self.message = "Waiting for a supported cartridge."

    def profiles(self) -> list[dict[str, Any]]:
        profiles = json.loads(self.catalog_path.read_text(encoding="utf-8"))["games"]
        if self.gba_catalog_path.is_file():
            profiles += json.loads(self.gba_catalog_path.read_text(encoding="utf-8"))["games"]
        return profiles

    def match(self, sha256: str) -> dict[str, Any] | None:
        return next((game for game in self.profiles()
                     if sha256 in game.get("rom_sha256", [])), None)

    def prepare(self, profile: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        if profile.get("format") == "GSF":
            track_files = profile.get("track_files", [])
            if len(track_files) != len(profile.get("tracks", [])):
                raise ValueError("GSF track files do not match the track list")
            missing = [name for name in track_files if not (self.gsf_dir / name).is_file()]
            if missing:
                raise ValueError(f"GSF library is missing {len(missing)} track file(s)")
            library_files = profile.get("library_files", [])
            missing_libraries = [name for name in library_files
                                 if not (self.gsf_dir / name).is_file()]
            if missing_libraries:
                raise ValueError("GSF library file is missing for this profile")
            track_hashes = profile.get("track_sha256", [])
            if track_hashes and len(track_hashes) != len(track_files):
                raise ValueError("GSF track checksums do not match the track list")
            for name, expected in zip(track_files, track_hashes):
                actual = hashlib.sha256((self.gsf_dir / name).read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError(f"cached GSF track failed its SHA-256 check: {Path(name).name}")
            library_hashes = profile.get("library_sha256", [])
            if library_hashes and len(library_hashes) != len(library_files):
                raise ValueError("GSF library checksums do not match the profile")
            for name, expected in zip(library_files, library_hashes):
                actual = hashlib.sha256((self.gsf_dir / name).read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError(f"cached GSF library failed its SHA-256 check: {Path(name).name}")
            track_path = self.gsf_dir / track_files[0]
            if not track_path.is_file():
                raise ValueError(f"GSF track is missing from the local library: {track_path.name}")
            return track_path, {"track_count": len(track_files), "first_track": 1}
        if profile.get("format") == "WAV":
            track_files = profile.get("track_files", [])
            if not track_files or len(track_files) != len(profile.get("tracks", [])):
                raise ValueError("WAV track files do not match the track list")
            missing = [name for name in track_files if not (self.gsf_dir.parent / name).is_file()]
            if missing:
                raise ValueError(f"WAV cache is missing {len(missing)} track file(s)")
            track_hashes = profile.get("track_sha256", [])
            if len(track_hashes) != len(track_files):
                raise ValueError("WAV track checksums do not match the track list")
            for name, expected in zip(track_files, track_hashes):
                actual = hashlib.sha256((self.gsf_dir.parent / name).read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError(f"cached WAV track failed its SHA-256 check: {Path(name).name}")
            return self.gsf_dir.parent / track_files[0], {
                "track_count": len(track_files), "first_track": 0,
            }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / profile["gbs_file"]
        metadata: dict[str, Any]
        if target.is_file():
            raw = target.read_bytes()
            metadata = read_gbs_header(raw)
            expected = profile.get("gbs_sha256")
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("cached GBS file failed its SHA-256 check")
        else:
            request = urllib.request.Request(profile["archive_url"], headers={
                "User-Agent": "EpilogueGBPlayer/0.1 (local cartridge playback prototype)"})
            with urllib.request.urlopen(request, timeout=30) as response:
                archive = response.read(8 * 1024 * 1024 + 1)
            if len(archive) > 8 * 1024 * 1024:
                raise ValueError("music profile archive exceeded the 8 MiB safety limit")
            if profile.get("archive_format") == "7z":
                archive_path = target.with_suffix(target.suffix + ".archive.tmp")
                archive_path.write_bytes(archive)
                try:
                    extracted = subprocess.run(
                        ["7z", "x", "-so", str(archive_path), profile["archive_member"]],
                        capture_output=True, check=False, timeout=30)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ValueError(f"could not extract the GBS archive: {exc}") from exc
                finally:
                    archive_path.unlink(missing_ok=True)
                if extracted.returncode != 0:
                    raise ValueError("could not extract the GBS module from its 7z archive")
                raw = extracted.stdout
            else:
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    raw = bundle.read(profile["archive_member"])
            metadata = read_gbs_header(raw)
            expected = profile.get("gbs_sha256")
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("downloaded GBS file failed its SHA-256 check")
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_bytes(raw)
            temp.replace(target)
        if metadata["track_count"] != len(profile["tracks"]):
            raise ValueError("profile track names do not match the GBS subsong count")
        return target, metadata

    def load_profile(self, profile: dict[str, Any]) -> None:
        with self.lock:
            self._stop_locked()
            if profile.get("verification") == "unsupported":
                self.profile = None
                self.gbs_file = None
                self.status = "UNAVAILABLE"
                self.message = profile.get(
                    "unsupported_reason",
                    "This music rip is known to be silent and cannot be played.")
                return
            try:
                gbs_file, metadata = self.prepare(profile)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                self.profile = None
                self.gbs_file = None
                self.status = "UNAVAILABLE"
                self.message = f"Could not prepare the music profile: {exc}"
                return
            self.profile = profile
            self.gbs_file = gbs_file
            self.track_index = 0
            self.first_track = metadata["first_track"]
            self.history.clear()
            if profile.get("verification") == "candidate":
                self.status = "CANDIDATE"
                self.message = (f"{metadata['track_count']} tracks loaded from a candidate profile. "
                                "The source ROM revision is unverified; press PLAY to audition.")
            else:
                self.status = "READY"
                self.message = f"{metadata['track_count']} tracks ready. Press PLAY."

    def _stop_locked(self) -> None:
        if self.proc is not None:
            try:
                if self.paused:
                    os.kill(self.proc.pid, signal.SIGCONT)
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            self.proc = None
        self.paused = False

    def play(self, index: int | None = None) -> None:
        with self.lock:
            if self.profile is None or self.gbs_file is None:
                self.status = "UNAVAILABLE"
                self.message = "No supported music playback profile is loaded."
                return
            if index is not None:
                self.track_index = index % len(self.profile["tracks"])
            self._stop_locked()
            subsong = self.first_track + self.track_index
            audio_format = self.profile.get("format", "GBS")
            if audio_format == "GSF":
                player_name = self.gsfplay
            elif audio_format == "WAV":
                player_name = "pw-play"
            else:
                player_name = self.gbsplay
            try:
                if audio_format == "GSF":
                    track_file = self.gsf_dir / self.profile["track_files"][self.track_index]
                    command = [self.gsfplay, "-t", "150000", str(track_file)]
                elif audio_format == "WAV":
                    track_file = self.gsf_dir.parent / self.profile["track_files"][self.track_index]
                    command = ["pw-play", str(track_file)]
                else:
                    command = [self.gbsplay, "-q", "-o", "pulse", "-L", "-t", "180",
                               "--", str(self.gbs_file), str(subsong)]
                self.proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self.status = "ERROR"
                self.message = f"Could not start {player_name}: {exc}"
                self.proc = None
                return
            time.sleep(0.15)
            if self.proc.poll() is not None:
                self.status = "ERROR"
                self.message = f"{player_name} could not open the selected track or audio output."
                self.proc = None
                return
            self.status = "PLAYING"
            if self.profile.get("verification") == "candidate":
                self.message = "Playing candidate track through the computer's default audio output."
            else:
                self.message = "Playing through the computer's default audio output."

    def toggle(self) -> None:
        with self.lock:
            playing = self.proc is not None and self.proc.poll() is None and not self.paused
        self.pause() if playing else self.resume()

    def pause(self) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None or self.paused:
                return
            try:
                os.kill(self.proc.pid, signal.SIGSTOP)
            except OSError as exc:
                self.status = "ERROR"
                self.message = f"Could not pause playback: {exc}"
                return
            self.paused = True
            self.status = "PAUSED"
            self.message = "Playback paused."

    def resume(self) -> None:
        with self.lock:
            if self.profile is None or self.gbs_file is None:
                return
            if self.proc is None or self.proc.poll() is not None:
                self.play()
            elif self.paused:
                try:
                    os.kill(self.proc.pid, signal.SIGCONT)
                except OSError as exc:
                    self.status = "ERROR"
                    self.message = f"Could not resume playback: {exc}"
                    return
                self.paused = False
                self.status = "PLAYING"
                self.message = "Playback resumed."

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()
            self.status = "READY" if self.profile else "STOPPED"
            self.message = "Playback stopped."

    def unload(self) -> None:
        with self.lock:
            self._stop_locked()
            self.profile = None
            self.gbs_file = None
            self.status = "STOPPED"
            self.message = "Waiting for a supported cartridge."

    def unsupported(self, message: str) -> None:
        with self.lock:
            self._stop_locked()
            self.profile = None
            self.gbs_file = None
            self.status = "UNAVAILABLE"
            self.message = message

    def step(self, amount: int) -> None:
        with self.lock:
            if self.profile is None:
                return
            count = len(self.profile["tracks"])
            if amount < 0 and self.shuffle and self.history:
                next_index = self.history.pop()
            elif amount > 0 and self.shuffle and count > 1:
                import random
                choices = [i for i in range(count) if i != self.track_index]
                next_index = random.choice(choices)
            else:
                next_index = (self.track_index + amount) % count
            if amount > 0 and next_index != self.track_index:
                self.history.append(self.track_index)
        self.play(next_index)

    def set_mode(self, mode: str, enabled: bool | None = None) -> None:
        with self.lock:
            if mode == "shuffle":
                self.shuffle = not self.shuffle if enabled is None else bool(enabled)
            elif mode == "repeat":
                self.repeat = not self.repeat if enabled is None else bool(enabled)

    def _advance_finished_locked(self) -> None:
        if not self.proc or self.paused or self.proc.poll() is None:
            return
        self.proc = None
        count = len(self.profile.get("tracks", [])) if self.profile else 0
        if count and (self.shuffle or self.track_index + 1 < count or self.repeat):
            if self.shuffle and count > 1:
                import random
                choices = [i for i in range(count) if i != self.track_index]
                next_index = random.choice(choices)
            else:
                next_index = (self.track_index + 1) % count
            self.history.append(self.track_index)
            self.play(next_index)
        else:
            self.status = "READY" if self.profile else "STOPPED"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._advance_finished_locked()
            tracks = self.profile.get("tracks", []) if self.profile else []
            return {
                "status": self.status, "message": self.message,
                "game": self.profile.get("title") if self.profile else None,
                "supported": bool(self.profile and
                                   self.profile.get("verification") != "candidate"),
                "playable": self.profile is not None,
                "track_index": self.track_index,
                "track_count": len(tracks), "tracks": tracks,
                "current_track": tracks[self.track_index] if tracks else None,
                "shuffle": self.shuffle, "repeat": self.repeat,
                "format": self.profile.get("format", "GBS") if self.profile else None,
            }


class Workbench:
    def __init__(self, throwback: str, output_dir: Path, catalog: Path,
                 cache_dir: Path, gbsplay: str, gba_library_dir: Path,
                 gb_library_dir: Path | None = None, gsf_library_dir: Path | None = None,
                 gsfplay: str = "playgsf", gba_catalog: Path | None = None) -> None:
        self.throwback = throwback
        self.output_dir = output_dir
        self.gba_library_dir = gba_library_dir
        self.gb_library_dir = gb_library_dir or (Path.home() / "games" / "gb")
        self.music = MusicPlayer(catalog, cache_dir, gbsplay, gsf_library_dir,
                                 gsfplay, gba_catalog)
        self.lock = threading.RLock()
        self.force_scan = threading.Event()
        self.active_key: str | None = None
        self.dumped_key: str | None = None
        self.state: dict[str, Any] = {
            "device": "CHECKING", "cartridge": "WAITING",
            "message": "Looking for the GB Operator.", "game": None,
            "recent_dumps": [], "dump_dir": str(output_dir), "updated": None,
        }
        self._refresh_dumps()

    def _run(self, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.throwback, *args], capture_output=True,
                              text=True, timeout=timeout, check=False)

    def _find_existing_dump(self, info: dict[str, str], suffix: str) -> Path | None:
        expected_title = info.get("title", "").strip().casefold()
        try:
            expected_version = int(info.get("version", ""), 0)
            expected_checksum = int(info.get("header_checksum", "").split()[0], 0)
        except (ValueError, IndexError):
            return None
        for candidate in self.output_dir.glob(f"*{suffix}"):
            if candidate.name.startswith("._") or not candidate.is_file():
                continue
            try:
                analysis = inspect_rom(candidate)
            except (OSError, ValueError):
                continue
            if (analysis.get("title", "").strip().casefold() == expected_title
                    and analysis.get("version") == expected_version
                    and int(analysis.get("header_checksum_expected", "0"), 0)
                    == expected_checksum):
                return candidate
        return None

    def _set(self, **values: Any) -> None:
        with self.lock:
            self.state.update(values)
            self.state["updated"] = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = json.loads(json.dumps(self.state))
        result["player"] = self.music.snapshot()
        return result

    def theme_preview(self, mode: str) -> dict[str, Any]:
        if mode == "gba16":
            library = self.gba_library_dir
            extensions = {".gba"}
            preferred_names = ("Wario Land 4 (USA, Europe).gba",)
        elif mode == "gbc-color":
            library = self.gb_library_dir
            extensions = {".gb", ".gbc"}
            preferred_names = ("Pokemon - Crystal Version (USA, Europe).gb",
                               "shantae.gbc")
        elif mode == "gb-mono":
            library = self.gb_library_dir
            extensions = {".gb", ".gbc"}
            preferred_names = ("Super Mario Land (World) (Rev A).gb",)
        else:
            return {"available": False, "message": "Unknown presentation mode."}
        preferred = next((library / name for name in preferred_names
                          if (library / name).is_file()), None)
        candidates = (preferred,) if preferred else tuple(
            p for p in sorted(library.iterdir())
            if p.suffix.lower() in extensions and not p.name.startswith("._")) if library.is_dir() else ()
        rom_path = None
        meta = None
        for candidate in candidates:
            try:
                inspected = inspect_rom(candidate)
            except (OSError, ValueError):
                continue
            if inspected.get("presentation_mode") == mode:
                rom_path, meta = candidate, inspected
                break
        if rom_path is None:
            return {"available": False, "message": f"No local {mode} ROM is available for a theme preview."}
        return {"available": True, "game": {
            key: meta[key] for key in ("title", "game_code", "maker_code", "region_code",
                                       "header_checksum_valid", "sha256", "file_size_bytes",
                                       "console_family", "presentation_mode")
            if key in meta
        }, "file_name": rom_path.name}

    def gba_preview(self) -> dict[str, Any]:
        return self.theme_preview("gba16")

    def scan_once(self, force_dump: bool = False) -> None:
        try:
            result = self._run(["info"], timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.active_key = self.dumped_key = None
            self._set(device="ERROR", cartridge="UNKNOWN", message=str(exc), game=None)
            self.music.stop()
            return
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).strip()
            no_cart = "no cartridge" in diagnostic.lower()
            self.active_key = self.dumped_key = None
            self._set(device="CONNECTED" if no_cart else "NOT FOUND",
                      cartridge="NO CARTRIDGE" if no_cart else "UNKNOWN",
                      message="Insert a GB/GBC cartridge." if no_cart else diagnostic,
                      game=None)
            self.music.unload()
            return

        info = parse_info(result.stdout)
        cart_type = info.get("type", "").upper()
        title = info.get("title", "UNKNOWN GAME")
        if cart_type == "GBA":
            key = "|".join(info.get(k, "") for k in
                           ("type", "game_code", "title", "version", "header_checksum"))
            if not force_dump and key == self.active_key and self.dumped_key == key:
                return
            self.active_key = key
            if key != self.dumped_key:
                self.music.unload()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            target = self.output_dir / f"cart-{hashlib.sha256(key.encode()).hexdigest()[:12]}.gba"
            if not target.is_file() and not force_dump:
                target = self._find_existing_dump(info, ".gba") or target
            if not target.is_file() or force_dump:
                self._set(device="CONNECTED", cartridge="DUMPING",
                          message=f"Reading GBA cartridge ROM to {target.name}.")
                try:
                    dumped = self._run(["dump-rom", str(target)], timeout=240)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self._set(cartridge="ERROR", message=f"GBA ROM dump failed: {exc}")
                    return
                if dumped.returncode != 0 or not target.is_file():
                    diagnostic = (dumped.stderr or dumped.stdout).strip()
                    self._set(cartridge="ERROR",
                              message=f"GBA ROM read failed: {diagnostic or 'no ROM file'}")
                    return
            try:
                analysis = inspect_rom(target)
            except (OSError, ValueError) as exc:
                self._set(cartridge="ERROR", message=f"GBA dump validation failed: {exc}")
                return
            self.dumped_key = key
            profile = self.music.match(analysis["sha256"])
            if profile:
                self.music.load_profile(profile)
                player_text = self.music.snapshot()["message"]
            else:
                self.music.unsupported("No compatible GSF music profile is installed for this GBA ROM.")
                player_text = "This GBA ROM has no compatible music rip in the local catalogue."
            player_state = self.music.snapshot()
            self._refresh_dumps()
            self._set(device="CONNECTED", cartridge="DUMPED",
                      message="GBA ROM read and validated locally.",
                      game={"title": analysis.get("title") or title, "operator_info": info,
                            "analysis": analysis,
                            "supported": player_state["supported"],
                            "playable": player_state["playable"]},
                      playback_message=player_text)
            return
        if cart_type not in ("GB", "GBC"):
            self.active_key = self.dumped_key = None
            self.music.unsupported("This cartridge type has no playback backend in this app.")
            self._set(device="CONNECTED", cartridge="UNSUPPORTED TYPE",
                      message=f"Detected {cart_type or 'unknown'} cartridge.",
                      game={"title": title, "operator_info": info, "analysis": None})
            return
        key = "|".join(info.get(k, "") for k in
                       # Header checksum is diagnostic output, not cartridge identity.
                       # Some Operator/CLI versions vary its formatting between scans,
                       # which would otherwise unload the profile and trigger a fresh dump.
                       ("game_id", "title", "mbc", "rom_size", "version"))
        is_new = key != self.active_key
        if not force_dump and not is_new and self.dumped_key == key:
            with self.lock:
                cached_game = self.state.get("game")
                cached_game = dict(cached_game) if cached_game else None
            analysis = cached_game.get("analysis") if cached_game else None
            if analysis and not self.music.snapshot()["playable"]:
                profile = self.music.match(analysis.get("sha256", ""))
                if profile:
                    self.music.load_profile(profile)
                    player_text = self.music.snapshot()["message"]
                    cached_game["supported"] = profile.get("verification") != "candidate"
                    cached_game["playable"] = True
                    self._set(game=cached_game, cartridge="DUMPED",
                              message="ROM read and validated locally.",
                              playback_message=player_text)
            return
        self.active_key = key
        if is_new:
            self.music.unload()
        self._set(device="CONNECTED", cartridge="DETECTED",
                  message="Cartridge detected. Reading ROM for a local playback profile.",
                  game={"title": title, "operator_info": info, "analysis": None})
        self.output_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".gbc" if cart_type == "GBC" else ".gb"
        target = self.output_dir / f"cart-{hashlib.sha256(key.encode()).hexdigest()[:12]}{suffix}"
        if not target.is_file() and not force_dump:
            target = self._find_existing_dump(info, suffix) or target
        reuse_dump = target.is_file() and not force_dump
        if reuse_dump:
            self._set(cartridge="DUMPED", message=f"Using existing local ROM dump {target.name}.")
        else:
            self._set(cartridge="DUMPING", message=f"Reading cartridge ROM to {target.name}.")
            try:
                dumped = self._run(["dump-rom", str(target)], timeout=180)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._set(cartridge="ERROR", message=f"ROM dump failed: {exc}")
                return
            if dumped.returncode != 0 or not target.is_file():
                diagnostic = (dumped.stderr or dumped.stdout).strip()
                self._set(cartridge="ERROR", message=f"ROM read failed: {diagnostic or 'no ROM file'}")
                return
        try:
            analysis = inspect_rom(target)
        except (OSError, ValueError) as exc:
            self._set(cartridge="ERROR", message=f"Dump saved but validation failed: {exc}")
            return
        self.dumped_key = key
        profile = self.music.match(analysis["sha256"])
        if profile:
            self.music.load_profile(profile)
            player_text = self.music.snapshot()["message"]
        else:
            self.music.unsupported("No verified music profile is installed for this ROM.")
            player_text = "This ROM has no installed playback profile yet."
        player_state = self.music.snapshot()
        self._refresh_dumps()
        self._set(cartridge="DUMPED", message="ROM read and validated locally.",
                  game={"title": analysis["title"], "operator_info": info,
                        "analysis": analysis,
                        "supported": player_state["supported"],
                        "playable": player_state["playable"]},
                  playback_message=player_text)

    def _refresh_dumps(self) -> None:
        files = []
        if self.output_dir.exists():
            for path in sorted(self.output_dir.glob("*"), key=lambda p: p.stat().st_mtime,
                               reverse=True):
                if path.suffix.lower() not in (".gb", ".gbc", ".gba") or path.name.startswith("._"):
                    continue
                try:
                    stat = path.stat()
                    files.append({"name": path.name, "bytes": stat.st_size,
                                  "modified": datetime.fromtimestamp(stat.st_mtime).astimezone()
                                  .isoformat(timespec="seconds")})
                except OSError:
                    continue
                if len(files) == 20:
                    break
        self._set(recent_dumps=files)

    def run(self) -> None:
        while True:
            self.force_scan.wait(2.0)
            self.force_scan.clear()
            self.scan_once(force_dump=False)


class Handler(BaseHTTPRequestHandler):
    workbench: Workbench
    web_root: Path

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/status":
            self._json(200, self.workbench.snapshot())
        elif route == "/api/gba-preview":
            self._json(200, self.workbench.gba_preview())
        elif route == "/api/theme-preview":
            mode = urlsplit(self.path).query
            query = dict(item.split("=", 1) if "=" in item else (item, "")
                         for item in mode.split("&") if item)
            self._json(200, self.workbench.theme_preview(query.get("mode", "")))
        elif route in ("/", "/index.html"):
            raw = (self.web_root / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        routes = {
            "/api/scan": lambda: self.workbench.force_scan.set(),
            "/api/play": lambda: self.workbench.music.toggle(),
            "/api/pause": lambda: self.workbench.music.pause(),
            "/api/resume": lambda: self.workbench.music.resume(),
            "/api/stop": lambda: self.workbench.music.stop(),
            "/api/next": lambda: self.workbench.music.step(1),
            "/api/previous": lambda: self.workbench.music.step(-1),
            "/api/shuffle": lambda: self.workbench.music.set_mode("shuffle"),
            "/api/repeat": lambda: self.workbench.music.set_mode("repeat"),
        }
        action = routes.get(self.path)
        if action is None and self.path.startswith("/api/track/"):
            try:
                index = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                self.send_error(400)
                return
            action = lambda: self.workbench.music.play(index)
        if action is None:
            self.send_error(404)
            return
        action()
        self._json(200, self.workbench.snapshot())

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--throwback", default=os.environ.get("THROWBACK", "throwback"))
    parser.add_argument("--gbsplay", default=os.environ.get("GBSPLAY", "gbsplay"))
    parser.add_argument("--gsfplay", default=os.environ.get("GSFPLAY", "playgsf"))
    parser.add_argument("--catalog", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / "games" / "gb" / "operator-dumps")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path.home() / ".local" / "share" / "epilogue-player" / "gbs")
    parser.add_argument("--gba-library-dir", type=Path,
                        default=Path.home() / "games" / "gba",
                        help="local GBA ROM folder used only for the labeled theme preview")
    parser.add_argument("--gb-library-dir", type=Path,
                        default=Path.home() / "games" / "gb",
                        help="local GB/GBC ROM folder used only for labeled theme previews")
    parser.add_argument("--gsf-library-dir", type=Path,
                        default=Path.home() / ".local" / "share" / "epilogue-player" / "gsf",
                        help="local GSF/miniGSF track library")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    app = Workbench(args.throwback, args.output_dir, args.catalog, args.cache_dir,
                    args.gbsplay, args.gba_library_dir, args.gb_library_dir,
                    args.gsf_library_dir, args.gsfplay)
    threading.Thread(target=app.run, name="operator-poller", daemon=True).start()
    handler = type("PlayerHandler", (Handler,), {"workbench": app,
                                                  "web_root": Path(__file__).parent / "web"})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Epilogue GB Player: http://{args.host}:{args.port}")
    print(f"ROM dumps: {args.output_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.music.stop()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
