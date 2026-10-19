"""
Telegram-бот: делает коллаж 9:16 из присланного видео.

Пользователь присылает видео → бот кладёт его в верхнюю половину кадра,
внизу крутится фиолетовое кольцо, посередине видео зависает и проигрывается
баннер (assets/banner.mp4, зелёный фон вырезается хромакеем), потом видео едет дальше.
"""

import asyncio
import logging
import os
import shutil
import tempfile

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from collage import CollageError, build_collage

log = logging.getLogger("collage-bot")

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str):
    """Читает .env рядом с bot.py: строки вида KEY=VALUE. Уже заданные переменные не трогает."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(HERE, ".env"))

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задан BOT_TOKEN: положи его в файл .env рядом с bot.py или в переменные окружения.")
CUT_RATIO = float(os.environ.get("CUT_RATIO", "0.5"))          # где зависает видео (0..1)
RING_PATH = os.environ.get("RING_PATH", os.path.join(HERE, "assets", "ring.mp4"))
BANNER_PATH = os.environ.get("BANNER_PATH", os.path.join(HERE, "assets", "banner.mp4"))
MAX_PARALLEL_JOBS = int(os.environ.get("MAX_PARALLEL_JOBS", "2"))

CANVAS_W = 1080
CANVAS_H = 1920                         # 9:16

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024   # лимит Bot API на скачивание
MAX_UPLOAD_BYTES = 50 * 1024 * 1024     # лимит Bot API на отправку

JOB_LIMIT = asyncio.Semaphore(MAX_PARALLEL_JOBS)


def _video_of(msg):
    if msg.video:
        return msg.video
    if msg.document and (msg.document.mime_type or "").startswith("video/"):
        return msg.document
    return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли видео, и я сделаю из него коллаж 9:16:\n"
        "сверху твоё видео, снизу кольцо, посередине видео зависнет и проиграется баннер."
    )


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    video = _video_of(msg)
    if video is None:
        return

    if video.file_size and video.file_size > MAX_DOWNLOAD_BYTES:
        await msg.reply_text("Файл больше 20 МБ, Bot API его не скачает. Пришли файл поменьше.")
        return

    await _process(msg, context, video.file_id)


async def _process(msg, context: ContextTypes.DEFAULT_TYPE, video_id: str):
    status = await msg.reply_text("Собираю коллаж, это займёт около минуты ⏳")
    tmp = tempfile.mkdtemp(prefix="collage_")
    video_path = os.path.join(tmp, "video.mp4")
    out_path = os.path.join(tmp, "collage.mp4")

    try:
        async with JOB_LIMIT:
            tg_video = await context.bot.get_file(video_id)
            await tg_video.download_to_drive(video_path)

            # ffmpeg тяжёлый — отдаём его в отдельный поток, чтобы бот не вис
            await asyncio.to_thread(
                build_collage,
                video_path, BANNER_PATH, RING_PATH, out_path,
                CANVAS_W, CANVAS_H, CUT_RATIO,
            )

            if os.path.getsize(out_path) > MAX_UPLOAD_BYTES:
                raise CollageError("получился файл больше 50 МБ, Bot API его не примет")

            await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_VIDEO)
            with open(out_path, "rb") as f:
                await msg.reply_video(
                    video=f,
                    supports_streaming=True,
                    caption="Готово ✨",
                )
    except CollageError as e:
        log.warning("collage error: %s", e)
        await msg.reply_text(f"Не вышло: {e}")
    except Exception:
        log.exception("collage job failed")
        await msg.reply_text("На сервере что-то сломалось. Попробуй ещё раз.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await status.delete()
        except Exception:
            pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    builder = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(300)
        .write_timeout(300)
    )
    # Если api.telegram.org недоступен напрямую — укажи прокси в .env: PROXY_URL=http://host:port
    # (или socks5://host:port, для socks нужен пакет httpx[socks])
    proxy = os.environ.get("PROXY_URL")
    if proxy:
        builder = builder.proxy(proxy).get_updates_proxy(proxy)
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
