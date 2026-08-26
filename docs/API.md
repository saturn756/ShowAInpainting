# Public API

[简体中文](API.zh-CN.md)

The browser talks only to the public logic service, normally through the same
HTTPS origin as the frontend. The GPU API is private and is documented in
[GPU_SERVICE_CONTRACT.md](GPU_SERVICE_CONTRACT.md).

## Authentication

### `POST /api/auth/activate`

Request:

```json
{"code":"ABCDE"}
```

On success the response is `{"ok":true}` and the server sets an HttpOnly
session cookie. The signed session value is not returned in JSON and should not
be stored by frontend JavaScript. Invalid codes return `401`; repeated failed
attempts are rate limited.

### `GET /api/health`

Unauthenticated service liveness response:

```json
{
  "status": "ok",
  "oss_enabled": true,
  "relay_enabled": true,
  "gpu_service_url": "http://127.0.0.1:19944",
  "queued_tasks": 0
}
```

This endpoint does not establish a user session.

## Browser key registration

### `GET /api/crypto/public-key`

Returns the current site public key used to encrypt input files:

```json
{
  "kid": "12-hex-character-id",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----...",
  "date": "2026-08-26"
}
```

The corresponding private key never leaves the GPU host.

### `POST /api/keys`

Requires the session cookie. The browser registers its own public key so that
the GPU service can encrypt generated results for that browser:

```json
{
  "public_key_pem": "-----BEGIN PUBLIC KEY-----..."
}
```

The browser private key remains local to that browser.

## Uploads

All input files are encrypted in the browser before upload. The CSE metadata is
carried as `crypto_iv`, `crypto_wk`, and optional `crypto_kid` values.

### `POST /api/oss/upload-policy`

Requires the session cookie. Request:

```json
{
  "filename": "background.png",
  "contentType": "application/octet-stream",
  "size": 123456
}
```

The response contains the short-lived OSS POST policy:

```json
{
  "url": "https://oss.example.invalid",
  "key": "input/gradio/<owner>/<object>.png",
  "fields": {
    "key": "...",
    "OSSAccessKeyId": "...",
    "policy": "...",
    "Signature": "...",
    "success_action_status": "204"
  },
  "expiresAt": 0
}
```

The browser posts the returned fields, the CSE metadata fields, and the
ciphertext file directly to the returned OSS URL. The logic service tracks the
issued key so that only the issuing session can use it.

### `POST /api/oss/proxy-upload`

Requires the session cookie. Multipart fields:

| Field | Required | Meaning |
|---|---:|---|
| `file` | yes | CSE ciphertext |
| `crypto_iv` | yes for CSE | AES-GCM IV, base64 |
| `crypto_wk` | yes for CSE | wrapped data key, base64 |
| `crypto_kid` | no | site key identifier |

This is the server-side OSS fallback used when browser-to-OSS upload is not
available but the logic host can still reach OSS.

### `POST /api/relay/upload`

Requires the session cookie and uses the same multipart fields as proxy upload.
The logic service forwards the ciphertext through the SSH tunnel to the GPU
relay store. Response:

```json
{"key":"relay://<32-lowercase-hex-id>","url":""}
```

The frontend tries these paths in order:

```text
direct OSS -> logic OSS proxy -> GPU relay
```

## Tasks

### `POST /api/tasks`

Requires the session cookie. Request:

```json
{
  "background_key": "OSS key or relay:// key",
  "reference_key": "OSS key or relay:// key",
  "mask_key": "OSS key or relay:// key",
  "roi": {"x":0,"y":0,"width":128,"height":128},
  "ddim_steps": 50,
  "guidance_scale": 7.5,
  "seed": 42
}
```

`roi` is optional. The response creates an in-memory FIFO task:

```json
{
  "task_id": "<hex-id>",
  "status": "queued",
  "queue_ahead": 0,
  "user_task_index": 1,
  "message": "..."
}
```

The logic service sends the task to the private GPU contract only after it is
queued. A transport failure is retried according to the logic TOML settings;
an explicit GPU `4xx` is treated as a task failure.

### `GET /api/tasks/{task_id}`

Requires the same session. Status response:

```json
{
  "task_id": "<hex-id>",
  "status": "queued | generating | done | failed",
  "queue_ahead": 0,
  "result_url": "https://signed-url-or-/api/relay/result/id",
  "crypto_iv": "...",
  "crypto_wk": "...",
  "error": null
}
```

The result crypto fields are present when the result is encrypted for the
registered browser key. The browser fetches `result_url` and decrypts the
returned bytes locally.

### `GET /api/tasks`

Requires the session. Returns the session's active tasks, completed result
gallery entries, queue counters, and configured per-user task limit.

### `GET /api/relay/result/{relay_id}`

Requires the same session that created the task. The logic service fetches the
temporary result from the GPU relay endpoint and streams the ciphertext back to
the browser. Ownership and TTL are checked before forwarding.

## Error handling

| Status | Meaning |
|---:|---|
| `400` | malformed JSON, invalid file metadata, or invalid request |
| `401` | activation required or invalid activation code |
| `403` | object/key is not owned by the current session |
| `404` | task or relay result is missing/expired |
| `413` | relay upload exceeds configured size limit |
| `429` | activation or per-user task limit reached |
| `502` | private GPU or OSS upstream is unavailable |
| `503` | configured OSS path is unavailable |
