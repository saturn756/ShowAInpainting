# 公共 API

[English](API.md)

浏览器只访问公共逻辑服务，通常与前端使用同一个 HTTPS 域名。GPU API
属于内部接口，详见 [GPU_SERVICE_CONTRACT.zh-CN.md](GPU_SERVICE_CONTRACT.zh-CN.md)。

## 认证

### `POST /api/auth/activate`

请求：

```json
{"code":"ABCDE"}
```

成功时返回 `{"ok":true}`，同时设置 HttpOnly 会话 Cookie。签名后的会话值
不会放在 JSON 中，前端 JavaScript 不应保存它。激活码错误返回 `401`，
连续失败会触发限流。

### `GET /api/health`

无需认证的逻辑服务存活检查：

```json
{
  "status": "ok",
  "oss_enabled": true,
  "relay_enabled": true,
  "gpu_service_url": "http://127.0.0.1:19944",
  "queued_tasks": 0
}
```

该接口不会建立用户会话。

## 浏览器密钥注册

### `GET /api/crypto/public-key`

返回用于加密输入文件的当前站点公钥：

```json
{
  "kid": "12-hex-character-id",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----...",
  "date": "2026-08-26"
}
```

对应的私钥永远不会离开 GPU 主机。

### `POST /api/keys`

需要会话 Cookie。浏览器注册自己的公钥，GPU 服务使用该公钥为当前浏览器
加密生成结果：

```json
{
  "public_key_pem": "-----BEGIN PUBLIC KEY-----..."
}
```

浏览器私钥只保留在该浏览器本地。

## 文件上传

所有输入文件都会先在浏览器中加密，再进行上传。CSE 元数据使用
`crypto_iv`、`crypto_wk` 和可选的 `crypto_kid` 传递。

### `POST /api/oss/upload-policy`

需要会话 Cookie。请求：

```json
{
  "filename": "background.png",
  "contentType": "application/octet-stream",
  "size": 123456
}
```

响应包含短期有效的 OSS POST 策略：

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

浏览器将响应中的 fields、CSE 元数据和密文文件直接 POST 到返回的 OSS
地址。逻辑服务会记录已签发的 object key，只有签发它的会话可以使用。

### `POST /api/oss/proxy-upload`

需要会话 Cookie。Multipart 字段：

| 字段 | 必填 | 含义 |
|---|---:|---|
| `file` | 是 | CSE 密文 |
| `crypto_iv` | CSE 时必填 | AES-GCM IV，base64 |
| `crypto_wk` | CSE 时必填 | 封装后的数据密钥，base64 |
| `crypto_kid` | 否 | 站点密钥标识 |

当浏览器无法直连 OSS、但逻辑主机仍可以访问 OSS 时，使用该服务端代理
上传路径。

### `POST /api/relay/upload`

需要会话 Cookie，使用与代理上传相同的 Multipart 字段。逻辑服务通过 SSH
隧道把密文转发到 GPU 中继存储。响应：

```json
{"key":"relay://<32-lowercase-hex-id>","url":""}
```

前端按以下顺序尝试上传：

```text
OSS 直传 -> 逻辑层 OSS 代理 -> GPU 中继
```

## 任务

### `POST /api/tasks`

需要会话 Cookie。请求：

```json
{
  "background_key": "OSS key 或 relay:// key",
  "reference_key": "OSS key 或 relay:// key",
  "mask_key": "OSS key 或 relay:// key",
  "roi": {"x":0,"y":0,"width":128,"height":128},
  "ddim_steps": 50,
  "guidance_scale": 7.5,
  "seed": 42
}
```

`roi` 可选。响应会创建一个内存 FIFO 任务：

```json
{
  "task_id": "<hex-id>",
  "status": "queued",
  "queue_ahead": 0,
  "user_task_index": 1,
  "message": "..."
}
```

逻辑服务会在任务入队后再调用内部 GPU 契约。传输失败按照逻辑层 TOML
中的策略重试；GPU 明确返回的 `4xx` 会被视为任务失败。

### `GET /api/tasks/{task_id}`

需要同一会话。状态响应：

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

当结果使用已注册的浏览器公钥加密时，响应包含结果 CSE 字段。浏览器获取
`result_url` 后，在本地解密返回的密文。

### `GET /api/tasks`

需要会话。返回当前会话的活动任务、已完成结果画廊、队列计数和配置的
单用户任务上限。

### `GET /api/relay/result/{relay_id}`

需要创建该任务的同一会话。逻辑服务从 GPU 中继接口取回临时结果，并将
密文流式返回浏览器，同时检查归属和 TTL。

## 错误处理

| 状态码 | 含义 |
|---:|---|
| `400` | JSON、文件元数据或请求格式错误 |
| `401` | 需要激活或激活码错误 |
| `403` | 当前会话不拥有该对象或密钥 |
| `404` | 任务或中继结果不存在/已过期 |
| `413` | 中继上传超过配置的大小限制 |
| `429` | 激活限流或单用户任务数达到上限 |
| `502` | 私有 GPU 或 OSS 上游不可用 |
| `503` | 配置的 OSS 路径不可用 |
