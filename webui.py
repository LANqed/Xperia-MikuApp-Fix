"""Mikuxperia 服务端的 Material Design 2 风格管理面板。

面板提供健康状态、访问统计、AI 新闻控制、Bilibili Cookie 与收藏夹缓存状态、
播放列表信息、定时任务控制和歌曲上传。访问密码来自 miku.conf，默认关闭。
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import date, datetime, timezone
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path

import bilicookies


BRAND = "#39C5BB"

ENABLED = False
PASSWORD = ""
SESSION_HOURS = 12
STATS_FILE: Path | None = None
BILIBILI_COOKIE_FILE: Path | None = None
BILIBILI_MEDIA_ROOT: Path | None = None
BILIBILI_FOLDER_IDS: list[int] = []

_LOCK = threading.Lock()
_SESSIONS: dict[str, float] = {}
_STATS: dict[str, object] = {"days": {}, "total": 0}
_STARTED_AT = time.time()
_UNIQUE_TODAY: dict[str, set[str]] = {}


def configure(parser: configparser.ConfigParser, base_dir: Path) -> None:
    """读取可选的 [webui] 小节，以及用于状态展示的 Bilibili 路径。"""
    global ENABLED, PASSWORD, SESSION_HOURS, STATS_FILE
    global BILIBILI_COOKIE_FILE, BILIBILI_MEDIA_ROOT, BILIBILI_FOLDER_IDS
    global _SESSIONS, _STATS, _UNIQUE_TODAY

    def resolve(value: str) -> Path:
        path = Path(value)
        return (path if path.is_absolute() else base_dir / path).resolve()

    if parser.has_section("webui"):
        ENABLED = parser.getboolean("webui", "enabled", fallback=False)
        PASSWORD = parser.get("webui", "password", fallback="").strip()
        SESSION_HOURS = max(1, min(720, parser.getint("webui", "session_hours", fallback=12)))
        STATS_FILE = resolve(parser.get("webui", "stats_file", fallback="webui-stats.json").strip() or "webui-stats.json")
    else:
        ENABLED = False
        PASSWORD = ""
        SESSION_HOURS = 12
        STATS_FILE = resolve("webui-stats.json")

    BILIBILI_COOKIE_FILE = None
    BILIBILI_MEDIA_ROOT = None
    BILIBILI_FOLDER_IDS = []
    if parser.has_section("bilibili"):
        cookie_value = parser.get("bilibili", "cookie_file", fallback="").strip()
        media_value = parser.get("bilibili", "media_root", fallback="").strip()
        if cookie_value:
            BILIBILI_COOKIE_FILE = resolve(cookie_value)
        if media_value:
            BILIBILI_MEDIA_ROOT = resolve(media_value)
        for token in re.split(r"[\s,]+", parser.get("bilibili", "folder_ids", fallback="").strip()):
            if token.isdigit():
                BILIBILI_FOLDER_IDS.append(int(token))

    with _LOCK:
        _SESSIONS = {}
        _STATS = {"days": {}, "total": 0}
        _UNIQUE_TODAY = {}
    load_stats()


def available() -> bool:
    return bool(ENABLED and PASSWORD)


def today_key() -> str:
    return date.today().isoformat()


def load_stats() -> None:
    global _STATS
    if STATS_FILE is None or not STATS_FILE.is_file():
        return
    try:
        payload = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    days = payload.get("days")
    if not isinstance(days, dict):
        return
    cleaned = {}
    for key, value in days.items():
        if isinstance(key, str) and isinstance(value, dict):
            cleaned[key] = {
                "requests": int(value.get("requests", 0) or 0),
                "visitors": int(value.get("visitors", 0) or 0),
                "media": int(value.get("media", 0) or 0),
                "app": int(value.get("app", 0) or 0),
            }
    with _LOCK:
        _STATS = {"days": cleaned, "total": int(payload.get("total", 0) or 0)}


def save_stats() -> None:
    if STATS_FILE is None:
        return
    with _LOCK:
        days = dict(sorted(_STATS.get("days", {}).items())[-120:])
        payload = {"total": int(_STATS.get("total", 0)), "days": days}
    try:
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATS_FILE.with_suffix(STATS_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(STATS_FILE)
    except OSError as error:
        print(f"WebUI stats write failed: {error}")


def record_request(path: str, client_ip: str) -> None:
    """记录一次请求。访客 IP 只以哈希形式计数，不会写入磁盘。"""
    if path.startswith(("/admin", "/healthz")):
        return
    key = today_key()
    kind = "media" if path.startswith("/media/") else ("app" if path.startswith(("/resources/", "/getdata", "/feature_songs_provider")) else "other")
    digest = hashlib.sha256(f"{key}|{client_ip}".encode("utf-8")).hexdigest()[:16]
    persist = False
    with _LOCK:
        days = _STATS.setdefault("days", {})
        day = days.setdefault(key, {"requests": 0, "visitors": 0, "media": 0, "app": 0})
        day["requests"] = int(day.get("requests", 0)) + 1
        if kind in ("media", "app"):
            day[kind] = int(day.get(kind, 0)) + 1
        _STATS["total"] = int(_STATS.get("total", 0)) + 1
        seen = _UNIQUE_TODAY.setdefault(key, set())
        for stale in [item for item in _UNIQUE_TODAY if item != key]:
            _UNIQUE_TODAY.pop(stale, None)
        if digest not in seen:
            seen.add(digest)
            day["visitors"] = len(seen)
            persist = True
        if day["requests"] % 25 == 0:
            persist = True
    if persist:
        save_stats()


def stats_summary() -> dict:
    key = today_key()
    with _LOCK:
        days = dict(_STATS.get("days", {}))
        total = int(_STATS.get("total", 0))
    today = days.get(key, {"requests": 0, "visitors": 0, "media": 0, "app": 0})
    history = [
        {"date": name, **days[name]}
        for name in sorted(days)[-14:]
    ]
    return {
        "today": {
            "date": key,
            "requests": int(today.get("requests", 0)),
            "visitors": int(today.get("visitors", 0)),
            "media": int(today.get("media", 0)),
            "app": int(today.get("app", 0)),
        },
        "total_requests": total,
        "history": history,
        "uptime_seconds": int(time.time() - _STARTED_AT),
    }


def uptime_text(seconds: int) -> str:
    days, rest = divmod(max(0, seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def file_stat(path: Path | None) -> dict:
    if path is None:
        return {"path": "", "exists": False, "size": 0, "modified": ""}
    try:
        info = path.stat()
    except OSError:
        return {"path": str(path), "exists": False, "size": 0, "modified": ""}
    return {
        "path": str(path),
        "exists": True,
        "size": info.st_size,
        "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    }


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def bilibili_status(playlists: list[dict]) -> dict:
    cookie = file_stat(BILIBILI_COOKIE_FILE)
    summary = bilicookies.describe(BILIBILI_COOKIE_FILE)
    folders = []
    generated = {int(item.get("source_folder_id", 0)): item for item in playlists if item.get("source") == "bilibili-favorites"}
    for folder_id in BILIBILI_FOLDER_IDS or sorted(generated):
        playlist = generated.get(folder_id)
        directory = (BILIBILI_MEDIA_ROOT / str(folder_id)) if BILIBILI_MEDIA_ROOT else None
        audio_count = 0
        cover_count = 0
        used_bytes = 0
        if directory is not None and directory.is_dir():
            for item in directory.iterdir():
                if not item.is_file():
                    continue
                used_bytes += item.stat().st_size
                if item.suffix.lower() in {".m4a", ".mp3", ".aac", ".ogg", ".webm", ".mp4"}:
                    audio_count += 1
                elif item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    cover_count += 1
        folders.append({
            "folder_id": folder_id,
            "synced": playlist is not None,
            "song_count": len(playlist.get("songs", [])) if playlist else 0,
            "playlist_title": str(playlist.get("title", "")) if playlist else "",
            "audio_files": audio_count,
            "cover_files": cover_count,
            "used_bytes": used_bytes,
            "used_text": human_size(used_bytes),
            "directory": str(directory) if directory else "",
            "directory_exists": bool(directory and directory.is_dir()),
        })
    return {
        "configured": bool(BILIBILI_FOLDER_IDS),
        "cookie": {
            **cookie,
            "has_sessdata": summary["has_sessdata"],
            "cookie_count": summary["count"],
            "format": summary["format"],
            "names": summary["names"],
            "missing_optional": summary["missing_optional"],
        },
        "media_root": str(BILIBILI_MEDIA_ROOT) if BILIBILI_MEDIA_ROOT else "",
        "folders": folders,
        "total_bytes": sum(item["used_bytes"] for item in folders),
        "total_text": human_size(sum(item["used_bytes"] for item in folders)),
    }


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    expiry = time.time() + SESSION_HOURS * 3600
    with _LOCK:
        for existing, deadline in list(_SESSIONS.items()):
            if deadline < time.time():
                _SESSIONS.pop(existing, None)
        _SESSIONS[token] = expiry
    return token


def valid_session(token: str) -> bool:
    if not token:
        return False
    with _LOCK:
        expiry = _SESSIONS.get(token)
        if expiry is None:
            return False
        if expiry < time.time():
            _SESSIONS.pop(token, None)
            return False
    return True


def drop_session(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token, None)


def session_token(cookie_header: str) -> str:
    if not cookie_header:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return ""
    morsel = jar.get("miku_admin")
    return morsel.value if morsel else ""


def password_matches(candidate: str) -> bool:
    if not PASSWORD or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), PASSWORD.encode("utf-8"))


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mikuxperia 管理面板 · 登录</title>
<style>{style}</style></head>
<body class="login-body">
<form class="card login-card" method="post" action="/admin/login">
  <div class="brand-mark">39</div>
  <h1>Mikuxperia 管理面板</h1>
  <p class="hint">请输入管理密码</p>
  <label class="field">
    <span>管理密码</span>
    <input type="password" name="password" autocomplete="current-password" required>
  </label>
  {error}
  <button class="btn btn-raised" type="submit">登录</button>
</form>
</body></html>"""


STYLE = """
:root{--brand:#39C5BB;--brand-dark:#1f9c93;--brand-light:#e3f7f5;--ink:#1f2933;--muted:#6b7a86;--bg:#f4f6f8;--card:#ffffff;--danger:#c62828;--warn:#ef6c00;--ok:#2e7d32}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Roboto,"Helvetica Neue","Segoe UI","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.6}
a{color:var(--brand-dark)}
.appbar{position:sticky;top:0;z-index:10;background:var(--brand);color:#fff;padding:0 24px;height:64px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 4px rgba(0,0,0,.24)}
.appbar h1{font-size:20px;font-weight:500;margin:0;letter-spacing:.5px}
.appbar .actions{display:flex;gap:8px;align-items:center}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{background:var(--card);border-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.16),0 1px 2px rgba(0,0,0,.12);padding:20px;margin-bottom:16px}
.card h2{margin:0 0 4px;font-size:16px;font-weight:500;letter-spacing:.3px}
.card .sub{color:var(--muted);font-size:12px;margin:0 0 16px}
.metric{font-size:34px;font-weight:500;color:var(--brand-dark);line-height:1.2}
.metric small{display:block;font-size:12px;color:var(--muted);font-weight:400;margin-top:4px}
.row{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid #eceff1}
.row:last-child{border-bottom:none}
.row span:first-child{color:var(--muted)}
.row span:last-child{text-align:right;word-break:break-all}
.chip{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:12px;font-size:12px;background:var(--brand-light);color:var(--brand-dark)}
.chip.ok{background:#e8f5e9;color:var(--ok)}
.chip.warn{background:#fff3e0;color:var(--warn)}
.chip.bad{background:#ffebee;color:var(--danger)}
.btn{appearance:none;border:none;border-radius:4px;height:36px;padding:0 16px;font-size:14px;font-weight:500;letter-spacing:.6px;text-transform:uppercase;cursor:pointer;background:transparent;color:var(--brand-dark);font-family:inherit}
.btn:hover{background:rgba(57,197,187,.12)}
.btn-raised{background:var(--brand);color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.24)}
.btn-raised:hover{background:var(--brand-dark)}
.btn-ghost{color:#fff;border:1px solid rgba(255,255,255,.6)}
.btn-ghost:hover{background:rgba(255,255,255,.16)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #eceff1}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
td.num,th.num{text-align:right}
.news li{margin-bottom:14px;list-style:none}
.news ul{margin:0;padding:0}
.news .title{font-weight:500}
.news .meta{color:var(--muted);font-size:12px;margin-top:2px}
.bar{height:6px;border-radius:3px;background:#eceff1;overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:var(--brand)}
.snack{position:fixed;left:50%;transform:translateX(-50%);bottom:24px;background:#323232;color:#fff;padding:14px 20px;border-radius:4px;box-shadow:0 3px 5px rgba(0,0,0,.3);font-size:14px;max-width:90vw}
.snack.bad{background:var(--danger)}
.task{padding:14px 0;border-bottom:1px solid #eceff1}
.task:last-child{border-bottom:none}
.task-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.task-name{font-weight:500}
.task-desc{color:var(--muted);font-size:12px;margin-top:2px}
.task-meta{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.8}
.task-forms{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
.task-forms form{display:flex;gap:6px;align-items:center;margin:0}
.inp{height:34px;border:1px solid #cfd8dc;border-radius:4px;padding:0 8px;font-size:13px;font-family:inherit;background:#fff}
.inp:focus{outline:none;border-color:var(--brand)}
input.inp[type=number]{width:88px}
.upload{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.upload label{display:block}
.upload label>span{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.upload input,.upload select{width:100%;height:38px;border:1px solid #cfd8dc;border-radius:4px;padding:0 8px;font-size:14px;font-family:inherit;background:#fff}
.upload input[type=file]{padding:7px 8px;height:auto}
.upload input:focus,.upload select:focus{outline:none;border-color:var(--brand)}
.upload-actions{margin-top:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.login-body{display:flex;align-items:center;justify-content:center;min-height:100vh;background:linear-gradient(160deg,#39C5BB 0%,#1f9c93 100%)}
.login-card{width:340px;max-width:92vw;text-align:center}
.login-card h1{font-size:18px;font-weight:500;margin:12px 0 0}
.login-card .hint{color:var(--muted);font-size:13px;margin:6px 0 20px}
.brand-mark{width:56px;height:56px;margin:0 auto;border-radius:50%;background:var(--brand);color:#fff;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:500}
.field{display:block;text-align:left;margin-bottom:20px}
.field span{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.field input{width:100%;height:40px;border:none;border-bottom:2px solid #cfd8dc;background:#fafafa;padding:0 10px;font-size:15px;font-family:inherit;border-radius:4px 4px 0 0}
.field input:focus{outline:none;border-bottom-color:var(--brand)}
.error{background:#ffebee;color:var(--danger);border-radius:4px;padding:10px;font-size:13px;margin-bottom:16px}
.empty{color:var(--muted);font-size:13px}
@media(max-width:600px){.appbar{padding:0 12px}.appbar h1{font-size:16px}.wrap{padding:16px 10px 48px}}
"""


def login_page(error: str = "") -> str:
    block = f'<div class="error">{escape(error)}</div>' if error else ""
    return LOGIN_PAGE.format(style=STYLE, error=block)


def chip(ok: bool, good: str, bad: str, warn: bool = False) -> str:
    if warn:
        return f'<span class="chip warn">{escape(bad)}</span>'
    css = "ok" if ok else "bad"
    return f'<span class="chip {css}">{escape(good if ok else bad)}</span>'


def interval_input(seconds: int) -> tuple[int, str]:
    """挑选能整除该间隔的最大单位，便于在表单里显示。"""
    for divisor, unit in ((86400, "days"), (3600, "hours"), (60, "minutes")):
        if seconds >= divisor and seconds % divisor == 0:
            return seconds // divisor, unit
    return seconds, "seconds"


def dashboard_page(data: dict) -> str:
    stats = data["stats"]
    health = data["health"]
    news = data["news"]
    bili = data["bilibili"]
    today = stats["today"]
    max_requests = max([item["requests"] for item in stats["history"]] or [1]) or 1

    history_rows = "".join(
        f'<tr><td>{escape(item["date"])}</td><td class="num">{item["requests"]}</td>'
        f'<td class="num">{item["visitors"]}</td><td class="num">{item["app"]}</td>'
        f'<td class="num">{item["media"]}</td>'
        f'<td><div class="bar"><i style="width:{min(100, round(item["requests"] * 100 / max_requests))}%"></i></div></td></tr>'
        for item in reversed(stats["history"])
    ) or '<tr><td colspan="6" class="empty">暂无访问记录</td></tr>'

    if news["items"]:
        news_items = "".join(
            f'<li><div class="title">{escape(item["title"])}</div>'
            f'<div class="meta">{escape(item.get("category", "news"))} · {escape(item.get("date", "") or "日期未知")}'
            + (f' · <a href="{escape(item["url"])}" target="_blank" rel="noreferrer noopener">来源</a>' if item.get("url") else "")
            + f'</div><div>{escape(item.get("summary", ""))}</div></li>'
            for item in news["items"]
        )
        news_block = f'<ul>{news_items}</ul>'
    elif not news["configured"]:
        news_block = '<p class="empty">未配置 AI 新闻。请在 miku.conf 的 [ainews] 中填写 Tavily 与 OpenAI 兼容接口。</p>'
    else:
        news_block = '<p class="empty">暂无新闻，点击「立即刷新」抓取。</p>'

    folder_rows = "".join(
        f'<tr><td>{item["folder_id"]}</td>'
        f'<td>{chip(item["synced"], "已同步", "未同步")}</td>'
        f'<td>{escape(item["playlist_title"] or "-")}</td>'
        f'<td class="num">{item["song_count"]}</td>'
        f'<td class="num">{item["audio_files"]}</td>'
        f'<td class="num">{item["cover_files"]}</td>'
        f'<td class="num">{escape(item["used_text"])}</td></tr>'
        for item in bili["folders"]
    ) or '<tr><td colspan="7" class="empty">未配置收藏夹</td></tr>'

    checks = "".join(
        f'<div class="row"><span>{escape(item["label"])}</span><span>{chip(item["ok"], item["good"], item["bad"], item.get("warn", False))}</span></div>'
        for item in health["checks"]
    )

    overall = health["status"]
    overall_chip = (
        '<span class="chip ok">运行正常</span>' if overall == "ok"
        else ('<span class="chip warn">存在提示</span>' if overall == "warn" else '<span class="chip bad">存在故障</span>')
    )
    message = data.get("message", "")
    failure = data.get("error", "")
    snack = ""
    if failure:
        snack = f'<div class="snack bad">{escape(failure)}</div>'
    elif message:
        snack = f'<div class="snack">{escape(message)}</div>'

    task_blocks = []
    for task in data.get("tasks", []):
        if task["last_ok"] is None:
            result = '<span class="chip">尚无结果</span>'
        elif task["last_ok"]:
            result = '<span class="chip ok">上次成功</span>'
        else:
            result = '<span class="chip bad">上次失败</span>'
        running = '<span class="chip warn">执行中</span>' if task["running"] else ""
        state = chip(task["enabled"], "自动开启", "自动关闭")
        default_value, default_unit = interval_input(task["interval_seconds"])
        options = "".join(
            f'<option value="{value}"{" selected" if value == default_unit else ""}>{escape(text)}</option>'
            for value, text in (("seconds", "秒"), ("minutes", "分钟"), ("hours", "小时"), ("days", "天"))
        )
        task_blocks.append(f"""    <div class="task">
      <div class="task-head">
        <div>
          <div class="task-name">{escape(task["label"])}</div>
          <div class="task-desc">{escape(task["description"])}</div>
        </div>
        <div>{running}{result} {state}</div>
      </div>
      <div class="task-meta">
        当前间隔 {escape(task["interval_text"])}（最小 {escape(task["minimum_text"])}） · 下次执行 {escape(task["next_run_text"])}<br>
        上次执行 {escape(task["last_run_text"])} · 累计 {task["run_count"]} 次{f' · 耗时 {task["last_duration"]} 秒' if task["last_duration"] else ""}
        {f'<br>结果：{escape(task["last_message"])}' if task["last_message"] else ""}
      </div>
      <div class="task-forms">
        <form method="post" action="/admin/task/run">
          <input type="hidden" name="task" value="{escape(task["name"])}">
          <button class="btn btn-raised" type="submit">立即执行</button>
        </form>
        <form method="post" action="/admin/task/interval">
          <input type="hidden" name="task" value="{escape(task["name"])}">
          <input class="inp" type="number" name="interval" min="1" step="1" value="{default_value}" required>
          <select class="inp" name="unit">{options}</select>
          <button class="btn" type="submit">保存间隔</button>
        </form>
        <form method="post" action="/admin/task/toggle">
          <input type="hidden" name="task" value="{escape(task["name"])}">
          <input type="hidden" name="enabled" value="{"0" if task["enabled"] else "1"}">
          <button class="btn" type="submit">{"关闭自动" if task["enabled"] else "开启自动"}</button>
        </form>
      </div>
    </div>""")
    tasks_block = "\n".join(task_blocks) or '<p class="empty">没有已注册的定时任务</p>'

    playlist_options = "".join(
        f'<option value="{item["id"]}">{item["id"]} · {escape(item["title"] or "未命名")}（{item["songs"]} 首）</option>'
        for item in data.get("playlists", [])
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="{BRAND}">
<title>Mikuxperia 管理面板</title>
<style>{STYLE}</style></head>
<body>
<header class="appbar">
  <h1>Mikuxperia 管理面板</h1>
  <div class="actions">
    <form method="post" action="/admin/task/run" style="display:inline">
      <input type="hidden" name="task" value="weather">
      <button class="btn btn-ghost" type="submit">更新天气</button>
    </form>
    <form method="post" action="/admin/task/run" style="display:inline">
      <input type="hidden" name="task" value="news">
      <button class="btn btn-ghost" type="submit">刷新新闻</button>
    </form>
    <form method="post" action="/admin/task/run" style="display:inline">
      <input type="hidden" name="task" value="bilibili">
      <button class="btn btn-ghost" type="submit">同步收藏夹</button>
    </form>
    <form method="post" action="/admin/logout" style="display:inline"><button class="btn btn-ghost" type="submit">退出</button></form>
  </div>
</header>
<main class="wrap">
  <div class="grid">
    <section class="card">
      <h2>今日访问量</h2>
      <p class="sub">{escape(today["date"])}</p>
      <div class="metric">{today["requests"]}<small>今日请求数</small></div>
      <div class="row"><span>今日独立访客</span><span>{today["visitors"]}</span></div>
      <div class="row"><span>APP 接口请求</span><span>{today["app"]}</span></div>
      <div class="row"><span>媒体文件请求</span><span>{today["media"]}</span></div>
      <div class="row"><span>累计请求</span><span>{stats["total_requests"]}</span></div>
    </section>
    <section class="card">
      <h2>健康状态</h2>
      <p class="sub">运行时长 {escape(uptime_text(stats["uptime_seconds"]))}</p>
      <div class="row"><span>总体状态</span><span>{overall_chip}</span></div>
      {checks}
    </section>
    <section class="card">
      <h2>服务信息</h2>
      <p class="sub">来自 miku.conf</p>
      <div class="row"><span>监听地址</span><span>{escape(health["listen"])}</span></div>
      <div class="row"><span>公开地址</span><span>{escape(health["public_base_url"] or "使用请求 Host")}</span></div>
      <div class="row"><span>天气位置</span><span>{escape(health["weather_city"])}</span></div>
      <div class="row"><span>播放列表</span><span>{health["playlist_count"]} 个 / {health["song_count"]} 首</span></div>
      <div class="row"><span>媒体目录</span><span>{escape(health["media_root"])}</span></div>
    </section>
  </div>

  <section class="card">
    <h2>定时任务</h2>
    <p class="sub">可立即执行，也可调整自动执行的间隔。修改后立即生效并写入磁盘。</p>
{tasks_block}
  </section>

  <section class="card">
    <h2>上传歌曲</h2>
    <p class="sub">音频必填，封面与歌词可选。上传后自动写入 music.json 并提升播放列表版本号。</p>
    <form method="post" action="/admin/upload" enctype="multipart/form-data">
      <div class="upload">
        <label><span>歌曲标题（必填）</span><input type="text" name="title" maxlength="160" required></label>
        <label><span>作者</span><input type="text" name="artist" maxlength="80" placeholder="未知作者"></label>
        <label><span>日期</span><input type="text" name="date" pattern="\\d{{4}}\\.\\d{{2}}\\.\\d{{2}}" placeholder="2026.09.01"></label>
        <label><span>目标播放列表</span><select name="playlist_id"><option value="">默认「手动上传」列表</option>{playlist_options}</select></label>
        <label><span>音频文件（必填）</span><input type="file" name="audio" accept=".mp3,.m4a,.aac,.ogg,.wav" required></label>
        <label><span>封面图片</span><input type="file" name="cover" accept=".jpg,.jpeg,.png,.webp"></label>
        <label><span>歌词文件（UTF-8）</span><input type="file" name="lyrics" accept=".txt,.lrc"></label>
        <label><span>相关链接</span><input type="text" name="link" placeholder="https://"></label>
      </div>
      <div class="upload-actions">
        <button class="btn btn-raised" type="submit">上传歌曲</button>
        <span class="empty">单次上传总大小上限 80 MB，文件保存在媒体目录的 uploads 子目录</span>
      </div>
    </form>
  </section>

  <section class="card news">
    <h2>Miku 新闻与新歌动态</h2>
    <p class="sub">
      {("数据源 " + escape(news["source"])) if news["source"] else "尚未抓取"}
      {(" · 更新于 " + escape(news["fetched_text"])) if news["fetched_text"] else ""}
      {(" · 摘要模型 " + escape(news["model"])) if news["model"] else " · 未启用 AI 摘要"}
    </p>
    {news_block}
    {f'<p class="empty">最近错误：{escape(news["last_error"])}</p>' if news["last_error"] else ""}
  </section>

  <section class="card">
    <h2>Bilibili 收藏夹缓存</h2>
    <p class="sub">Cookie 与已下载音频封面状态，总占用 {escape(bili["total_text"])}</p>
    <div class="row"><span>Cookie 文件</span><span>{chip(bili["cookie"]["exists"], "已存在", "缺失")}</span></div>
    <div class="row"><span>SESSDATA</span><span>{chip(bili["cookie"]["has_sessdata"], "有效字段", "未检测到")}</span></div>
    <div class="row"><span>Cookie 格式</span><span>{escape({"netscape": "Netscape 格式", "header": "请求头格式"}.get(bili["cookie"]["format"], "无法识别"))}</span></div>
    <div class="row"><span>Cookie 条目</span><span>{bili["cookie"]["cookie_count"]}{(" · " + escape(", ".join(bili["cookie"]["names"]))) if bili["cookie"]["names"] else ""}</span></div>
    {f'<div class="row"><span>缺少可选字段</span><span><span class="chip warn">{escape(", ".join(bili["cookie"]["missing_optional"]))}</span></span></div>' if bili["cookie"]["missing_optional"] else ""}
    <div class="row"><span>Cookie 更新时间</span><span>{escape(bili["cookie"]["modified"] or "-")}</span></div>
    <div class="row"><span>缓存根目录</span><span>{escape(bili["media_root"] or "未配置")}</span></div>
    <table>
      <thead><tr><th>收藏夹</th><th>状态</th><th>播放列表</th><th class="num">歌曲</th><th class="num">音频</th><th class="num">封面</th><th class="num">占用</th></tr></thead>
      <tbody>{folder_rows}</tbody>
    </table>
  </section>

  <section class="card">
    <h2>最近 14 天访问统计</h2>
    <p class="sub">独立访客按 IP 哈希去重，不保存原始 IP</p>
    <table>
      <thead><tr><th>日期</th><th class="num">请求</th><th class="num">访客</th><th class="num">APP</th><th class="num">媒体</th><th>趋势</th></tr></thead>
      <tbody>{history_rows}</tbody>
    </table>
  </section>
</main>
{snack}
</body></html>"""
