"""
Telegram-бот, который следит за несколькими Tumblr-блогами и уведомляет
подписавшихся пользователей о новых постах.

Источник данных — нативный RSS каждого блога (https://blog.tumblr.com/rss),
без API-ключей и регистрации приложений. Опрос блогов выполняется
периодически через JobQueue из python-telegram-bot.

Настройка — см. .env.example и README.md.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import feedparser
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

RSS_URL_TEMPLATE = "https://{blog}/rss"
STATE_FILE = Path(os.environ.get("STATE_FILE", "bot_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tumblr_bot")


class Config:
    """Настройки бота, загружаются из переменных окружения (см. .env.example)."""

    def __init__(self) -> None:
        self.telegram_token = self._require("TELEGRAM_BOT_TOKEN")

        raw_blogs = os.environ.get("TUMBLR_BLOGS", "")
        self.blogs = [normalize_blog(b) for b in raw_blogs.split(",") if b.strip()]
        if not self.blogs:
            raise SystemExit(
                "TUMBLR_BLOGS пуст. Укажи хотя бы один блог через запятую, например:\n"
                "TUMBLR_BLOGS=staff,engineering"
            )

        self.poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
        self.posts_per_check = int(os.environ.get("POSTS_PER_CHECK", "20"))

    @staticmethod
    def _require(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise SystemExit(f"Не задана переменная окружения {name} (см. .env.example)")
        return value


def normalize_blog(name: str) -> str:
    """Приводит имя блога к виду identifier.tumblr.com — хосту, из которого
    строится адрес RSS-фида (https://{blog}/rss).

    Принимает как короткое имя ('staff'), так и полный адрес
    ('staff.tumblr.com' или 'https://staff.tumblr.com/').
    """
    name = name.strip()
    if name.startswith("http://") or name.startswith("https://"):
        name = name.split("//", 1)[1]
    name = name.rstrip("/")
    if "." not in name:
        name = f"{name}.tumblr.com"
    return name


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"subscribers": [], "last_seen": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Убирает теги и раскодирует HTML-сущности — RSS отдаёт summary как HTML."""
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def fetch_posts(client: httpx.AsyncClient, blog: str, limit: int) -> list[dict]:
    """Забирает последние посты блога через нативный Tumblr RSS — без ключей.

    Приводит записи фида к тому же виду словаря (id/post_url/summary/type),
    что раньше отдавал Tumblr API, — остальной код ниже это не замечает.
    """
    url = RSS_URL_TEMPLATE.format(blog=blog)
    resp = await client.get(url, timeout=15)
    resp.raise_for_status()
    # Парсинг маленького XML-документа занимает микросекунды, поэтому
    # гонять его в отдельном потоке смысла нет — можно прямо в корутине.
    parsed = feedparser.parse(resp.content)
    posts = []
    for entry in parsed.entries[:limit]:
        post_url = entry.get("link", "")
        raw_summary = entry.get("summary") or entry.get("title") or ""
        posts.append(
            {
                "id": entry.get("id") or post_url,
                "post_url": post_url,
                "summary": strip_html(raw_summary),
                "type": "post",
            }
        )
    return posts


def format_notification(blog: str, post: dict) -> str:
    post_type = post.get("type", "post")
    url = post.get("post_url", "")
    summary = (post.get("summary") or "").strip()
    if not summary:
        summary = f"[{post_type}]"
    elif len(summary) > 300:
        summary = summary[:300].rstrip() + "…"
    return f"📬 <b>{blog}</b>\n{summary}\n{url}"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data["state"]
    config: Config = context.bot_data["config"]
    chat_id = update.effective_chat.id

    if chat_id not in state["subscribers"]:
        state["subscribers"].append(chat_id)
        save_state(state)

    await update.message.reply_text(
        "Ура! Буду присылать посты!"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data["state"]
    chat_id = update.effective_chat.id
    if chat_id in state["subscribers"]:
        state["subscribers"].remove(chat_id)
        save_state(state)
    await update.message.reply_text("Ну блин...")


async def check_blogs(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data["state"]
    config: Config = context.bot_data["config"]

    if not state["subscribers"]:
        return

    async with httpx.AsyncClient() as client:
        for blog in config.blogs:
            try:
                posts = await fetch_posts(client, blog, config.posts_per_check)
            except httpx.HTTPStatusError as e:
                log.warning("RSS %s вернул ошибку: %s", blog, e)
                continue
            except httpx.HTTPError as e:
                log.warning("Не удалось связаться с RSS %s: %s", blog, e)
                continue

            if not posts:
                continue

            last_seen_id = state["last_seen"].get(blog)

            if last_seen_id is None:
                # первый запуск для этого блога — запоминаем текущее состояние,
                # чтобы не забросить подписчиков всей историей блога разом
                state["last_seen"][blog] = str(posts[0]["id"])
                save_state(state)
                continue

            new_posts = []
            for post in posts:
                if str(post["id"]) == str(last_seen_id):
                    break
                new_posts.append(post)

            if not new_posts:
                continue

            for post in reversed(new_posts):  # от старых к новым
                text = format_notification(blog, post)
                for chat_id in list(state["subscribers"]):
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        log.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, e)

            state["last_seen"][blog] = str(posts[0]["id"])
            save_state(state)


def main() -> None:
    config = Config()
    state = load_state()

    application = Application.builder().token(config.telegram_token).build()
    application.bot_data["config"] = config
    application.bot_data["state"] = state

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("stop", cmd_stop))

    application.job_queue.run_repeating(
        check_blogs, interval=config.poll_interval, first=10, name="check_blogs"
    )

    log.info("Бот запущен. Слежу за: %s", ", ".join(config.blogs))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
