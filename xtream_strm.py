#!/usr/bin/env python3
"""Export an Xtream-compatible VOD library as .strm files."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

VERSION = "1.0.0"
LOG = logging.getLogger("xtream-strm")
MANIFEST_NAME = ".xtream-strm-manifest.json"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")


class SyncError(RuntimeError):
    """A user-facing synchronization failure."""


@dataclass(frozen=True)
class Entry:
    relative_path: Path
    stream_url: str


@dataclass
class Stats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0


DEFAULTS: dict[str, Any] = {
    "server_url": "",
    "username": "",
    "password": "",
    "output_dir": "./library",
    "sync_movies": True,
    "sync_series": True,
    "movies_directory": "Movies",
    "series_directory": "TV Shows",
    "category_directories": True,
    "include_categories": [],
    "exclude_categories": [],
    "clean_stale": True,
    "allow_empty_library": False,
    "sample_size": 0,
    "request_timeout": 30,
    "retries": 3,
    "verify_tls": True,
    "file_mode": "0640",
    "directory_mode": "0750",
}


def safe_name(value: Any, fallback: str = "Unknown", max_bytes: int = 180) -> str:
    """Return a portable, bounded filename component."""
    name = unicodedata.normalize("NFC", str(value or "")).strip()
    name = INVALID_FILENAME.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip(" .") or fallback
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return fallback


def parse_mode(value: Any, field: str) -> int:
    try:
        mode = int(str(value), 8)
    except (TypeError, ValueError) as exc:
        raise SyncError(f"{field} must be an octal mode such as 0644") from exc
    if not 0 <= mode <= 0o777:
        raise SyncError(f"{field} must be between 0000 and 0777")
    return mode


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SyncError("server_url must be a complete http:// or https:// URL")
    if parts.query or parts.fragment:
        raise SyncError("server_url must not contain a query string or fragment")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def connection_from_input(value: str) -> tuple[str, str | None, str | None]:
    """Accept either a server address or a full provider playlist/API URL."""
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    query = parse_qs(parts.query)
    username = query.get("username", [None])[0]
    password = query.get("password", [None])[0]
    path = parts.path.rstrip("/")
    for endpoint in ("/player_api.php", "/get.php", "/xmltv.php"):
        if path.lower().endswith(endpoint):
            path = path[: -len(endpoint)]
            break
    server_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return normalize_server_url(server_url), username, password


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or (default or "")


def interactive_setup(path: Path) -> None:
    if not sys.stdin.isatty():
        raise SyncError("guided setup needs an interactive terminal")
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except (OSError, json.JSONDecodeError):
            pass

    print("\nXtream STRM guided setup")
    print("Use only a provider and video library you are authorized to access.\n")
    raw_url = prompt("Provider server or full playlist URL", str(existing.get("server_url", "")) or None)
    if not raw_url:
        raise SyncError("a provider server URL is required")
    server_url, url_username, url_password = connection_from_input(raw_url)
    username = prompt("Username", str(existing.get("username") or url_username or "") or None)
    password_default = str(existing.get("password") or url_password or "")
    password = prompt("Password" + (" (press Enter to use the detected or saved value)" if password_default else ""), password_default, secret=True)
    output_dir = str(Path(prompt("Library directory", str(existing.get("output_dir", "/srv/media/xtream")))).expanduser().resolve())
    content = prompt("Content to export: both, movies, or series", "both").lower()
    if content not in {"both", "movies", "series"}:
        raise SyncError("content selection must be both, movies, or series")
    if not username or not password:
        raise SyncError("username and password are required")

    config = dict(DEFAULTS)
    config.update(existing)
    config.update({
        "server_url": server_url,
        "username": username,
        "password": password,
        "output_dir": output_dir,
        "sync_movies": content in {"both", "movies"},
        "sync_series": content in {"both", "series"},
        "sample_size": 0,
    })

    print("\nChecking provider login...")
    probe = dict(config)
    probe["server_url"] = normalize_server_url(str(config["server_url"]))
    probe["request_timeout"] = float(config["request_timeout"])
    probe["retries"] = int(config["retries"])
    probe["verify_tls"] = as_bool(config["verify_tls"], "verify_tls")
    user = XtreamClient(probe).authenticate()
    print(f"Login accepted (status: {user.get('status', 'active')}).")

    serializable = {key: config[key] for key in DEFAULTS}
    atomic_write(path.resolve(), json.dumps(serializable, indent=2) + "\n", 0o600, 0o750)
    print(f"Configuration saved to {path.resolve()}")


def as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise SyncError(f"{field} must be true or false")


def load_config(path: Path | None, args: argparse.Namespace) -> dict[str, Any]:
    config = dict(DEFAULTS)
    if path:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SyncError(f"configuration file not found: {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncError(f"could not read configuration file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SyncError("configuration must be a JSON object")
        unknown = sorted(set(loaded) - set(DEFAULTS))
        if unknown:
            raise SyncError(f"unknown configuration option(s): {', '.join(unknown)}")
        config.update(loaded)

    environment = {
        "server_url": os.getenv("XTREAM_URL"),
        "username": os.getenv("XTREAM_USERNAME"),
        "password": os.getenv("XTREAM_PASSWORD"),
        "output_dir": os.getenv("XTREAM_OUTPUT"),
    }
    config.update({key: value for key, value in environment.items() if value is not None})
    for key in ("server_url", "username", "password", "output_dir"):
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value

    if args.movies_only:
        config["sync_movies"], config["sync_series"] = True, False
    if args.series_only:
        config["sync_movies"], config["sync_series"] = False, True
    if args.keep_stale:
        config["clean_stale"] = False
    if args.sample_size is not None:
        config["sample_size"] = args.sample_size

    for field in ("sync_movies", "sync_series", "category_directories", "clean_stale", "allow_empty_library", "verify_tls"):
        config[field] = as_bool(config[field], field)
    if not config["sync_movies"] and not config["sync_series"]:
        raise SyncError("at least one of sync_movies or sync_series must be enabled")
    for field in ("server_url", "username", "password", "output_dir"):
        if not str(config[field]).strip():
            raise SyncError(f"missing required setting: {field}")
    for field in ("include_categories", "exclude_categories"):
        if not isinstance(config[field], list) or not all(isinstance(item, str) for item in config[field]):
            raise SyncError(f"{field} must be a list of category names")
    try:
        config["request_timeout"] = float(config["request_timeout"])
        config["retries"] = int(config["retries"])
        config["sample_size"] = int(config["sample_size"])
    except (TypeError, ValueError) as exc:
        raise SyncError("request_timeout, retries, and sample_size must be numeric") from exc
    if config["request_timeout"] <= 0 or not 1 <= config["retries"] <= 10:
        raise SyncError("request_timeout must be positive and retries must be between 1 and 10")
    if not 0 <= config["sample_size"] <= 1000:
        raise SyncError("sample_size must be between 0 and 1000")
    config["server_url"] = normalize_server_url(str(config["server_url"]))
    config["output_dir"] = Path(str(config["output_dir"])).expanduser().resolve()
    config["file_mode"] = parse_mode(config["file_mode"], "file_mode")
    config["directory_mode"] = parse_mode(config["directory_mode"], "directory_mode")
    config["movies_directory"] = safe_name(config["movies_directory"], "Movies")
    config["series_directory"] = safe_name(config["series_directory"], "TV Shows")
    return config


class XtreamClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = config["server_url"]
        self.username = str(config["username"])
        self.password = str(config["password"])
        self.timeout = config["request_timeout"]
        self.retries = config["retries"]
        self.ssl_context = None
        if self.base_url.startswith("https://") and not config["verify_tls"]:
            LOG.warning("TLS certificate verification is disabled")
            self.ssl_context = ssl._create_unverified_context()

    def api(self, action: str | None = None, **parameters: Any) -> Any:
        query: dict[str, Any] = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update(parameters)
        url = f"{self.base_url}/player_api.php?{urlencode(query)}"
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": f"xtream-strm/{VERSION}"})
                with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8-sig"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    delay = min(2 ** (attempt - 1), 4)
                    LOG.warning("Provider request failed (attempt %d/%d); retrying in %ds", attempt, self.retries, delay)
                    time.sleep(delay)
        raise SyncError(f"provider request failed after {self.retries} attempt(s): {last_error}")

    def authenticate(self) -> dict[str, Any]:
        payload = self.api()
        if not isinstance(payload, dict) or not isinstance(payload.get("user_info"), dict):
            raise SyncError("provider returned an unexpected login response")
        user = payload["user_info"]
        authenticated = str(user.get("auth", "0")) == "1"
        status = str(user.get("status", "")).lower()
        if not authenticated or status in {"disabled", "banned", "expired"}:
            raise SyncError(f"provider login was rejected (status: {user.get('status', 'unknown')})")
        return user

    def stream_url(self, kind: str, stream_id: Any, extension: Any) -> str:
        if kind not in {"movie", "series"}:
            raise ValueError("invalid stream kind")
        stream_id = quote(str(stream_id), safe="")
        username = quote(self.username, safe="")
        password = quote(self.password, safe="")
        extension = re.sub(r"[^A-Za-z0-9]", "", str(extension or "mp4")) or "mp4"
        return f"{self.base_url}/{kind}/{username}/{password}/{stream_id}.{extension}"


def category_map(payload: Any) -> dict[str, str]:
    if not isinstance(payload, list):
        raise SyncError("provider returned an invalid category list")
    result = {}
    for item in payload:
        if isinstance(item, dict) and item.get("category_id") is not None:
            result[str(item["category_id"])] = safe_name(item.get("category_name"), "Uncategorized")
    return result


def category_allowed(name: str, config: dict[str, Any]) -> bool:
    normalized = name.casefold()
    include = {item.casefold() for item in config["include_categories"]}
    exclude = {item.casefold() for item in config["exclude_categories"]}
    return (not include or normalized in include) and normalized not in exclude


def with_category(root: str, category: str, config: dict[str, Any]) -> Path:
    path = Path(root)
    if config["category_directories"]:
        path /= safe_name(category, "Uncategorized")
    return path


def movie_year(item: dict[str, Any]) -> str | None:
    for key in ("year", "releaseDate", "release_date", "releasedate"):
        match = YEAR_PATTERN.search(str(item.get(key, "")))
        if match:
            return match.group(1)
    return None


def unique_entries(entries: Iterable[Entry]) -> list[Entry]:
    """Resolve provider-side duplicate titles without overwriting either stream."""
    used: set[str] = set()
    result: list[Entry] = []
    for entry in entries:
        path = entry.relative_path
        candidate = path
        counter = 2
        while candidate.as_posix().casefold() in used:
            candidate = path.with_name(f"{path.stem} [{counter}]{path.suffix}")
            counter += 1
        used.add(candidate.as_posix().casefold())
        result.append(Entry(candidate, entry.stream_url))
    return result


def collect_movies(client: XtreamClient, config: dict[str, Any]) -> list[Entry]:
    categories = category_map(client.api("get_vod_categories"))
    streams = client.api("get_vod_streams")
    if not isinstance(streams, list):
        raise SyncError("provider returned an invalid movie list")
    entries = []
    for item in streams:
        if not isinstance(item, dict) or item.get("stream_id") is None:
            continue
        category = categories.get(str(item.get("category_id")), "Uncategorized")
        if not category_allowed(category, config):
            continue
        title = safe_name(item.get("name"), f"Movie {item['stream_id']}")
        year = movie_year(item)
        display = title if not year or YEAR_PATTERN.search(title) else f"{title} ({year})"
        folder = with_category(config["movies_directory"], category, config) / display
        path = folder / f"{display}.strm"
        entries.append(Entry(path, client.stream_url("movie", item["stream_id"], item.get("container_extension"))))
        if config["sample_size"] and len(entries) >= config["sample_size"]:
            break
    return unique_entries(entries)


def episode_number(item: dict[str, Any], fallback: int) -> int:
    value = item.get("episode_num", item.get("episode_number", fallback))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def iter_episodes(payload: Any) -> Iterable[tuple[int, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return
    episodes = payload.get("episodes")
    if isinstance(episodes, dict):
        groups = episodes.items()
    elif isinstance(episodes, list):
        groups = (("0", episodes),)
    else:
        return
    for season_key, group in groups:
        if not isinstance(group, list):
            continue
        for index, episode in enumerate(group, start=1):
            if not isinstance(episode, dict) or episode.get("id") is None:
                continue
            try:
                season = int(episode.get("season", season_key))
            except (TypeError, ValueError):
                season = 0
            yield max(0, season), {**episode, "_fallback_number": index}


def collect_series(client: XtreamClient, config: dict[str, Any]) -> list[Entry]:
    categories = category_map(client.api("get_series_categories"))
    series = client.api("get_series")
    if not isinstance(series, list):
        raise SyncError("provider returned an invalid series list")
    entries = []
    total = len(series)
    for position, show in enumerate(series, start=1):
        if not isinstance(show, dict) or show.get("series_id") is None:
            continue
        category = categories.get(str(show.get("category_id")), "Uncategorized")
        if not category_allowed(category, config):
            continue
        show_name = safe_name(show.get("name"), f"Series {show['series_id']}")
        LOG.info("Reading series %d/%d: %s", position, total, show_name)
        detail = client.api("get_series_info", series_id=show["series_id"])
        for season, episode in iter_episodes(detail):
            number = episode_number(episode, episode["_fallback_number"])
            episode_title = safe_name(episode.get("title"), f"Episode {number}")
            code = f"S{season:02d}E{number:02d}"
            folder = with_category(config["series_directory"], category, config) / show_name / f"Season {season:02d}"
            filename = safe_name(f"{show_name} - {code} - {episode_title}") + ".strm"
            entries.append(Entry(folder / filename, client.stream_url("series", episode["id"], episode.get("container_extension"))))
            if config["sample_size"] and len(entries) >= config["sample_size"]:
                return unique_entries(entries)
    return unique_entries(entries)


def read_manifest(output: Path) -> set[str]:
    path = output / MANIFEST_NAME
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data.get("files", [])
        if not isinstance(files, list):
            raise ValueError("files is not a list")
        return {str(item) for item in files if isinstance(item, str)}
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SyncError(f"could not read existing manifest {path}: {exc}") from exc


def safe_manifest_target(output: Path, relative: str) -> Path | None:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or posix.suffix.lower() != ".strm":
        LOG.warning("Ignoring unsafe manifest entry: %s", relative)
        return None
    target = output.joinpath(*posix.parts).resolve()
    try:
        target.relative_to(output)
    except ValueError:
        LOG.warning("Ignoring manifest entry outside output directory: %s", relative)
        return None
    return target


def atomic_write(path: Path, content: str, mode: int, directory_mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, directory_mode)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remove_empty_parents(path: Path, output: Path) -> None:
    current = path.parent
    while current != output:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def apply_entries(entries: list[Entry], config: dict[str, Any], dry_run: bool) -> Stats:
    output: Path = config["output_dir"]
    old_files = read_manifest(output) if output.exists() else set()
    new_files = {entry.relative_path.as_posix() for entry in entries}
    sample_mode = config["sample_size"] > 0
    active_roots = set()
    if config["sync_movies"]:
        active_roots.add(config["movies_directory"].casefold())
    if config["sync_series"]:
        active_roots.add(config["series_directory"].casefold())
    old_managed_files = set() if sample_mode else {
        relative
        for relative in old_files
        if PurePosixPath(relative).parts and PurePosixPath(relative).parts[0].casefold() in active_roots
    }
    preserved_files = old_files - old_managed_files
    stats = Stats()
    for entry in entries:
        target = output / entry.relative_path
        content = entry.stream_url + "\n"
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else None
        except OSError as exc:
            raise SyncError(f"could not read {target}: {exc}") from exc
        if existing == content:
            stats.unchanged += 1
        elif existing is None:
            stats.created += 1
            LOG.debug("Create %s", entry.relative_path)
            if not dry_run:
                atomic_write(target, content, config["file_mode"], config["directory_mode"])
        else:
            stats.updated += 1
            LOG.debug("Update %s", entry.relative_path)
            if not dry_run:
                atomic_write(target, content, config["file_mode"], config["directory_mode"])

    if config["clean_stale"]:
        for relative in sorted(old_managed_files - new_files):
            target = safe_manifest_target(output, relative)
            if target and target.is_file():
                stats.removed += 1
                LOG.debug("Remove stale %s", relative)
                if not dry_run:
                    target.unlink()
                    remove_empty_parents(target, output)

    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        manifest = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "files": sorted(new_files | preserved_files),
        }
        atomic_write(output / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n", config["file_mode"], config["directory_mode"])
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export an Xtream-compatible VOD library as .strm files.")
    parser.add_argument("--config", type=Path, help="JSON configuration file")
    parser.add_argument("--setup", action="store_true", help="run the guided configuration wizard")
    parser.add_argument("--url", dest="server_url", help="provider server URL")
    parser.add_argument("--username", help="provider username")
    parser.add_argument("--password", help="provider password (prefer XTREAM_PASSWORD)")
    parser.add_argument("--output", dest="output_dir", help="library output directory")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--movies-only", action="store_true", help="export movies only")
    selection.add_argument("--series-only", action="store_true", help="export series only")
    parser.add_argument("--keep-stale", action="store_true", help="do not remove files missing from the provider")
    parser.add_argument("--sample", dest="sample_size", nargs="?", type=int, const=5, metavar="SIZE", help="process a small sample (default: 5 per selected library)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing files")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase logging detail")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.setup:
            interactive_setup(args.config or Path("config.json"))
            return 0
        config = load_config(args.config, args)
        if config["sample_size"]:
            LOG.info("Sample mode: processing at most %d item(s) per selected library; stale cleanup is disabled", config["sample_size"])
        client = XtreamClient(config)
        user = client.authenticate()
        LOG.info("Connected (account status: %s)", user.get("status", "active"))
        entries: list[Entry] = []
        if config["sync_movies"]:
            movies = collect_movies(client, config)
            if not movies and not config["allow_empty_library"]:
                raise SyncError("movie catalog is empty; refusing to replace the existing library (set allow_empty_library to true to permit this)")
            entries.extend(movies)
            LOG.info("Found %d movie(s)", len(movies))
        if config["sync_series"]:
            episodes = collect_series(client, config)
            if not episodes and not config["allow_empty_library"]:
                raise SyncError("series catalog has no episodes; refusing to replace the existing library (set allow_empty_library to true to permit this)")
            entries.extend(episodes)
            LOG.info("Found %d episode(s)", len(episodes))
        stats = apply_entries(entries, config, args.dry_run)
        prefix = "Dry run complete" if args.dry_run else "Sync complete"
        LOG.info("%s: %d created, %d updated, %d unchanged, %d removed", prefix, stats.created, stats.updated, stats.unchanged, stats.removed)
        return 0
    except (SyncError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(run())
