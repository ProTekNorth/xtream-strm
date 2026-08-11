#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root: sudo sh bootstrap.sh" >&2
  exit 1
fi

install_curl() {
  echo "curl is not installed; installing it now..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y curl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y curl
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install curl
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache curl
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm curl
  else
    echo "Could not find a supported package manager to install curl." >&2
    echo "Install curl manually, then run this installer again." >&2
    exit 1
  fi
}

if ! command -v curl >/dev/null 2>&1; then
  install_curl
fi

ARCHIVE_URL=https://github.com/ProTekNorth/xtream-strm/archive/refs/heads/main.tar.gz
TEMP_DIR=$(mktemp -d)
ARCHIVE_FILE=$TEMP_DIR/xtream-strm.tar.gz

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT HUP INT TERM

echo "Downloading Xtream STRM exporter..."
curl --fail --silent --show-error --location "$ARCHIVE_URL" --output "$ARCHIVE_FILE"

tar -xzf "$ARCHIVE_FILE" -C "$TEMP_DIR"
sh "$TEMP_DIR/xtream-strm-main/install.sh"
