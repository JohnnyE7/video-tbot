import asyncio
import logging
import re
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import Config
from bot.links import extract_urls, is_supported
from bot.ytdlp import BEST_AVAILABLE, Quality, VideoInfo, YtDlp, YtDlpError, format_size

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
# Telegram allows 4096 units, and an emoji takes two, so this always fits.
TEXT_CHUNK = 2000
UPLOAD_TIMEOUT = 60 * 60
MAX_PENDING_REQUESTS = 500

HELP_TEXT = (
    "Пришлите ссылку на видео из YouTube, Instagram или TikTok. "
    "Можно просто переслать сообщение с ссылкой."
)


class ToggleCallback(CallbackData, prefix="toggle"):
    request_id: str
    field: str


class QualityCallback(CallbackData, prefix="quality"):
    request_id: str
    height: int


class AudioCallback(CallbackData, prefix="audio"):
    request_id: str


@dataclass
class VideoRequest:
    url: str
    info: VideoInfo
    qualities: tuple[Quality, ...]
    source_message_id: int
    offer_audio: bool = True
    include_title: bool = True
    include_description: bool = False
    in_progress: bool = False


def build_text(title: str, description: str, include_title: bool, include_description: bool) -> str:
    title = title if include_title else ""
    description = description if include_description else ""
    # TikTok's title is the start of its description, so showing both would repeat it.
    if title and description.startswith(title.removesuffix("...")):
        title = ""
    return "\n\n".join(part for part in (title, description) if part)


def split_caption(text: str) -> tuple[str | None, list[str]]:
    """Returns the video caption and, if the text doesn't fit into one, follow-up messages."""
    if not text:
        return None, []
    if len(text.encode("utf-16-le")) // 2 <= CAPTION_LIMIT:
        return text, []
    return None, [text[start : start + TEXT_CHUNK] for start in range(0, len(text), TEXT_CHUNK)]


def quality_label(quality: Quality) -> str:
    name = "Лучшее" if quality.height == BEST_AVAILABLE else f"{quality.height}p"
    return f"{name} · ~{format_size(quality.size)}" if quality.size else name


def audio_label(size: int | None) -> str:
    return f"🎵 Только аудио · ~{format_size(size)}" if size else "🎵 Только аудио"


def audio_filename(title: str) -> str:
    """Names the saved file after the video, without characters Windows forbids."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:100]
    return f"{name or 'audio'}.m4a"


def build_keyboard(request_id: str, request: VideoRequest) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'✅' if request.include_title else '⬜'} Заголовок",
        callback_data=ToggleCallback(request_id=request_id, field="title"),
    )
    builder.button(
        text=f"{'✅' if request.include_description else '⬜'} Описание",
        callback_data=ToggleCallback(request_id=request_id, field="description"),
    )
    for quality in request.qualities:
        builder.button(
            text=quality_label(quality),
            callback_data=QualityCallback(request_id=request_id, height=quality.height),
        )
    builder.adjust(2)
    if request.offer_audio:
        builder.row(
            InlineKeyboardButton(
                text=audio_label(request.info.audio_size),
                callback_data=AudioCallback(request_id=request_id).pack(),
            )
        )
    return builder.as_markup()


def build_menu_text(info: VideoInfo) -> str:
    lines = [info.title[:300] or "Без названия"]
    if info.duration:
        minutes, seconds = divmod(info.duration, 60)
        hours, minutes = divmod(minutes, 60)
        lines.append(f"Длительность: {f'{hours}:' if hours else ''}{minutes:02}:{seconds:02}")
    lines.append("\nЧто добавить к видео и в каком качестве скачать?")
    return "\n".join(lines)


def create_router(config: Config, ytdlp: YtDlp) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private")

    requests: OrderedDict[str, VideoRequest] = OrderedDict()
    download_slots = asyncio.Semaphore(config.max_concurrent_downloads)

    def is_allowed(user_id: int) -> bool:
        return user_id in config.allowed_user_ids

    async def show_menu(status: Message, url: str, source_message_id: int) -> None:
        try:
            info = await ytdlp.probe(url)
        except YtDlpError as error:
            await status.edit_text(str(error))
            return

        limit = config.upload_limit
        qualities = info.qualities or (Quality(BEST_AVAILABLE, None),)
        fitting = tuple(q for q in qualities if q.size is None or q.size <= limit)
        offer_audio = info.audio_size is None or info.audio_size <= limit
        if not fitting and not offer_audio:
            smallest = min(q.size for q in qualities)
            await status.edit_text(
                f"Видео слишком большое: минимум {format_size(smallest)}, "
                f"а лимит Telegram {format_size(limit)}."
            )
            return

        request_id = uuid.uuid4().hex[:12]
        request = VideoRequest(url, info, fitting, source_message_id, offer_audio)
        requests[request_id] = request
        while len(requests) > MAX_PENDING_REQUESTS:
            requests.popitem(last=False)
        text = build_menu_text(info)
        if not fitting:
            text = f"Видео не помещается в лимит Telegram, но можно скачать аудио.\n\n{text}"
        await status.edit_text(text, reply_markup=build_keyboard(request_id, request))

    def caption_parts(request: VideoRequest) -> tuple[str | None, list[str]]:
        return split_caption(
            build_text(
                request.info.title,
                request.info.description,
                request.include_title,
                request.include_description,
            )
        )

    async def send_audio(status: Message, request: VideoRequest) -> None:
        caption, extra_messages = caption_parts(request)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            audio = await ytdlp.download_audio(request.url, Path(directory))
            await status.edit_text("Отправляю в Telegram…")
            sent = await status.bot.send_audio(
                chat_id=status.chat.id,
                audio=FSInputFile(audio.path, filename=audio_filename(request.info.title)),
                caption=caption,
                title=audio.title,
                performer=audio.performer,
                duration=audio.duration,
                reply_parameters=ReplyParameters(
                    message_id=request.source_message_id,
                    allow_sending_without_reply=True,
                ),
                request_timeout=UPLOAD_TIMEOUT,
            )
        for part in extra_messages:
            await sent.reply(part)

    async def send_video(status: Message, request: VideoRequest, height: int) -> None:
        caption, extra_messages = caption_parts(request)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            video = await ytdlp.download(request.url, height, Path(directory))
            await status.edit_text("Отправляю в Telegram…")
            sent = await status.bot.send_video(
                chat_id=status.chat.id,
                video=FSInputFile(video.path),
                caption=caption,
                width=video.width,
                height=video.height,
                duration=video.duration,
                supports_streaming=True,
                reply_parameters=ReplyParameters(
                    message_id=request.source_message_id,
                    allow_sending_without_reply=True,
                ),
                request_timeout=UPLOAD_TIMEOUT,
            )
        for part in extra_messages:
            await sent.reply(part)

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        if is_allowed(message.from_user.id):
            await message.answer(HELP_TEXT)
        else:
            await message.answer(
                f"Доступа пока нет. Ваш ID: {message.from_user.id}\n"
                "Владелец бота может добавить его в ALLOWED_USER_IDS."
            )

    @router.message()
    async def handle_message(message: Message) -> None:
        if not is_allowed(message.from_user.id):
            await message.answer(f"Доступа нет. Ваш ID: {message.from_user.id}")
            return

        urls = extract_urls(message)
        supported = [url for url in urls if is_supported(url)]
        if not supported:
            await message.answer(
                "Такие ссылки я не поддерживаю.\n" + HELP_TEXT if urls else HELP_TEXT
            )
            return

        statuses = [await message.reply("Получаю информацию о видео…") for _ in supported]
        await asyncio.gather(
            *(
                show_menu(status, url, message.message_id)
                for status, url in zip(statuses, supported)
            )
        )

    @router.callback_query(ToggleCallback.filter())
    async def handle_toggle(callback: CallbackQuery, callback_data: ToggleCallback) -> None:
        request = requests.get(callback_data.request_id)
        if request is None or not is_allowed(callback.from_user.id):
            await callback.answer("Запрос устарел, пришлите ссылку ещё раз.", show_alert=True)
            return
        if request.in_progress:
            await callback.answer()
            return

        if callback_data.field == "title":
            request.include_title = not request.include_title
        else:
            request.include_description = not request.include_description
        await callback.message.edit_reply_markup(
            reply_markup=build_keyboard(callback_data.request_id, request)
        )
        await callback.answer()

    @router.callback_query(QualityCallback.filter())
    async def handle_quality(callback: CallbackQuery, callback_data: QualityCallback) -> None:
        await deliver(
            callback,
            callback_data.request_id,
            "Скачиваю видео…",
            lambda status, request: send_video(status, request, callback_data.height),
        )

    @router.callback_query(AudioCallback.filter())
    async def handle_audio(callback: CallbackQuery, callback_data: AudioCallback) -> None:
        await deliver(callback, callback_data.request_id, "Скачиваю аудио…", send_audio)

    async def deliver(
        callback: CallbackQuery,
        request_id: str,
        progress_text: str,
        send: Callable[[Message, VideoRequest], Awaitable[None]],
    ) -> None:
        request = requests.get(request_id)
        if request is None or not is_allowed(callback.from_user.id):
            await callback.answer("Запрос устарел, пришлите ссылку ещё раз.", show_alert=True)
            return
        if request.in_progress:
            await callback.answer("Уже обрабатываю это видео.")
            return

        request.in_progress = True
        await callback.answer()
        status = callback.message
        try:
            await status.edit_text("В очереди…")
            async with download_slots:
                await status.edit_text(progress_text)
                await send(status, request)
        except (YtDlpError, TelegramAPIError) as error:
            logger.warning("Failed to deliver %s: %s", request.url, error)
            message = str(error) if isinstance(error, YtDlpError) else "Telegram не принял файл."
            await status.edit_text(
                f"{message}\n\n{build_menu_text(request.info)}",
                reply_markup=build_keyboard(request_id, request),
            )
        else:
            requests.pop(request_id, None)
            await status.delete()
        finally:
            request.in_progress = False

    return router
