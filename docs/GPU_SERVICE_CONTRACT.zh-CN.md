# GPU 服务契约

[English](GPU_SERVICE_CONTRACT.md)

版本：`1`

本文档定义逻辑服务与 GPU 服务之间的内部 HTTP 契约。浏览器不会直接调用
GPU 服务，逻辑层通过 SSH 转发访问它：

```text
逻辑服务 127.0.0.1:19944
        -> SSH 转发
GPU 服务 127.0.0.1:7861
```

## 职责边界

| 层 | 负责 | 不负责 |
|---|---|---|
| 浏览器 | 用户会话、图片压缩、CSE 加密和结果解密 | 站点私钥 |
| 逻辑层 | 公共 API、激活/会话、任务队列、OSS 策略、GPU HTTP 客户端 | 站点私钥、模型权重 |
| GPU API 层 | 鉴权、中继/OSS 对象访问、请求校验、任务编排 | 用户会话状态 |
| CSE 服务 | 站点密钥轮换、RSA-OAEP 和 AES-GCM 操作 | HTTP 路由、模型加载 |
| 模型运行时 | 私有模型加载和 `generate()` | OSS 凭证、浏览器会话 |

GPU 服务只在校验和推理所需的时间内接触明文。模型权重、checkpoint、
站点私钥和本地存档都是运行时输入，必须放在公开仓库之外。

## 鉴权与传输

- 逻辑层发送 `Authorization: Bearer <GPU_SERVICE_API_KEY>`。
- API key 只在 SSH 连接两端的逻辑进程和 GPU 进程之间共享。
- GPU 端口绑定 `127.0.0.1`，不暴露到公网。
- 除 Multipart 或图片响应接口外，请求和响应均使用 UTF-8 JSON。
- `/generate` 必须使用较长超时；只对传输失败重试。合法的 `4xx` 响应
  属于业务错误，不应重试。

## 接口

### `GET /health`

无需鉴权的存活/就绪检查：

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

只有在 smoke 模式或未来的启动阶段才允许 `model_ready=false`。逻辑层在
服务未就绪时不应提交生成任务。

### `GET /public-key`

需要 GPU Bearer API key。返回当前站点公钥，供浏览器加密输入：

```json
{
  "kid": "12-hex-character-id",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----...",
  "date": "2026-08-26"
}
```

逻辑层会短暂缓存该响应，并通过 `GET /api/crypto/public-key` 代理给浏览器。
站点私钥不会跨越该边界。

### `POST /relay/upload`

仅在 OSS 不可用时使用的 Multipart 上传接口。字段：

- `file`：CSE 密文文件
- `crypto_iv`：base64 编码的 AES-GCM IV
- `crypto_wk`：base64 编码的 RSA-OAEP 封装数据密钥
- `crypto_kid`：可选的站点密钥标识

响应：

```json
{"key":"relay://<32-lowercase-hex-id>","url":""}
```

GPU 会临时保存密文及其元数据。逻辑层把返回的 `relay://` key 传给
`/generate`。

### `POST /generate`

请求 JSON：

```json
{
  "background_key": "OSS 对象 key 或 relay:// id",
  "reference_key": "OSS 对象 key 或 relay:// id",
  "mask_key": "OSS 对象 key 或 relay:// id",
  "roi": {"x":0,"y":0,"width":128,"height":128},
  "ddim_steps": 50,
  "guidance_scale": 7.5,
  "seed": 42,
  "output_format": "jpeg",
  "task_id": "<逻辑层任务 id>",
  "user_public_key": "<浏览器 PEM 公钥>"
}
```

GPU 读取对象，使用站点私钥处理 CSE 元数据，完成图片预处理并调用模型
运行时。正常生产路径不应通过该接口接收用户明文图片。

响应：

```json
{
  "result_key": "output/results/...jpg",
  "result_url": "https://signed-oss-url/...",
  "elapsed_seconds": 9.3,
  "crypto_iv": "<base64>",
  "crypto_wk": "<base64>"
}
```

当 OSS 结果上传失败时，`result_key` 为 `relay://<id>`，逻辑层把它暴露为
`/api/relay/result/<id>`。结果使用 `user_public_key` 加密，浏览器用自己的
私钥解密。

### `GET /relay/result/{id}`

返回 GPU 中继结果的图片密文，需要 GPU API key，由逻辑层调用。生成成功后
输入中继对象会被删除；所有中继对象也会在配置 TTL 到期后清理。

## 失败语义

| 条件 | GPU 响应 | 逻辑层行为 |
|---|---:|---|
| API key 缺失/错误 | `401` | 内部请求失败 |
| 请求或 CSE 元数据无效 | `400` | 标记任务失败 |
| 中继对象不存在/过期 | `404` | 标记任务失败 |
| GPU/SSH 传输超时 | 无响应/传输错误 | 任务回队列并重试 |
| OSS 输入/输出不可用 | 生成可继续使用中继 | 使用 `relay://` 路径 |
| 模型未加载 | `503` | 检查就绪状态后重试，最终失败 |

## 部署规则

唯一的公网服务是 Nginx 后面的逻辑层。SSH 转发必须使用
`ExitOnForwardFailure=yes`、keepalive 选项，并将远程监听绑定到 loopback。
隧道建立后、接受任务前，应先检查 GPU `/health`。

公开仓库可以包含契约、API 层、冒烟检查和 mock runtime，但必须排除私有
运行时资源、站点私钥、OSS 凭证、本地明文存档和真实激活码。

## 配置

GPU 进程通过 `--config /path/to/gpu.toml` 或 `GPU_CONFIG_PATH` 选择配置文件。
配置文件保存非敏感部署路径和行为；环境变量覆盖 TOML，
`GPU_SERVICE_API_KEY` 始终从环境中读取，不应写入仓库。

API/relay 冒烟检查可使用 `runtime.backend = "mock"` 或
`runtime.smoke_mode = true`。私有 GPU 部署使用
`runtime.backend = "external"`，并通过 `runtime.module` 与
`runtime.module_path` 从仓库外加载推理适配器。

推荐启动方式：

```bash
GPU_SERVICE_API_KEY='...' \
python -m gpu_service.server --config /etc/anomaly-gpu/gpu.toml
```
