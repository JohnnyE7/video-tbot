import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from bot.config import Config

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 60 * 60
UPDATE_TIMEOUT = 5 * 60

# 0 means "no explicit resolution": the site did not report any.
BEST_AVAILABLE = 0


class YtDlpError(Exception):
    """A failure whose message is safe and useful to show to the user."""


@dataclass(frozen=True)
class Quality:
    height: int
    size: int | None


@dataclass(frozen=True)
class VideoInfo:
    title: str
    description: str
    duration: int | None
    qualities: tuple[Quality, ...]


@dataclass(frozen=True)
class DownloadedVideo:
    path: Path
    width: int | None
    height: int | None
    duration: int | None


def _short_side(fmt: dict) -> int | None:
    """Vertical videos are named by width: 1080x1920 is 1080p, as in yt-dlp's own sorting."""
    width, height = fmt.get("width"), fmt.get("height")
    return min(width, height) if width and height else height


def _estimate_size(fmt: dict, duration: float | None) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)
    if fmt.get("tbr") and duration:
        return int(fmt["tbr"] * 125 * duration)  # tbr is in kbit/s
    return None


def _bitrate(fmt: dict) -> float:
    return fmt.get("tbr") or 0


def _is_h264(fmt: dict) -> bool:
    return (fmt.get("vcodec") or "").startswith(("avc", "h264"))


def parse_qualities(info: dict) -> tuple[Quality, ...]:
    """Lists the available resolutions, best first, with sizes where they can be estimated."""
    duration = info.get("duration")
    formats = [fmt for fmt in info.get("formats") or [] if not fmt.get("has_drm")]

    audio_only = [
        fmt for fmt in formats if fmt.get("vcodec") == "none" and fmt.get("acodec") != "none"
    ]
    # Mirrors the "acodec:aac" preference of the download selector.
    best_audio = max(
        audio_only,
        key=lambda fmt: ((fmt.get("acodec") or "").startswith("mp4a"), _bitrate(fmt)),
        default=None,
    )

    # Mirrors the "vcodec:h264" preference, so the estimate matches what gets downloaded.
    best_by_height: dict[int, dict] = {}
    for fmt in formats:
        height = _short_side(fmt)
        if fmt.get("vcodec") == "none" or not height:
            continue
        current = best_by_height.get(height)
        if current is None or (_is_h264(fmt), _bitrate(fmt)) > (_is_h264(current), _bitrate(current)):
            best_by_height[height] = fmt

    qualities = []
    for height in sorted(best_by_height, reverse=True):
        fmt = best_by_height[height]
        size = _estimate_size(fmt, duration)
        if size is not None and fmt.get("acodec") == "none" and best_audio:
            audio_size = _estimate_size(best_audio, duration)
            size = size + audio_size if audio_size else size
        qualities.append(Quality(height, size))
    return tuple(qualities)


def parse_info(info: dict) -> VideoInfo:
    # An Instagram carousel comes as a playlist; the bot works with its first video.
    if info.get("_type") == "playlist":
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise YtDlpError("По этой ссылке нет видео.")
        info = entries[0]

    if info.get("live_status") in ("is_live", "is_upcoming"):
        raise YtDlpError("Прямые эфиры скачать нельзя: дождитесь, когда появится запись.")

    duration = info.get("duration")
    return VideoInfo(
        title=(info.get("title") or "").strip(),
        description=(info.get("description") or "").strip(),
        duration=int(duration) if duration else None,
        qualities=parse_qualities(info),
    )


def friendly_error(stderr: str) -> str:
    errors = [line for line in stderr.splitlines() if line.startswith("ERROR:")]
    details = errors[-1].removeprefix("ERROR:").strip() if errors else ""
    lowered = details.lower()

    if "no suitable extractor" in lowered:
        return "Нужна ссылка на конкретное видео, а не на плейлист или профиль."
    if any(word in lowered for word in ("private", "login", "log in", "sign in", "cookies")):
        return "Видео недоступно без входа: оно закрытое или площадка требует cookies."
    if any(word in lowered for word in ("no video", "no formats", "requested format")):
        return "В этой публикации не нашлось видео: возможно, там только фото."
    if any(word in lowered for word in ("unavailable", "removed", "deleted", "not available")):
        return "Видео удалено или недоступно."
    return f"Не получилось обработать ссылку.\n{details[:300]}".strip()


def _child_env() -> dict[str, str]:
    # Without this, Windows consoles decode output in the legacy code page.
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class YtDlp:
    def __init__(self, config: Config) -> None:
        self._config = config

    def _base_args(self) -> list[str]:
        args = [
            sys.executable, "-m", "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--playlist-items", "1",
            "--no-warnings",
            "--no-progress",
            # Rejects anything outside these sites, including yt-dlp's generic extractor.
            "--use-extractors", "youtube,tiktok,vm.tiktok,instagram",
        ]
        if self._config.cookies_file:
            args += ["--cookies", self._config.cookies_file]
        if self._config.proxy:
            args += ["--proxy", self._config.proxy]
        return args

    async def _run(self, args: list[str], timeout: int) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise YtDlpError("Площадка отвечает слишком долго, попробуйте ещё раз.") from None

        error_text = stderr.decode("utf-8", "replace")
        if process.returncode != 0:
            logger.warning("yt-dlp failed: %s", error_text)
            raise YtDlpError(friendly_error(error_text))
        return stdout.decode("utf-8", "replace")

    async def probe(self, url: str) -> VideoInfo:
        output = await self._run(
            [*self._base_args(), "--dump-single-json", "--", url], PROBE_TIMEOUT
        )
        return parse_info(json.loads(output))

    async def download(self, url: str, height: int, directory: Path) -> DownloadedVideo:
        limit = self._config.upload_limit
        # H.264 + AAC in MP4 is what plays inline on every Telegram client.
        sort = "vcodec:h264,acodec:aac,ext:mp4:m4a"
        if height != BEST_AVAILABLE:
            sort = f"res:{height},{sort}"

        output = await self._run(
            [
                *self._base_args(),
                "--format", "bv*+ba/b",
                "--format-sort", sort,
                "--merge-output-format", "mp4",
                "--remux-video", "mp4",
                # Moves the index to the start of the file so playback starts immediately.
                "--postprocessor-args", "Merger+ffmpeg:-movflags +faststart",
                "--postprocessor-args", "VideoRemuxer+ffmpeg:-movflags +faststart",
                "--max-filesize", str(limit),
                "--paths", str(directory),
                "--output", "video.%(ext)s",
                "--print", "after_move:%(.{width,height,duration})j",
                "--",
                url,
            ],
            DOWNLOAD_TIMEOUT,
        )

        path = directory / "video.mp4"
        if not path.exists():
            # yt-dlp exits cleanly when --max-filesize skips the only format.
            raise YtDlpError("Видео в этом качестве не помещается в лимит Telegram.")
        if path.stat().st_size > limit:
            raise YtDlpError(
                f"Файл получился {format_size(path.stat().st_size)}, "
                f"а лимит Telegram {format_size(limit)}. Выберите качество пониже."
            )

        lines = output.strip().splitlines()
        metadata = json.loads(lines[-1]) if lines else {}
        duration = metadata.get("duration")
        return DownloadedVideo(
            path=path,
            width=metadata.get("width"),
            height=metadata.get("height"),
            duration=int(duration) if duration else None,
        )


def format_size(size: int) -> str:
    if size >= 1000 * 1000 * 1000:
        return f"{size / (1000 * 1000 * 1000):.1f} ГБ"
    return f"{max(size // (1000 * 1000), 1)} МБ"


async def update_forever() -> None:
    """Sites break yt-dlp regularly, so a stale copy eventually stops working."""
    while True:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "--pre",
            "yt-dlp[default,curl-cffi]",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), UPDATE_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("yt-dlp update timed out")
        else:
            if process.returncode == 0:
                logger.info("yt-dlp is up to date")
            else:
                logger.warning("yt-dlp update failed: %s", stderr.decode("utf-8", "replace"))
        await asyncio.sleep(24 * 60 * 60)
