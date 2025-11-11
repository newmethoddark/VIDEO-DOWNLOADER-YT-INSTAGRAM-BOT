#!/usr/bin/env python3
# savemedia_two_features.py
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from dotenv import load_dotenv
import yt_dlp
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
UPDATE_LINK = os.getenv("UPDATE_LINK", "")
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB", "1900"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None

if not BOT_TOKEN:
    raise SystemExit("Set BOT_TOKEN in .env")

bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

URL_RE = re.compile(r"(https?://[^\s]+)")

# temporary request store for callbacks
requests_store = {}  # uid -> {"url":..., "meta": {...}}

def extract_url(text: str):
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(1) if m else None

def safe_mkdir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def yt_opts(outtmpl: str, audio_only=False, cookies=None):
    opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 2,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        opts["keepvideo"] = False
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
    if cookies:
        opts["cookiefile"] = cookies
    return opts

def probe_info(url: str):
    """Get metadata only (no download)"""
    try:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info
    except Exception:
        return None

def download_with_yt(url: str, outdir: Path, audio_only=False):
    outtmpl = str(outdir / "%(title).120s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }

    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        # safer fallback for 403 errors
        opts["format"] = "bestvideo*[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = Path(ydl.prepare_filename(info))
        if audio_only:
            filepath = filepath.with_suffix(".mp3")
        if not filepath.exists():
            files = sorted(outdir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                filepath = files[0]
        return filepath, info

def hashtags_from_text(text: str):
    if not text:
        return []
    tags = re.findall(r"(#\w+)", text)
    seen = set(); out=[]
    for t in tags:
        low = t.lower()
        if low not in seen:
            seen.add(low); out.append(t)
    return out

def file_too_big(path: Path):
    try:
        size_mb = path.stat().st_size / (1024*1024)
        return size_mb > MAX_FILESIZE_MB
    except Exception:
        return True

# ----------------- Handlers -----------------

@bot.message_handler(commands=['start'])
def cmd_start(m):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🔗 Updates", url=UPDATE_LINK)) if UPDATE_LINK else None
    kb.add(InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"))
    kb.add(InlineKeyboardButton("हिन्दी 🇮🇳", callback_data="lang_hi"))
    bot.send_message(m.chat.id, "Welcome — send an Instagram or YouTube link to start.", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def cb_lang(call):
    code = call.data.split("_",1)[1]
    texts = {
        "en": "Language set to English. Send an Instagram or YouTube link.",
        "hi": "भाषा हिन्दी सेट हुई। अब Instagram या YouTube लिंक भेजें।"
    }
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, texts.get(code, texts["en"]))

@bot.message_handler(func=lambda m: extract_url(m.text) is not None, content_types=['text'])
def handle_links(m):
    url = extract_url(m.text)
    if not url:
        bot.send_message(m.chat.id, "Send a valid link.")
        return

    # Instagram
    if "instagram.com" in url or "instagr.am" in url:
        msg = bot.send_message(m.chat.id, "🔎 Fetching Instagram info...")
        info = probe_info(url)
        # get caption/description
        caption = ""
        if info:
            caption = info.get("description") or info.get("title") or ""
        hashtags = hashtags_from_text(caption)
        thumb = None
        if info:
            thumb = info.get("thumbnail")
        # build caption message
        out_text = ""
        if info:
            title = info.get("title") or ""
            out_text += f"*{title}*\n"
        if caption:
            snippet = caption.strip()
            if len(snippet) > 800:
                snippet = snippet[:800] + "..."
            out_text += f"\n{snippet}\n"
        if hashtags:
            out_text += "\n" + " ".join(hashtags)
        if not out_text:
            out_text = "Instagram post found."

        # make a uid and store
        uid = str(uuid.uuid4())[:8]
        requests_store[uid] = {"url": url, "type": "instagram"}

        # prepare button
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("▶️ Download Video", callback_data=f"download|{uid}|video"))

        try:
            if thumb:
                bot.send_photo(m.chat.id, thumb, caption=out_text, parse_mode="Markdown", reply_markup=kb)
            else:
                bot.send_message(m.chat.id, out_text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            # sometimes thumbnail url not sendable — fallback to text
            bot.send_message(m.chat.id, out_text, parse_mode="Markdown", reply_markup=kb)
        try: bot.delete_message(m.chat.id, msg.message_id)
        except: pass
        return

    # YouTube
    if "youtube.com" in url or "youtu.be" in url:
        msg = bot.send_message(m.chat.id, "🔎 Fetching YouTube info...")
        info = probe_info(url)
        title = info.get("title") if info else "YouTube Video"
        duration = info.get("duration") if info else None
        mins = f"{int(duration//60)}m{int(duration%60)}s" if duration else ""
        thumb = info.get("thumbnail") if info else None
        out_text = f"*{title}*\n{mins}\n\nChoose download type:"
        uid = str(uuid.uuid4())[:8]
        requests_store[uid] = {"url": url, "type": "youtube"}
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🎥 Download Video", callback_data=f"download|{uid}|video"))
        kb.add(InlineKeyboardButton("🎧 Download Audio", callback_data=f"download|{uid}|audio"))

        try:
            if thumb:
                bot.send_photo(m.chat.id, thumb, caption=out_text, parse_mode="Markdown", reply_markup=kb)
            else:
                bot.send_message(m.chat.id, out_text, parse_mode="Markdown", reply_markup=kb)
        except Exception:
            bot.send_message(m.chat.id, out_text, parse_mode="Markdown", reply_markup=kb)
        try: bot.delete_message(m.chat.id, msg.message_id)
        except: pass
        return

    # fallback
    bot.send_message(m.chat.id, "Unsupported link. Send Instagram or YouTube public link.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("download|"))
def cb_download(call):
    # format: download|<uid>|<video|audio>
    try:
        _, uid, mode = call.data.split("|", 2)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid request.")
        return

    req = requests_store.get(uid)
    if not req:
        bot.answer_callback_query(call.id, "Request expired. Send link again.")
        return

    url = req["url"]
    platform = req.get("type")
    bot.answer_callback_query(call.id, "Starting download...")

    notify = bot.send_message(call.message.chat.id, f"⏳ Downloading {mode} — this can take some time.")
    tmpdir = Path(tempfile.mkdtemp(prefix="savemedia_"))
    try:
        audio_only = (mode == "audio")
        filepath, info = download_with_yt(url, tmpdir, audio_only=audio_only)
        if file_too_big(filepath):
            bot.send_message(call.message.chat.id, f"⚠️ File too large ({int(filepath.stat().st_size/(1024*1024))} MB).")
            return
        title = info.get("title", "File")
        caption = title
        # Send according to file type
        ext = filepath.suffix.lower()
        if audio_only or ext in [".mp3", ".m4a", ".opus", ".ogg"]:
            bot.send_audio(call.message.chat.id, open(filepath, "rb"), caption=caption)
        else:
            # video
            bot.send_video(call.message.chat.id, open(filepath, "rb"), caption=caption)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Download failed: {e}")
    finally:
        try:
            bot.delete_message(call.message.chat.id, notify.message_id)
        except: pass
        # cleanup
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        # remove stored request
        try:
            del requests_store[uid]
        except KeyError:
            pass

@bot.message_handler(content_types=['photo','video','document'])
def handle_media_forward(m):
    # simple save: re-send back
    if m.video:
        bot.send_video(m.chat.id, m.video.file_id, caption="Saved video")
    elif m.photo:
        bot.send_photo(m.chat.id, m.photo[-1].file_id, caption="Saved photo")
    elif m.document:
        bot.send_document(m.chat.id, m.document.file_id, caption="Saved document")

@bot.message_handler(func=lambda m: True, content_types=['text'])
def fallback_text(m):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🌐 Select Language", callback_data="select_lang"),
        InlineKeyboardButton("🔁 Update Bot", url="https://t.me/+ee1g-ybcMas5ODdl")  # change link
    )

    bot.send_message(
        m.chat.id,
        (
            "📩 *Welcome to SaveMedia Bot!*\n\n"
            "Send me any of the following links:\n"
            "▶️ YouTube video → I’ll give you *Video or Audio* download options.\n"
            "🎞 Instagram Reel → I’ll fetch *video, caption, and hashtags*.\n\n"
            "💡 Example Links:\n"
            "https://www.youtube.com/watch?v=abcd1234\n"
            "https://www.instagram.com/reel/xyz987/\n\n"
            "👇 Choose your language or check for updates below."
        ),
        parse_mode="Markdown",
        reply_markup=markup
    )

if __name__ == "__main__":
    print("Bot started...")
    bot.infinity_polling()
