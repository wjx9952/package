#!/usr/bin/env python3
"""Live terminal monitor for local Codex token usage."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


DEFAULT_CODEX_HOME = Path.home() / ".codex"
TAIWAN_TZ = ZoneInfo("Asia/Taipei")
COMPLETED_HOLD_SECONDS = 60
PROCESSING_STALE_SECONDS = 15 * 60
CONFIRMATION_SESSION_LIMIT = 12
CONFIRMATION_SESSION_MAX_AGE_SECONDS = 6 * 60 * 60
_confirmation_sessions: dict[Path, dict[str, Any]] = {}


def _empty_session_state() -> dict[str, Any]:
    return {
        "offset": 0,
        "calls": {},
        "task_active": False,
        "latest_event_at": "",
        "latest_started_at": "",
        "latest_complete_at": "",
    }


@dataclass
class UsageSnapshot:
    session_file: Path
    event_timestamp: str
    total: dict[str, int]
    last: dict[str, int]
    context_window: int | None
    rate_limits: dict[str, Any]


@dataclass(frozen=True)
class CodexStatus:
    code: str
    label: str
    changed_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Realtime terminal monitor for Codex usage from local session logs."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=DEFAULT_CODEX_HOME,
        help=f"Codex home directory. Default: {DEFAULT_CODEX_HOME}",
    )
    parser.add_argument(
        "--session",
        type=Path,
        help="Watch a specific rollout JSONL file instead of the newest session.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds. Default: 1.0",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one snapshot and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the latest snapshot as JSON. Implies --once.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List known sessions and exit.",
    )
    return parser.parse_args()


def session_files(codex_home: Path) -> list[Path]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(
        sessions_dir.glob("**/rollout-*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def newest_session(codex_home: Path) -> Path | None:
    files = session_files(codex_home)
    return files[0] if files else None


def load_thread_names(codex_home: Path) -> dict[str, str]:
    index_path = codex_home / "session_index.jsonl"
    names: dict[str, str] = {}
    if not index_path.exists():
        return names

    with index_path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = item.get("id")
            thread_name = item.get("thread_name")
            if session_id and thread_name:
                names[session_id] = thread_name
    return names


def session_id_from_path(path: Path) -> str:
    matches = re.findall(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        path.stem,
    )
    return matches[-1] if matches else path.stem


def iter_json_lines(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line in file:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def snapshot_from_event(path: Path, event: dict[str, Any]) -> UsageSnapshot | None:
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None

    info = payload.get("info") or {}
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}
    if not isinstance(total, dict) or not isinstance(last, dict):
        return None

    return UsageSnapshot(
        session_file=path,
        event_timestamp=str(event.get("timestamp", "")),
        total={key: int(value or 0) for key, value in total.items()},
        last={key: int(value or 0) for key, value in last.items()},
        context_window=info.get("model_context_window"),
        rate_limits=payload.get("rate_limits") or {},
    )


def read_latest_snapshot(path: Path) -> UsageSnapshot | None:
    latest = None
    for event in iter_json_lines(path):
        snapshot = snapshot_from_event(path, event)
        if snapshot is not None:
            latest = snapshot
    return latest


def parse_event_datetime(timestamp: str) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def call_needs_confirmation(payload: dict[str, Any]) -> bool:
    name = str(payload.get("name", ""))
    if name in {"request_permissions", "request_user_input"}:
        return True
    tool_input = payload.get("input", payload.get("arguments", ""))
    if not isinstance(tool_input, str):
        return False
    call = re.search(
        r"\bawait\s+tools\."
        r"(request_permissions|request_user_input|exec_command)\s*\(",
        tool_input,
    )
    if not call:
        return False
    if call.group(1) in {"request_permissions", "request_user_input"}:
        return True
    return bool(re.search(
        r"\bsandbox_permissions\s*:\s*[\"']require_escalated[\"']",
        tool_input,
    ))


def pending_confirmation_across_sessions(
    codex_home: Path = DEFAULT_CODEX_HOME,
) -> tuple[str, dict[str, Any]] | None:
    """Incrementally find the newest unresolved prompt in recent tasks."""
    try:
        candidates = session_files(codex_home)[:CONFIRMATION_SESSION_LIMIT]
    except OSError:
        return None
    cutoff = time.time() - CONFIRMATION_SESSION_MAX_AGE_SECONDS
    sessions = []
    for path in candidates:
        try:
            if path.stat().st_mtime >= cutoff:
                sessions.append(path)
        except OSError:
            continue
    if not sessions and candidates:
        sessions = candidates[:1]

    active_paths = set(sessions)
    for stale_path in set(_confirmation_sessions) - active_paths:
        _confirmation_sessions.pop(stale_path, None)

    for path in sessions:
        try:
            stat = path.stat()
        except OSError:
            continue
        state = _confirmation_sessions.setdefault(path, _empty_session_state())
        if stat.st_size < int(state["offset"]):
            state.clear()
            state.update(_empty_session_state())
        try:
            with path.open("r", encoding="utf-8", errors="replace") as file:
                file.seek(int(state["offset"]))
                while True:
                    line_start = file.tell()
                    line = file.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        file.seek(line_start)
                        break
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    timestamp = str(event.get("timestamp", ""))
                    if timestamp:
                        state["latest_event_at"] = timestamp
                    if event.get("type") == "event_msg":
                        payload_type = payload.get("type")
                        if payload_type in {"task_started", "turn_started"}:
                            state["task_active"] = True
                            state["latest_started_at"] = timestamp
                            state["latest_complete_at"] = ""
                            state["calls"].clear()
                        elif payload_type in {"task_complete", "turn_complete"}:
                            state["task_active"] = False
                            state["latest_complete_at"] = timestamp
                            state["calls"].clear()
                        elif payload_type == "turn_aborted":
                            state["task_active"] = False
                            state["latest_complete_at"] = ""
                            state["calls"].clear()
                        continue
                    if event.get("type") != "response_item":
                        continue
                    call_id = str(payload.get("call_id", ""))
                    payload_type = payload.get("type")
                    if payload_type in {"function_call", "custom_tool_call"}:
                        if call_id and call_needs_confirmation(payload):
                            order = (
                                str(event.get("timestamp", "")),
                                stat.st_mtime_ns,
                                file.tell(),
                            )
                            state["calls"][call_id] = (order, payload)
                    elif payload_type in {
                        "function_call_output", "custom_tool_call_output"
                    }:
                        state["calls"].pop(call_id, None)
                state["offset"] = file.tell()
        except OSError:
            continue

    pending = [
        item
        for state in _confirmation_sessions.values()
        for item in state["calls"].values()
    ]
    if not pending:
        return None
    order, payload = max(pending, key=lambda item: item[0])
    return str(order[0]), payload


def read_combined_codex_status(
    codex_home: Path = DEFAULT_CODEX_HOME,
) -> CodexStatus:
    """Combine recent tasks with confirmation > processing > completed priority."""
    pending = pending_confirmation_across_sessions(codex_home)
    if pending is not None:
        timestamp, _payload = pending
        return CodexStatus("waiting_confirmation", "等待确认", timestamp)

    states = list(_confirmation_sessions.values())
    now = datetime.now().astimezone()
    active = []
    for state in states:
        if not state.get("task_active"):
            continue
        latest = parse_event_datetime(str(state.get("latest_event_at", "")))
        if latest is None or (
            now - latest.astimezone()
        ).total_seconds() <= PROCESSING_STALE_SECONDS:
            active.append(state)
    if active:
        newest = max(active, key=lambda state: str(state.get("latest_event_at", "")))
        changed_at = str(
            newest.get("latest_started_at") or newest.get("latest_event_at") or ""
        )
        return CodexStatus("processing", "处理中", changed_at)

    completed: list[tuple[datetime, str]] = []
    for state in states:
        timestamp = str(state.get("latest_complete_at", ""))
        parsed = parse_event_datetime(timestamp)
        if parsed is None:
            continue
        if (now - parsed.astimezone()).total_seconds() <= COMPLETED_HOLD_SECONDS:
            completed.append((parsed, timestamp))
    if completed:
        _parsed, timestamp = max(completed, key=lambda item: item[0])
        return CodexStatus("completed", "已完成", timestamp)

    latest_event = max(
        (str(state.get("latest_event_at", "")) for state in states),
        default="",
    )
    return CodexStatus("idle", "空闲", latest_event)


def read_codex_status(path: Path) -> CodexStatus:
    task_active = False
    latest_event_at = ""
    latest_started_at = ""
    latest_complete_at = ""
    pending_confirmations: dict[str, str] = {}

    for event in iter_json_lines(path):
        timestamp = str(event.get("timestamp", ""))
        if timestamp:
            latest_event_at = timestamp
        event_type = event.get("type")
        payload = event.get("payload") or {}

        if event_type == "event_msg":
            payload_type = payload.get("type")
            if payload_type == "task_started":
                task_active = True
                latest_started_at = timestamp
                latest_complete_at = ""
                pending_confirmations.clear()
            elif payload_type == "task_complete":
                task_active = False
                latest_complete_at = timestamp
                pending_confirmations.clear()
            elif payload_type == "turn_aborted":
                task_active = False
                latest_complete_at = ""
                pending_confirmations.clear()
            continue

        if event_type != "response_item" or not isinstance(payload, dict):
            continue

        payload_type = payload.get("type")
        call_id = str(payload.get("call_id", ""))
        if payload_type in {"function_call", "custom_tool_call"}:
            if call_id and call_needs_confirmation(payload):
                pending_confirmations[call_id] = timestamp
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            pending_confirmations.pop(call_id, None)

    if task_active and pending_confirmations:
        changed_at = max(pending_confirmations.values())
        return CodexStatus("waiting_confirmation", "等待确认", changed_at)

    now = datetime.now().astimezone()
    latest_event_time = parse_event_datetime(latest_event_at)
    if task_active:
        if latest_event_time is not None:
            idle_for = (now - latest_event_time.astimezone()).total_seconds()
            if idle_for > PROCESSING_STALE_SECONDS:
                return CodexStatus("idle", "空闲", latest_event_at)
        return CodexStatus("processing", "处理中", latest_started_at or latest_event_at)

    complete_time = parse_event_datetime(latest_complete_at)
    if complete_time is not None:
        age = (now - complete_time.astimezone()).total_seconds()
        if age <= COMPLETED_HOLD_SECONDS:
            return CodexStatus("completed", "已完成", latest_complete_at)

    return CodexStatus("idle", "空闲", latest_complete_at or latest_event_at)


def fmt_int(value: int | None) -> str:
    return f"{int(value or 0):,}"


def fmt_percent(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_time(epoch: Any) -> str:
    if not epoch:
        return "-"
    try:
        return datetime.fromtimestamp(float(epoch), TAIWAN_TZ).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    except (OverflowError, OSError, TypeError, ValueError):
        return "-"


def fmt_event_time(timestamp: str) -> str:
    if not timestamp:
        return "-"
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    return parsed.astimezone(TAIWAN_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_duration_until(epoch: Any) -> str:
    if not epoch:
        return "-"
    try:
        seconds = max(0, int(float(epoch) - time.time()))
    except (TypeError, ValueError):
        return "-"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def usage_line(label: str, data: dict[str, int]) -> str:
    return (
        f"{label:<8} total {fmt_int(data.get('total_tokens')):>10} | "
        f"in {fmt_int(data.get('input_tokens')):>10} | "
        f"cached {fmt_int(data.get('cached_input_tokens')):>10} | "
        f"out {fmt_int(data.get('output_tokens')):>8} | "
        f"reasoning {fmt_int(data.get('reasoning_output_tokens')):>8}"
    )


def context_line(snapshot: UsageSnapshot) -> str:
    total_tokens = snapshot.last.get("total_tokens", 0)
    window = snapshot.context_window
    if not window:
        return "Last ctx -"
    used = total_tokens * 100 / window
    return f"Last ctx {fmt_int(total_tokens)} / {fmt_int(window)} ({used:.1f}%)"


def rate_limit_lines(rate_limits: dict[str, Any]) -> list[str]:
    lines = []
    for key, label in (("primary", "5h"), ("secondary", "7d")):
        value = rate_limits.get(key) or {}
        if not isinstance(value, dict):
            continue
        reset = value.get("resets_at")
        lines.append(
            f"Rate {label:<2}  used {fmt_percent(value.get('used_percent')):<7} "
            f"resets {fmt_time(reset)} ({fmt_duration_until(reset)})"
        )
    if rate_limits.get("plan_type"):
        lines.append(f"Plan     {rate_limits.get('plan_type')}")
    if rate_limits.get("rate_limit_reached_type"):
        lines.append(f"Limit    {rate_limits.get('rate_limit_reached_type')}")
    return lines


def terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 88


def current_refresh_time() -> str:
    return datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def render(
    snapshot: UsageSnapshot,
    status: CodexStatus,
    thread_name: str | None,
) -> str:
    width = terminal_width()
    title = " Codex Usage Monitor "
    rule = title.center(width, "=")
    session_id = session_id_from_path(snapshot.session_file)
    lines = [
        rule,
        f"Session  {thread_name or session_id}",
        f"Status   {status.label}",
        f"File     {snapshot.session_file}",
        f"Refresh  {current_refresh_time()}",
        f"Event    {fmt_event_time(snapshot.event_timestamp)}",
        "",
        usage_line("Total", snapshot.total),
        usage_line("Last", snapshot.last),
        context_line(snapshot),
        "",
        *rate_limit_lines(snapshot.rate_limits),
        "",
        "Ctrl+C to quit. Reads local ~/.codex session logs only.",
    ]
    return "\n".join(lines)


def snapshot_to_json(snapshot: UsageSnapshot, status: CodexStatus) -> str:
    return json.dumps(
        {
            "session_file": str(snapshot.session_file),
            "session_id": session_id_from_path(snapshot.session_file),
            "event_timestamp": snapshot.event_timestamp,
            "event_timestamp_taiwan": fmt_event_time(snapshot.event_timestamp),
            "refresh_time_taiwan": current_refresh_time(),
            "status": {
                "code": status.code,
                "label": status.label,
                "changed_at": status.changed_at,
                "changed_at_taiwan": fmt_event_time(status.changed_at),
            },
            "total": snapshot.total,
            "last": snapshot.last,
            "context_window": snapshot.context_window,
            "rate_limits": snapshot.rate_limits,
        },
        ensure_ascii=False,
        indent=2,
    )


def list_sessions(codex_home: Path) -> int:
    names = load_thread_names(codex_home)
    files = session_files(codex_home)
    if not files:
        print(f"No Codex sessions found under {codex_home / 'sessions'}", file=sys.stderr)
        return 1

    for path in files:
        session_id = session_id_from_path(path)
        name = names.get(session_id, "")
        updated = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{updated}  {session_id}  {name}\n  {path}")
    return 0


def main() -> int:
    args = parse_args()
    if args.json:
        args.once = True

    codex_home = args.codex_home.expanduser()
    if args.list:
        return list_sessions(codex_home)

    target = args.session.expanduser() if args.session else newest_session(codex_home)
    if target is None:
        print(f"No Codex sessions found under {codex_home / 'sessions'}", file=sys.stderr)
        return 1
    if not target.exists():
        print(f"Session file does not exist: {target}", file=sys.stderr)
        return 1

    thread_names = load_thread_names(codex_home)
    thread_name = thread_names.get(session_id_from_path(target))
    snapshot = read_latest_snapshot(target)
    if snapshot is None:
        print(f"No token_count events found yet in {target}", file=sys.stderr)
        return 1
    status = read_codex_status(target)

    if args.once:
        print(
            snapshot_to_json(snapshot, status)
            if args.json
            else render(snapshot, status, thread_name)
        )
        return 0

    try:
        while True:
            newest = newest_session(codex_home) if args.session is None else target
            if newest is not None and newest != target:
                target = newest
                thread_names = load_thread_names(codex_home)
                thread_name = thread_names.get(session_id_from_path(target))
            next_snapshot = read_latest_snapshot(target)
            if next_snapshot is not None:
                snapshot = next_snapshot
            status = read_codex_status(target)

            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(render(snapshot, status, thread_name))
            sys.stdout.write("\n")
            sys.stdout.flush()

            time.sleep(max(args.interval, 0.1))
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
