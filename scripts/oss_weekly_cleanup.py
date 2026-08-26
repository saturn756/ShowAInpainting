#!/usr/bin/env python3
"""OSS 周度清理：删除 input/output 前缀下超过 max-age-days 天的对象。

数据在 OSS 中为 CSE 密文（阿里云无法解密），删除后无泄露风险。
本地存档 (data/anomaly_records/) 不受影响，保留作记录。

用法:
    python scripts/oss_weekly_cleanup.py --dry-run        # 试运行，只看不删
    python scripts/oss_weekly_cleanup.py --max-age-days 7 # 删除 7 天前对象
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from oss_direct_upload import DirectOssUpload, load_oss_storage_config  # noqa: E402


def _obj_age_timestamp(obj) -> float:
    """把 oss2 对象的 last_modified 转成 unix 时间戳。"""
    lm = obj.last_modified
    if isinstance(lm, datetime):
        return lm.timestamp()
    if isinstance(lm, str):
        return datetime.fromisoformat(lm.replace("Z", "+00:00")).timestamp()
    return float(lm)


def main() -> int:
    ap = argparse.ArgumentParser(description="OSS 周度清理")
    ap.add_argument("--max-age-days", type=int, default=7, help="超过该天数的对象删除")
    ap.add_argument("--dry-run", action="store_true", help="只列出不删除")
    ap.add_argument("--config", default=str(_PROJECT_ROOT / "oss.json"), help="OSS 配置文件路径")
    args = ap.parse_args()

    uploader = DirectOssUpload(load_oss_storage_config(Path(args.config)))
    bucket = uploader._get_bucket()
    input_prefix = uploader.config.input_prefix.rstrip("/")
    output_prefix = input_prefix.replace("input", "output")
    prefixes = [input_prefix + "/", output_prefix + "/"]

    cutoff = time.time() - args.max_age_days * 86400
    total = 0
    skipped = 0

    for prefix in prefixes:
        marker = None
        while True:
            kw = {"prefix": prefix, "max_keys": 1000}
            if marker:
                kw["marker"] = marker
            result = bucket.list_objects(**kw)
            objects = getattr(result, "object_list", []) or []
            for obj in objects:
                ts = _obj_age_timestamp(obj)
                if ts < cutoff:
                    total += 1
                    if not args.dry_run:
                        bucket.delete_object(obj.key)
                    age_d = round((time.time() - ts) / 86400, 1)
                    print(f"{'DRY ' if args.dry_run else 'DEL '}{obj.key} (age={age_d}d)")
                else:
                    skipped += 1
            if getattr(result, "is_truncated", False):
                marker = result.next_marker
            else:
                break

    print(f"[cleanup] done: {total} to clean, {skipped} kept "
          f"({'dry-run, nothing deleted' if args.dry_run else 'deleted'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
