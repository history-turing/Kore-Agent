from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    category: str
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    summary: str


_CATEGORY_ALIASES = {
    "files": ("文件", "file", "workspace"),
    "system": ("系统", "system"),
    "web": ("网络", "联网", "搜索", "web"),
    "history": ("历史", "会话", "history", "session"),
    "memory": ("记忆", "长期记忆", "memory"),
    "tasks": ("任务", "待办", "task"),
    "skills": ("技能", "skill"),
    "evidence": ("证据", "evidence"),
    "results": ("工具结果", "结果", "result"),
    "automation": ("自动化", "定时", "automation", "cron"),
    "computer": ("电脑", "桌面", "computer", "gui"),
    "meta": ("工具目录", "能力", "catalog"),
}

_TOOL_ALIASES = {
    "read_file": ("读文件", "读取文件", "read"),
    "write_file": ("写文件", "写入文件", "write"),
    "list_files": ("列目录", "文件列表", "list"),
    "publish_file": ("发布文件", "交付物", "publish"),
    "artifact_publish": ("交付物", "发布链接", "artifact"),
    "shell": ("命令", "终端", "bash"),
    "web_search": ("搜索", "联网搜索", "search"),
    "http_request": ("http 请求", "接口", "request"),
    "get_current_time": ("当前时间", "时间", "time"),
    "task_create": ("创建任务", "新建任务"),
    "task_update": ("更新任务", "任务进度"),
    "task_list": ("任务列表", "待办列表"),
}

_CATEGORY_TAGS = {
    "files": ("file", "filesystem", "workspace"),
    "system": ("system", "local"),
    "web": ("web", "network", "search"),
    "history": ("history", "conversation"),
    "memory": ("memory", "durable"),
    "tasks": ("task", "todo"),
    "skills": ("skill", "instructions"),
    "evidence": ("evidence", "trace"),
    "results": ("result", "archive"),
    "automation": ("automation", "schedule"),
    "computer": ("computer", "desktop", "gui"),
    "meta": ("catalog", "discovery"),
}

_CATEGORY_WORDS = {
    *(_CATEGORY_ALIASES),
    *(alias.lower() for aliases in _CATEGORY_ALIASES.values() for alias in aliases),
}


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", value.lower())))


def _category(name: str) -> str:
    if name in {"read_file", "write_file", "list_files", "publish_file", "artifact_publish"}:
        return "files"
    if name in {"shell", "get_current_time"}:
        return "system"
    if name in {"web_search", "http_request"}:
        return "web"
    if name.startswith("history_") or name == "session_search":
        return "history"
    if name.startswith("memory_") or name.startswith("core_memory_") or name == "remember":
        return "memory"
    if name.startswith("task_"):
        return "tasks"
    if name.startswith("skill_"):
        return "skills"
    if name.startswith("evidence_"):
        return "evidence"
    if name.startswith("tool_result_"):
        return "results"
    if name.startswith("automation_"):
        return "automation"
    if name.startswith("computer_"):
        return "computer"
    if name == "tool_catalog":
        return "meta"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return f"mcp:{parts[1]}" if len(parts) >= 2 else "mcp"
    return "other"


def _spec(definition: dict[str, Any]) -> ToolSpec:
    function = definition.get("function", {})
    name = str(function.get("name") or "")
    description = str(function.get("description") or "")
    category = _category(name)
    aliases = tuple(
        dict.fromkeys(
            [
                *_CATEGORY_ALIASES.get(category, ()),
                *_CATEGORY_ALIASES.get(category.split(":", 1)[0], ()),
                *_TOOL_ALIASES.get(name, ()),
            ]
        )
    )
    tags = tuple(
        dict.fromkeys(
            [
                category,
                *_CATEGORY_TAGS.get(category, ()),
                *(_CATEGORY_TAGS.get(category.split(":", 1)[0], ())),
                *_tokens(name),
            ]
        )
    )
    summary = description.strip()
    if len(summary) > 180:
        summary = summary[:177].rstrip() + "..."
    return ToolSpec(name, description, category, tags, aliases, summary)


def _field_score(term: str, value: str, exact: int, partial: int) -> int:
    lowered = value.lower()
    if term == lowered:
        return exact
    return partial if term in lowered else 0


class ToolCatalog:
    """Bounded local retrieval over the tools registered for the current run."""

    def __init__(self, definitions: list[dict[str, Any]], *, active_names: set[str] | None = None):
        self.specs = [_spec(definition) for definition in definitions]
        self.by_name = {spec.name: spec for spec in self.specs}
        self.active = set(active_names or ())

    def inventory(self) -> list[dict[str, Any]]:
        groups: dict[str, list[ToolSpec]] = {}
        for spec in self.specs:
            groups.setdefault(spec.category, []).append(spec)
        result = []
        for category in sorted(groups):
            specs = sorted(groups[category], key=lambda item: item.name)
            result.append(
                {
                    "category": category,
                    "count": len(specs),
                    "examples": [spec.name for spec in specs[:3]],
                }
            )
        return result

    def search(
        self,
        *,
        query: str = "",
        category: str | None = None,
        names: list[str] | None = None,
        limit: int = 8,
        activate_limit: int = 3,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 20))
        requested = [str(name) for name in (names or []) if str(name)]
        if requested:
            known = [self.by_name[name] for name in requested if name in self.by_name][:20]
            unknown = [name for name in requested if name not in self.by_name]
            activate = [spec.name for spec in known if spec.name not in self.active]
            return {
                "matches": [self._match(spec, 100, ["name"]) for spec in known[:limit]],
                "matching_count": len(known),
                "unknown": unknown,
                "activated": activate,
                "message": "Explicit tool names selected.",
            }

        terms = _tokens(query)
        if not terms and not category:
            return {
                "inventory": self.inventory(),
                "matches": [],
                "matching_count": len(self.specs),
                "activated": [],
                "message": "No query provided; listing capability groups. "
                "Call tool_catalog with query, category, or names to activate tools.",
            }

        scored: list[tuple[int, list[str], ToolSpec]] = []
        for spec in self.specs:
            if category and not (spec.category == category or spec.category.startswith(category + ":")):
                continue
            if terms:
                score, matched_on = self._score(spec, terms)
            else:
                score, matched_on = 1, ["category"]
            if score > 0:
                scored.append((score, matched_on, spec))
        scored.sort(key=lambda item: (-item[0], item[2].name))
        matches = [self._match(spec, score, matched_on) for score, matched_on, spec in scored[:limit]]
        broad_query = bool(category) or any(term in _CATEGORY_WORDS for term in terms)
        activation_slots = limit if broad_query else activate_limit
        activate = [spec.name for score, _matched, spec in scored if score >= 20 and spec.name not in self.active][
            :activation_slots
        ]
        return {
            "matches": matches,
            "matching_count": len(scored),
            "unknown": [],
            "activated": activate,
            "message": (
                "Activated tools are available on the next model step."
                if activate
                else "No new tools activated; inspect matches or pass names."
            ),
        }

    def _score(self, spec: ToolSpec, terms: tuple[str, ...]) -> tuple[int, list[str]]:
        score = 0
        matched_on: list[str] = []
        for term in terms:
            term_score = 0
            for field, value, exact, partial in (
                ("name", spec.name, 100, 60),
                *[("alias", alias, 80, 40) for alias in spec.aliases],
                *[("tag", tag, 50, 25) for tag in spec.tags],
                ("description", spec.description, 0, 10),
            ):
                value_score = _field_score(term, value, exact, partial)
                if value_score:
                    term_score += value_score
                    if field not in matched_on:
                        matched_on.append(field)
            score += term_score
        if terms and all(
            any(term in value.lower() for value in (spec.name, *spec.aliases, *spec.tags, spec.description))
            for term in terms
        ):
            score += 20
        return score, matched_on

    def _match(self, spec: ToolSpec, score: int, matched_on: list[str]) -> dict[str, Any]:
        return {
            "name": spec.name,
            "category": spec.category,
            "summary": spec.summary,
            "score": score,
            "matched_on": matched_on,
            "already_active": spec.name in self.active,
        }
