"""Typed configuration for the GPU service.

Secrets remain environment-only. The optional TOML file contains deployment
paths and non-secret behavior; GPU_CONFIG_PATH or --config selects the file.
"""

from __future__ import annotations

import os
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _nested(data: dict[str, Any], section: str, key: str, default: Any) -> Any:
    value = data.get(section, {})
    if not isinstance(value, dict):
        return default
    return value.get(key, default)


def _env_or(
    name: str,
    value: Any,
    *,
    cast: type | None = None,
) -> Any:
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
    protocol_version: str


@dataclass(frozen=True)
class StorageSettings:
    oss_enabled: bool
    oss_config_path: Path
    output_prefix: str | None


@dataclass(frozen=True)
class RelaySettings:
    storage_dir: Path
    max_upload_bytes: int
    ttl_seconds: int


@dataclass(frozen=True)
class CryptoSettings:
    site_private_key_path: Path


@dataclass(frozen=True)
class RuntimeSettings:
    backend: str
    module: str
    module_path: Path | None
    smoke_mode: bool


@dataclass(frozen=True)
class ArchiveSettings:
    enabled: bool
    data_dir: Path


@dataclass(frozen=True)
class Settings:
    api_key: str
    config_path: Path | None
    server: ServerSettings
    storage: StorageSettings
    relay: RelaySettings
    crypto: CryptoSettings
    runtime: RuntimeSettings
    archive: ArchiveSettings


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load TOML first, then apply environment overrides and validate secrets."""
    project_root = Path(__file__).resolve().parent.parent
    selected = config_path or os.environ.get("GPU_CONFIG_PATH")
    path = Path(selected).expanduser() if selected else None
    data: dict[str, Any] = {}
    if path:
        if not path.exists():
            raise RuntimeError(f"GPU config file does not exist: {path}")
        with path.open("rb") as handle:
            data = tomllib.load(handle)

    api_key = os.environ.get("GPU_SERVICE_API_KEY", "").strip()
    if not api_key or len(api_key) < 16:
        raise RuntimeError("GPU_SERVICE_API_KEY must be set to at least 16 characters")

    server = ServerSettings(
        host=_env_or(
            "GPU_SERVICE_HOST",
            _nested(data, "server", "host", "127.0.0.1"),
        ),
        port=_env_or(
            "GPU_SERVICE_PORT",
            _nested(data, "server", "port", 7861),
            cast=int,
        ),
        protocol_version=str(_nested(data, "server", "protocol_version", "1")),
    )

    storage = StorageSettings(
        oss_enabled=_env_or(
            "GPU_OSS_ENABLED",
            _nested(data, "storage", "oss_enabled", True),
            cast=bool,
        ),
        oss_config_path=Path(_env_or(
            "OSS_CONFIG_PATH",
            _nested(data, "storage", "oss_config_path", str(project_root / "oss.json")),
        )).expanduser(),
        output_prefix=_env_or(
            "OSS_OUTPUT_PREFIX",
            _nested(data, "storage", "output_prefix", None),
        ),
    )

    relay = RelaySettings(
        storage_dir=Path(_env_or(
            "RELAY_STORAGE_DIR",
            _nested(data, "relay", "storage_dir", "/tmp/anomaly_gpu_relay"),
        )).expanduser(),
        max_upload_bytes=_env_or(
            "RELAY_MAX_UPLOAD_BYTES",
            _nested(data, "relay", "max_upload_bytes", 50 * 1024 * 1024),
            cast=int,
        ),
        ttl_seconds=_env_or(
            "RELAY_TTL_SECONDS",
            _nested(data, "relay", "ttl_seconds", 60 * 60),
            cast=int,
        ),
    )

    crypto = CryptoSettings(
        site_private_key_path=Path(_env_or(
            "SITE_PRIVATE_KEY_PATH",
            _nested(
                data,
                "crypto",
                "site_private_key_path",
                str(project_root / "data" / "crypto" / "site_master_private.pem"),
            ),
        )).expanduser(),
    )

    module_path_value = _env_or(
        "GPU_RUNTIME_MODULE_PATH",
        _nested(data, "runtime", "module_path", None),
    )
    runtime = RuntimeSettings(
        backend=str(_env_or(
            "GPU_RUNTIME_BACKEND",
            _nested(data, "runtime", "backend", "external"),
        )),
        module=str(_env_or(
            "GPU_RUNTIME_MODULE",
            _nested(data, "runtime", "module", "private_runtime_adapter"),
        )),
        module_path=Path(module_path_value).expanduser() if module_path_value else None,
        smoke_mode=_env_or(
            "SERVICE_SMOKE_MODE",
            _nested(data, "runtime", "smoke_mode", False),
            cast=bool,
        ),
    )

    archive = ArchiveSettings(
        enabled=_env_or(
            "ANOMALY_ARCHIVE_ENABLED",
            _nested(data, "archive", "enabled", True),
            cast=bool,
        ),
        data_dir=Path(_env_or(
            "ANOMALY_DATA_DIR",
            _nested(data, "archive", "data_dir", str(project_root / "data" / "anomaly_records")),
        )).expanduser(),
    )

    if runtime.backend not in {"external", "mock"}:
        raise RuntimeError(
            f"unsupported GPU_RUNTIME_BACKEND={runtime.backend}; expected external or mock"
        )
    if relay.max_upload_bytes <= 0 or relay.ttl_seconds <= 0:
        raise RuntimeError("relay limits must be positive")

    return Settings(
        api_key=api_key,
        config_path=path,
        server=server,
        storage=storage,
        relay=relay,
        crypto=crypto,
        runtime=runtime,
        archive=archive,
    )
