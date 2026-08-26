# 逻辑服务配置

[English](LOGIC_SERVICE_CONFIG.md)

逻辑服务是面向公网的任务编排层，负责激活/会话、OSS 上传策略、内存 FIFO
队列，以及访问私有 GPU 服务的 HTTP 客户端。

## 启动

在 `service_release` 根目录执行：

```bash
GRADIO_ACTIVATION_CODE='ABCDE' \
GRADIO_SESSION_SECRET='at-least-32-character-secret' \
GPU_SERVICE_API_KEY='same-as-gpu-service' \
python -m logic_service.main --config /etc/anomaly-gen/logic.toml
```

使用 ASGI runner 时，也可以通过 `LOGIC_CONFIG_PATH` 选择配置文件：

```bash
LOGIC_CONFIG_PATH=/etc/anomaly-gen/logic.toml \
  uvicorn logic_service.main:app
```

推荐使用 `deploy/logic-service.service.example` 作为 systemd 模板。

## 配置优先级

配置值按以下顺序解析：

1. `--config /path/to/logic.toml` 或 `LOGIC_CONFIG_PATH` 选择文件。
2. 支持的环境变量覆盖 TOML 值。
3. TOML 值覆盖代码中的稳妥默认值。

以下值始终只从环境中读取：

- `GRADIO_ACTIVATION_CODE`
- `GRADIO_SESSION_SECRET`
- `GPU_SERVICE_API_KEY`
- OSS 凭证，除非通过 `OSS_CONFIG_PATH` 指向受保护的 JSON 文件

公开仓库可以包含 `configs/logic.example.toml`，生产配置应放在
`/etc/anomaly-gen/` 并限制文件权限。

## TOML 配置区段

| 区段 | 用途 |
|---|---|
| `[server]` | 监听地址和端口 |
| `[auth]` | Cookie 生命周期和激活限流 |
| `[oss]` | OSS 开关、凭证文件路径、签发 key 生命周期 |
| `[gpu]` | SSH 隧道地址、HTTP 超时和传输重试策略 |
| `[relay]` | 输入大小限制和结果中继生命周期 |
| `[queue]` | 任务 TTL、单用户任务上限和预计等待时间 |
| `[static]` | 上传缓存和由部署环境提供的可选 demo 目录 |

## 部署边界

```text
浏览器
  -> Nginx / HTTPS
  -> 逻辑服务 / 8000
  -> SSH 反向隧道 / 19944
  -> GPU 服务 / 7861
```

逻辑层不会接收 GPU 站点私钥，也不会导入私有模型运行时。它只转发
版本化 HTTP 契约，并在发送到 GPU 中继存储时仅保留短生命周期的中继
请求数据。
