# Show AI Inpainting

[English](../README.md)

这是一个工业异常图像生成应用的可部署服务层。项目把前端、公共逻辑层、
GPU 服务 API、客户端加密、OSS 故障中继和部署配置从研究代码中分离出来，
便于作为独立工程维护和公开展示。

## 架构图预览

| 语言 | 静态预览 | 交互预览 |
|---|---|---|
| English | [PNG](diagrams/architecture.en.png) | [HTML](https://saturn756.github.io/ShowAInpainting/diagrams/architecture.en.html) |
| 简体中文 | [PNG](diagrams/architecture.zh-CN.png) | [HTML](https://saturn756.github.io/ShowAInpainting/diagrams/architecture.zh-CN.html) |

PNG 文件仍保留在仓库中，可作为静态预览下载。HTML 是由 GitHub Pages
工作流部署的独立交互式查看器，需要在仓库设置中将 Pages 来源设置为
GitHub Actions 一次。

模型权重、研究代码、私有运行时、站点私钥、OSS 凭证和生产数据不包含在
公开仓库中，而是通过部署环境和受保护配置注入。

## 架构概览

请求级时序图和详细数据流见
[架构与数据流](ARCHITECTURE.zh-CN.md)。

浏览器使用 AES-256-GCM 加密每个输入文件，再用 GPU 站点 RSA 公钥封装
数据密钥。OSS 和逻辑服务只接触密文。GPU 服务在推理前临时解密输入；
当 OSS 不可用时，浏览器将同一份密文发送到逻辑服务，再通过 SSH 隧道
转发到 GPU 中继存储。生成结果使用用户浏览器公钥加密后返回或写入 OSS。

## 界面预览

<p align="center">
  <img src="screenshots/01-overview.png" alt="应用总览" width="100%">
</p>

<p align="center"><em>应用总览与操作流程</em></p>

<table>
  <tr>
    <td width="50%" align="center">
      <img src="screenshots/02-input-and-roi.png" alt="图片输入与区域选择" width="100%">
      <br><em>图片输入与区域选择</em>
    </td>
    <td width="50%" align="center">
      <img src="screenshots/03-result.png" alt="生成结果" width="100%">
      <br><em>生成结果与任务控制</em>
    </td>
  </tr>
</table>

## 项目结构

```text
service_release/
├── frontend/                  Vue 3 + Vite 单页应用
├── logic_service/             公共 API、认证、队列、OSS 策略、中继
├── gpu_service/               私有 HTTP API、CSE、存储、运行时加载器
├── configs/                   非敏感 TOML 配置示例
├── deploy/                    Nginx 和 systemd 模板
├── docs/                      API、架构、契约和配置文档
├── scripts/                   密钥轮换和 OSS 清理工具
└── oss_direct_upload.py       Aliyun OSS 策略和对象工具
```

生产模型运行时通过 `gpu_service.runtime_loader` 加载。公开服务包不需要
包含私有运行时资源、私有运行时模块、站点私钥、OSS 凭证或明文存档。

## 本地开发

环境要求：Python 3.10+、Node.js 18+，以及各服务 requirements 文件中的
依赖。GPU 主机还需要私有运行时和 CUDA 环境；逻辑服务不需要模型依赖。

安装前端依赖并构建静态文件：

```bash
cd frontend
npm ci
npm run dev       # Vite 开发服务器
npm run build     # 产物：frontend/dist/
```

可以使用 mock GPU 运行时进行 API 和路由冒烟测试。mock 运行时只验证
服务启动和接口编排，不会产生模型结果：

```bash
GPU_SERVICE_API_KEY='replace-with-a-long-random-key' \
GPU_RUNTIME_BACKEND=mock \
SERVICE_SMOKE_MODE=1 \
GPU_OSS_ENABLED=0 \
python -m gpu_service.server --config configs/gpu.example.toml
```

在第二个终端启动逻辑层：

```bash
GRADIO_ACTIVATION_CODE='ABCDE' \
GRADIO_SESSION_SECRET='replace-with-at-least-32-random-characters' \
GPU_SERVICE_API_KEY='replace-with-a-long-random-key' \
GPU_SERVICE_URL='http://127.0.0.1:7861' \
DIRECT_OSS_UPLOAD_ENABLED=0 \
python -m logic_service.main --config configs/logic.example.toml
```

真实部署时，将 GPU 运行时切换为 `external`，并在私有 GPU TOML
配置中通过 `runtime.module` 与 `runtime.module_path` 指向外部推理适配器。不要将示例中的密钥、
OSS 凭证或包含敏感信息的私有路径提交到 Git。

## 生产部署流程

1. 将 `configs/gpu.example.toml` 复制为 GPU 主机上的受保护配置，并通过
   `EnvironmentFile` 提供 `GPU_SERVICE_API_KEY`。
2. 将 `configs/logic.example.toml` 复制为逻辑主机上的受保护配置，并通过
   受保护环境变量提供激活码、Session secret、GPU API key 和 OSS 凭证。
3. 在 GPU 主机 loopback 地址启动 `7861` 端口的 GPU API。
4. 从逻辑主机建立 SSH 反向隧道：

   ```bash
   ssh -N \
     -R 127.0.0.1:19944:127.0.0.1:7861 \
     -o ExitOnForwardFailure=yes \
     -o ServerAliveInterval=30 \
     gpu-host
   ```

5. 在逻辑主机 loopback 地址启动 `8000` 端口的逻辑服务。
6. 构建 `frontend/dist/`，部署到 Nginx 文档根目录，并将 `/api/` 和
   `/cache/` 代理到逻辑服务。

`deploy/` 中的 unit 文件展示了推荐的 systemd 边界。对外公开的只有
   HTTPS 后的逻辑服务；GPU 端口和 SSH 监听端口都应保持 loopback。

## 配置规则

两个服务都遵循相同的配置优先级：

```text
--config 路径或 *_CONFIG_PATH
  -> 环境变量覆盖
  -> TOML 配置
  -> 稳妥的应用默认值
```

TOML 只保存端口、路径、超时、队列限制、中继限制和运行时选择。激活码、
Session secret、GPU API key、OSS 凭证和站点私钥必须放在受保护环境或
受限文件中。

## 文档

| 主题 | English | 简体中文 |
|---|---|---|
| 公共 API | [API.md](API.md) | [API.zh-CN.md](API.zh-CN.md) |
| 架构与数据流 | [ARCHITECTURE.md](ARCHITECTURE.md) | [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md) |
| GPU 服务契约 | [GPU_SERVICE_CONTRACT.md](GPU_SERVICE_CONTRACT.md) | [GPU_SERVICE_CONTRACT.zh-CN.md](GPU_SERVICE_CONTRACT.zh-CN.md) |
| 逻辑服务配置 | [LOGIC_SERVICE_CONFIG.md](LOGIC_SERVICE_CONFIG.md) | [LOGIC_SERVICE_CONFIG.zh-CN.md](LOGIC_SERVICE_CONFIG.zh-CN.md) |

## 公开仓库边界

以下内容必须保留在 Git 仓库之外：

- 激活码、Session secret、GPU API key 和 OSS 凭证
- 受保护的 OSS 凭证 JSON 文件
- 站点私钥和密钥轮换状态
- 私有运行时资源、私有运行时模块和明文存档
- 生产日志和生成数据

仓库中的 `.gitignore` 已覆盖默认私有路径，但在公开仓库前仍应结合实际
部署目录进行一次检查。
