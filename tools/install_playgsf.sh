#!/bin/sh
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT INT TERM
ARCHIVE="$BUILD_DIR/playgsf.tar.gz"
SOURCE_URL="https://projects.raphnet.net/playgsf/playgsf-0.7.1.tar.gz"

command -v cc >/dev/null || { echo "A C compiler is required." >&2; exit 1; }
command -v c++ >/dev/null || { echo "A C++ compiler is required." >&2; exit 1; }
command -v make >/dev/null || { echo "make is required." >&2; exit 1; }
AO_CPPFLAGS=
AO_LDFLAGS=
if ! printf '#include <ao/ao.h>\n' | cc -E - >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null || ! command -v dpkg-deb >/dev/null; then
    echo "Install libao development headers (for example: libao-dev) and zlib development headers first." >&2
    exit 1
  fi
  (cd "$BUILD_DIR" && apt-get download libao-dev >/dev/null)
  mkdir "$BUILD_DIR/ao"
  dpkg-deb -x "$BUILD_DIR"/libao-dev_*.deb "$BUILD_DIR/ao"
  AO_RUNTIME=$(ldconfig -p | awk '$1 == "libao.so.4" { print $NF; exit }')
  if [ -z "$AO_RUNTIME" ]; then
    echo "The libao runtime library is required to play GSF audio." >&2
    exit 1
  fi
  rm -f "$BUILD_DIR/ao/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)/libao.so"
  ln -s "$AO_RUNTIME" "$BUILD_DIR/ao/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)/libao.so"
  AO_CPPFLAGS="-I$BUILD_DIR/ao/usr/include"
  AO_LDFLAGS="-L$BUILD_DIR/ao/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
fi

curl -fsSL "$SOURCE_URL" -o "$ARCHIVE"
printf '%s  %s\n' \
  '1ba45e733ca29fdbf36ee7d307391ae6e0a134deb9f1f2576b44dc8f4ac3ebcd' "$ARCHIVE" \
  | sha256sum -c - >/dev/null
tar -xzf "$ARCHIVE" -C "$BUILD_DIR"
cd "$BUILD_DIR/playgsf-0.7.1"

# Current compilers treat the old missing return as an illegal instruction.
python3 - <<'PY'
from pathlib import Path
p = Path("VBA/Util.cpp")
s = p.read_text()
old = "*dst = src[0] | (src[1]<<8) | (src[2]<<16) | (src[3]<<24);\n}"
new = "*dst = src[0] | (src[1]<<8) | (src[2]<<16) | (src[3]<<24);\n\treturn 1;\n}"
if old not in s:
    raise SystemExit("playgsf source did not match the expected compatibility patch")
p.write_text(s.replace(old, new, 1))
PY

CFLAGS='-fpermissive' CXXFLAGS='-fpermissive' \
CPPFLAGS="$AO_CPPFLAGS" LDFLAGS="-no-pie $AO_LDFLAGS" \
  ./configure --enable-ccore --disable-optimisations
# Keep libraries after the objects for modern linkers using --as-needed.
sed -i "s|^CFLAGS=.*|& -fpermissive $AO_CPPFLAGS|" Makefile
sed -i "s|^LDFLAGS=.*|& $AO_LDFLAGS -no-pie|" Makefile
sed -i 's/-lresample -o playgsf/-lresample -lao -lz -o playgsf/' Makefile
make -j2
install -Dm755 playgsf "$HOME/.local/bin/playgsf"
echo "Installed $HOME/.local/bin/playgsf"
