#!/usr/bin/env python3
"""
Owner-only commands to manage the Auto-Leech registered user list,
plus a user-facing opt-out toggle.

Commands:
  /addautouser <user_id>     (owner only)
  /removeautouser <user_id>  (owner only)
  /listautousers             (owner only)
  /autoleech                 (any user - toggle their own opt-out status)
"""

from pyrogram import filters
from pyrogram.types import Message

from bot import bot, OWNER_ID, config_dict, user_data, DATABASE_URL
from bot.helper.ext_utils.db_handler import DbManger
from bot.helper.ext_utils.bot_utils import update_user_ldata, new_task
from bot.helper.telegram_helper.message_utils import sendMessage
from bot.helper.telegram_helper.filters import CustomFilters


def _get_auto_users_list():
    auto_users = config_dict.get("AUTO_LEECH_USERS", [])
    if isinstance(auto_users, str):
        auto_users = [int(u) for u in auto_users.split() if u.strip().lstrip("-").isdigit()]
    return list(auto_users)


def _save_auto_users_list(users_list):
    config_dict["AUTO_LEECH_USERS"] = users_list
    # Persist to DB if available so it survives restarts
    if DATABASE_URL:
        try:
            DbManger().update_config({"AUTO_LEECH_USERS": users_list})
        except Exception:
            pass


@bot.on_message(filters.command("addautouser") & CustomFilters.owner)
@new_task
async def add_auto_user(client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        await sendMessage(message, "❌ Usage: <code>/addautouser user_id</code>")
        return

    uid = int(args[1])
    auto_users = _get_auto_users_list()

    if uid in auto_users:
        await sendMessage(message, f"⚠️ User <code>{uid}</code> is already in the auto-leech list.")
        return

    auto_users.append(uid)
    _save_auto_users_list(auto_users)

    await sendMessage(
        message,
        f"✅ User <code>{uid}</code> added to Auto-Leech list.\n"
        f"Total registered users: <b>{len(auto_users)}</b>",
    )


@bot.on_message(filters.command("removeautouser") & CustomFilters.owner)
@new_task
async def remove_auto_user(client, message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        await sendMessage(message, "❌ Usage: <code>/removeautouser user_id</code>")
        return

    uid = int(args[1])
    auto_users = _get_auto_users_list()

    if uid not in auto_users:
        await sendMessage(message, f"⚠️ User <code>{uid}</code> is not in the auto-leech list.")
        return

    auto_users.remove(uid)
    _save_auto_users_list(auto_users)

    await sendMessage(
        message,
        f"✅ User <code>{uid}</code> removed from Auto-Leech list.\n"
        f"Total registered users: <b>{len(auto_users)}</b>",
    )


@bot.on_message(filters.command("listautousers") & CustomFilters.owner)
@new_task
async def list_auto_users(client, message: Message):
    auto_users = _get_auto_users_list()

    if not auto_users:
        await sendMessage(message, "📋 No users registered for Auto-Leech yet.")
        return

    lines = ["📋 <b>Auto-Leech Registered Users :</b>\n"]
    for uid in auto_users:
        u_dict = user_data.get(uid, {})
        opted_out = u_dict.get("auto_leech_optout", False)
        status = "🔴 Opted Out" if opted_out else "🟢 Active"
        lines.append(f"• <code>{uid}</code> — {status}")

    await sendMessage(message, "\n".join(lines))


@bot.on_message(filters.command("autoleech") & filters.private)
@new_task
async def toggle_auto_leech_optout(client, message: Message):
    """
    Lets a registered user opt out of (or back into) auto-leech
    without needing the owner's involvement.
    """
    user_id = message.from_user.id
    auto_users = _get_auto_users_list()

    if user_id not in auto_users:
        await sendMessage(
            message,
            "ℹ️ You are not currently registered for Auto-Leech.\n"
            "Ask the bot owner to add you first.",
        )
        return

    u_dict = user_data.get(user_id, {})
    current_optout = u_dict.get("auto_leech_optout", False)
    new_state = not current_optout

    update_user_ldata(user_id, "auto_leech_optout", new_state)
    if DATABASE_URL:
        await DbManger().update_user_data(user_id)

    if new_state:
        await sendMessage(
            message,
            "🔴 You have <b>opted out</b> of Auto-Leech.\n"
            "Send /autoleech again anytime to opt back in.",
        )
    else:
        await sendMessage(
            message,
            "🟢 You have <b>opted back in</b> to Auto-Leech.\n"
            "You'll now receive auto-leeched files again.",
        )
