"""Client-side encryption (CSE) primitives for the GPU service.

This module owns the site's private keys and is intentionally independent from
HTTP, OSS, relay storage, and model inference. Plaintext exists only while the
caller is preparing an image for inference or encrypting a result.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class CseService:
    """RSA-OAEP(SHA-256) + AES-256-GCM service with rotating site keys."""

    def __init__(self, private_key_path: Path):
        self.private_key_path = private_key_path
        self._key_cache: dict[str, Any] = {
            "mtime": -1,
            "current_kid": None,
            "keys": {},
        }

    @staticmethod
    def _oaep_padding() -> Any:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )

    @staticmethod
    def _key_kid(private_key: Any) -> str:
        from cryptography.hazmat.primitives import serialization

        der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return hashlib.sha256(der).hexdigest()[:12]

    @staticmethod
    def _load_pem_private(path: Path) -> Any:
        from cryptography.hazmat.primitives import serialization

        return serialization.load_pem_private_key(path.read_bytes(), password=None)

    def _discover_site_keys(self) -> dict[str, Any]:
        """Load current and historical site keys, keeping old uploads decryptable."""
        crypto_dir = self.private_key_path.parent
        current_json = crypto_dir / "current.json"
        mtime = current_json.stat().st_mtime if current_json.exists() else 0
        if self._key_cache["mtime"] == mtime and self._key_cache["keys"]:
            return self._key_cache

        keys: dict[str, Any] = {}
        key_dir = crypto_dir / "keys"
        if key_dir.exists():
            for private_path in sorted(key_dir.glob("*/site_master_private.pem"), reverse=True):
                try:
                    private_key = self._load_pem_private(private_path)
                    keys[self._key_kid(private_key)] = private_key
                except Exception:  # noqa: BLE001
                    continue

        # Compatibility with the pre-rotation key location.
        if self.private_key_path.exists():
            try:
                private_key = self._load_pem_private(self.private_key_path)
                keys.setdefault(self._key_kid(private_key), private_key)
            except Exception:  # noqa: BLE001
                pass

        current_kid = None
        if current_json.exists():
            try:
                current_kid = json.loads(current_json.read_text()).get("kid")
            except Exception:  # noqa: BLE001
                pass

        self._key_cache.update({
            "mtime": mtime,
            "current_kid": current_kid,
            "keys": keys,
        })
        print(f"[gpu][cse] site keys loaded: {len(keys)} (current kid={current_kid})", flush=True)
        return self._key_cache

    def public_key_payload(self) -> dict[str, str | None]:
        """Return the current public key and key id for browser-side encryption."""
        crypto_dir = self.private_key_path.parent
        current_json = crypto_dir / "current.json"
        if current_json.exists():
            info = json.loads(current_json.read_text())
            public_path = crypto_dir / info["public_key"]
            return {
                "kid": info["kid"],
                "public_key_pem": public_path.read_text(),
                "date": info.get("date"),
            }

        legacy_public = crypto_dir / "site_master_public.pem"
        if legacy_public.exists():
            discovered = self._discover_site_keys()
            return {
                "kid": discovered["current_kid"],
                "public_key_pem": legacy_public.read_text(),
                "date": None,
            }
        raise FileNotFoundError("site public key not found")

    def decrypt_file_in_place(self, path: Path, metadata: dict[str, str]) -> bool:
        """Decrypt a CSE object in place; return False for missing/bad keys."""
        iv_b64 = metadata.get("x-oss-meta-crypto-iv")
        wrapped_key_b64 = metadata.get("x-oss-meta-crypto-wk")
        if not iv_b64 or not wrapped_key_b64:
            return False

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = base64.b64decode(iv_b64)
        wrapped_key = base64.b64decode(wrapped_key_b64)
        discovered = self._discover_site_keys()
        requested_kid = metadata.get("x-oss-meta-crypto-kid")

        ordered: list[Any] = []
        if requested_kid and requested_kid in discovered["keys"]:
            ordered.append(discovered["keys"][requested_kid])
        if discovered["current_kid"] in discovered["keys"]:
            ordered.append(discovered["keys"][discovered["current_kid"]])

        candidates: list[Any] = []
        seen: set[int] = set()
        for private_key in ordered:
            if id(private_key) not in seen:
                seen.add(id(private_key))
                candidates.append(private_key)
        candidates.extend(
            private_key for private_key in discovered["keys"].values()
            if id(private_key) not in seen
        )

        for private_key in candidates:
            try:
                data_key = private_key.decrypt(wrapped_key, self._oaep_padding())
                plaintext = AESGCM(data_key).decrypt(iv, path.read_bytes(), None)
                path.write_bytes(plaintext)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    @staticmethod
    def encrypt_result_file(path: Path, user_public_pem: str) -> dict[str, str]:
        """Encrypt a generated result in place with the browser's public key."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        public_key = serialization.load_pem_public_key(user_public_pem.encode())
        data_key = os.urandom(32)
        iv = os.urandom(12)
        ciphertext = AESGCM(data_key).encrypt(iv, path.read_bytes(), None)
        wrapped_key = public_key.encrypt(
            data_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        path.write_bytes(ciphertext)
        return {
            "x-oss-meta-crypto-iv": base64.b64encode(iv).decode(),
            "x-oss-meta-crypto-wk": base64.b64encode(wrapped_key).decode(),
        }
