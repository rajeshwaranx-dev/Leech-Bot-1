#!/usr/bin/env python3
import re
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler
from bot import LOGGER, user_data, config_dict
from bot.helper.telegram_helper.bot_commands import BotCommands

MAGNET_REGEX = re.compile(r"magnet:\?xt=urn:[a-zA-Z0-9]+:[a-zA-Z0-9]{32,}", re.IGNORECASE)

def _is_torrent_doc(message):
    doc = message.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    return name.endswith(".torrent") or mime in ("application/x-bittorrent", "application/x-torrent")

def _is_magnet(message):
    text = message.text or message.caption or ""
    return bool(MAGNET_REGEX.search(text))

async def _auto_leech_filter(_, __, message):
    if not config_dict.get("AUTO_LEECH", False):
        return False
    chat_id = message.chat.id
    chat_data = user_data.get(chat_id, {})
    if not chat_data.get("is_auth") and chat_id != config_dict.get("OWNER_ID"):
        return False
    return _is_torrent_doc(message) or _is_magnet(message)

auto_leech_filter = create(_auto_leech_filter)

async def handle_auto_leech(client, message):
    from bot.modules.mirror_leech import _mirror_leech
    user_id = message.from_user.id if message.from_user else None
    chat_id = message.chat.id
    LOGGER.info(f"AutoLeech: triggered by user {user_id} in chat {chat_id}")

    try:
        cmd = f"/{BotCommands.QbLeechCommand[0]}"

        if _is_torrent_doc(message):
            # Patch: make message look like /qbleech was sent
            # and the torrent is in reply_to_message
            message.reply_to_message = message
            message.text = cmd

        elif _is_magnet(message):
            magnet = MAGNET_REGEX.search(message.text or message.caption).group(0)
            message.text = f"{cmd} {magnet}"

        await _mirror_leech(client, message, isQbit=True, isLeech=True)
        LOGGER.info(f"AutoLeech: started for user {user_id}")
    except Exception as e:
        LOGGER.error(f"AutoLeech: Failed: {e}")

def add_auto_leech_handlers(app):
    app.add_handler(MessageHandler(handle_auto_leech, filters=auto_leech_filter), group=2)
    LOGGER.info("AutoLeech: Handler registered.")
