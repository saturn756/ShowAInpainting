"""Typed deployment configuration for the Logic service.

The TOML file contains deployment behavior and paths. Activation/session
secrets and the GPU API key remain environment-only so the public service
package can be checked into a repository safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def _nested(data: dict[str, Any], section: str, key: str, default: Any) -> Any:
    value = data.get(section, {})
    if not isinstance(value, dict):
        return default
    return value.get(key, default)


def _env_or(name: str, value: Any, *, cast: type | None = None) -> Any:
    raw = os.environ.get(name)
    if raw is None:
        return value
    if cast is bool:
        return raw.lower() not in {"0", "false", "no", "off"}
    if cast is int:
        return int(raw)
    if cast is float:
        return float(raw)
    return raw


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int


@dataclass(frozen=True)
class AuthSettings:
    activation_code: str
    session_secret: str
    cookie_name: str
    session_ttl_seconds: int
    activation_max_attempts: int
    activation_window_seconds: int


@dataclass(frozen=True)
class OssSettings:
    enabled: bool
    config_path: Path
    issued_key_ttl_seconds: int


@dataclass(frozen=True)
class GpuSettings:
    url: str
    api_key: str
    relay_upload_timeout_seconds: float
    relay_result_timeout_seconds: float
    public_key_timeout_seconds: float
    public_key_cache_ttl_seconds: int
    generate_timeout_seconds: float
    retry_max: int
    retry_delay_seconds: float


@dataclass(frozen=True)
class RelaySettings:
    upload_max_bytes: int
    result_ttl_seconds: int


@dataclass(frozen=True)
class QueueSettings:
    task_ttl_seconds: int
    max_tasks_per_user: int
    estimated_seconds_per_task: float


@dataclass(frozen=True)
class StaticSettings:
    cache_dir: Path
    demo_dir: Path


@dataclass(frozen=True)
class Settings:
    config_path: Path | None
    server: ServerSettings
    auth: AuthSettings
    oss: OssSettings
    gpu: GpuSettings
    relay: RelaySettings
    queue: QueueSettings
    static: StaticSettings


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load TOML, then apply environment overrides and validate secrets."""
    project_root = Path(__file__).resolve().parent.parent
    selected = config_path or os.environ.get("LOGIC_CONFIG_PATH")
    path = Path(selected).expanduser() if selected else None
    data: dict[str, Any] = {}
    if path:
        if not path.exists():
            raise RuntimeError(f"Logic config file does not exist: {path}")
        with path.open("rb") as handle:
            data = tomllib.load(handle)

    activation_code = os.environ.get("GRADIO_ACTIVATION_CODE", "").strip()
    if len(activation_code) != 5:
        raise RuntimeError("GRADIO_ACTIVATION_CODE must contain exactly 5 characters")

    session_secret = os.environ.get("GRADIO_SESSION_SECRET", "").strip()
    if len(session_secret) < 32:
        raise RuntimeError("GRADIO_SESSION_SECRET must contain at least 32 characters")

    gpu_api_key = os.environ.get("GPU_SERVICE_API_KEY", "").strip()
    if not gpu_api_key:
        raise RuntimeError("GPU_SERVICE_API_KEY must be set")

    server = ServerSettings(
        host=_env_or("LOGIC_SERVICE_HOST", _nested(data, "server", "host", "127.0.0.1")),
        port=_env_or("LOGIC_SERVICE_PORT", _nested(data, "server", "port", 8000), cast=int),
    )

    auth = AuthSettings(
        activation_code=activation_code,
        session_secret=session_secret,
        cookie_name=str(_env_or(
            "LOGIC_SESSION_COOKIE_NAME",
            _nested(data, "auth", "cookie_name", "anomaly_generation_access"),
        )),
        session_ttl_seconds=_env_or(
            "LOGIC_SESSION_TTL_SECONDS",
            _nested(data, "auth", "session_ttl_seconds", 12 * 60 * 60),
            cast=int,
        ),
        activation_max_attempts=_env_or(
            "LOGIC_ACTIVATION_MAX_ATTEMPTS",
            _nested(data, "auth", "activation_max_attempts", 5),
            cast=int,
        ),
        activation_window_seconds=_env_or(
            "LOGIC_ACTIVATION_WINDOW_SECONDS",
            _nested(data, "auth", "activation_window_seconds", 15 * 60),
            cast=int,
        ),
    )

    oss = OssSettings(
        enabled=_env_or(
            "DIRECT_OSS_UPLOAD_ENABLED",
            _nested(data, "oss", "enabled", True),
            cast=bool,
        ),
        config_path=Path(_env_or(
            "OSS_CONFIG_PATH",
            _nested(data, "oss", "config_path", str(project_root / "oss.json")),
        )).expanduser(),
        issued_key_ttl_seconds=_env_or(
            "LOGIC_OSS_ISSUED_KEY_TTL_SECONDS",
            _nested(data, "oss", "issued_key_ttl_seconds", 15 * 60),
            cast=int,
        ),
    )

    gpu = GpuSettings(
        url=str(_env_or(
            "GPU_SERVICE_URL",
            _nested(data, "gpu", "url", "http://127.0.0.1:19944"),
        )).rstrip("/"),
        api_key=gpu_api_key,
        relay_upload_timeout_seconds=_env_or(
            "RELAY_UPLOAD_TIMEOUT_SECONDS",
            _nested(data, "gpu", "relay_upload_timeout_seconds", 120.0),
            cast=float,
        ),
        relay_result_timeout_seconds=_env_or(
            "GPU_RELAY_RESULT_TIMEOUT_SECONDS",
            _nested(data, "gpu", "relay_result_timeout_seconds", 120.0),
            cast=float,
        ),
        public_key_timeout_seconds=_env_or(
            "GPU_PUBLIC_KEY_TIMEOUT_SECONDS",
            _nested(data, "gpu", "public_key_timeout_seconds", 15.0),
            cast=float,
        ),
        public_key_cache_ttl_seconds=_env_or(
            "GPU_PUBLIC_KEY_CACHE_TTL_SECONDS",
            _nested(data, "gpu", "public_key_cache_ttl_seconds", 300),
            cast=int,
        ),
        generate_timeout_seconds=_env_or(
            "GPU_GENERATE_TIMEOUT_SECONDS",
            _nested(data, "gpu", "generate_timeout_seconds", 3600.0),
            cast=float,
        ),
        retry_max=_env_or(
            "GPU_RETRY_MAX",
            _nested(data, "gpu", "retry_max", 5),
            cast=int,
        ),
        retry_delay_seconds=_env_or(
            "GPU_RETRY_DELAY_SECONDS",
            _nested(data, "gpu", "retry_delay_seconds", 10.0),
            cast=float,
        ),
    )

    relay = RelaySettings(
        upload_max_bytes=_env_or(
            "RELAY_UPLOAD_MAX_BYTES",
            _nested(data, "relay", "upload_max_bytes", 50 * 1024 * 1024),
            cast=int,
        ),
        result_ttl_seconds=_env_or(
            "LOGIC_RELAY_RESULT_TTL_SECONDS",
            _nested(data, "relay", "result_ttl_seconds", 24 * 60 * 60),
            cast=int,
        ),
    )

    queue = QueueSettings(
        task_ttl_seconds=_env_or(
            "LOGIC_TASK_TTL_SECONDS",
            _nested(data, "queue", "task_ttl_seconds", 60 * 60),
            cast=int,
        ),
        max_tasks_per_user=_env_or(
            "LOGIC_MAX_TASKS_PER_USER",
            _nested(data, "queue", "max_tasks_per_user", 5),
            cast=int,
        ),
        estimated_seconds_per_task=_env_or(
            "LOGIC_ESTIMATED_SECONDS_PER_TASK",
            _nested(data, "queue", "estimated_seconds_per_task", 8.0),
            cast=float,
        ),
    )

    static = StaticSettings(
        cache_dir=Path(_env_or(
            "LOGIC_CACHE_DIR",
            _nested(data, "static", "cache_dir", str(Path(os.getenv("TMPDIR", "/tmp")) / "anomaly_logic_cache")),
        )).expanduser(),
        demo_dir=Path(_env_or(
            "LOGIC_DEMO_DIR",
            _nested(data, "static", "demo_dir", str(project_root / "demo_example")),
        )).expanduser(),
    )

    positive = {
        "server.port": server.port,
        "auth.session_ttl_seconds": auth.session_ttl_seconds,
        "auth.activation_max_attempts": auth.activation_max_attempts,
        "auth.activation_window_seconds": auth.activation_window_seconds,
        "oss.issued_key_ttl_seconds": oss.issued_key_ttl_seconds,
        "gpu.relay_upload_timeout_seconds": gpu.relay_upload_timeout_seconds,
        "gpu.relay_result_timeout_seconds": gpu.relay_result_timeout_seconds,
        "gpu.public_key_timeout_seconds": gpu.public_key_timeout_seconds,
        "gpu.public_key_cache_ttl_seconds": gpu.public_key_cache_ttl_seconds,
        "gpu.generate_timeout_seconds": gpu.generate_timeout_seconds,
        "gpu.retry_max": gpu.retry_max,
        "gpu.retry_delay_seconds": gpu.retry_delay_seconds,
        "relay.upload_max_bytes": relay.upload_max_bytes,
        "relay.result_ttl_seconds": relay.result_ttl_seconds,
        "queue.task_ttl_seconds": queue.task_ttl_seconds,
        "queue.max_tasks_per_user": queue.max_tasks_per_user,
        "queue.estimated_seconds_per_task": queue.estimated_seconds_per_task,
    }
    if not 1 <= server.port <= 65535:
        raise RuntimeError("LOGIC_SERVICE_PORT must be between 1 and 65535")
    invalid = [name for name, value in positive.items() if value <= 0]
    if invalid:
        raise RuntimeError(f"configuration values must be positive: {', '.join(invalid)}")

    return Settings(
        config_path=path,
        server=server,
        auth=auth,
        oss=oss,
        gpu=gpu,
        relay=relay,
        queue=queue,
        static=static,
    )
