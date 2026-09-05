"""Mikuxperia 服务端的后台定时任务调度器。

任务处理函数由 server.py 注册，这样本模块就不会产生循环导入。每个任务都能从
管理面板立即触发，按可调整的间隔自动执行，并把间隔与上次执行时间写进一个小
JSON 文件，重启后不会丢失排程。
"""

from __future__ import annotations

import configparser
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


TICK_SECONDS = 5
STATE_FILE: Path | None = None

_LOCK = threading.RLock()
_TASKS: dict[str, "Task"] = {}
_SCHEDULER: threading.Thread | None = None
_STOP = threading.Event()


class Task:
    """一个间隔可调的周期任务。"""

    def __init__(
        self,
        name: str,
        label: str,
        handler: Callable[[], str],
        interval_seconds: int,
        minimum_seconds: int,
        enabled: bool = True,
        description: str = "",
    ) -> None:
        self.name = name
        self.label = label
        self.handler = handler
        self.minimum_seconds = max(30, minimum_seconds)
        self.interval_seconds = max(self.minimum_seconds, interval_seconds)
        self.enabled = enabled
        self.description = description
        self.last_run = 0.0
        self.last_ok: bool | None = None
        self.last_message = ""
        self.last_duration = 0.0
        self.running = False
        self.run_count = 0

    def due(self, now: float) -> bool:
        if not self.enabled or self.running:
            return False
        return now - self.last_run >= self.interval_seconds

    def next_run(self) -> float:
        if not self.enabled:
            return 0.0
        return self.last_run + self.interval_seconds


def configure(parser: configparser.ConfigParser, base_dir: Path) -> None:
    """读取可选的 [tasks] 小节，并清空已有的任务注册。"""
    global STATE_FILE
    with _LOCK:
        _TASKS.clear()
    value = "tasks-state.json"
    if parser.has_section("tasks"):
        value = parser.get("tasks", "state_file", fallback=value).strip() or value
    path = Path(value)
    STATE_FILE = (path if path.is_absolute() else base_dir / path).resolve()


def defaults_from_config(parser: configparser.ConfigParser, option: str, fallback: int) -> int:
    if parser.has_section("tasks"):
        return parser.getint("tasks", option, fallback=fallback)
    return fallback


def enabled_from_config(parser: configparser.ConfigParser, option: str, fallback: bool) -> bool:
    if parser.has_section("tasks"):
        return parser.getboolean("tasks", option, fallback=fallback)
    return fallback


def register(task: Task) -> None:
    with _LOCK:
        _TASKS[task.name] = task


def load_state() -> None:
    """恢复各任务的间隔、开关状态与上次执行时间。"""
    if STATE_FILE is None or not STATE_FILE.is_file():
        return
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    saved = payload.get("tasks")
    if not isinstance(saved, dict):
        return
    with _LOCK:
        for name, task in _TASKS.items():
            entry = saved.get(name)
            if not isinstance(entry, dict):
                continue
            try:
                interval = int(entry.get("interval_seconds", task.interval_seconds))
            except (TypeError, ValueError):
                interval = task.interval_seconds
            task.interval_seconds = max(task.minimum_seconds, interval)
            task.enabled = bool(entry.get("enabled", task.enabled))
            try:
                task.last_run = float(entry.get("last_run", 0.0) or 0.0)
            except (TypeError, ValueError):
                task.last_run = 0.0
            task.last_message = str(entry.get("last_message", ""))[:400]
            last_ok = entry.get("last_ok")
            task.last_ok = bool(last_ok) if last_ok is not None else None


def save_state() -> None:
    if STATE_FILE is None:
        return
    with _LOCK:
        payload = {
            "tasks": {
                name: {
                    "interval_seconds": task.interval_seconds,
                    "enabled": task.enabled,
                    "last_run": task.last_run,
                    "last_ok": task.last_ok,
                    "last_message": task.last_message,
                }
                for name, task in _TASKS.items()
            }
        }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(STATE_FILE)
    except OSError as error:
        print(f"Task state write failed: {error}")


def set_interval(name: str, seconds: int) -> tuple[bool, str]:
    with _LOCK:
        task = _TASKS.get(name)
        if task is None:
            return False, f"未知任务：{name}"
        if seconds < task.minimum_seconds:
            return False, f"{task.label} 的间隔不能小于 {format_duration(task.minimum_seconds)}"
        if seconds > 30 * 24 * 3600:
            return False, f"{task.label} 的间隔不能超过 30 天"
        task.interval_seconds = seconds
        label = task.label
    save_state()
    return True, f"{label} 的间隔已设为 {format_duration(seconds)}"


def set_enabled(name: str, enabled: bool) -> tuple[bool, str]:
    with _LOCK:
        task = _TASKS.get(name)
        if task is None:
            return False, f"未知任务：{name}"
        task.enabled = enabled
        label = task.label
    save_state()
    return True, f"{label} 自动执行已{'开启' if enabled else '关闭'}"


def execute(name: str) -> tuple[bool, str]:
    """同步执行一个任务并记录结果。"""
    with _LOCK:
        task = _TASKS.get(name)
        if task is None:
            return False, f"未知任务：{name}"
        if task.running:
            return False, f"{task.label} 正在执行中"
        task.running = True
    started = time.time()
    try:
        message = task.handler() or "完成"
        ok = True
    except Exception as error:  # 处理函数抛错不能拖垮调度线程
        message = str(error)[:400] or error.__class__.__name__
        ok = False
        print(f"Task {name} failed: {message}")
    duration = time.time() - started
    with _LOCK:
        task.running = False
        task.last_run = time.time()
        task.last_ok = ok
        task.last_message = message[:400]
        task.last_duration = duration
        task.run_count += 1
        label = task.label
    save_state()
    return ok, f"{label}：{message}" if not ok else f"{label}：{message}"


def run_async(name: str) -> tuple[bool, str]:
    """在后台线程里启动任务，让 HTTP 处理函数可以立即返回。"""
    with _LOCK:
        task = _TASKS.get(name)
        if task is None:
            return False, f"未知任务：{name}"
        if task.running:
            return False, f"{task.label} 正在执行中，请稍后查看结果"
        label = task.label
    threading.Thread(target=execute, args=(name,), daemon=True, name=f"task-{name}").start()
    return True, f"{label} 已在后台开始执行，稍后刷新查看结果"


def _loop() -> None:
    while not _STOP.wait(TICK_SECONDS):
        now = time.time()
        with _LOCK:
            due = [task.name for task in _TASKS.values() if task.due(now)]
        for name in due:
            execute(name)


def start_scheduler() -> None:
    global _SCHEDULER
    with _LOCK:
        if _SCHEDULER is not None and _SCHEDULER.is_alive():
            return
        _STOP.clear()
        _SCHEDULER = threading.Thread(target=_loop, daemon=True, name="task-scheduler")
        _SCHEDULER.start()


def stop_scheduler() -> None:
    _STOP.set()


def format_duration(seconds: int) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} 分" + (f" {rest} 秒" if rest else "")
    if seconds < 86400:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours} 小时" + (f" {minutes} 分" if minutes else "")
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


def local_text(timestamp: float) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def status() -> list[dict]:
    now = time.time()
    with _LOCK:
        tasks = list(_TASKS.values())
    report = []
    for task in tasks:
        next_run = task.next_run()
        remaining = max(0, int(next_run - now)) if next_run else 0
        report.append({
            "name": task.name,
            "label": task.label,
            "description": task.description,
            "enabled": task.enabled,
            "running": task.running,
            "interval_seconds": task.interval_seconds,
            "interval_text": format_duration(task.interval_seconds),
            "minimum_seconds": task.minimum_seconds,
            "minimum_text": format_duration(task.minimum_seconds),
            "last_run": task.last_run,
            "last_run_text": local_text(task.last_run) or "尚未执行",
            "last_ok": task.last_ok,
            "last_message": task.last_message,
            "last_duration": round(task.last_duration, 1),
            "run_count": task.run_count,
            "next_run_text": (
                "执行中" if task.running
                else ("已关闭" if not task.enabled else (format_duration(remaining) + "后" if task.last_run else "即将执行"))
            ),
        })
    return report
