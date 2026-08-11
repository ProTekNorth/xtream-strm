# Xtream STRM exporter

A dependency-free Linux command-line tool that signs in to an Xtream Codes-compatible provider, reads its movie and series catalogs, and creates Jellyfin/Kodi-compatible `.strm` files.

Only use it with a provider and content library you are authorized to access. A `.strm` file contains a playable provider URL, so it necessarily contains the Xtream username and password. Keep the output directory private and do not publish, sync, or share it.

## Quick start

On Debian, Ubuntu, and other systemd-based Linux distributions, download and run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/ProTekNorth/xtream-strm/main/bootstrap.sh -o /tmp/install-xtream-strm.sh
sudo sh /tmp/install-xtream-strm.sh
```

Alternatively, clone the repository:

```bash
git clone https://github.com/ProTekNorth/xtream-strm.git
cd xtream-strm
sudo sh install.sh
```

The installer asks for the provider address (a full M3U/API link is also accepted), username, password, library location, and whether to export movies, series, or both. It checks the login and creates a five-item sample first. You can inspect that sample before approving the complete sync and six-hour schedule.

## Test with a small batch first

Preview five movies and five episodes without writing anything:

```bash
sudo /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --sample 5 --dry-run
```

To create playable STRM files for that sample, omit `--dry-run`:

```bash
sudo -u xtream-strm /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --sample 5
```

Sample mode never removes unsampled files or drops them from the sync manifest. When the results look right, start the complete sync with `sudo systemctl start xtream-strm.service`.

Useful commands after installation:

```bash
sudo systemctl start xtream-strm.service
sudo journalctl -u xtream-strm.service -n 100
sudo /opt/xtream-strm/xtream_strm.py --setup --config /etc/xtream-strm/config.json
```

Python 3.10 or newer is required. No Python packages need to be installed.

For a quick run without a config file, use environment variables so the password does not appear in shell history:

```bash
export XTREAM_URL='https://provider.example.com:443'
export XTREAM_USERNAME='username'
read -rsp 'Xtream password: ' XTREAM_PASSWORD && export XTREAM_PASSWORD
export XTREAM_OUTPUT='/srv/media/xtream'
python3 xtream_strm.py --dry-run
python3 xtream_strm.py
```

The generated layout is:

```text
/srv/media/xtream/
├── Movies/Category/Movie (2026)/Movie (2026).strm
└── TV Shows/Category/Show/Season 01/Show - S01E01 - Episode.strm
```

Point Jellyfin at the `Movies` and `TV Shows` directories as separate libraries. A later run updates changed URLs and removes only stale `.strm` files listed in the tool's own manifest. Use `--keep-stale` to disable removal. Movie-only runs leave the existing series files alone, and vice versa.

## Manual systemd installation

The guided installer handles this section automatically. For a custom deployment, create the service user and grant it, Jellyfin, and this tool access to a shared `media` group.

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin xtream-strm
sudo usermod -aG media jellyfin
sudo chown xtream-strm:xtream-strm /etc/xtream-strm/config.json
sudo chown -R xtream-strm:media /srv/media/xtream
sudo install -m 0644 xtream-strm.service /etc/systemd/system/
sudo install -m 0644 xtream-strm.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xtream-strm.timer
sudo systemctl start xtream-strm.service
sudo journalctl -u xtream-strm.service
```

The included timer refreshes every six hours.

## Docker

Copy values into a local `.env`, then run the one-shot container whenever a refresh is needed:

```bash
docker compose run --rm xtream-strm
```

Schedule that command with the host's systemd timer or cron. The default host output is `./library`; set `MEDIA_PATH` to change it.

## Configuration

Command-line connection settings override environment variables, which override the JSON file. Supported environment variables are `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`, and `XTREAM_OUTPUT`.

- `include_categories`: exact, case-insensitive category names to include; empty means all.
- `exclude_categories`: exact, case-insensitive names to skip.
- `category_directories`: place titles beneath their provider category.
- `clean_stale`: remove missing files created by earlier successful syncs.
- `allow_empty_library`: permit an empty provider response to clear a selected library. It is disabled by default to protect against temporary provider problems and category-filter mistakes.
- `verify_tls`: keep enabled. Disable only for a trusted server with a known self-signed certificate.
- `file_mode` and `directory_mode`: octal permissions applied to generated content.

Run `python3 xtream_strm.py --help` for one-off options.
