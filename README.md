# Xtream STRM exporter

A dependency-free Linux command-line tool that signs in to an Xtream Codes-compatible provider, reads its movie and series catalogs, and creates Jellyfin/Kodi-compatible `.strm` files.

Only use it with a provider and content library you are authorized to access. A `.strm` file contains a playable provider URL, so it necessarily contains the Xtream username and password. Keep the output directory private and do not publish, sync, or share it.

## Quick start

On Debian, Ubuntu, and other systemd-based Linux distributions, download and run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/ProTekNorth/xtream-strm/main/bootstrap.sh -o /tmp/install-xtream-strm.sh
sudo sh /tmp/install-xtream-strm.sh
```

If curl is not installed yet, use wget to fetch the bootstrapper; it will install curl before continuing:

```bash
wget -qO /tmp/install-xtream-strm.sh https://raw.githubusercontent.com/ProTekNorth/xtream-strm/main/bootstrap.sh
sudo sh /tmp/install-xtream-strm.sh
```

Alternatively, clone the repository:

```bash
git clone https://github.com/ProTekNorth/xtream-strm.git
cd xtream-strm
sudo sh install.sh
```

The installer asks for the provider address (a full M3U/API link is also accepted), username, password, library location, and whether to export movies, series, or both. After checking the login, it displays the provider's numbered movie and TV groups so you can import all groups or select individual numbers and ranges. It then creates a five-item sample. You can choose a gradual initial import, a complete import, or stop after the sample. Gradual import is recommended for very large Jellyfin libraries.

If Python 3 or curl is missing, the installer installs it automatically using `apt`, `dnf`, `yum`, `zypper`, `apk`, or `pacman`. Python 3.10 or newer is required.

## Test with a small batch first

Preview five movies and five episodes without writing anything:

```bash
sudo /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --sample 5 --dry-run
```

To create playable STRM files for that sample, omit `--dry-run`:

```bash
sudo -u xtream-strm /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --sample 5
```

Sample mode never removes unsampled files or drops them from the sync manifest. When the results look right, choose either the gradual import below or start a complete sync with `sudo systemctl start xtream-strm.service`.

## Gradual first import for large libraries

Batch mode remembers completed Xtream stream and series IDs in the output directory. Repeating the same command processes the next group without duplicating completed items. Partial runs never remove items from earlier batches.

For a fully automatic import, create an API key in the Jellyfin administrator dashboard and save the Jellyfin server URL and key through guided setup:

```bash
sudo /opt/xtream-strm/xtream_strm.py --setup --config /etc/xtream-strm/config.json
```

Then start continuous mode:

```bash
sudo -u xtream-strm /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --batch 100 --continuous
```

Continuous mode saves batch progress, starts a supported Jellyfin library scan, waits for the scan task to finish, and automatically creates the next batch. If the command is interrupted or the machine restarts, run the same command again to resume.

For movies, a batch of 1,000 is a reasonable starting point:

```bash
sudo -u xtream-strm /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --movies-only --batch 1000
```

Shows are batched as whole shows so seasons are never split. Start smaller because one show can contain many episodes:

```bash
sudo -u xtream-strm /opt/xtream-strm/xtream_strm.py --config /etc/xtream-strm/config.json --series-only --batch 100
```

Without continuous mode, repeat each command after Jellyfin finishes scanning the previous group. The exporter reports when no additional unprocessed movies or shows remain. To intentionally start batch progress over, add `--reset-batch` to the first new batch command. Once the initial import is complete, enable the regular full refresh:

```bash
sudo systemctl enable --now xtream-strm.timer
sudo systemctl start xtream-strm.service
```

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
├── Movies/Category/Movie (2026) [tmdbid-12345]/Movie (2026) [tmdbid-12345].strm
└── TV Shows/Category/Show (2020) [tmdbid-67890]/Season 01/Show (2020) - S01E01 - Episode.strm
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
- `movie_category_ids` and `series_category_ids`: provider group IDs selected by guided setup. Empty means every group of that media type.
- `normalize_names`: produce consistent media-server names by cleaning Unicode, HTML entities, whitespace, quality tags, release years, episode numbers, and season numbers.
- `add_provider_ids`: append a Jellyfin-compatible TMDB or IMDb ID when that ID is already supplied by the Xtream provider. The exporter does not scrape metadata sites or make extra per-movie metadata requests.
- `auto_strip_name_tags`: remove short uppercase provider tags before a separator, such as `PS -`, `SOM -`, `VIP:`, or `[ABC]`.
- `strip_name_prefixes`: case-insensitive provider prefixes to remove from movie and show names, such as `US:`, `UK:`, or `|EN|`.
- `preserve_name_prefixes`: uppercase prefixes that automatic detection must retain when they are part of a legitimate title.
- `category_directories`: place titles beneath their provider category.
- `clean_stale`: remove missing files created by earlier successful syncs.
- `allow_empty_library`: permit an empty provider response to clear a selected library. It is disabled by default to protect against temporary provider problems and category-filter mistakes.
- `batch_size`: process only the next remembered group of movies and whole shows. The command-line `--batch` option overrides it.
- `jellyfin_url` and `jellyfin_api_key`: optional Jellyfin connection used by `--continuous`. Keep the configuration file private because it contains both provider credentials and this key.
- `jellyfin_poll_seconds`: how often continuous mode checks Jellyfin's library-scan task.
- `jellyfin_scan_timeout`: maximum seconds to wait for one Jellyfin scan before stopping safely.
- `jellyfin_verify_tls`: keep enabled unless the local Jellyfin server uses a trusted, known self-signed certificate.
- `verify_tls`: keep enabled. Disable only for a trusted server with a known self-signed certificate.
- `file_mode` and `directory_mode`: octal permissions applied to generated content.

Run `python3 xtream_strm.py --help` for one-off options.
