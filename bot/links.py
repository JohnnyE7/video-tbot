from urllib.parse import urlsplit

from aiogram.types import Message

SUPPORTED_DOMAINS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com")


def is_supported(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    host = host.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_DOMAINS)


def extract_urls(message: Message) -> list[str]:
    """Returns the links of a message in order, including those hidden behind text."""
    text = message.text or message.caption or ""
    urls = []
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "text_link":
            url = entity.url
        elif entity.type == "url":
            url = entity.extract_from(text)
        else:
            continue
        # Telegram marks "youtu.be/abc" as a link even without a scheme.
        if "://" not in url:
            url = f"https://{url}"
        if url not in urls:
            urls.append(url)
    return urls
