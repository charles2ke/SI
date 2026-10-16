"""Small extensible guardrails; heuristics are NOT production PII guarantees.

Injection detection is defense in depth, not a security boundary. Tool permissions,
approval, isolation and data access controls must be enforced independently.
"""

import json
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from si.models import ChatRequest


class GuardrailError(ValueError):
    """Input was rejected by a guardrail."""


Stage = Callable[[str], str]


class Guardrails:
    def __init__(
        self,
        *,
        max_chars: int = 32000,
        max_tokens: int = 16000,
        blocked_terms: Iterable[str] = (),
        pre_stages: Iterable[Stage] = (),
        post_stages: Iterable[Stage] = (),
    ) -> None:
        if max_chars < 1 or max_tokens < 1:
            raise ValueError("Guardrail limits must be positive")
        self.max_chars = max_chars
        self.max_tokens = max_tokens
        self.blocked_terms = tuple(term.casefold() for term in blocked_terms if term)
        self.pre_stages = list(pre_stages)
        self.post_stages = list(post_stages)

    def pre(self, text: str) -> str:
        if len(text) > self.max_chars or self.tokens(text) > self.max_tokens:
            raise GuardrailError("Input exceeds configured limits")
        for stage in self.pre_stages:
            text = stage(text)
        if len(text) > self.max_chars or self.tokens(text) > self.max_tokens:
            raise GuardrailError("Processed input exceeds configured limits")
        if any(term in text.casefold() for term in self.blocked_terms):
            raise GuardrailError("Input contains blocked content")
        if re.search(
            r"ignore\s+(?:all\s+)?(?:previous|prior|system)\s+"
            r"(?:instructions?|prompts?)|"
            r"(?:reveal|print|show|leak)\s+(?:the\s+|your\s+)?"
            r"(?:system\s+prompt|secret\s+keys?|api\s+keys?)|"
            r"<\|(?:system|im_start)\|>|"
            r"disregard\s+(?:all\s+)?(?:previous|system)\s+instructions?",
            text,
            re.IGNORECASE,
        ):
            raise GuardrailError("Potential prompt injection detected")
        return text

    def post(self, text: str) -> str:
        for stage in self.post_stages:
            text = stage(text)
        text = re.sub(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            "[REDACTED_EMAIL]",
            text,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"(?<!\w)(?:\+?\d{1,3}[-.\s]?)?"
            r"(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\w)",
            "[REDACTED_PHONE]",
            text,
        )

    @staticmethod
    def tokens(text: str) -> int:
        # Conservative, deterministic budget estimate, not provider tokenization.
        return (len(text.encode("utf-8")) + 2) // 3

    def check_request(self, request: "ChatRequest") -> "ChatRequest":
        messages = [
            message.model_copy(update={"content": self.pre(message.content)})
            for message in request.messages
        ]
        payload = request.model_dump(mode="json", exclude={"max_tokens"})
        estimate = self.tokens(json.dumps(payload, ensure_ascii=False))
        if estimate + request.max_tokens > self.max_tokens:
            raise GuardrailError("Request exceeds total input/output token budget")
        return request.model_copy(update={"messages": messages})


__all__ = ["GuardrailError", "Guardrails"]
