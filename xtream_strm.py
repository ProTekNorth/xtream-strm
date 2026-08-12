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
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

VERSION = "2.0.0"
LOG = logging.getLogger("xtream-strm")
MANIFEST_NAME = ".xtream-strm-manifest.json"
BATCH_STATE_NAME = ".xtream-strm-batch.json"
PENDING_STATE_NAME = ".xtream-strm-pending.json"
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
    identity: str = ""
    collection_identity: str = ""
    aliases: tuple[str, ...] = ()


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


class ProgressBar:
    """Small dependency-free terminal progress bar that stays quiet in service logs."""

    def __init__(self, label: str, total: int = 0, enabled: bool | None = None):
        self.label = label
        self.total = max(0, int(total))
        self.current = 0
        self.detail = ""
        self.enabled = sys.stderr.isatty() if enabled is None else enabled
        self._last_length = 0
        self._pulse = 0

    def update(self, current: int | None = None, detail: str = "") -> None:
        if current is not None:
            self.current = max(0, int(current))
        self.detail = safe_name(detail, "", 50) if detail else ""
        if not self.enabled:
            return
        if self.total:
            current_value = min(self.current, self.total)
            ratio = current_value / self.total
            width = 28
            filled = int(width * ratio)
            bar = "#" * filled + "-" * (width - filled)
            status = f"[{bar}] {current_value}/{self.total} {ratio * 100:5.1f}%"
        else:
            spinner = "|/-\\"[self._pulse % 4]
            self._pulse += 1
            status = f"[{spinner}] {self.current:,} bytes"
        suffix = f"  {self.detail}" if self.detail else ""
        line = f"{self.label}: {status}{suffix}"
        padding = " " * max(0, self._last_length - len(line))
        sys.stderr.write("\r" + line + padding)
        sys.stderr.flush()
        self._last_length = len(line)

    def finish(self, detail: str = "") -> None:
        if detail:
            self.detail = detail
        self.update(self.current, self.detail)
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def __enter__(self) -> "ProgressBar":
        self.update(0)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.finish("failed" if exc_type else self.detail)


def threaded_map(function: Any, items: Iterable[Any], workers: int) -> Iterable[Any]:
    """Map with bounded pending work so very large catalogs do not exhaust memory."""
    iterator = iter(items)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="xtream")
    pending: deque[Any] = deque()
    try:
        for _ in range(workers * 2):
            try:
                pending.append(executor.submit(function, next(iterator)))
            except StopIteration:
                break
        while pending:
            yield pending.popleft().result()
            try:
                pending.append(executor.submit(function, next(iterator)))
            except StopIteration:
                pass
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


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
    "providers": [],
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
    "workers": 8,
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


def safe_relative_directory(value: Any, fallback: str) -> str:
    """Validate and normalize a configurable directory beneath the library root."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raw = fallback
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise SyncError("movie and TV folders must be relative to the main library directory")
    path = PurePosixPath(raw)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SyncError("movie and TV folders cannot contain . or .. path components")
    return PurePosixPath(*(safe_name(part, "Media") for part in path.parts)).as_posix()


def media_directories(config: dict[str, Any]) -> tuple[str, str]:
    movies = safe_relative_directory(config["movies_directory"], "Movies")
    series = safe_relative_directory(config["series_directory"], "TV Shows")
    movie_parts = tuple(part.casefold() for part in PurePosixPath(movies).parts)
    series_parts = tuple(part.casefold() for part in PurePosixPath(series).parts)
    shortest = min(len(movie_parts), len(series_parts))
    if movie_parts[:shortest] == series_parts[:shortest]:
        raise SyncError("movie and TV folders must be separate and cannot be nested inside one another")
    return movies, series


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


PROVIDER_FIELDS = {
    "name", "server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"
}


def legacy_provider(config: dict[str, Any]) -> dict[str, Any]:
    host = urlsplit(str(config.get("server_url", ""))).hostname or "Primary"
    return {
        "name": safe_name(config.get("provider_name") or host, "Primary", 80),
        "server_url": str(config.get("server_url", "")),
        "username": str(config.get("username", "")),
        "password": str(config.get("password", "")),
        "movie_category_ids": list(config.get("movie_category_ids", [])),
        "series_category_ids": list(config.get("series_category_ids", [])),
        "verify_tls": config.get("verify_tls", True),
    }


def normalize_providers(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw = config.get("providers")
    if raw in (None, []):
        raw = [legacy_provider(config)]
    if not isinstance(raw, list) or not raw:
        raise SyncError("providers must be a non-empty list")
    providers: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SyncError(f"provider {index} must be an object")
        unknown = sorted(set(item) - PROVIDER_FIELDS)
        if unknown:
            raise SyncError(f"unknown option(s) for provider {index}: {', '.join(unknown)}")
        provider = {
            "name": safe_name(item.get("name"), f"Provider {index}", 80),
            "server_url": normalize_server_url(str(item.get("server_url", ""))),
            "username": str(item.get("username", "")).strip(),
            "password": str(item.get("password", "")),
            "movie_category_ids": item.get("movie_category_ids", []),
            "series_category_ids": item.get("series_category_ids", []),
            "verify_tls": as_bool(item.get("verify_tls", True), f"providers[{index}].verify_tls"),
        }
        if not provider["username"] or not provider["password"]:
            raise SyncError(f"provider {index} requires a username and password")
        for field in ("movie_category_ids", "series_category_ids"):
            if not isinstance(provider[field], list) or not all(isinstance(value, str) for value in provider[field]):
                raise SyncError(f"providers[{index}].{field} must be a list of strings")
        folded = provider["name"].casefold()
        if folded in names:
            raise SyncError(f"provider names must be unique: {provider['name']}")
        names.add(folded)
        providers.append(provider)
    return providers


def provider_config(config: dict[str, Any], provider: dict[str, Any]) -> dict[str, Any]:
    result = dict(config)
    result.update({key: provider[key] for key in (
        "server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"
    )})
    return result


def prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or (default or "")


def prompt_bool(label: str, default: bool) -> bool:
    answer = prompt(label, "yes" if default else "no").casefold()
    if answer in {"y", "yes", "true", "1"}:
        return True
    if answer in {"n", "no", "false", "0"}:
        return False
    raise SyncError(f"{label.lower()} must be yes or no")


def choose_setup_sections() -> set[str]:
    print("What would you like to reconfigure?")
    print("  1) Provider accounts and priority")
    print("  2) Library and media folders")
    print("  3) Content and provider groups")
    print("  4) Jellyfin connection")
    print("  5) Sync behavior")
    print("  6) Everything")
    print("  7) Cancel")
    print("You can choose more than one section, such as 2,3.")
    answer = prompt("Selection", "3").casefold()
    if answer in {"6", "all", "*"}:
        return {"provider", "library", "content", "jellyfin", "behavior"}
    if answer in {"7", "cancel", "q", "quit"}:
        return set()
    mapping = {
        "1": "provider",
        "2": "library",
        "3": "content",
        "4": "jellyfin",
        "5": "behavior",
    }
    sections: set[str] = set()
    for value in (part.strip() for part in answer.split(",")):
        if value not in mapping:
            raise SyncError("configuration selection must use numbers 1 through 7")
        sections.add(mapping[value])
    return sections


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


def prompt_provider(existing: dict[str, Any] | None, index: int) -> dict[str, Any]:
    saved = existing or {}
    name = prompt("Provider name", str(saved.get("name", f"Provider {index}")))
    raw_url = prompt("Provider server or full playlist URL", str(saved.get("server_url", "")) or None)
    if not raw_url:
        raise SyncError("a provider server URL is required")
    server_url, url_username, url_password = connection_from_input(raw_url)
    username = prompt("Username", str(saved.get("username") or url_username or "") or None)
    password_default = str(saved.get("password") or url_password or "")
    password = prompt(
        "Password" + (" (press Enter to keep the saved value)" if password_default else ""),
        password_default,
        secret=True,
    )
    if not username or not password:
        raise SyncError("username and password are required")
    return {
        "name": safe_name(name, f"Provider {index}", 80),
        "server_url": server_url,
        "username": username,
        "password": password,
        "movie_category_ids": list(saved.get("movie_category_ids", [])),
        "series_category_ids": list(saved.get("series_category_ids", [])),
        "verify_tls": saved.get("verify_tls", True),
    }


def manage_provider_credentials(config: dict[str, Any], first_setup: bool) -> None:
    if first_setup:
        providers = [prompt_provider(None, 1)]
        while prompt_bool("Add another provider", False):
            providers.append(prompt_provider(None, len(providers) + 1))
        config["providers"] = providers
        return

    providers = list(config.get("providers") or [legacy_provider(config)])
    while True:
        print("\nConfigured providers (highest priority first):")
        for index, provider in enumerate(providers, start=1):
            print(f"  {index}) {provider['name']} - {provider['server_url']}")
        print("Commands: add, edit N, remove N, move N, or done")
        command = prompt("Provider command", "done").strip().casefold()
        if command in {"done", "d", "q", "quit"}:
            break
        if command in {"add", "a"}:
            providers.append(prompt_provider(None, len(providers) + 1))
            continue
        match = re.fullmatch(r"(edit|remove|move)\s+(\d+)", command)
        if not match:
            raise SyncError("provider command must be add, edit N, remove N, move N, or done")
        action, raw_index = match.groups()
        index = int(raw_index) - 1
        if not 0 <= index < len(providers):
            raise SyncError("provider number is out of range")
        if action == "edit":
            providers[index] = prompt_provider(providers[index], index + 1)
        elif action == "remove":
            if len(providers) == 1:
                raise SyncError("at least one provider is required")
            removed = providers.pop(index)
            print(f"Removed {removed['name']}.")
        else:
            destination = prompt("New priority number", str(index + 1))
            if not destination.isdigit() or not 1 <= int(destination) <= len(providers):
                raise SyncError("new priority must be a configured provider number")
            provider = providers.pop(index)
            providers.insert(int(destination) - 1, provider)
    config["providers"] = providers


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

    config = dict(DEFAULTS)
    config.update(existing)
    if existing:
        try:
            config["providers"] = list(config.get("providers") or [legacy_provider(config)])
            primary = config["providers"][0]
            for field in ("server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"):
                config[field] = primary[field]
        except (IndexError, TypeError):
            raise SyncError("the saved provider list is invalid")

    print("\nXtream STRM guided setup")
    print("Use only a provider and video library you are authorized to access.\n")
    sections = choose_setup_sections() if existing else {"provider", "library", "content", "jellyfin"}
    if not sections:
        print("Configuration was not changed.")
        return

    if "provider" in sections:
        manage_provider_credentials(config, not existing)
        sections.add("content")

    if "library" in sections:
        config["output_dir"] = str(Path(prompt(
            "Library directory", str(config.get("output_dir", "/srv/media/xtream"))
        )).expanduser().resolve())
        config["movies_directory"] = safe_relative_directory(
            prompt("Movies folder within the library", str(config.get("movies_directory", "Movies"))),
            "Movies",
        )
        config["series_directory"] = safe_relative_directory(
            prompt("TV shows folder within the library", str(config.get("series_directory", "TV Shows"))),
            "TV Shows",
        )
        config["movies_directory"], config["series_directory"] = media_directories(config)

    if "content" in sections:
        existing_content = "both"
        if config.get("sync_movies") is True and config.get("sync_series") is False:
            existing_content = "movies"
        elif config.get("sync_movies") is False and config.get("sync_series") is True:
            existing_content = "series"
        content = prompt("Content to export: both, movies, or series", existing_content).lower()
        if content not in {"both", "movies", "series"}:
            raise SyncError("content selection must be both, movies, or series")
        config["sync_movies"] = content in {"both", "movies"}
        config["sync_series"] = content in {"both", "series"}

    if "jellyfin" in sections:
        saved_jellyfin_url = str(config.get("jellyfin_url", ""))
        jellyfin_url = prompt(
            "Jellyfin server URL (enter none to disable)", saved_jellyfin_url or None
        )
        if jellyfin_url.casefold() in {"none", "off", "disable"}:
            jellyfin_url = ""
        jellyfin_api_key = str(config.get("jellyfin_api_key", ""))
        if jellyfin_url:
            jellyfin_api_key = prompt(
                "Jellyfin API key" + (" (press Enter to keep the saved key)" if jellyfin_api_key else ""),
                jellyfin_api_key,
                secret=True,
            )
            if not jellyfin_api_key:
                raise SyncError("a Jellyfin API key is required when a Jellyfin server URL is configured")
        config["jellyfin_url"] = normalize_server_url(jellyfin_url) if jellyfin_url else ""
        config["jellyfin_api_key"] = jellyfin_api_key if jellyfin_url else ""

    if "behavior" in sections:
        config["normalize_names"] = prompt_bool(
            "Normalize movie and show names", as_bool(config["normalize_names"], "normalize_names")
        )
        config["category_directories"] = prompt_bool(
            "Create provider-group directories", as_bool(config["category_directories"], "category_directories")
        )
        config["clean_stale"] = prompt_bool(
            "Remove stale managed STRM files", as_bool(config["clean_stale"], "clean_stale")
        )
        batch_size = prompt("Default batch size (0 means a complete sync)", str(config["batch_size"]))
        try:
            config["batch_size"] = int(batch_size)
        except ValueError as exc:
            raise SyncError("default batch size must be a number") from exc
        if not 0 <= config["batch_size"] <= 10000:
            raise SyncError("default batch size must be between 0 and 10000")
        if config["batch_size"]:
            config["sample_size"] = 0
        workers = prompt("Concurrent workers", str(config["workers"]))
        try:
            config["workers"] = int(workers)
        except ValueError as exc:
            raise SyncError("concurrent workers must be a number") from exc
        if not 1 <= config["workers"] <= 32:
            raise SyncError("concurrent workers must be between 1 and 32")

    if "provider" in sections or "content" in sections:
        providers = config.get("providers") or [legacy_provider(config)]
        for provider in providers:
            print(f"\nChecking provider: {provider['name']}...")
            probe = provider_config(config, provider)
            probe["server_url"] = normalize_server_url(str(provider["server_url"]))
            probe["request_timeout"] = float(config["request_timeout"])
            probe["retries"] = int(config["retries"])
            probe["verify_tls"] = as_bool(provider.get("verify_tls", True), "verify_tls")
            client = XtreamClient(probe)
            user = client.authenticate()
            print(f"Login accepted for {provider['name']} (status: {user.get('status', 'active')}).")

            if "content" in sections and config["sync_movies"]:
                movie_categories = category_map(client.api("get_vod_categories"))
                provider["movie_category_ids"] = choose_category_ids(
                    f"{provider['name']} movie", movie_categories, provider.get("movie_category_ids", [])
                )
            if "content" in sections and config["sync_series"]:
                series_categories = category_map(client.api("get_series_categories"))
                provider["series_category_ids"] = choose_category_ids(
                    f"{provider['name']} TV show", series_categories, provider.get("series_category_ids", [])
                )
        config["providers"] = providers
        primary = providers[0]
        for field in ("server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"):
            config[field] = primary[field]

    config["providers"] = normalize_providers(config)
    primary = config["providers"][0]
    for field in ("server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"):
        config[field] = primary[field]
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
    provider_override = any(environment[key] is not None for key in ("server_url", "username", "password"))
    config.update({key: value for key, value in environment.items() if value is not None})
    for key in ("server_url", "username", "password", "output_dir"):
        value = getattr(args, key, None)
        if value is not None:
            config[key] = value
            if key in {"server_url", "username", "password"}:
                provider_override = True
    if provider_override and config.get("providers"):
        if isinstance(config["providers"], list) and config["providers"] and isinstance(config["providers"][0], dict):
            config["providers"] = [dict(provider) if isinstance(provider, dict) else provider for provider in config["providers"]]
            config["providers"][0].update({
                "server_url": config["server_url"],
                "username": config["username"],
                "password": config["password"],
            })

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
    for field in ("output_dir",):
        if not str(config[field]).strip():
            raise SyncError(f"missing required setting: {field}")
    for field in ("include_categories", "exclude_categories", "movie_category_ids", "series_category_ids", "strip_name_prefixes", "preserve_name_prefixes"):
        if not isinstance(config[field], list) or not all(isinstance(item, str) for item in config[field]):
            raise SyncError(f"{field} must be a list of strings")
    try:
        config["request_timeout"] = float(config["request_timeout"])
        config["retries"] = int(config["retries"])
        config["workers"] = int(config["workers"])
        config["sample_size"] = int(config["sample_size"])
        config["batch_size"] = int(config["batch_size"])
        config["jellyfin_poll_seconds"] = int(config["jellyfin_poll_seconds"])
        config["jellyfin_scan_timeout"] = int(config["jellyfin_scan_timeout"])
    except (TypeError, ValueError) as exc:
        raise SyncError("request_timeout, retries, workers, sample_size, batch_size, and Jellyfin timing settings must be numeric") from exc
    if config["request_timeout"] <= 0 or not 1 <= config["retries"] <= 10:
        raise SyncError("request_timeout must be positive and retries must be between 1 and 10")
    if not 1 <= config["workers"] <= 32:
        raise SyncError("workers must be between 1 and 32")
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
    config["jellyfin_url"] = normalize_server_url(str(config["jellyfin_url"])) if str(config["jellyfin_url"]).strip() else ""
    config["jellyfin_api_key"] = str(config["jellyfin_api_key"]).strip()
    if config["jellyfin_api_key"] and not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", config["jellyfin_api_key"]):
        raise SyncError("jellyfin_api_key contains invalid characters")
    config["output_dir"] = Path(str(config["output_dir"])).expanduser().resolve()
    config["file_mode"] = parse_mode(config["file_mode"], "file_mode")
    config["directory_mode"] = parse_mode(config["directory_mode"], "directory_mode")
    config["movies_directory"], config["series_directory"] = media_directories(config)
    config["providers"] = normalize_providers(config)
    primary = config["providers"][0]
    for field in ("server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"):
        config[field] = primary[field]
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

    def api(self, action: str | None = None, *, show_progress: bool = True, **parameters: Any) -> Any:
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
                    try:
                        total_bytes = int(response.headers.get("Content-Length", "0"))
                    except (TypeError, ValueError):
                        total_bytes = 0
                    labels = {
                        None: "Connecting to provider",
                        "get_vod_categories": "Loading movie groups",
                        "get_vod_streams": "Downloading movie catalog",
                        "get_series_categories": "Loading TV groups",
                        "get_series": "Downloading TV catalog",
                        "get_series_info": "Downloading show details",
                    }
                    progress = ProgressBar(labels.get(action, "Downloading provider data"), total_bytes) if show_progress else None
                    chunks: list[bytes] = []
                    received = 0
                    if progress:
                        progress.update(0)
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        received += len(chunk)
                        if progress:
                            progress.update(received)
                    if progress:
                        progress.finish()
                    raw = b"".join(chunks)
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
        progress_bar = ProgressBar(message, 100)
        progress_bar.update(0)
        while True:
            state, progress = self.scan_status()
            if state.casefold() == "idle":
                progress_bar.finish("ready")
                return
            if time.monotonic() >= deadline:
                raise SyncError("timed out waiting for Jellyfin's library scan")
            rounded = int(progress) if progress is not None else None
            if rounded is not None and rounded != last_progress:
                progress_bar.update(rounded)
                if not progress_bar.enabled:
                    LOG.info("%s: %d%%", message, rounded)
                last_progress = rounded
            elif last_progress is None:
                progress_bar.update(0, "working")
                if not progress_bar.enabled:
                    LOG.info("%s", message)
            time.sleep(self.poll_seconds)

    def refresh_and_wait(self) -> None:
        deadline = time.monotonic() + self.scan_timeout
        self.wait_until_idle(deadline, "Waiting for an existing Jellyfin scan to finish")
        LOG.info("Starting Jellyfin library scan")
        self.request("POST", "/Library/Refresh")
        idle_checks = 0
        last_progress: int | None = None
        progress_bar = ProgressBar("Jellyfin library scan", 100)
        progress_bar.update(0, "starting")
        while True:
            state, progress = self.scan_status()
            if state.casefold() == "idle":
                idle_checks += 1
                if idle_checks >= 2:
                    progress_bar.current = 100
                    progress_bar.finish("finished")
                    LOG.info("Jellyfin library scan finished")
                    return
            else:
                idle_checks = 0
                rounded = int(progress) if progress is not None else None
                if rounded is not None and rounded != last_progress:
                    progress_bar.update(rounded)
                    if not progress_bar.enabled:
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


def media_identity(kind: str, title: str, item: dict[str, Any]) -> str:
    suffix = provider_id_suffix(item)
    if suffix:
        return f"{kind}:{suffix.casefold()}"
    return fallback_media_identity(kind, title)


def fallback_media_identity(kind: str, title: str) -> str:
    normalized = JELLYFIN_PROVIDER_ID.sub("", title).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    return f"{kind}:title:{normalized}"


def merge_provider_entries(provider_entries: Iterable[tuple[str, list[Entry]]]) -> tuple[list[Entry], int]:
    """Merge ordered providers; the first provider wins duplicates and later providers fill gaps."""
    merged: list[Entry] = []
    seen: set[str] = set()
    canonical_show_folders: dict[str, Path] = {}
    duplicates = 0
    for provider_name, entries in provider_entries:
        for entry in entries:
            identity = entry.identity or f"path:{entry.relative_path.as_posix().casefold()}"
            keys = (identity, *entry.aliases)
            if any(key in seen for key in keys):
                duplicates += 1
                LOG.debug("Merged duplicate from %s: %s", provider_name, entry.relative_path)
                continue
            seen.update(keys)
            adjusted = entry
            if entry.collection_identity:
                show_folder = entry.relative_path.parent.parent
                canonical = canonical_show_folders.setdefault(entry.collection_identity, show_folder)
                if show_folder != canonical:
                    filename = entry.relative_path.name
                    marker = re.search(r" - (S\d{2}E\d{2} - .+)$", entry.relative_path.stem, re.IGNORECASE)
                    if marker:
                        prefix = JELLYFIN_PROVIDER_ID.sub("", canonical.name).strip()
                        filename = safe_name(f"{prefix} - {marker.group(1)}") + ".strm"
                    adjusted = Entry(
                        canonical / entry.relative_path.parent.name / filename,
                        entry.stream_url,
                        entry.identity,
                        entry.collection_identity,
                        entry.aliases,
                    )
            merged.append(adjusted)
    return unique_entries(merged), duplicates


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
        result.append(Entry(candidate, entry.stream_url, entry.identity, entry.collection_identity, entry.aliases))
    return result


def collect_movies(
    client: XtreamClient, config: dict[str, Any], batch: BatchState | None = None, batch_prefix: str = ""
) -> list[Entry]:
    categories = category_map(client.api("get_vod_categories"))
    streams = client.api("get_vod_streams")
    if not isinstance(streams, list):
        raise SyncError("provider returned an invalid movie list")
    entries = []
    progress = ProgressBar("Scanning movies", len(streams), enabled=None if streams else False)
    progress.update(0)
    for position, item in enumerate(streams, start=1):
        progress.update(position, str(item.get("name", "")) if isinstance(item, dict) else "")
        if not isinstance(item, dict) or item.get("stream_id") is None:
            continue
        stream_id = str(item["stream_id"])
        batch_id = f"{batch_prefix}{stream_id}"
        if batch and batch_id in batch.movies:
            continue
        category = categories.get(str(item.get("category_id")), "Uncategorized")
        if not category_allowed(item.get("category_id"), category, "movie", config):
            continue
        display = canonical_media_title(item.get("name"), item, config, f"Movie {item['stream_id']}")
        folder = with_category(config["movies_directory"], category, config) / display
        path = folder / f"{display}.strm"
        identity = media_identity("movie", display, item)
        fallback_identity = fallback_media_identity("movie", display)
        entries.append(Entry(
            path,
            client.stream_url("movie", item["stream_id"], item.get("container_extension")),
            identity,
            "",
            (fallback_identity,) if fallback_identity != identity else (),
        ))
        if batch:
            batch.new_movies.add(batch_id)
            if len(batch.new_movies) >= config["batch_size"]:
                break
        if config["sample_size"] and len(entries) >= config["sample_size"]:
            break
    progress.finish(f"{len(entries)} selected")
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


def collect_series(
    client: XtreamClient, config: dict[str, Any], batch: BatchState | None = None, batch_prefix: str = ""
) -> list[Entry]:
    categories = category_map(client.api("get_series_categories"))
    series = client.api("get_series")
    if not isinstance(series, list):
        raise SyncError("provider returned an invalid series list")
    candidates: list[tuple[dict[str, Any], str]] = []
    for show in series:
        if not isinstance(show, dict) or show.get("series_id") is None:
            continue
        series_id = str(show["series_id"])
        batch_id = f"{batch_prefix}{series_id}"
        if batch and batch_id in batch.series:
            continue
        category = categories.get(str(show.get("category_id")), "Uncategorized")
        if not category_allowed(show.get("category_id"), category, "series", config):
            continue
        candidates.append(({**show, "_batch_id": batch_id}, category))
        if batch and len(candidates) >= config["batch_size"]:
            break

    def fetch_show(candidate: tuple[dict[str, Any], str]) -> tuple[str, str, list[Entry]]:
        show, category = candidate
        series_id = str(show["_batch_id"])
        detail = client.api("get_series_info", show_progress=False, series_id=show["series_id"])
        info = detail.get("info", {}) if isinstance(detail, dict) else {}
        metadata = {**show, **(info if isinstance(info, dict) else {})}
        show_name = canonical_media_title(show.get("name"), metadata, config, f"Series {show['series_id']}")
        show_identity = media_identity("series", show_name, metadata)
        fallback_show_identity = fallback_media_identity("series", show_name)
        episode_show_name = JELLYFIN_PROVIDER_ID.sub("", show_name).strip()
        show_entries: list[Entry] = []
        for season, episode in iter_episodes(detail):
            number = episode_number(episode, episode["_fallback_number"])
            episode_title = normalize_media_name(episode.get("title"), config, f"Episode {number}")
            code = f"S{season:02d}E{number:02d}"
            folder = with_category(config["series_directory"], category, config) / show_name / f"Season {season:02d}"
            filename = safe_name(f"{episode_show_name} - {code} - {episode_title}") + ".strm"
            show_entries.append(Entry(
                folder / filename,
                client.stream_url("series", episode["id"], episode.get("container_extension")),
                f"{show_identity}:s{season:03d}e{number:04d}",
                fallback_show_identity,
                ((f"{fallback_show_identity}:s{season:03d}e{number:04d}",)
                 if fallback_show_identity != show_identity else ()),
            ))
        return series_id, show_name, show_entries

    entries: list[Entry] = []
    total = len(candidates)
    progress = ProgressBar("Reading TV shows", total, enabled=None if total else False)
    progress.update(0)
    if config["sample_size"] or config["workers"] == 1 or total < 2:
        results: Iterable[tuple[str, str, list[Entry]]] = map(fetch_show, candidates)
    else:
        results = threaded_map(fetch_show, candidates, min(config["workers"], total))
    for position, (series_id, show_name, show_entries) in enumerate(results, start=1):
        progress.update(position, show_name)
        LOG.debug("Read series %d/%d: %s", position, total, show_name)
        entries.extend(show_entries)
        if batch:
            batch.new_series.add(series_id)
        if config["sample_size"] and len(entries) >= config["sample_size"]:
            entries = entries[:config["sample_size"]]
            break
    progress.finish(f"{len(entries)} episodes selected")
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
    providers = config.get("providers") or [legacy_provider(config)]
    identity = "\0".join(
        f"{provider['server_url']}\0{provider['username']}" for provider in providers
    ).encode("utf-8")
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


def pending_signature(config: dict[str, Any]) -> str:
    fields = (
        "providers", "sync_movies", "sync_series", "movies_directory", "series_directory",
        "category_directories", "normalize_names", "add_provider_ids", "auto_strip_name_tags",
        "strip_name_prefixes", "preserve_name_prefixes", "include_categories", "exclude_categories",
        "movie_category_ids", "series_category_ids", "clean_stale",
    )
    payload = {field: config[field] for field in fields}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def legacy_pending_signature(config: dict[str, Any]) -> str:
    """Signature used by 1.8.x, retained so an in-progress single-provider write can resume after upgrading."""
    fields = (
        "server_url", "username", "sync_movies", "sync_series", "movies_directory", "series_directory",
        "category_directories", "normalize_names", "add_provider_ids", "auto_strip_name_tags",
        "strip_name_prefixes", "preserve_name_prefixes", "include_categories", "exclude_categories",
        "movie_category_ids", "series_category_ids", "clean_stale",
    )
    payload = {field: config[field] for field in fields}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def save_pending_entries(config: dict[str, Any], entries: list[Entry]) -> None:
    payload = {
        "version": 1,
        "signature": pending_signature(config),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": [
            {"path": entry.relative_path.as_posix(), "url": entry.stream_url}
            for entry in entries
        ],
    }
    path: Path = config["output_dir"] / PENDING_STATE_NAME
    LOG.info("Saving a write checkpoint for %d STRM file(s)", len(entries))
    atomic_write(path, json.dumps(payload, separators=(",", ":")) + "\n", config["file_mode"], config["directory_mode"])


def load_pending_entries(config: dict[str, Any]) -> list[Entry] | None:
    path: Path = config["output_dir"] / PENDING_STATE_NAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        signatures = {pending_signature(config)}
        if len(config.get("providers", [])) == 1:
            signatures.add(legacy_pending_signature(config))
        if payload.get("version") != 1 or payload.get("signature") not in signatures:
            LOG.warning("Ignoring an old write checkpoint because the provider or folder settings changed")
            return None
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("entries is not a list")
        entries: list[Entry] = []
        for item in raw_entries:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("url"), str):
                raise ValueError("invalid checkpoint entry")
            relative = PurePosixPath(item["path"])
            if relative.is_absolute() or ".." in relative.parts or relative.suffix.casefold() != ".strm":
                raise ValueError("unsafe checkpoint path")
            entries.append(Entry(Path(*relative.parts), item["url"]))
        LOG.info("Resuming %d STRM file(s) from the saved checkpoint; provider catalogs will not be reread", len(entries))
        return entries
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SyncError(f"could not read write checkpoint {path}: {exc}") from exc


def clear_pending_entries(config: dict[str, Any]) -> None:
    path: Path = config["output_dir"] / PENDING_STATE_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SyncError(f"could not remove completed write checkpoint {path}: {exc}") from exc


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
    active_roots: list[tuple[str, ...]] = []
    if config["sync_movies"]:
        active_roots.append(tuple(part.casefold() for part in PurePosixPath(config["movies_directory"]).parts))
    if config["sync_series"]:
        active_roots.append(tuple(part.casefold() for part in PurePosixPath(config["series_directory"]).parts))
    old_managed_files = set() if partial_mode else {
        relative
        for relative in old_files
        if any(
            tuple(part.casefold() for part in PurePosixPath(relative).parts[:len(root)]) == root
            for root in active_roots
        )
    }
    preserved_files = old_files - old_managed_files
    stats = Stats()
    write_label = "Checking STRM files" if dry_run else "Writing STRM files"
    write_progress = ProgressBar(write_label, len(entries), enabled=None if entries else False)
    write_progress.update(0)

    def apply_entry(entry: Entry) -> tuple[str, str]:
        target = output / entry.relative_path
        content = entry.stream_url + "\n"
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else None
        except PermissionError:
            LOG.warning("Replacing unreadable managed STRM file: %s", entry.relative_path)
            if not dry_run:
                try:
                    atomic_write(target, content, config["file_mode"], config["directory_mode"])
                except OSError as exc:
                    raise SyncError(
                        f"could not replace unreadable {target}; check the NFS directory ownership: {exc}"
                    ) from exc
            return "updated", entry.relative_path.name
        except OSError as exc:
            raise SyncError(f"could not read {target}: {exc}") from exc
        if existing == content:
            result = "unchanged"
        elif existing is None:
            result = "created"
            LOG.debug("Create %s", entry.relative_path)
            if not dry_run:
                atomic_write(target, content, config["file_mode"], config["directory_mode"])
        else:
            result = "updated"
            LOG.debug("Update %s", entry.relative_path)
            if not dry_run:
                atomic_write(target, content, config["file_mode"], config["directory_mode"])
        return result, entry.relative_path.name

    if config["workers"] == 1 or len(entries) < 2:
        write_results: Iterable[tuple[str, str]] = map(apply_entry, entries)
    else:
        write_results = threaded_map(apply_entry, entries, min(config["workers"], len(entries)))
    for position, (result, filename) in enumerate(write_results, start=1):
        setattr(stats, result, getattr(stats, result) + 1)
        write_progress.update(position, filename)
    write_progress.finish()

    if config["clean_stale"]:
        stale_files = sorted(old_managed_files - new_files)
        stale_progress = ProgressBar("Cleaning stale files", len(stale_files), enabled=None if stale_files else False)
        stale_progress.update(0)
        for position, relative in enumerate(stale_files, start=1):
            stale_progress.update(position, PurePosixPath(relative).name)
            target = safe_manifest_target(output, relative)
            if target and target.is_file():
                stats.removed += 1
                LOG.debug("Remove stale %s", relative)
                if not dry_run:
                    target.unlink()
                    remove_empty_parents(target, output)
        stale_progress.finish()

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
        provider_clients: list[tuple[str, dict[str, Any], XtreamClient]] = []
        for provider in config["providers"]:
            current_config = provider_config(config, provider)
            client = XtreamClient(current_config)
            user = client.authenticate()
            LOG.info("Connected to %s (account status: %s)", provider["name"], user.get("status", "active"))
            provider_clients.append((provider["name"], current_config, client))
        jellyfin = JellyfinClient(config) if args.continuous else None
        if jellyfin:
            jellyfin.scan_status()
            LOG.info("Connected to Jellyfin; continuous batching is enabled")

        while True:
            checkpoint_enabled = not batch and not config["sample_size"] and not args.dry_run
            saved_entries = load_pending_entries(config) if checkpoint_enabled else None
            entries: list[Entry] = saved_entries if saved_entries is not None else []
            if saved_entries is None:
                provider_results: list[tuple[str, list[Entry]]] = []
                for provider_index, (provider_name, current_config, client) in enumerate(provider_clients):
                    current_entries: list[Entry] = []
                    batch_prefix = "" if provider_index == 0 else f"p{provider_index}:"
                    if config["sync_movies"]:
                        movies = collect_movies(client, current_config, batch, batch_prefix)
                        current_entries.extend(movies)
                        LOG.info("%s supplied %d selected movie(s)", provider_name, len(movies))
                    if config["sync_series"]:
                        episodes = collect_series(client, current_config, batch, batch_prefix)
                        current_entries.extend(episodes)
                        LOG.info("%s supplied %d selected episode(s)", provider_name, len(episodes))
                    provider_results.append((provider_name, current_entries))
                entries, duplicate_count = merge_provider_entries(provider_results)
                if config["sample_size"]:
                    movies = [entry for entry in entries if entry.identity.startswith("movie:")][:config["sample_size"]]
                    episodes = [entry for entry in entries if entry.identity.startswith("series:")][:config["sample_size"]]
                    entries = movies + episodes
                movie_count = sum(entry.identity.startswith("movie:") for entry in entries)
                episode_count = sum(entry.identity.startswith("series:") for entry in entries)
                if config["sync_movies"] and not movie_count and not config["allow_empty_library"] and not batch:
                    raise SyncError("combined movie catalog is empty; refusing to replace the existing library")
                if config["sync_series"] and not episode_count and not config["allow_empty_library"] and not batch:
                    raise SyncError("combined series catalog has no episodes; refusing to replace the existing library")
                LOG.info(
                    "Combined library: %d movie(s), %d episode(s), %d duplicate source item(s) merged",
                    movie_count, episode_count, duplicate_count,
                )
                if checkpoint_enabled:
                    save_pending_entries(config, entries)
            stats = apply_entries(entries, config, args.dry_run)
            if checkpoint_enabled:
                clear_pending_entries(config)
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
