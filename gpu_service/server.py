"""GPU 推理服务 — 运行在国内 GPU 服务器上，仅负责模型推理。

启动方式:
    python -m gpu_service.server --config /etc/anomaly-gpu/gpu.toml

配置:
    --config / GPU_CONFIG_PATH  TOML 配置文件路径
    GPU_SERVICE_API_KEY        内部 API Key（仅环境变量/EnvironmentFile）
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

# 把服务包根目录加入 sys.path，以便导入私有模型运行时模块。
# 该副本可以独立于研究仓库运行；权重和密钥通过环境变量注入。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from .config import load_settings
    from .contracts import GenerateRequest, GenerateResponse
    from .crypto_service import CseService
    from .runtime_loader import load_runtime
    from .storage_service import RelayObjectExpired, RelayObjectInvalid, RelayObjectNotFound, RelayStorage
except ImportError:
    # Compatibility with the documented direct script launch.
    from config import load_settings
    from contracts import GenerateRequest, GenerateResponse
    from crypto_service import CseService
    from runtime_loader import load_runtime
    from storage_service import RelayObjectExpired, RelayObjectInvalid, RelayObjectNotFound, RelayStorage
from oss_direct_upload import DirectOssUpload, DirectOssUploadError, load_oss_storage_config


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _config_path_from_argv() -> str | None:
    for index, argument in enumerate(sys.argv[1:]):
        if argument == "--config":
            if index + 2 > len(sys.argv) - 1:
                raise RuntimeError("--config requires a TOML path")
            return sys.argv[index + 2]
        if argument.startswith("--config="):
            return argument.split("=", 1)[1]
    return os.environ.get("GPU_CONFIG_PATH")


CONFIG_PATH = _config_path_from_argv()
SETTINGS = load_settings(CONFIG_PATH)

API_KEY = SETTINGS.api_key
SMOKE_MODE = SETTINGS.runtime.smoke_mode
SERVICE_HOST = SETTINGS.server.host
SERVICE_PORT = SETTINGS.server.port

OSS_CONFIG_PATH = SETTINGS.storage.oss_config_path
GPU_OSS_ENABLED = SETTINGS.storage.oss_enabled
OSS_OUTPUT_PREFIX = SETTINGS.storage.output_prefix

RELAY_MAX_UPLOAD_BYTES = SETTINGS.relay.max_upload_bytes
RELAY_TTL_SECONDS = SETTINGS.relay.ttl_seconds
RELAY_STORAGE = RelayStorage(
    SETTINGS.relay.storage_dir,
    max_upload_bytes=RELAY_MAX_UPLOAD_BYTES,
    ttl_seconds=RELAY_TTL_SECONDS,
)

OUTPUT_PREFIX: str  # 从 OSS 配置中读取，形如 "openoctopus/output"

ANOMALY_DATA_DIR = SETTINGS.archive.data_dir
ANOMALY_ARCHIVE_ENABLED = SETTINGS.archive.enabled

SITE_PRIVATE_KEY_PATH = SETTINGS.crypto.site_private_key_path


# ---------------------------------------------------------------------------
# 加载 OSS
# ---------------------------------------------------------------------------

def _init_oss(config_path: Path) -> tuple[DirectOssUpload | None, str]:
    if not GPU_OSS_ENABLED:
        print("[gpu] OSS disabled by GPU_OSS_ENABLED; relay output fallback is active", flush=True)
        return None, (OSS_OUTPUT_PREFIX or "relay").strip().strip("/")

    cfg = load_oss_storage_config(config_path)
    uploader = DirectOssUpload(cfg)
    uploader.ensure_sdk_available()
    # outputPrefix 用于存放生成结果
    output_prefix = os.environ.get(
        "OSS_OUTPUT_PREFIX",
        OSS_OUTPUT_PREFIX or cfg.input_prefix.replace("input", "output"),
    ).strip().strip("/")
    print(f"[gpu] OSS enabled bucket={cfg.bucket} endpoint={cfg.endpoint}", flush=True)
    print(f"[gpu] output prefix={output_prefix}", flush=True)
    return uploader, output_prefix


_oss_uploader, OUTPUT_PREFIX = _init_oss(OSS_CONFIG_PATH)

CSE_SERVICE = CseService(SITE_PRIVATE_KEY_PATH)
MODEL_RUNTIME = load_runtime(SETTINGS)


# ---------------------------------------------------------------------------
# 模型运行时
# ---------------------------------------------------------------------------
# 具体模型实现由 ModelRuntime 隔离；HTTP 层只依赖 generate() 契约。

# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------
# Pydantic contract definitions live in contracts.py.

# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

_RELAY_ID_RE = RelayStorage.RELAY_ID_RE


def _relay_id_from_key(object_key: str) -> str | None:
    if not isinstance(object_key, str) or not object_key.startswith("relay://"):
        return None
    relay_id = object_key[8:]
    if not _RELAY_ID_RE.fullmatch(relay_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid relay key")
    return relay_id


def _delete_relay_id(relay_id: str) -> None:
    RELAY_STORAGE.delete(relay_id)


def _store_relay_bytes(
    content: bytes,
    filename: str | None,
    crypto_meta: dict[str, str],
    kind: str = "input",
) -> str:
    return RELAY_STORAGE.store_bytes(content, filename, crypto_meta, kind=kind)


def _store_relay_file(path: Path, kind: str = "result") -> str:
    return RELAY_STORAGE.store_file(path, kind=kind)


def _load_relay_record(relay_id: str) -> tuple[Path, dict[str, Any]]:
    try:
        return RELAY_STORAGE.load(relay_id)
    except RelayObjectNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relay object not found") from exc
    except RelayObjectExpired as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="relay object expired") from exc
    except RelayObjectInvalid as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid relay object") from exc


def _download_from_relay(object_key: str, label: str) -> Path:
    relay_id = _relay_id_from_key(object_key)
    if relay_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid relay key")
    data_path, record = _load_relay_record(relay_id)
    download_dir = Path("/tmp/gpu_inference")
    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / f"{uuid.uuid4().hex}.png"
    shutil.copyfile(data_path, dest)

    meta = record.get("crypto") or {}
    if meta.get("x-oss-meta-crypto-iv") and meta.get("x-oss-meta-crypto-wk"):
        if not CSE_SERVICE.decrypt_file_in_place(dest, meta):
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无法解密中继文件 {label}（站点密钥不匹配或对象损坏）",
            )
        print(f"[gpu] decrypted relay CSE object ({label})", flush=True)
    try:
        with Image.open(dest) as img:
            img.verify()
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"中继对象不是有效图片 ({label}): {exc}",
        ) from exc
    return dest


def _get_object_crypto_meta(object_key: str) -> dict[str, str]:
    """Read the CSE metadata from an OSS object before decrypting it."""
    if _oss_uploader is None:
        return {}
    try:
        headers = _oss_uploader._get_bucket().head_object(object_key).headers
    except Exception as exc:  # noqa: BLE001
        print(f"[gpu] could not read OSS crypto metadata: {exc}", flush=True)
        return {}
    lower = {str(key).lower(): str(value) for key, value in headers.items()}
    return {
        key: lower.get(key, "")
        for key in (
            "x-oss-meta-crypto-iv",
            "x-oss-meta-crypto-wk",
            "x-oss-meta-crypto-kid",
        )
    }


def _download_from_oss(object_key: str, label: str = "image") -> Path:
    """从 OSS 或 GPU 中继读取图片；中继对象同样按 CSE 元数据解密。"""
    if object_key.startswith("relay://"):
        return _download_from_relay(object_key, label)

    if _oss_uploader is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"OSS 不可用且输入不是中继对象: {label}")

    download_dir = Path("/tmp/gpu_inference")
    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / f"{uuid.uuid4().hex}.png"
    try:
        _oss_uploader.import_object(object_key, dest)
    except DirectOssUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无法从 OSS 下载 {label}: {exc}",
        ) from exc
    # 客户端加密：带 crypto 元数据则解密（私钥只在本机）
    meta = _get_object_crypto_meta(object_key)
    if meta.get("x-oss-meta-crypto-iv") and meta.get("x-oss-meta-crypto-wk"):
        if not CSE_SERVICE.decrypt_file_in_place(dest, meta):
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"无法解密 {label}（站点密钥不匹配或对象损坏）",
            )
        print(f"[gpu] decrypted CSE object ({label})", flush=True)
    try:
        with Image.open(dest) as img:
            img.verify()
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OSS 对象不是有效图片 ({label}): {exc}",
        ) from exc
    return dest


def _upload_to_oss(local_path: Path, owner_hash: str, crypto_headers: dict | None = None) -> str:
    """上传生成结果到 OSS，返回 object key。crypto_headers 非空时对象为密文。"""
    if _oss_uploader is None:
        raise DirectOssUploadError("OSS disabled; use relay output")
    suffix = local_path.suffix.lower()
    object_key = f"{OUTPUT_PREFIX}/results/{owner_hash}/{uuid.uuid4().hex}{suffix}"
    bucket = _oss_uploader._get_bucket()
    if crypto_headers:
        bucket.put_object_from_file(object_key, str(local_path), headers=crypto_headers)
    else:
        bucket.put_object_from_file(object_key, str(local_path))
    print(f"[gpu] uploaded result to OSS: {object_key}", flush=True)
    return object_key


def _generate_signed_url(object_key: str, ttl: int = 3600) -> str:
    """为 OSS 对象生成带签名的临时访问 URL。"""
    if _oss_uploader is None:
        raise DirectOssUploadError("OSS disabled; use relay output")
    import oss2
    bucket = _oss_uploader._get_bucket()
    url = bucket.sign_url("GET", object_key, ttl)
    return url


# ---------------------------------------------------------------------------
# 客户端加密（CSE）
# ---------------------------------------------------------------------------
# RSA-OAEP/AES-GCM 实现位于 crypto_service.py；此处只负责把 OSS/relay 元数据
# 交给 CseService，避免协议层和密码学实现互相耦合。

# ---------------------------------------------------------------------------
# 本地存档：把每次生成任务的输入/输出整理保存到本机
# 目录结构: <ANOMALY_DATA_DIR>/<日期>/<owner_hash>/<task_id>/
#   ├── 0_stitched.png      拼接图（2×2：参考 | 背景 / 蒙版 | 结果）
#   ├── 1_reference.png     最终参考图（ROI 裁剪后，512×512）
#   ├── 2_background.png    背景图（512×512）
#   ├── 3_mask.png          蒙版（512×512）
#   ├── 4_result.png        生成结果
#   └── meta.json           参数/OSS key/ROI 等信息
# ---------------------------------------------------------------------------

def _record_dir_for(owner_hash: str, req: GenerateRequest) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    task_dir = req.task_id or uuid.uuid4().hex[:12]
    return ANOMALY_DATA_DIR / today / owner_hash / task_dir


def _letterbox_to_square(image: Image.Image, size=(512, 512), fill=(0, 0, 0)):
    """等比例缩放 + 居中填充到正方形画布（不拉伸，保持原图比例）。

    返回 (padded_image, box)，box = (left, top, width, height)，即原图内容在画布中的位置，
    推理后可据此裁掉填充恢复原图比例。
    """
    iw, ih = image.size
    w, h = size
    scale = min(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    resized = image.resize((nw, nh), Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", size, fill)
    ox, oy = (w - nw) // 2, (h - nh) // 2
    canvas.paste(resized, (ox, oy))
    return canvas, (ox, oy, nw, nh)


def _archive_task_data(
    record_dir: Path,
    ref: Image.Image,
    bg: Image.Image,
    mask: Image.Image,
    result: Image.Image,
    req: GenerateRequest,
    result_key: str,
) -> None:
    """保存单张图 + 拼接图 + 元数据。失败不阻塞主流程，只打印日志。"""
    try:
        record_dir.mkdir(parents=True, exist_ok=True)
        ref.convert("RGB").save(record_dir / "1_reference.png")
        bg.convert("RGB").save(record_dir / "2_background.png")
        mask.convert("L").save(record_dir / "3_mask.png")
        result.convert("RGB").save(record_dir / "4_result.png")

        # 拼接图（2×2），每格等比例填充到 512，保证各张图不拉伸
        cell = 512
        grid = Image.new("RGB", (cell * 2, cell * 2), (0, 0, 0))
        for (x, y), img in (
            ((0, 0), ref),
            ((cell, 0), bg),
            ((0, cell), mask),
            ((cell, cell), result),
        ):
            padded, _ = _letterbox_to_square(img.convert("RGB"), (cell, cell))
            grid.paste(padded, (x, y))
        grid.save(record_dir / "0_stitched.png")

        meta = {
            "task_id": req.task_id,
            "seed": req.seed,
            "ddim_steps": req.ddim_steps,
            "guidance_scale": req.guidance_scale,
            "output_format": req.output_format,
            "background_key": req.background_key,
            "reference_key": req.reference_key,
            "mask_key": req.mask_key,
            "result_key": result_key,
            "roi": req.roi.model_dump() if req.roi else None,
            "encrypted_result": bool(req.user_public_key and req.user_public_key.strip()),
            "saved_at": datetime.now().isoformat(),
        }
        (record_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2)
        )
        print(f"[gpu] archived task data to {record_dir}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[gpu] archive failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GPU Inference Service",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def verify_api_key(request: Request) -> None:
    actual = request.headers.get("Authorization", "")
    expected = f"Bearer {API_KEY}"
    if actual != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized"},
        )


@app.exception_handler(HTTPException)
async def http_exc_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(detail)})


@app.post("/relay/upload", dependencies=[Depends(verify_api_key)])
async def relay_upload(
    file: UploadFile = File(...),
    crypto_iv: str = Form(""),
    crypto_wk: str = Form(""),
    crypto_kid: str = Form(""),
):
    """接收公网逻辑层转发的浏览器 CSE 密文，不落 OSS。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="no file")
    if not crypto_iv or not crypto_wk:
        raise HTTPException(status_code=400, detail="crypto_iv and crypto_wk are required")
    content = await file.read(RELAY_MAX_UPLOAD_BYTES + 1)
    if len(content) > RELAY_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="relay file too large")
    key = _store_relay_bytes(
        content,
        file.filename,
        {
            "x-oss-meta-crypto-iv": crypto_iv,
            "x-oss-meta-crypto-wk": crypto_wk,
            "x-oss-meta-crypto-kid": crypto_kid,
        },
    )
    print(f"[gpu] relay input stored: {key}", flush=True)
    return {"key": key, "url": ""}


@app.get("/relay/result/{relay_id}", dependencies=[Depends(verify_api_key)])
async def relay_result(relay_id: str):
    if not _RELAY_ID_RE.fullmatch(relay_id):
        raise HTTPException(status_code=404, detail="relay object not found")
    data_path, record = _load_relay_record(relay_id)
    if record.get("kind") != "result":
        raise HTTPException(status_code=404, detail="relay result not found")
    media_type = mimetypes.guess_type(record.get("filename", ""))[0] or "application/octet-stream"
    return FileResponse(data_path, media_type=media_type)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "protocol_version": SETTINGS.server.protocol_version,
        "model_ready": MODEL_RUNTIME.ready,
        "cuda_available": MODEL_RUNTIME.cuda_available,
        "device": MODEL_RUNTIME.device,
        "oss_enabled": _oss_uploader is not None,
        "relay_enabled": True,
    }


@app.get("/public-key", dependencies=[Depends(verify_api_key)])
async def get_public_key():
    """返回当前站点公钥（含 kid），供逻辑层代理给前端做上传加密。"""
    try:
        return CSE_SERVICE.public_key_payload()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="站点公钥不存在") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取当前公钥失败: {exc}") from exc


@app.post("/generate", response_model=GenerateResponse, dependencies=[Depends(verify_api_key)])
async def generate(req: GenerateRequest):
    """执行图像生成推理。"""
    start = time.perf_counter()

    if SMOKE_MODE:
        smoke_paths = []
        smoke_relay_ids = []
        try:
            for key, label in (
                (req.background_key, "背景图"),
                (req.reference_key, "参考图"),
                (req.mask_key, "蒙版"),
            ):
                path = _download_from_oss(key, label=label)
                smoke_paths.append(path)
                relay_id = _relay_id_from_key(key)
                if relay_id:
                    smoke_relay_ids.append(relay_id)
            return GenerateResponse(
                result_key=f"smoke/{req.task_id or uuid.uuid4().hex}.jpg",
                result_url="https://example.invalid/smoke-result.jpg",
                elapsed_seconds=round(time.perf_counter() - start, 3),
            )
        finally:
            for path in smoke_paths:
                path.unlink(missing_ok=True)
            for relay_id in smoke_relay_ids:
                _delete_relay_id(relay_id)

    if not MODEL_RUNTIME.ready:
        raise HTTPException(status_code=503, detail="model runtime is not ready")

    # 1. 从 OSS 或 GPU 中继下载三张输入图片
    input_keys = (req.background_key, req.reference_key, req.mask_key)
    relay_ids = [rid for key in input_keys if (rid := _relay_id_from_key(key))]
    bg_path = _download_from_oss(req.background_key, label="背景图")
    ref_path = _download_from_oss(req.reference_key, label="参考图")
    mask_path = _download_from_oss(req.mask_key, label="蒙版")

    try:
        target_size = (512, 512)
        # 背景：等比例缩放 + 黑边填充（letterbox）到 512，不拉伸，保持原图比例。
        # 蒙版前端已按同一 letterbox 布局绘制（填充空间），与背景对齐。
        bg, bg_box = _letterbox_to_square(Image.open(bg_path).convert("RGB"), target_size)
        ref = Image.open(ref_path).convert("RGB")
        # ROI 裁剪（像素坐标，在 resize 之前裁剪）
        if req.roi and req.roi.width > 0 and req.roi.height > 0:
            rw, rh = ref.size
            side = max(req.roi.width, req.roi.height)  # 正方形取大边
            left = max(0, req.roi.x)
            top = max(0, req.roi.y)
            right = min(rw, req.roi.x + side)
            bottom = min(rh, req.roi.y + side)
            if right > left and bottom > top:
                ref = ref.crop((left, top, right, bottom))
                print(f"[gpu] roi crop: ({left},{top})-({right},{bottom})", flush=True)
        ref = ref.resize(target_size, Image.Resampling.BICUBIC)
        mask = Image.open(mask_path).convert("L").resize(target_size, Image.Resampling.NEAREST)
        mask = mask.point(lambda p: 255 if p > 0 else 0)

        # 2. 推理
        print(
            f"[gpu] inference starting steps={req.ddim_steps} "
            f"scale={req.guidance_scale} seed={req.seed}",
            flush=True,
        )
        images = MODEL_RUNTIME.generate(
            pil_ref_image=ref,
            pil_background_image=bg,
            pil_mask_image=mask,
            num_samples=1,
            num_inference_steps=req.ddim_steps,
            guidance_scale=req.guidance_scale,
            seed=req.seed,
        )

        # 3. 保存结果：裁掉填充，恢复原图比例
        result_dir = Path("/tmp/gpu_inference")
        ext = "jpg" if req.output_format == "jpeg" else req.output_format
        result_path = result_dir / f"result_{uuid.uuid4().hex}.{ext}"
        result_img = images[0].convert("RGB")
        pad_x, pad_y, pw, ph = bg_box
        if pw < 512 or ph < 512:
            result_img = result_img.crop((pad_x, pad_y, pad_x + pw, pad_y + ph))
            print(f"[gpu] result cropped to original aspect: {result_img.size}", flush=True)
        result_img.save(result_path, format="JPEG" if ext == "jpg" else ext.upper(), quality=95)

        elapsed = round(time.perf_counter() - start, 3)
        print(f"[gpu] inference done elapsed={elapsed}s", flush=True)

        # 4. 上传结果到 OSS；OSS 不可用时保存在 GPU 中继目录
        owner_hash = hashlib.sha256(
            f"{req.background_key}:{req.seed}".encode()
        ).hexdigest()[:16]
        crypto_headers = {}
        crypto_iv = None
        crypto_wk = None
        if req.user_public_key and req.user_public_key.strip():
            crypto_headers = CSE_SERVICE.encrypt_result_file(result_path, req.user_public_key)
            crypto_iv = crypto_headers.get("x-oss-meta-crypto-iv")
            crypto_wk = crypto_headers.get("x-oss-meta-crypto-wk")
            print(f"[gpu] result encrypted for user (CSE)", flush=True)
        try:
            result_key = _upload_to_oss(result_path, owner_hash, crypto_headers)
            result_url = _generate_signed_url(result_key)
        except Exception as exc:  # noqa: BLE001
            print(f"[gpu] OSS result upload unavailable, using relay fallback: {exc}", flush=True)
            result_key = _store_relay_file(result_path, kind="result")
            result_url = result_key

        # 5. 本地存档（用户图片/mask/参考图/ROI 信息 + 拼接图与单张图）
        if ANOMALY_ARCHIVE_ENABLED:
            _archive_task_data(
                record_dir=_record_dir_for(owner_hash, req),
                ref=ref, bg=bg, mask=mask, result=result_img,
                req=req, result_key=result_key,
            )

    finally:
        # 清理临时文件和本次任务的输入中继对象
        for p in (bg_path, ref_path, mask_path):
            p.unlink(missing_ok=True)
        for relay_id in relay_ids:
            _delete_relay_id(relay_id)
        if 'result_path' in locals():
            result_path.unlink(missing_ok=True)

    return GenerateResponse(
        result_key=result_key,
        result_url=result_url,
        elapsed_seconds=elapsed,
        crypto_iv=crypto_iv,
        crypto_wk=crypto_wk,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVICE_HOST, port=SERVICE_PORT)
