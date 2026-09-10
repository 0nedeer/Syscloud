# 运行与开发

## 环境与配置

使用 Python 3.12、uv、MySQL 8.0.16+，推荐 MySQL 8.0.44；容器方式需要 Docker 和 Compose。

复制 `.env.example` 为 `.env` 并填写真实配置。环境变量优先于文件；不要覆盖已有密钥。环境文件、数据库、音频、缓存和本机记录均由 Git 忽略，提交前检查暂存区。

| 配置 | 说明与默认值 |
| --- | --- |
| DATABASE_URL | mysql+asyncmy 连接串，指定独立库；用户名密码中的特殊字符需 URL 编码 |
| UPLOAD_DIR | 本地音频目录，默认 ./data/recordings；API/Worker 必须一致 |
| MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD | Compose 初始化配置；密码建议使用随机 URL 安全字符 |
| API_PORT | Compose 对本机监听端口，默认 8000 |
| LLM_BASE_URL / LLM_MODEL / LLM_API_KEY | Worker 必填的真实模型配置；API 无需模型密钥 |
| LLM_API_STYLE | responses 或 chat_completions，默认 responses |
| LLM_REASONING_EFFORT | 可选 low/medium/high；仅在模型支持时填写 |
| LLM_PROXY_URL | 可选显式 HTTP/HTTPS 代理；不自动读取系统代理 |
| LLM_TIMEOUT_SECONDS / LLM_CONNECT_TIMEOUT_SECONDS | 整体 60 秒、连接 5 秒 |
| LLM_MAX_RESPONSE_BYTES | 完整响应最大 262144 字节 |
| WORKER_CONCURRENCY / WORKER_POLL_SECONDS | 每进程最多 3 并发，默认每秒扫描 |
| WORKER_LEASE_SECONDS / WORKER_HEARTBEAT_SECONDS | 租约 120 秒，心跳 10 秒 |
| WORKER_SHUTDOWN_SECONDS / WORKER_CLEANUP_SECONDS | 关闭宽限 10 秒，删除补偿间隔 10 秒 |
| WORKER_RETRY_BASE_SECONDS | 自动重试退避基数 2 秒，最多 3 次 |

数据库连接、读取和连接池等待默认各 5 秒，就绪检查整体默认 3 秒。完整参数边界由配置模型在启动时校验。

## Docker Compose

```text
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail 50 api worker
```

使用 `.env.docker` 时每条命令加 `--env-file .env.docker`。应用镜像不包含真实环境文件；LLM 密钥只注入 Worker。数据库不暴露宿主机端口，API 仅绑定 127.0.0.1。容器代理地址须能从容器访问，不能直接使用宿主机回环地址。

MySQL 与音频分别保存在命名卷中。普通 `docker compose down` 保留卷；不要使用 `down -v` 清理业务数据。已有 MySQL 卷不会因环境变量改动而自动更改账号或密码。


## 本地 Python

先创建专用数据库与业务用户，授予该库运行和迁移所需权限，字符集 utf8mb4。将连接串写入 `.env`，不要在命令行传入密码。

```text
uv sync --frozen
uv run --frozen alembic upgrade head
uv run --frozen uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

另开终端运行 `uv run --frozen python -m app.worker`。Windows 也可分别使用 `scripts/start-local.ps1` 与 `scripts/start-worker.ps1`。

## 第一次接口闭环

1. 打开 http://127.0.0.1:8000/docs ，确认 `/health/ready` 返回 ready。
2. 使用 `POST /v1/recordings` 上传允许扩展名的非空文件，保存返回的 recording_id 和 task_id。
3. 轮询任务接口；默认 Mock ASR 需要等待 5–15 秒，失败时会自动退避。done 后核对三个摘要字段。
4. 查询录音详情和分页；重复上传相同字节应返回 200 及相同初始任务。
5. failed 任务可发起手动重试；重复请求复用后继任务，done 或进行中任务返回 409。
6. 删除刚上传的录音，返回 204；之后查询为 404。保留原有业务数据。

可使用 [HTTP 调试文件](../requests/recordings.http) 顺序调用，将 ID 替换为实际响应值。示例上传内容仅供 Mock ASR 调试，不是可播放的录音。

## 迁移、维护与提交

迁移前停止 API/Worker，备份数据库和音频。依次执行基础表、自动重试、内容哈希迁移，当前 head 为 `0003_recording_hash`。哈希迁移须访问 UPLOAD_DIR，发现文件缺失、大小不符或重复内容会停止，需人工处理后继续；MySQL 多条 DDL 不具备整体事务回滚能力。

只读文件对账：

```text
uv run --frozen python scripts/audit-storage.py
```

该工具输出缺失引用、孤立文件、临时文件等分类，不自动删除文件。应在停写后复核，避免把正在上传的文件误判为孤立文件。

提交前检查格式、静态问题及差异：

```text
uv run --frozen ruff check .
uv run --frozen ruff format --check .
git diff --check
git status --short
```

按业务功能组织具体的中文提交，使用 feat/fix/refactor/docs/chore 标头；代码和对应说明一起维护。只提交可复用的项目文件，本机配置与审阅记录存放于项目之外。

排障先检查就绪接口、迁移版本、Worker 脱敏日志和任务最近错误。API 就绪不保证模型可达；模型协议、权限、代理和超时需按供应商实际能力配置。
