#!/usr/bin/env python3
"""Authenticated local web dashboard for Xtream STRM."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import xtream_strm as exporter

try:
    import pwd
except ImportError:  # pragma: no cover - permits development tests on Windows
    pwd = None  # type: ignore[assignment]

LOG = logging.getLogger("xtream-strm-web")
CONFIG_PATH = Path("/etc/xtream-strm/config.json")
WEB_CONFIG_PATH = Path("/etc/xtream-strm/web.json")
STATE_DIR = Path("/var/lib/xtream-strm")
STATUS_PATH = STATE_DIR / "status.json"
JOB_PATH = STATE_DIR / "web-job.json"
PROGRAM_PATH = Path("/opt/xtream-strm/xtream_strm.py")
SERVICE_UNIT = "xtream-strm.service"
TIMER_UNIT = "xtream-strm.timer"
PASSWORD_ITERATIONS = 310_000
MAX_BODY = 1_000_000
SESSION_AGE = 12 * 60 * 60
CONFIG_LOCK = threading.Lock()
MANIFEST_LOCK = threading.Lock()
MANIFEST_CACHE: dict[str, Any] = {"key": None, "value": None}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def make_password_config(password: str, host: str = "0.0.0.0", port: int = 8787) -> dict[str, Any]:
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return {
        "host": host,
        "port": port,
        "password_salt": base64.b64encode(salt).decode(),
        "password_hash": base64.b64encode(digest).decode(),
        "password_iterations": PASSWORD_ITERATIONS,
    }


def verify_password(password: str, config: dict[str, Any]) -> bool:
    try:
        salt = base64.b64decode(config["password_salt"], validate=True)
        expected = base64.b64decode(config["password_hash"], validate=True)
        iterations = int(config.get("password_iterations", PASSWORD_ITERATIONS))
    except (KeyError, TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def run_command(arguments: list[str], timeout: int = 20, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode:
        message = (result.stderr or result.stdout or f"command exited with {result.returncode}").strip()
        raise exporter.SyncError(message)
    return result


def unit_properties(unit: str) -> dict[str, str]:
    result = run_command([
        "systemctl", "show", unit, "--no-pager",
        "--property=LoadState", "--property=ActiveState", "--property=SubState",
        "--property=Result", "--property=ExecMainStatus",
    ])
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def unit_running(unit: str) -> bool:
    return unit_properties(unit).get("ActiveState") in {"active", "activating", "reloading"}


def current_unit() -> str:
    try:
        saved = json.loads(JOB_PATH.read_text(encoding="utf-8"))
        unit = str(saved.get("unit", ""))
        if re.fullmatch(r"xtream-strm(?:-batch|-sample)?-[0-9-]+\.service", unit):
            return unit
    except (OSError, json.JSONDecodeError):
        pass
    return SERVICE_UNIT


def read_status() -> dict[str, Any]:
    try:
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"state": "idle", "stage": "Ready", "current": 0, "total": 0, "detail": ""}


def existing_library_discovery() -> dict[str, Any]:
    """Count the managed library already on disk, cached until its manifest changes."""
    try:
        config = raw_config()
        output = Path(str(config.get("output_dir", ""))).expanduser().resolve()
        movies_directory = exporter.safe_relative_directory(config.get("movies_directory"), "Movies")
        series_directory = exporter.safe_relative_directory(config.get("series_directory"), "TV Shows")
        manifest = output / exporter.MANIFEST_NAME
        stat = manifest.stat()
        cache_key = (
            str(manifest), stat.st_mtime_ns, stat.st_size,
            movies_directory.casefold(), series_directory.casefold(),
        )
    except (OSError, exporter.SyncError, ValueError):
        return {"movies": 0, "shows": 0, "episodes": 0, "recent_found": []}
    with MANIFEST_LOCK:
        if MANIFEST_CACHE["key"] == cache_key:
            return dict(MANIFEST_CACHE["value"])
        try:
            files = exporter.read_manifest(output)
        except exporter.SyncError:
            return {"movies": 0, "shows": 0, "episodes": 0, "recent_found": []}
        movie_root = tuple(part.casefold() for part in exporter.PurePosixPath(movies_directory).parts)
        series_root = tuple(part.casefold() for part in exporter.PurePosixPath(series_directory).parts)
        movie_count = 0
        episode_count = 0
        shows: set[tuple[str, ...]] = set()
        samples: list[dict[str, str]] = []
        sample_keys: set[tuple[str, str]] = set()
        for relative in sorted(files):
            path = exporter.PurePosixPath(relative)
            folded = tuple(part.casefold() for part in path.parts)
            if folded[:len(movie_root)] == movie_root:
                movie_count += 1
                title = path.parent.name if path.parent != exporter.PurePosixPath(".") else path.stem
                sample_key = ("movie", title.casefold())
                if len(samples) < 12 and sample_key not in sample_keys:
                    samples.append({"kind": "movie", "title": title})
                    sample_keys.add(sample_key)
            elif folded[:len(series_root)] == series_root:
                episode_count += 1
                season_index = next(
                    (index for index in range(len(path.parts) - 2, len(series_root) - 1, -1)
                     if re.fullmatch(r"season\s+\d+", path.parts[index], re.IGNORECASE)),
                    len(path.parts) - 2,
                )
                show_index = max(len(series_root), season_index - 1)
                show_key = folded[:show_index + 1]
                shows.add(show_key)
                title = path.parts[show_index]
                sample_key = ("show", title.casefold())
                if len(samples) < 12 and sample_key not in sample_keys:
                    samples.append({"kind": "show", "title": title})
                    sample_keys.add(sample_key)
        value = {
            "movies": movie_count,
            "shows": len(shows),
            "episodes": episode_count,
            "recent_found": samples,
        }
        MANIFEST_CACHE.update({"key": cache_key, "value": value})
        return dict(value)


def dashboard_status(include_logs: bool = False) -> dict[str, Any]:
    unit = current_unit()
    properties = unit_properties(unit)
    service_properties = properties if unit == SERVICE_UNIT else unit_properties(SERVICE_UNIT)
    running = properties.get("ActiveState") in {"active", "activating", "reloading"}
    if not running and service_properties.get("ActiveState") in {"active", "activating", "reloading"}:
        unit, properties, running = SERVICE_UNIT, service_properties, True
    timer_enabled = run_command(["systemctl", "is-enabled", TIMER_UNIT]).returncode == 0
    timer_active = run_command(["systemctl", "is-active", TIMER_UNIT]).returncode == 0
    status = read_status()
    status["existing_library"] = existing_library_discovery()
    if running:
        status["state"] = "running"
    result: dict[str, Any] = {
        "running": running,
        "unit": unit,
        "active_state": properties.get("ActiveState", "unknown"),
        "result": properties.get("Result", ""),
        "timer_enabled": timer_enabled,
        "timer_active": timer_active,
        "progress": status,
    }
    if include_logs:
        logs = run_command(["journalctl", "-u", unit, "-n", "100", "--no-pager", "-o", "cat"], timeout=20)
        result["logs"] = logs.stdout[-30_000:]
    return result


def raw_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise exporter.SyncError(f"Configuration not found: {CONFIG_PATH}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise exporter.SyncError(f"Could not read configuration: {exc}") from exc
    if not isinstance(value, dict):
        raise exporter.SyncError("Configuration must be a JSON object")
    return value


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    handle, name = tempfile.mkstemp(prefix="xtream-web-config-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        args = exporter.build_parser().parse_args([])
        return exporter.load_config(path, args)
    finally:
        path.unlink(missing_ok=True)


def sanitized_config() -> dict[str, Any]:
    current = raw_config()
    validated = validate_config(current)
    providers = []
    for index, provider in enumerate(validated["providers"]):
        providers.append({
            "original_index": index,
            "name": provider["name"],
            "server_url": provider["server_url"],
            "username": provider["username"],
            "password": "",
            "password_present": bool(provider["password"]),
            "movie_category_ids": provider["movie_category_ids"],
            "series_category_ids": provider["series_category_ids"],
            "verify_tls": provider["verify_tls"],
        })
    settings = {}
    public_fields = (
        "output_dir", "sync_movies", "sync_series", "movies_directory", "series_directory",
        "category_directories", "normalize_names", "add_provider_ids", "auto_strip_name_tags",
        "strip_name_prefixes", "preserve_name_prefixes", "clean_stale", "allow_empty_library",
        "jellyfin_url", "jellyfin_poll_seconds", "jellyfin_scan_timeout", "jellyfin_verify_tls",
        "request_timeout", "retries", "workers",
    )
    for field in public_fields:
        value = validated[field]
        settings[field] = str(value) if isinstance(value, Path) else value
    settings["jellyfin_api_key"] = ""
    settings["jellyfin_api_key_present"] = bool(validated["jellyfin_api_key"])
    return {"providers": providers, "settings": settings}


def save_config(submitted: dict[str, Any]) -> None:
    if dashboard_status()["running"]:
        raise exporter.SyncError("Stop the active sync before changing its configuration")
    with CONFIG_LOCK:
        existing = raw_config()
        old_validated = validate_config(existing)
        incoming_providers = submitted.get("providers")
        incoming_settings = submitted.get("settings")
        if not isinstance(incoming_providers, list) or not incoming_providers:
            raise exporter.SyncError("Add at least one provider")
        if not isinstance(incoming_settings, dict):
            raise exporter.SyncError("Settings are missing")
        providers = []
        for number, item in enumerate(incoming_providers, start=1):
            if not isinstance(item, dict):
                raise exporter.SyncError(f"Provider {number} is invalid")
            password = str(item.get("password", ""))
            if not password:
                try:
                    original_index = int(item.get("original_index"))
                    password = str(old_validated["providers"][original_index]["password"])
                except (TypeError, ValueError, IndexError, KeyError):
                    raise exporter.SyncError(f"Provider {number} needs a password")
            providers.append({
                "name": str(item.get("name", "")),
                "server_url": str(item.get("server_url", "")),
                "username": str(item.get("username", "")),
                "password": password,
                "movie_category_ids": item.get("movie_category_ids", []),
                "series_category_ids": item.get("series_category_ids", []),
                "verify_tls": bool(item.get("verify_tls", True)),
            })
        editable = {
            "output_dir", "sync_movies", "sync_series", "movies_directory", "series_directory",
            "category_directories", "normalize_names", "add_provider_ids", "auto_strip_name_tags",
            "strip_name_prefixes", "preserve_name_prefixes", "clean_stale", "allow_empty_library",
            "jellyfin_url", "jellyfin_poll_seconds", "jellyfin_scan_timeout", "jellyfin_verify_tls",
            "request_timeout", "retries", "workers",
        }
        updated = dict(existing)
        updated["providers"] = providers
        for field in editable:
            if field in incoming_settings:
                updated[field] = incoming_settings[field]
        api_key = str(incoming_settings.get("jellyfin_api_key", ""))
        if api_key:
            updated["jellyfin_api_key"] = api_key
        primary = providers[0]
        for field in ("server_url", "username", "password", "movie_category_ids", "series_category_ids", "verify_tls"):
            updated[field] = primary[field]
        validate_config(updated)
        atomic_json(CONFIG_PATH, updated)
        try:
            if pwd is None:
                raise KeyError("pwd unavailable")
            account = pwd.getpwnam("xtream-strm")
            os.chown(CONFIG_PATH, account.pw_uid, 0)
        except (KeyError, PermissionError):
            pass


def provider_groups(index: int) -> dict[str, Any]:
    config = validate_config(raw_config())
    try:
        provider = config["providers"][index]
    except IndexError as exc:
        raise exporter.SyncError("Provider does not exist") from exc
    current = exporter.provider_config(config, provider)
    client = exporter.XtreamClient(current)
    client.authenticate()
    movies = exporter.category_map(client.api("get_vod_categories", show_progress=False))
    series = exporter.category_map(client.api("get_series_categories", show_progress=False))
    return {
        "movies": [{"id": key, "name": value} for key, value in movies.items()],
        "series": [{"id": key, "name": value} for key, value in series.items()],
    }


def reset_status(stage: str) -> None:
    atomic_json(STATUS_PATH, {
        "state": "starting", "stage": stage, "current": 0, "total": 0,
        "detail": "Waiting for the sync process", "updated_at": utc_now(),
    }, 0o640)
    try:
        if pwd is None:
            raise KeyError("pwd unavailable")
        account = pwd.getpwnam("xtream-strm")
        os.chown(STATUS_PATH, account.pw_uid, account.pw_gid)
    except (KeyError, PermissionError):
        pass


def start_job(kind: str, batch_size: int = 100) -> str:
    status = dashboard_status()
    if status["running"]:
        raise exporter.SyncError("A sync is already running")
    if kind == "full":
        reset_status("Starting complete sync")
        run_command(["systemctl", "start", "--no-block", SERVICE_UNIT], check=True)
        atomic_json(JOB_PATH, {"unit": SERVICE_UNIT, "kind": kind, "started_at": utc_now()})
        return SERVICE_UNIT
    if kind not in {"batch", "sample"}:
        raise exporter.SyncError("Unknown sync type")
    if not 1 <= batch_size <= 10_000:
        raise exporter.SyncError("Batch size must be between 1 and 10,000")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unit = f"xtream-strm-{kind}-{stamp}.service"
    arguments = [str(PROGRAM_PATH), "--config", str(CONFIG_PATH)]
    if kind == "batch":
        arguments += ["--batch", str(batch_size)]
        stage = "Starting resumable batch"
    else:
        arguments += ["--sample", str(min(batch_size, 1000))]
        stage = "Starting sample sync"
    reset_status(stage)
    command = [
        "systemd-run", f"--unit={unit[:-8]}", "--collect", "--property=Type=oneshot",
        "--property=User=xtream-strm", "--property=Group=media",
        f"--setenv=XTREAM_STATUS_FILE={STATUS_PATH}", *arguments,
    ]
    run_command(command, check=True)
    atomic_json(JOB_PATH, {"unit": unit, "kind": kind, "started_at": utc_now()})
    return unit


class Dashboard:
    def __init__(self, web_config: dict[str, Any]):
        self.web_config = web_config
        self.sessions: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def new_session(self) -> tuple[str, str]:
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[session_id] = {"csrf": csrf, "expires": time.time() + SESSION_AGE}
        return session_id, csrf

    def session(self, session_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self.lock:
            expired = [key for key, value in self.sessions.items() if value["expires"] < now]
            for key in expired:
                self.sessions.pop(key, None)
            value = self.sessions.get(session_id)
            if value:
                value["expires"] = now + SESSION_AGE
            return value

    def login_allowed(self, address: str) -> bool:
        cutoff = time.time() - 300
        with self.lock:
            recent = [item for item in self.failures.get(address, []) if item > cutoff]
            self.failures[address] = recent
            return len(recent) < 8

    def login_failed(self, address: str) -> None:
        with self.lock:
            self.failures.setdefault(address, []).append(time.time())


class Handler(BaseHTTPRequestHandler):
    server_version = "XtreamSTRMWeb/2.1"
    dashboard: Dashboard

    def log_message(self, format_string: str, *args: Any) -> None:
        LOG.info("%s - %s", self.client_address[0], format_string % args)

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def json_response(self, value: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_bytes(json.dumps(value).encode(), "application/json; charset=utf-8", status, headers)

    def body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise exporter.SyncError("Invalid request size") from exc
        if length < 0 or length > MAX_BODY:
            raise exporter.SyncError("Request is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise exporter.SyncError("Request must contain valid JSON") from exc
        if not isinstance(value, dict):
            raise exporter.SyncError("Request must be an object")
        return value

    def cookie_session(self) -> tuple[str, dict[str, Any] | None]:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session_id = cookie.get("xtream_session").value if cookie.get("xtream_session") else ""
        return session_id, self.dashboard.session(session_id)

    def require_auth(self, write: bool = False) -> dict[str, Any] | None:
        _, session = self.cookie_session()
        if not session:
            self.json_response({"error": "Login required"}, HTTPStatus.UNAUTHORIZED)
            return None
        if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            self.json_response({"error": "Security token expired; refresh the page"}, HTTPStatus.FORBIDDEN)
            return None
        return session

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.json_response({"ok": True, "version": exporter.VERSION})
            return
        if path in {"/", "/index.html"}:
            self.send_bytes(APP_HTML.encode(), "text/html; charset=utf-8")
            return
        session = self.require_auth()
        if not session:
            return
        try:
            if path == "/api/bootstrap":
                self.json_response({"csrf": session["csrf"], "config": sanitized_config(), "status": dashboard_status(True), "version": exporter.VERSION})
            elif path == "/api/status":
                self.json_response(dashboard_status(True))
            elif path == "/api/groups":
                query = parse_qs(urlsplit(self.path).query)
                self.json_response(provider_groups(int(query.get("provider", ["0"])[0])))
            else:
                self.json_response({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (exporter.SyncError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/api/login":
                address = self.client_address[0]
                if not self.dashboard.login_allowed(address):
                    self.json_response({"error": "Too many attempts; wait five minutes"}, HTTPStatus.TOO_MANY_REQUESTS)
                    return
                password = str(self.body().get("password", ""))
                if not verify_password(password, self.dashboard.web_config):
                    self.dashboard.login_failed(address)
                    self.json_response({"error": "Incorrect password"}, HTTPStatus.UNAUTHORIZED)
                    return
                session_id, csrf = self.dashboard.new_session()
                cookie = f"xtream_session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_AGE}"
                self.json_response({"ok": True, "csrf": csrf}, headers={"Set-Cookie": cookie})
                return
            session = self.require_auth(write=True)
            if not session:
                return
            body = self.body()
            if path == "/api/logout":
                session_id, _ = self.cookie_session()
                with self.dashboard.lock:
                    self.dashboard.sessions.pop(session_id, None)
                self.json_response({"ok": True}, headers={"Set-Cookie": "xtream_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
            elif path == "/api/config":
                save_config(body)
                self.json_response({"ok": True, "config": sanitized_config()})
            elif path == "/api/sync/start":
                kind = str(body.get("kind", "full"))
                batch_size = int(body.get("batch_size", 100))
                unit = start_job(kind, batch_size)
                self.json_response({"ok": True, "unit": unit})
            elif path == "/api/sync/stop":
                active = dashboard_status()
                if not active["running"]:
                    raise exporter.SyncError("No sync is currently running")
                unit = str(active["unit"])
                run_command(["systemctl", "stop", unit], check=True)
                self.json_response({"ok": True})
            elif path == "/api/timer":
                enabled = bool(body.get("enabled"))
                command = ["systemctl", "enable" if enabled else "disable", "--now", TIMER_UNIT]
                run_command(command, check=True)
                self.json_response({"ok": True})
            else:
                self.json_response({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (exporter.SyncError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


APP_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Xtream STRM</title><style>
:root{color-scheme:dark;--bg:#08111d;--panel:#111f31;--line:#243b55;--muted:#91a7bd;--text:#edf6ff;--blue:#4aa8ff;--cyan:#5de5d0;--red:#ff6d7a;--shadow:0 22px 55px #02060d99}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#17304b 0,transparent 34%),var(--bg);color:var(--text);font:15px system-ui,-apple-system,Segoe UI,sans-serif}button,input,select{font:inherit}.hidden{display:none!important}.login{min-height:100vh;display:grid;place-items:center;padding:24px}.login-card{width:min(420px,100%);background:#0e1b2bd9;border:1px solid var(--line);padding:34px;border-radius:22px;box-shadow:var(--shadow)}.mark{width:48px;height:48px;border-radius:14px;background:linear-gradient(135deg,var(--blue),var(--cyan));display:grid;place-items:center;color:#06111e;font-weight:900;font-size:22px}.login h1{margin:18px 0 6px}.muted{color:var(--muted)}label{display:block;color:#bdd0e2;font-size:13px;margin:18px 0 7px}input,select{width:100%;border:1px solid var(--line);background:#091523;color:var(--text);border-radius:10px;padding:11px 12px;outline:none}input:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 3px #4aa8ff22}button{border:0;border-radius:10px;padding:10px 14px;background:#203751;color:var(--text);cursor:pointer;font-weight:650}button:hover{filter:brightness(1.12)}button.primary{background:linear-gradient(135deg,#1683e5,#28bca8);color:#fff}button.danger{background:#502630;color:#ffdce0}button:disabled{opacity:.5;cursor:not-allowed}.wide{width:100%;margin-top:18px}.shell{display:grid;grid-template-columns:240px 1fr;min-height:100vh}.sidebar{background:#0b1726;border-right:1px solid var(--line);padding:24px 18px;position:sticky;top:0;height:100vh}.brand{display:flex;align-items:center;gap:12px;padding:0 8px 26px}.brand b{font-size:18px}.nav button{display:block;width:100%;text-align:left;background:transparent;margin:4px 0;padding:11px 12px;color:var(--muted)}.nav button.active{background:#17304a;color:#fff}.sidebar-foot{position:absolute;bottom:22px;left:18px;right:18px}.main{padding:32px clamp(20px,4vw,58px);max-width:1400px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}.top h1{margin:0;font-size:28px}.pill{padding:7px 11px;border-radius:99px;background:#183148;color:#bde5ff}.pill.running{background:#12433b;color:#9df5df}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:18px}.card{background:#0f1e2fd9;border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 8px 30px #02070d44}.span8{grid-column:span 8}.span4{grid-column:span 4}.span12{grid-column:span 12}.card h2{margin:0 0 4px;font-size:17px}.metric{font-size:27px;font-weight:750;margin:13px 0 4px}.media-metric{font-size:34px;margin-top:8px}.media-kind{display:flex;justify-content:space-between;align-items:center}.media-icon{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;background:#193853;color:#8dd7ff;font-weight:800}.recent-found{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px}.found-item{display:flex;align-items:center;gap:10px;background:#0a1725;border:1px solid #1b334a;border-radius:10px;padding:9px 11px;min-width:0}.found-badge{font-size:10px;font-weight:800;letter-spacing:.05em;color:#80ddcf;background:#123a38;border-radius:6px;padding:4px 6px}.found-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.progress{height:12px;border-radius:99px;background:#07111d;overflow:hidden;margin:22px 0 10px}.progress div{height:100%;width:0;background:linear-gradient(90deg,var(--blue),var(--cyan));transition:width .35s}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}.logs{background:#07111b;border:1px solid #1b3248;border-radius:12px;padding:14px;height:300px;overflow:auto;white-space:pre-wrap;color:#b9cee0;font:12px ui-monospace,SFMono-Regular,Consolas,monospace}.provider{display:grid;grid-template-columns:36px 1fr auto;gap:12px;align-items:start;margin-top:14px}.handle{color:#6f8ca7;padding-top:14px}.provider-body{border:1px solid var(--line);border-radius:13px;padding:16px;background:#0a1725}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 14px}.check{display:flex;align-items:center;gap:9px;margin:13px 0;color:#bdd0e2}.check input{width:auto}.provider-actions{display:flex;gap:8px;margin-top:14px}.groups{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}.group-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;max-height:240px;overflow:auto;margin-top:10px}.group-list label{display:flex;gap:8px;align-items:center;margin:0;padding:7px;background:#0d1c2b;border-radius:8px}.group-list input{width:auto}.section-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}.toast{position:fixed;right:24px;bottom:24px;padding:12px 16px;border-radius:10px;background:#214764;box-shadow:var(--shadow);z-index:5}.toast.error{background:#672c39}.error-text{color:#ff9aa4;margin-top:10px}@media(max-width:850px){.shell{display:block}.sidebar{position:static;height:auto;display:flex;align-items:center;gap:12px;overflow:auto;padding:12px}.brand{padding:0}.brand span,.sidebar-foot{display:none}.nav{display:flex}.nav button{white-space:nowrap}.main{padding:22px}.span8,.span4{grid-column:span 12}.fields,.group-list,.recent-found{grid-template-columns:1fr}.top{align-items:flex-start;gap:10px}}
</style></head><body>
<div id="login" class="login"><form id="loginForm" class="login-card"><div class="mark">X</div><h1>Xtream STRM</h1><p class="muted">Sign in to manage your private media library.</p><label>Dashboard password</label><input id="loginPassword" type="password" autocomplete="current-password" required><button class="primary wide">Sign in</button><div id="loginError" class="error-text"></div></form></div>
<div id="app" class="shell hidden"><aside class="sidebar"><div class="brand"><div class="mark">X</div><span><b>Xtream STRM</b><br><small class="muted" id="version"></small></span></div><nav class="nav"><button data-page="dashboard" class="active">Overview</button><button data-page="providers">Providers & groups</button><button data-page="library">Library settings</button><button data-page="activity">Activity</button></nav><div class="sidebar-foot"><button id="logout">Sign out</button></div></aside><main class="main"><div class="top"><h1 id="pageTitle">Overview</h1><span id="statePill" class="pill">Ready</span></div>
<section id="page-dashboard" class="page"><div class="grid"><div class="card span8"><h2>Library sync</h2><div id="stage" class="metric">Ready</div><div id="detail" class="muted">No sync is running</div><div class="progress"><div id="progressBar"></div></div><div id="progressText" class="muted">0%</div><div class="actions"><button class="primary" data-sync="full">Sync complete library</button><button data-sync="batch">Run next batch</button><button data-sync="sample">Create test sample</button><button id="stopSync" class="danger">Stop</button></div></div><div class="card span4"><h2>Schedule</h2><div id="timerMetric" class="metric">Checking…</div><p class="muted">Automatic full refresh every six hours.</p><label class="check"><input id="timerToggle" type="checkbox"> Enable scheduled syncs</label><label>Batch or sample size</label><input id="batchSize" type="number" min="1" max="10000" value="100"></div><div class="card span4"><div class="media-kind"><h2>Movies found</h2><span class="media-icon">M</span></div><div id="moviesFound" class="metric media-metric">0</div><div id="moviesFoundNote" class="muted">Waiting for a catalog</div></div><div class="card span4"><div class="media-kind"><h2>TV shows found</h2><span class="media-icon">TV</span></div><div id="showsFound" class="metric media-metric">0</div><div id="showsFoundNote" class="muted">Waiting for a catalog</div></div><div class="card span4"><div class="media-kind"><h2>Episodes found</h2><span class="media-icon">E</span></div><div id="episodesFound" class="metric media-metric">0</div><div id="episodesFoundNote" class="muted">Waiting for show details</div></div><div class="card span12"><h2>Recently found</h2><p class="muted">The latest selected titles from the provider catalogs.</p><div id="recentFound" class="recent-found"><span class="muted">Titles will appear here during a sync.</span></div></div><div class="card span12"><h2>Recent activity</h2><pre id="miniLogs" class="logs"></pre></div></div></section>
<section id="page-providers" class="page hidden"><div class="card"><h2>Provider priority and groups</h2><p class="muted">The first provider wins duplicates. Move providers to change priority; later providers fill missing titles and episodes.</p><div id="providerList"></div><div class="actions"><button id="addProvider">Add provider</button></div><div class="section-actions"><button class="primary saveConfig">Save changes</button></div></div></section>
<section id="page-library" class="page hidden"><div class="card"><h2>Folders and sync behavior</h2><div class="fields"><div><label>Main library directory</label><input data-setting="output_dir"></div><div></div><div><label>Movies folder</label><input data-setting="movies_directory"></div><div><label>TV shows folder</label><input data-setting="series_directory"></div><div><label>Worker threads</label><input data-setting="workers" type="number" min="1" max="32"></div><div><label>Request timeout (seconds)</label><input data-setting="request_timeout" type="number" min="1"></div><div><label>Jellyfin server URL</label><input data-setting="jellyfin_url" placeholder="Optional"></div><div><label>Jellyfin API key</label><input data-setting="jellyfin_api_key" type="password" placeholder="Leave blank to keep existing key"></div></div><div class="fields"><label class="check"><input data-setting="sync_movies" type="checkbox"> Sync movies</label><label class="check"><input data-setting="sync_series" type="checkbox"> Sync TV shows</label><label class="check"><input data-setting="category_directories" type="checkbox"> Keep provider group folders</label><label class="check"><input data-setting="clean_stale" type="checkbox"> Remove stale managed files</label><label class="check"><input data-setting="normalize_names" type="checkbox"> Normalize media names</label><label class="check"><input data-setting="add_provider_ids" type="checkbox"> Add TMDB/IMDb IDs when supplied</label><label class="check"><input data-setting="auto_strip_name_tags" type="checkbox"> Remove short provider tags</label><label class="check"><input data-setting="jellyfin_verify_tls" type="checkbox"> Verify Jellyfin TLS</label></div><div class="section-actions"><button class="primary saveConfig">Save changes</button></div></div></section>
<section id="page-activity" class="page hidden"><div class="card"><h2>Live service log</h2><p class="muted" id="unitName"></p><pre id="fullLogs" class="logs" style="height:65vh"></pre></div></section>
</main></div><div id="toast" class="toast hidden"></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];let csrf='',model=null,statusTimer=null;
async function api(path,options={}){options.headers={...(options.headers||{}),'Content-Type':'application/json'};if(csrf)options.headers['X-CSRF-Token']=csrf;const r=await fetch(path,options);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(data.error||`Request failed (${r.status})`);return data}
function toast(message,error=false){const e=$('#toast');e.textContent=message;e.className='toast'+(error?' error':'');setTimeout(()=>e.classList.add('hidden'),3500)}
async function boot(){try{const data=await api('/api/bootstrap');csrf=data.csrf;model=data.config;$('#version').textContent='v'+data.version;$('#login').classList.add('hidden');$('#app').classList.remove('hidden');renderConfig();renderStatus(data.status);clearInterval(statusTimer);statusTimer=setInterval(refreshStatus,2000)}catch(e){if(!String(e).includes('Login required'))toast(e.message,true)}}
$('#loginForm').onsubmit=async e=>{e.preventDefault();$('#loginError').textContent='';try{const d=await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#loginPassword').value})});csrf=d.csrf;$('#loginPassword').value='';boot()}catch(x){$('#loginError').textContent=x.message}};
$$('.nav button').forEach(b=>b.onclick=()=>{$$('.nav button').forEach(x=>x.classList.toggle('active',x===b));$$('.page').forEach(x=>x.classList.add('hidden'));$('#page-'+b.dataset.page).classList.remove('hidden');$('#pageTitle').textContent=b.textContent});
function renderStatus(s){const p=s.progress||{},total=Number(p.total||0),current=Number(p.current||0),percent=total?Math.min(100,current/total*100):0,m=p.metrics||{},existing=p.existing_library||{},libraryReady=Number(m.library_movies||0)+Number(m.library_shows||0)+Number(m.library_episodes||0)>0,discoveryActive=s.running&&(Number(m.source_movies||0)+Number(m.source_shows||0)+Number(m.source_episodes||0)>0);$('#stage').textContent=p.stage||'Ready';$('#detail').textContent=p.detail||(s.running?'Sync is running':'No sync is running');$('#progressBar').style.width=percent+'%';$('#progressText').textContent=total?`${current.toLocaleString()} of ${total.toLocaleString()} — ${percent.toFixed(1)}%`:(s.running?'Working…':p.state==='complete'?'Complete':'Ready');$('#statePill').textContent=s.running?'Syncing':p.state==='failed'?'Needs attention':'Ready';$('#statePill').classList.toggle('running',s.running);$('#stopSync').disabled=!s.running;$('#timerToggle').checked=s.timer_enabled;$('#timerMetric').textContent=s.timer_enabled?'Enabled':'Disabled';renderDiscovery(m,p.recent_found||[],existing,libraryReady,discoveryActive);$('#miniLogs').textContent=s.logs||'No activity yet.';$('#fullLogs').textContent=s.logs||'No activity yet.';$('#unitName').textContent=s.unit||'';for(const e of [$('#miniLogs'),$('#fullLogs')])e.scrollTop=e.scrollHeight}
function renderDiscovery(m,recent,existing,libraryReady,discoveryActive){const useExisting=!libraryReady&&!discoveryActive,movieCount=useExisting?Number(existing.movies||0):(libraryReady?Number(m.library_movies||0):Number(m.source_movies||0)),showCount=useExisting?Number(existing.shows||0):(libraryReady?Number(m.library_shows||0):Number(m.source_shows||0)),episodeCount=useExisting?Number(existing.episodes||0):(libraryReady?Number(m.library_episodes||0):Number(m.source_episodes||0));$('#moviesFound').textContent=movieCount.toLocaleString();$('#showsFound').textContent=showCount.toLocaleString();$('#episodesFound').textContent=episodeCount.toLocaleString();if(useExisting){$('#moviesFoundNote').textContent='Already in the managed library';$('#showsFoundNote').textContent='Already in the managed library';$('#episodesFoundNote').textContent='Already in the managed library'}else{$('#moviesFoundNote').textContent=libraryReady?`${Number(m.source_movies||0).toLocaleString()} provider matches; duplicates merged`:'Selected across providers';$('#showsFoundNote').textContent=libraryReady?`${Number(m.source_shows||0).toLocaleString()} provider matches; duplicates merged`:'Selected shows read so far';$('#episodesFoundNote').textContent=libraryReady?`${Number(m.duplicates_merged||0).toLocaleString()} duplicate source items merged`:'Playable episodes read so far'}const shown=recent.length?recent:(existing.recent_found||[]),root=$('#recentFound');root.innerHTML='';if(!shown.length){const empty=document.createElement('span');empty.className='muted';empty.textContent='No managed titles are recorded yet.';root.appendChild(empty);return}shown.slice(0,12).forEach(item=>{const row=document.createElement('div');row.className='found-item';const badge=document.createElement('span');badge.className='found-badge';badge.textContent=item.kind==='movie'?'MOVIE':'SHOW';const title=document.createElement('span');title.className='found-title';title.textContent=item.title||'Unknown';row.append(badge,title);root.appendChild(row)})}
async function refreshStatus(){try{renderStatus(await api('/api/status'))}catch(e){if(!String(e).includes('Login required'))console.warn(e)}}
function renderConfig(){$$('[data-setting]').forEach(e=>{const value=model.settings[e.dataset.setting];if(e.type==='checkbox')e.checked=!!value;else e.value=value??''});renderProviders()}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderProviders(){const root=$('#providerList');root.innerHTML='';model.providers.forEach((p,i)=>{const box=document.createElement('div');box.className='provider';box.innerHTML=`<div class="handle">${i+1}</div><div class="provider-body"><div class="fields"><div><label>Name</label><input data-p="name" value="${esc(p.name)}"></div><div><label>Server URL</label><input data-p="server_url" value="${esc(p.server_url)}"></div><div><label>Username</label><input data-p="username" value="${esc(p.username)}"></div><div><label>Password</label><input data-p="password" type="password" placeholder="${p.password_present?'Leave blank to keep it':'Required'}"></div></div><label class="check"><input data-p="verify_tls" type="checkbox" ${p.verify_tls?'checked':''}> Verify provider TLS certificate</label><div class="provider-actions"><button data-act="up">Move up</button><button data-act="down">Move down</button><button data-act="groups">Load groups</button><button class="danger" data-act="remove">Remove</button></div><div class="groups hidden"><b>Selected groups</b><p class="muted">No boxes checked means all groups.</p><div class="group-list movie-groups"></div><div class="group-list series-groups"></div></div></div>`;root.appendChild(box);box.querySelectorAll('[data-p]').forEach(e=>e.oninput=()=>{p[e.dataset.p]=e.type==='checkbox'?e.checked:e.value});box.querySelector('[data-act=up]').onclick=()=>moveProvider(i,-1);box.querySelector('[data-act=down]').onclick=()=>moveProvider(i,1);box.querySelector('[data-act=remove]').onclick=()=>{if(model.providers.length===1)return toast('At least one provider is required',true);model.providers.splice(i,1);renderProviders()};box.querySelector('[data-act=groups]').onclick=()=>loadGroups(i,box)})}
function moveProvider(i,d){const n=i+d;if(n<0||n>=model.providers.length)return;[model.providers[i],model.providers[n]]=[model.providers[n],model.providers[i]];renderProviders()}
async function loadGroups(i,box){const button=box.querySelector('[data-act=groups]');button.disabled=true;button.textContent='Loading…';try{await save(false);const d=await api('/api/groups?provider='+i);box=$$('#providerList .provider')[i];const p=model.providers[i];drawGroups(box.querySelector('.movie-groups'),'Movies',d.movies,p.movie_category_ids,v=>p.movie_category_ids=v);drawGroups(box.querySelector('.series-groups'),'TV shows',d.series,p.series_category_ids,v=>p.series_category_ids=v);box.querySelector('.groups').classList.remove('hidden')}catch(e){toast(e.message,true)}finally{const live=$$('#providerList [data-act=groups]')[i];if(live){live.disabled=false;live.textContent='Load groups'}}}
function drawGroups(root,title,items,selected,setter){root.innerHTML=`<div style="grid-column:1/-1"><b>${title}</b></div>`;const chosen=new Set(selected||[]);items.forEach(item=>{const l=document.createElement('label');l.innerHTML=`<input type="checkbox" value="${esc(item.id)}" ${chosen.has(item.id)?'checked':''}> <span>${esc(item.name)}</span>`;l.querySelector('input').onchange=()=>setter([...root.querySelectorAll('input:checked')].map(x=>x.value));root.appendChild(l)})}
function collectSettings(){$$('[data-setting]').forEach(e=>{model.settings[e.dataset.setting]=e.type==='checkbox'?e.checked:(e.type==='number'?Number(e.value):e.value)})}
async function save(show=true){collectSettings();const d=await api('/api/config',{method:'POST',body:JSON.stringify(model)});model=d.config;renderConfig();if(show)toast('Configuration saved')}
$$('.saveConfig').forEach(b=>b.onclick=()=>save().catch(e=>toast(e.message,true)));
$('#addProvider').onclick=()=>{model.providers.push({original_index:null,name:'New Provider',server_url:'',username:'',password:'',password_present:false,movie_category_ids:[],series_category_ids:[],verify_tls:true});renderProviders()};
$$('[data-sync]').forEach(b=>b.onclick=async()=>{try{const size=Number($('#batchSize').value||100);await api('/api/sync/start',{method:'POST',body:JSON.stringify({kind:b.dataset.sync,batch_size:size})});toast('Sync started');refreshStatus()}catch(e){toast(e.message,true)}});
$('#stopSync').onclick=async()=>{if(!confirm('Stop the active sync? Its checkpoint will be kept.'))return;try{await api('/api/sync/stop',{method:'POST',body:'{}'});toast('Sync stopped');refreshStatus()}catch(e){toast(e.message,true)}};
$('#timerToggle').onchange=async e=>{try{await api('/api/timer',{method:'POST',body:JSON.stringify({enabled:e.target.checked})});toast(e.target.checked?'Schedule enabled':'Schedule disabled');refreshStatus()}catch(x){toast(x.message,true);e.target.checked=!e.target.checked}};
$('#logout').onclick=async()=>{try{await api('/api/logout',{method:'POST',body:'{}'})}catch{}location.reload()};boot();
</script></body></html>'''


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Xtream STRM local web dashboard")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--web-config", type=Path, default=WEB_CONFIG_PATH)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--init-password", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    return parser.parse_args()


def main() -> int:
    global CONFIG_PATH, WEB_CONFIG_PATH
    args = parse_arguments()
    CONFIG_PATH, WEB_CONFIG_PATH = args.config, args.web_config
    if args.init_password or args.reset_password:
        if args.init_password and WEB_CONFIG_PATH.exists():
            return 0
        password = secrets.token_urlsafe(12)
        host = args.host or "0.0.0.0"
        port = args.port or 8787
        atomic_json(WEB_CONFIG_PATH, make_password_config(password, host, port))
        print(password)
        return 0
    try:
        web_config = json.loads(WEB_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(web_config, dict):
            raise ValueError("web configuration is not an object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: Could not read {WEB_CONFIG_PATH}: {exc}", file=sys.stderr)
        print("Run this program with --reset-password first.", file=sys.stderr)
        return 1
    host = args.host or str(web_config.get("host", "0.0.0.0"))
    port = args.port or int(web_config.get("port", 8787))
    if not 1 <= port <= 65535:
        print("ERROR: dashboard port must be between 1 and 65535", file=sys.stderr)
        return 1
    Handler.dashboard = Dashboard(web_config)
    server = ThreadingHTTPServer((host, port), Handler)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    LOG.info("Dashboard listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
