from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import re

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(password|passwd|pwd|secret|token|api[_-]?key|authorization|credential|cookie|private[_-]?key|refresh[_-]?token)(?:$|[_-])",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)


def redact(value: Any) -> Any:
    """Return a recursively redacted copy suitable for logs/evidence."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text):
                result[key_text] = _REDACTED
            else:
                result[key_text] = redact(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _BEARER.sub("Bearer [REDACTED]", value)
    return value
