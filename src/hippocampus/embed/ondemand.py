"""On-demand local BGE-M3 supervisor for the compose `bge` service.

This module deliberately starts the existing Docker Compose service instead of
loading BGE in the host Python process. The base package stays light, while a
fresh repo checkout can still cold-start semantic search only when needed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

DEFAULT_URL = "http://127.0.0.1:8086"
DEFAULT_PORT = 8086
DEFAULT_IDLE_SECONDS = 300
DEFAULT_STARTUP_TIMEOUT_SECONDS = 900


class OnDemandError(RuntimeError):
    """Raised when the on-demand backend cannot be started or verified."""


@dataclass(frozen=True)
class Endpoint:
    url: str
    token: str


def state_dir() -> Path:
    base = os.environ.get("HIPPOCAMPUS_BGE_ONDEMAND_STATE_DIR")
    if base:
        root = Path(base)
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        home = os.environ.get("HOME")
        if xdg:
            root = Path(xdg) / "hippocampus" / "embed-ondemand"
        elif home:
            root = Path(home) / ".local" / "state" / "hippocampus" / "embed-ondemand"
        else:
            raise OnDemandError(
                "bge-ondemand requires HOME, XDG_STATE_HOME, or "
                "HIPPOCAMPUS_BGE_ONDEMAND_STATE_DIR"
            )
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def token_path() -> Path:
    return state_dir() / "token"


def state_path() -> Path:
    return state_dir() / "state.json"


def read_or_create_token() -> str:
    path = token_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return token


def idle_seconds() -> int:
    raw = os.environ.get("BGE_ONDEMAND_IDLE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_IDLE_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise OnDemandError("BGE_ONDEMAND_IDLE_SECONDS must be an integer") from exc
    if value < 30:
        raise OnDemandError("BGE_ONDEMAND_IDLE_SECONDS must be >= 30")
    return value


def startup_timeout_seconds() -> int:
    raw = os.environ.get("BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_STARTUP_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise OnDemandError("BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS must be an integer") from exc
    if value < 10:
        raise OnDemandError("BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS must be >= 10")
    return value


def endpoint_url() -> str:
    explicit = os.environ.get("BGE_ONDEMAND_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("BGE_ONDEMAND_PORT", "").strip()
    if port:
        try:
            value = int(port)
        except ValueError as exc:
            raise OnDemandError("BGE_ONDEMAND_PORT must be an integer") from exc
        if value <= 0 or value > 65535:
            raise OnDemandError("BGE_ONDEMAND_PORT must be in 1..65535")
        return f"http://127.0.0.1:{value}"
    return DEFAULT_URL


def compose_dir() -> Path:
    return Path(os.environ.get("HIPPOCAMPUS_COMPOSE_DIR", os.getcwd())).resolve()


def config_hash(*, url: str, idle: int, cwd: Path) -> str:
    token = os.environ.get("BGE_EMBED_TOKEN") or read_or_create_token()
    payload = {
        "url": url,
        "idle_seconds": idle,
        "compose_dir": str(cwd),
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


@contextmanager
def _lock() -> Iterator[None]:
    path = state_dir() / "lock"
    with open(path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield


def _write_state(**values: object) -> None:
    payload = {"updated_at": time.time(), **values}
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(state_path())


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "cold"}
    except json.JSONDecodeError:
        return {"state": "failed", "last_error": "state.json is not valid JSON"}


def _request_json(url: str, path: str, *, token: str | None,
                  timeout: float) -> tuple[int, dict]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{url}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _ready(url: str, token: str, *, timeout: float = 2.0) -> bool:
    try:
        status, body = _request_json(url, "/ready", token=token, timeout=timeout)
    except Exception:
        return False
    return status == 200 and bool(body.get("ok")) and bool(body.get("model_loaded"))


def _health(url: str, *, timeout: float = 1.0) -> bool:
    try:
        status, body = _request_json(url, "/health", token=None, timeout=timeout)
    except Exception:
        return False
    return status == 200 and bool(body.get("ok"))


def _compose_up(cwd: Path, *, token: str, idle: int, url: str) -> None:
    if shutil.which("docker") is None:
        raise OnDemandError(
            "docker not found on PATH; install Docker or use --embed none / "
            "remote bge-http"
        )
    compose_file = cwd / "compose.yaml"
    if not compose_file.exists():
        raise OnDemandError(
            f"compose.yaml not found in {cwd}; set HIPPOCAMPUS_COMPOSE_DIR "
            "or run from the hippocampus repo"
        )
    env = os.environ.copy()
    env["BGE_EMBED_TOKEN"] = token
    env["BGE_ONDEMAND_IDLE_SECONDS"] = str(idle)
    env["BGE_RESTART_POLICY"] = "no"
    env["BGE_ONDEMAND_PORT"] = url_port(url)
    env.setdefault("PG_PASSWORD", "unused-for-bge-ondemand")
    cmd = ["docker", "compose", "--profile", "bge", "up", "-d", "bge"]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = detail[-1] if detail else f"docker compose exited {proc.returncode}"
        raise OnDemandError(f"could not start compose bge service: {msg}")


def _can_auto_port_retry() -> bool:
    return not os.environ.get("BGE_ONDEMAND_URL") and not os.environ.get("BGE_ONDEMAND_PORT")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ensure_endpoint() -> Endpoint:
    """Start the local BGE compose service if needed, then return a ready URL."""
    url = endpoint_url()
    token = os.environ.get("BGE_EMBED_TOKEN") or read_or_create_token()
    idle = idle_seconds()
    cwd = compose_dir()
    cfg = config_hash(url=url, idle=idle, cwd=cwd)

    with _lock():
        # Reconcile before reusing an already-hot service. A manually started
        # bge-http container may be ready but lack the on-demand idle/restart
        # settings; `compose up -d bge` is idempotent and updates that config.
        _write_state(state="starting", url=url, idle_seconds=idle,
                     compose_dir=str(cwd), config_hash=cfg,
                     started_at=time.time())
        try:
            _compose_up(cwd, token=token, idle=idle, url=url)
        except OnDemandError as exc:
            if "address already in use" not in str(exc) or not _can_auto_port_retry():
                _write_state(state="failed", url=url, idle_seconds=idle,
                             compose_dir=str(cwd), config_hash=cfg,
                             last_error=str(exc))
                raise
            port = _free_loopback_port()
            url = f"http://127.0.0.1:{port}"
            cfg = config_hash(url=url, idle=idle, cwd=cwd)
            _write_state(state="starting", url=url, idle_seconds=idle,
                         compose_dir=str(cwd), config_hash=cfg,
                         started_at=time.time(),
                         last_error="default port busy; retrying alternate port")
            _compose_up(cwd, token=token, idle=idle, url=url)

        if _ready(url, token):
            _write_state(state="hot", url=url, idle_seconds=idle,
                         compose_dir=str(cwd), config_hash=cfg,
                         last_health_at=time.time())
            return Endpoint(url=url, token=token)

        deadline = time.monotonic() + startup_timeout_seconds()
        while time.monotonic() < deadline:
            if _ready(url, token):
                _write_state(state="hot", url=url, idle_seconds=idle,
                             compose_dir=str(cwd), config_hash=cfg,
                             last_health_at=time.time())
                return Endpoint(url=url, token=token)
            state = "loading" if _health(url) else "starting"
            _write_state(state=state, url=url, idle_seconds=idle,
                         compose_dir=str(cwd), config_hash=cfg,
                         last_health_at=time.time())
            time.sleep(1)

        _write_state(state="failed", url=url, idle_seconds=idle,
                     compose_dir=str(cwd), config_hash=cfg,
                     last_error="startup timed out")
        raise OnDemandError(
            "bge-ondemand startup timed out; first start may still be "
            "downloading BGE-M3. Check `docker compose logs bge`, retry, or "
            "increase BGE_ONDEMAND_STARTUP_TIMEOUT_SECONDS."
        )


def url_port(url: str) -> str:
    from urllib.parse import urlsplit  # noqa: PLC0415

    parsed = urlsplit(url)
    if parsed.port is None:
        return str(DEFAULT_PORT)
    return str(parsed.port)


def passive_status(*, token: str | None = None) -> dict:
    """Return status without starting the backend and without calling /embed."""
    state = read_state()
    configured_url = endpoint_url()
    state_url = str(state.get("url") or "").strip()
    if _can_auto_port_retry() and state_url:
        url = state_url
    else:
        url = configured_url
    if token and _ready(url, token, timeout=1.0):
        return {**state, "state": "hot", "url": url, "verified": True}
    if _health(url, timeout=1.0):
        return {**state, "state": "running", "url": url, "verified": False}
    if state.get("state") in {"starting", "loading", "hot", "running"}:
        return {"state": "cold", "url": url, "last_state": state.get("state")}
    return {**state, "state": state.get("state", "cold"), "url": url}


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "Endpoint",
    "OnDemandError",
    "ensure_endpoint",
    "passive_status",
    "read_or_create_token",
    "state_dir",
]
