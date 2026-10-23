#!/usr/bin/env python3
"""
TMV Scraper — integrated into leech bot.
Scrapes TamilMV for new torrents and triggers _mirror_leech() directly.
No user session needed — everything runs inside the leech bot process.
"""
import os
import re
import random
import asyncio
import requests
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import unquote, urljoin
from bot import LOGGER, config_dict
from bot.helper.ext_utils.db_handler import DbManger

# TamilMV permanent redirect domain — always redirects to current official domain
TMV_REDIRECT_URL = "https://www.1tamilmv.fi/"

# Track alert timing to avoid spamming owner
_last_alert_sent = 0


async def _resolve_via_redirect():
    """
    Follow 1tamilmv.fi redirect to get the current official domain.
    This is TamilMV's own official redirect — always points to current domain.
    Returns the resolved URL or None.
    """
    try:
        import requests
        resp = await asyncio.to_thread(
            requests.get, TMV_REDIRECT_URL, timeout=15, allow_redirects=True
        )
        if resp.status_code == 200 and "tamilmv" in resp.url.lower():
            # Normalize to base URL
            from urllib.parse import urlparse
            parsed = urlparse(resp.url)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"
            LOGGER.info(f"TMVScraper: Resolved via redirect: {base_url}")
            return base_url
    except Exception as e:
        LOGGER.error(f"TMVScraper: Redirect resolve failed: {e}")
    return None


async def _check_url(scraper, url):
    """Returns True if URL is reachable and looks like TamilMV."""
    try:
        resp = await asyncio.to_thread(scraper.get, url, timeout=15)
        return resp.status_code == 200 and ("topic" in resp.text or "tamilmv" in resp.text.lower())
    except Exception:
        return False


async def _find_working_domain():
    """
    1. Try current TMV_URL first (fastest path)
    2. If fails → follow 1tamilmv.fi redirect to get current domain
    3. If redirect also fails → alert owner once per hour
    Returns working URL or None.
    """
    global _last_alert_sent
    import time
    from bot import bot, OWNER_ID

    scraper = cloudscraper.create_scraper()
    current_url = config_dict.get("TMV_URL", "https://www.1tamilmv.solar/")

    # Step 1 — try current URL
    if await _check_url(scraper, current_url):
        return current_url

    LOGGER.warning(f"TMVScraper: Current URL failed: {current_url} — trying redirect...")

    # Step 2 — follow official redirect
    new_url = await _resolve_via_redirect()
    if new_url and new_url != current_url:
        if await _check_url(scraper, new_url):
            LOGGER.info(f"TMVScraper: Auto-updated URL: {current_url} → {new_url}")
            config_dict["TMV_URL"] = new_url
            try:
                from bot import DATABASE_URL
                if DATABASE_URL:
                    from bot.helper.ext_utils.db_handler import DbManger
                    await DbManger().update_config({"TMV_URL": new_url})
            except Exception as e:
                LOGGER.error(f"TMVScraper: DB update failed: {e}")
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"🔄 <b>TMV URL Auto-Updated</b>\n\n"
                    f"Old: <code>{current_url}</code>\n"
                    f"New: <code>{new_url}</code>\n\n"
                    f"<i>Resolved via 1tamilmv.fi redirect. Scraping continues.</i>",
                )
            except Exception:
                pass
            return new_url

    # Step 3 — everything failed, alert owner (max once per hour)
    now = time.time()
    if now - _last_alert_sent > 3600:
        _last_alert_sent = now
        try:
            await bot.send_message(
                OWNER_ID,
                f"⚠️ <b>TMV Scraper: URL unreachable!</b>\n\n"
                f"Current URL: <code>{current_url}</code>\n"
                f"Redirect (1tamilmv.fi) also failed.\n\n"
                f"Please update manually:\n"
                f"<b>/bsetting → 🎬 TMV Settings → TMV URL</b>\n\n"
                f"Official site: <code>www.1tamilmv.solar</code>",
            )
        except Exception:
            pass

    LOGGER.error("TMVScraper: All domain detection methods failed.")
    return None

LANGUAGE_INDICATORS = [
    "tamil", "telugu", "hindi", "malayalam", "kannada", "english",
    "bengali", "marathi", "punjabi", "urdu", "gujarati", "odia",
    "tam", "tel", "hin", "mal", "mlm", "kan", "eng", "ben",
]

# --- Language filter helpers ---

def is_multi_audio(title):
    t = title.lower()
    if "multi" in t or "multi audio" in t:
        return True
    found = [lang for lang in LANGUAGE_INDICATORS if re.search(rf'\b{re.escape(lang)}\b', t)]
    return len(found) >= 2

def passes_language(title):
    """Check if title matches TMV_LANGUAGE setting."""
    lang_filter = config_dict.get("TMV_LANGUAGE", "all").lower().strip()
    if lang_filter == "all":
        return True
    t = title.lower()
    filters = [f.strip() for f in lang_filter.split(",") if f.strip()]
    if "tamil+multi" in filters or "tamil_multi" in filters:
        if "tamil" in t or is_multi_audio(t):
            return True
        return False
    if "multi" in filters and is_multi_audio(t):
        return True
    for f in filters:
        if re.search(rf'\b{re.escape(f)}\b', t):
            return True
    return False

def passes_cam_predvd(title):
    """Returns True if CAM/PreDVD allowed or title doesn't contain them."""
    cam_on = config_dict.get("TMV_CAM_PREDVD", False)
    if cam_on:
        return True
    t = title.lower()
    cam_keywords = ["predvd", "pre-dvd", "camrip", "cam-rip", "hdcam", "hd-cam", "dvdscr", "scr"]
    return not any(kw in t for kw in cam_keywords)

def passes_size(size_mb):
    """Returns True if file size is within TMV_SIZE_LIMIT."""
    limit_gb = config_dict.get("TMV_SIZE_LIMIT", 0)
    if not limit_gb:
        return True
    return size_mb <= (limit_gb * 1024)

def should_process(title, size_mb=0):
    """Master filter — all conditions must pass."""
    if not config_dict.get("AUTO_TMV", False):
        return False
    if not passes_language(title):
        LOGGER.info(f"TMVScraper: Language filter skip: {title[:60]}")
        return False
    if not passes_cam_predvd(title):
        LOGGER.info(f"TMVScraper: CAM/PreDVD skip: {title[:60]}")
        return False
    if size_mb and not passes_size(size_mb):
        LOGGER.info(f"TMVScraper: Size limit skip ({size_mb:.0f}MB): {title[:60]}")
        return False
    return True

# --- File helpers ---

def clean_filename(name):
    name = unquote(name.strip())
    name = re.sub(r'^\s*(www\.[^\-\s]+[\s-]*)+', '', name, flags=re.I)
    name = re.sub(r'^\s*(\S*TamilMV\S*[\s-]*)+', '', name, flags=re.I)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    if not name.lower().endswith(".torrent"):
        name += ".torrent"
    return name

def fix_url(href, base_url):
    return href if href.startswith("http") else urljoin(base_url, href)

def categorize_content(title):
    t = title.lower()
    series_patterns = [r's\d{1,2}', r'ep\s?\d+', r'episode', r'season', r'complete']
    if any(re.search(p, t) for p in series_patterns) or "web series" in t:
        return "Series"
    if "dubbed" in t or "multi" in t:
        return "Dubbed"
    return "Movies"

def download_torrent(scraper, url, filename):
    try:
        response = scraper.get(url, stream=True, timeout=60)
        if response.status_code == 200:
            with open(filename, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return os.path.getsize(filename) > 0
        return False
    except Exception as e:
        LOGGER.error(f"TMVScraper: Download failed: {e}")
        return False

# --- DB helpers (uses leech bot's existing MongoDB connection) ---

async def is_tmv_processed(file_url):
    """Check if torrent already processed — uses leech bot's DB."""
    try:
        db = DbManger()
        result = await db.db.tamilmv.find_one({"file_url": file_url})
        return result is not None
    except Exception as e:
        LOGGER.error(f"TMVScraper: DB check failed: {e}")
        return False

async def mark_tmv_processed(file_name, file_url, size_mb, category):
    """Save processed torrent to DB."""
    import datetime
    try:
        db = DbManger()
        await db.db.tamilmv.insert_one({
            "file_name": file_name,
            "file_url": file_url,
            "size_mb": size_mb,
            "category": category,
            "upload_date": datetime.date.today().isoformat(),
        })
    except Exception as e:
        LOGGER.error(f"TMVScraper: DB save failed: {e}")

# --- Core scraper ---

async def process_torrent(client, file_path, title, file_url, size_mb, category):
    """
    Trigger leech directly via _mirror_leech — no Telegram message needed.
    Creates a fake message object that _mirror_leech can process.
    """
    from bot.modules.mirror_leech import _mirror_leech
    from bot.helper.telegram_helper.bot_commands import BotCommands
    from bot import bot, OWNER_ID

    try:
        # Build a minimal fake message that _mirror_leech can handle
        # We send the torrent file to the owner's PM first to get a real message object
        cmd = f"/{BotCommands.QbLeechCommand[0]}"

        # Send torrent file to owner PM silently — this gives us a real Pyrogram message
        sent_msg = await bot.send_document(
            chat_id=OWNER_ID,
            document=file_path,
            caption=f"#AutoTMV {category}\n{title}",
        )

        # Reply with the command — this creates the reply_to_message pattern
        cmd_msg = await sent_msg.reply(cmd)
        cmd_msg = await bot.get_messages(OWNER_ID, cmd_msg.id)
        cmd_msg.from_user = await bot.get_users(OWNER_ID)

        await _mirror_leech(bot, cmd_msg, isQbit=True, isLeech=True)

        await mark_tmv_processed(title, file_url, size_mb, category)
        LOGGER.info(f"TMVScraper: Leech triggered for: {title[:60]}")

    except Exception as e:
        LOGGER.error(f"TMVScraper: process_torrent failed for '{title[:60]}': {e}")

async def tmv_scraper_job():
    """
    Main scraper job — called by scheduler every TMV_SCRAPE_INTERVAL seconds.
    """
    if not config_dict.get("AUTO_TMV", False):
        return

    # Auto domain detection
    tmv_url = await _find_working_domain()
    if not tmv_url:
        LOGGER.warning("TMVScraper: No working domain found — skipping this cycle.")
        return

    LOGGER.info(f"TMVScraper: Starting scrape of {tmv_url}")

    scraper = cloudscraper.create_scraper()

    try:
        resp = scraper.get(tmv_url, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        topics = [
            fix_url(a["href"], tmv_url)
            for a in soup.find_all("a", href=True)
            if "topic" in a["href"]
        ][:40]

        LOGGER.info(f"TMVScraper: Found {len(topics)} topics")

        for topic_url in topics:
            await asyncio.sleep(random.uniform(2, 4))
            try:
                topic_html = scraper.get(topic_url, timeout=30).text
                topic_soup = BeautifulSoup(topic_html, "html.parser")
                posts = topic_soup.find_all("div", class_="cPost_contentWrap")

                for post in posts:
                    for a in post.find_all("a", href=True):
                        link_text = a.get_text(strip=True)
                        if "torrent" not in link_text.lower():
                            continue

                        href = fix_url(a["href"], tmv_url)

                        # DB check first — cheapest operation
                        if await is_tmv_processed(href):
                            continue

                        # Extract size
                        size_mb = 0
                        for sib in a.find_all_next(string=True, limit=6):
                            match = re.search(r"(\d+(\.\d+)?)\s*(gb|mb)", str(sib), re.I)
                            if match:
                                val = float(match.group(1))
                                unit = match.group(3).lower()
                                size_mb = val * 1024 if unit == "gb" else val
                                break

                        # Apply filters
                        if not should_process(link_text, size_mb):
                            continue

                        category = categorize_content(link_text)
                        filename = clean_filename(link_text)
                        tmp_path = f"/tmp/tmv_{filename}"

                        if await asyncio.to_thread(download_torrent, scraper, href, tmp_path):
                            LOGGER.info(f"TMVScraper: [{category}] Processing: {link_text[:80]}")
                            await process_torrent(None, tmp_path, link_text, href, size_mb, category)
                        else:
                            LOGGER.warning(f"TMVScraper: Download failed: {link_text[:60]}")

                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

            except Exception as e:
                LOGGER.error(f"TMVScraper: Topic error {topic_url}: {e}")
                continue

    except Exception as e:
        LOGGER.error(f"TMVScraper: Scrape error: {e}")


def register_tmv_scheduler(scheduler):
    """Register TMV scraper job with the existing bot scheduler."""
    interval = config_dict.get("TMV_SCRAPE_INTERVAL", 300)
    scheduler.add_job(
        tmv_scraper_job,
        trigger="interval",
        seconds=interval,
        id="tmv_scraper",
        replace_existing=True,
    )
    LOGGER.info(f"TMVScraper: Scheduled every {interval}s")
