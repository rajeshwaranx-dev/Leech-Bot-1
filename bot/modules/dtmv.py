#!/usr/bin/env python3
"""
/dtmv — Manual TamilMV movie browser and leech trigger.

Flow:
  1. User sends /dtmv in authorized chat
  2. Bot scrapes TamilMV main page, shows paginated movie list (10 per page)
  3. User taps a movie → bot fetches that topic page, downloads all torrent files
  4. Bot sends each torrent to the same chat
  5. If Auto Leech is ON → leeching starts automatically
     If Auto Leech is OFF → user needs to send /qbleech manually
"""

import os
import re
import asyncio
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from pyrogram import Client
from pyrogram.filters import command, create
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import Message, CallbackQuery
from bot import LOGGER, config_dict, bot
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import sendMessage, editMessage, deleteMessage

ITEMS_PER_PAGE = 10

# In-memory store per user: {user_id: {"movies": [...], "page": 0, "msg_id": int}}
_dtmv_sessions = {}


def _scraper():
    return cloudscraper.create_scraper()


def _fix_url(href, base):
    return href if href.startswith("http") else urljoin(base, href)


def _clean_title(raw):
    """Extract a clean movie title from TamilMV topic title."""
    title = re.sub(r'^\s*(www\.[^\-\s]+[\s\-]*)+', '', raw, flags=re.I)
    title = re.sub(r'^\s*(\S*TamilMV\S*[\s\-]*)+', '', title, flags=re.I)
    title = re.sub(r'\s+', ' ', title).strip()
    return title or raw


async def _fetch_movies():
    """
    Scrape TamilMV main page and return list of {title, url} dicts.
    Groups by topic — one entry per movie/show.
    """
    from bot.helper.ext_utils.tmv_scraper import _find_working_domain
    tmv_url = await _find_working_domain()
    if not tmv_url:
        return []
    scraper = _scraper()
    movies = []
    seen_urls = set()

    try:
        resp = await asyncio.to_thread(scraper.get, tmv_url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "topic" not in href:
                continue
            url = _fix_url(href, tmv_url)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = _clean_title(a.get_text(strip=True))
            if not title or len(title) < 5:
                continue
            movies.append({"title": title, "url": url})

    except Exception as e:
        LOGGER.error(f"DTMV: fetch_movies error: {e}")

    return movies[:100]  # cap at 100


async def _fetch_torrents(topic_url):
    """
    Fetch all torrent download links from a topic page.
    Returns list of {name, url} dicts.
    """
    scraper = _scraper()
    torrents = []
    tmv_url = config_dict.get("TMV_URL", "https://www.1tamilmv.land/")

    try:
        resp = await asyncio.to_thread(scraper.get, topic_url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        posts = soup.find_all("div", class_="cPost_contentWrap")

        for post in posts:
            for a in post.find_all("a", href=True):
                text = a.get_text(strip=True)
                if "torrent" not in text.lower():
                    continue
                href = _fix_url(a["href"], tmv_url)
                # Deduplicate
                if any(t["url"] == href for t in torrents):
                    continue
                torrents.append({"name": text, "url": href})

    except Exception as e:
        LOGGER.error(f"DTMV: fetch_torrents error: {e}")

    return torrents


async def _download_torrent(url, name):
    """Download a .torrent file to /tmp, return path or None."""
    scraper = _scraper()
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", name)
    if not safe_name.lower().endswith(".torrent"):
        safe_name += ".torrent"
    path = f"/tmp/dtmv_{safe_name[:100]}"
    try:
        resp = await asyncio.to_thread(scraper.get, url, stream=True, timeout=60)
        if resp.status_code == 200:
            with open(path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            if os.path.getsize(path) > 0:
                return path
    except Exception as e:
        LOGGER.error(f"DTMV: download error for '{name[:50]}': {e}")
    return None


def _build_movie_buttons(movies, page, user_id):
    """Build paginated inline keyboard for movie list."""
    buttons = ButtonMaker()
    start = page * ITEMS_PER_PAGE
    end = min(start + ITEMS_PER_PAGE, len(movies))

    for i, movie in enumerate(movies[start:end], start=start):
        # Truncate long titles for button label
        label = movie["title"][:55] + ("…" if len(movie["title"]) > 55 else "")
        buttons.ibutton(label, f"dtmv pick {user_id} {i}")

    # Navigation
    nav = []
    total_pages = (len(movies) - 1) // ITEMS_PER_PAGE + 1
    if page > 0:
        buttons.ibutton("◀️ Prev", f"dtmv page {user_id} {page - 1}", "footer")
    buttons.ibutton(f"📄 {page + 1}/{total_pages}", "dtmv noop", "footer")
    if end < len(movies):
        buttons.ibutton("Next ▶️", f"dtmv page {user_id} {page + 1}", "footer")
    buttons.ibutton("❌ Cancel", f"dtmv cancel {user_id}", "footer")

    return buttons.build_menu(1)


# --- Command handler ---

async def dtmv_command(client: Client, message: Message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    status_msg = await sendMessage(message, "🔍 Fetching latest movies from TamilMV...")
    movies = await _fetch_movies()

    if not movies:
        await editMessage(status_msg, "❌ Failed to fetch movies. TamilMV may be down or URL changed.")
        return

    _dtmv_sessions[user_id] = {
        "movies": movies,
        "page": 0,
        "chat_id": chat_id,
        "msg_id": status_msg.id,
    }

    text = (
        f"🎬 <b>Select a Movie from the list</b>\n"
        f"Total: <b>{len(movies)} movies</b>"
    )
    await editMessage(status_msg, text, _build_movie_buttons(movies, 0, user_id))


# --- Callback handler ---

async def dtmv_callback(client: Client, query: CallbackQuery):
    data = query.data.split()
    # data: ["dtmv", action, user_id, ...]
    if len(data) < 3:
        await query.answer()
        return

    action = data[1]
    target_user = int(data[2])
    caller = query.from_user.id

    # Only the user who triggered /dtmv can interact
    if caller != target_user:
        await query.answer("This isn't your session!", show_alert=True)
        return

    session = _dtmv_sessions.get(target_user)
    if not session:
        await query.answer("Session expired. Send /dtmv again.", show_alert=True)
        return

    if action == "noop":
        await query.answer()
        return

    if action == "cancel":
        await query.answer("Cancelled.")
        _dtmv_sessions.pop(target_user, None)
        await deleteMessage(query.message)
        return

    if action == "page":
        new_page = int(data[3])
        session["page"] = new_page
        movies = session["movies"]
        text = (
            f"🎬 <b>Select a Movie from the list</b>\n"
            f"Total: <b>{len(movies)} movies</b>"
        )
        await query.answer()
        await editMessage(query.message, text, _build_movie_buttons(movies, new_page, target_user))
        return

    if action == "pick":
        idx = int(data[3])
        movies = session["movies"]
        if idx >= len(movies):
            await query.answer("Invalid selection.", show_alert=True)
            return

        movie = movies[idx]
        await query.answer(f"Fetching: {movie['title'][:40]}...")
        await editMessage(
            query.message,
            f"⏳ <b>Fetching torrents for:</b>\n<code>{movie['title']}</code>",
        )

        torrents = await _fetch_torrents(movie["url"])
        if not torrents:
            await editMessage(
                query.message,
                f"❌ No torrent files found for:\n<code>{movie['title']}</code>\n\n"
                f"The topic may not have torrent links yet.",
            )
            _dtmv_sessions.pop(target_user, None)
            return

        chat_id = session["chat_id"]
        await editMessage(
            query.message,
            f"📥 <b>Found {len(torrents)} torrent(s)</b> for:\n<code>{movie['title']}</code>\n\n"
            f"Downloading and sending...",
        )

        sent_count = 0
        auto_leech = config_dict.get("AUTO_LEECH", False)

        for torrent in torrents:
            path = await _download_torrent(torrent["url"], torrent["name"])
            if not path:
                LOGGER.warning(f"DTMV: Skipping failed download: {torrent['name'][:50]}")
                continue
            try:
                if auto_leech:
                    # Auto Leech ON — trigger leech directly using torrent file path
                    try:
                        from bot.modules.mirror_leech import _mirror_leech
                        from bot.helper.telegram_helper.bot_commands import BotCommands
                        cmd = f"/{BotCommands.QbLeechCommand[0]} {path}"
                        # Use the original query message, patch text to include torrent path
                        query.message.text = cmd
                        query.message.from_user = query.from_user
                        query.message.reply_to_message = None
                        await _mirror_leech(bot, query.message, isQbit=True, isLeech=True)
                        sent_count += 1
                        LOGGER.info(f"DTMV: Leech triggered for: {torrent['name'][:50]}")
                    except Exception as e:
                        LOGGER.error(f"DTMV: Leech trigger failed: {e}")
                        # Fallback — send as document if leech fails
                        await bot.send_document(
                            chat_id=chat_id,
                            document=path,
                            caption=f"<b>{torrent['name']}</b>\n\n#DTMV #TamilMV",
                        )
                        sent_count += 1
                else:
                    # Auto Leech OFF — just send torrent file to chat
                    await bot.send_document(
                        chat_id=chat_id,
                        document=path,
                        caption=f"<b>{torrent['name']}</b>\n\n#DTMV #TamilMV",
                    )
                    sent_count += 1

                await asyncio.sleep(2)
            except Exception as e:
                LOGGER.error(f"DTMV: Send failed for '{torrent['name'][:50]}': {e}")
            finally:
                if os.path.exists(path) and auto_leech:
                    # Keep file for qBit to read — qBit will handle cleanup
                    pass
                elif os.path.exists(path):
                    os.remove(path)

        final_text = (
            f"✅ <b>Sent {sent_count}/{len(torrents)} torrent(s)</b> for:\n"
            f"<code>{movie['title']}</code>\n\n"
        )
        if auto_leech:
            final_text += "🤖 <i>Auto Leech is ON — leeching started automatically.</i>"
        else:
            final_text += "ℹ️ <i>Auto Leech is OFF — send /qbleech to start leeching.</i>"

        await editMessage(query.message, final_text)
        _dtmv_sessions.pop(target_user, None)


def add_dtmv_handlers(app: Client):
    app.add_handler(
        MessageHandler(
            dtmv_command,
            filters=command("dtmv") & CustomFilters.authorized & ~CustomFilters.blacklisted,
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            dtmv_callback,
            filters=create(lambda _, __, q: q.data.startswith("dtmv")),
        )
    )
    LOGGER.info("DTMV: Handlers registered.")
