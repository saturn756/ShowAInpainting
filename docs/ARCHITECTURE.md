# Anomaly Image Generation Service: Architecture

[简体中文](ARCHITECTURE.zh-CN.md)

Version: 2.1 | Updated: 2026-08-26

This document describes the deployable service boundary: a Vue frontend, a
public logic service, a private GPU API, encrypted object storage, an SSH relay
fallback, and local task archives. The versioned logic-to-GPU contract is in
[GPU_SERVICE_CONTRACT.md](GPU_SERVICE_CONTRACT.md).

## 1. Overall architecture

```text
                             Browser
       Vue SPA: activate -> upload -> mask/ROI -> generate -> decrypt result
          | HTTPS API and same-origin static assets
          | CSE ciphertext upload
          v
    Nginx / HTTPS / static frontend
          |
          v
    Public Logic service :8000
      | auth, upload policy, queue, task/result API
      |
      +-----------------------> Managed object storage
      |                         ciphertext only
      |
      +-- SSH reverse tunnel :19944 -> GPU API :7861
                                      | auth, relay, CSE, runtime
                                      | local archive
                                      v
                                  GPU host
```

Normal input and output traffic uses object storage. If object storage is
unavailable, the browser sends the same CSE ciphertext to the logic service;
the logic service forwards it through the loopback-bound SSH tunnel to
temporary GPU relay storage. The GPU decrypts only immediately before image
processing and inference. Results are encrypted for the browser before they
are uploaded or relayed back.

## 2. Component responsibilities

| Component | Technology | Default port | Responsibility |
|---|---|---:|---|
| Frontend | Vue 3, Vite, WebCrypto | dev: 3000 | Activation, previews, mask/ROI editing, encrypted uploads, task polling, result decryption and download |
| Logic service | FastAPI, Python | 8000 | Public API, HMAC sessions, OSS policy, user public keys, FIFO queue, GPU HTTP client, relay fallback |
| GPU service | FastAPI, CSE adapter, runtime loader | 7861 | Internal authentication, OSS/relay object access, CSE operations, runtime invocation, result output and archive |
| SSH tunnel | OpenSSH reverse forward | 19944 | Private transport between logic and GPU services |

The browser never calls the GPU API directly. The GPU service never owns public
user sessions, activation codes, or frontend routing.

## 3. Repository structure

```text
service_release/
├── frontend/
│   ├── src/                   # Vue components and browser-side workflows
│   └── vite.config.js         # local API proxy and build configuration
├── logic_service/
│   ├── main.py                # public HTTP API and task orchestration
│   ├── config.py              # TOML + environment configuration
│   └── requirements.txt
├── gpu_service/
│   ├── server.py              # private API orchestration
│   ├── config.py              # TOML + environment configuration
│   ├── contracts.py           # request/response schemas
│   ├── crypto_service.py      # site-key and CSE operations
│   ├── storage_service.py     # temporary relay object lifecycle
│   ├── runtime_protocol.py    # runtime interface
│   ├── runtime_loader.py      # external runtime selection
│   ├── mock_runtime.py        # smoke-test adapter
│   └── requirements.txt
├── configs/                   # non-secret TOML examples
├── deploy/                    # Nginx and systemd templates
├── scripts/                   # key rotation and OSS cleanup
├── oss_direct_upload.py       # OSS policy and object helper
└── docs/                      # API, architecture, contract, configuration
```

The production inference adapter, runtime artifacts, site keys, and local
archives are supplied outside this package through configuration and protected
deployment storage.

### 3.1 Configuration boundary

Both services select a TOML file with `--config /path/to/file.toml` or their
respective `*_CONFIG_PATH` variable. Environment variables override TOML
values. TOML contains ports, paths, timeouts, queue limits, relay limits, and
runtime selection. Secrets remain environment-only:

- `GRADIO_ACTIVATION_CODE`
- `GRADIO_SESSION_SECRET`
- `GPU_SERVICE_API_KEY`
- OSS credentials, unless read from a protected credential file
- site private key paths and private runtime paths in production

## 4. Authentication and sessions

1. The user submits a five-character activation code to
   `POST /api/auth/activate`.
2. The logic service compares it with `hmac.compare_digest` and rate-limits
   failed attempts by client address.
3. A successful activation creates an HMAC-SHA256 signed session token in an
   HttpOnly Cookie. The token is not returned in JSON or stored by frontend
   JavaScript.
4. Protected endpoints validate the cookie and derive an owner fingerprint
   from `sha256(token)`.
5. The frontend treats `401` responses as an expired session and returns to the
   activation view.

The default session lifetime is 12 hours. The cookie uses `Secure` when the
request reaches the logic layer through HTTPS and uses `SameSite=Lax`.

## 5. Client-side encryption

### 5.1 Purpose

Server-side encryption managed by an object-storage provider does not protect
objects from an administrator who controls the provider account. The browser
therefore encrypts the image before it reaches object storage or a relay.

### 5.2 Envelope format

Each input file uses the same format in JavaScript and Python:

```text
1. Generate a random 32-byte data key DK.
2. Encrypt the file with AES-256-GCM using a random 12-byte IV.
3. Append the 16-byte GCM authentication tag to the ciphertext.
4. Wrap DK with RSA-OAEP using SHA-256 and the site public key.
5. Upload ciphertext plus base64 IV/WK metadata.
```

OSS metadata names are:

```text
x-oss-meta-crypto-iv
x-oss-meta-crypto-wk
x-oss-meta-crypto-kid
```

The GPU service reverses the operation with the site private key. The logic
service forwards metadata but never receives that private key.

### 5.3 Two key pairs

| Key pair | Location | Purpose |
|---|---|---|
| Site key pair | Public key exposed through the logic API; private key only on GPU | Decrypt browser-encrypted input files |
| User key pair | Public key registered with logic service; private key remains in the browser | Encrypt generated results for the requesting browser |

The browser keeps its private key locally. Clearing browser storage can make
previously encrypted results unrecoverable.

### 5.4 Site-key rotation

The site key can rotate daily. The current public key response includes a key
identifier (`kid`), and historical private keys remain available on the GPU so
older objects can still be decrypted. Rotation affects input decryption only;
generated results use the user's key pair.

## 6. Upload data flow

```text
Plain browser file
    -> optional browser resize/compression
    -> AES-256-GCM + RSA-OAEP envelope
    -> ciphertext
       |
       +-> direct OSS POST using a short-lived policy
       |
       +-> logic OSS proxy if direct upload fails
       |
       +-> logic /api/relay/upload if OSS is unavailable
               -> SSH reverse tunnel
               -> GPU /relay/upload
               -> relay:// object key
```

The frontend attempts the three upload paths in order. It uses the local
plaintext only for immediate preview; it does not fetch an uploaded ciphertext
for preview.

The relay path stores ciphertext and CSE metadata temporarily on the GPU host.
The input relay object is deleted after a successful generation and all relay
objects are subject to TTL cleanup.

## 7. Task queue and logic-to-GPU request

The logic service keeps an in-memory FIFO queue. It records task state as:

```text
queued -> generating -> done | failed
```

The queue applies a per-session task limit and removes stale task records after
their configured TTL. Only one inference request is dispatched at a time in
the current implementation. Transport failures caused by an SSH or GPU
connection problem return the task to the queue for bounded retry; explicit
GPU `4xx` responses are treated as business failures.

The internal request contains object keys rather than image bytes:

```json
{
  "background_key": "OSS key or relay:// id",
  "reference_key": "OSS key or relay:// id",
  "mask_key": "OSS key or relay:// id",
  "roi": {"x":0,"y":0,"width":128,"height":128},
  "ddim_steps": 50,
  "guidance_scale": 7.5,
  "seed": 42,
  "task_id": "<logic-task-id>",
  "user_public_key": "<browser PEM public key>"
}
```

The complete public endpoint reference is in [API.md](API.md); the private
GPU contract is in [GPU_SERVICE_CONTRACT.md](GPU_SERVICE_CONTRACT.md).

## 8. GPU processing flow

1. Resolve each input key from OSS or GPU relay storage.
2. Read CSE metadata and decrypt into a short-lived local inference file.
3. Validate that the decrypted file is an image.
4. Normalize background, reference, and mask images for the runtime; apply the
   optional ROI.
5. Call the runtime through its `generate()` interface.
6. If a user public key is registered, encrypt the generated result with a new
   AES-GCM data key wrapped by that user key.
7. Upload the encrypted result to OSS, or store it in GPU relay storage when
   OSS output is unavailable.
8. Write a local task archive when archiving is enabled.

Plaintext is required on the GPU host for inference and archival. The design
does not claim to protect against compromise of the GPU host itself; host disk
encryption, a hardware-backed key store, and stricter archive retention are
separate hardening options.

## 9. Result flow

For the normal output path, the GPU returns an OSS object key, signed URL, and
result CSE metadata. The browser fetches the ciphertext from the signed URL and
decrypts it with its local private key.

For the fallback output path, the GPU returns a `relay://` key. The logic layer
maps that key to `/api/relay/result/{id}`, checks task ownership and TTL, then
streams the ciphertext to the browser. The browser decrypts both output paths
the same way.

## 10. Local archive

When enabled, each inference is saved under:

```text
data/anomaly_records/<date>/<owner_hash>/<task_id>/
├── 0_stitched.png       reference | background / mask | result
├── 1_reference.png      normalized reference image
├── 2_background.png     normalized background image
├── 3_mask.png           normalized mask image
├── 4_result.png         generated plaintext result
└── meta.json             parameters, keys, ROI, and timestamps
```

Archive failures are logged and do not block the response. Because this archive
contains plaintext, its directory is a private deployment path and must never
be committed.

## 11. Cleanup

The OSS cleanup script can delete objects older than a configured age and
supports `--dry-run`. Relay storage uses a TTL and is cleaned by the GPU
service. Site-key rotation retains historical keys deliberately so that older
encrypted inputs remain readable.

## 12. Deployment

### 12.1 SSH reverse tunnel

The tunnel is initiated from the logic host and binds both ends to loopback:

```bash
ssh -N \
  -R 127.0.0.1:19944:127.0.0.1:7861 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  gpu-host
```

The deployment should verify GPU `/health` after the tunnel is established and
before accepting generation tasks.

### 12.2 GPU service

```bash
GPU_SERVICE_API_KEY='replace-with-a-long-random-key' \
python -m gpu_service.server --config /etc/anomaly-gpu/gpu.toml
```

The GPU API should listen on `127.0.0.1:7861`. Production configuration sets
the external runtime module, site-key path, OSS settings, and archive
directory.

### 12.3 Logic service and Nginx

```bash
GRADIO_ACTIVATION_CODE='ABCDE' \
GRADIO_SESSION_SECRET='replace-with-at-least-32-random-characters' \
GPU_SERVICE_API_KEY='same-as-gpu-service' \
python -m logic_service.main --config /etc/anomaly-gen/logic.toml
```

Nginx serves the built frontend and proxies `/api/` and `/cache/` to the logic
service. The example unit files in `deploy/` provide a systemd deployment
shape; the GPU port and SSH listener should remain private.

## 13. API summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/auth/activate` | none | Exchange activation code for session cookie |
| GET | `/api/crypto/public-key` | none | Get current site public key |
| POST | `/api/keys` | session | Register browser result-encryption key |
| POST | `/api/oss/upload-policy` | session | Issue direct-upload policy |
| POST | `/api/oss/proxy-upload` | session | Upload ciphertext through logic host |
| POST | `/api/relay/upload` | session | Forward ciphertext to GPU relay |
| POST | `/api/tasks` | session | Enqueue generation task |
| GET | `/api/tasks/{id}` | session | Read task state and result metadata |
| GET | `/api/tasks` | session | List session tasks and results |
| GET | `/api/relay/result/{id}` | session | Proxy a GPU relay result |
| GET | `/api/health` | none | Read logic service health |

GPU-private endpoints are documented separately in
[GPU_SERVICE_CONTRACT.md](GPU_SERVICE_CONTRACT.md).

## 14. Security model

1. Object storage and the logic layer receive ciphertext rather than plaintext
   user images.
2. The GPU site private key is never sent to the browser, OSS, or logic layer.
3. The GPU API is authenticated and reachable only through the SSH tunnel.
4. Session cookies are HttpOnly, signed, time-limited, and rate-limited at
   activation.
5. Relay objects are temporary and ownership-checked before result streaming.
6. Plaintext exists on the GPU host during inference and in optional local
   archives; those are explicit private deployment responsibilities.

## 15. Internal dependency direction

```text
HTTP/API orchestration (server.py)
    ├── OSS and relay adapters
    ├── CSE service (crypto_service.py)
    └── runtime adapter (runtime_loader -> external runtime)
```

`server.py` owns HTTP orchestration but not cryptographic primitives or model
implementation. `crypto_service.py` owns local-file CSE operations but not
HTTP, OSS, relay, or sessions. The runtime adapter exposes only the stable
`generate()` interface. Replacing the private runtime should not require a
change to the public logic API or the GPU HTTP contract.
