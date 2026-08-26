#!/usr/bin/env python3
"""每日轮换站点主密钥。

- 生成新的 RSA-2048 密钥对，保存到 data/crypto/keys/<日期>/site_master_{private,public}.pem
- 更新 data/crypto/current.json（当前密钥指针 + kid）
- 历史密钥全部留存（GPU 解密时按 kid 选择或逐个尝试），私钥 chmod 600
- 旧位置的 data/crypto/site_master_private.pem（遗留密钥）不删除，GPU 会一并加载

用法:
    python scripts/rotate_site_key.py            # 生成今日密钥（幂等，已有则跳过）
    python scripts/rotate_site_key.py --force    # 强制重新生成今日密钥
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CRYPTO_DIR = _ROOT / "data" / "crypto"
_KEYS_DIR = _CRYPTO_DIR / "keys"
_CURRENT_JSON = _CRYPTO_DIR / "current.json"


def _kid_from_public_der(der: bytes) -> str:
    import hashlib
    return hashlib.sha256(der).hexdigest()[:12]


def main() -> int:
    ap = argparse.ArgumentParser(description="每日轮换站点主密钥")
    ap.add_argument("--force", action="store_true", help="强制重新生成今日密钥")
    args = ap.parse_args()

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    _KEYS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    day_dir = _KEYS_DIR / today
    priv_path = day_dir / "site_master_private.pem"
    pub_path = day_dir / "site_master_public.pem"

    if priv_path.exists() and not args.force:
        print(f"[rotate] key for {today} already exists, skip")
        return 0

    day_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.write_bytes(priv_pem)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_pem)

    kid = _kid_from_public_der(pub_der)
    _CURRENT_JSON.write_text(json.dumps({
        "kid": kid,
        "date": today,
        "private_key": str(priv_path.relative_to(_CRYPTO_DIR)),
        "public_key": str(pub_path.relative_to(_CRYPTO_DIR)),
        "generated_at": datetime.now().isoformat(),
    }, ensure_ascii=False, indent=2))
    print(f"[rotate] new key generated for {today}, kid={kid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
