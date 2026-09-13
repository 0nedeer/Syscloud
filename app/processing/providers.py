"""Mock 转写提供者及可公开的处理错误。"""

import asyncio
import random


# 携带稳定错误码与可公开描述；不把供应商原始响应、密钥或本机路径保存为任务错误。
class ProcessingError(Exception):
    """Only stable codes and public messages may be persisted or logged."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MockASR:
    # 默认等待 5～15 秒，约 20% 概率失败；成功返回固定会议文本供后续 LLM 摘要。
    async def transcribe(self) -> str:
        await asyncio.sleep(random.uniform(5, 15))
        if random.random() < 0.2:
            raise ProcessingError("asr_failed", "Transcription failed.")
        return (
            "今天讨论录音转写服务的发布计划。小李负责在周五前完成接口联调，"
            "小王负责整理使用文档。团队决定先验证上传、转写和摘要完整流程，"
            "再核查服务重启后的处理情况。下周一复盘结果并确定发布安排。"
        )
