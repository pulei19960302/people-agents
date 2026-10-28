import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol


class TokenCounter(Protocol):
    """上下文Token计数器协议。"""

    def count_text(self, text: str) -> int:
        ...

    def count_message(self, message: dict[str, Any]) -> int:
        ...


class HeuristicTokenCounter:
    """未配置模型Tokenizer时使用的兼容降级实现。"""

    def count_text(self, text: str) -> int:
        if not text:
            return 0

        return max(1, len(text) // 3)

    def count_message(self, message: dict[str, Any]) -> int:
        return self.count_text(_serialize_message(message))


class ModelTokenCounter:
    """使用模型官方 ``tokenizer.json`` 进行真实Token计数。

    ``tokenizer.json`` 应来自当前实际使用的模型。该类只读取本地文件，
    不会在应用启动时访问Hugging Face或自动下载模型文件。
    """

    def __init__(self, tokenizer_path: str) -> None:
        path = Path(tokenizer_path).expanduser().resolve()

        if not path.is_file():
            raise ValueError(f"模型Tokenizer文件不存在: {path}")

        self._tokenizer = _load_tokenizer(str(path))

    def count_text(self, text: str) -> int:
        if not text:
            return 0

        return len(self._tokenizer.encode(text).ids)

    def count_message(self, message: dict[str, Any]) -> int:
        # 对完整消息结构编码，确保tool_calls、arguments等字段也被计入。
        return self.count_text(_serialize_message(message))


def create_token_counter(
    tokenizer_path: str | None,
) -> TokenCounter:
    """根据配置创建计数器；未配置时保留字符估算策略。"""

    if tokenizer_path:
        return ModelTokenCounter(tokenizer_path)

    return HeuristicTokenCounter()


def estimate_text_tokens(text: str) -> int:
    """兼容旧调用的Token估算函数。"""

    return HeuristicTokenCounter().count_text(text)


def estimate_message_tokens(
    message: dict[str, Any],
) -> int:
    """兼容旧调用的消息Token估算函数。"""

    return HeuristicTokenCounter().count_message(message)


def _serialize_message(message: dict[str, Any]) -> str:
    return json.dumps(
        message,
        ensure_ascii=False,
        separators=(",", ":"),
    )


@lru_cache(maxsize=8)
def _load_tokenizer(tokenizer_path: str) -> Any:
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise RuntimeError(
            "已配置context_tokenizer_path，但未安装tokenizers依赖"
        ) from exc

    return Tokenizer.from_file(tokenizer_path)
