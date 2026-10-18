#!/usr/bin/env python3
"""
Auto Leech Handler
------------------
Watches authorized chats for .torrent files or magnet links sent by
whitelisted users and automatically triggers qbleech — no command needed.

Global toggle  : config_dict["AUTO_LEECH"]          (owner via /bsetting)
Whitelist      : config_dict["AUTO_LEECH_WHITELIST"] (space-separated user IDs)
User dest chan  : user_dict["auto_leech_channel"]    (per-user, optional)

Flow:
  1. Message arrives in an authorized chat
  2. Sender is in AUTO_LEECH_WHITELIST
  3. Message contains a .torrent document OR a magnet link
  4. Bot replies with /qbleech (triggering the existing leech pipeline)
  5. If user has auto_leech_channel set → leech goes there
     Otherwise → falls back to user's normal leech destination
"""

import re
from pyrogram import Client
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from bot import LOGGER, user_data, config_dict
from bot.helper.telegram_helper.bot_commands import BotCommands
from bot.helper.telegram_helper.filters import CustomFilters

MAGNET_REGEX = re.compile(r"magnet:\?xt=urn:[a-zA-Z0-9]+:[a-zA-Z0-9]{32,}", re.IGNORECASE)


def _is_torrent_doc(message: Message) -> bool:
    """Returns True if the message contains a .torrent file."""
    doc = message.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    return name.endswith(".torrent") or mime in (
        "application/x-bittorrent",
        "application/x-torrent",
    )


def _is_magnet(message: Message) -> bool:
    """Returns True if the message text/caption contains a magnet link."""
    text = message.text or message.caption or ""
    return bool(MAGNET_REGEX.search(text))


def _in_whitelist(user_id: int) -> bool:
    """Returns True if user_id is in AUTO_LEECH_WHITELIST."""
    whitelist_raw = config_dict.get("AUTO_LEECH_WHITELIST", "")
    if not whitelist_raw:
        return False
    whitelist = {int(uid.strip()) for uid in whitelist_raw.split() if uid.strip().lstrip("-").isdigit()}
    return user_id in whitelist


async def _auto_leech_filter(_, __, message: Message) -> bool:
    """
    Pyrogram filter — passes only when ALL of these are true:
    - AUTO_LEECH global toggle is ON
    - Message is in an authorized chat
    - Sender is in the whitelist
    - Message is a .torrent file or contains a magnet link
    """
    if not config_dict.get("AUTO_LEECH", False):
        return False

    # Must be in an authorized chat (reuse existing auth logic)
    chat_id = message.chat.id
    chat_data = user_data.get(chat_id, {})
    if not chat_data.get("is_auth") and chat_id != config_dict.get("OWNER_ID"):
        return False

    # Sender must be whitelisted
    sender_id = message.from_user.id if message.from_user else None
    if not sender_id or not _in_whitelist(sender_id):
        return False

    # Must carry a torrent or magnet
    return _is_torrent_doc(message) or _is_magnet(message)


auto_leech_filter = create(_auto_leech_filter)


async def handle_auto_leech(client: Client, message: Message):
    """
    Main handler — triggers /qbleech automatically.

    If the user has an auto_leech_channel set, we pass it as the upload
    destination by replying with:
        /qbleech
    (The existing qbleech handler reads the user's settings including
    auto_leech_channel the same way it reads leech_dest.)
    """
    user_id = message.from_user.id
    chat_id = message.chat.id

    user_dict = user_data.get(user_id, {})
    auto_leech_channel = user_dict.get("auto_leech_channel", "")

    trigger_type = "torrent" if _is_torrent_doc(message) else "magnet"
    LOGGER.info(
        f"AutoLeech: {trigger_type} from user {user_id} in chat {chat_id}"
        + (f" → channel {auto_leech_channel}" if auto_leech_channel else " → default dest")
    )

    try:
        cmd = f"/{BotCommands.QbLeechCommand[0]}"
        await message.reply(cmd)
        LOGGER.info(f"AutoLeech: Triggered '{cmd}' for message {message.id} in {chat_id}")
    except Exception as e:
        LOGGER.error(f"AutoLeech: Failed to trigger for message {message.id}: {e}")


def add_auto_leech_handlers(app: Client):
    """Call this from your bot startup to register the Auto Leech handler."""
    app.add_handler(
        MessageHandler(handle_auto_leech, filters=auto_leech_filter),
        group=2,  # group 2 so it runs after auth checks in group 0/1
    )
    LOGGER.info("AutoLeech: Handler registered.")
