# 运行与开发

准备 Python 3.12、uv 和专用 MySQL 8.0 数据库，复制 `.env.example` 为 `.env` 并填写数据库连接与音频目录。真实配置不提交 Git。

```text
uv sync --frozen
uv run --frozen alembic upgrade head
uv run --frozen uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

访问 http://127.0.0.1:8000/docs 查看当前接口。迁移前停止应用并备份数据库与音频。

提交前运行 `uv run --frozen ruff check .` 与 `git diff --check`，核对配置和文档是否与实现一致。
