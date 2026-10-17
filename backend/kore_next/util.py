from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def uid() -> str:
    return uuid4().hex


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default
