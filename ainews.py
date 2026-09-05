"""AI 辅助的 Miku 新闻聚合。

搜索结果来自 Tavily API，再交给任意 OpenAI 兼容的 chat completions 接口做中文
汇总。两者都是可选的：没有配置摘要模型时直接使用原始搜索结果；没有配置 Tavily
时本模块保持空闲，旧版新闻接口回退到静态占位条目。
"""

from __future__ import annotations

import configparser
import gzip
import json
import re
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ENABLED = False
TAVILY_API_KEY = ""
TAVILY_ENDPOINT = "https://api.tavily.com/search"
TAVILY_MAX_RESULTS = 8
TAVILY_TOPIC = "news"
SEARCH_QUERIES: list[str] = []
OPENAI_BASE_URL = ""
OPENAI_API_KEY = ""
OPENAI_MODEL = ""
REFRESH_SECONDS = 3600
MAX_ITEMS = 8
TIMEOUT_SECONDS = 30
CACHE_FILE: Path | None = None

DEFAULT_QUERIES = (
    "初音ミク 新曲 最新情報",
    "初音未来 最新新闻 动态",
    "Hatsune Miku news new song",
)

_LOCK = threading.Lock()
_ITEMS: list[dict] = []
_FETCHED_AT = 0.0
_LAST_ERROR = ""
_LAST_SOURCE = ""
_REFRESHING = False


def configure(parser: configparser.ConfigParser, base_dir: Path) -> None:
    """读取可选的 [ainews] 小节，并清空内存中的缓存。"""
    global ENABLED, TAVILY_API_KEY, TAVILY_ENDPOINT, TAVILY_MAX_RESULTS, TAVILY_TOPIC
    global SEARCH_QUERIES, OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL
    global REFRESH_SECONDS, MAX_ITEMS, TIMEOUT_SECONDS, CACHE_FILE
    global _ITEMS, _FETCHED_AT, _LAST_ERROR, _LAST_SOURCE

    section = "ainews"
    if not parser.has_section(section):
        ENABLED = False
        TAVILY_API_KEY = ""
        OPENAI_API_KEY = ""
        SEARCH_QUERIES = list(DEFAULT_QUERIES)
        CACHE_FILE = None
        with _LOCK:
            _ITEMS = []
            _FETCHED_AT = 0.0
            _LAST_ERROR = ""
            _LAST_SOURCE = ""
        return

    def text(option: str, default: str = "") -> str:
        return parser.get(section, option, fallback=default).strip()

    ENABLED = parser.getboolean(section, "enabled", fallback=False)
    TAVILY_API_KEY = text("tavily_api_key")
    TAVILY_ENDPOINT = text("tavily_endpoint", "https://api.tavily.com/search") or "https://api.tavily.com/search"
    TAVILY_MAX_RESULTS = max(1, min(20, parser.getint(section, "tavily_max_results", fallback=8)))
    TAVILY_TOPIC = text("tavily_topic", "news") or "news"
    OPENAI_BASE_URL = text("openai_base_url").rstrip("/")
    OPENAI_API_KEY = text("openai_api_key")
    OPENAI_MODEL = text("openai_model")
    REFRESH_SECONDS = max(300, parser.getint(section, "refresh_seconds", fallback=3600))
    MAX_ITEMS = max(1, min(30, parser.getint(section, "max_items", fallback=8)))
    TIMEOUT_SECONDS = max(5, min(120, parser.getint(section, "timeout_seconds", fallback=30)))

    queries = [line.strip() for line in text("search_queries").splitlines() if line.strip()]
    SEARCH_QUERIES = queries or list(DEFAULT_QUERIES)

    cache_value = Path(text("cache_file", "ainews-cache.json") or "ainews-cache.json")
    CACHE_FILE = (cache_value if cache_value.is_absolute() else base_dir / cache_value).resolve()

    with _LOCK:
        _ITEMS = []
        _FETCHED_AT = 0.0
        _LAST_ERROR = ""
        _LAST_SOURCE = ""
    load_cache()


def configured() -> bool:
    return bool(ENABLED and TAVILY_API_KEY)


def summariser_configured() -> bool:
    return bool(OPENAI_BASE_URL and OPENAI_API_KEY and OPENAI_MODEL)


def load_cache() -> None:
    """恢复上一次成功抓取的结果，避免重启后新闻列表变空。"""
    global _ITEMS, _FETCHED_AT, _LAST_SOURCE
    if CACHE_FILE is None or not CACHE_FILE.is_file():
        return
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    items = payload.get("items")
    if not isinstance(items, list):
        return
    with _LOCK:
        _ITEMS = [item for item in items if isinstance(item, dict) and item.get("title")][:MAX_ITEMS]
        _FETCHED_AT = float(payload.get("fetched_at") or 0.0)
        _LAST_SOURCE = str(payload.get("source") or "cache")


def save_cache() -> None:
    if CACHE_FILE is None:
        return
    with _LOCK:
        payload = {"fetched_at": _FETCHED_AT, "source": _LAST_SOURCE, "items": _ITEMS}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(CACHE_FILE)
    except OSError as error:
        print(f"AI news cache write failed: {error}")


def decode_body(data: bytes, content_encoding: str = "") -> dict:
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
        raise RuntimeError(f"invalid JSON response: {error}") from error


def post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json", **headers}
    request = Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return decode_body(response.read(), response.headers.get("Content-Encoding", ""))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {error.code} from {url}: {detail}") from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"request to {url} failed: {error}") from error


def tavily_search(query: str) -> list[dict]:
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": TAVILY_MAX_RESULTS,
        "search_depth": "basic",
        "topic": TAVILY_TOPIC,
        "include_answer": False,
    }
    headers = {"Authorization": f"Bearer {TAVILY_API_KEY}"}
    data = post_json(TAVILY_ENDPOINT, payload, headers)
    results = data.get("results")
    if not isinstance(results, list):
        raise RuntimeError("Tavily response contains no results array")
    found = []
    for result in results:
        if not isinstance(result, dict):
            continue
        title = str(result.get("title", "")).strip()
        url = str(result.get("url", "")).strip()
        if not title or not url.startswith(("http://", "https://")):
            continue
        found.append({
            "title": title,
            "url": url,
            "content": str(result.get("content", "")).strip(),
            "published": str(result.get("published_date", "")).strip(),
        })
    return found


def collect_results() -> list[dict]:
    seen: set[str] = set()
    collected: list[dict] = []
    errors: list[str] = []
    for query in SEARCH_QUERIES:
        try:
            for result in tavily_search(query):
                if result["url"] in seen:
                    continue
                seen.add(result["url"])
                collected.append(result)
        except RuntimeError as error:
            errors.append(str(error))
    if not collected and errors:
        raise RuntimeError(errors[0])
    return collected


def extract_json_array(text: str) -> list:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end <= start:
        raise RuntimeError("summariser returned no JSON array")
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"summariser returned invalid JSON: {error}") from error
    if not isinstance(parsed, list):
        raise RuntimeError("summariser returned a non-array payload")
    return parsed


def summarise(results: list[dict]) -> list[dict]:
    sources = [
        {"index": index + 1, "title": item["title"], "url": item["url"], "excerpt": item["content"][:600]}
        for index, item in enumerate(results[:20])
    ]
    instruction = (
        "你是初音未来（Hatsune Miku）与 VOCALOID 领域的新闻编辑。"
        "请根据提供的搜索结果，整理最新的新闻、活动与新歌信息。"
        f"最多输出 {MAX_ITEMS} 条，按重要性排序，全部使用简体中文。"
        "只输出 JSON 数组，不要输出其他文字。"
        '每个元素格式为 {"title": "标题", "summary": "两句以内的摘要", '
        '"category": "news 或 song 或 event", "url": "来源链接", "date": "YYYY-MM-DD 或空字符串"}。'
        "url 必须来自给定的搜索结果，不要编造链接或事实。"
    )
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(sources, ensure_ascii=False)},
        ],
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = post_json(f"{OPENAI_BASE_URL}/chat/completions", payload, headers)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("summariser response contains no choices")
    content = str(((choices[0] or {}).get("message") or {}).get("content", ""))
    if not content.strip():
        raise RuntimeError("summariser returned an empty message")
    allowed = {item["url"] for item in results}
    items: list[dict] = []
    for entry in extract_json_array(content):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not title:
            continue
        if url not in allowed:
            url = results[0]["url"] if results else ""
        category = str(entry.get("category", "news")).strip().lower()
        items.append({
            "title": title[:160],
            "summary": re.sub(r"\s+", " ", str(entry.get("summary", "")).strip())[:400],
            "category": category if category in {"news", "song", "event"} else "news",
            "url": url,
            "date": str(entry.get("date", "")).strip()[:10],
        })
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise RuntimeError("summariser produced no usable items")
    return items


def fallback_items(results: list[dict]) -> list[dict]:
    items = []
    for result in results[:MAX_ITEMS]:
        items.append({
            "title": result["title"][:160],
            "summary": re.sub(r"\s+", " ", result["content"])[:400],
            "category": "news",
            "url": result["url"],
            "date": result["published"][:10],
        })
    return items


def refresh(force: bool = False) -> tuple[int, str]:
    """抓取并保存最新条目，返回（条目数，错误信息）。"""
    global _ITEMS, _FETCHED_AT, _LAST_ERROR, _LAST_SOURCE, _REFRESHING
    if not configured():
        return 0, "AI 新闻未配置或未启用"
    with _LOCK:
        if _REFRESHING:
            return len(_ITEMS), "已有刷新任务正在执行"
        if not force and _ITEMS and time.time() - _FETCHED_AT < REFRESH_SECONDS:
            return len(_ITEMS), ""
        _REFRESHING = True
    try:
        results = collect_results()
        if not results:
            raise RuntimeError("搜索没有返回任何结果")
        source = "tavily"
        if summariser_configured():
            try:
                items = summarise(results)
                source = f"tavily+{OPENAI_MODEL}"
            except RuntimeError as error:
                print(f"AI news summariser failed, using raw results: {error}")
                items = fallback_items(results)
                source = "tavily (总结失败)"
        else:
            items = fallback_items(results)
        with _LOCK:
            _ITEMS = items
            _FETCHED_AT = time.time()
            _LAST_ERROR = ""
            _LAST_SOURCE = source
        save_cache()
        return len(items), ""
    except RuntimeError as error:
        message = str(error)
        with _LOCK:
            _LAST_ERROR = message
        print(f"AI news refresh failed: {message}")
        return 0, message
    finally:
        with _LOCK:
            _REFRESHING = False


def items(auto_refresh: bool = True) -> list[dict]:
    """返回缓存的条目；缓存已过期时先刷新一次。"""
    if auto_refresh and configured():
        with _LOCK:
            stale = not _ITEMS or time.time() - _FETCHED_AT >= REFRESH_SECONDS
        if stale:
            refresh(force=False)
    with _LOCK:
        return list(_ITEMS)


def status() -> dict:
    with _LOCK:
        fetched = _FETCHED_AT
        return {
            "enabled": bool(ENABLED),
            "configured": configured(),
            "summariser": summariser_configured(),
            "model": OPENAI_MODEL if summariser_configured() else "",
            "queries": list(SEARCH_QUERIES),
            "item_count": len(_ITEMS),
            "items": list(_ITEMS),
            "refresh_seconds": REFRESH_SECONDS,
            "max_items": MAX_ITEMS,
            "source": _LAST_SOURCE,
            "last_error": _LAST_ERROR,
            "refreshing": _REFRESHING,
            "fetched_at": fetched,
            "fetched_text": (
                datetime.fromtimestamp(fetched, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                if fetched else ""
            ),
            "cache_file": str(CACHE_FILE) if CACHE_FILE else "",
        }
