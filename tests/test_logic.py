import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, Message, MessageEntity

from bot.config import Config
from bot.handlers import audio_filename, build_caption
from bot.links import extract_urls, is_supported
from bot.storage import Storage
from bot.ytdlp import (
    PROBE_CACHE_SIZE,
    PROBE_CACHE_TTL,
    Quality,
    YtDlp,
    YtDlpError,
    friendly_error,
    parse_audio_size,
    parse_info,
    parse_qualities,
)


class LinksTest(unittest.TestCase):
    def test_supported_hosts(self):
        for url in (
            "https://youtu.be/abc",
            "https://www.youtube.com/shorts/abc",
            "https://m.youtube.com/watch?v=abc",
            "https://vm.tiktok.com/abc/",
            "https://www.instagram.com/reel/abc/",
        ):
            self.assertTrue(is_supported(url), url)

    def test_rejected_urls(self):
        for url in (
            "https://nottiktok.com/abc",
            "https://tiktok.com.evil.example/abc",
            "file:///C:/Windows/win.ini",
            "http://localhost:8081/",
            "https://[broken",
        ):
            self.assertFalse(is_supported(url), url)

    def test_extracts_plain_and_hidden_links(self):
        text = "🔥 youtu.be/abc и вот тут"
        message = Message(
            message_id=1,
            date=0,
            chat=Chat(id=1, type="private"),
            text=text,
            entities=[
                # Offsets are UTF-16: the emoji takes two units.
                MessageEntity(type="url", offset=3, length=12),
                MessageEntity(
                    type="text_link", offset=16, length=3, url="https://www.tiktok.com/@a/video/1"
                ),
            ],
        )
        self.assertEqual(
            extract_urls(message), ["https://youtu.be/abc", "https://www.tiktok.com/@a/video/1"]
        )


class QualitiesTest(unittest.TestCase):
    def test_adds_audio_and_prefers_h264_for_estimate(self):
        info = {
            "duration": 10,
            "formats": [
                {"vcodec": "none", "acodec": "mp4a.40.2", "filesize": 1_000_000, "tbr": 128},
                {"vcodec": "none", "acodec": "opus", "filesize": 9_000_000, "tbr": 160},
                {"vcodec": "vp9", "acodec": "none", "width": 1920, "height": 1080,
                 "filesize": 50_000_000, "tbr": 4000},
                {"vcodec": "avc1.640028", "acodec": "none", "width": 1920, "height": 1080,
                 "filesize": 20_000_000, "tbr": 3000},
            ],
        }
        self.assertEqual(parse_qualities(info), (Quality(1080, 21_000_000),))

    def test_vertical_video_is_named_by_short_side(self):
        info = {"formats": [{"vcodec": "h264", "acodec": "aac", "width": 576, "height": 1024}]}
        self.assertEqual(parse_qualities(info), (Quality(576, None),))

    def test_estimates_from_bitrate_and_skips_drm(self):
        info = {
            "duration": 8,
            "formats": [
                {"vcodec": "h264", "acodec": "aac", "height": 720, "tbr": 1000},
                {"vcodec": "h264", "acodec": "aac", "height": 1080, "has_drm": True},
            ],
        }
        self.assertEqual(parse_qualities(info), (Quality(720, 1_000_000),))

    def test_audio_size_prefers_aac_stream(self):
        info = {
            "formats": [
                {"vcodec": "none", "acodec": "opus", "filesize": 9_000_000, "tbr": 160},
                {"vcodec": "none", "acodec": "mp4a.40.2", "filesize": 1_000_000, "tbr": 128},
            ],
        }
        self.assertEqual(parse_audio_size(info), 1_000_000)

    def test_audio_size_is_unknown_without_separate_stream(self):
        info = {"formats": [{"vcodec": "h264", "acodec": "aac", "height": 720, "filesize": 5}]}
        self.assertIsNone(parse_audio_size(info))

    def test_keeps_lowest_resolutions(self):
        heights = (2160, 1440, 1080, 720, 480, 360, 240, 144)
        info = {"formats": [{"vcodec": "h264", "acodec": "aac", "height": h} for h in heights]}
        self.assertEqual(tuple(q.height for q in parse_qualities(info)), heights)

    def test_playlist_uses_first_entry(self):
        info = {"_type": "playlist", "entries": [None, {"title": "Первое", "formats": []}]}
        self.assertEqual(parse_info(info).title, "Первое")

    def test_live_is_rejected(self):
        with self.assertRaises(YtDlpError):
            parse_info({"title": "Эфир", "live_status": "is_live"})

    def test_video_key_identifies_video_on_its_site(self):
        info = {"extractor_key": "Youtube", "id": "abc", "formats": []}
        self.assertEqual(parse_info(info).video_key, "Youtube:abc")
        self.assertIsNone(parse_info({"formats": []}).video_key)


class ProbeCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ytdlp = YtDlp(Config(bot_token="1:token", allowed_user_ids=frozenset()))
        self.run_mock = AsyncMock(return_value=json.dumps({"title": "T", "formats": []}))
        self.ytdlp._run = self.run_mock

    async def test_repeated_link_is_not_probed_again(self):
        first = await self.ytdlp.probe("https://youtu.be/a")
        self.assertIs(await self.ytdlp.probe("https://youtu.be/a"), first)
        self.assertEqual(self.run_mock.await_count, 1)

        await self.ytdlp.probe("https://youtu.be/b")
        self.assertEqual(self.run_mock.await_count, 2)

    async def test_entry_expires(self):
        with patch("bot.ytdlp.time.monotonic", return_value=1000):
            await self.ytdlp.probe("https://youtu.be/a")
        with patch("bot.ytdlp.time.monotonic", return_value=1000 + PROBE_CACHE_TTL):
            await self.ytdlp.probe("https://youtu.be/a")
        self.assertEqual(self.run_mock.await_count, 2)

    async def test_failure_is_not_cached(self):
        self.run_mock.side_effect = [YtDlpError("Сломалось"), json.dumps({"title": "T"})]
        with self.assertRaises(YtDlpError):
            await self.ytdlp.probe("https://youtu.be/a")
        self.assertEqual((await self.ytdlp.probe("https://youtu.be/a")).title, "T")

    async def test_oldest_entries_are_evicted(self):
        for index in range(PROBE_CACHE_SIZE + 1):
            await self.ytdlp.probe(f"https://youtu.be/{index}")
        self.assertEqual(len(self.ytdlp._probes), PROBE_CACHE_SIZE)
        self.assertNotIn("https://youtu.be/0", self.ytdlp._probes)


class StorageTest(unittest.TestCase):
    def test_data_survives_reopening(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "bot.sqlite3"
            storage = Storage(path)
            storage.save_setting(1, "title", False)
            storage.save_file_id("Youtube:a", "720", "file")
            storage.close()

            storage = Storage(path)
            self.addCleanup(storage.close)
            self.assertEqual(storage.load_settings(1), {"title": False})
            self.assertEqual(storage.get_file_id("Youtube:a", "720"), "file")

    def test_replaces_and_forgets(self):
        storage = Storage(":memory:")
        self.addCleanup(storage.close)
        self.assertEqual(storage.load_settings(1), {})
        storage.save_setting(1, "title", False)
        storage.save_setting(1, "title", True)
        self.assertEqual(storage.load_settings(1), {"title": True})

        storage.save_file_id("k", "720", "old")
        storage.save_file_id("k", "720", "new")
        storage.save_file_id("k", "audio", "track")
        self.assertEqual(storage.get_file_id("k", "720"), "new")
        storage.forget_file_id("k", "720")
        self.assertIsNone(storage.get_file_id("k", "720"))
        self.assertEqual(storage.get_file_id("k", "audio"), "track")


class CaptionTest(unittest.TestCase):
    def assert_quote(self, entities, text, quoted):
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        self.assertEqual(entity.type, "expandable_blockquote")
        self.assertEqual(entity.extract_from(text), quoted)

    def test_respects_choices(self):
        self.assertEqual(build_caption("T", "D", False, False), ({}, []))
        caption, extra = build_caption("T", "D", True, False)
        self.assertEqual((caption["caption"], caption["caption_entities"], extra), ("T", [], []))
        caption, extra = build_caption("T", "D", False, True)
        self.assertEqual((caption["caption"], extra), ("D", []))
        self.assert_quote(caption["caption_entities"], "D", "D")

    def test_only_description_is_quoted(self):
        caption, extra = build_caption("😀 Заголовок", "Описание 😀", True, True)
        self.assertEqual(caption["caption"], "😀 Заголовок\n\nОписание 😀")
        self.assert_quote(caption["caption_entities"], caption["caption"], "Описание 😀")
        self.assertEqual(extra, [])

    def test_drops_title_repeated_in_description(self):
        caption, _ = build_caption("Начало...", "Начало и конец", True, True)
        self.assertEqual(caption["caption"], "Начало и конец")

    def test_keeps_title_when_description_is_empty(self):
        caption, _ = build_caption("...", "", True, True)
        self.assertEqual(caption["caption"], "...")

    def test_caption_limit_includes_title(self):
        caption, extra = build_caption("T" * 10, "x" * 1012, True, True)
        self.assertEqual(len(caption["caption"]), 1024)
        self.assertEqual(extra, [])

        caption, extra = build_caption("T" * 10, "x" * 1013, True, True)
        self.assertEqual((caption["caption"], caption["caption_entities"]), ("T" * 10, []))
        self.assertEqual(len(extra), 1)

    def test_emoji_count_towards_caption_limit(self):
        caption, extra = build_caption("", "😀" * 600, False, True)
        self.assertEqual(caption, {})
        self.assertEqual("".join(part["text"] for part in extra), "😀" * 600)

    def test_long_description_is_split_into_quotes_without_loss(self):
        description = "x" * 4500
        caption, extra = build_caption("T", description, True, True)
        self.assertEqual(caption["caption"], "T")
        self.assertEqual("".join(part["text"] for part in extra), description)
        for part in extra:
            self.assertLessEqual(len(part["text"]), 2000)
            self.assert_quote(part["entities"], part["text"], part["text"])

    def test_very_long_title_still_fits_caption(self):
        caption, extra = build_caption("😀" * 2000, "", True, False)
        self.assertLessEqual(len(caption["caption"].encode("utf-16-le")) // 2, 1024)
        self.assertEqual(extra, [])


class AudioFilenameTest(unittest.TestCase):
    def test_replaces_forbidden_characters(self):
        self.assertEqual(audio_filename('AC/DC: "Back"?'), "AC_DC_ _Back__.m4a")

    def test_empty_title_gets_default_name(self):
        self.assertEqual(audio_filename(" . "), "audio.m4a")


class ErrorsTest(unittest.TestCase):
    def test_maps_login_errors(self):
        message = friendly_error("ERROR: [Instagram] abc: login required to view this")
        self.assertIn("без входа", message)

    def test_maps_playlist_and_profile_links(self):
        message = friendly_error("ERROR: No suitable extractor found for URL https://x")
        self.assertIn("конкретное видео", message)

    def test_maps_silent_video(self):
        message = friendly_error(
            "ERROR: Postprocessing: WARNING: unable to obtain file audio codec with ffprobe"
        )
        self.assertEqual(message, "В этом видео нет звука.")

    def test_unknown_error_keeps_details(self):
        self.assertIn("boom", friendly_error("WARNING: x\nERROR: boom"))


if __name__ == "__main__":
    unittest.main()
