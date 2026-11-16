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
    def __init__(self, catalog: Path, cache_dir: Path, gbsplay: str) -> None:
        self.catalog_path = catalog
        self.cache_dir = cache_dir
        self.gbsplay = gbsplay
        self.lock = threading.RLock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.paused = False
        self.profile: dict[str, Any] | None = None
        self.gbs_file: Path | None = None
        self.track_index = 0
        self.status = "STOPPED"
        self.message = "Waiting for a supported cartridge."

    def profiles(self) -> list[dict[str, Any]]:
        return json.loads(self.catalog_path.read_text(encoding="utf-8"))["games"]

    def match(self, sha256: str) -> dict[str, Any] | None:
        return next((game for game in self.profiles()
                     if sha256 in game.get("rom_sha256", [])), None)

    def prepare(self, profile: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
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
            self.track_index = metadata["first_track"] - 1
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
                self.message = "No supported GBS playback profile is loaded."
                return
            if index is not None:
                self.track_index = index % len(self.profile["tracks"])
            self._stop_locked()
            subsong = self.track_index + 1
            try:
                self.proc = subprocess.Popen(
                    [self.gbsplay, "-q", "-o", "pulse", "-L", "-t", "3600",
                     "--", str(self.gbs_file), str(subsong)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self.status = "ERROR"
                self.message = f"Could not start gbsplay: {exc}"
                self.proc = None
                return
            time.sleep(0.15)
            if self.proc.poll() is not None:
                self.status = "ERROR"
                self.message = "gbsplay could not open the selected module or audio output."
                self.proc = None
                return
            self.status = "PLAYING"
            self.message = "Playing through the computer's default audio output."

    def toggle(self) -> None:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.play()
            elif self.paused:
                os.kill(self.proc.pid, signal.SIGCONT)
                self.paused = False
                self.status = "PLAYING"
                self.message = "Playback resumed."
            else:
                os.kill(self.proc.pid, signal.SIGSTOP)
                self.paused = True
                self.status = "PAUSED"
                self.message = "Playback paused."

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

    def step(self, amount: int) -> None:
        with self.lock:
            if self.profile is None:
                return
            next_index = (self.track_index + amount) % len(self.profile["tracks"])
        self.play(next_index)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            if self.proc and not self.paused and self.proc.poll() is not None:
                self.proc = None
                self.status = "READY" if self.profile else "STOPPED"
            tracks = self.profile.get("tracks", []) if self.profile else []
            return {
                "status": self.status, "message": self.message,
                "game": self.profile.get("title") if self.profile else None,
                "supported": self.profile is not None,
                "track_index": self.track_index,
                "track_count": len(tracks), "tracks": tracks,
                "current_track": tracks[self.track_index] if tracks else None,
            }


class Workbench:
    def __init__(self, throwback: str, output_dir: Path, catalog: Path,
                 cache_dir: Path, gbsplay: str) -> None:
        self.throwback = throwback
        self.output_dir = output_dir
        self.music = MusicPlayer(catalog, cache_dir, gbsplay)
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

    def _set(self, **values: Any) -> None:
        with self.lock:
            self.state.update(values)
            self.state["updated"] = datetime.now(timezone.utc).isoformat()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = json.loads(json.dumps(self.state))
        result["player"] = self.music.snapshot()
        return result

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
        if cart_type not in ("GB", "GBC"):
            self.music.unload()
            self._set(device="CONNECTED", cartridge="UNSUPPORTED TYPE",
                      message=f"Detected {cart_type or 'unknown'} cartridge.",
                      game={"title": title, "operator_info": info, "analysis": None})
            return
        key = "|".join(info.get(k, "") for k in
                       ("game_id", "title", "mbc", "rom_size", "version", "header_checksum"))
        is_new = key != self.active_key
        if not force_dump and not is_new and self.dumped_key == key:
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
            self.music.stop()
            player_text = "This ROM has no installed playback profile yet."
        self._refresh_dumps()
        self._set(cartridge="DUMPED", message="ROM read and validated locally.",
                  game={"title": analysis["title"], "operator_info": info,
                        "analysis": analysis, "supported": profile is not None},
                  playback_message=player_text)

    def _refresh_dumps(self) -> None:
        files = []
        if self.output_dir.exists():
            for path in sorted(self.output_dir.glob("*"), key=lambda p: p.stat().st_mtime,
                               reverse=True):
                if path.suffix.lower() not in (".gb", ".gbc") or path.name.startswith("._"):
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
            force = self.force_scan.wait(2.0)
            self.force_scan.clear()
            self.scan_once(force_dump=force)


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
        if self.path == "/api/status":
            self._json(200, self.workbench.snapshot())
        elif self.path in ("/", "/index.html"):
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
            "/api/stop": lambda: self.workbench.music.stop(),
            "/api/next": lambda: self.workbench.music.step(1),
            "/api/previous": lambda: self.workbench.music.step(-1),
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
    parser.add_argument("--catalog", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output-dir", type=Path,
                        default=Path.home() / "games" / "gb" / "operator-dumps")
    parser.add_argument("--cache-dir", type=Path,
                        default=Path.home() / ".local" / "share" / "epilogue-player" / "gbs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    app = Workbench(args.throwback, args.output_dir, args.catalog, args.cache_dir, args.gbsplay)
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
