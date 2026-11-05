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

def _title_from_url(url):
    """Extract clean movie title from TamilMV topic URL slug."""
    import re as _re
    # URL like: /forums/topic/200528-baththa-2026-tamil-web
    match = _re.search(r'/topic/\d+-([^/]+)', url)
    if not match:
        return None
    slug = match.group(1)
    # Remove trailing quality/source tags
    slug = _re.sub(r'-(true|web|hd|hq|blu|dvd|cam|pre|rip|dl|hevc|avc|x264|x265|esub|multi|tamil|telugu|hindi|eng|mal|kan).*$', '', slug, flags=_re.I)
    # Convert hyphens to spaces and title case
    title = slug.replace('-', ' ').strip().title()
    return title if len(title) > 2 else None


async def _fetch_movies():
    """
    Scrape TamilMV. Fetches full title from each topic page title tag.
    10 concurrent requests per batch — fast enough for pagination.
    """
    import asyncio as _asyncio
    from bot.helper.ext_utils.tmv_scraper import _find_working_domain
    tmv_url = await _find_working_domain()
    if not tmv_url:
        return []
    scraper = _scraper()
    seen_urls = set()
    raw_urls = []

    try:
        resp = await _asyncio.to_thread(scraper.get, tmv_url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "topic" not in href:
                continue
            url = _fix_url(href, tmv_url)
            if url in seen_urls:
                continue
            text = a.get_text(strip=True)
            if not text or len(text) < 5 or text == "Languages":
                continue
            seen_urls.add(url)
            raw_urls.append(url)
    except Exception as e:
        LOGGER.error(f"DTMV: fetch_movies error: {e}")
        return []

    raw_urls = raw_urls[:100]

    async def fetch_title(url):
        try:
            r = await _asyncio.to_thread(scraper.get, url, timeout=15)
            s = BeautifulSoup(r.text, "html.parser")
            if s.title:
                full = s.title.get_text(strip=True)
                title = re.sub(r'\s*[-|]\s*1TamilMV.*$', '', full, flags=re.I).strip()
                return {"title": title, "url": url}
        except Exception:
            pass
        return None

    movies = []
    batch_size = 10
    for i in range(0, len(raw_urls), batch_size):
        batch = raw_urls[i:i+batch_size]
        results = await _asyncio.gather(*[fetch_title(u) for u in batch])
        for r in results:
            if r:
                movies.append(r)
        if len(movies) >= 100:
            break

    seen = set()
    unique = []
    for m in movies:
        if m["url"] not in seen:
            seen.add(m["url"])
            unique.append(m)

    return unique[:100]

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
                size_mb = 0
                for sib in a.find_all_next(string=True, limit=6):
                    m_ = re.search(r"(\d+(\.\d+)?)\s*(gb|mb)", str(sib), re.I)
                    if m_:
                        size_mb = float(m_.group(1)) * (1024 if m_.group(3).lower() == "gb" else 1)
                        break
                torrents.append({"name": text, "url": href, "size_mb": size_mb})

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


def _bdecode(data, i=0):
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    s = j + 1
    return data[s:s + n], s + n


def _torrent_size_mb(path):
    """Real total size (MB) from the .torrent file itself; 0 if unreadable."""
    try:
        with open(path, "rb") as f:
            meta, _ = _bdecode(f.read())
        info = meta[b"info"]
        if b"length" in info:
            total = info[b"length"]
        else:
            total = sum(x[b"length"] for x in info[b"files"])
        return total / 1048576
    except Exception:
        return 0


def _size_mode():
    v = str(config_dict.get("DTMV_SIZE_MODE") or "").strip().lower()
    if not v:
        try:
            import json
            with open("post_stickers.json") as f:
                v = str(json.load(f).get("dtmv_mode", "")).strip().lower()
        except Exception:
            v = ""
    return "high" if v == "high" else "low"  # no-off


def _size_ok(size_mb, auto_leech):
    """4GB High = only 4GB+, 4GB Low = only under 4GB, Off = old TMV Size Limit."""
    if not size_mb or size_mb <= 0:
        return True  # size unknown -> upload anyway
    mode = _size_mode()
    if mode == "high":
        return size_mb >= 4096
    if mode == "low":
        return size_mb < 4096
    try:
        lim = float(config_dict.get("TMV_SIZE_LIMIT", 0) or 0)
    except (TypeError, ValueError):
        lim = 0
    if auto_leech and lim and size_mb > lim * 1024:
        return False
    return True


def _mode_label():
    m = _size_mode()
    if m == "high":
        return "4GB High: only 4GB+"
    if m == "low":
        return "4GB Low: only under 4GB"
    return f"size limit {config_dict.get('TMV_SIZE_LIMIT') or 0}GB"


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
        skipped_big = 0

        for torrent in torrents:
            path = await _download_torrent(torrent["url"], torrent["name"])
            if not path:
                LOGGER.warning(f"DTMV: Skipping failed download: {torrent['name'][:50]}")
                continue
            _real = _torrent_size_mb(path)
            _mb = _real or torrent.get("size_mb") or 0
            LOGGER.info(
                f"DTMV: size {_mb:.0f}MB "
                f"({'torrent file' if _real else 'page' if torrent.get('size_mb') else 'unknown'}) "
                f"mode={_size_mode()}: {torrent['name'][:50]}"
            )
            if not _size_ok(_mb, auto_leech):
                skipped_big += 1
                if os.path.exists(path):
                    os.remove(path)
                continue
            try:
                if auto_leech:
                    try:
                        from bot.helper.mirror_utils.download_utils.qbit_download import add_qb_torrent
                        from bot.helper.listeners.tasks_listener import MirrorLeechListener
                        from bot import DOWNLOAD_DIR, user_data
                        from aiofiles.os import path as aiopath

                        uid = query.from_user.id
                        udict = user_data.get(uid, {})

                        # Get user's leech prefix
                        lprefix = udict.get("lprefix", config_dict.get("LEECH_FILENAME_PREFIX", ""))

                        # Get user's thumbnail
                        thumb_path = f"Thumbnails/{uid}.jpg"
                        thumb = thumb_path if await aiopath.exists(thumb_path) else None

                        # Clean filename — strip domain prefix, keep movie name
                        clean_name = re.sub(r'^(www\.\S+\s*[-–]\s*)', '', torrent["name"]).strip()
                        if lprefix:
                            clean_name = f"{lprefix} {clean_name}"

                        # Rename file to use prefix + clean name before sending
                        import os as _os
                        clean_file_name = clean_name if clean_name.lower().endswith(".torrent") else clean_name + ".torrent"
                        clean_file_name = re.sub(r'[\\/*?:"<>|]', "_", clean_file_name)
                        clean_path = f"/tmp/{clean_file_name[:100]}"
                        if _os.path.exists(path):
                            _os.rename(path, clean_path)
                            path = clean_path

                        # Send torrent to chat — gets unique message ID per torrent
                        sent_doc = await bot.send_document(
                            chat_id=chat_id,
                            document=path,
                            caption=f"<b>{clean_name}</b>",
                            force_document=True,
                        )
                        sent_count += 1
                        await asyncio.sleep(1)

                        sent_doc.from_user = query.from_user
                        listener = MirrorLeechListener(
                            message=sent_doc,
                            isLeech=True,
                            isQbit=True,
                            tag=query.from_user.mention,
                            source_url=torrent["url"],
                            leech_utils={"screenshots": 0, "thumb": None},
                        )
                        try:
                            from bot.helper.ext_utils.post_tracker import start_movie, tag_listener
                            tag_listener(listener, start_movie(movie["url"], movie["title"]), torrent["name"], 0)
                        except Exception as e:
                            LOGGER.error(f"PostTracker: tag failed: {e}")
                        dl_path = f"{DOWNLOAD_DIR}{listener.uid}/"
                        await add_qb_torrent(path, dl_path, listener, None, None)
                        LOGGER.info(f"DTMV: qBit leech triggered for: {clean_name[:50]}")
                        await asyncio.sleep(5)

                    except Exception as e:
                        LOGGER.error(f"DTMV: Leech trigger failed for '{torrent['name'][:50]}': {e}")
                else:
                    # Auto Leech OFF — clean filename + bold caption
                    import os as _os
                    from bot import user_data as _ud
                    _udict = _ud.get(query.from_user.id, {})
                    _lprefix = _udict.get("lprefix", config_dict.get("LEECH_FILENAME_PREFIX", ""))
                    _clean = re.sub(r'^(www\.\S+\s*[-–]\s*)', '', torrent["name"]).strip()
                    if _lprefix:
                        _clean = f"{_lprefix} {_clean}"
                    _fname = re.sub(r'[\\/*?:"<>|]', "_", _clean)
                    if not _fname.lower().endswith(".torrent"):
                        _fname += ".torrent"
                    _cpath = f"/tmp/{_fname[:100]}"
                    if _os.path.exists(path):
                        _os.rename(path, _cpath)
                        path = _cpath
                    await bot.send_document(
                        chat_id=chat_id,
                        document=path,
                        caption=f"<b>{_clean}</b>",
                        force_document=True,
                    )
                    sent_count += 1
                    await asyncio.sleep(1.5)
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

        if skipped_big:
            final_text += f"\n\n⏭ <i>Skipped {skipped_big} torrent(s) by the size rule ({_mode_label()}).</i>"
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
