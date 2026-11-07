import unittest
from collections.abc import AsyncGenerator
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendAudio,
    SendMessage,
    SendVideo,
)
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from bot.config import Config
from bot.handlers import create_router
from bot.ytdlp import DownloadedAudio, DownloadedVideo, Quality, VideoInfo, YtDlpError

USER = User(id=1, is_bot=False, first_name="Друг")
CHAT = Chat(id=1, type="private")
URL = "https://vm.tiktok.com/abc/"


class FakeSession(BaseSession):
    """Answers Bot API calls locally and records them."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = []
        self.next_id = 100

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage, EditMessageReplyMarkup)):
            return True
        self.next_id += 1
        return Message(message_id=self.next_id, date=0, chat=CHAT, text="ok").as_(bot)


class FakeYtDlp:
    def __init__(self) -> None:
        self.info = VideoInfo(
            title="Заголовок",
            description="Описание",
            duration=65,
            qualities=(Quality(1080, 90_000_000), Quality(720, 30_000_000), Quality(360, None)),
            audio_size=3_000_000,
        )
        self.download_error = None
        self.downloads = []

    async def probe(self, url):
        return self.info

    async def download(self, url, height, directory: Path):
        self.downloads.append((url, height))
        if self.download_error:
            raise self.download_error
        path = directory / "video.mp4"
        path.write_bytes(b"video")
        return DownloadedVideo(path, 576, 1024, 65)

    async def download_audio(self, url, directory: Path):
        self.downloads.append((url, "audio"))
        if self.download_error:
            raise self.download_error
        path = directory / "audio.m4a"
        path.write_bytes(b"audio")
        return DownloadedAudio(path, "Трек", "Автор", 65)


class FlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = FakeSession()
        self.bot = Bot("1:token", session=self.session)
        self.ytdlp = FakeYtDlp()
        self.dispatcher = Dispatcher()
        config = Config(bot_token="1:token", allowed_user_ids=frozenset({USER.id}))
        self.dispatcher.include_router(create_router(config, self.ytdlp))
        self.update_id = 0

    async def feed(self, **payload):
        self.update_id += 1
        update = Update(update_id=self.update_id, **payload)
        await self.dispatcher.feed_update(self.bot, update)

    async def send_link(self, user=USER):
        text = f"смотри {URL}"
        message = Message(
            message_id=10,
            date=0,
            chat=CHAT,
            from_user=user,
            text=text,
            entities=[MessageEntity(type="url", offset=7, length=len(URL))],
        )
        await self.feed(message=message)

    def calls(self, method_type):
        return [call for call in self.session.calls if isinstance(call, method_type)]

    def last_keyboard(self):
        for call in reversed(self.session.calls):
            markup = getattr(call, "reply_markup", None)
            if markup:
                return markup.inline_keyboard
        self.fail("no keyboard was sent")

    async def press(self, text_part):
        buttons = [button for row in self.last_keyboard() for button in row]
        button = next(button for button in buttons if text_part in button.text)
        keyboard_message = Message(message_id=101, date=0, chat=CHAT, text="menu")
        query = CallbackQuery(
            id="q",
            from_user=USER,
            chat_instance="c",
            message=keyboard_message,
            data=button.callback_data,
        )
        await self.feed(callback_query=query)

    async def test_full_flow_with_title_only_by_default(self):
        await self.send_link()
        labels = [button.text for row in self.last_keyboard() for button in row]
        # 1080p does not fit the 50 MB limit of the cloud Bot API, so it isn't offered.
        self.assertEqual(
            labels,
            ["✅ Заголовок", "⬜ Описание", "720p · ~30 МБ", "360p", "🎵 Только аудио · ~3 МБ"],
        )

        await self.press("720p")
        self.assertEqual(self.ytdlp.downloads, [(URL, 720)])
        video = self.calls(SendVideo)[0]
        self.assertEqual(video.caption, "Заголовок")
        self.assertEqual((video.width, video.height, video.duration), (576, 1024, 65))
        self.assertEqual(video.reply_parameters.message_id, 10)
        self.assertEqual(len(self.calls(DeleteMessage)), 1)

    async def test_toggles_change_caption(self):
        await self.send_link()
        await self.press("Заголовок")
        self.assertIn("⬜ Заголовок", self.calls(EditMessageReplyMarkup)[-1].reply_markup.inline_keyboard[0][0].text)
        await self.press("Описание")
        await self.press("360p")
        self.assertEqual(self.calls(SendVideo)[0].caption, "Описание")

    async def test_failed_download_offers_menu_again(self):
        await self.send_link()
        self.ytdlp.download_error = YtDlpError("Сломалось")
        await self.press("720p")
        self.assertEqual(self.calls(SendVideo), [])
        last_edit = self.calls(EditMessageText)[-1]
        self.assertIn("Сломалось", last_edit.text)

        self.ytdlp.download_error = None
        await self.press("360p")
        self.assertEqual(len(self.calls(SendVideo)), 1)

    async def test_stranger_is_refused_and_told_their_id(self):
        await self.send_link(User(id=2, is_bot=False, first_name="Чужой"))
        self.assertEqual(self.ytdlp.downloads, [])
        self.assertIn("2", self.calls(SendMessage)[0].text)
        self.assertEqual(self.calls(EditMessageText), [])

    async def test_audio_is_sent_with_metadata_and_chosen_caption(self):
        await self.send_link()
        await self.press("Только аудио")
        self.assertEqual(self.ytdlp.downloads, [(URL, "audio")])
        self.assertEqual(self.calls(SendVideo), [])
        audio = self.calls(SendAudio)[0]
        self.assertEqual(audio.caption, "Заголовок")
        self.assertEqual((audio.title, audio.performer, audio.duration), ("Трек", "Автор", 65))
        self.assertEqual(audio.audio.filename, "Заголовок.m4a")
        self.assertEqual(audio.reply_parameters.message_id, 10)
        self.assertEqual(len(self.calls(DeleteMessage)), 1)

    async def test_failed_audio_offers_menu_again(self):
        await self.send_link()
        self.ytdlp.download_error = YtDlpError("Сломалось")
        await self.press("Только аудио")
        self.assertEqual(self.calls(SendAudio), [])
        self.assertIn("Сломалось", self.calls(EditMessageText)[-1].text)
        self.assertTrue(self.last_keyboard())

    async def test_too_large_video_still_offers_audio(self):
        self.ytdlp.info = VideoInfo(
            "T", "", 60, (Quality(1080, 900_000_000),), audio_size=5_000_000
        )
        await self.send_link()
        self.assertIn("можно скачать аудио", self.calls(EditMessageText)[-1].text)
        labels = [button.text for row in self.last_keyboard() for button in row]
        self.assertEqual(labels, ["✅ Заголовок", "⬜ Описание", "🎵 Только аудио · ~5 МБ"])

    async def test_too_large_video_and_audio_explains_limit(self):
        self.ytdlp.info = VideoInfo(
            "T", "", 60, (Quality(1080, 900_000_000),), audio_size=80_000_000
        )
        await self.send_link()
        self.assertIn("слишком большое", self.calls(EditMessageText)[-1].text)


if __name__ == "__main__":
    unittest.main()
