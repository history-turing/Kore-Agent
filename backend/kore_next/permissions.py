from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .database import Database
from .util import dumps, now, uid


@dataclass(frozen=True)
class PermissionVerdict:
    effect: str
    rule_id: str | None = None

    @property
    def decided(self) -> bool:
        return self.effect in {"allow", "deny"}


class PermissionRuleStore:
    """Match tool arguments by exact values, command, or tool scope."""

    MATCHER_VERSION = 3
    LEGACY_MATCHER_VERSION = 2
    MATCH_MODES = {"arguments", "command", "tool"}

    def __init__(self, db: Database, *, signing_key: bytes | None = None) -> None:
        self.db = db
        self.signing_key = signing_key

    def set_signing_key(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("permission signing key must be at least 32 bytes")
        self.signing_key = signing_key

    def _digest(self, value: dict[str, Any]) -> str:
        if self.signing_key is None:
            raise RuntimeError("persistent permission signing key is unavailable")
        # Canonical JSON makes equivalent key orders match; HMAC avoids storing
        # command arguments or a guessable plain hash in persistent rules.
        canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        digest = hmac.new(self.signing_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest

    def matcher_for(self, arguments: dict[str, Any], *, match: str = "arguments") -> dict[str, Any]:
        if match not in self.MATCH_MODES:
            raise ValueError(f"unsupported permission match mode: {match}")
        if self.signing_key is None:
            raise RuntimeError("persistent permission signing key is unavailable")
        if match == "tool":
            return {"version": self.MATCHER_VERSION, "mode": "tool"}
        if match == "command":
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("command matching requires a non-empty command argument")
            # Keep the workspace in the matcher so a command rule cannot leak
            # into a different project; timeout is intentionally ignored.
            value = {"command": command, "workspace_root": arguments.get("workspace_root")}
            return {
                "version": self.MATCHER_VERSION,
                "mode": "command",
                "command_hmac": self._digest(value),
            }
        return {
            "version": self.MATCHER_VERSION,
            "mode": "arguments",
            "arguments_hmac": self._digest(arguments),
        }

    @staticmethod
    def _matches_hmac(expected: Any, actual: str) -> bool:
        return isinstance(expected, str) and hmac.compare_digest(expected, actual)

    def _matches(self, matcher: dict[str, Any], arguments: dict[str, Any]) -> bool:
        if self.signing_key is None:
            return False
        version = matcher.get("version")
        if version == self.LEGACY_MATCHER_VERSION:
            return self._matches_hmac(matcher.get("arguments_hmac"), self._digest(arguments))
        if version != self.MATCHER_VERSION:
            return False
        mode = matcher.get("mode")
        if mode == "tool":
            return True
        if mode == "arguments":
            return self._matches_hmac(matcher.get("arguments_hmac"), self._digest(arguments))
        if mode == "command":
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                return False
            value = {"command": command, "workspace_root": arguments.get("workspace_root")}
            return self._matches_hmac(matcher.get("command_hmac"), self._digest(value))
        return False

    @staticmethod
    def _scope_priority(scope: str) -> int:
        return {"global": 1, "conversation": 2, "run": 3}[scope]

    @classmethod
    def _match_priority(cls, matcher: dict[str, Any]) -> int:
        if matcher.get("version") == cls.LEGACY_MATCHER_VERSION:
            return 3
        return {"tool": 1, "command": 2, "arguments": 3}.get(str(matcher.get("mode")), 0)

    async def evaluate(
        self,
        *,
        tool_name: str,
        conversation_id: str | None,
        run_id: str,
        arguments: dict[str, Any],
    ) -> PermissionVerdict:
        rows = await self.db.fetchall(
            "SELECT * FROM permission_rules WHERE tool_name=? ORDER BY created_at DESC",
            (tool_name,),
        )
        candidates: list[tuple[int, int, Any]] = []
        for row in rows:
            scope = str(row["scope"])
            scope_id = str(row["scope_id"])
            if scope == "run" and scope_id != run_id:
                continue
            if scope == "conversation" and scope_id != (conversation_id or ""):
                continue
            if scope not in {"global", "conversation", "run"}:
                continue
            matcher = json.loads(row["matcher_json"] or "{}")
            if not self._matches(matcher, arguments):
                continue
            candidates.append((self._scope_priority(scope), self._match_priority(matcher), row))
        if not candidates:
            return PermissionVerdict("ask")
        # The narrowest scope wins, then the most specific matcher, then newest.
        _, _, selected = max(candidates, key=lambda item: (item[0], item[1], item[2]["created_at"]))
        return PermissionVerdict(str(selected["effect"]), str(selected["id"]))

    async def add(
        self,
        *,
        tool_name: str,
        effect: str,
        scope: str,
        scope_id: str,
        arguments: dict[str, Any],
        description: str,
        match: str = "arguments",
    ) -> dict[str, Any]:
        if effect not in {"allow", "deny"}:
            raise ValueError("permission effect must be allow or deny")
        if scope not in {"run", "conversation", "global"}:
            raise ValueError("permission scope must be run, conversation, or global")
        matcher = self.matcher_for(arguments, match=match)
        rule = {
            "id": uid(),
            "tool_name": tool_name,
            "effect": effect,
            "scope": scope,
            "scope_id": scope_id if scope != "global" else "*",
            "match": match,
            "matcher": matcher,
            "description": description,
            "created_at": now(),
        }
        await self.db.execute(
            "INSERT INTO permission_rules(id,tool_name,effect,scope,scope_id,matcher_json,description,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                rule["id"],
                tool_name,
                effect,
                scope,
                rule["scope_id"],
                dumps(matcher),
                description,
                rule["created_at"],
            ),
        )
        return rule
