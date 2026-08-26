"""Relay object storage for the GPU service.

This module handles temporary relay bytes and metadata only. It has no FastAPI,
OSS, cryptography, or model dependencies.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any


class RelayObjectError(Exception):
    """Base error for relay object lookup."""


class RelayObjectNotFound(RelayObjectError):
    pass


class RelayObjectExpired(RelayObjectError):
    pass


class RelayObjectInvalid(RelayObjectError):
    pass


class RelayStorage:
    RELAY_ID_RE = re.compile(r"^[0-9a-f]{32}$")

    def __init__(self, storage_dir: Path, max_upload_bytes: int, ttl_seconds: int):
        self.storage_dir = storage_dir
        self.max_upload_bytes = max_upload_bytes
        self.ttl_seconds = ttl_seconds
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.storage_dir, 0o700)
        except OSError:
            pass

    def paths(self, relay_id: str) -> tuple[Path, Path]:
        return (
            self.storage_dir / f"{relay_id}.data",
            self.storage_dir / f"{relay_id}.json",
        )

    def is_valid_id(self, relay_id: str) -> bool:
        return bool(self.RELAY_ID_RE.fullmatch(relay_id))

    def delete(self, relay_id: str) -> None:
        data_path, metadata_path = self.paths(relay_id)
        data_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)

    def cleanup_stale(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for metadata_path in self.storage_dir.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text())
                if float(metadata.get("created_at", 0)) < cutoff:
                    self.delete(metadata_path.stem)
            except Exception:  # noqa: BLE001
                self.delete(metadata_path.stem)

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        name = Path(filename or "image.bin").name
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        return cleaned[:120] or "image.bin"

    def store_bytes(
        self,
        content: bytes,
        filename: str | None,
        crypto_meta: dict[str, str],
        kind: str = "input",
    ) -> str:
        if len(content) > self.max_upload_bytes:
            raise ValueError("relay file too large")

        self.cleanup_stale()
        relay_id = uuid.uuid4().hex
        data_path, metadata_path = self.paths(relay_id)
        data_path.write_bytes(content)
        try:
            os.chmod(data_path, 0o600)
        except OSError:
            pass
        metadata_path.write_text(json.dumps({
            "created_at": time.time(),
            "filename": self._safe_filename(filename),
            "crypto": crypto_meta,
            "kind": kind,
        }))
        try:
            os.chmod(metadata_path, 0o600)
        except OSError:
            pass
        return f"relay://{relay_id}"

    def store_file(self, path: Path, kind: str = "result") -> str:
        return self.store_bytes(
            path.read_bytes(),
            path.name,
            {},
            kind=kind,
        )

    def load(self, relay_id: str) -> tuple[Path, dict[str, Any]]:
        if not self.is_valid_id(relay_id):
            raise RelayObjectInvalid("invalid relay id")
        data_path, metadata_path = self.paths(relay_id)
        if not data_path.exists() or not metadata_path.exists():
            raise RelayObjectNotFound("relay object not found")
        try:
            metadata = json.loads(metadata_path.read_text())
            if float(metadata.get("created_at", 0)) < time.time() - self.ttl_seconds:
                self.delete(relay_id)
                raise RelayObjectExpired("relay object expired")
        except RelayObjectError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.delete(relay_id)
            raise RelayObjectInvalid("invalid relay object") from exc
        return data_path, metadata
