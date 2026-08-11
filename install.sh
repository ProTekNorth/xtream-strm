#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root: sudo sh install.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install python3 and run this again." >&2
  exit 1
fi

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

chown xtream-strm:media "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

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
  chgrp media "$OUTPUT_DIR"
  chmod g+rwx "$OUTPUT_DIR"
else
  install -d -o xtream-strm -g media -m 0770 "$OUTPUT_DIR"
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
printf "Continue with the complete library and enable six-hour refreshes? [Y/n]: "
read -r continue_answer
case "$continue_answer" in
  n|N|no|NO)
    echo "Stopped after the sample. No scheduled full sync was enabled."
    echo "When ready, run: sudo systemctl enable --now xtream-strm.timer"
    echo "Then run: sudo systemctl start xtream-strm.service"
    exit 0
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
#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root: sudo sh install.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install python3 and run this again." >&2
  exit 1
fi

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

chown xtream-strm:media "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

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
  chgrp media "$OUTPUT_DIR"
  chmod g+rwx "$OUTPUT_DIR"
else
  install -d -o xtream-strm -g media -m 0770 "$OUTPUT_DIR"
fi
install -m 0644 "$SCRIPT_DIR/xtream-strm.service" /etc/systemd/system/xtream-strm.service
install -m 0644 "$SCRIPT_DIR/xtream-strm.timer" /etc/systemd/system/xtream-strm.timer

if id jellyfin >/dev/null 2>&1; then
  usermod -aG media jellyfin
fi

systemctl daemon-reload
systemctl enable --now xtream-strm.timer

echo ""
echo "Installation complete. Starting the first library sync now..."
if systemctl start xtream-strm.service; then
  echo "Sync finished successfully. The library will refresh every six hours."
  echo "Library: $OUTPUT_DIR"
else
  echo "The service was installed, but the first sync failed." >&2
  echo "View the reason with: sudo journalctl -u xtream-strm.service -n 100" >&2
  exit 1
fi
