#!/usr/bin/env python3
"""
Auto-Leech Listener for TamilMV (or any) RSS Scraper.

Detects torrent files posted by a fixed scraper account (USER_SESSION)
in a designated group, then triggers a leech task for every registered
auto-leech user — reusing each user's own settings (prefix, thumb,
watermark, merge mode, etc.) exactly as if they had sent /qbleech
themselves.

This file does NOT modify mirror_leech.py or tasks_listener.py.
It only calls the same qb_leech() entry point those files already expose.
"""

from pyrogram.handlers import MessageHandler
from pyrogram.filters import document, chat as chat_filter

from bot import bot, LOGGER, config_dict, user_data
from bot.helper.ext_utils.db_handler import DbManger
from bot.helper.telegram_helper.message_utils import sendMessage

# ---- CONFIGURATION ----
# The numeric Telegram user ID of the scraper's USER_SESSION account.
# Only torrents posted by this exact account will trigger auto-leech.
SCRAPER_USER_ID = 0  # <-- SET THIS to your scraper's account ID (see config_dict override below)

# The group chat ID where the scraper posts torrents (TMV_LEECH_GRP_1).
AUTO_LEECH_GROUP_ID = 0  # <-- SET THIS, or leave 0 to allow any group/chat


def get_scraper_id():
    """Allow override via config_dict (config.env: AUTO_LEECH_SCRAPER_ID)."""
    val = config_dict.get("AUTO_LEECH_SCRAPER_ID", "")
    if val and str(val).lstrip("-").isdigit():
        return int(val)
    return SCRAPER_USER_ID


def get_group_id():
    """Allow override via config_dict (config.env: AUTO_LEECH_GROUP_ID)."""
    val = config_dict.get("AUTO_LEECH_GROUP_ID", "")
    if val and str(val).lstrip("-").isdigit():
        return int(val)
    return AUTO_LEECH_GROUP_ID


async def get_registered_auto_users():
    """
    Returns list of user_ids who are:
      1. Added by the owner via /addautouser
      2. Have NOT opted out via their own settings (auto_leech_optout != True)
    """
    auto_users = config_dict.get("AUTO_LEECH_USERS", [])
    if isinstance(auto_users, str):
        auto_users = [int(u) for u in auto_users.split() if u.strip().lstrip("-").isdigit()]

    active_users = []
    for uid in auto_users:
        u_dict = user_data.get(uid, {})
        if not u_dict.get("auto_leech_optout", False):
            active_users.append(uid)
    return active_users


def is_torrent_file(message):
    """Check if message contains a .torrent document."""
    if not message.document:
        return False
    fname = message.document.file_name or ""
    return fname.lower().endswith(".torrent")


async def auto_leech_handler(client, message):
    """
    Triggered on every new message in the configured group.
    Filters down to: torrent file + posted by the scraper account.
    """
    scraper_id = get_scraper_id()
    group_id = get_group_id()

    if scraper_id == 0:
        return  # Not configured yet, do nothing

    if group_id != 0 and message.chat.id != group_id:
        return

    if not message.from_user or message.from_user.id != scraper_id:
        return  # Not from the scraper account, ignore (manual posts unaffected)

    if not is_torrent_file(message):
        return

    auto_users = await get_registered_auto_users()
    if not auto_users:
        LOGGER.info("Auto-Leech: Torrent detected from scraper, but no registered users.")
        return

    LOGGER.info(
        f"Auto-Leech: Torrent '{message.document.file_name}' detected from scraper. "
        f"Triggering leech for {len(auto_users)} user(s): {auto_users}"
    )

    # Import here to avoid circular import at module load time
    from bot.modules.mirror_leech import _mirror_leech
    from copy import copy

    for uid in auto_users:
        try:
            # Create an independent shallow copy per user so concurrent
            # _mirror_leech tasks (each is @new_task, runs concurrently)
            # never share or race on the same message.from_user object.
            user_message = copy(message)
            user_message.from_user = await client.get_users(uid)

            # qbleech-equivalent: isQbit=True, isLeech=True (matches qb_leech())
            _mirror_leech(client, user_message, isQbit=True, isLeech=True)

        except Exception as e:
            LOGGER.error(f"Auto-Leech: Failed to trigger for user {uid}: {e}")
            continue


# Register the handler — scoped to document messages only.
# Group/sender/file-type checks happen inside auto_leech_handler itself,
# so this stays safe even if AUTO_LEECH_GROUP_ID is set after bot start.
bot.add_handler(
    MessageHandler(
        auto_leech_handler,
        filters=document,
    )
)
