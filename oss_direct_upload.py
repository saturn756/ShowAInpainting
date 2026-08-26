"""Aliyun OSS direct-upload support for the Gradio image inputs.

The browser receives a short-lived, single-object POST policy, uploads the
    image directly to OSS, and then asks the GPU service to import that object
into Gradio's normal upload cache.  Image bytes therefore never travel through
the public reverse proxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class DirectOssUploadError(RuntimeError):
    """Raised when direct OSS upload configuration or an object is invalid."""


@dataclass(frozen=True)
class OssStorageConfig:
    access_key_id: str
    access_key_secret: str
    bucket: str
    endpoint: str
    public_base_url: str
    input_prefix: str
    policy_ttl_seconds: int
    max_upload_bytes: int


def _require_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DirectOssUploadError(f"missing OSS configuration value: {key}")
    return value.strip()


def _normalize_prefix(value: str) -> str:
    normalized = value.strip().strip("/")
    if not normalized:
        raise DirectOssUploadError("OSS inputPrefix must not be empty")
    return normalized


def load_oss_storage_config(config_path: Path | None = None) -> OssStorageConfig:
    path = config_path or Path(
        os.environ.get("OSS_CONFIG_PATH", Path(__file__).with_name("oss.json"))
    )
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DirectOssUploadError(f"OSS configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DirectOssUploadError(f"OSS configuration file is not valid JSON: {path}") from exc

    storage = config.get("assetStorage")
    credentials = config.get("credentials")
    if not isinstance(storage, dict) or not isinstance(credentials, dict):
        raise DirectOssUploadError(
            "OSS configuration must contain assetStorage and credentials objects"
        )
    if storage.get("provider") != "aliyun-oss":
        raise DirectOssUploadError("only aliyun-oss is supported for direct upload")

    access_key_id = os.environ.get("OSS_ACCESS_KEY_ID", "").strip() or _require_string(
        credentials, "accessKeyId"
    )
    access_key_secret = os.environ.get(
        "OSS_ACCESS_KEY_SECRET", ""
    ).strip() or _require_string(credentials, "accessKeySecret")
    ttl_seconds = int(os.environ.get("OSS_UPLOAD_POLICY_TTL_SECONDS", "900"))
    max_upload_bytes = int(os.environ.get("OSS_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    if ttl_seconds < 60 or ttl_seconds > 3600:
        raise DirectOssUploadError("OSS upload policy TTL must be between 60 and 3600 seconds")
    if max_upload_bytes < 1 or max_upload_bytes > 100 * 1024 * 1024:
        raise DirectOssUploadError("OSS max upload size must be between 1 byte and 100 MiB")

    public_base_url = _require_string(storage, "publicBaseUrl").rstrip("/")
    parsed_public_url = urlparse(public_base_url)
    if parsed_public_url.scheme != "https" or not parsed_public_url.netloc:
        raise DirectOssUploadError("OSS publicBaseUrl must be an HTTPS URL")

    return OssStorageConfig(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        bucket=_require_string(storage, "bucket"),
        endpoint=_require_string(storage, "endpoint"),
        public_base_url=public_base_url,
        input_prefix=_normalize_prefix(_require_string(storage, "inputPrefix")),
        policy_ttl_seconds=ttl_seconds,
        max_upload_bytes=max_upload_bytes,
    )


class DirectOssUpload:
    """Issues OSS POST policies and imports validated images into Gradio's cache."""

    _SAFE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}
    _OBJECT_VISIBILITY_TIMEOUT_SECONDS = 20
    _OBJECT_VISIBILITY_POLL_SECONDS = 0.5

    def __init__(self, config: OssStorageConfig):
        self.config = config
        self._bucket: Any | None = None

    @classmethod
    def from_platform_config(cls) -> "DirectOssUpload":
        return cls(load_oss_storage_config())

    def ensure_sdk_available(self) -> None:
        self._import_oss2()

    def make_object_key(self, owner_key: str, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in self._SAFE_SUFFIXES:
            raise DirectOssUploadError("only BMP, JPEG, PNG, and WebP images are supported")
        owner_segment = hashlib.sha256(owner_key.encode("utf-8")).hexdigest()[:24]
        return f"{self.config.input_prefix}/gradio/{owner_segment}/{uuid.uuid4().hex}{suffix}"

    def create_upload_policy(self, object_key: str) -> dict[str, Any]:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.config.policy_ttl_seconds
        )
        policy = {
            "expiration": expires_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "conditions": [
                ["eq", "$key", object_key],
                ["content-length-range", 1, self.config.max_upload_bytes],
                # 客户端加密（CSE）元数据：数据密钥封装 + IV + 密钥 kid
                ["starts-with", "$x-oss-meta-crypto-iv", ""],
                ["starts-with", "$x-oss-meta-crypto-wk", ""],
                ["starts-with", "$x-oss-meta-crypto-kid", ""],
            ],
        }
        encoded_policy = base64.b64encode(
            json.dumps(policy, separators=(",", ":")).encode("utf-8")
        )
        signature = base64.b64encode(
            hmac.new(
                self.config.access_key_secret.encode("utf-8"),
                encoded_policy,
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        return {
            "url": self.config.public_base_url,
            "key": object_key,
            "fields": {
                "key": object_key,
                "OSSAccessKeyId": self.config.access_key_id,
                "policy": encoded_policy.decode("ascii"),
                "Signature": signature,
                "success_action_status": "204",
            },
            "expiresAt": int(expires_at.timestamp()),
        }

    def import_object(self, object_key: str, destination: Path) -> None:
        bucket = self._get_bucket()
        deadline = time.monotonic() + self._OBJECT_VISIBILITY_TIMEOUT_SECONDS
        while True:
            try:
                metadata = bucket.head_object(object_key)
                content_length = int(metadata.content_length)
                break
            except Exception as exc:  # noqa: BLE001
                details = getattr(exc, "details", {})
                code = details.get("Code", "") if isinstance(details, dict) else ""
                status = getattr(exc, "status", None)
                if status != 404 and code not in {"NoSuchKey", "NoSuchObject"}:
                    raise DirectOssUploadError(
                        f"could not inspect the uploaded OSS object ({code or type(exc).__name__})"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise DirectOssUploadError(
                        "uploaded OSS object was not visible before the import timeout"
                    ) from exc
                time.sleep(self._OBJECT_VISIBILITY_POLL_SECONDS)
        if content_length < 1 or content_length > self.config.max_upload_bytes:
            raise DirectOssUploadError("uploaded OSS object has an invalid size")

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination.with_suffix(destination.suffix + ".part")
        try:
            bucket.get_object_to_file(object_key, str(temporary_path))
            temporary_path.replace(destination)
        except Exception as exc:  # noqa: BLE001
            temporary_path.unlink(missing_ok=True)
            raise DirectOssUploadError("could not download the uploaded OSS object") from exc

    def delete_object(self, object_key: str) -> None:
        try:
            self._get_bucket().delete_object(object_key)
        except Exception as exc:  # noqa: BLE001
            raise DirectOssUploadError("could not delete the uploaded OSS object") from exc

    def _get_bucket(self) -> Any:
        if self._bucket is not None:
            return self._bucket
        oss2 = self._import_oss2()
        auth = oss2.Auth(self.config.access_key_id, self.config.access_key_secret)
        self._bucket = oss2.Bucket(auth, self.config.endpoint, self.config.bucket)
        return self._bucket

    @staticmethod
    def _import_oss2() -> Any:
        try:
            import oss2  # type: ignore
        except ImportError as exc:
            raise DirectOssUploadError(
                "missing dependency oss2; install the project requirements"
            ) from exc
        return oss2
