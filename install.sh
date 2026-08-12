#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root: sudo sh install.sh" >&2
  exit 1
fi

install_dependencies() {
  need_python=$1
  need_curl=$2

  if command -v apt-get >/dev/null 2>&1; then
    packages=""
    if [ "$need_python" = yes ]; then packages="$packages python3"; fi
    if [ "$need_curl" = yes ]; then packages="$packages curl"; fi
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y $packages
  elif command -v dnf >/dev/null 2>&1; then
    packages=""
    if [ "$need_python" = yes ]; then packages="$packages python3"; fi
    if [ "$need_curl" = yes ]; then packages="$packages curl"; fi
    dnf install -y $packages
  elif command -v yum >/dev/null 2>&1; then
    packages=""
    if [ "$need_python" = yes ]; then packages="$packages python3"; fi
    if [ "$need_curl" = yes ]; then packages="$packages curl"; fi
    yum install -y $packages
  elif command -v zypper >/dev/null 2>&1; then
    packages=""
    if [ "$need_python" = yes ]; then packages="$packages python3"; fi
    if [ "$need_curl" = yes ]; then packages="$packages curl"; fi
    zypper --non-interactive install $packages
  elif command -v apk >/dev/null 2>&1; then
    packages=""
    if [ "$need_python" = yes ]; then packages="$packages python3"; fi
    if [ "$need_curl" = yes ]; then packages="$packages curl"; fi
    apk add --no-cache $packages
  elif command -v pacman >/dev/null 2>&1; then
    packages=""
    if [ "$need_python" = yes ]; then packages="$packages python"; fi
    if [ "$need_curl" = yes ]; then packages="$packages curl"; fi
    pacman -Sy --noconfirm $packages
  else
    echo "Could not find a supported package manager." >&2
    echo "Install Python 3.10+ and curl manually, then run this installer again." >&2
    exit 1
  fi
}

NEED_PYTHON=no
NEED_CURL=no
command -v python3 >/dev/null 2>&1 || NEED_PYTHON=yes
command -v curl >/dev/null 2>&1 || NEED_CURL=yes

if [ "$NEED_PYTHON" = yes ] || [ "$NEED_CURL" = yes ]; then
  echo "Installing missing runtime requirements..."
  install_dependencies "$NEED_PYTHON" "$NEED_CURL"
fi

command -v python3 >/dev/null 2>&1 || { echo "Python installation failed." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl installation failed." >&2; exit 1; }

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required. Upgrade python3 and run this again." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "This installer requires a systemd-based Linux distribution." >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR=/opt/xtream-strm
CONFIG_DIR=/etc/xtream-strm
CONFIG_FILE=$CONFIG_DIR/config.json

if ! getent group media >/dev/null 2>&1; then
  groupadd --system media
fi

if ! id xtream-strm >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin --gid media xtream-strm
fi

install -d -m 0755 "$INSTALL_DIR"
install -d -o root -g media -m 0750 "$CONFIG_DIR"
install -m 0755 "$SCRIPT_DIR/xtream_strm.py" "$INSTALL_DIR/xtream_strm.py"

RECONFIGURE=yes
if [ -f "$CONFIG_FILE" ]; then
  printf "An existing configuration was found. Reconfigure it? [y/N]: "
  read -r answer
  case "$answer" in
    y|Y|yes|YES) RECONFIGURE=yes ;;
    *) RECONFIGURE=no ;;
  esac
fi

if [ "$RECONFIGURE" = yes ]; then
  "$INSTALL_DIR/xtream_strm.py" --setup --config "$CONFIG_FILE"
fi

chown xtream-strm:root "$CONFIG_FILE"
chmod 0600 "$CONFIG_FILE"

OUTPUT_DIR=$(python3 -c 'import json, os, sys; print(os.path.abspath(os.path.expanduser(json.load(open(sys.argv[1], encoding="utf-8"))["output_dir"])))' "$CONFIG_FILE")
case "$OUTPUT_DIR" in
  /*) ;;
  *) echo "The library directory must be an absolute path." >&2; exit 1 ;;
esac

case "$OUTPUT_DIR" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    echo "Refusing to use a system directory as the library root: $OUTPUT_DIR" >&2
    echo "Choose a dedicated subdirectory such as /srv/media/xtream." >&2
    exit 1
    ;;
  /boot/*|/etc/*|/home/*|/root/*|/usr/*)
    echo "The scheduled service cannot write to this protected location: $OUTPUT_DIR" >&2
    echo "Choose a dedicated location such as /srv/media/xtream." >&2
    exit 1
    ;;
esac

if [ -L "$OUTPUT_DIR" ]; then
  echo "Refusing to change permissions on a symbolic-link library directory: $OUTPUT_DIR" >&2
  exit 1
fi

if [ -d "$OUTPUT_DIR" ]; then
  if ! chgrp media "$OUTPUT_DIR" 2>/dev/null; then
    echo "Note: this mounted library does not allow group changes; checking actual write access instead."
  fi
  if ! chmod g+rwx "$OUTPUT_DIR" 2>/dev/null; then
    echo "Note: this mounted library does not allow permission changes; checking actual write access instead."
  fi
else
  install -d -o xtream-strm -g media -m 0770 "$OUTPUT_DIR"
fi

if command -v runuser >/dev/null 2>&1; then
  if ! runuser -u xtream-strm -- python3 -c 'import os, sys, tempfile; fd, path = tempfile.mkstemp(prefix=".xtream-strm-write-test.", dir=sys.argv[1]); os.close(fd); os.unlink(path)' "$OUTPUT_DIR"; then
    echo "The xtream-strm service account cannot write to: $OUTPUT_DIR" >&2
    echo "Adjust the mount ownership or permissions, then run this installer again." >&2
    exit 1
  fi
elif ! su -s /bin/sh xtream-strm -c "test -w '$OUTPUT_DIR'"; then
  echo "The xtream-strm service account cannot write to: $OUTPUT_DIR" >&2
  echo "Adjust the mount ownership or permissions, then run this installer again." >&2
  exit 1
fi
install -m 0644 "$SCRIPT_DIR/xtream-strm.service" /etc/systemd/system/xtream-strm.service
install -m 0644 "$SCRIPT_DIR/xtream-strm.timer" /etc/systemd/system/xtream-strm.timer

if id jellyfin >/dev/null 2>&1; then
  usermod -aG media jellyfin
fi

systemctl daemon-reload

echo ""
echo "Running a small sample sync before processing the full library..."
if command -v runuser >/dev/null 2>&1; then
  runuser -u xtream-strm -- "$INSTALL_DIR/xtream_strm.py" --config "$CONFIG_FILE" --sample 5
else
  su -s /bin/sh xtream-strm -c "'$INSTALL_DIR/xtream_strm.py' --config '$CONFIG_FILE' --sample 5"
fi

echo ""
echo "The sample is ready in: $OUTPUT_DIR"
echo "Choose how to create the initial library:"
echo "  1) Gradual batches (recommended for large libraries)"
echo "  2) Complete library now"
echo "  3) Stop after the sample"
printf "Selection [1]: "
read -r import_choice
case "$import_choice" in
  3)
    echo "Stopped after the sample. No scheduled sync was enabled."
    exit 0
    ;;
  2) ;;
  1|"")
    JELLYFIN_READY=$(python3 -c 'import json, sys; c=json.load(open(sys.argv[1], encoding="utf-8")); print("yes" if c.get("jellyfin_url") and c.get("jellyfin_api_key") else "no")' "$CONFIG_FILE")
    if [ "$JELLYFIN_READY" = yes ]; then
      printf "Run every batch automatically and wait for Jellyfin scans? [Y/n]: "
      read -r automatic_answer
      case "$automatic_answer" in
        n|N|no|NO) ;;
        *)
          echo "Starting continuous gradual import. You can safely interrupt and resume this command."
          if command -v runuser >/dev/null 2>&1; then
            runuser -u xtream-strm -- "$INSTALL_DIR/xtream_strm.py" --config "$CONFIG_FILE" --batch 100 --continuous
          else
            su -s /bin/sh xtream-strm -c "'$INSTALL_DIR/xtream_strm.py' --config '$CONFIG_FILE' --batch 100 --continuous"
          fi
          systemctl enable --now xtream-strm.timer
          echo "Initial import complete. Regular six-hour refreshes are enabled."
          echo "Library: $OUTPUT_DIR"
          exit 0
          ;;
      esac
    fi
    echo "Starting the first gradual batch (up to 100 movies and 100 shows)..."
    if command -v runuser >/dev/null 2>&1; then
      runuser -u xtream-strm -- "$INSTALL_DIR/xtream_strm.py" --config "$CONFIG_FILE" --batch 100
    else
      su -s /bin/sh xtream-strm -c "'$INSTALL_DIR/xtream_strm.py' --config '$CONFIG_FILE' --batch 100"
    fi
    echo ""
    echo "The first batch is ready. Let Jellyfin scan it, then repeat:"
    echo "  sudo -u xtream-strm $INSTALL_DIR/xtream_strm.py --config $CONFIG_FILE --batch 100"
    echo "If Jellyfin is configured later, add --continuous to run every remaining batch automatically."
    echo "When batching is complete, enable regular six-hour refreshes:"
    echo "  sudo systemctl enable --now xtream-strm.timer"
    echo "  sudo systemctl start xtream-strm.service"
    exit 0
    ;;
  *)
    echo "Invalid selection." >&2
    exit 1
    ;;
esac

systemctl enable --now xtream-strm.timer
echo "Starting the complete library sync now..."
if systemctl start xtream-strm.service; then
  echo "Sync finished successfully. The library will refresh every six hours."
  echo "Library: $OUTPUT_DIR"
else
  echo "The service was installed, but the first sync failed." >&2
  echo "View the reason with: sudo journalctl -u xtream-strm.service -n 100" >&2
  exit 1
fi
