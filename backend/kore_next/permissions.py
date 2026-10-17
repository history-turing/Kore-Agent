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
    """Match the complete JSON arguments without persisting their sensitive values."""

    MATCHER_VERSION = 2

    def __init__(self, db: Database, *, signing_key: bytes | None = None) -> None:
        self.db = db
        self.signing_key = signing_key

    def set_signing_key(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("permission signing key must be at least 32 bytes")
        self.signing_key = signing_key

    def matcher_for(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.signing_key is None:
            raise RuntimeError("persistent permission signing key is unavailable")
        # Canonical JSON makes equivalent key orders match; HMAC avoids storing
        # command arguments or a guessable plain hash in persistent rules.
        canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        digest = hmac.new(self.signing_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"version": self.MATCHER_VERSION, "arguments_hmac": digest}

    def _matches(self, matcher: dict[str, Any], arguments: dict[str, Any]) -> bool:
        if matcher.get("version") != self.MATCHER_VERSION or not isinstance(matcher.get("arguments_hmac"), str):
            return False
        if self.signing_key is None:
            return False
        expected = self.matcher_for(arguments)["arguments_hmac"]
        return hmac.compare_digest(matcher["arguments_hmac"], expected)

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
        candidates: list[tuple[int, Any]] = []
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
            priority = {"global": 1, "conversation": 2, "run": 3}[scope]
            candidates.append((priority, row))
        if not candidates:
            return PermissionVerdict("ask")
        # The narrowest matching scope wins, with the newest rule breaking ties.
        _, selected = max(candidates, key=lambda pair: (pair[0], pair[1]["created_at"]))
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
    ) -> dict[str, Any]:
        if effect not in {"allow", "deny"}:
            raise ValueError("permission effect must be allow or deny")
        if scope not in {"run", "conversation", "global"}:
            raise ValueError("permission scope must be run, conversation, or global")
        matcher = self.matcher_for(arguments)
        rule = {
            "id": uid(),
            "tool_name": tool_name,
            "effect": effect,
            "scope": scope,
            "scope_id": scope_id if scope != "global" else "*",
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
