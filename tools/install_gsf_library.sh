#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=$(mktemp -d)
GSF_DIR=${GSF_LIBRARY_DIR:-"$HOME/.local/share/epilogue-player/gsf"}
trap 'rm -rf "$BUILD_DIR"' EXIT INT TERM

UNRAR=${UNRAR:-}
if [ -z "$UNRAR" ]; then
  UNRAR=$(command -v unrar || command -v unrar-nonfree || true)
fi
if [ -z "$UNRAR" ]; then
  echo "Install a RAR extractor (unrar or unrar-free) and set UNRAR if needed." >&2
  exit 1
fi

python3 - "$APP_DIR/profiles/gba_music_catalog.json" "$BUILD_DIR/manifest.json" <<'PY'
import json, sys
from pathlib import Path
catalog = json.loads(Path(sys.argv[1]).read_text())
folders = sorted({name.split('/', 1)[0] for game in catalog['games']
                  for name in game.get('track_files', [])})
Path(sys.argv[2]).write_text(json.dumps(folders))
PY
python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))))' \
  "$BUILD_DIR/manifest.json" > "$BUILD_DIR/folders.txt"

while IFS= read -r folder; do
  archive="$folder.rar"
  case "$folder" in
    "Pokemon FRLG") expected='3a49fe4a69481f47d3df44d1ca1f1a011c6771dfb6dd1e2be0c0948fb3ef0b23' ;;
    ccrown) expected='5cb6933e7ee52c83d2e6238dcff38951f5b6524a5f8fc559b605cdf3260e2b0f' ;;
    fzero) expected='aee4a2ea2d66bf9a6e8a8498d092908cbe898933685d1ef99a174cc7cc7cd20d' ;;
    ml_saga) expected='0e70283f6ba7a30ab97c49d86e477b1e54bc198b689e6bc219d4f85e3a5e184c' ;;
    mmbn) expected='456404a9ee6e93854d3ca5985aab3245fc315d461bbf9dbeafa90b981efd517b' ;;
    mmbn3b) expected='0343fed261190b8b29ebc7d11baf39b964808b54f4a99c84caa9d02730249cca' ;;
    mmbn6) expected='09d1e416d01cb6cef8aad6f4cf5c72d089a79af4d298dec1d903ef456723c17f' ;;
    sma1) expected='d37f0e961dac41b73ab369aacd607e3d23dc19f26b593ed1ad3e41cd3b1327c4' ;;
    sma2) expected='6e6531ef99b7db7bad4bf223a533fb07a7c4e60901df300d25951d06ae59ccb1' ;;
    warioadvance) expected='767f84c2201b9d46d8ebb831661f39fbb46069dcb0fe7ac6c69d607e647f4838' ;;
    zerom) expected='2e6d6f92f8b241c712d8f347771e1aee76769c3c2e7003bfa3aa61b3c3a4b9a0' ;;
    *) echo "Unknown GSF archive: $archive" >&2; exit 1 ;;
  esac
  archive_path="$BUILD_DIR/$archive"
  encoded_archive=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$archive")
  curl -fsSL -A 'EpilogueGBPlayer/0.1 local music profile downloader' \
    "https://www.caitsith2.com/gsf/gsfs/$encoded_archive" -o "$archive_path"
  actual=$(sha256sum "$archive_path" | cut -d' ' -f1)
  if [ "$actual" != "$expected" ]; then
    echo "GSF source archive checksum changed: $archive" >&2
    exit 1
  fi
  extract_dir="$BUILD_DIR/extracted/$folder"
  mkdir -p "$extract_dir"
  "$UNRAR" x -o+ "$archive_path" "$extract_dir/" >/dev/null
  mkdir -p "$GSF_DIR/$folder"
  find "$extract_dir" -type f \( -iname '*.minigsf' -o -iname '*.gsflib' \) \
    -exec cp -f '{}' "$GSF_DIR/$folder/" \;
done < "$BUILD_DIR/folders.txt"

python3 - "$APP_DIR/profiles/gba_music_catalog.json" "$GSF_DIR" <<'PY'
import hashlib, json, sys
from pathlib import Path
catalog = json.loads(Path(sys.argv[1]).read_text())
root = Path(sys.argv[2])
for game in catalog['games']:
    for name, expected in zip(game['track_files'], game['track_sha256']):
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"Track checksum mismatch: {name}")
    for name, expected in zip(game['library_files'], game['library_sha256']):
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"GSFLIB checksum mismatch: {name}")
PY
echo "Installed GSF tracks under $GSF_DIR"
