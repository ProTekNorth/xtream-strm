#!/bin/sh
set -eu

ARCHIVE_URL=https://github.com/ProTekNorth/xtream-strm/archive/refs/heads/main.tar.gz
TEMP_DIR=$(mktemp -d)
ARCHIVE_FILE=$TEMP_DIR/xtream-strm.tar.gz

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT HUP INT TERM

echo "Downloading Xtream STRM exporter..."
if command -v curl >/dev/null 2>&1; then
  curl --fail --silent --show-error --location "$ARCHIVE_URL" --output "$ARCHIVE_FILE"
elif command -v wget >/dev/null 2>&1; then
  wget --quiet "$ARCHIVE_URL" --output-document="$ARCHIVE_FILE"
else
  echo "curl or wget is required." >&2
  exit 1
fi

tar -xzf "$ARCHIVE_FILE" -C "$TEMP_DIR"
sh "$TEMP_DIR/xtream-strm-main/install.sh"
