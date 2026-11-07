import os
from dataclasses import dataclass

from dotenv import load_dotenv

CLOUD_UPLOAD_LIMIT = 50 * 1000 * 1000
LOCAL_UPLOAD_LIMIT = 2000 * 1000 * 1000


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    bot_api_url: str | None = None
    cookies_file: str | None = None
    proxy: str | None = None
    max_concurrent_downloads: int = 2
    auto_update: bool = True
    data_dir: str = "data"

    @property
    def upload_limit(self) -> int:
        return LOCAL_UPLOAD_LIMIT if self.bot_api_url else CLOUD_UPLOAD_LIMIT


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN: скопируйте .env.example в .env и заполните его")

    cookies_file = os.getenv("COOKIES_FILE", "").strip() or None
    if cookies_file and not os.path.isfile(cookies_file):
        raise SystemExit(f"Файл из COOKIES_FILE не найден: {cookies_file}")

    raw_ids = os.getenv("ALLOWED_USER_IDS", "").replace(" ", "")
    return Config(
        bot_token=token,
        allowed_user_ids=frozenset(int(item) for item in raw_ids.split(",") if item),
        bot_api_url=os.getenv("BOT_API_URL", "").strip().rstrip("/") or None,
        cookies_file=cookies_file,
        proxy=os.getenv("YTDLP_PROXY", "").strip() or None,
        max_concurrent_downloads=int(os.getenv("MAX_CONCURRENT_DOWNLOADS") or 2),
        auto_update=os.getenv("YTDLP_AUTO_UPDATE", "true").strip().lower() != "false",
        data_dir=os.getenv("DATA_DIR", "").strip() or "data",
    )
