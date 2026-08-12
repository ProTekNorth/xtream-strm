#!/usr/bin/env python3
"""Export an Xtream-compatible VOD library as .strm files."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

VERSION = "1.3.0"
LOG = logging.getLogger("xtream-strm")
MANIFEST_NAME = ".xtream-strm-manifest.json"
BATCH_STATE_NAME = ".xtream-strm-batch.json"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")
QUALITY_TOKEN = r"(?:4k|(?:2160|1080|720|576|480)p|uhd|fhd|hd|sd|hdr10\+?|hdr|dolby[ ._-]*vision|dv|hevc|x26[45]|h[ .]?26[45]|multi(?:[ ._-]*(?:audio|sub(?:title)?s?))?)"
BRACKETED_QUALITY = re.compile(rf"\s*[\[({{]\s*{QUALITY_TOKEN}\s*[\])}}]\s*", re.IGNORECASE)
LEADING_QUALITY = re.compile(rf"^\s*{QUALITY_TOKEN}\s*(?:[-|•:]\s*)", re.IGNORECASE)
TRAILING_QUALITY = re.compile(rf"\s*(?:[-|•]\s*){QUALITY_TOKEN}\s*$", re.IGNORECASE)
TRAILING_BRACKETED_YEAR = re.compile(r"\s*[\[({](19\d{2}|20\d{2})[\])}]\s*$")
TRAILING_SEPARATED_YEAR = re.compile(r"\s*(?:[-|]\s*|\s+)(19\d{2}|20\d{2})\s*$")
LEADING_PROVIDER_TAG = re.compile(r"^(?P<tag>[A-Z][A-Z0-9]{1,5})\s*(?:-\s+|\|\s*|:\s+)")
LEADING_BRACKETED_TAG = re.compile(r"^[\[({](?P<tag>[A-Z][A-Z0-9]{1,5})[\])}]\s*(?:[-|:]\s*)?")
JELLYFIN_PROVIDER_ID = re.compile(r"\[(?:tmdb|imdb)id-[^\]]+\]", re.IGNORECASE)


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


@dataclass
class BatchState:
    account: str
    movies: set[str] = field(default_factory=set)
    series: set[str] = field(default_factory=set)
    new_movies: set[str] = field(default_factory=set)
    new_series: set[str] = field(default_factory=set)


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
    "normalize_names": True,
    "add_provider_ids": True,
    "auto_strip_name_tags": True,
    "strip_name_prefixes": ["US:", "UK:", "|EN|"],
    "preserve_name_prefixes": ["IT"],
    "include_categories": [],
    "exclude_categories": [],
    "movie_category_ids": [],
    "series_category_ids": [],
    "clean_stale": True,
    "allow_empty_library": False,
    "sample_size": 0,
    "batch_size": 0,
    "jellyfin_url": "",
    "jellyfin_api_key": "",
    "jellyfin_poll_seconds": 15,
    "jellyfin_scan_timeout": 43200,
    "jellyfin_verify_tls": True,
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


def choose_category_ids(label: str, categories: dict[str, str], existing: Any = None) -> list[str]:
    if not categories:
        print(f"No {label.lower()} groups were returned by the provider.")
        return []
    items = list(categories.items())
    selected_before = {str(value) for value in existing} if isinstance(existing, list) else set()
    default_numbers = [str(index) for index, (category_id, _) in enumerate(items, start=1) if category_id in selected_before]
    default = ",".join(default_numbers) if selected_before and default_numbers else "all"

    print(f"\nAvailable {label.lower()} groups:")
    for index, (_, name) in enumerate(items, start=1):
        marker = " *" if str(index) in default_numbers else ""
        print(f"  {index:>3}) {name}{marker}")
    print("Enter all for every group, individual numbers such as 1,4,7, or ranges such as 2-6.")
    answer = prompt(f"{label} groups to import", default).casefold()
    if answer in {"all", "*"}:
        return []

    positions: list[int] = []
    for part in (value.strip() for value in answer.split(",")):
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if len(bounds) != 2 or not all(value.isdigit() for value in bounds):
                raise SyncError(f"invalid {label.lower()} group selection: {part}")
            start, end = (int(value) for value in bounds)
            if start > end:
                raise SyncError(f"invalid descending group range: {part}")
            positions.extend(range(start, end + 1))
        elif part.isdigit():
            positions.append(int(part))
        else:
            raise SyncError(f"invalid {label.lower()} group selection: {part}")
    if not positions or any(position < 1 or position > len(items) for position in positions):
        raise SyncError(f"{label.lower()} group selection must use numbers from 1 to {len(items)}")

    result: list[str] = []
    for position in positions:
        category_id = items[position - 1][0]
        if category_id not in result:
            result.append(category_id)
    return result


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
    existing_content = "both"
    if existing.get("sync_movies") is True and existing.get("sync_series") is False:
        existing_content = "movies"
    elif existing.get("sync_movies") is False and existing.get("sync_series") is True:
        existing_content = "series"
    content = prompt("Content to export: both, movies, or series", existing_content).lower()
    if content not in {"both", "movies", "series"}:
        raise SyncError("content selection must be both, movies, or series")
    if not username or not password:
        raise SyncError("username and password are required")
    jellyfin_url = prompt(
        "Jellyfin server URL (leave blank to skip automatic batching)",
        str(existing.get("jellyfin_url", "")) or None,
    )
    jellyfin_api_key = str(existing.get("jellyfin_api_key", ""))
    if jellyfin_url:
        jellyfin_api_key = prompt(
            "Jellyfin API key" + (" (press Enter to keep the saved key)" if jellyfin_api_key else ""),
            jellyfin_api_key,
            secret=True,
        )
        if not jellyfin_api_key:
            raise SyncError("a Jellyfin API key is required when a Jellyfin server URL is configured")

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
        "batch_size": 0,
        "jellyfin_url": normalize_server_url(jellyfin_url) if jellyfin_url else "",
        "jellyfin_api_key": jellyfin_api_key if jellyfin_url else "",
    })

    print("\nChecking provider login...")
    probe = dict(config)
    probe["server_url"] = normalize_server_url(str(config["server_url"]))
    probe["request_timeout"] = float(config["request_timeout"])
    probe["retries"] = int(config["retries"])
    probe["verify_tls"] = as_bool(config["verify_tls"], "verify_tls")
    client = XtreamClient(probe)
    user = client.authenticate()
    print(f"Login accepted (status: {user.get('status', 'active')}).")

    if config["sync_movies"]:
        movie_categories = category_map(client.api("get_vod_categories"))
        config["movie_category_ids"] = choose_category_ids(
            "Movie", movie_categories, existing.get("movie_category_ids", [])
        )
    if config["sync_series"]:
        series_categories = category_map(client.api("get_series_categories"))
        config["series_category_ids"] = choose_category_ids(
            "TV show", series_categories, existing.get("series_category_ids", [])
        )

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
    if args.batch_size is not None:
        config["batch_size"] = args.batch_size

    for field in ("sync_movies", "sync_series", "category_directories", "normalize_names", "add_provider_ids", "auto_strip_name_tags", "clean_stale", "allow_empty_library", "verify_tls", "jellyfin_verify_tls"):
        config[field] = as_bool(config[field], field)
    if not config["sync_movies"] and not config["sync_series"]:
        raise SyncError("at least one of sync_movies or sync_series must be enabled")
    for field in ("server_url", "username", "password", "output_dir"):
        if not str(config[field]).strip():
            raise SyncError(f"missing required setting: {field}")
    for field in ("include_categories", "exclude_categories", "movie_category_ids", "series_category_ids", "strip_name_prefixes", "preserve_name_prefixes"):
        if not isinstance(config[field], list) or not all(isinstance(item, str) for item in config[field]):
            raise SyncError(f"{field} must be a list of strings")
    try:
        config["request_timeout"] = float(config["request_timeout"])
        config["retries"] = int(config["retries"])
        config["sample_size"] = int(config["sample_size"])
        config["batch_size"] = int(config["batch_size"])
        config["jellyfin_poll_seconds"] = int(config["jellyfin_poll_seconds"])
        config["jellyfin_scan_timeout"] = int(config["jellyfin_scan_timeout"])
    except (TypeError, ValueError) as exc:
        raise SyncError("request_timeout, retries, sample_size, batch_size, and Jellyfin timing settings must be numeric") from exc
    if config["request_timeout"] <= 0 or not 1 <= config["retries"] <= 10:
        raise SyncError("request_timeout must be positive and retries must be between 1 and 10")
    if not 0 <= config["sample_size"] <= 1000:
        raise SyncError("sample_size must be between 0 and 1000")
    if not 0 <= config["batch_size"] <= 10000:
        raise SyncError("batch_size must be between 0 and 10000")
    if config["sample_size"] and config["batch_size"]:
        raise SyncError("sample_size and batch_size cannot both be enabled")
    if not 5 <= config["jellyfin_poll_seconds"] <= 300:
        raise SyncError("jellyfin_poll_seconds must be between 5 and 300")
    if not 60 <= config["jellyfin_scan_timeout"] <= 86400:
        raise SyncError("jellyfin_scan_timeout must be between 60 and 86400")
    config["server_url"] = normalize_server_url(str(config["server_url"]))
    config["jellyfin_url"] = normalize_server_url(str(config["jellyfin_url"])) if str(config["jellyfin_url"]).strip() else ""
    config["jellyfin_api_key"] = str(config["jellyfin_api_key"]).strip()
    if config["jellyfin_api_key"] and not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", config["jellyfin_api_key"]):
        raise SyncError("jellyfin_api_key contains invalid characters")
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


class JellyfinClient:
    """Small Jellyfin API client used only for continuous batch scans."""

    def __init__(self, config: dict[str, Any]):
        self.base_url = config["jellyfin_url"]
        self.api_key = config["jellyfin_api_key"]
        self.timeout = config["request_timeout"]
        self.poll_seconds = config["jellyfin_poll_seconds"]
        self.scan_timeout = config["jellyfin_scan_timeout"]
        self.ssl_context = None
        if self.base_url.startswith("https://") and not config["jellyfin_verify_tls"]:
            LOG.warning("Jellyfin TLS certificate verification is disabled")
            self.ssl_context = ssl._create_unverified_context()

    def request(self, method: str, path: str) -> Any:
        request = Request(
            f"{self.base_url}{path}",
            data=b"" if method == "POST" else None,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": (
                    f'MediaBrowser Client="xtream-strm", Device="Linux", '
                    f'DeviceId="xtream-strm", Version="{VERSION}", Token="{self.api_key}"'
                ),
                "User-Agent": f"xtream-strm/{VERSION}",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8-sig"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise SyncError("Jellyfin rejected the API key; create an administrator API key and update the configuration") from exc
            raise SyncError(f"Jellyfin API request failed with HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SyncError(f"Jellyfin API request failed: {exc}") from exc

    @staticmethod
    def value(item: dict[str, Any], name: str) -> Any:
        for key, value in item.items():
            if str(key).casefold() == name.casefold():
                return value
        return None

    def scan_status(self) -> tuple[str, float | None]:
        tasks = self.request("GET", "/ScheduledTasks")
        if not isinstance(tasks, list):
            raise SyncError("Jellyfin returned an invalid scheduled-task list")
        for task in tasks:
            if not isinstance(task, dict):
                continue
            key = str(self.value(task, "Key") or "").casefold()
            name = str(self.value(task, "Name") or "").casefold()
            if key in {"refreshlibrary", "scanlibrary"} or ("scan" in name and "library" in name):
                state = str(self.value(task, "State") or "Idle")
                progress = self.value(task, "CurrentProgressPercentage")
                try:
                    percentage = float(progress) if progress is not None else None
                except (TypeError, ValueError):
                    percentage = None
                return state, percentage
        raise SyncError("Jellyfin's library scan task was not found")

    def wait_until_idle(self, deadline: float, message: str) -> None:
        last_progress: int | None = None
        while True:
            state, progress = self.scan_status()
            if state.casefold() == "idle":
                return
            if time.monotonic() >= deadline:
                raise SyncError("timed out waiting for Jellyfin's library scan")
            rounded = int(progress) if progress is not None else None
            if rounded is not None and rounded != last_progress:
                LOG.info("%s: %d%%", message, rounded)
                last_progress = rounded
            elif last_progress is None:
                LOG.info("%s", message)
            time.sleep(self.poll_seconds)

    def refresh_and_wait(self) -> None:
        deadline = time.monotonic() + self.scan_timeout
        self.wait_until_idle(deadline, "Waiting for an existing Jellyfin scan to finish")
        LOG.info("Starting Jellyfin library scan")
        self.request("POST", "/Library/Refresh")
        idle_checks = 0
        last_progress: int | None = None
        while True:
            state, progress = self.scan_status()
            if state.casefold() == "idle":
                idle_checks += 1
                if idle_checks >= 2:
                    LOG.info("Jellyfin library scan finished")
                    return
            else:
                idle_checks = 0
                rounded = int(progress) if progress is not None else None
                if rounded is not None and rounded != last_progress:
                    LOG.info("Jellyfin library scan: %d%%", rounded)
                    last_progress = rounded
            if time.monotonic() >= deadline:
                raise SyncError("timed out waiting for Jellyfin's library scan")
            time.sleep(self.poll_seconds)


def category_map(payload: Any) -> dict[str, str]:
    if not isinstance(payload, list):
        raise SyncError("provider returned an invalid category list")
    result = {}
    for item in payload:
        if isinstance(item, dict) and item.get("category_id") is not None:
            result[str(item["category_id"])] = safe_name(item.get("category_name"), "Uncategorized")
    return result


def category_allowed(category_id: Any, name: str, kind: str, config: dict[str, Any]) -> bool:
    selection_field = "movie_category_ids" if kind == "movie" else "series_category_ids"
    selected_ids = {str(value) for value in config[selection_field]}
    normalized = name.casefold()
    include = {item.casefold() for item in config["include_categories"]}
    exclude = {item.casefold() for item in config["exclude_categories"]}
    return (
        (not selected_ids or str(category_id) in selected_ids)
        and (not include or normalized in include)
        and normalized not in exclude
    )


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


def provider_id_suffix(item: dict[str, Any]) -> str | None:
    """Return one validated Jellyfin provider-id suffix, preferring TMDB."""
    normalized = {re.sub(r"[^a-z]", "", str(key).casefold()): value for key, value in item.items()}
    for key in ("tmdbid", "tmdb"):
        value = str(normalized.get(key, "")).strip()
        match = re.search(r"(?<!\d)(\d{1,10})(?!\d)", value)
        if match:
            return f"[tmdbid-{match.group(1)}]"
    for key in ("imdbid", "imdb"):
        value = str(normalized.get(key, "")).strip()
        match = re.search(r"tt\d{5,10}", value, re.IGNORECASE)
        if match:
            return f"[imdbid-{match.group(0).lower()}]"
        numeric = re.fullmatch(r"\d{5,10}", value)
        if numeric:
            return f"[imdbid-tt{numeric.group(0)}]"
    return None


def add_jellyfin_provider_id(title: str, item: dict[str, Any], config: dict[str, Any], fallback: str) -> str:
    if not config["add_provider_ids"] or JELLYFIN_PROVIDER_ID.search(title):
        return safe_name(title, fallback)
    suffix = provider_id_suffix(item)
    if not suffix:
        return safe_name(title, fallback)
    title_limit = 180 - len(suffix.encode("utf-8")) - 1
    base = safe_name(title, fallback, max_bytes=title_limit)
    return safe_name(f"{base} {suffix}", fallback)


def normalize_media_name(value: Any, config: dict[str, Any], fallback: str) -> str:
    name = html.unescape(str(value or ""))
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\u2013", " - ").replace("\u2014", " - ").replace("\u00b7", " ")
    name = re.sub(r"\s+", " ", name).strip()
    if config["normalize_names"]:
        name = BRACKETED_QUALITY.sub(" ", name)
        name = LEADING_QUALITY.sub("", name)
        name = TRAILING_QUALITY.sub("", name)
        prefixes = sorted((prefix for prefix in config["strip_name_prefixes"] if prefix), key=len, reverse=True)
        removed = True
        while removed and name:
            removed = False
            for prefix in prefixes:
                if name.casefold().startswith(prefix.casefold()):
                    name = name[len(prefix):].lstrip(" |:-")
                    removed = True
                    break
        if config["auto_strip_name_tags"]:
            preserved = {prefix.casefold().strip(" [](){}|:-") for prefix in config["preserve_name_prefixes"]}
            removed = True
            while removed and name:
                removed = False
                for pattern in (LEADING_PROVIDER_TAG, LEADING_BRACKETED_TAG):
                    match = pattern.match(name)
                    if match and match.group("tag").casefold() not in preserved:
                        remainder = name[match.end():].lstrip(" |:-")
                        if re.search(r"[A-Za-z0-9]", remainder):
                            name = remainder
                            removed = True
                            break
        name = re.sub(r"\s+", " ", name).strip(" -|•")
    return safe_name(name, fallback)


def canonical_media_title(value: Any, item: dict[str, Any], config: dict[str, Any], fallback: str) -> str:
    title = normalize_media_name(value, config, fallback)
    if not config["normalize_names"]:
        return add_jellyfin_provider_id(title, item, config, fallback)
    metadata_year = movie_year(item)
    bracketed = TRAILING_BRACKETED_YEAR.search(title)
    separated = TRAILING_SEPARATED_YEAR.search(title)
    title_year = bracketed.group(1) if bracketed else (separated.group(1) if separated else None)
    year = metadata_year or title_year
    year_match = bracketed or separated
    if year_match and title[:year_match.start()].strip(" -|([{ "):
        if bracketed or not metadata_year or title_year == metadata_year:
            title = title[:year_match.start()].rstrip(" -|")
            title = TRAILING_QUALITY.sub("", title).rstrip(" -|")
    if year and not re.search(rf"\({re.escape(year)}\)$", title):
        title = f"{title} ({year})"
    return add_jellyfin_provider_id(title, item, config, fallback)


def parse_number(value: Any, fallback: int, specials_are_zero: bool = False) -> int:
    text = str(value or "").strip()
    if specials_are_zero and text.casefold() in {"special", "specials", "extra", "extras"}:
        return 0
    match = re.search(r"\d+", text)
    if not match:
        return fallback
    return max(0, int(match.group(0)))


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


def collect_movies(client: XtreamClient, config: dict[str, Any], batch: BatchState | None = None) -> list[Entry]:
    categories = category_map(client.api("get_vod_categories"))
    streams = client.api("get_vod_streams")
    if not isinstance(streams, list):
        raise SyncError("provider returned an invalid movie list")
    entries = []
    for item in streams:
        if not isinstance(item, dict) or item.get("stream_id") is None:
            continue
        stream_id = str(item["stream_id"])
        if batch and stream_id in batch.movies:
            continue
        category = categories.get(str(item.get("category_id")), "Uncategorized")
        if not category_allowed(item.get("category_id"), category, "movie", config):
            continue
        display = canonical_media_title(item.get("name"), item, config, f"Movie {item['stream_id']}")
        folder = with_category(config["movies_directory"], category, config) / display
        path = folder / f"{display}.strm"
        entries.append(Entry(path, client.stream_url("movie", item["stream_id"], item.get("container_extension"))))
        if batch:
            batch.new_movies.add(stream_id)
            if len(batch.new_movies) >= config["batch_size"]:
                break
        if config["sample_size"] and len(entries) >= config["sample_size"]:
            break
    return unique_entries(entries)


def episode_number(item: dict[str, Any], fallback: int) -> int:
    value = item.get("episode_num", item.get("episode_number", fallback))
    return parse_number(value, fallback)


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
            season = parse_number(episode.get("season", season_key), 0, specials_are_zero=True)
            yield max(0, season), {**episode, "_fallback_number": index}


def collect_series(client: XtreamClient, config: dict[str, Any], batch: BatchState | None = None) -> list[Entry]:
    categories = category_map(client.api("get_series_categories"))
    series = client.api("get_series")
    if not isinstance(series, list):
        raise SyncError("provider returned an invalid series list")
    entries = []
    total = len(series)
    for position, show in enumerate(series, start=1):
        if not isinstance(show, dict) or show.get("series_id") is None:
            continue
        series_id = str(show["series_id"])
        if batch and series_id in batch.series:
            continue
        category = categories.get(str(show.get("category_id")), "Uncategorized")
        if not category_allowed(show.get("category_id"), category, "series", config):
            continue
        detail = client.api("get_series_info", series_id=show["series_id"])
        info = detail.get("info", {}) if isinstance(detail, dict) else {}
        metadata = {**show, **(info if isinstance(info, dict) else {})}
        show_name = canonical_media_title(show.get("name"), metadata, config, f"Series {show['series_id']}")
        episode_show_name = JELLYFIN_PROVIDER_ID.sub("", show_name).strip()
        LOG.info("Reading series %d/%d: %s", position, total, show_name)
        for season, episode in iter_episodes(detail):
            number = episode_number(episode, episode["_fallback_number"])
            episode_title = normalize_media_name(episode.get("title"), config, f"Episode {number}")
            code = f"S{season:02d}E{number:02d}"
            folder = with_category(config["series_directory"], category, config) / show_name / f"Season {season:02d}"
            filename = safe_name(f"{episode_show_name} - {code} - {episode_title}") + ".strm"
            entries.append(Entry(folder / filename, client.stream_url("series", episode["id"], episode.get("container_extension"))))
            if config["sample_size"] and len(entries) >= config["sample_size"]:
                return unique_entries(entries)
        if batch:
            batch.new_series.add(series_id)
            if len(batch.new_series) >= config["batch_size"]:
                break
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


def account_fingerprint(config: dict[str, Any]) -> str:
    identity = f"{config['server_url']}\0{config['username']}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def load_batch_state(config: dict[str, Any], reset: bool = False) -> BatchState:
    account = account_fingerprint(config)
    path: Path = config["output_dir"] / BATCH_STATE_NAME
    if reset or not path.exists():
        return BatchState(account)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("movies"), list) or not isinstance(data.get("series"), list):
            raise ValueError("unsupported state format")
        if data.get("account") != account:
            raise SyncError("batch progress belongs to a different provider account; use --reset-batch to start new progress")
        return BatchState(account, {str(value) for value in data["movies"]}, {str(value) for value in data["series"]})
    except SyncError:
        raise
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SyncError(f"could not read batch progress {path}: {exc}") from exc


def save_batch_state(config: dict[str, Any], state: BatchState) -> None:
    state.movies.update(state.new_movies)
    state.series.update(state.new_series)
    state.new_movies.clear()
    state.new_series.clear()
    payload = {
        "version": 1,
        "account": state.account,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "movies": sorted(state.movies),
        "series": sorted(state.series),
    }
    path: Path = config["output_dir"] / BATCH_STATE_NAME
    atomic_write(path, json.dumps(payload, indent=2) + "\n", config["file_mode"], config["directory_mode"])


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
    partial_mode = config["sample_size"] > 0 or config["batch_size"] > 0
    active_roots = set()
    if config["sync_movies"]:
        active_roots.add(config["movies_directory"].casefold())
    if config["sync_series"]:
        active_roots.add(config["series_directory"].casefold())
    old_managed_files = set() if partial_mode else {
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
    sizing = parser.add_mutually_exclusive_group()
    sizing.add_argument("--sample", dest="sample_size", nargs="?", type=int, const=5, metavar="SIZE", help="process a small sample (default: 5 per selected library)")
    sizing.add_argument("--batch", dest="batch_size", nargs="?", type=int, const=100, metavar="SIZE", help="process the next resumable batch (default: 100 movies or shows)")
    parser.add_argument("--reset-batch", action="store_true", help="restart resumable batch progress for this provider")
    parser.add_argument("--continuous", action="store_true", help="repeat batches and wait for Jellyfin to scan each one")
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
        if args.reset_batch and not config["batch_size"]:
            raise SyncError("--reset-batch must be used together with --batch")
        if args.continuous and not config["batch_size"]:
            raise SyncError("--continuous must be used together with --batch")
        if args.continuous and args.dry_run:
            raise SyncError("--continuous cannot be used with --dry-run")
        if args.continuous and (not config["jellyfin_url"] or not config["jellyfin_api_key"]):
            raise SyncError("--continuous requires jellyfin_url and jellyfin_api_key in the configuration")
        if config["sample_size"]:
            LOG.info("Sample mode: processing at most %d item(s) per selected library; stale cleanup is disabled", config["sample_size"])
        batch = None
        if config["batch_size"]:
            batch = load_batch_state(config, args.reset_batch)
            LOG.info("Batch mode: processing the next %d movie(s) and show(s); stale cleanup is disabled", config["batch_size"])
        client = XtreamClient(config)
        user = client.authenticate()
        LOG.info("Connected (account status: %s)", user.get("status", "active"))
        jellyfin = JellyfinClient(config) if args.continuous else None
        if jellyfin:
            jellyfin.scan_status()
            LOG.info("Connected to Jellyfin; continuous batching is enabled")

        while True:
            entries: list[Entry] = []
            if config["sync_movies"]:
                movies = collect_movies(client, config, batch)
                if not movies and not config["allow_empty_library"] and not batch:
                    raise SyncError("movie catalog is empty; refusing to replace the existing library (set allow_empty_library to true to permit this)")
                entries.extend(movies)
                LOG.info("Found %d movie(s)", len(movies))
            if config["sync_series"]:
                episodes = collect_series(client, config, batch)
                if not episodes and not config["allow_empty_library"] and not batch:
                    raise SyncError("series catalog has no episodes; refusing to replace the existing library (set allow_empty_library to true to permit this)")
                entries.extend(episodes)
                LOG.info("Found %d episode(s)", len(episodes))
            stats = apply_entries(entries, config, args.dry_run)
            new_movie_count = len(batch.new_movies) if batch else 0
            new_series_count = len(batch.new_series) if batch else 0
            if batch and not args.dry_run:
                save_batch_state(config, batch)
                LOG.info("Batch progress: %d movie(s) and %d show(s) completed", len(batch.movies), len(batch.series))
                if config["sync_movies"] and new_movie_count < config["batch_size"]:
                    LOG.info("Movie batching is complete; no additional unprocessed movies were found")
                if config["sync_series"] and new_series_count < config["batch_size"]:
                    LOG.info("Series batching is complete; no additional unprocessed shows were found")
            elif batch:
                LOG.info("Dry run: batch progress was not saved")
            prefix = "Dry run complete" if args.dry_run else "Sync complete"
            LOG.info("%s: %d created, %d updated, %d unchanged, %d removed", prefix, stats.created, stats.updated, stats.unchanged, stats.removed)

            if not jellyfin:
                break
            if new_movie_count + new_series_count == 0:
                LOG.info("Continuous import is complete")
                break
            if entries:
                jellyfin.refresh_and_wait()
            else:
                LOG.info("The batch contained no playable episodes; continuing to the next batch")
        return 0
    except (SyncError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(run())
