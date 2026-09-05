import gzip
import json
import subprocess
import time
import unittest
import zlib
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree

import ainews
import bilicookies
import server
import sync_bilibili_favorites as bilisync
import tasks
import uploads
import webui


MINIMAL_CONFIG = """[server]
listen_host = 127.0.0.1
listen_port = 18090
public_base_url =
[qweather]
api_host = test.qweatherapi.com
api_key = test-key
bearer_token =
cache_seconds = 60
[weather]
city = 南海
latitude = 22.83
longitude = 113.02
[music]
media_root = {media}
catalog = {catalog}
"""


def write_minimal_config(root: Path) -> Path:
    """一份不含可选 [webui] 与 [ainews] 小节的配置。"""
    path = root / "minimal.conf"
    path.write_text(
        MINIMAL_CONFIG.format(media=server.MEDIA_ROOT.as_posix(), catalog=server.MUSIC_CONFIG.as_posix()),
        encoding="utf-8",
    )
    return path


def forecast_day(day: int) -> dict:
    return {
        "forecastStartTime": f"2026-09-{day:02d}T00:00+08:00",
        "temperatureMax": {"value": 30},
        "temperatureMin": {"value": 21},
        "uvIndexMax": 7,
        "daytime": {
            "condition": {"code": "305"},
            "wind": {"direction": {"degree": 90}, "speed": {"value": 4}},
            "precipitation": {"probability": 0.6},
        },
    }


class WeatherAdapterTest(unittest.TestCase):
    def test_decodes_gzip_qweather_response(self):
        expected = {"days": [forecast_day(1)]}
        compressed = gzip.compress(json.dumps(expected).encode("utf-8"))
        self.assertEqual(server.decode_json_response(compressed, "gzip"), expected)

    def test_detects_gzip_without_response_header(self):
        expected = {"code": "200"}
        compressed = gzip.compress(json.dumps(expected).encode("utf-8"))
        self.assertEqual(server.decode_json_response(compressed), expected)

    def test_decodes_deflate_qweather_response(self):
        expected = {"code": "200"}
        compressed = zlib.compress(json.dumps(expected).encode("utf-8"))
        self.assertEqual(server.decode_json_response(compressed, "deflate"), expected)

    @patch.object(server, "fetch_qweather")
    def test_converts_qweather_to_legacy_xml(self, fetch):
        fetch.return_value = {"days": [forecast_day(day) for day in range(1, 9)]}
        xml = server.CompatibilityHandler.weather_xml(
            None,
            {"APID": ["SC-04E_OTENKIMIKU"], "AREA": ["4410"]},
        )
        root = ElementTree.fromstring(xml)
        groups = root.findall("weatherdata")
        self.assertEqual(root.findtext("point"), f"{server.WEATHER_CITY}（和风天气）")
        self.assertEqual(len(groups), 8)
        self.assertEqual(groups[0].attrib["type"], "today")
        self.assertEqual(groups[-1].attrib["type"], "weekly6")
        self.assertEqual(groups[0].findtext("weather"), "300")
        self.assertEqual(groups[0].findtext("proba01"), "60")

    @patch.object(server, "fetch_qweather")
    def test_server_location_ignores_client_area(self, fetch):
        fetch.return_value = {"days": [forecast_day(day) for day in range(1, 9)]}
        xml = server.CompatibilityHandler.weather_xml(
            None,
            {"APID": ["SC-04E_OTENKIMIKU"], "AREA": ["9999"]},
        )
        self.assertEqual(ElementTree.fromstring(xml).findtext("point"), f"{server.WEATHER_CITY}（和风天气）")

    def test_music_config_references_available_media(self):
        config = server.load_music_config()
        song = config["playlists"][0]["songs"][0]
        self.assertGreater(server.media_size(song["music"]), 0)
        self.assertGreater(server.media_size(song["thumbnail"]), 0)
        self.assertEqual(server.media_size(""), 0)
        self.assertEqual(server.validate_music_config(), (1, 6))

    def test_empty_optional_media_has_no_file(self):
        self.assertIsNone(server.media_file(""))

    def test_loads_miku_conf_and_resolves_relative_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "custom.conf"
            config.write_text(
                """[server]
listen_host = 127.0.0.1
listen_port = 18080
public_base_url = http://example.test
[qweather]
api_host = test.qweatherapi.com
api_key = test-key
bearer_token =
cache_seconds = 60
[weather]
city = 广州
latitude = 23.13
longitude = 113.26
[music]
media_root = assets
catalog = catalog.json
""",
                encoding="utf-8",
            )
            server.configure(config)
            self.assertEqual(server.LISTEN_PORT, 18080)
            self.assertEqual(server.WEATHER_CITY, "广州")
            self.assertEqual(server.MEDIA_ROOT, root / "assets")
            server.configure(server.DEFAULT_CONFIG_PATH)


class BilibiliCookieTest(unittest.TestCase):
    def test_parses_netscape_format(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tTRUE\t1800000000\tSESSDATA\tabc%2Cdef\n"
            "#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t1800000000\tbili_jct\ttoken\n"
        )
        cookies = bilicookies.parse_cookie_text(text)
        self.assertEqual(cookies["SESSDATA"], "abc%2Cdef")
        self.assertEqual(cookies["bili_jct"], "token")

    def test_parses_request_header_format(self):
        cookies = bilicookies.parse_cookie_text("SESSDATA=abc%2Cdef; bili_jct=token; buvid3=xyz")
        self.assertEqual(cookies["SESSDATA"], "abc%2Cdef")
        self.assertEqual(cookies["buvid3"], "xyz")

    def test_parses_single_line_without_semicolon(self):
        cookies = bilicookies.parse_cookie_text("SESSDATA=0c9c%2A82CjC-value\n")
        self.assertEqual(cookies, {"SESSDATA": "0c9c%2A82CjC-value"})

    def test_parses_multiline_pairs(self):
        cookies = bilicookies.parse_cookie_text("SESSDATA=one\nbili_jct=two\n")
        self.assertEqual(cookies, {"SESSDATA": "one", "bili_jct": "two"})

    def test_ignores_comments_and_blank_lines(self):
        cookies = bilicookies.parse_cookie_text("# comment\n\nSESSDATA=value\n")
        self.assertEqual(cookies, {"SESSDATA": "value"})

    def test_cookie_header_round_trip(self):
        header = bilicookies.cookie_header({"SESSDATA": "a", "bili_jct": "b"})
        self.assertEqual(header, "SESSDATA=a; bili_jct=b")
        self.assertEqual(bilicookies.parse_cookie_text(header), {"SESSDATA": "a", "bili_jct": "b"})

    def test_describe_reports_format_and_missing_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            header_file = root / "header.txt"
            header_file.write_text("SESSDATA=value\n", encoding="utf-8")
            summary = bilicookies.describe(header_file)
            self.assertEqual(summary["format"], "header")
            self.assertTrue(summary["has_sessdata"])
            self.assertIn("bili_jct", summary["missing_optional"])

            netscape_file = root / "netscape.txt"
            netscape_file.write_text(
                "# Netscape HTTP Cookie File\n"
                ".bilibili.com\tTRUE\t/\tTRUE\t1800000000\tSESSDATA\tvalue\n",
                encoding="utf-8",
            )
            self.assertEqual(bilicookies.describe(netscape_file)["format"], "netscape")

    def test_describe_missing_file(self):
        summary = bilicookies.describe(Path("does-not-exist.txt"))
        self.assertEqual(summary["count"], 0)
        self.assertFalse(summary["has_sessdata"])
        self.assertEqual(summary["format"], "unknown")

    def test_converts_header_file_to_netscape_for_ytdlp(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "header.txt"
            source.write_text("SESSDATA=value; bili_jct=token\n", encoding="utf-8")
            path, temporary = bilicookies.netscape_file_for(source)
            try:
                self.assertIsNotNone(path)
                self.assertEqual(path, temporary)
                content = path.read_text(encoding="utf-8")
                self.assertTrue(content.startswith("# Netscape HTTP Cookie File"))
                self.assertEqual(bilicookies.parse_cookie_text(content)["SESSDATA"], "value")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def test_keeps_existing_netscape_file(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "netscape.txt"
            source.write_text(
                "# Netscape HTTP Cookie File\n"
                ".bilibili.com\tTRUE\t/\tTRUE\t1800000000\tSESSDATA\tvalue\n",
                encoding="utf-8",
            )
            path, temporary = bilicookies.netscape_file_for(source)
            self.assertEqual(path, source)
            self.assertIsNone(temporary)

    def test_no_cookies_yields_no_file(self):
        with TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.txt"
            empty.write_text("# only a comment\n", encoding="utf-8")
            self.assertEqual(bilicookies.netscape_file_for(empty), (None, None))


class TaskSchedulerTest(unittest.TestCase):
    def setUp(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def tearDown(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def test_registers_three_tasks(self):
        names = {item["name"] for item in tasks.status()}
        self.assertEqual(names, {"weather", "bilibili", "news", "cache_cleanup"})

    def test_interval_respects_minimum_and_maximum(self):
        ok, message = tasks.set_interval("weather", 10)
        self.assertFalse(ok)
        self.assertIn("不能小于", message)
        ok, _ = tasks.set_interval("weather", 31 * 24 * 3600)
        self.assertFalse(ok)
        ok, message = tasks.set_interval("weather", 900)
        self.assertTrue(ok)
        self.assertIn("15 分", message)
        weather = next(item for item in tasks.status() if item["name"] == "weather")
        self.assertEqual(weather["interval_seconds"], 900)

    def test_unknown_task_is_rejected(self):
        self.assertFalse(tasks.set_interval("nope", 600)[0])
        self.assertFalse(tasks.set_enabled("nope", True)[0])
        self.assertFalse(tasks.run_async("nope")[0])
        self.assertFalse(tasks.execute("nope")[0])

    def test_toggle_enabled(self):
        ok, message = tasks.set_enabled("bilibili", True)
        self.assertTrue(ok)
        self.assertIn("开启", message)
        self.assertTrue(next(item for item in tasks.status() if item["name"] == "bilibili")["enabled"])
        tasks.set_enabled("bilibili", False)
        self.assertFalse(next(item for item in tasks.status() if item["name"] == "bilibili")["enabled"])

    def test_execute_records_success_and_failure(self):
        calls = []
        tasks.register(tasks.Task("probe", "探针", lambda: calls.append(1) or "已完成", 600, 60))
        ok, message = tasks.execute("probe")
        self.assertTrue(ok)
        self.assertIn("已完成", message)
        probe = next(item for item in tasks.status() if item["name"] == "probe")
        self.assertTrue(probe["last_ok"])
        self.assertEqual(probe["run_count"], 1)
        self.assertGreater(probe["last_run"], 0)

        def boom() -> str:
            raise RuntimeError("接口失败")

        tasks.register(tasks.Task("boom", "故障", boom, 600, 60))
        ok, message = tasks.execute("boom")
        self.assertFalse(ok)
        self.assertIn("接口失败", message)
        self.assertFalse(next(item for item in tasks.status() if item["name"] == "boom")["last_ok"])

    def test_due_only_when_enabled_and_stale(self):
        task = tasks.Task("probe2", "探针", lambda: "ok", 600, 60)
        self.assertTrue(task.due(time.time()))
        task.last_run = time.time()
        self.assertFalse(task.due(time.time()))
        task.last_run = time.time() - 601
        self.assertTrue(task.due(time.time()))
        task.enabled = False
        self.assertFalse(task.due(time.time()))

    def test_state_survives_reconfigure(self):
        tasks.set_interval("news", 1800)
        tasks.set_enabled("news", False)
        server.configure(server.DEFAULT_CONFIG_PATH)
        news = next(item for item in tasks.status() if item["name"] == "news")
        self.assertEqual(news["interval_seconds"], 1800)
        self.assertFalse(news["enabled"])
        tasks.set_enabled("news", True)
        tasks.set_interval("news", 3600)

    def test_format_duration(self):
        self.assertEqual(tasks.format_duration(45), "45 秒")
        self.assertEqual(tasks.format_duration(900), "15 分")
        self.assertEqual(tasks.format_duration(5400), "1 小时 30 分")
        self.assertEqual(tasks.format_duration(90000), "1 天 1 小时")

    def test_interval_input_picks_largest_unit(self):
        self.assertEqual(webui.interval_input(86400), (1, "days"))
        self.assertEqual(webui.interval_input(7200), (2, "hours"))
        self.assertEqual(webui.interval_input(900), (15, "minutes"))
        self.assertEqual(webui.interval_input(45), (45, "seconds"))

    def test_weather_handler_clears_cache(self):
        payload = {"days": [forecast_day(day) for day in range(1, 9)]}
        with patch.object(server, "fetch_qweather", return_value=payload) as fetch:
            with server.WEATHER_CACHE_LOCK:
                server.WEATHER_CACHE["stale"] = (time.time(), {"days": []})
            message = server.refresh_weather_now()
        self.assertIn("8 天", message)
        fetch.assert_called_once()
        with server.WEATHER_CACHE_LOCK:
            self.assertNotIn("stale", server.WEATHER_CACHE)

    def test_bilibili_handler_requires_configuration(self):
        original = server.BILIBILI_CONFIGURED
        try:
            server.BILIBILI_CONFIGURED = False
            with self.assertRaises(RuntimeError) as context:
                server.sync_bilibili_now()
            self.assertIn("folder_ids", str(context.exception))
        finally:
            server.BILIBILI_CONFIGURED = original

    def test_bilibili_handler_reports_script_failure(self):
        original = server.BILIBILI_CONFIGURED
        try:
            server.BILIBILI_CONFIGURED = True
            failure = subprocess.CompletedProcess([], 1, "", "error: cookie file not found\n")
            with patch.object(server.subprocess, "run", return_value=failure):
                with self.assertRaises(RuntimeError) as context:
                    server.sync_bilibili_now()
            self.assertIn("cookie file not found", str(context.exception))
        finally:
            server.BILIBILI_CONFIGURED = original

    def test_bilibili_handler_summarises_output(self):
        original = server.BILIBILI_CONFIGURED
        try:
            server.BILIBILI_CONFIGURED = True
            done = subprocess.CompletedProcess([], 0, "folder 123: 4 video(s)\nupdated music.json: 2 playlist(s)\n", "")
            with patch.object(server.subprocess, "run", return_value=done):
                message = server.sync_bilibili_now()
            self.assertIn("folder 123: 4 video(s)", message)
            self.assertIn("updated", message)
        finally:
            server.BILIBILI_CONFIGURED = original


class UploadTest(unittest.TestCase):
    def tearDown(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    @staticmethod
    def build_body(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
        boundary = "----MikuTestBoundary"
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        for name, (filename, payload) in files.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
                + payload
                + b"\r\n"
            )
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(chunks), f"multipart/form-data; boundary={boundary}"

    def workspace(self, root: Path) -> tuple[Path, Path]:
        media = root / "media"
        media.mkdir()
        catalog = root / "music.json"
        catalog.write_text(
            json.dumps({"playlists": [{
                "id": 1, "version": 3, "title": "手动列表", "image": "cover.jpg", "songs": [],
            }]}, ensure_ascii=False),
            encoding="utf-8",
        )
        return media, catalog

    def test_parses_fields_and_files(self):
        body, content_type = self.build_body({"title": "曲名"}, {"audio": ("song.mp3", b"ID3data")})
        fields, files = uploads.parse_multipart(body, content_type)
        self.assertEqual(fields["title"], "曲名")
        self.assertEqual(files["audio"], ("song.mp3", b"ID3data"))

    def test_rejects_non_multipart(self):
        with self.assertRaises(uploads.UploadError):
            uploads.parse_multipart(b"a=1", "application/x-www-form-urlencoded")

    def test_stores_song_and_bumps_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media, catalog = self.workspace(root)
            message = uploads.store(
                {"title": "初音未来的歌", "artist": "作者", "date": "2026.09.01", "playlist_id": "1"},
                {
                    "audio": ("原曲名.mp3", b"0" * 1024),
                    "cover": ("cover.png", b"1" * 64),
                    "lyrics": ("lyric.txt", "歌词".encode("utf-8")),
                },
                media,
                catalog,
            )
            self.assertIn("初音未来的歌", message)
            data = json.loads(catalog.read_text(encoding="utf-8"))
            playlist = data["playlists"][0]
            self.assertEqual(playlist["version"], 4)
            song = playlist["songs"][0]
            self.assertTrue(song["music"].startswith("uploads/"))
            self.assertTrue(song["thumbnail"].startswith("uploads/"))
            self.assertTrue(song["lyrics"].startswith("uploads/"))
            self.assertEqual(song["title"], "初音未来的歌")
            self.assertTrue((media / song["music"]).is_file())
            self.assertTrue((media / song["lyrics"]).is_file())

    def test_creates_default_playlist_when_unspecified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media, catalog = self.workspace(root)
            uploads.store(
                {"title": "无列表歌曲"},
                {"audio": ("a.mp3", b"0" * 32)},
                media,
                catalog,
            )
            data = json.loads(catalog.read_text(encoding="utf-8"))
            ids = [item["id"] for item in data["playlists"]]
            self.assertIn(uploads.UPLOAD_PLAYLIST_ID, ids)

    def test_rejects_missing_audio_and_bad_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media, catalog = self.workspace(root)
            with self.assertRaises(uploads.UploadError):
                uploads.store({"title": "无音频"}, {}, media, catalog)
            with self.assertRaises(uploads.UploadError):
                uploads.store({"title": ""}, {"audio": ("a.mp3", b"0")}, media, catalog)
            with self.assertRaises(uploads.UploadError):
                uploads.store({"title": "曲", "date": "2026-09-01"}, {"audio": ("a.mp3", b"0")}, media, catalog)
            with self.assertRaises(uploads.UploadError):
                uploads.store({"title": "曲", "link": "ftp://x"}, {"audio": ("a.mp3", b"0")}, media, catalog)

    def test_rejects_bad_extensions_and_encoding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media, catalog = self.workspace(root)
            with self.assertRaises(uploads.UploadError):
                uploads.store({"title": "曲"}, {"audio": ("a.exe", b"0")}, media, catalog)
            with self.assertRaises(uploads.UploadError):
                uploads.store({"title": "曲"}, {"audio": ("a.mp3", b"0"), "cover": ("c.svg", b"0")}, media, catalog)
            with self.assertRaises(uploads.UploadError):
                uploads.store(
                    {"title": "曲"},
                    {"audio": ("a.mp3", b"0"), "lyrics": ("l.txt", b"\xff\xfe\x00bad")},
                    media,
                    catalog,
                )

    def test_rejects_bilibili_playlist_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            catalog = root / "music.json"
            catalog.write_text(
                json.dumps({"playlists": [{
                    "id": 1000123, "version": 1, "title": "收藏夹",
                    "source": "bilibili-favorites", "source_folder_id": 123, "songs": [],
                }]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(uploads.UploadError) as context:
                uploads.store({"title": "曲", "playlist_id": "1000123"}, {"audio": ("a.mp3", b"0")}, media, catalog)
            self.assertIn("Bilibili", str(context.exception))
            self.assertEqual(uploads.uploadable_playlists(json.loads(catalog.read_text(encoding="utf-8"))), [])

    def test_safe_stem_handles_non_ascii(self):
        self.assertTrue(uploads.safe_stem("初音ミク - 曲").startswith("song-"))
        self.assertEqual(uploads.safe_stem("Hello World!"), "Hello-World")
        self.assertEqual(uploads.safe_stem("miku_39 song.mp3"), "miku_39-song.mp3")
        self.assertTrue(uploads.safe_stem("").startswith("song-"))

    def test_unique_target_avoids_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.mp3").write_bytes(b"0")
            self.assertEqual(uploads.unique_target(root, "a", ".mp3").name, "a-2.mp3")


class MediaSubdirectoryTest(unittest.TestCase):
    def setUp(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def test_serves_files_from_subdirectories(self):
        subdir = server.MEDIA_ROOT / "uploads"
        subdir.mkdir(parents=True, exist_ok=True)
        sample = subdir / "unit-test-sample.mp3"
        sample.write_bytes(b"0" * 128)
        try:
            self.assertEqual(server.media_file("uploads/unit-test-sample.mp3"), sample.resolve())
            self.assertEqual(server.media_size("uploads/unit-test-sample.mp3"), 128)
        finally:
            sample.unlink(missing_ok=True)

    def test_rejects_path_traversal(self):
        with self.assertRaises(RuntimeError):
            server.media_file("../miku.conf")
        with self.assertRaises(RuntimeError):
            server.media_file("uploads/../../miku.conf")


class BilibiliSyncTest(unittest.TestCase):
    @staticmethod
    def media(bvid: str, **overrides) -> dict:
        entry = {"bvid": bvid, "title": "正常视频", "attr": 0, "type": 2, "cover": "https://example.test/c.jpg"}
        entry.update(overrides)
        return entry

    def test_accepts_normal_video(self):
        self.assertTrue(bilisync.playable(self.media("BV1xx411c7mD")))

    def test_rejects_invalidated_video(self):
        self.assertFalse(bilisync.playable(self.media("BV1r8411o77f", title="已失效视频", attr=9)))
        self.assertFalse(bilisync.playable(self.media("BV1Fs42137gv", title="已失效视频", attr=1)))

    def test_rejects_non_video_and_bad_bvid(self):
        self.assertFalse(bilisync.playable(self.media("BV1xx411c7mD", business="article")))
        self.assertFalse(bilisync.playable(self.media("av12345")))
        self.assertFalse(bilisync.playable(self.media("BV1xx411c7mD", type=12)))
        self.assertFalse(bilisync.playable(self.media("BV1xx411c7mD", attr="bad")))

    def test_favorite_videos_counts_skipped(self):
        payload = {"data": {"medias": [
            self.media("BV1aaaaaaaaa"),
            self.media("BV1bbbbbbbbb", title="已失效视频", attr=9),
            self.media("BV1ccccccccc"),
        ], "has_more": False}}
        with patch.object(bilisync, "api_json", return_value=payload):
            videos, skipped = bilisync.favorite_videos(1, Path("cookies.txt"), 20, 0)
        self.assertEqual([item["bvid"] for item in videos], ["BV1aaaaaaaaa", "BV1ccccccccc"])
        self.assertEqual(skipped, 1)

    def test_classify_failure_detects_dead_video(self):
        self.assertEqual(
            bilisync.classify_failure("ERROR: 1r8411o77f: An extractor error has occurred. (caused by KeyError('bvid'))"),
            "视频已失效或被删除",
        )
        self.assertEqual(bilisync.classify_failure("ERROR: Video unavailable"), "视频不可用")
        self.assertEqual(bilisync.classify_failure("ERROR: Private video"), "私密视频")
        self.assertEqual(bilisync.classify_failure("ERROR: something new"), "")

    def test_classify_failure_detects_rate_limit_and_format_issues(self):
        self.assertIn("风控", bilisync.classify_failure("ERROR: unable to download API page: HTTP Error 412"))
        self.assertIn("风控", bilisync.classify_failure("ERROR: response code -352"))
        self.assertIn("纯音频流", bilisync.classify_failure("ERROR: Requested format is not available"))
        self.assertIn("媒体流", bilisync.classify_failure("ERROR: No video formats found!"))
        self.assertIn("超时", bilisync.classify_failure("ERROR: The read operation timed out"))
        self.assertIn("Cookie", bilisync.classify_failure("ERROR: unable to download: HTTP Error 403: Forbidden"))

    def test_error_detail_prefers_error_line_over_progress(self):
        stdout = "[BiliBili] Extracting URL: https://example.test\n[BiliBili] 1eT41137WH: Downloading webpage\n"
        stderr = "ERROR: [BiliBili] 1eT41137WH: Requested format is not available\n"
        self.assertIn("Requested format", bilisync.error_detail(stderr, stdout))

    def test_error_detail_falls_back_when_no_error_line(self):
        stdout = "[BiliBili] 1eT41137WH: Downloading webpage\n"
        detail = bilisync.error_detail("", stdout)
        self.assertIn("Downloading webpage", detail)
        self.assertEqual(bilisync.error_detail("", ""), "yt-dlp failed without output")

    def test_ytdlp_command_includes_retry_and_timeout_flags(self):
        command, temporary = bilisync.ytdlp_command(Path("/tmp/BV1x"), "https://example.test", None, "bestaudio")
        self.assertIsNone(temporary)
        self.assertIn("--socket-timeout", command)
        self.assertIn("--retries", command)
        self.assertIn("--extractor-retries", command)
        self.assertIn("--sleep-requests", command)
        self.assertEqual(command[-1], "https://example.test")

    def test_unknown_failure_now_skips_instead_of_aborting(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            failure = subprocess.CalledProcessError(1, ["yt-dlp"], "[BiliBili] x: Downloading webpage", "")
            with patch.object(bilisync.subprocess, "run", side_effect=failure), \
                 patch.object(bilisync.bilicookies, "netscape_file_for", return_value=(None, None)):
                with self.assertRaises(bilisync.SkipVideo) as context:
                    bilisync.download_audio("https://example.test", folder, Path("cookies.txt"), "BV1aaaaaaaaa")
            self.assertIn("下载失败", str(context.exception))

    def test_missing_output_file_skips(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            done = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(bilisync.subprocess, "run", return_value=done), \
                 patch.object(bilisync.bilicookies, "netscape_file_for", return_value=(None, None)):
                with self.assertRaises(bilisync.SkipVideo) as context:
                    bilisync.download_audio("https://example.test", folder, Path("cookies.txt"), "BV1aaaaaaaaa")
            self.assertIn("未产生", str(context.exception))

    def test_permission_error_still_aborts(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            failure = subprocess.CalledProcessError(
                1, ["yt-dlp"], "", "ERROR: unable to open for writing: [Errno 13] Permission denied",
            )
            with patch.object(bilisync.subprocess, "run", side_effect=failure), \
                 patch.object(bilisync.bilicookies, "netscape_file_for", return_value=(None, None)):
                with self.assertRaises(RuntimeError) as context:
                    bilisync.download_audio("https://example.test", folder, Path("cookies.txt"), "BV1aaaaaaaaa")
            self.assertNotIsInstance(context.exception, bilisync.SkipVideo)
            self.assertIn("chown", str(context.exception))

    def test_download_audio_raises_skip_for_dead_video(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            failure = subprocess.CalledProcessError(
                1, ["yt-dlp"], "",
                "ERROR: 1r8411o77f: An extractor error has occurred. (caused by KeyError('bvid'))",
            )
            with patch.object(bilisync.subprocess, "run", side_effect=failure), \
                 patch.object(bilisync.bilicookies, "netscape_file_for", return_value=(None, None)):
                with self.assertRaises(bilisync.SkipVideo) as context:
                    bilisync.download_audio("https://example.test", folder, Path("cookies.txt"), "BV1r8411o77f")
            self.assertIn("失效", str(context.exception))

    def test_download_audio_raises_runtime_for_unknown_failure(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            failure = subprocess.CalledProcessError(1, ["yt-dlp"], "", "ERROR: disk quota exceeded")
            with patch.object(bilisync.subprocess, "run", side_effect=failure), \
                 patch.object(bilisync.bilicookies, "netscape_file_for", return_value=(None, None)):
                with self.assertRaises(bilisync.SkipVideo) as context:
                    bilisync.download_audio("https://example.test", folder, Path("cookies.txt"), "BV1aaaaaaaaa")
            self.assertIn("disk quota", str(context.exception))

    def test_sync_folder_continues_after_skip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media" / "bilibili"
            videos = [
                self.media("BV1good11111", title="可用歌曲"),
                self.media("BV1dead22222", title="已失效视频"),
                self.media("BV1good33333", title="另一首"),
            ]

            def fake_download(url: str, output_dir: Path, cookie_file: Path, bvid: str, redownload: bool = False) -> Path:
                if bvid == "BV1dead22222":
                    raise bilisync.SkipVideo("视频已失效或被删除")
                target = output_dir / f"{bvid}.m4a"
                target.write_bytes(b"0" * 16)
                return target

            def fake_cover(url: str, target: Path) -> None:
                target.write_bytes(b"1" * 8)

            with patch.object(bilisync, "download_audio", side_effect=fake_download), \
                 patch.object(bilisync, "download_cover", side_effect=fake_cover), \
                 patch.object(bilisync, "convert_to_mp3", side_effect=lambda path, redownload=False: path), \
                 patch.object(bilisync, "folder_info", return_value={"title": "Test Folder", "media_count": 2}):
                playlists, skipped = bilisync.sync_folder(123, videos, media_root, Path("cookies.txt"))
            playlist = playlists[0]
            self.assertEqual([song["id"] for song in playlist["songs"]], ["BV1good11111", "BV1good33333"])
            self.assertEqual(len(skipped), 1)
            self.assertIn("BV1dead22222", skipped[0])
            self.assertEqual(playlist["songs"][0]["music"], "bilibili/123/BV1good11111.m4a")
            self.assertTrue(playlist["image"].endswith(".jpg"))

    def test_sync_folder_tolerates_cover_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media" / "bilibili"

            def fake_download(url: str, output_dir: Path, cookie_file: Path, bvid: str, redownload: bool = False) -> Path:
                target = output_dir / f"{bvid}.m4a"
                target.write_bytes(b"0" * 16)
                return target

            with patch.object(bilisync, "download_audio", side_effect=fake_download), \
                 patch.object(bilisync, "download_cover", side_effect=RuntimeError("cover 404")), \
                 patch.object(bilisync, "convert_to_mp3", side_effect=lambda path, redownload=False: path), \
                 patch.object(bilisync, "folder_info", return_value={"title": "Test Folder", "media_count": 1}):
                playlists, skipped = bilisync.sync_folder(123, [self.media("BV1good11111")], media_root, Path("c.txt"))
            playlist = playlists[0]
            self.assertEqual(len(playlist["songs"]), 1)
            self.assertEqual(playlist["songs"][0]["thumbnail"], "")
            self.assertEqual(playlist["image"], "")
            self.assertEqual(skipped, [])


class PlaylistVersionTest(unittest.TestCase):
    def setUp(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def test_small_manual_versions_map_to_distinct_days(self):
        first = server.version_date(1)
        second = server.version_date(2)
        self.assertLess(first, second)
        self.assertEqual((second - first).days, 1)

    def test_unix_timestamp_does_not_overflow(self):
        # Bilibili 同步曾经在这里写入 int(time.time())，导致
        # timedelta(days=...) 抛 OverflowError，接口返回 HTTP 502。
        moment = server.version_date(1788275668)
        self.assertEqual(moment.year, 2020)
        later = server.version_date(1788275668 + 3600)
        self.assertLess(moment, later)

    def test_new_sync_version_is_newer_than_old_timestamp(self):
        old = server.version_date(1788275668)
        new = server.version_date(bilisync.playlist_version())
        self.assertLess(old, new)

    def test_invalid_versions_fall_back_safely(self):
        baseline = server.version_date(1)
        for value in (0, -5, "x", None, 1.5):
            self.assertGreaterEqual(server.version_date(value), baseline - timedelta(days=1))

    def test_sync_version_is_small_and_monotonic(self):
        version = bilisync.playlist_version()
        self.assertGreater(version, 0)
        self.assertLess(version, 10_000_000)
        with patch.object(bilisync.time, "time", return_value=1767225600 + 600):
            self.assertEqual(bilisync.playlist_version(), 10)
        with patch.object(bilisync.time, "time", return_value=1767225600 + 660):
            self.assertEqual(bilisync.playlist_version(), 11)

    def test_playlist_xml_renders_with_timestamp_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            (media / "song.mp3").write_bytes(b"0" * 32)
            catalog = root / "music.json"
            catalog.write_text(
                json.dumps({"playlists": [{
                    "id": 1000000001,
                    "version": 1788275668,
                    "title": "Bilibili 收藏夹",
                    "image": "",
                    "brief_image": "",
                    "source": "bilibili-favorites",
                    "source_folder_id": 1,
                    "songs": [{
                        "id": "BV1", "title": "曲", "artist": "作者", "date": "2026.01.01",
                        "music": "song.mp3", "thumbnail": "", "lyrics": "", "link": "",
                    }],
                }]}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = root / "playlist.conf"
            config.write_text(
                f"""[server]
listen_host = 127.0.0.1
listen_port = 18093
public_base_url = http://miku-api.example.test
[qweather]
api_host = test.qweatherapi.com
api_key = test-key
bearer_token =
cache_seconds = 60
[weather]
city = 南海
latitude = 22.83
longitude = 113.02
[music]
media_root = {media.as_posix()}
catalog = {catalog.as_posix()}
""",
                encoding="utf-8",
            )
            server.configure(config)
            xml = server.CompatibilityHandler.playlist_xml(None)
            tree = ElementTree.fromstring(xml)
            self.assertEqual(tree.findtext("playlist/id"), "1000000001")
            self.assertEqual(tree.findtext("playlist/item/musicFileName"), "1000000001_song.mp3")
            self.assertEqual(tree.findtext("playlist/item/musicFileSize"), "32")
            self.assertTrue(tree.findtext("playlist/pubDate"))


    def test_playlist_id_stays_inside_java_int_range(self):
        # 3275482587 + 1000000000 会溢出 Java 的 int，让客户端抛出
        # NumberFormatException: Invalid int: "4275482587"。
        for folder_id in (3275482587, 1, 123456789, 2147483647, 999999999):
            playlist_id = bilisync.playlist_id_for(folder_id)
            self.assertGreater(playlist_id, 0)
            self.assertLessEqual(playlist_id, bilisync.JAVA_INT_MAX)

    def test_playlist_id_is_stable_and_distinct(self):
        self.assertEqual(bilisync.playlist_id_for(3275482587), bilisync.playlist_id_for(3275482587))
        self.assertNotEqual(bilisync.playlist_id_for(1), bilisync.playlist_id_for(2))

    def test_validate_rejects_oversized_playlist_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            (media / "song.mp3").write_bytes(b"0" * 8)
            catalog = root / "music.json"
            catalog.write_text(
                json.dumps({"playlists": [{
                    "id": 4275482587, "version": 1, "title": "溢出",
                    "songs": [{"id": "BV1", "music": "song.mp3"}],
                }]}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = root / "oversize.conf"
            config.write_text(
                f"""[server]
listen_host = 127.0.0.1
listen_port = 18094
public_base_url =
[qweather]
api_host = h
api_key = k
bearer_token =
cache_seconds = 60
[weather]
city = X
latitude = 1
longitude = 1
[music]
media_root = {media.as_posix()}
catalog = {catalog.as_posix()}
""",
                encoding="utf-8",
            )
            server.configure(config)
            with self.assertRaises(RuntimeError) as context:
                server.validate_music_config()
            self.assertIn("32-bit", str(context.exception))

    def test_upload_playlist_id_fits_java_int(self):
        self.assertLessEqual(uploads.UPLOAD_PLAYLIST_ID, server.JAVA_INT_MAX)


class WebUiTest(unittest.TestCase):
    def setUp(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def tearDown(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def configure_webui(self, root: Path, password: str = "secret-pass") -> None:
        config = root / "webui.conf"
        config.write_text(
            f"""[server]
listen_host = 127.0.0.1
listen_port = 18081
public_base_url =
[qweather]
api_host = test.qweatherapi.com
api_key = test-key
bearer_token =
cache_seconds = 60
[weather]
city = 南海
latitude = 22.83
longitude = 113.02
[music]
media_root = {server.MEDIA_ROOT.as_posix()}
catalog = {server.MUSIC_CONFIG.as_posix()}
[webui]
enabled = true
password = {password}
session_hours = 2
stats_file = stats.json
[bilibili]
folder_ids =
    123456
cookie_file = cookies.txt
media_root = favorites
""",
            encoding="utf-8",
        )
        server.configure(config)

    def test_disabled_by_default(self):
        with TemporaryDirectory() as directory:
            server.configure(write_minimal_config(Path(directory)))
            self.assertFalse(webui.available())

    def test_session_lifecycle_and_password_check(self):
        with TemporaryDirectory() as directory:
            self.configure_webui(Path(directory))
            self.assertTrue(webui.available())
            self.assertTrue(webui.password_matches("secret-pass"))
            self.assertFalse(webui.password_matches("wrong-pass"))
            self.assertFalse(webui.password_matches(""))
            token = webui.create_session()
            self.assertTrue(webui.valid_session(token))
            self.assertEqual(webui.session_token(f"a=1; miku_admin={token}"), token)
            webui.drop_session(token)
            self.assertFalse(webui.valid_session(token))

    def test_counts_today_requests_and_ignores_admin(self):
        with TemporaryDirectory() as directory:
            self.configure_webui(Path(directory))
            webui.record_request("/resources/xml/MikuNews/list.xml", "10.0.0.1")
            webui.record_request("/media/01.mp3", "10.0.0.1")
            webui.record_request("/media/01.mp3", "10.0.0.2")
            webui.record_request("/admin", "10.0.0.3")
            webui.record_request("/healthz", "10.0.0.4")
            today = webui.stats_summary()["today"]
            self.assertEqual(today["requests"], 3)
            self.assertEqual(today["visitors"], 2)
            self.assertEqual(today["media"], 2)
            self.assertEqual(today["app"], 1)

    def test_bilibili_status_reports_missing_cookie(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure_webui(root)
            status = webui.bilibili_status([])
            self.assertTrue(status["configured"])
            self.assertFalse(status["cookie"]["exists"])
            self.assertFalse(status["cookie"]["has_sessdata"])
            self.assertEqual([item["folder_id"] for item in status["folders"]], [123456])
            self.assertFalse(status["folders"][0]["synced"])

    def test_bilibili_status_detects_cookie_and_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure_webui(root)
            (root / "cookies.txt").write_text(
                "# Netscape HTTP Cookie File\n"
                ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tvalue\n",
                encoding="utf-8",
            )
            folder = root / "favorites" / "123456"
            folder.mkdir(parents=True)
            (folder / "BV1.m4a").write_bytes(b"0" * 2048)
            (folder / "BV1.jpg").write_bytes(b"0" * 512)
            playlists = [{
                "id": 1000123456,
                "source": "bilibili-favorites",
                "source_folder_id": 123456,
                "title": "Bilibili 收藏夹 123456",
                "songs": [{"id": "BV1"}],
            }]
            status = webui.bilibili_status(playlists)
            self.assertTrue(status["cookie"]["exists"])
            self.assertTrue(status["cookie"]["has_sessdata"])
            self.assertEqual(status["cookie"]["format"], "netscape")
            folder_status = status["folders"][0]
            self.assertTrue(folder_status["synced"])
            self.assertEqual(folder_status["song_count"], 1)
            self.assertEqual(folder_status["audio_files"], 1)
            self.assertEqual(folder_status["cover_files"], 1)
            self.assertEqual(status["total_bytes"], 2560)

    def test_bilibili_status_accepts_header_cookie_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure_webui(root)
            (root / "cookies.txt").write_text("SESSDATA=value-only\n", encoding="utf-8")
            status = webui.bilibili_status([])
            self.assertTrue(status["cookie"]["has_sessdata"])
            self.assertEqual(status["cookie"]["format"], "header")
            self.assertEqual(status["cookie"]["cookie_count"], 1)
            self.assertIn("bili_jct", status["cookie"]["missing_optional"])

    def test_dashboard_and_login_pages_use_brand_colour(self):
        with TemporaryDirectory() as directory:
            self.configure_webui(Path(directory))
            login = webui.login_page("密码错误")
            self.assertIn("#39C5BB", login)
            self.assertIn("密码错误", login)
            page = webui.dashboard_page(server.admin_snapshot("已更新"))
            self.assertIn("#39C5BB", page)
            self.assertIn("今日访问量", page)
            self.assertIn("Bilibili 收藏夹缓存", page)
            self.assertIn("已更新", page)


class AiNewsTest(unittest.TestCase):
    def tearDown(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def configure_ainews(self, root: Path, with_summariser: bool = True) -> None:
        summariser = (
            "openai_base_url = https://api.example.test/v1\nopenai_api_key = sk-test\nopenai_model = test-model\n"
            if with_summariser
            else "openai_base_url =\nopenai_api_key =\nopenai_model =\n"
        )
        config = root / "ainews.conf"
        config.write_text(
            f"""[server]
listen_host = 127.0.0.1
listen_port = 18082
public_base_url = http://news.example.test
[qweather]
api_host = test.qweatherapi.com
api_key = test-key
bearer_token =
cache_seconds = 60
[weather]
city = 南海
latitude = 22.83
longitude = 113.02
[music]
media_root = {server.MEDIA_ROOT.as_posix()}
catalog = {server.MUSIC_CONFIG.as_posix()}
[ainews]
enabled = true
tavily_api_key = tvly-test
search_queries =
    初音ミク 新曲
refresh_seconds = 300
max_items = 3
cache_file = ainews-cache.json
{summariser}""",
            encoding="utf-8",
        )
        server.configure(config)

    def test_disabled_without_configuration(self):
        with TemporaryDirectory() as directory:
            server.configure(write_minimal_config(Path(directory)))
            self.assertFalse(ainews.configured())
            self.assertEqual(ainews.refresh(force=True), (0, "AI 新闻未配置或未启用"))

    def test_summarises_search_results_and_writes_cache(self):
        results = [
            {"title": "新曲公开", "url": "https://example.test/a", "content": "新歌信息", "published": "2026-09-01"},
            {"title": "活动情报", "url": "https://example.test/b", "content": "活动信息", "published": "2026-09-02"},
        ]
        summary = json.dumps([
            {"title": "初音未来新曲公开", "summary": "摘要一", "category": "song", "url": "https://example.test/a", "date": "2026-09-01"},
            {"title": "演唱会情报", "summary": "摘要二", "category": "event", "url": "https://example.test/b", "date": "2026-09-02"},
        ], ensure_ascii=False)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure_ainews(root)
            with patch.object(ainews, "tavily_search", return_value=results), \
                 patch.object(ainews, "post_json", return_value={"choices": [{"message": {"content": f"```json\n{summary}\n```"}}]}):
                count, error = ainews.refresh(force=True)
            self.assertEqual((count, error), (2, ""))
            items = ainews.items(auto_refresh=False)
            self.assertEqual(items[0]["title"], "初音未来新曲公开")
            self.assertEqual(items[0]["category"], "song")
            self.assertTrue((root / "ainews-cache.json").is_file())
            status = ainews.status()
            self.assertTrue(status["configured"])
            self.assertTrue(status["summariser"])
            self.assertEqual(status["item_count"], 2)

    def test_falls_back_to_raw_results_without_summariser(self):
        results = [{"title": "新曲", "url": "https://example.test/a", "content": "内容", "published": "2026-09-01"}]
        with TemporaryDirectory() as directory:
            self.configure_ainews(Path(directory), with_summariser=False)
            with patch.object(ainews, "tavily_search", return_value=results):
                count, error = ainews.refresh(force=True)
            self.assertEqual((count, error), (1, ""))
            self.assertEqual(ainews.items(auto_refresh=False)[0]["title"], "新曲")
            self.assertFalse(ainews.status()["summariser"])

    def test_reports_search_failure(self):
        with TemporaryDirectory() as directory:
            self.configure_ainews(Path(directory))
            with patch.object(ainews, "tavily_search", side_effect=RuntimeError("HTTP 401 from tavily")):
                count, error = ainews.refresh(force=True)
            self.assertEqual(count, 0)
            self.assertIn("401", error)
            self.assertIn("401", ainews.status()["last_error"])

    def test_rejects_invalid_summariser_output(self):
        with self.assertRaises(RuntimeError):
            ainews.extract_json_array("这里没有 JSON")

    def test_news_xml_uses_ai_items(self):
        with TemporaryDirectory() as directory:
            self.configure_ainews(Path(directory), with_summariser=False)
            with patch.object(ainews, "tavily_search", return_value=[
                {"title": "初音未来新曲", "url": "https://example.test/a", "content": "摘要", "published": "2026-09-01"},
            ]):
                ainews.refresh(force=True)
            xml = server.CompatibilityHandler.news_xml(None)
            root = ElementTree.fromstring(xml)
            self.assertEqual(root.findtext("item/title"), "初音未来新曲")
            self.assertEqual(root.findtext("item/link"), "https://example.test/a")


class HealthReportTest(unittest.TestCase):
    def setUp(self):
        server.configure(server.DEFAULT_CONFIG_PATH)

    def test_reports_playlist_counts_and_checks(self):
        report = server.health_report()
        self.assertIn(report["status"], {"ok", "warn", "error"})
        self.assertEqual(report["playlist_count"], 1)
        self.assertEqual(report["song_count"], 6)
        labels = [item["label"] for item in report["checks"]]
        self.assertIn("播放列表配置", labels)
        self.assertIn("媒体目录", labels)
        self.assertIn("AI 新闻", labels)

    def test_admin_snapshot_contains_all_sections(self):
        snapshot = server.admin_snapshot("测试消息")
        self.assertEqual(snapshot["message"], "测试消息")
        self.assertIn("today", snapshot["stats"])
        self.assertIn("checks", snapshot["health"])
        self.assertIn("configured", snapshot["news"])
        self.assertIn("folders", snapshot["bilibili"])


if __name__ == "__main__":
    unittest.main()
