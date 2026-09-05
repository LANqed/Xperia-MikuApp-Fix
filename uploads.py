"""管理面板的歌曲上传支持。

只用标准库解析 ``multipart/form-data``，校验上传的文件，把它们保存到媒体根目录
下的专用子目录，并把歌曲追加到 ``music.json`` 的某个播放列表中。
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path


AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".wav"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
LYRIC_SUFFIXES = {".txt", ".lrc"}
MAX_TOTAL_BYTES = 80 * 1024 * 1024
UPLOAD_SUBDIR = "uploads"
UPLOAD_PLAYLIST_ID = 900000001


class UploadError(RuntimeError):
    """上传被拒绝时抛出，附带面向用户的中文提示。"""


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """从 multipart 请求体中解析出（文本字段，文件字段）两个字典。"""
    if "multipart/form-data" not in content_type.lower():
        raise UploadError("请求不是 multipart/form-data 表单")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=HTTP).parsebytes(header + body)
    if not message.is_multipart():
        raise UploadError("表单内容为空或格式不正确")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            if payload:
                files[str(name)] = (str(filename), payload)
        else:
            fields[str(name)] = payload.decode("utf-8", errors="replace").strip()
    return fields, files


def safe_stem(value: str) -> str:
    """生成文件系统安全的 ASCII 文件名主干，全部字符都不可用时退回时间戳。"""
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", ascii_only).strip("-._")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:60]
    return cleaned or f"song-{int(time.time())}"


def check_suffix(filename: str, allowed: set[str], label: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in allowed:
        raise UploadError(f"{label}格式不支持：{suffix or '未知'}，允许 {', '.join(sorted(allowed))}")
    return suffix


def unique_target(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def load_catalog(path: Path) -> dict:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UploadError(f"无法读取 music.json：{error}") from error
    if not isinstance(catalog.get("playlists"), list):
        raise UploadError("music.json 缺少 playlists 数组")
    return catalog


def save_catalog(path: Path, catalog: dict) -> None:
    try:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise UploadError(f"无法写入 music.json：{error}") from error


def target_playlist(catalog: dict, requested: str) -> dict:
    """找到指定的播放列表，未指定时创建并返回默认的「手动上传」列表。"""
    if requested:
        try:
            wanted = int(requested)
        except ValueError as error:
            raise UploadError("播放列表 ID 必须是整数") from error
        for playlist in catalog["playlists"]:
            if int(playlist.get("id", 0)) == wanted:
                if playlist.get("source") == "bilibili-favorites":
                    raise UploadError("不能上传到由 Bilibili 同步生成的播放列表，请选择手动播放列表")
                return playlist
        raise UploadError(f"找不到播放列表 {wanted}")
    for playlist in catalog["playlists"]:
        if int(playlist.get("id", 0)) == UPLOAD_PLAYLIST_ID:
            return playlist
    playlist = {
        "id": UPLOAD_PLAYLIST_ID,
        "version": 1,
        "title": "手动上传",
        "description": "通过管理面板上传的歌曲",
        "brief_description": "手动上传",
        "image": "",
        "brief_image": "",
        "songs": [],
    }
    catalog["playlists"].append(playlist)
    return playlist


def uploadable_playlists(catalog: dict) -> list[dict]:
    return [
        {"id": int(item.get("id", 0)), "title": str(item.get("title", "")), "songs": len(item.get("songs", []))}
        for item in catalog.get("playlists", [])
        if item.get("source") != "bilibili-favorites"
    ]


def store(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes]],
    media_root: Path,
    catalog_path: Path,
) -> str:
    """校验并保存一首上传的歌曲，返回给用户看的结果说明。"""
    audio = files.get("audio")
    if audio is None:
        raise UploadError("请选择音频文件")
    total = sum(len(payload) for _, payload in files.values())
    if total > MAX_TOTAL_BYTES:
        raise UploadError(f"上传总大小 {total // (1024 * 1024)} MB 超过 {MAX_TOTAL_BYTES // (1024 * 1024)} MB 限制")

    title = fields.get("title", "").strip()
    if not title:
        raise UploadError("请填写歌曲标题")
    artist = fields.get("artist", "").strip() or "未知作者"
    date = fields.get("date", "").strip() or time.strftime("%Y.%m.%d")
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", date):
        raise UploadError("日期格式必须是 YYYY.MM.DD")
    link = fields.get("link", "").strip()
    if link and not link.startswith(("http://", "https://")):
        raise UploadError("链接必须以 http:// 或 https:// 开头")

    audio_suffix = check_suffix(audio[0], AUDIO_SUFFIXES, "音频")
    cover = files.get("cover")
    cover_suffix = check_suffix(cover[0], IMAGE_SUFFIXES, "封面") if cover else ""
    lyrics = files.get("lyrics")
    lyrics_suffix = check_suffix(lyrics[0], LYRIC_SUFFIXES, "歌词") if lyrics else ""
    if lyrics is not None:
        try:
            lyrics[1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise UploadError("歌词文件必须是 UTF-8 编码的纯文本") from error

    catalog = load_catalog(catalog_path)
    playlist = target_playlist(catalog, fields.get("playlist_id", "").strip())

    directory = media_root / UPLOAD_SUBDIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise UploadError(f"无法创建上传目录：{error}") from error

    stem = safe_stem(fields.get("song_id", "").strip() or Path(audio[0]).stem or title)
    written: list[Path] = []
    try:
        audio_path = unique_target(directory, stem, audio_suffix)
        audio_path.write_bytes(audio[1])
        written.append(audio_path)
        stem = audio_path.stem

        cover_path = None
        if cover is not None:
            cover_path = unique_target(directory, stem, cover_suffix)
            cover_path.write_bytes(cover[1])
            written.append(cover_path)

        lyrics_path = None
        if lyrics is not None:
            lyrics_path = unique_target(directory, stem, lyrics_suffix)
            lyrics_path.write_bytes(lyrics[1])
            written.append(lyrics_path)
    except OSError as error:
        for path in written:
            path.unlink(missing_ok=True)
        raise UploadError(f"写入文件失败：{error}") from error

    def relative(path: Path | None) -> str:
        return f"{UPLOAD_SUBDIR}/{path.name}" if path else ""

    song_id = stem
    existing = {str(item.get("id", "")) for item in playlist.get("songs", [])}
    while song_id in existing:
        song_id = f"{song_id}-{int(time.time())}"

    playlist.setdefault("songs", []).append({
        "id": song_id,
        "title": title[:160],
        "artist": artist[:80],
        "date": date,
        "music": relative(audio_path),
        "thumbnail": relative(cover_path) or str(playlist.get("image", "")),
        "lyrics": relative(lyrics_path),
        "link": link,
    })
    playlist["version"] = int(playlist.get("version", 1)) + 1
    if not playlist.get("image") and cover_path is not None:
        playlist["image"] = relative(cover_path)
        playlist["brief_image"] = relative(cover_path)

    try:
        save_catalog(catalog_path, catalog)
    except UploadError:
        for path in written:
            path.unlink(missing_ok=True)
        raise

    size_text = f"{len(audio[1]) / (1024 * 1024):.1f} MB"
    return (
        f"已上传《{title}》到播放列表 {playlist['id']}（{playlist.get('title', '')}），"
        f"音频 {size_text}，版本号更新为 {playlist['version']}"
    )
