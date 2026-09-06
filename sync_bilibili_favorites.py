#!/usr/bin/env python3
"""把 Bilibili 收藏夹同步进旧版播放列表目录。

脚本只下载选中的音频流和视频封面，不下载视频画面。请只处理你有权下载和再分发
的内容。
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import bilicookies


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "miku.conf"
USER_AGENT = "MikuxperiaFavoritesSync/1.0"
GENERATED_SOURCE = "bilibili-favorites"
BV_RE = re.compile(r"^BV[0-9A-Za-z]+$")
UNAVAILABLE_TITLES = {"已失效视频", "已失效稿件", "视频已失效", "已删除视频"}
# Android 客户端用 Integer.parseInt 解析播放列表 ID，必须落在 32 位有符号整数
# 范围内，而收藏夹 ID 本身就已经超出这个范围。
JAVA_INT_MAX = 2147483647
PLAYLIST_ID_BASE = 1000000000
PLAYLIST_ID_SPAN = 1000000000
# 按收藏夹推导出的播放列表 ID 落在 [10亿, ~20亿)，合集从 20 亿起，
# 保证与任何收藏夹分片、手工列表都不冲突。
COMBINED_ID_BASE = 2000000000
DEFAULT_COMBINED_TITLE = "Bilibili 收藏夹合集"
# 客户端把播放列表封面以 BLOB 形式存进 tbl_play_music_info 的一行，并通过
# 2 MiB 的 CursorWindow 读取。两张 1920x1080 的原图就会溢出，导致
# MusicListActivity 抛出 "Couldn't read row 0, col 0 from CursorWindow"
# 崩溃，因此这里把封面压得很小。
LIST_COVER_WIDTH = 240
BRIEF_COVER_WIDTH = 80
SONG_COVER_WIDTH = 170
DEFAULT_SONGS_PER_PLAYLIST = 50
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".webm", ".mp4"}
PREFERRED_AUDIO_SUFFIX = ".mp3"


def read_settings(path: Path) -> dict:
    parser = configparser.ConfigParser(interpolation=None)
    with path.expanduser().resolve().open("r", encoding="utf-8") as handle:
        parser.read_file(handle)
    if not parser.has_section("bilibili"):
        raise RuntimeError("missing [bilibili] section")
    folders: list[int] = []
    raw_ids = parser.get("bilibili", "folder_ids", fallback="")
    for value in re.split(r"[\s,]+", raw_ids.strip()):
        if value:
            folder_id = int(value)
            if folder_id <= 0 or folder_id in folders:
                raise RuntimeError(f"invalid or duplicate favorite folder ID: {value}")
            folders.append(folder_id)
    if not folders:
        raise RuntimeError("[bilibili] folder_ids is empty")

    def relative_path(option: str, default: str) -> Path:
        value = Path(parser.get("bilibili", option, fallback=default).strip())
        return (value if value.is_absolute() else path.parent / value).resolve()

    return {
        "folders": folders,
        "cookie_file": relative_path("cookie_file", "bilibili.cookies.txt"),
        "media_root": relative_path("media_root", "media/bilibili"),
        "catalog": relative_path("catalog", "music.json"),
        "page_size": max(1, min(20, parser.getint("bilibili", "page_size", fallback=20))),
        "maximum": max(0, parser.getint("bilibili", "max_videos_per_folder", fallback=0)),
        "songs_per_playlist": max(
            1,
            min(60, parser.getint("bilibili", "songs_per_playlist", fallback=DEFAULT_SONGS_PER_PLAYLIST)),
        ),
        "redownload": parser.getboolean("bilibili", "redownload_existing", fallback=False),
        # 在保留各收藏夹播放列表的同时，追加一个包含全部已下载歌曲的合集。
        "combined_enabled": parser.getboolean("bilibili", "combined_enabled", fallback=False),
        "combined_title": parser.get("bilibili", "combined_title", fallback="").strip() or DEFAULT_COMBINED_TITLE,
    }


def api_json(url: str, cookie_file: Path) -> dict:
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
    cookies = bilicookies.load_cookies(cookie_file)
    if cookies:
        headers["Cookie"] = bilicookies.cookie_header(cookies)
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Bilibili API request failed: {error}") from error
    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message", "unknown error"))
        if code in (-101, -400, -403, 11010):
            message += "（Cookie 可能失效或无权访问该收藏夹，请重新导出 Cookie）"
        raise RuntimeError(f"Bilibili API error {code}: {message}")
    return payload


def playable(video: dict) -> bool:
    """跳过收藏夹接口仍然返回、但谁都下载不了的条目。

    被删除或转为私密的视频仍保留 bvid，但 attr 不为 0 且标题是占位文案；
    非视频资源则带有 business 标记。
    """
    if video.get("business"):
        return False
    if not BV_RE.fullmatch(str(video.get("bvid", "")).strip()):
        return False
    try:
        if int(video.get("attr", 0) or 0) != 0:
            return False
    except (TypeError, ValueError):
        return False
    title = str(video.get("title", "")).strip()
    if title in UNAVAILABLE_TITLES:
        return False
    try:
        if int(video.get("type", 2) or 2) != 2:
            return False
    except (TypeError, ValueError):
        pass
    return True


def folder_info(folder_id: int, cookie_file: Path) -> dict:
    """取收藏夹自身的标题，用作播放列表名称。"""
    url = f"https://api.bilibili.com/x/v3/fav/folder/info?media_id={folder_id}"
    data = api_json(url, cookie_file).get("data") or {}
    return {
        "title": str(data.get("title", "")).strip(),
        "media_count": data.get("media_count", 0),
    }


def favorite_videos(folder_id: int, cookie_file: Path, page_size: int, maximum: int) -> tuple[list[dict], int]:
    videos: list[dict] = []
    skipped = 0
    page = 1
    while True:
        url = (
            "https://api.bilibili.com/x/v3/fav/resource/list?"
            f"media_id={folder_id}&pn={page}&ps={page_size}&platform=web"
        )
        data = api_json(url, cookie_file).get("data") or {}
        for video in data.get("medias") or []:
            if playable(video):
                videos.append(video)
                if maximum and len(videos) >= maximum:
                    return videos, skipped
            else:
                skipped += 1
        if not data.get("has_more") or not data.get("medias"):
            return videos, skipped
        page += 1


def safe_name(bvid: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", bvid)


def playlist_version() -> int:
    """生成一个旧客户端与服务端都能接受的、单调递增的小版本号。

    server.py 会把版本号映射成 pubDate 的偏移量，直接用 Unix 时间戳过大。
    取「2026-01-01 起的分钟数」既足够小，又能在每次同步时递增，从而在内容变化
    时给客户端一个不同的日期。
    """
    base = 1767225600  # 2026-01-01T00:00:00Z
    return max(1, int((time.time() - base) // 60))


def playlist_id_for(folder_id: int, part: int = 0) -> int:
    """由 Bilibili 收藏夹 ID 推导出稳定的 32 位播放列表 ID。

    客户端对播放列表 ID 调用 Integer.parseInt，超过 2147483647 会让
    MusicDataService 抛 NumberFormatException。像 3275482587 这样的收藏夹 ID
    已经超限，所以折算进一个固定区间。拆分出的分片按同一个收藏夹依次取相邻 ID。
    """
    folded = PLAYLIST_ID_BASE + (int(folder_id) % PLAYLIST_ID_SPAN) + max(0, int(part))
    while folded > JAVA_INT_MAX:
        folded = PLAYLIST_ID_BASE + (folded % PLAYLIST_ID_SPAN)
    return folded


def resize_jpeg(source: Path, target: Path, width: int) -> bool:
    """用 FFmpeg 缩放图片。FFmpeg 不存在时返回 False。"""
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(source),
        "-vf", f"scale={width}:-2:flags=lanczos",
        "-q:v", "4",
        str(target),
    ]
    try:
        subprocess.run(
            command, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return False
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip().splitlines()
        raise RuntimeError(f"缩放封面失败 {source.name}: {detail[-1] if detail else 'ffmpeg failed'}") from error
    return target.is_file() and target.stat().st_size > 0


def download_cover(url: str, target: Path, width: int = SONG_COVER_WIDTH) -> None:
    request = Request(url.replace("http://", "https://"), headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})
    with urlopen(request, timeout=30) as response:
        data = response.read()
    if not data:
        raise RuntimeError(f"empty cover response: {url}")
    target.write_bytes(data)
    if width <= 0:
        return
    scaled = target.with_name(f"{target.stem}.scaled{target.suffix}")
    try:
        if resize_jpeg(target, scaled, width) and scaled.stat().st_size < target.stat().st_size:
            scaled.replace(target)
    finally:
        scaled.unlink(missing_ok=True)


def shrink_existing_cover(target: Path, width: int = SONG_COVER_WIDTH) -> None:
    """把之前下载的封面就地压小，尺寸已经合适时不动。"""
    if not target.is_file():
        return
    scaled = target.with_name(f"{target.stem}.scaled{target.suffix}")
    try:
        if resize_jpeg(target, scaled, width) and scaled.stat().st_size < target.stat().st_size:
            scaled.replace(target)
    finally:
        scaled.unlink(missing_ok=True)


def convert_to_mp3(source: Path, redownload: bool = False) -> Path:
    """把旧缓存里的 AAC/M4A 转成 Android 4.2 更兼容的 MP3。"""
    if source.suffix.lower() == PREFERRED_AUDIO_SUFFIX:
        return source
    target = source.with_suffix(PREFERRED_AUDIO_SUFFIX)
    if target.is_file() and target.stat().st_size > 0 and not redownload:
        source.unlink(missing_ok=True)
        return target
    temporary = target.with_name(f"{target.stem}.tmp{target.suffix}")
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
        "-vn", "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        str(temporary),
    ]
    try:
        subprocess.run(
            command, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as error:
        raise RuntimeError("需要 ffmpeg 才能将 Bilibili 音频转换为 MP3") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "音频转换失败").strip().splitlines()[-1]
        raise SkipVideo(f"AAC/M4A 无法转换为 MP3（{detail}）") from error
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise SkipVideo("FFmpeg 未产生有效 MP3 文件")
    temporary.replace(target)
    source.unlink(missing_ok=True)
    return target


def playlist_cover(source: Path, width: int, suffix: str) -> Path | None:
    """在歌曲封面旁边生成一张缩小后的播放列表封面。"""
    if not source.is_file():
        return None
    target = source.with_name(f"{source.stem}.{suffix}{source.suffix}")
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    try:
        if resize_jpeg(source, target, width):
            return target
    except RuntimeError as error:
        print(f"  {error}")
    target.unlink(missing_ok=True)
    return None


def ensure_writable_dir(path: Path, label: str) -> None:
    """提前失败并给出可执行的处理建议，而不是抛出难懂的 yt-dlp 报错。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            f"无法创建{label} {path}：{error}。"
            f"请执行 sudo mkdir -p {path} && sudo chown -R {current_user()} {path}"
        ) from error
    if not os.access(path, os.W_OK | os.X_OK):
        owner = describe_owner(path)
        raise RuntimeError(
            f"{label} {path} 对当前用户 {current_user()} 不可写（当前属主 {owner}）。"
            f"请执行 sudo chown -R {current_user()} {path} && sudo chmod -R u+rwX {path}"
        )
    probe = path / ".write-test"
    try:
        probe.write_bytes(b"")
    except OSError as error:
        owner = describe_owner(path)
        raise RuntimeError(
            f"无法写入{label} {path}：{error}（当前属主 {owner}，运行用户 {current_user()}）。"
            f"请执行 sudo chown -R {current_user()} {path}"
        ) from error
    finally:
        probe.unlink(missing_ok=True)


def ensure_writable_file(path: Path, label: str) -> None:
    if path.exists() and not os.access(path, os.W_OK):
        raise RuntimeError(
            f"{label} {path} 对当前用户 {current_user()} 不可写（当前属主 {describe_owner(path)}）。"
            f"请执行 sudo chown {current_user()} {path} && sudo chmod u+rw {path}"
        )
    if not path.exists() and not os.access(path.parent, os.W_OK):
        raise RuntimeError(f"无法在 {path.parent} 创建{label}，目录不可写")


def current_user() -> str:
    try:
        import getpass

        name = getpass.getuser()
    except Exception:
        name = ""
    if hasattr(os, "getuid"):
        return f"{name or os.getuid()}:{os.getgid()}" if name else f"uid={os.getuid()}"
    return name or "current"


def describe_owner(path: Path) -> str:
    if not hasattr(os, "getuid"):
        return "未知"
    try:
        info = path.stat()
    except OSError:
        return "未知"
    owner = str(info.st_uid)
    group = str(info.st_gid)
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(info.st_uid).pw_name
        group = grp.getgrgid(info.st_gid).gr_name
    except Exception:
        pass
    return f"{owner}:{group} mode={oct(info.st_mode & 0o777)}"


class SkipVideo(RuntimeError):
    """某个视频无法下载、但整轮同步应当继续时抛出。"""


SKIP_PATTERNS = (
    ("KeyError('bvid')", "视频已失效或被删除"),
    ("Video unavailable", "视频不可用"),
    ("This video is unavailable", "视频不可用"),
    ("稿件不可见", "稿件不可见"),
    ("已失效", "视频已失效"),
    ("404 Not Found", "视频不存在"),
    ("-404", "视频不存在"),
    ("Private video", "私密视频"),
    ("需要登录", "需要登录或权限不足"),
    ("大会员", "需要大会员权限"),
    ("Unsupported URL", "不支持的链接类型"),
    ("charge", "付费内容"),
    ("Requested format is not available", "没有可用的纯音频流，可能是仅有 FLV 合并流的老视频"),
    ("No video formats found", "没有找到可用的媒体流"),
    ("-352", "触发 B 站风控，请降低同步频率后重试"),
    ("HTTP Error 412", "触发 B 站风控，请降低同步频率后重试"),
    ("风控", "触发 B 站风控，请降低同步频率后重试"),
    ("HTTP Error 403", "访问被拒绝，Cookie 可能失效或触发风控"),
    ("Unable to download webpage", "网页下载失败，可能是网络问题或风控"),
    ("Unable to download API page", "接口请求失败，可能是网络问题或风控"),
    ("timed out", "网络超时"),
    ("Connection reset", "连接被重置"),
    ("Temporary failure in name resolution", "DNS 解析失败"),
    ("An extractor error has occurred", "解析失败，可能已失效或需要更新 yt-dlp"),
)


def classify_failure(detail: str) -> str:
    for needle, reason in SKIP_PATTERNS:
        if needle in detail:
            return reason
    return ""


def error_detail(stderr: str, stdout: str) -> str:
    """挑出信息量最大的一行，而不是最后一行进度输出。

    yt-dlp 会把 "[BiliBili] xxx: Downloading webpage" 这样的进度写到 stdout，
    直接取最后一行会把真正的失败原因吞掉。
    """
    for stream in (stderr, stdout):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        for line in reversed(lines):
            if "ERROR:" in line or "error:" in line:
                return line
    for stream in (stderr, stdout):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        interesting = [line for line in lines if not line.startswith("[")]
        if interesting:
            return interesting[-1]
        if lines:
            return lines[-1]
    return "yt-dlp failed without output"


def ytdlp_command(prefix: Path, url: str, cookie_file: Path | None, formats: str) -> tuple[list[str], Path | None]:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-part",
        "--no-progress",
        "--no-warnings",
        "--restrict-filenames",
        "--socket-timeout",
        "30",
        "--retries",
        "3",
        "--extractor-retries",
        "2",
        "--sleep-requests",
        "1",
        "-f",
        formats,
        "-o",
        str(prefix) + ".%(ext)s",
        url,
    ]
    netscape, temporary = bilicookies.netscape_file_for(cookie_file) if cookie_file else (None, None)
    if netscape is not None:
        command[1:1] = ["--cookies", str(netscape)]
    return command, temporary


def download_audio(url: str, output_dir: Path, cookie_file: Path, bvid: str, redownload: bool = False) -> Path:
    ensure_writable_dir(output_dir, "音频缓存目录")
    prefix = output_dir / safe_name(bvid)
    existing = [
        path for path in output_dir.glob(f"{prefix.name}.*")
        if path.suffix.lower() in AUDIO_SUFFIXES and path.stat().st_size > 0
    ]
    if existing and not redownload:
        return existing[0]
    for old in output_dir.glob(f"{prefix.name}.*"):
        old.unlink()
    command, temporary = ytdlp_command(prefix, url, cookie_file, "bestaudio")
    command[1:1] = ["-x", "--audio-format", "mp3", "--audio-quality", "128K"]
    try:
        subprocess.run(
            command, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as error:
        raise RuntimeError("yt-dlp is not installed or not in PATH") from error
    except subprocess.CalledProcessError as error:
        stderr = error.stderr or ""
        stdout = error.stdout or ""
        output = stderr + "\n" + stdout
        detail = error_detail(stderr, stdout)
        if "Permission denied" in output or "unable to open for writing" in output:
            raise RuntimeError(
                f"audio download failed for {bvid}: {detail}"
                f"（目录 {output_dir} 属主 {describe_owner(output_dir)}，运行用户 {current_user()}，"
                f"请执行 sudo chown -R {current_user()} {output_dir.parent}）"
            ) from error
        reason = classify_failure(output)
        for leftover in output_dir.glob(f"{prefix.name}.*"):
            leftover.unlink(missing_ok=True)
        if reason:
            raise SkipVideo(f"{reason}（{detail}）") from error
        raise SkipVideo(f"下载失败：{detail}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    files = [
        path for path in output_dir.glob(f"{prefix.name}.*")
        if path.suffix.lower() in AUDIO_SUFFIXES and path.stat().st_size > 0
    ]
    if not files:
        raise SkipVideo("yt-dlp 未产生任何音频文件")
    return convert_to_mp3(files[0], redownload=True)


def probe_formats(bvid: str, cookie_file: Path) -> int:
    """打印 yt-dlp 看到的所有可用格式，用于手工诊断某个视频。"""
    url = f"https://www.bilibili.com/video/{bvid}"
    netscape, temporary = bilicookies.netscape_file_for(cookie_file)
    command = ["yt-dlp", "--no-playlist", "--list-formats", url]
    if netscape is not None:
        command[1:1] = ["--cookies", str(netscape)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("yt-dlp is not installed or not in PATH", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(result.stdout.strip() or "(no stdout)")
    if result.returncode != 0:
        print("--- stderr ---", file=sys.stderr)
        print((result.stderr or "").strip(), file=sys.stderr)
    return result.returncode


def load_catalog(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid catalog {path}: {error}") from error
    if not isinstance(value.get("playlists"), list):
        raise RuntimeError("catalog must contain a playlists array")
    return value


def sync_folder(
    folder_id: int,
    videos: list[dict],
    media_root: Path,
    cookie_file: Path,
    songs_per_playlist: int = DEFAULT_SONGS_PER_PLAYLIST,
    redownload: bool = False,
) -> tuple[list[dict], list[str]]:
    # 用收藏夹名称作为播放列表标题，取不到时退回收藏夹 ID。
    try:
        folder_data = folder_info(folder_id, cookie_file)
        folder_title = str(folder_data.get("title", "")).strip()
    except (RuntimeError, OSError, ValueError) as error:
        print(f"  获取收藏夹名称失败，使用默认标题：{error}")
        folder_title = ""
    if not folder_title:
        folder_title = f"Bilibili 收藏夹 {folder_id}"
    
    folder_dir = media_root / str(folder_id)
    ensure_writable_dir(folder_dir, "收藏夹缓存目录")
    songs = []
    skipped: list[str] = []
    for index, video in enumerate(videos, start=1):
        bvid = str(video["bvid"])
        title = re.sub(r"\s+", " ", str(video.get("title", bvid))).strip()
        audio_url = f"https://www.bilibili.com/video/{bvid}"
        try:
            audio = download_audio(audio_url, folder_dir, cookie_file, bvid, redownload)
            audio = convert_to_mp3(audio, redownload=redownload)
        except SkipVideo as error:
            skipped.append(f"{bvid} {title[:40]}：{error}")
            print(f"  [{index}/{len(videos)}] 跳过 {bvid}：{error}")
            continue
        cover = folder_dir / f"{safe_name(bvid)}.jpg"
        if not cover.exists():
            try:
                download_cover(str(video.get("cover", "")), cover)
            except (OSError, RuntimeError, URLError, HTTPError, TimeoutError) as error:
                print(f"  [{index}/{len(videos)}] {bvid} 封面下载失败：{error}")
                cover.unlink(missing_ok=True)
        else:
            try:
                shrink_existing_cover(cover)
            except (OSError, RuntimeError) as error:
                print(f"  [{index}/{len(videos)}] {bvid} 封面缩放失败：{error}")
        songs.append({
            "id": bvid,
            "title": title,
            "artist": str((video.get("upper") or {}).get("name", "未知作者")),
            "date": str(video.get("pubdate", ""))[:10].replace("-", "."),
            "music": audio.relative_to(media_root.parent).as_posix(),
            "thumbnail": cover.relative_to(media_root.parent).as_posix() if cover.is_file() else "",
            "lyrics": "",
            "link": audio_url,
        })
    playlists = []
    size = max(1, min(60, int(songs_per_playlist)))
    chunks = [songs[index:index + size] for index in range(0, len(songs), size)] or [[]]
    for part, chunk in enumerate(chunks):
        cover_songs = [song for song in chunk if song["thumbnail"]]
        title = folder_title
        if len(chunks) > 1:
            title += f" ({part + 1}/{len(chunks)})"
        image = cover_songs[0]["thumbnail"] if cover_songs else ""
        full_cover = playlist_cover(folder_dir / Path(image).name, LIST_COVER_WIDTH, "list") if image else None
        brief_cover = playlist_cover(folder_dir / Path(image).name, BRIEF_COVER_WIDTH, "brief") if image else None
        playlist = {
            "id": playlist_id_for(folder_id, part),
            "version": playlist_version(),
            "title": title,
            "description": f"来自 Bilibili 收藏夹 {folder_id}",
            "brief_description": f"来自 Bilibili 收藏夹 {folder_id}",
            "image": full_cover.relative_to(media_root.parent).as_posix() if full_cover else image,
            "brief_image": brief_cover.relative_to(media_root.parent).as_posix() if brief_cover else image,
            "source": GENERATED_SOURCE,
            "source_folder_id": folder_id,
            "source_part": part,
            "source_parts": len(chunks),
            "songs": chunk,
        }
        playlists.append(playlist)
    return playlists, skipped


def collect_folder_songs(generated: list[dict]) -> list[dict]:
    """按收藏夹顺序收集全部已成功下载的歌曲，跨文件夹重复的视频只保留首次出现。

    输入是各收藏夹生成的播放列表（每首歌曲的 id 即 bvid）。
    """
    seen: set[str] = set()
    songs: list[dict] = []
    for playlist in generated:
        for song in playlist.get("songs", []):
            key = str(song.get("id") or "").strip()
            if key:
                if key in seen:
                    continue
                seen.add(key)
            songs.append(song)
    return songs


def build_combined_playlists(
    folder_ids: list[int],
    songs: list[dict],
    media_root: Path,
    songs_per_playlist: int,
    title: str,
) -> list[dict]:
    """把去重后的全部歌曲打成合集播放列表（超出上限自动拆成 1/2/3… part）。

    封面沿用对应歌曲已缓存的封面，并就地生成 list/brief 小尺寸变体。
    """
    media_base = media_root.parent
    size = max(1, min(60, int(songs_per_playlist)))
    chunks = [songs[index:index + size] for index in range(0, len(songs), size)] or [[]]
    playlists: list[dict] = []
    for part, chunk in enumerate(chunks):
        cover_songs = [song for song in chunk if song.get("thumbnail")]
        image = cover_songs[0]["thumbnail"] if cover_songs else ""
        full_cover: Path | None = None
        brief_cover: Path | None = None
        if image:
            source = media_base / image
            if source.is_file():
                try:
                    full_cover = playlist_cover(source, LIST_COVER_WIDTH, "list")
                    brief_cover = playlist_cover(source, BRIEF_COVER_WIDTH, "brief")
                except (OSError, RuntimeError):
                    pass
        playlist_title = title
        if len(chunks) > 1:
            playlist_title += f" ({part + 1}/{len(chunks)})"
        description = f"来自 {len(folder_ids)} 个 Bilibili 收藏夹的合集"
        playlists.append({
            "id": COMBINED_ID_BASE + part,
            "version": playlist_version(),
            "title": playlist_title,
            "description": description,
            "brief_description": description,
            "image": full_cover.relative_to(media_base).as_posix() if full_cover else image,
            "brief_image": brief_cover.relative_to(media_base).as_posix() if brief_cover else image,
            "source": GENERATED_SOURCE,
            "source_combined": True,
            "source_parts": len(chunks),
            "songs": chunk,
        })
    return playlists


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 Bilibili 收藏夹到 Mikuxperia 播放列表")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="只读取收藏夹，不下载文件或修改 music.json")
    parser.add_argument("--check-cookie", action="store_true", help="只检查 Cookie 文件格式与登录状态")
    parser.add_argument("--check-paths", action="store_true", help="只检查媒体目录与 music.json 的写入权限")
    parser.add_argument("--probe", metavar="BVID", help="列出指定视频的可用格式，用于诊断下载失败")
    args = parser.parse_args()
    try:
        settings = read_settings(args.config)
        folder_ids = settings["folders"]
        cookie_file = settings["cookie_file"]
        media_root = settings["media_root"]
        catalog_path = settings["catalog"]
        page_size = settings["page_size"]
        maximum = settings["maximum"]
        songs_per_playlist = settings["songs_per_playlist"]
        redownload = settings["redownload"]
        if args.probe:
            if not cookie_file.is_file():
                raise RuntimeError(f"cookie file not found: {cookie_file}")
            return probe_formats(args.probe.strip(), cookie_file)
        if args.check_paths:
            print(f"运行用户: {current_user()}")
            ensure_writable_file(catalog_path, "播放列表文件")
            print(f"播放列表可写: {catalog_path}")
            ensure_writable_dir(media_root, "媒体缓存目录")
            print(f"媒体目录可写: {media_root}（{describe_owner(media_root)}）")
            for folder_id in folder_ids:
                folder_dir = media_root / str(folder_id)
                ensure_writable_dir(folder_dir, "收藏夹缓存目录")
                print(f"收藏夹目录可写: {folder_dir}（{describe_owner(folder_dir)}）")
            return 0
        if not cookie_file.is_file():
            raise RuntimeError(f"cookie file not found: {cookie_file}")
        summary = bilicookies.describe(cookie_file)
        if summary["count"] == 0:
            raise RuntimeError(
                f"cookie file has no usable cookies: {cookie_file}. "
                "支持 Netscape 格式（7 个制表符字段）或 name=value 请求头格式"
            )
        print(f"cookie: {summary['count']} 个字段，格式 {summary['format']}，字段 {', '.join(summary['names'])}")
        if not summary["has_sessdata"]:
            raise RuntimeError("cookie 缺少 SESSDATA，无法访问收藏夹接口")
        if summary["missing_optional"]:
            print(f"cookie 提示: 缺少可选字段 {', '.join(summary['missing_optional'])}，部分接口可能受限")
        if args.check_cookie:
            data = api_json("https://api.bilibili.com/x/web-interface/nav", cookie_file).get("data") or {}
            if data.get("isLogin"):
                print(f"登录状态正常: uname={data.get('uname', '')} mid={data.get('mid', '')}")
                return 0
            raise RuntimeError("Cookie 未通过登录校验，请重新导出 SESSDATA")
        catalog = load_catalog(catalog_path)
        if not args.dry_run:
            ensure_writable_file(catalog_path, "播放列表文件")
            ensure_writable_dir(media_root, "媒体缓存目录")
        generated = []
        all_skipped: list[str] = []
        for folder_id in folder_ids:
            videos, unavailable = favorite_videos(folder_id, cookie_file, page_size, maximum)
            note = f"，跳过 {unavailable} 个失效条目" if unavailable else ""
            print(f"folder {folder_id}: {len(videos)} video(s){note}")
            if not args.dry_run:
                playlists, skipped = sync_folder(
                    folder_id,
                    videos,
                    media_root,
                    cookie_file,
                    songs_per_playlist,
                    redownload,
                )
                generated.extend(playlists)
                all_skipped.extend(skipped)
                print(
                    f"folder {folder_id}: 生成 {len(playlists)} 个播放列表，"
                    f"成功 {sum(len(item['songs']) for item in playlists)} 首，下载失败 {len(skipped)} 个"
                )
        if not args.dry_run:
            if settings["combined_enabled"] and generated:
                combined_songs = collect_folder_songs(generated)
                folder_total = sum(len(item["songs"]) for item in generated)
                duplicated = folder_total - len(combined_songs)
                combined_playlists = build_combined_playlists(
                    folder_ids,
                    combined_songs,
                    media_root,
                    songs_per_playlist,
                    settings["combined_title"],
                )
                generated.extend(combined_playlists)
                print(
                    f"combined: 生成 {len(combined_playlists)} 个合集播放列表，"
                    f"共 {len(combined_songs)} 首（去重 {duplicated} 首重复视频）"
                )
            manual = [item for item in catalog["playlists"] if item.get("source") != GENERATED_SOURCE]
            catalog["playlists"] = manual + generated
            temporary = catalog_path.with_suffix(catalog_path.suffix + ".tmp")
            temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(catalog_path)
            total = sum(len(item["songs"]) for item in generated if not item.get("source_combined"))
            print(f"updated {catalog_path}: {len(catalog['playlists'])} playlist(s), {total} song(s)")
            if all_skipped:
                print(f"skipped {len(all_skipped)} video(s):")
                for line in all_skipped[:20]:
                    print(f"  - {line}")
                if len(all_skipped) > 20:
                    print(f"  ... 另有 {len(all_skipped) - 20} 个未列出")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
