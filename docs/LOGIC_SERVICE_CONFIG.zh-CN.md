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

## OSS 浏览器访问与 CORS

逻辑层和 GPU 服务通过 OSS SDK 以服务器到服务器的方式访问 OSS，因此
它们自身的请求不依赖浏览器 CORS。但正常路径下，浏览器有两类请求需要
OSS 允许跨域：

- 使用 OSS 策略 URL 进行 `POST` 直传；
- 使用签名结果 URL 进行 `GET`，让 JavaScript 读取密文并在本地解密，之后
  才能显示或下载图片。

请在受保护 OSS JSON 配置文件所引用的**准确 Bucket**上配置 CORS。新建
Bucket，或切换到另一位 OSS 用户后，新 Bucket 不会自动继承旧 Bucket 的
CORS 规则。

推荐配置：

| 配置项 | 值 |
|---|---|
| 允许来源 | 精确的 HTTPS 部署域名，例如 `https://your-domain.example` |
| 允许方法 | `GET`、`POST`、`PUT`、`DELETE`、`HEAD` |
| 允许 Headers | `*` |
| 暴露 Headers | `ETag`、`Content-Type`、`Content-Length` |
| 缓存时间 | 可先设置为 `600` 秒 |

阿里云控制台可能不会把 `OPTIONS` 列为可编辑方法，这是正常的：浏览器
发起的预检请求由 OSS 自动处理。应用对跨域签名 URL 使用
`credentials: omit`；生产环境仍建议填写精确来源，不要使用 `*`。

验证保存后的规则时，可以给短生命周期的签名结果 URL 加上 `Origin` 请求头；
不要把真实签名 URL 写入源码或日志：

```bash
curl -I \
  -H 'Origin: https://your-domain.example' \
  'https://bucket.example.invalid/path/to/signed-result'
```

响应中应出现部署域名对应的 `Access-Control-Allow-Origin`，并且
`Access-Control-Allow-Methods` 包含 `GET`。如果 CORS 缺失，GPU 仍可能已经
成功完成推理，但浏览器无法读取加密结果，于是页面显示破图，直接下载得到
的也是密文而不是可打开的 JPEG。

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
