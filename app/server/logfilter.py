"""`--hide`/`--only` filters for `yard logs`: field rules, text rules, trace groups.

A field rule is `key=value`, `key!=value`, `key>n`, or `key<n` on one JSON log
record: the key reads the top level first, then `attributes[key]` (Stario
attribute names carry dots). Anything else is a text rule, globbed `*expr*`
against the raw message. Lines that share a `trace_id` are one group; every
other line is its own group. A group stays when every `only` rule that applies
to it matches and no `hide` rule matches.
"""

import fnmatch
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from stario import json

from app.shared import YardError

_HOLD_SECONDS = 2.0
_DECIDED_LIMIT = 10_000

_FIELD = re.compile(r"^([A-Za-z_][\w.]*)(!=|=|>|<)(.*)\Z")
_HELP = "forms: key=value, key!=value, key>n, key<n, or text"


@dataclass(frozen=True, slots=True)
class _Line:
    message: str
    record: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _Rule:
    """key+op is a field rule; text alone is a substring glob on the message."""

    text: str
    key: str = ""
    op: str = ""
    value: str = ""
    number: float = 0.0

    def applies(self, group: list[_Line]) -> bool:
        """A field rule judges only a group that holds a JSON line."""
        return not self.key or any(line.record is not None for line in group)

    def matches(self, group: list[_Line]) -> bool:
        if not self.key:
            pattern = f"*{self.text}*"
            return any(fnmatch.fnmatchcase(line.message, pattern) for line in group)
        return any(line.record is not None and _field_matches(self, line.record) for line in group)


@dataclass(slots=True)
class _HeldTrace[T]:
    since: float
    items: list[T] = field(default_factory=list)
    lines: list[_Line] = field(default_factory=list)


def _record(message: str) -> dict[str, Any] | None:
    """The parsed object when the message is a JSON object."""
    if not message.lstrip().startswith("{"):
        return None
    try:
        value = json.loads(message)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _trace_id(record: dict[str, Any] | None) -> str | None:
    trace_id = record.get("trace_id") if record else None
    return trace_id if isinstance(trace_id, str) and trace_id else None


def _lookup(record: dict[str, Any], key: str) -> Any:
    """Top level wins; `attributes` answers a dotted name like request.path."""
    if key in record:
        return record[key]
    attributes = record.get("attributes")
    if isinstance(attributes, dict):
        return attributes.get(key)
    return None


def _text_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _field_matches(rule: _Rule, record: dict[str, Any]) -> bool:
    """A missing key and a dict/list/None value match no op, not even `!=`."""
    value = _lookup(record, rule.key)
    if rule.op in (">", "<"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return value > rule.number if rule.op == ">" else value < rule.number
    text = _text_value(value)
    if text is None:
        return False
    matched = fnmatch.fnmatchcase(text, rule.value)
    return matched if rule.op == "=" else not matched


def _rule(expr: str) -> _Rule:
    if not expr.strip():
        raise YardError(400, "bad_filter", f"bad filter {expr!r}", help_text=_HELP)
    found = _FIELD.match(expr)
    if found is None:
        return _Rule(text=expr)
    key, op, value = found.group(1), found.group(2), found.group(3)
    if op in (">", "<"):
        try:
            number = float(value)
        except ValueError as exc:
            raise YardError(400, "bad_filter", f"bad filter {expr!r}", help_text=_HELP) from exc
        return _Rule(text=expr, key=key, op=op, value=value, number=number)
    return _Rule(text=expr, key=key, op=op, value=value)


class LogFilter:
    """hide/only rules decided per trace group. `parse` runs once per request."""

    __slots__ = ("_hide", "_only")

    def __init__(self, hide: list[_Rule], only: list[_Rule]) -> None:
        self._hide = hide
        self._only = only

    @classmethod
    def parse(cls, hide: list[str], only: list[str]) -> LogFilter | None:
        """The filter for these expressions, or None when nothing was asked."""
        if not hide and not only:
            return None
        return cls([_rule(expr) for expr in hide], [_rule(expr) for expr in only])

    def _keeps(self, group: list[_Line]) -> bool:
        """Every `only` rule that applies must match; no `hide` rule may."""
        if any(rule.applies(group) and not rule.matches(group) for rule in self._only):
            return False
        return not any(rule.matches(group) for rule in self._hide)

    def select(self, messages: list[str]) -> list[bool]:
        """Which messages to keep: one verdict per trace group."""
        lines = [_Line(message, _record(message)) for message in messages]
        by_trace: dict[str, list[int]] = {}
        groups: list[list[int]] = []
        for index, line in enumerate(lines):
            trace_id = _trace_id(line.record)
            if trace_id is None:
                groups.append([index])
            elif trace_id in by_trace:
                by_trace[trace_id].append(index)
            else:
                by_trace[trace_id] = [index]
                groups.append(by_trace[trace_id])
        keep = [False] * len(messages)
        for group in groups:
            verdict = self._keeps([lines[index] for index in group])
            for index in group:
                keep[index] = verdict
        return keep


class Held[T]:
    """Follow-side filter: a trace holds until its root shows or it ages out.

    `now` is the caller's clock in seconds. `push` returns the items the line
    frees (itself when the line decides at once), `expire` decides held traces
    older than _HOLD_SECONDS on what they have, `drain` flushes the rest.
    """

    def __init__(self, log_filter: LogFilter) -> None:
        self._filter = log_filter
        self._held: dict[str, _HeldTrace[T]] = {}
        self._decided: dict[str, bool] = {}
        self._decided_order: deque[str] = deque()

    def push(self, item: T, message: str, now: float) -> list[T]:
        record = _record(message)
        trace_id = _trace_id(record)
        if trace_id is None:
            return [item] if self._filter._keeps([_Line(message, record)]) else []
        decided = self._decided.get(trace_id)
        if decided is not None:
            return [item] if decided else []
        held = self._held.get(trace_id)
        if held is None:
            held = _HeldTrace[T](since=now)
            self._held[trace_id] = held
        held.items.append(item)
        held.lines.append(_Line(message, record))
        if record is not None and record.get("parent_id") is None:
            return self._release(trace_id)
        return []

    def expire(self, now: float) -> list[T]:
        out: list[T] = []
        for trace_id in [tid for tid, held in self._held.items() if now - held.since > _HOLD_SECONDS]:
            out += self._release(trace_id)
        return out

    def drain(self) -> list[T]:
        out: list[T] = []
        for trace_id in list(self._held):
            out += self._release(trace_id)
        return out

    def _release(self, trace_id: str) -> list[T]:
        held = self._held.pop(trace_id)
        verdict = self._filter._keeps(held.lines)
        self._remember(trace_id, verdict)
        return held.items if verdict else []

    def _remember(self, trace_id: str, verdict: bool) -> None:
        self._decided[trace_id] = verdict
        self._decided_order.append(trace_id)
        while len(self._decided_order) > _DECIDED_LIMIT:
            del self._decided[self._decided_order.popleft()]
