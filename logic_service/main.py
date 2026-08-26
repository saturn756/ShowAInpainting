"""逻辑服务入口 — 部署在公网逻辑服务器上。

负责:
- 激活码验证 + Session 管理
- OSS 上传策略签发
- 任务队列管理
- 转发到国内 GPU 推理服务

启动方式:
    python -m logic_service.main --config /etc/anomaly-gen/logic.toml

环境变量（敏感值和配置文件路径）:
    GRADIO_ACTIVATION_CODE       5 位激活码
    GRADIO_SESSION_SECRET        32+ 位 session 签名密钥
    GPU_SERVICE_API_KEY          国内 GPU 服务的 API Key
    LOGIC_CONFIG_PATH         逻辑层 TOML 配置文件路径
"""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import hmac
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# 把项目根目录加入 sys.path，以便导入 oss_direct_upload
# 支持从 service_release 根目录或直接部署目录启动
_FILE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = None
for _ROOT in (_FILE_DIR.parent.parent, _FILE_DIR.parent):
    if (_ROOT / "oss_direct_upload.py").exists():
        _PROJECT_ROOT = _ROOT
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        break
if _PROJECT_ROOT is None:
    raise RuntimeError("Cannot find oss_direct_upload.py — check deployment structure")

from oss_direct_upload import DirectOssUpload, DirectOssUploadError, load_oss_storage_config

try:
    from .config import load_settings
except ImportError:  # direct execution: python logic_service/main.py
    from config import load_settings


def _config_path_from_argv() -> str | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    args, _ = parser.parse_known_args()
    return args.config


# The module supports both uvicorn import style and `python -m` startup.
SETTINGS = load_settings(_config_path_from_argv())

ACTIVATION_CODE = SETTINGS.auth.activation_code
SESSION_SECRET = SETTINGS.auth.session_secret
GPU_SERVICE_URL = SETTINGS.gpu.url
GPU_SERVICE_API_KEY = SETTINGS.gpu.api_key
SERVICE_HOST = SETTINGS.server.host
SERVICE_PORT = SETTINGS.server.port
OSS_CONFIG_PATH = SETTINGS.oss.config_path

# OSS 失效时的输入/输出中继限制。中继内容仍是浏览器端 CSE 密文。
RELAY_UPLOAD_MAX_BYTES = SETTINGS.relay.upload_max_bytes
RELAY_UPLOAD_TIMEOUT_SECONDS = SETTINGS.gpu.relay_upload_timeout_seconds
SESSION_COOKIE_NAME = SETTINGS.auth.cookie_name
SESSION_TTL_SECONDS = SETTINGS.auth.session_ttl_seconds

# ---------------------------------------------------------------------------
# 初始化 OSS
# ---------------------------------------------------------------------------

try:
    if not SETTINGS.oss.enabled:
        raise DirectOssUploadError("disabled by configuration")
    OSS_UPLOADER = DirectOssUpload(load_oss_storage_config(OSS_CONFIG_PATH))
    OSS_UPLOADER.ensure_sdk_available()
    print(f"[logic] OSS enabled prefix={OSS_UPLOADER.config.input_prefix}", flush=True)
except DirectOssUploadError as exc:
    OSS_UPLOADER = None
    print(f"[logic] OSS disabled: {exc}", flush=True)


# ---------------------------------------------------------------------------
# 任务队列（内存）
# ---------------------------------------------------------------------------

_TASK_LOCK = threading.Lock()
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_TTL_SECONDS = SETTINGS.queue.task_ttl_seconds
_MAX_TASKS_PER_USER = SETTINGS.queue.max_tasks_per_user
_ESTIMATED_SECONDS_PER_TASK = SETTINGS.queue.estimated_seconds_per_task
# GPU 连接异常（隧道抖动）时任务重试策略，避免一次抖动就让任务失败
_GPU_RETRY_MAX = SETTINGS.gpu.retry_max
_GPU_RETRY_DELAY_SECONDS = SETTINGS.gpu.retry_delay_seconds

_RESULTS_BY_OWNER: dict[str, list[dict[str, str]]] = {}
_RELAY_RESULT_OWNERS: dict[str, tuple[str, float]] = {}


def _is_transient_gpu_error(exc: Exception) -> bool:
    """判断是否属于隧道/GPU 连接类瞬时错误（可重试），而非 GPU 明确返回的业务失败。"""
    return isinstance(
        exc,
        (httpx.TransportError, httpx.TimeoutException, httpx.RemoteProtocolError),
    )


def _generation_owner_key(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not _is_valid_session_token(token):
        return "unknown"
    return hashlib.sha256(token.encode()).hexdigest()


def _cleanup_tasks_locked():
    cutoff = time.monotonic() - _TASK_TTL_SECONDS
    stale = [tid for tid, t in _TASKS.items() if t["updated_at"] < cutoff]
    for tid in stale:
        _TASKS.pop(tid, None)


# ---------------------------------------------------------------------------
# Session / Auth
# ---------------------------------------------------------------------------

def _create_session_token() -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{expires_at}.{uuid.uuid4().hex}"
    signature = hmac.new(
        SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _is_valid_session_token(token: str) -> bool:
    try:
        parts = token.split(".")
        if len(parts) == 2:
            expires_at_text, signature = parts
            payload = expires_at_text
        elif len(parts) == 3:
            expires_at_text, nonce, signature = parts
            if not nonce:
                return False
            payload = f"{expires_at_text}.{nonce}"
        else:
            return False
        expires_at = int(expires_at_text)
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return expires_at > int(time.time()) and hmac.compare_digest(signature, expected)


def _has_valid_session(request: Request) -> bool:
    return _is_valid_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))


def require_session(request: Request) -> str:
    """FastAPI 依赖：验证 session 并返回 owner_key。"""
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not _is_valid_session_token(token):
        raise HTTPException(status_code=401, detail={"error": "activation required"})
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# OSS 签发
# ---------------------------------------------------------------------------

_OSS_ISSUED_LOCK = threading.Lock()
_OSS_ISSUED_KEYS: dict[str, dict[str, Any]] = {}
_OSS_ISSUED_KEY_TTL_SECONDS = SETTINGS.oss.issued_key_ttl_seconds


def _oss_or_503() -> DirectOssUpload:
    if OSS_UPLOADER is None:
        raise HTTPException(status_code=503, detail={"error": "OSS unavailable"})
    return OSS_UPLOADER


def _oss_object_is_encrypted(uploader: DirectOssUpload, object_key: str) -> bool:
    """判断 OSS 对象是否带 CSE 密文元数据（有则为密文，无法做图片校验）。"""
    try:
        headers = uploader._get_bucket().head_object(object_key).headers
        lower = {k.lower(): v for k, v in headers.items()}
        return bool(lower.get("x-oss-meta-crypto-iv") and lower.get("x-oss-meta-crypto-wk"))
    except Exception:  # noqa: BLE001
        return False


def _safe_filename(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem).strip("._")
    return f"{(cleaned or 'image')[:80]}{suffix}"


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(
    title="异常图片生成模型 — 逻辑层",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 上传缓存静态服务
_CACHE_DIR = SETTINGS.static.cache_dir
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/cache", StaticFiles(directory=str(_CACHE_DIR)), name="cache")
_DEMO_DIR = SETTINGS.static.demo_dir
if _DEMO_DIR.exists():
    app.mount("/demo_example", StaticFiles(directory=str(_DEMO_DIR)), name="demo")


# ---------------------------------------------------------------------------
# 内置参考图 → OSS（前端传的 reference_key 可能是 demo_example/ 静态路径，
# GPU 服务只能从 OSS 下载，所以需要在任务入队前把本地文件导入 OSS）
# ---------------------------------------------------------------------------

_DEMO_REF_CACHE: dict[str, str] = {}

def _resolve_demo_reference(reference_key: str) -> str:
    """如果 reference_key 指向 demo_example/ 内置参考图，则导入 OSS 并返回真实 OSS key。"""
    if not reference_key.startswith("demo_example/"):
        return reference_key
    if reference_key in _DEMO_REF_CACHE:
        return _DEMO_REF_CACHE[reference_key]
    if OSS_UPLOADER is None:
        raise HTTPException(status_code=400, detail={"error": "OSS 未启用，无法使用内置参考图"})
    filename = Path(reference_key).name
    local = _DEMO_DIR / filename
    if not local.exists():
        raise HTTPException(status_code=400, detail={"error": f"内置参考图不存在: {reference_key}"})
    object_key = f"{OSS_UPLOADER.config.input_prefix}/demo/{filename}"
    try:
        bucket = OSS_UPLOADER._get_bucket()
        bucket.put_object_from_file(object_key, str(local))
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"内置参考图导入 OSS 失败: {exc}"})
    # 轮询等待对象可见（OSS 偶发一致性延迟），不可见则直接报错
    verify_path = _CACHE_DIR / f"demo_{uuid.uuid4().hex}{local.suffix}"
    try:
        OSS_UPLOADER.import_object(object_key, verify_path)
        verify_path.unlink(missing_ok=True)
    except DirectOssUploadError as exc:
        raise HTTPException(status_code=502, detail={"error": f"内置参考图导入后不可见: {exc}"})
    print(f"[logic] demo reference {reference_key} -> OSS {object_key}", flush=True)
    _DEMO_REF_CACHE[reference_key] = object_key
    return object_key


@app.exception_handler(HTTPException)
async def http_exc_handler(_: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(detail)})


# --- 激活码 ---

_ACTIVATION_LOCK = threading.Lock()
_ACTIVATION_FAILURES: dict[str, list[float]] = {}
_ACTIVATION_MAX_ATTEMPTS = SETTINGS.auth.activation_max_attempts
_ACTIVATION_WINDOW_SECONDS = SETTINGS.auth.activation_window_seconds


def _activation_client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    if request.client:
        return request.client.host
    return "unknown"


@app.post("/api/auth/activate")
async def api_activate(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid JSON"})
    code = (body.get("code") or "").strip()
    client_key = _activation_client_key(request)

    now = time.monotonic()
    with _ACTIVATION_LOCK:
        failures = [
            ts for ts in _ACTIVATION_FAILURES.get(client_key, [])
            if now - ts < _ACTIVATION_WINDOW_SECONDS
        ]
        _ACTIVATION_FAILURES[client_key] = failures
        if len(failures) >= _ACTIVATION_MAX_ATTEMPTS:
            return JSONResponse(
                status_code=429,
                content={"error": "尝试次数过多，请 15 分钟后再试"},
            )

    if len(code) != 5 or not hmac.compare_digest(code, ACTIVATION_CODE):
        with _ACTIVATION_LOCK:
            _ACTIVATION_FAILURES.setdefault(client_key, []).append(now)
            if len(_ACTIVATION_FAILURES[client_key]) >= _ACTIVATION_MAX_ATTEMPTS:
                return JSONResponse(
                    status_code=429,
                    content={"error": "尝试次数过多，请 15 分钟后再试"},
                )
        return JSONResponse(status_code=401, content={"error": "激活码无效"})

    with _ACTIVATION_LOCK:
        _ACTIVATION_FAILURES.pop(client_key, None)

    token = _create_session_token()
    # The session is intentionally delivered only as an HttpOnly cookie.
    # Returning the signed value in JSON would make it available to XSS code.
    response = JSONResponse(content={"ok": True})
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    secure = forwarded_proto.split(",")[0].strip() == "https" if forwarded_proto else False
    response.set_cookie(
        SESSION_COOKIE_NAME, token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return response


# --- OSS 上传策略 ---

@app.post("/api/oss/upload-policy")
async def create_upload_policy(request: Request, owner: str = Depends(require_session)):
    uploader = _oss_or_503()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid JSON"})
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"error": "body must be an object"})

    filename = payload.get("filename", "")
    size = payload.get("size", 0)
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail={"error": "invalid filename"})
    if not isinstance(size, int) or not 0 < size <= uploader.config.max_upload_bytes:
        raise HTTPException(status_code=400, detail={"error": "invalid size"})

    object_key = uploader.make_object_key(owner, filename)
    policy = uploader.create_upload_policy(object_key)

    with _OSS_ISSUED_LOCK:
        stale = [k for k, r in _OSS_ISSUED_KEYS.items() if r["expires_at"] <= time.monotonic()]
        for k in stale:
            _OSS_ISSUED_KEYS.pop(k, None)
        _OSS_ISSUED_KEYS[object_key] = {
            "owner": owner,
            "filename": filename,
            "expires_at": time.monotonic() + _OSS_ISSUED_KEY_TTL_SECONDS,
        }
    return JSONResponse(policy)


@app.post("/api/oss/import")
async def import_oss_upload(request: Request, owner: str = Depends(require_session)):
    uploader = _oss_or_503()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid JSON"})
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, list) or not 1 <= len(keys) <= 16:
        raise HTTPException(status_code=400, detail={"error": "keys must be a list of 1-16 keys"})

    with _OSS_ISSUED_LOCK:
        stale = [k for k, r in _OSS_ISSUED_KEYS.items() if r["expires_at"] <= time.monotonic()]
        for k in stale:
            _OSS_ISSUED_KEYS.pop(k, None)
        records = [_OSS_ISSUED_KEYS.get(k) for k in keys]
        if any(r is None or r["owner"] != owner for r in records):
            raise HTTPException(status_code=403, detail={"error": "key not issued to this session"})

    # 从 OSS 下载到本地缓存
    import tempfile
    cache_dir = Path(tempfile.gettempdir()) / "anomaly_logic_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    urls = []
    for key, rec in zip(keys, records):
        dest = cache_dir / f"{uuid.uuid4().hex}{Path(rec['filename']).suffix}"
        uploader.import_object(key, dest)
        # 校验图片（CSE 密文无法校验，跳过）
        if not _oss_object_is_encrypted(uploader, key):
            try:
                from PIL import Image
                with Image.open(dest) as img:
                    img.verify()
            except Exception:
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail={"error": f"not a valid image: {rec['filename']}"})
        # 返回相对路径，前端通过 /cache/ 访问
        urls.append(f"/cache/{dest.name}")

    # OSS 对象保留不删，GPU 服务还需要下载
    with _OSS_ISSUED_LOCK:
        for key in keys:
            _OSS_ISSUED_KEYS.pop(key, None)

    return JSONResponse({"urls": urls})


# --- OSS 代理上传（本地开发兜底，绕过 CORS）---

@app.post("/api/oss/proxy-upload")
async def proxy_upload(
    request: Request,
    file: UploadFile = File(...),
    crypto_iv: str = Form(""),
    crypto_wk: str = Form(""),
    crypto_kid: str = Form(""),
    owner: str = Depends(require_session),
):
    """服务端代理上传：浏览器 → 逻辑服务 → OSS。用于 CORS 不可用的场景。

    内容为 CSE 密文时带上 crypto_iv / crypto_wk / crypto_kid，逻辑服务器只负责存储
    不透传私钥，密文无法校验图片内容，直接返回 key。
    """
    uploader = _oss_or_503()
    if not file.filename:
        raise HTTPException(status_code=400, detail={"error": "no file"})

    content = await file.read()
    if len(content) > uploader.config.max_upload_bytes:
        raise HTTPException(status_code=400, detail={"error": "file too large"})

    object_key = uploader.make_object_key(owner, file.filename or "image.png")

    # 直传到 OSS（服务端，不走浏览器 CORS）
    import oss2
    headers = {}
    if crypto_iv and crypto_wk:
        headers["x-oss-meta-crypto-iv"] = crypto_iv
        headers["x-oss-meta-crypto-wk"] = crypto_wk
    if crypto_kid:
        headers["x-oss-meta-crypto-kid"] = crypto_kid
    try:
        bucket = uploader._get_bucket()
        if headers:
            bucket.put_object(object_key, content, headers=headers)
        else:
            bucket.put_object(object_key, content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"OSS proxy upload failed: {exc}"})

    return JSONResponse({"key": object_key, "url": ""})


# --- OSS 不可用时的 GPU 中继上传 ---
@app.post("/api/relay/upload")
async def relay_upload(
    file: UploadFile = File(...),
    crypto_iv: str = Form(""),
    crypto_wk: str = Form(""),
    crypto_kid: str = Form(""),
    owner: str = Depends(require_session),
):
    """浏览器 -> 逻辑服务器 -> SSH/GPU；密文不在逻辑服务器持久化。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail={"error": "no file"})
    if not crypto_iv or not crypto_wk:
        raise HTTPException(status_code=400, detail={"error": "crypto_iv and crypto_wk are required"})

    content = await file.read(RELAY_UPLOAD_MAX_BYTES + 1)
    if len(content) > RELAY_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail={"error": "file too large"})

    files = {
        "file": (
            file.filename,
            content,
            "application/octet-stream",
        ),
    }
    data = {
        "crypto_iv": crypto_iv,
        "crypto_wk": crypto_wk,
        "crypto_kid": crypto_kid,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(RELAY_UPLOAD_TIMEOUT_SECONDS)
        ) as client:
            resp = await client.post(
                f"{GPU_SERVICE_URL}/relay/upload",
                files=files,
                data=data,
                headers={"Authorization": f"Bearer {GPU_SERVICE_API_KEY}"},
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": f"GPU relay upload failed: {exc}"}) from exc

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = {"error": resp.text[:200] or f"HTTP {resp.status_code}"}
        status_code = resp.status_code if 400 <= resp.status_code < 500 else 502
        raise HTTPException(status_code=status_code, detail=detail)

    result = resp.json()
    print(f"[logic] relay upload forwarded owner={owner[:12]} key={result.get('key', '')}", flush=True)
    return JSONResponse(result)


# --- 用户公钥注册（CSE：生成结果用用户公钥加密，浏览器私钥解密）---

_USER_PUBKEYS: dict[str, str] = {}

@app.post("/api/keys")
async def register_public_key(request: Request, owner: str = Depends(require_session)):
    """注册用户公钥（浏览器 WebCrypto 生成，私钥只存浏览器本地）。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid JSON"})
    pem = payload.get("public_key_pem", "") if isinstance(payload, dict) else ""
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        raise HTTPException(status_code=400, detail={"error": "invalid public key"})
    _USER_PUBKEYS[owner] = pem
    print(f"[logic] registered public key for owner={owner[:12]}", flush=True)
    return JSONResponse({"ok": True})


# --- 站点公钥（上传加密用，逻辑层代理 GPU 当前公钥，短缓存）---

_PUBKEY_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_PUBKEY_TTL_SECONDS = SETTINGS.gpu.public_key_cache_ttl_seconds

@app.get("/api/crypto/public-key")
async def get_public_key():
    """返回当前站点公钥（kid + PEM），前端用它加密上传。"""
    now = time.monotonic()
    if _PUBKEY_CACHE["data"] and now - _PUBKEY_CACHE["ts"] < _PUBKEY_TTL_SECONDS:
        return _PUBKEY_CACHE["data"]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(SETTINGS.gpu.public_key_timeout_seconds)
        ) as client:
            resp = await client.get(
                f"{GPU_SERVICE_URL}/public-key",
                headers={"Authorization": f"Bearer {GPU_SERVICE_API_KEY}"},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail={"error": "GPU 公钥获取失败"})
            data = resp.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": f"GPU 公钥获取失败: {exc}"})
    _PUBKEY_CACHE["ts"] = now
    _PUBKEY_CACHE["data"] = data
    return data


# --- 任务提交 ---

_RELAY_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _relay_id_from_key(key: str) -> str | None:
    if not isinstance(key, str) or not key.startswith("relay://"):
        return None
    relay_id = key[8:]
    return relay_id if _RELAY_ID_RE.fullmatch(relay_id) else None


def _public_result_url(result_key: str, result_url: str) -> tuple[str, str | None]:
    relay_id = _relay_id_from_key(result_key)
    if relay_id:
        return f"/api/relay/result/{relay_id}", relay_id
    return result_url, None


@app.get("/api/relay/result/{relay_id}")
async def relay_result(
    relay_id: str,
    owner: str = Depends(require_session),
):
    """把 GPU 本地中继结果通过逻辑层返回给同一会话的浏览器。"""
    if not _RELAY_ID_RE.fullmatch(relay_id):
        raise HTTPException(status_code=404, detail={"error": "relay result not found"})
    record = _RELAY_RESULT_OWNERS.get(relay_id)
    if not record or record[0] != owner:
        raise HTTPException(status_code=404, detail={"error": "relay result not found"})
    if time.monotonic() - record[1] > SETTINGS.relay.result_ttl_seconds:
        _RELAY_RESULT_OWNERS.pop(relay_id, None)
        raise HTTPException(status_code=404, detail={"error": "relay result expired"})

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(SETTINGS.gpu.relay_result_timeout_seconds)
        ) as client:
            resp = await client.get(
                f"{GPU_SERVICE_URL}/relay/result/{relay_id}",
                headers={"Authorization": f"Bearer {GPU_SERVICE_API_KEY}"},
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={"error": f"GPU relay result unavailable: {exc}"}) from exc

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail={"error": "GPU relay result unavailable"})
    media_type = resp.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
    return Response(content=resp.content, media_type=media_type)


@app.post("/api/tasks")
async def submit_task(request: Request, owner: str = Depends(require_session)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "invalid JSON"})

    required = ["background_key", "reference_key", "mask_key"]
    if not all(k in body for k in required):
        raise HTTPException(status_code=400, detail={"error": f"missing required fields: {required}"})

    with _TASK_LOCK:
        _cleanup_tasks_locked()
        # 只统计排队中和生成中的任务，已完成的清掉
        for tid in list(_TASKS.keys()):
            t = _TASKS[tid]
            if t.get("owner") == owner and t["state"] in ("done", "failed"):
                _TASKS.pop(tid, None)
        user_tasks = [t for t in _TASKS.values() if t.get("owner") == owner]
        if len(user_tasks) >= _MAX_TASKS_PER_USER:
            raise HTTPException(
                status_code=429,
                detail={"error": f"你已有 {len(user_tasks)} 个任务在队列中，最多允许 {_MAX_TASKS_PER_USER} 个"},
            )

        user_task_index = len(user_tasks) + 1
        ahead = len(_TASKS)
        task_id = uuid.uuid4().hex

        _TASKS[task_id] = {
            "state": "queued",
            "owner": owner,
            "user_task_index": user_task_index,
            "ddim_steps": body.get("ddim_steps", 50),
            "guidance_scale": body.get("guidance_scale", 7.5),
            "seed": body.get("seed", 42),
            "background_key": body["background_key"],
            "reference_key": _resolve_demo_reference(body["reference_key"]),
            "mask_key": body["mask_key"],
            "roi": body.get("roi"),
            "created_at": time.monotonic(),
            "updated_at": time.monotonic(),
            "result_url": None,
            "error": None,
            "retries": 0,
        }

        if len(user_tasks) == 0:
            _RESULTS_BY_OWNER.pop(owner, None)

        waiting_count = sum(1 for t in _TASKS.values() if t["state"] == "queued")

    if ahead:
        est_wait = ahead * _ESTIMATED_SECONDS_PER_TASK
        est_str = _format_eta(est_wait)
        # 计算该用户最前面的排队任务还要排多少
        user_ahead = sum(1 for t in _TASKS.values()
                        if t["state"] in ("queued", "generating") and t["created_at"] < _TASKS[task_id]["created_at"])
        msg = f"前面还有 {user_ahead} 个任务，预计等待 {est_str}（你的第 {user_task_index} 个任务）"
    else:
        msg = f"正在生成中（你的第 {user_task_index} 个任务）"

    # 异步启动处理
    asyncio.create_task(_process_next_task())

    print(
        f"[task:{task_id[:8]}] submitted ahead={ahead} "
        f"user_task={user_task_index} owner={owner[:12]}",
        flush=True,
    )

    return JSONResponse({
        "task_id": task_id,
        "status": _TASKS[task_id]["state"],
        "queue_ahead": ahead,
        "user_task_index": user_task_index,
        "message": f"{msg} | 当前有 {waiting_count} 个任务正在排队",
    })


async def _process_next_task():
    """从队列取下一个等待任务，发送到 GPU 服务。"""
    with _TASK_LOCK:
        # 检查是否已有正在生成的任务
        generating = [t for t in _TASKS.values() if t["state"] == "generating"]
        if generating:
            return  # 已有任务在执行，不需要启动新的
        waiting = [
            (tid, t) for tid, t in _TASKS.items()
            if t["state"] == "queued"
        ]
        if not waiting:
            return
        # 按创建时间排序，取最早
        waiting.sort(key=lambda x: x[1]["created_at"])
        task_id, task = waiting[0]
        task["state"] = "generating"
        task["updated_at"] = time.monotonic()

    print(f"[task:{task_id[:8]}] sending to GPU service", flush=True)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(SETTINGS.gpu.generate_timeout_seconds)
        ) as client:
            resp = await client.post(
                f"{GPU_SERVICE_URL}/generate",
                json={
                    "background_key": task["background_key"],
                    "reference_key": task["reference_key"],
                    "mask_key": task["mask_key"],
                    "roi": task.get("roi"),
                    "ddim_steps": task["ddim_steps"],
                    "guidance_scale": task["guidance_scale"],
                    "seed": task["seed"],
                    "task_id": task_id,
                    "user_public_key": _USER_PUBKEYS.get(task["owner"]),
                },
                headers={"Authorization": f"Bearer {GPU_SERVICE_API_KEY}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                result_key = data.get("result_key", "")
                result_url, relay_id = _public_result_url(
                    result_key,
                    data.get("result_url", ""),
                )
                with _TASK_LOCK:
                    task["state"] = "done"
                    task["result_key"] = result_key
                    task["relay_result_id"] = relay_id
                    task["result_url"] = result_url
                    task["crypto_iv"] = data.get("crypto_iv")
                    task["crypto_wk"] = data.get("crypto_wk")
                    task["updated_at"] = time.monotonic()
                    if relay_id:
                        _RELAY_RESULT_OWNERS[relay_id] = (task["owner"], time.monotonic())
                # 追加到结果画廊
                owner = task["owner"]
                _RESULTS_BY_OWNER.setdefault(owner, []).append({
                    "url": result_url,
                    "crypto_iv": data.get("crypto_iv"),
                    "crypto_wk": data.get("crypto_wk"),
                    "label": f"任务 #{task['user_task_index']} | seed={task['seed']}",
                })
                print(
                    f"[task:{task_id[:8]}] done "
                    f"elapsed={data.get('elapsed_seconds', '?')}s",
                    flush=True,
                )
            else:
                raise RuntimeError(f"GPU service returned {resp.status_code}: {resp.text}")
    except Exception as exc:
        transient = _is_transient_gpu_error(exc)
        retries = task.get("retries", 0)
        if transient and retries < _GPU_RETRY_MAX:
            # 隧道/GPU 瞬时不可达：任务回队列，稍后重试，不判失败
            with _TASK_LOCK:
                task["state"] = "queued"
                task["retries"] = retries + 1
                task["updated_at"] = time.monotonic()
            print(
                f"[task:{task_id[:8]}] GPU 连接异常，回队重试 {retries + 1}/{_GPU_RETRY_MAX}: {exc}",
                flush=True,
            )
            await asyncio.sleep(_GPU_RETRY_DELAY_SECONDS)
        else:
            with _TASK_LOCK:
                task["state"] = "failed"
                task["error"] = str(exc)
                task["updated_at"] = time.monotonic()
            print(f"[task:{task_id[:8]}] failed: {exc}", flush=True)
    finally:
        # 继续处理下一个
        asyncio.create_task(_process_next_task())


# --- 任务查询 ---

@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, owner: str = Depends(require_session)):
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
    if task is None or task["owner"] != owner:
        raise HTTPException(status_code=404, detail={"error": "task not found"})

    with _TASK_LOCK:
        ahead = sum(
            1 for t in _TASKS.values()
            if t["state"] in ("queued",) and t["created_at"] < task["created_at"]
        )

    return {
        "task_id": task_id,
        "status": task["state"],
        "user_task_index": task.get("user_task_index"),
        "queue_ahead": ahead,
        "result_url": task.get("result_url"),
        "crypto_iv": task.get("crypto_iv"),
        "crypto_wk": task.get("crypto_wk"),
        "error": task.get("error"),
    }


@app.get("/api/tasks")
async def list_tasks(owner: str = Depends(require_session)):
    with _TASK_LOCK:
        user_tasks = [
            {
                "task_id": tid,
                "status": t["state"],
                "user_task_index": t.get("user_task_index"),
                "created_at": t["created_at"],
            }
            for tid, t in _TASKS.items()
            if t.get("owner") == owner and t["state"] in ("queued", "generating")
        ]
        total_queued = sum(1 for t in _TASKS.values() if t["state"] == "queued")
        total_generating = sum(1 for t in _TASKS.values() if t["state"] == "generating")
        results = _RESULTS_BY_OWNER.get(owner, [])

    return {
        "tasks": user_tasks,
        "results": results,
        "total_queued": total_queued,
        "max_tasks_per_user": _MAX_TASKS_PER_USER,
    }


# --- 健康检查 ---

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "oss_enabled": OSS_UPLOADER is not None,
        "relay_enabled": True,
        "gpu_service_url": GPU_SERVICE_URL,
        "queued_tasks": sum(1 for t in _TASKS.values() if t["state"] == "queued"),
    }


# --- 工具函数 ---

def _format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    if seconds < 1:
        return "不足 1 秒"
    if seconds < 60:
        return f"约 {seconds} 秒"
    m, s = divmod(seconds, 60)
    if s:
        return f"约 {m} 分 {s} 秒"
    return f"约 {m} 分钟"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVICE_HOST, port=SERVICE_PORT)
