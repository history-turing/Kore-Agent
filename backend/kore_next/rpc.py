from __future__ import annotations

from typing import Any


class RpcFault(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def required(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RpcFault(-32602, f"Missing or invalid parameter: {key}")
    return value
