import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

from bot.config import load_config
from bot.handlers import create_router
from bot.storage import Storage
from bot.ytdlp import YtDlp, update_forever


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()
    if not config.allowed_user_ids:
        logging.warning("ALLOWED_USER_IDS is empty: the bot will refuse everyone")

    session = None
    if config.bot_api_url:
        server = TelegramAPIServer.from_base(config.bot_api_url, is_local=True)
        session = AiohttpSession(api=server)
    bot = Bot(config.bot_token, session=session)

    data_dir = Path(config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(data_dir / "bot.sqlite3")

    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(config, YtDlp(config), storage))

    update_task = asyncio.create_task(update_forever()) if config.auto_update else None
    try:
        await dispatcher.start_polling(bot)
    finally:
        if update_task:
            update_task.cancel()
        storage.close()


if __name__ == "__main__":
    asyncio.run(main())
