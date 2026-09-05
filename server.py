from __future__ import annotations

import argparse
import base64
import configparser
import gzip
import io
import json
import math
import mimetypes
import re
import struct
import subprocess
import sys
import threading
import time
import wave
import zlib
from datetime import datetime, timedelta, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode
from urllib.request import Request, urlopen

import ainews
import tasks
import uploads
import webui


SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SERVER_DIR / "miku.conf"

# 单次 socket 读操作（请求头/请求体）的空闲超时秒数，由 [server] read_timeout_seconds 覆盖。
READ_TIMEOUT_SECONDS = 120

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
PUBLIC_BASE_URL = ""
QWEATHER_API_HOST = ""
QWEATHER_API_KEY = ""
QWEATHER_BEARER_TOKEN = ""
QWEATHER_CACHE_SECONDS = 1800
WEATHER_LATITUDE = 39.92
WEATHER_LONGITUDE = 116.41
WEATHER_CITY = "北京"
MEDIA_ROOT = SERVER_DIR / "media"
MUSIC_CONFIG = SERVER_DIR / "music.json"
ACTIVE_CONFIG_PATH = DEFAULT_CONFIG_PATH
BILIBILI_CONFIGURED = False
BILIBILI_SYNC_TIMEOUT = 3600

# 四位 ID 对应旧客户端的整数 AREA 字段，坐标取各省级行政区首府，
# 覆盖中国大陆全部省级行政区。当前天气位置由 miku.conf 指定，
# 这张表暂未被代码引用，保留备查。
CHINA_AREAS = {
    "1101": ("北京", 39.92, 116.41), "1201": ("天津", 39.13, 117.20),
    "1301": ("石家庄", 38.04, 114.51), "1401": ("太原", 37.87, 112.55),
    "1501": ("呼和浩特", 40.84, 111.75), "2101": ("沈阳", 41.80, 123.43),
    "2201": ("长春", 43.82, 125.32), "2301": ("哈尔滨", 45.80, 126.53),
    "3101": ("上海", 31.23, 121.47), "3201": ("南京", 32.06, 118.80),
    "3301": ("杭州", 30.27, 120.15), "3401": ("合肥", 31.82, 117.23),
    "3501": ("福州", 26.08, 119.30), "3601": ("南昌", 28.68, 115.86),
    "3701": ("济南", 36.67, 116.98), "4101": ("郑州", 34.75, 113.62),
    "4201": ("武汉", 30.59, 114.31), "4301": ("长沙", 28.23, 112.94),
    "4401": ("广州", 23.13, 113.26), "4501": ("南宁", 22.82, 108.37),
    "4601": ("海口", 20.04, 110.20), "5001": ("重庆", 29.56, 106.55),
    "5101": ("成都", 30.67, 104.07), "5201": ("贵阳", 26.65, 106.63),
    "5301": ("昆明", 25.04, 102.71), "5401": ("拉萨", 29.65, 91.12),
    "6101": ("西安", 34.34, 108.94), "6201": ("兰州", 36.06, 103.83),
    "6301": ("西宁", 36.62, 101.78), "6401": ("银川", 38.49, 106.23),
    "6501": ("乌鲁木齐", 43.83, 87.62),
}
WEATHER_CACHE: dict[str, tuple[float, dict]] = {}
WEATHER_CACHE_LOCK = threading.Lock()
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAACXBIWXMAAAsTAAALEwEAmpwY"
    "AAAAP0lEQVR4nO3PQQ0AIBDAsAP/nuGNAvZoFSzZOjNnyNi1dwfgUQCeBOBJAA4F4EkAngTg"
    "SQCOBOBJAA4F4EkAHg3wAV4AAULZL6gAAAAASUVORK5CYII="
)


def config_int(parser: configparser.ConfigParser, section: str, option: str, default: int, low: int, high: int) -> int:
    """读取整数配置项；缺省、留空或非法时退回 default，并夹在 [low, high] 范围内。"""
    try:
        value = parser.getint(section, option, fallback=default)
    except (configparser.Error, ValueError, TypeError):
        value = default
    return max(low, min(high, value))


def configure(path: Path) -> None:
    global LISTEN_HOST, LISTEN_PORT, PUBLIC_BASE_URL
    global QWEATHER_API_HOST, QWEATHER_API_KEY, QWEATHER_BEARER_TOKEN, QWEATHER_CACHE_SECONDS
    global WEATHER_LATITUDE, WEATHER_LONGITUDE, WEATHER_CITY, MEDIA_ROOT, MUSIC_CONFIG
    global READ_TIMEOUT_SECONDS

    path = path.expanduser().resolve()
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except OSError as error:
        raise RuntimeError(f"cannot read config {path}: {error}") from error

    required_sections = {"server", "qweather", "weather", "music"}
    missing = required_sections.difference(parser.sections())
    if missing:
        raise RuntimeError(f"missing config sections: {', '.join(sorted(missing))}")

    LISTEN_HOST = parser.get("server", "listen_host", fallback="0.0.0.0").strip()
    LISTEN_PORT = parser.getint("server", "listen_port", fallback=8080)
    READ_TIMEOUT_SECONDS = config_int(parser, "server", "read_timeout_seconds", 120, 5, 3600)
    PUBLIC_BASE_URL = parser.get("server", "public_base_url", fallback="").strip().rstrip("/")
    QWEATHER_API_HOST = parser.get("qweather", "api_host", fallback="").strip().removeprefix("https://").rstrip("/")
    QWEATHER_API_KEY = parser.get("qweather", "api_key", fallback="").strip()
    QWEATHER_BEARER_TOKEN = parser.get("qweather", "bearer_token", fallback="").strip()
    QWEATHER_CACHE_SECONDS = parser.getint("qweather", "cache_seconds", fallback=1800)
    WEATHER_CITY = parser.get("weather", "city", fallback="北京").strip() or "北京"
    WEATHER_LATITUDE = parser.getfloat("weather", "latitude", fallback=39.92)
    WEATHER_LONGITUDE = parser.getfloat("weather", "longitude", fallback=116.41)

    def config_path(section: str, option: str, default: str) -> Path:
        value = Path(parser.get(section, option, fallback=default).strip())
        return (value if value.is_absolute() else path.parent / value).resolve()

    MEDIA_ROOT = config_path("music", "media_root", "media")
    MUSIC_CONFIG = config_path("music", "catalog", "music.json")
    if not 1 <= LISTEN_PORT <= 65535:
        raise RuntimeError("server.listen_port must be between 1 and 65535")
    if not -90 <= WEATHER_LATITUDE <= 90 or not -180 <= WEATHER_LONGITUDE <= 180:
        raise RuntimeError("weather latitude or longitude is out of range")
    WEATHER_CACHE.clear()
    ainews.configure(parser, path.parent)
    webui.configure(parser, path.parent)
    configure_tasks(parser, path)


EPOCH = datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=9)))
MINUTE_VERSION_FLOOR = 100000  # 手工维护的版本号远低于这个值
LEGACY_TIMESTAMP_FLOOR = 1000000000  # 旧版同步脚本写入的 Unix 时间戳
SYNC_VERSION_EPOCH = 1767225600  # 2026-01-01T00:00:00Z
# Android 客户端用 Integer.parseInt 解析播放列表 ID，
# 超过 32 位有符号整数上限会让 MusicDataService 崩溃。
JAVA_INT_MAX = 2147483647


def version_date(version: object) -> datetime:
    """把播放列表版本号映射成旧客户端可比较的 pubDate。

    客户端只比较日期，因此只要版本号增大、日期也随之增大就能工作。
    手工维护的小版本号按「天」偏移，每加一都会得到不同的日期；
    Bilibili 同步写入的大版本号按「分钟」偏移，这样结果仍落在
    datetime 的有效范围内。早期直接传给 timedelta(days=...) 会抛
    OverflowError。
    """
    try:
        value = max(1, int(version))
    except (TypeError, ValueError):
        value = 1
    if value >= LEGACY_TIMESTAMP_FLOOR:
        value = max(MINUTE_VERSION_FLOOR, (value - SYNC_VERSION_EPOCH) // 60)
    if value >= MINUTE_VERSION_FLOOR:
        return EPOCH + timedelta(minutes=value)
    return EPOCH + timedelta(days=value)


def base_url(handler: BaseHTTPRequestHandler) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    scheme = handler.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip()
    return f"{scheme}://{handler.headers.get('Host', f'127.0.0.1:{LISTEN_PORT}')}"


def rfc_date(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone(timedelta(hours=9)))
    return value.strftime("%a,%d %b %Y %H:%M:%S %z")


def make_wav() -> bytes:
    output = io.BytesIO()
    sample_rate = 22050
    duration = 3
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_rate * duration):
            fade = min(1.0, index / 1000, (sample_rate * duration - index) / 1000)
            sample = int(9000 * fade * math.sin(2 * math.pi * 440 * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
        audio.writeframes(frames)
    return output.getvalue()


WAV = make_wav()


def qweather_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate"}
    if QWEATHER_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {QWEATHER_BEARER_TOKEN}"
    elif QWEATHER_API_KEY:
        headers["X-QW-Api-Key"] = QWEATHER_API_KEY
    else:
        raise RuntimeError("QWEATHER_API_KEY or QWEATHER_BEARER_TOKEN is required")
    return headers


def decode_json_response(data: bytes, content_encoding: str = "") -> dict:
    encoding = content_encoding.lower().strip()
    try:
        if encoding == "gzip" or data.startswith(b"\x1f\x8b"):
            data = gzip.decompress(data)
        elif encoding == "deflate":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        return json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, zlib.error, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid QWeather response: {error}") from error


def fetch_qweather() -> dict:
    cache_key = f"{WEATHER_LATITUDE:.2f},{WEATHER_LONGITUDE:.2f}"
    with WEATHER_CACHE_LOCK:
        cached = WEATHER_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < QWEATHER_CACHE_SECONDS:
            return cached[1]
    if not QWEATHER_API_HOST:
        raise RuntimeError("QWEATHER_API_HOST is required")
    query = urlencode({"days": 8, "localTime": "true", "lang": "zh"})
    url = f"https://{QWEATHER_API_HOST}/weather/v1/daily/{WEATHER_LATITUDE:.2f}/{WEATHER_LONGITUDE:.2f}?{query}"
    try:
        with urlopen(Request(url, headers=qweather_headers()), timeout=10) as response:
            payload = decode_json_response(response.read(), response.headers.get("Content-Encoding", ""))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"QWeather HTTP {error.code}: {detail}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"QWeather request failed: {error}") from error
    if len(payload.get("days", [])) < 2:
        raise RuntimeError("QWeather returned insufficient daily forecast data")
    with WEATHER_CACHE_LOCK:
        WEATHER_CACHE[cache_key] = (time.time(), payload)
    return payload


def load_music_config() -> dict:
    try:
        config = json.loads(MUSIC_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid music config {MUSIC_CONFIG}: {error}") from error
    if not isinstance(config.get("playlists"), list):
        raise RuntimeError("music.json must contain a playlists array")
    return config


def validate_music_config() -> tuple[int, int]:
    config = load_music_config()
    playlist_ids: set[int] = set()
    song_count = 0
    for playlist in config["playlists"]:
        try:
            playlist_id = int(playlist["id"])
            version = int(playlist["version"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("every playlist requires integer id and version") from error
        if playlist_id <= 0 or playlist_id in playlist_ids:
            raise RuntimeError(f"playlist id must be unique and positive: {playlist_id}")
        if playlist_id > JAVA_INT_MAX:
            raise RuntimeError(
                f"playlist id {playlist_id} exceeds the 32-bit limit {JAVA_INT_MAX}; "
                "the Android client parses it with Integer.parseInt and would crash"
            )
        if version <= 0:
            raise RuntimeError(f"playlist {playlist_id} version must be positive")
        playlist_ids.add(playlist_id)
        media_size(str(playlist.get("image", "debug.png")))
        media_size(str(playlist.get("brief_image", playlist.get("image", "debug.png"))))
        song_ids: set[str] = set()
        for song in playlist.get("songs", []):
            song_id = str(song.get("id", "")).strip()
            music = str(song.get("music", "")).strip()
            if not song_id or song_id in song_ids:
                raise RuntimeError(f"playlist {playlist_id} song id must be unique and non-empty: {song_id!r}")
            if not music:
                raise RuntimeError(f"playlist {playlist_id} song {song_id} requires music")
            song_ids.add(song_id)
            media_size(music)
            media_size(str(song.get("thumbnail", "debug.png")).strip())
            media_size(str(song.get("lyrics", "")).strip())
            song_count += 1
    return len(playlist_ids), song_count


def media_file(name: str) -> Path | None:
    if not name:
        return None
    if name in ("debug.wav", "debug.png", "debug.txt") and not (MEDIA_ROOT / name).is_file():
        return None
    candidate = (MEDIA_ROOT / name).resolve()
    if MEDIA_ROOT not in candidate.parents or not candidate.is_file():
        raise RuntimeError(f"media file not found: {name}")
    return candidate


def wire_media_name(name: str, playlist_id: object = "") -> str:
    """返回 Android 4.2 客户端能接受的扁平文件名。

    客户端会把 musicFileName 直接交给 File()，名字里带斜杠会抛
    IllegalArgumentException，所以这里压平成一个文件名，
    服务端保存的真实路径仍然可以带子目录。
    """
    base = Path(str(name).replace("\\", "/")).name
    prefix = str(playlist_id).strip()
    return f"{prefix}_{base}" if prefix else base


def resolve_wire_media(name: str) -> str:
    """把客户端传回的扁平文件名还原成 music.json 里的安全相对路径。"""
    if "/" in name or "\\" in name:
        return name
    try:
        playlists = load_music_config().get("playlists", [])
    except RuntimeError:
        return name
    for playlist in playlists:
        playlist_id = playlist.get("id", "")
        for song in playlist.get("songs", []):
            for field in ("music", "thumbnail", "lyrics"):
                value = str(song.get(field, "")).strip()
                if value and wire_media_name(value, playlist_id) == name:
                    return value
    return name

def media_size(name: str) -> int:
    if not name:
        return 0
    path = media_file(name)
    if path is None:
        return {"debug.wav": len(WAV), "debug.png": len(PNG), "debug.txt": 68}[name]
    return path.stat().st_size


def integer(value: object, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def legacy_weather_code(code: object) -> int:
    value = integer(code, 100)
    if value in (100, 150):
        return 100
    if value in (101, 102, 103, 151, 152, 153):
        return 101
    if value in (104, 154):
        return 200
    if 300 <= value < 400:
        return 300
    if 400 <= value < 500:
        return 400
    if 500 <= value < 600:
        return 200
    return 100


def precipitation_probability(day: dict) -> int:
    value = float(day.get("daytime", {}).get("precipitation", {}).get("probability", 0) or 0)
    return max(0, min(100, round(value * 100 if value <= 1 else value)))


def legacy_day_xml(kind: str, day: dict, details: bool) -> str:
    daytime = day.get("daytime", {})
    wind = daytime.get("wind", {})
    date = str(day.get("forecastStartTime", ""))[:10].replace("-", "")
    if len(date) != 8:
        date = datetime.now().strftime("%Y%m%d")
    rain = precipitation_probability(day)
    weather = legacy_weather_code(daytime.get("condition", {}).get("code"))
    common = (
        f"<day>{date}</day><weather>{weather}</weather>"
        f"<temphi>{integer(day.get('temperatureMax', {}).get('value'))}</temphi>"
        f"<templo>{integer(day.get('temperatureMin', {}).get('value'))}</templo>"
        f"<proba01>{rain}</proba01>"
    )
    if not details:
        return f'<weatherdata type="{kind}">{common}</weatherdata>'
    direction = integer(wind.get("direction", {}).get("degree")) // 23
    speed = integer(wind.get("speed", {}).get("value"))
    uv = max(1, min(5, math.ceil(float(day.get("uvIndexMax", 0) or 0) / 3)))
    wash = 1 if rain >= 50 else (2 if rain >= 20 else 4)
    star = 1 if weather in (300, 400) else (4 if weather == 100 else 2)
    dry = 2 if rain >= 30 else 4
    extra = (
        f"<proba02>{rain}</proba02><proba03>{rain}</proba03><proba04>{rain}</proba04>"
        f"<wind>{direction}</wind><velocity>{speed}</velocity><uvr>{uv}</uvr>"
        f"<wash>{wash}</wash><star>{star}</star><dry>{dry}</dry>"
    )
    return f'<weatherdata type="{kind}">{common}{extra}</weatherdata>'


def refresh_weather_now() -> str:
    """清空预报缓存并重新向和风天气请求一次。"""
    with WEATHER_CACHE_LOCK:
        WEATHER_CACHE.clear()
    payload = fetch_qweather()
    days = len(payload.get("days", []))
    return f"已获取 {WEATHER_CITY} 的 {days} 天预报"


def sync_bilibili_now() -> str:
    """以子进程运行收藏夹同步脚本，并汇报播放列表结果。"""
    script = SERVER_DIR / "sync_bilibili_favorites.py"
    if not script.is_file():
        raise RuntimeError("找不到 sync_bilibili_favorites.py")
    if not BILIBILI_CONFIGURED:
        raise RuntimeError("miku.conf 未配置 [bilibili] folder_ids")
    command = [sys.executable, str(script), "--config", str(ACTIVE_CONFIG_PATH)]
    try:
        result = subprocess.run(
            command,
            cwd=str(SERVER_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BILIBILI_SYNC_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"同步超过 {BILIBILI_SYNC_TIMEOUT} 秒未完成，已中止") from error
    output = (result.stdout or "").strip().splitlines()
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        message = (detail or output or ["同步脚本失败"])[-1]
        raise RuntimeError(message)
    counts = [line for line in output if line.startswith("folder ")]
    updated = [line for line in output if line.startswith("updated ")]
    summary = "；".join(counts) if counts else "没有收藏夹返回内容"
    return f"{summary}{'；' + updated[-1] if updated else ''}"


def cleanup_cache() -> str:
    """删除媒体目录下所有不再被 music.json 引用的文件。"""
    try:
        config = load_music_config()
    except RuntimeError as error:
        raise RuntimeError(f"无法加载 music.json: {error}") from error

    referenced: set[Path] = set()
    for playlist in config.get("playlists", []):
        for field in ("image", "brief_image"):
            value = str(playlist.get(field, "")).strip()
            if value:
                referenced.add((MEDIA_ROOT / value).resolve())
        for song in playlist.get("songs", []):
            for field in ("music", "thumbnail", "lyrics"):
                value = str(song.get(field, "")).strip()
                if value:
                    referenced.add((MEDIA_ROOT / value).resolve())

    removed_count = 0
    removed_bytes = 0
    if MEDIA_ROOT.is_dir():
        for path in MEDIA_ROOT.rglob("*"):
            if path.is_file():
                try:
                    resolved = path.resolve()
                except Exception:
                    continue
                if resolved not in referenced:
                    try:
                        size = path.stat().st_size
                        path.unlink()
                        removed_count += 1
                        removed_bytes += size
                    except OSError:
                        pass

    return f"清理完成：删除 {removed_count} 个文件，释放 {removed_bytes / 1024 / 1024:.2f} MiB"


def refresh_news_now() -> str:
    count, error = ainews.refresh(force=True)
    if error:
        raise RuntimeError(error)
    return f"已更新 {count} 条新闻"


def configure_tasks(parser: configparser.ConfigParser, config_path: Path) -> None:
    """注册四个定时任务，初始值来自可选的 [tasks] 小节。"""
    global ACTIVE_CONFIG_PATH, BILIBILI_CONFIGURED, BILIBILI_SYNC_TIMEOUT
    ACTIVE_CONFIG_PATH = config_path
    BILIBILI_CONFIGURED = bool(
        parser.has_section("bilibili")
        and parser.get("bilibili", "folder_ids", fallback="").strip()
    )
    BILIBILI_SYNC_TIMEOUT = max(60, tasks.defaults_from_config(parser, "bilibili_timeout_seconds", 3600))
    tasks.configure(parser, config_path.parent)
    tasks.register(tasks.Task(
        name="weather",
        label="天气更新",
        handler=refresh_weather_now,
        interval_seconds=tasks.defaults_from_config(parser, "weather_interval_seconds", QWEATHER_CACHE_SECONDS),
        minimum_seconds=300,
        enabled=tasks.enabled_from_config(parser, "weather_enabled", True),
        description="清空缓存并重新请求和风天气",
    ))
    tasks.register(tasks.Task(
        name="bilibili",
        label="Bilibili 收藏夹同步",
        handler=sync_bilibili_now,
        interval_seconds=tasks.defaults_from_config(parser, "bilibili_interval_seconds", 6 * 3600),
        minimum_seconds=600,
        enabled=tasks.enabled_from_config(parser, "bilibili_enabled", False),
        description="下载收藏夹音频与封面并更新播放列表",
    ))
    tasks.register(tasks.Task(
        name="news",
        label="AI 新闻更新",
        handler=refresh_news_now,
        interval_seconds=tasks.defaults_from_config(parser, "news_interval_seconds", ainews.REFRESH_SECONDS),
        minimum_seconds=300,
        enabled=tasks.enabled_from_config(parser, "news_enabled", True),
        description="通过 Tavily 搜索并用 AI 汇总 Miku 动态",
    ))
    tasks.register(tasks.Task(
        name="cache_cleanup",
        label="缓存清理",
        handler=cleanup_cache,
        interval_seconds=tasks.defaults_from_config(parser, "cache_cleanup_interval_seconds", 7 * 24 * 3600),
        minimum_seconds=24 * 3600,
        enabled=tasks.enabled_from_config(parser, "cache_cleanup_enabled", True),
        description="清理不再被 music.json 引用的媒体文件",
    ))
    tasks.load_state()


configure(DEFAULT_CONFIG_PATH)


def health_report() -> dict:
    """收集供 /healthz 与管理面板使用的机器可读状态快照。"""
    checks: list[dict] = []
    playlist_count = 0
    song_count = 0
    try:
        playlist_count, song_count = validate_music_config()
        checks.append({"label": "播放列表配置", "ok": True, "good": "正常", "bad": "异常"})
    except RuntimeError as error:
        checks.append({"label": "播放列表配置", "ok": False, "good": "正常", "bad": str(error)[:80]})
    checks.append({
        "label": "媒体目录",
        "ok": MEDIA_ROOT.is_dir(),
        "good": "可访问",
        "bad": "不存在",
    })
    weather_ready = bool(QWEATHER_API_HOST and (QWEATHER_API_KEY or QWEATHER_BEARER_TOKEN))
    checks.append({"label": "天气接口凭据", "ok": weather_ready, "good": "已配置", "bad": "未配置"})
    with WEATHER_CACHE_LOCK:
        weather_cached = bool(WEATHER_CACHE)
    checks.append({
        "label": "天气数据缓存",
        "ok": weather_cached,
        "good": "已缓存",
        "bad": "尚未请求",
        "warn": not weather_cached,
    })
    news_status = ainews.status()
    if news_status["configured"]:
        news_ok = news_status["item_count"] > 0 and not news_status["last_error"]
        checks.append({
            "label": "AI 新闻",
            "ok": news_ok,
            "good": f"{news_status['item_count']} 条",
            "bad": news_status["last_error"][:80] or "暂无数据",
            "warn": not news_ok and not news_status["last_error"],
        })
    else:
        checks.append({"label": "AI 新闻", "ok": False, "good": "已启用", "bad": "未配置", "warn": True})
    failed = [item for item in checks if not item["ok"] and not item.get("warn")]
    warned = [item for item in checks if not item["ok"] and item.get("warn")]
    status = "ok" if not failed and not warned else ("warn" if not failed else "error")
    return {
        "status": status,
        "checks": checks,
        "listen": f"{LISTEN_HOST}:{LISTEN_PORT}",
        "public_base_url": PUBLIC_BASE_URL,
        "weather_city": f"{WEATHER_CITY} ({WEATHER_LATITUDE:.4f}, {WEATHER_LONGITUDE:.4f})",
        "media_root": str(MEDIA_ROOT),
        "playlist_count": playlist_count,
        "song_count": song_count,
    }


def admin_snapshot(message: str = "", error: str = "") -> dict:
    try:
        catalog = load_music_config()
    except RuntimeError:
        catalog = {"playlists": []}
    playlists = catalog.get("playlists", [])
    return {
        "stats": webui.stats_summary(),
        "health": health_report(),
        "news": ainews.status(),
        "bilibili": webui.bilibili_status(playlists),
        "tasks": tasks.status(),
        "playlists": uploads.uploadable_playlists(catalog),
        "bilibili_ready": BILIBILI_CONFIGURED,
        "message": message,
        "error": error,
    }


# 同一台机器在不同地区 ROM 的型号前缀：docomo Xperia A = SO-04E，
# 国际版 Xperia ZR = C5502 / C5503，中国版 = M36h。
LEGACY_OTENKIMIKU_MODELS = frozenset({"SO-04E", "C5502", "C5503", "M36h"})


def legacy_otenkimiku_apid(value: str) -> bool:
    """校验旧版天气小组件的 APID，前缀机型不区分大小写。"""
    return value.strip().upper() in {
        f"{model}_OTENKIMIKU".upper() for model in LEGACY_OTENKIMIKU_MODELS
    }


class CompatibilityHandler(BaseHTTPRequestHandler):
    server_version = "MikuxperiaCompatibility/1.0"

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, body: str, content_type: str = "application/xml; charset=UTF-8", status: int = 200) -> None:
        self.send_bytes(body.encode("utf-8"), content_type, status)

    def send_html(self, body: str, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        self.send_bytes(body.encode("utf-8"), "text/html; charset=UTF-8", status, extra_headers)

    def redirect(self, location: str, extra_headers: dict[str, str] | None = None) -> None:
        headers = {"Location": location}
        headers.update(extra_headers or {})
        self.send_bytes(b"", "text/plain; charset=UTF-8", 303, headers)

    def authenticated(self) -> bool:
        return webui.valid_session(webui.session_token(self.headers.get("Cookie", "")))

    def track(self, path: str) -> None:
        try:
            webui.record_request(path, self.client_address[0])
        except Exception as error:
            print(f"Visit statistics error: {error}")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        self.track(path)
        admin_base = webui.admin_base()
        routes = {
            "/resources/xml/MikuNews/list.xml": self.news_xml,
            "/resources/xml/MikuDownloader/applist.xml": self.apps_xml,
            "/resources/xml/MikuDownloader/noticelist.xml": self.notices_xml,
            "/resources/xml/FeatureSongsPlayer/playlist.xml": self.playlist_xml,
        }
        if path == "/healthz":
            report = health_report()
            body = json.dumps({
                "status": report["status"],
                "playlists": report["playlist_count"],
                "songs": report["song_count"],
                "news_items": ainews.status()["item_count"],
                "today_requests": webui.stats_summary()["today"]["requests"],
            }, ensure_ascii=False)
            self.send_text(body, "application/json; charset=UTF-8", 200 if report["status"] != "error" else 503)
        elif path == admin_base or path.startswith(admin_base + "/"):
            if not self.admin_https_ok():
                self.upgrade_to_https()
                return
            self.admin_get(path, admin_base)
        elif path in routes:
            try:
                self.send_text(routes[path]())
            except RuntimeError as error:
                print(f"Configuration error: {error}")
                self.send_text(str(error), "text/plain; charset=UTF-8", 500)
        elif path.endswith(".png"):
            self.send_media(path)
        elif path.endswith(".wav"):
            self.send_media(path)
        elif path.endswith((".mp3", ".m4a", ".aac", ".ogg", ".webm", ".jpg", ".jpeg", ".webp")):
            self.send_media(path)
        elif path.endswith(".txt"):
            self.send_media(path)
        elif path == "/" or path.startswith("/pages/"):
            self.send_text("<html><body><h1>Mikuxperia compatibility server</h1></body></html>", "text/html; charset=UTF-8")
        else:
            self.send_text("Not found", "text/plain; charset=UTF-8", 404)

    def admin_get(self, path: str, base: str) -> None:
        if not webui.available():
            self.send_text("WebUI 未启用。请在 miku.conf 的 [webui] 中设置 enabled 与 password。", "text/plain; charset=UTF-8", 404)
            return
        if path in (base, base + "/"):
            if not self.authenticated():
                self.send_html(webui.login_page())
                return
            self.send_html(webui.dashboard_page(admin_snapshot()))
        elif path == f"{base}/api/status":
            if not self.authenticated():
                self.send_text('{"error":"unauthorized"}', "application/json; charset=UTF-8", 401)
                return
            self.send_text(json.dumps(admin_snapshot(), ensure_ascii=False), "application/json; charset=UTF-8")
        else:
            self.send_text("Not found", "text/plain; charset=UTF-8", 404)

    def send_media(self, request_path: str) -> None:
        name = unquote(request_path)
        name = name[len("/media/"):] if name.startswith("/media/") else name.rsplit("/", 1)[-1]
        name = name.lstrip("/")
        if name.rsplit("/", 1)[-1] == "_empty.txt":
            self.send_text("", "text/plain; charset=UTF-8")
            return
        try:
            path = media_file(name)
        except RuntimeError:
            self.send_text("Not found", "text/plain; charset=UTF-8", 404)
            return
        if path is None and name == "debug.wav":
            self.send_bytes(WAV, "audio/wav")
            return
        if path is None and name == "debug.png":
            self.send_bytes(PNG, "image/png")
            return
        if path is None and name == "debug.txt":
            self.send_text("[00:00.00]Mikuxperia compatibility server\n[00:01.00]Debug audio\n", "text/plain; charset=UTF-8")
            return
        if path is None:
            self.send_text("Not found", "text/plain; charset=UTF-8", 404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_bytes(path.read_bytes(), content_type)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        self.track(path)
        admin_base = webui.admin_base()
        content_type = self.headers.get("Content-Type", "")
        if (path == admin_base or path.startswith(admin_base + "/")) and not self.admin_https_ok():
            self.upgrade_to_https()
            return
        if path == f"{admin_base}/upload" and "multipart/form-data" in content_type.lower():
            self.admin_upload(content_type)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        if path == "/getdata.php":
            try:
                self.send_text(self.weather_xml(form))
            except (KeyError, RuntimeError, UnicodeError, ValueError) as error:
                print(f"Weather adapter error: {error}")
                self.send_text(str(error), "text/plain; charset=UTF-8", 502)
        elif path == "/feature_songs_provider/addresses":
            try:
                self.send_text(self.addresses_xml(form))
            except RuntimeError as error:
                print(f"Music configuration error: {error}")
                self.send_text(str(error), "text/plain; charset=UTF-8", 500)
        elif path == admin_base or path.startswith(admin_base + "/"):
            self.admin_post(path, admin_base, form)
        else:
            self.send_text("Not found", "text/plain; charset=UTF-8", 404)

    def admin_upload(self, content_type: str) -> None:
        if not webui.available():
            self.send_text("WebUI 未启用", "text/plain; charset=UTF-8", 404)
            return
        if not self.authenticated():
            self.redirect(webui.admin_base())
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self.send_html(webui.dashboard_page(admin_snapshot(error="上传内容为空")), 400)
            return
        if length > uploads.MAX_TOTAL_BYTES:
            limit = uploads.MAX_TOTAL_BYTES // (1024 * 1024)
            self.send_html(webui.dashboard_page(admin_snapshot(error=f"上传大小超过 {limit} MB 限制")), 413)
            return
        body = self.rfile.read(length)
        try:
            fields, files = uploads.parse_multipart(body, content_type)
            message = uploads.store(fields, files, MEDIA_ROOT, MUSIC_CONFIG)
            self.send_html(webui.dashboard_page(admin_snapshot(message=message)))
        except uploads.UploadError as error:
            self.send_html(webui.dashboard_page(admin_snapshot(error=str(error))), 400)
        except Exception as error:
            print(f"Upload failed: {error}")
            self.send_html(webui.dashboard_page(admin_snapshot(error=f"上传失败：{error}")), 500)

    def admin_post(self, path: str, base: str, form: dict[str, list[str]]) -> None:
        if not webui.available():
            self.send_text("WebUI 未启用", "text/plain; charset=UTF-8", 404)
            return
        secure = self.https_request()
        if path == f"{base}/login":
            allowed, retry_after = webui.gate_status(self.client_address[0])
            if not allowed:
                self.send_html(
                    webui.login_page(f"尝试次数过多，请 {retry_after} 秒后再试"),
                    429,
                    {"Retry-After": str(retry_after)},
                )
                return
            if webui.password_matches(form.get("password", [""])[0]):
                webui.gate_success(self.client_address[0])
                self.redirect(base, {"Set-Cookie": webui.session_cookie(base, secure)})
            else:
                webui.gate_failure(self.client_address[0])
                self.send_html(webui.login_page("密码错误"), 401)
            return
        if not self.authenticated():
            self.redirect(base)
            return
        if path == f"{base}/logout":
            webui.drop_session(webui.session_token(self.headers.get("Cookie", "")))
            self.redirect(base, {"Set-Cookie": webui.clear_session_cookie(base, secure)})
        elif path == f"{base}/task/run":
            name = form.get("task", [""])[0].strip()
            ok, message = tasks.run_async(name)
            self.send_html(webui.dashboard_page(admin_snapshot(message if ok else "", "" if ok else message)))
        elif path == f"{base}/task/interval":
            name = form.get("task", [""])[0].strip()
            raw = form.get("interval", [""])[0].strip()
            unit = form.get("unit", ["minutes"])[0].strip()
            multiplier = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}.get(unit, 60)
            try:
                seconds = int(float(raw) * multiplier)
            except ValueError:
                self.send_html(webui.dashboard_page(admin_snapshot(error="间隔必须是数字")), 400)
                return
            ok, message = tasks.set_interval(name, seconds)
            self.send_html(webui.dashboard_page(admin_snapshot(message if ok else "", "" if ok else message)))
        elif path == f"{base}/task/toggle":
            name = form.get("task", [""])[0].strip()
            enabled = form.get("enabled", ["0"])[0].strip() in ("1", "true", "on", "yes")
            ok, message = tasks.set_enabled(name, enabled)
            self.send_html(webui.dashboard_page(admin_snapshot(message if ok else "", "" if ok else message)))
        elif path == f"{base}/news/refresh":
            ok, message = tasks.run_async("news")
            self.send_html(webui.dashboard_page(admin_snapshot(message if ok else "", "" if ok else message)))
        else:
            self.send_text("Not found", "text/plain; charset=UTF-8", 404)

    def news_xml(self) -> str:
        now = datetime.now(timezone(timedelta(hours=9)))
        host = base_url(self)
        entries = ainews.items()
        if entries:
            items = []
            for index, entry in enumerate(entries, start=1):
                link = entry.get("url") or f"{host}/pages/news"
                category = {"song": "music", "event": "event"}.get(entry.get("category", "news"), "news")
                items.append(f'''  <item>
    <id>{index}</id>
    <title>{escape(entry["title"])}</title>
    <link>{escape(link)}</link>
    <description>{escape(entry.get("summary", ""))}</description>
    <pubdate>{rfc_date(now)}</pubdate>
    <thumbnail>debug.png</thumbnail>
    <category>{escape(category)}</category>
  </item>''')
            return f'''<?xml version="1.0" encoding="UTF-8"?>
<mikunews>
  <lastBuildDate>{rfc_date(now)}</lastBuildDate>
  <notice>Miku 新闻由 AI 汇总。</notice>
{chr(10).join(items)}
</mikunews>'''
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<mikunews>
  <lastBuildDate>{rfc_date(now)}</lastBuildDate>
  <notice>Compatibility service is online.</notice>
  <item>
    <id>1</id>
    <title>Mikuxperia debug service</title>
    <link>{host}/pages/news</link>
    <description>Local compatibility endpoint</description>
    <pubdate>{rfc_date(now)}</pubdate>
    <thumbnail>debug.png</thumbnail>
    <category>illustration</category>
  </item>
</mikunews>'''

    def apps_xml(self) -> str:
        now = datetime.now(timezone(timedelta(hours=9)))
        host = base_url(self)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<mikudownloaderapps>
  <lastBuildDate>{rfc_date(now)}</lastBuildDate>
  <notice>Debug applications use the local compatibility service.</notice>
  <item>
    <id>1</id><name>Miku News Debug</name><link>{host}/pages/news</link>
    <packageName>com.mikuxperia.mikunewsapp</packageName>
    <className>com.mikuxperia.mikunewsapp.activity.MikuNewsActivity</className>
    <pubDate>{rfc_date(now)}</pubDate>
  </item>
  <item>
    <id>2</id><name>Feature Songs Debug</name><link>{host}/pages/songs</link>
    <packageName>com.mikuxperia.featuresongsplayerapp</packageName>
    <className>com.mikuxperia.featuresongsplayerapp.MusicListActivity</className>
    <pubDate>{rfc_date(now)}</pubDate>
  </item>
</mikudownloaderapps>'''

    def notices_xml(self) -> str:
        now = datetime.now(timezone(timedelta(hours=9)))
        host = base_url(self)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<mikudownloaderapps>
  <lastBuildDate>{rfc_date(now)}</lastBuildDate>
  <item><id>1</id><title>Compatibility service online</title>
    <link>{host}/pages/downloader</link>
    <packageName>com.mikuxperia.mikudownloader</packageName>
    <className>com.mikuxperia.mikudownloader.activity.MikuDownloaderActivity</className>
    <pubDate>{rfc_date(now)}</pubDate></item>
</mikudownloaderapps>'''

    def playlist_xml(self) -> str:
        now = datetime.now(timezone(timedelta(hours=9)))
        host = base_url(self)
        playlists = []
        for playlist in load_music_config()["playlists"]:
            songs = []
            for song in playlist.get("songs", []):
                music = song["music"]
                thumbnail = song.get("thumbnail", "debug.png")
                lyrics = song.get("lyrics", "debug.txt")
                wire_music = wire_media_name(music, playlist["id"])
                wire_thumbnail = wire_media_name(thumbnail, playlist["id"]) if thumbnail else ""
                wire_lyrics = wire_media_name(lyrics, playlist["id"]) if lyrics else ""
                songs.append(f'''<item><id>{escape(str(song.get("id", music)))}</id>
<title>{escape(str(song.get("title", music)))}</title><artist>{escape(str(song.get("artist", "Unknown")))}</artist>
<time>{escape(str(song.get("date", now.strftime("%Y.%m.%d"))))}</time>
<musicFileName>{escape(wire_music)}</musicFileName><thumbnailFileName>{escape(wire_thumbnail)}</thumbnailFileName>
<link>{escape(str(song.get("link", "")))}</link><lyricsFileName>{escape(wire_lyrics)}</lyricsFileName>
<musicFileSize>{media_size(music)}</musicFileSize><thumbnailFileSize>{media_size(thumbnail)}</thumbnailFileSize>
<lyricsFileSize>{media_size(lyrics)}</lyricsFileSize></item>''')
            playlist_id = int(playlist["id"])
            playlist_date = version_date(playlist.get("version", 1))
            image = playlist.get("image", "debug.png")
            brief_image = playlist.get("brief_image", image)
            playlists.append(f'''<playlist><id>{playlist_id}</id><title>{escape(str(playlist.get("title", playlist_id)))}</title>
<description>{escape(str(playlist.get("description", "")))}</description>
<descriptionImage>{host}/media/{escape(image)}</descriptionImage><pubDate>{rfc_date(playlist_date)}</pubDate>
<briefDescription>{escape(str(playlist.get("brief_description", "")))}</briefDescription>
<briefDescriptionImage>{host}/media/{escape(brief_image)}</briefDescriptionImage>{''.join(songs)}</playlist>''')
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<featuresongsplayer><lastBuildDate>{rfc_date(now)}</lastBuildDate>{''.join(playlists)}</featuresongsplayer>'''

    def addresses_xml(self, form: dict[str, list[str]]) -> str:
        host = base_url(self)
        items = []
        pattern = re.compile(r"feature_songs_provider\[\{(\d+)\}\]\[playlist_id\]")
        for key in sorted(form, key=lambda item: int(pattern.match(item).group(1)) if pattern.match(item) else -1):
            match = pattern.fullmatch(key)
            if not match:
                continue
            index = match.group(1)
            prefix = f"feature_songs_provider[{{{index}}}]"
            playlist_id = form[key][0]
            wire_music = form.get(f"{prefix}[musicFileName]", ["debug.wav"])[0]
            wire_thumbnail = form.get(f"{prefix}[thumbnailFileName]", ["debug.png"])[0]
            wire_lyrics = form.get(f"{prefix}[lyricsFileName]", ["debug.txt"])[0]
            music = resolve_wire_media(wire_music)
            thumbnail = resolve_wire_media(wire_thumbnail)
            lyrics = resolve_wire_media(wire_lyrics)
            media_size(music)
            media_size(thumbnail)
            media_size(lyrics)
            lyrics_url = f"{host}/media/{escape(lyrics)}" if lyrics else f"{host}/media/_empty.txt"
            items.append(f'''<item><playlistId>{escape(playlist_id)}</playlistId>
<music><fileName>{escape(wire_music)}</fileName><url>{host}/media/{escape(music)}</url></music>
<thumbnail><fileName>{escape(wire_thumbnail)}</fileName><url>{host}/media/{escape(thumbnail)}</url></thumbnail>
<lyrics><fileName>{escape(wire_lyrics)}</fileName><url>{lyrics_url}</url></lyrics></item>''')
        return '<?xml version="1.0" encoding="UTF-8"?><featuresongsplayer><addresses>' + "".join(items) + "</addresses></featuresongsplayer>"

    def weather_xml(self, form: dict[str, list[str]]) -> str:
        area = form.get("AREA", ["4410"])[0].zfill(4)
        if not legacy_otenkimiku_apid(form.get("APID", [""])[0]):
            raise RuntimeError("invalid legacy APID")
        payload = fetch_qweather()
        days = payload["days"][:8]
        kinds = ["today", "tomorrow"] + [f"weekly{index}" for index in range(1, 7)]
        groups = [legacy_day_xml(kind, day, index < 2) for index, (kind, day) in enumerate(zip(kinds, days))]
        updated = str(days[0].get("forecastStartTime", ""))
        try:
            now = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            now = datetime.now()
        update = f"{now.year}年{now.month}月{now.day}日{now.hour}時発表"
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<response><id>{int(area)}</id><point>{escape(WEATHER_CITY)}（和风天气）</point><update>{update}</update>{''.join(groups)}</response>'''

    def https_request(self) -> bool:
        """判断当前请求是否应视为 HTTPS（决定会话 Cookie Secure 与 https 限制）。

        依次识别：配置为 https 的 public_base_url、可信反代注入的
        X-Forwarded-Proto / X-Forwarded-Ssl、Cloudflare 的 CF-Visitor。
        """
        if PUBLIC_BASE_URL.lower().startswith("https://"):
            return True
        proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        if proto == "https":
            return True
        ssl = self.headers.get("X-Forwarded-Ssl", "").strip().lower()
        if ssl in ("on", "1", "true"):
            return True
        try:
            visitor = json.loads(self.headers.get("CF-Visitor", ""))
            if visitor.get("scheme") == "https":
                return True
        except (ValueError, TypeError):
            pass
        return False

    def admin_https_ok(self) -> bool:
        """[webui] require_https = true 时，管理面板只接受 HTTPS 请求。"""
        return not webui.require_https() or self.https_request()

    def upgrade_to_https(self) -> None:
        """管理面板的明文请求：GET 升级到 https，其余方法直接拒绝。"""
        host = self.headers.get("Host", "")
        if self.command == "GET" and host:
            location = f"https://{host}{self.path}"
            self.send_bytes(b"", "text/html; charset=UTF-8", 302, {"Location": location})
            return
        self.send_html("<html><body><h1>管理面板仅允许通过 HTTPS 访问</h1></body></html>", 403)

    def setup(self) -> None:
        super().setup()
        try:
            # 请求头/请求体的单次读操作超时，防止 slowloris 占住线程。
            self.connection.settimeout(READ_TIMEOUT_SECONDS)
        except OSError:
            pass

    def handle(self) -> None:
        try:
            super().handle()
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            # 读超时或客户端提前断开：安静关闭连接，不打印堆栈。
            pass

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mikuxperia APK compatibility server")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--check", action="store_true", help="validate configuration and music files, then exit")
    args = parser.parse_args()
    configure(args.config)
    if args.check:
        playlists, songs = validate_music_config()
        print(f"Configuration OK: {playlists} playlist(s), {songs} song(s)")
        return
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), CompatibilityHandler)
    print(f"Config: {args.config.expanduser().resolve()}")
    print(f"Serving on http://{LISTEN_HOST}:{LISTEN_PORT}")
    if webui.available():
        scheme = "https" if webui.require_https() else "http"
        print(f"Admin WebUI: {scheme}://{LISTEN_HOST}:{LISTEN_PORT}{webui.admin_base()}")
        if not webui.require_https():
            print("  Warning: admin panel is NOT restricted to HTTPS ([webui] require_https = true 可开启)")
    else:
        print("Admin WebUI disabled: set [webui] enabled and password in the config file")
    for task in tasks.status():
        state = "enabled" if task["enabled"] else "disabled"
        print(f"Task {task['name']}: {state}, every {task['interval_text']}")
    tasks.start_scheduler()
    try:
        server.serve_forever()
    finally:
        tasks.stop_scheduler()


if __name__ == "__main__":
    main()
