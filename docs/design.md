# 架构与接口设计

## 架构

API 负责参数校验与 HTTP 协议，业务服务管理事务，独立 Worker 领取任务并执行 Mock ASR 和真实 LLM。MySQL 8.0.16+ / InnoDB 同时保存业务数据及持久化队列，推荐 8.0.44；API 和 Worker 共享音频目录。

采用 FastAPI、Pydantic 2、SQLAlchemy 2、asyncmy、Alembic、HTTPX。数据库会话不跨协程共享，外部调用和文件操作尽量放在短事务之外，不引入通用仓储框架或消息总线。

## 数据模型

ID 使用应用生成的 UUID，存为 CHAR(36)；时间为 UTC DATETIME(6)，接口返回带 Z 的 ISO 8601。字符集 utf8mb4，路径仅由服务端生成，原始文件名只供展示。

| 表 | 字段组 | 作用 |
| --- | --- | --- |
| recordings | id、original_filename、storage_key、size_bytes | 文件元数据，storage_key 唯一，大小受 CHECK 约束 |
| recordings | content_sha256 | 唯一内容哈希，删除清理完成前保留占位 |
| recordings | created_at、updated_at、deleted_at | 时间及删除意图；有效查询排除删除中的录音 |
| tasks | id、recording_id、attempt_no、retry_of_task_id | 关联录音、手动尝试序号及来源；来源唯一 |
| tasks | status、transcript、summary_result | 状态与处理结果，摘要为 JSON |
| tasks | error_code、error_message | 终态错误代码与脱敏描述 |
| tasks | lease_token、lease_expires_at、recovery_count | 所有权令牌、到期时间与恢复次数 |
| tasks | auto_retry_count、next_attempt_at、last_error_code、last_error_message | 自动重试预算、调度与最近失败 |
| tasks | created_at、updated_at、started_at、finished_at | 创建、修改、首次开始和结束时间 |

`tasks.recording_id` 外键级联删除；`(recording_id, attempt_no)` 唯一，同一来源任务只允许一个直接后继。来源 ID 不使用自引用外键，存在性和录音归属由事务保证。最新任务按最大 attempt_no 选择。

索引支持有效录音倒序分页、按状态领取、过期租约恢复和到期重试。迁移按基础表、自动重试、内容哈希顺序演进，数据库约束同时限制合法状态、大小与次数。存量哈希迁移要求停写并提供原音频，重复或缺失文件需人工处理后继续。

## 状态与恢复

正常路径为 `pending → transcribing → summarizing → done`。转写提交时持久化 transcript 与 summarizing 状态；摘要通过严格校验后与 done 原子提交。done 和 failed 为终态，手动重试创建新的 pending 任务。

领取先发现候选，再按“录音 → 任务”顺序加锁复查，使用 FOR UPDATE SKIP LOCKED 跳过被占用行。领取生成新令牌和到期时间后提交，再执行耗时操作。续租及结果写入均检查录音有效、当前阶段、令牌匹配、租约未过期。

默认租约 120 秒，每 10 秒心跳；租约至少覆盖三个心跳间隔。心跳失败时取消执行，旧令牌不得覆盖新执行结果。未完成任务在租约过期后继续原阶段，summarizing 复用已提交的转写。外部服务不参与数据库事务，因此语义为至少一次调用。

每个任务共享 3 次自动重试预算，初次执行不计入。失败时保持当前阶段，写入最近错误与下次时间，释放租约和并发槽位；默认延迟 2/4/8 秒。预算耗尽后的失败写为 failed。进程重启不重置预算或退避时间，租约恢复单独计数。

每进程最多 3 个任务并发。关闭时停止领取，默认等待 10 秒后取消活动协程，剩余工作由租约恢复。删除补偿循环独立执行，默认每 10 秒扫描一次。

## 文件一致性

上传分块读取并统计实际字节数，同步计算 SHA-256，使用随机临时文件，校验后原子重命名。录音与初始任务同事务写入。重复内容由数据库唯一约束仲裁，返回已有录音及初始任务，并清理本次重复文件；删除中的同内容返回冲突。

连接在提交时中断不能直接推断回滚，上传通过预先生成的 ID 查询提交结果。仍无法确认时保留文件，避免误删已提交记录的音频，使用只读存储审计核查遗留文件。

删除先持久化 deleted_at，阻止查询、领取及写回；随后删除文件，再硬删除录音并级联任务。失败保留标记，由重复删除请求或 Worker 补偿。文件系统与数据库不能原子提交，崩溃后的孤立文件需要人工核查。

## HTTP 契约

| 方法与路径 | 成功响应 | 主要规则 |
| --- | --- | --- |
| POST /v1/recordings | 202；重复内容 200 | multipart file，返回 recording_id、task_id、status |
| GET /v1/tasks/{task_id} | 200 | 返回状态、结果、错误、重试次数和等待信息 |
| GET /v1/recordings | 200 | page 默认 1，page_size 默认 20、范围 1–100；返回 items、page、page_size、total |
| GET /v1/recordings/{recording_id} | 200 | 元数据、latest_task、transcript、summary_result |
| POST /v1/tasks/{task_id}/retry | 202；复用后继 200 | 来源必须 failed，返回新任务 ID 和 attempt_no |
| DELETE /v1/recordings/{recording_id} | 204 | 文件及关联数据清理成功；不存在为 404 |
| GET /health/live | 200 | 进程存活 |
| GET /health/ready | 200 或 503 | 有界数据库连接及迁移版本检查 |

摘要对象恰好包含非空 `summary` 字符串、`key_points` 字符串数组和 `todos` 字符串数组；空待办使用 []。严格拒绝字段缺失、额外字段、类型转换及 Markdown 围栏。摘要仅在 done 后对外发布。

统一错误形如 `{"error":{"code":"...","message":"...","request_id":"..."}}`。参数错误 400，资源不存在 404，状态或内容清理冲突 409，文件超限 413，基础设施不可用 503，未预期错误 500。日志通过 request_id、recording_id、task_id 串联，不输出密钥、连接串、转写、摘要或供应商原始响应。

OpenAPI 使用 `ErrorResponse` 描述统一错误；各响应的 status 共用 `TaskStatus` 枚举。框架参数校验统一返回 400，接口文档不声明默认的 422 响应。request_id 同时出现在错误对象和 X-Request-ID 响应头中；数据库驱动未提供错误号时返回脱敏 500。

## 模型调用与边界

LLM 可选择 Responses 或 Chat Completions 协议，使用完整响应。显式配置地址、模型、密钥及可选推理强度；远程地址要求 HTTPS，代理单独配置。请求设置连接超时、整体期限及响应字节上限，错误转换为稳定脱敏代码。

Mock ASR 仅模拟耗时、随机失败和转写文本。无音频解码、鉴权、前端或公网部署；并发限制按 Worker 进程计算，多个进程不保证全局 3 并发。multipart 解析在业务检查前可能使用临时磁盘，运行环境需预留空间。
