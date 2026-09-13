# 源码阅读指南

主线是“上传保存 → 创建任务 → Worker 领取 → Mock 转写 → 真实模型摘要 → 查询结果”。API 和 Worker 为独立进程，以 MySQL 中的任务作为协作入口。

| 路径 | 职责 |
| --- | --- |
| app/main.py | 应用工厂、生命周期、异常处理和路由注册 |
| app/config.py | 公共 Settings 与 WorkerSettings，启动时校验参数 |
| app/db.py | 连接池、会话、迁移就绪检查 |
| app/models.py | Recording、Task、状态枚举与数据库约束 |
| app/schemas.py | HTTP 响应及严格摘要契约 |
| app/api.py | 健康、录音、任务路由，参数和状态码 |
| app/services/uploads.py | 保存文件、提交录音和任务、去重、提交结果确认 |
| app/services/queries.py | 查询、分页及统一最新任务选择 |
| app/services/retries.py | failed 任务手动重试与幂等后继 |
| app/services/deletions.py | 删除标记、文件清理和关联数据删除 |
| app/storage.py | 安全本地路径、文件读写、哈希、只读对账 |
| app/processing/providers.py | Mock ASR 与脱敏处理异常 |
| app/processing/llm.py | 双协议模型请求、完整响应解析与摘要校验 |
| app/processing/queue.py | 短事务领取、所有权检查、续租、结果提交与退避 |
| app/worker.py | 并发调度、流水线、心跳、删除补偿和进程入口 |
| app/errors.py、app/logging.py | 统一错误和结构化日志 |
| migrations/ | 按顺序维护数据库结构 |
| requests/、scripts/ | HTTP 调试入口、启动及只读存储审计 |

先读 models 与 schemas，区分“存什么”和“返回什么”；再从 api.upload_recording 跟到 uploads.create_recording，理解文件保存和录音/任务事务。

之后读 Worker._pipeline：转写提交后进入 summarizing，模型返回完整摘要后提交 done。TaskQueue 管理所有状态写入，耗时操作不占用数据库事务。最后阅读 claim、_owned、fail，理解租约恢复与重试边界。

手动重试创建新任务，自动重试复用原任务；attempt_no 与 auto_retry_count 含义不同。查询通过最大 attempt_no 取得最新任务，旧失败任务保留到录音删除。

删除先写意图再做物理清理，上传在提交连接中断后确认提交结果。这些代码处理数据库和文件系统之间的一致性，不宜简单替换为一次写入或一次删除。

日志只传稳定 ID、阶段、耗时与脱敏错误。扩展业务时保持路由、事务、外部调用的职责边界，涉及数据库结构时配套迁移，修改接口时同步响应模型和 HTTP 示例。
