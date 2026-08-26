# Logic Service Configuration

[简体中文](LOGIC_SERVICE_CONFIG.zh-CN.md)

The Logic service is the public-facing orchestration layer. It owns
activation/session handling, OSS upload policy, the in-memory FIFO queue, and
the HTTP client for the private GPU service.

## Startup

From the `service_release` directory:

```bash
GRADIO_ACTIVATION_CODE='ABCDE' \
GRADIO_SESSION_SECRET='at-least-32-character-secret' \
GPU_SERVICE_API_KEY='same-as-gpu-service' \
python -m logic_service.main --config /etc/anomaly-gen/logic.toml
```

An ASGI runner can select the file with `LOGIC_CONFIG_PATH` instead:

```bash
LOGIC_CONFIG_PATH=/etc/anomaly-gen/logic.toml \
  uvicorn logic_service.main:app
```

The recommended systemd template is
`deploy/logic-service.service.example`.

## Precedence

Values are resolved in this order:

1. `--config /path/to/logic.toml` or `LOGIC_CONFIG_PATH` selects the file.
2. Supported environment variables override TOML values.
3. TOML values override safe code defaults.

The following values are always environment-only:

- `GRADIO_ACTIVATION_CODE`
- `GRADIO_SESSION_SECRET`
- `GPU_SERVICE_API_KEY`
- OSS credentials, unless the protected OSS JSON file is used by `OSS_CONFIG_PATH`

The public repository may contain `configs/logic.example.toml`, but the
production file should live under `/etc/anomaly-gen/` with restricted
permissions.

## TOML Sections

| Section | Purpose |
|---|---|
| `[server]` | Bind host and port |
| `[auth]` | Cookie lifetime and activation rate limits |
| `[oss]` | OSS enablement, credential-file path, issued-key lifetime |
| `[gpu]` | SSH-tunnel URL, HTTP timeouts, and transport retry policy |
| `[relay]` | Input size and result relay lifetime |
| `[queue]` | Task TTL, per-user limit, and ETA estimate |
| `[static]` | Upload cache and optional deployment-provided demo directory |

## Deployment Boundary

```text
Browser
  -> Nginx / HTTPS
  -> Logic service / 8000
  -> SSH reverse tunnel / 19944
  -> GPU service / 7861
```

The Logic layer never receives the GPU site's private key and never imports
the private model runtime. It forwards the versioned HTTP contract and keeps
only short-lived relay request bytes in memory while sending them to GPU relay
storage.
