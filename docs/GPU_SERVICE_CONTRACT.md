# GPU Service Contract

[简体中文](GPU_SERVICE_CONTRACT.zh-CN.md)

Version: `1`

This document defines the private HTTP contract between the logic
layer and the GPU service. The browser never calls the GPU service directly.
The Logic layer reaches it through an SSH forward, normally:

```text
Logic service 127.0.0.1:19944
        -> SSH forward
GPU service 127.0.0.1:7861
```

## Responsibility boundaries

| Layer | Owns | Must not own |
|---|---|---|
| Browser | user session, image compression, CSE encryption/decryption of results | site private key |
| Logic layer | public API, activation/session, task queue, OSS policy, GPU HTTP client | site private key, model weights |
| GPU API layer | authentication, relay/OSS object access, request validation, orchestration | user session state |
| CSE service | site key rotation, RSA-OAEP and AES-GCM operations | HTTP routing, model loading |
| Model runtime | private model loading and `generate()` | OSS credentials, browser/session handling |

The GPU service keeps plaintext only for the time needed to validate and run
inference. Model weights, checkpoints, site private keys, and local archives
are runtime inputs and must stay outside a public repository.

## Authentication and transport

- The Logic layer sends `Authorization: Bearer <GPU_SERVICE_API_KEY>`.
- The API key is shared only by the SSH-connected Logic and GPU processes.
- The GPU port binds to `127.0.0.1`; it is not exposed to the public internet.
- Request and response bodies use UTF-8 JSON unless an endpoint is multipart or
  returns an image.
- The Logic layer must use a long timeout for `/generate` and retry only
  transport failures. A valid 4xx response is a business error, not a retry.

## Endpoints

### `GET /health`

Unauthenticated liveness/readiness probe. The response includes:

```json
{
  "status": "ok",
  "protocol_version": "1",
  "model_ready": true,
  "cuda_available": true,
  "device": "cuda",
  "oss_enabled": true,
  "relay_enabled": true
}
```

`model_ready=false` is allowed only in smoke mode or during a future startup
state. The logic layer must not submit generation tasks unless the service is
ready.

### `GET /public-key`

Requires the GPU Bearer API key. Returns the current site public key for browser-side input encryption:

```json
{
  "kid": "12-hex-character-id",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----...",
  "date": "2026-08-26"
}
```

The Logic layer caches this response briefly and exposes it as
`GET /api/crypto/public-key`. The private key never crosses this boundary.

### `POST /relay/upload`

Multipart upload used only when OSS is unavailable. Required parts:

- `file`: CSE ciphertext bytes
- `crypto_iv`: base64 AES-GCM IV
- `crypto_wk`: base64 RSA-OAEP wrapped data key
- `crypto_kid`: optional site key id

Response:

```json
{"key": "relay://<32-lowercase-hex-id>", "url": ""}
```

The GPU stores the ciphertext temporarily and associates the metadata with the
relay id. The Logic layer passes this `relay://` key to `/generate`.

### `POST /generate`

Required JSON fields:

```json
{
  "background_key": "OSS object key or relay:// id",
  "reference_key": "OSS object key or relay:// id",
  "mask_key": "OSS object key or relay:// id",
  "roi": {"x": 0, "y": 0, "width": 128, "height": 128},
  "ddim_steps": 50,
  "guidance_scale": 7.5,
  "seed": 42,
  "output_format": "jpeg",
  "task_id": "<Logic task id>",
  "user_public_key": "<browser PEM public key>"
}
```

The GPU reads each object, decrypts CSE metadata with the site private key,
preprocesses the three images, and invokes the model runtime. It must not
accept a plaintext user image over this API in the normal production path.

Response:

```json
{
  "result_key": "openoctopus/output/results/...jpg",
  "result_url": "https://signed-oss-url/...",
  "elapsed_seconds": 9.3,
  "crypto_iv": "<base64>",
  "crypto_wk": "<base64>"
}
```

When result upload to OSS fails, `result_key` is `relay://<id>` and the
Logic layer exposes the result as `/api/relay/result/<id>`. The result is
encrypted with `user_public_key`, so the browser decrypts it with its own
private key.

### `GET /relay/result/{id}`

Returns a relay result as image bytes. It requires the GPU API key and is called
only by the Logic layer. Input relay objects are deleted after a successful
generation; all relay objects expire after the configured TTL.

## Failure semantics

| Condition | GPU response | Logic behavior |
|---|---:|---|
| Missing/invalid API key | `401` | fail the internal request |
| Invalid request or CSE metadata | `400` | mark task failed |
| Relay object missing/expired | `404` | mark task failed |
| GPU/SSH transport timeout | no response / transport error | return task to queue and retry |
| OSS input/output unavailable | generation may continue with relay | use `relay://` path |
| Model not loaded | `503` | retry after readiness check, then fail |

## Deployment rules

The only public-facing service is the Logic layer behind Nginx. The SSH
forward must use `ExitOnForwardFailure=yes`, keepalive options, and bind the
remote listener to loopback. A deployment should verify `/health` after the
tunnel is established and before accepting user tasks.

The public repository may contain this contract, the API layer, smoke checks,
and a mock adapter. It must exclude private runtime artifacts, site private
keys, OSS credentials, local plaintext archives, and real activation codes.


## Configuration

The GPU process accepts `--config /path/to/gpu.toml` or `GPU_CONFIG_PATH`. The file contains non-secret deployment paths and behavior. Environment variables override TOML values; `GPU_SERVICE_API_KEY` is always read from the environment and must not be written to the repository.

Use `runtime.backend = "mock"` or `runtime.smoke_mode = true` for API/relay smoke checks. Use `runtime.backend = "external"` on a private GPU deployment and set `runtime.module` and `runtime.module_path` to load an external inference adapter outside the public repository.

The recommended production command is:

```bash
GPU_SERVICE_API_KEY='...' python -m gpu_service.server --config /etc/anomaly-gpu/gpu.toml
```
