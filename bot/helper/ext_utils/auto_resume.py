#!/usr/bin/env python3
"""
Auto Resume Task — re-triggers leech/mirror tasks that were interrupted
by a bot restart/crash.

⚠️ CRITICAL ORDERING REQUIREMENT ⚠️
The existing get_incomplete_tasks() (used by INCOMPLETE_TASK_NOTIFIER)
DROPS the entire MongoDB tasks table after reading it. Therefore
resume_incomplete_tasks() MUST be called BEFORE the notifier block
in __main__.py, otherwise the table will already be empty and resume
will silently find nothing. See main_patch_instructions for exact
placement.

This runs ONCE at bot startup. It does not modify tasks_listener.py,
mirror_leech.py, or any live download/upload logic.

Safety design:
  - Each task resume is wrapped in its own try/except — one failure
    never stops the others or crashes bot startup.
  - Staggered with a short delay between each, to avoid bursting
    qBittorrent/aria2 with simultaneous requests right at boot.
  - Controlled by a dedicated config flag (AUTO_RESUME_TASKS) so it
    can be disabled instantly without a code rollback.
  - Re-fetches the REAL original Telegram message (not a synthetic
    reconstruction), so attached torrent files are preserved exactly
    as they were.
"""

from asyncio import sleep

from bot import bot, LOGGER, DATABASE_URL, config_dict
from bot.helper.ext_utils.db_handler import DbManger

RESUME_STAGGER_SECONDS = 3


async def resume_incomplete_tasks():
    """
    Called once from __main__.py at startup, after the existing
    INCOMPLETE_TASK_NOTIFIER block. Reads raw incomplete task rows
    directly from MongoDB (cid, link, tag, source, org_msg) and
    re-triggers each as a fresh leech/mirror task.
    """
    if not config_dict.get("AUTO_RESUME_TASKS", False):
        return

    if not DATABASE_URL:
        LOGGER.info("Auto Resume: Skipped, DATABASE_URL not set.")
        return

    db = DbManger()
    raw_tasks = await db.get_incomplete_tasks_raw()

    if not raw_tasks:
        LOGGER.info("Auto Resume: No incomplete tasks found.")
        return

    LOGGER.info(f"Auto Resume: Found {len(raw_tasks)} incomplete task(s). Resuming...")

    # Import here to avoid circular import at module load time
    from bot.modules.mirror_leech import _mirror_leech

    resumed_count = 0
    failed_count = 0

    for task in raw_tasks:
        link_id = task.get("_id")
        cid = task.get("cid")
        tag = task.get("tag", "")

        try:
            # Re-fetch the REAL original message from Telegram.
            # link_id is the message.link stored by add_incomplete_task,
            # which Pyrogram can resolve back via get_messages using
            # the chat id + message id parsed from that link.
            msg_id = _extract_message_id(link_id)
            if not msg_id:
                LOGGER.warning(f"Auto Resume: Could not parse message id from '{link_id}', skipping.")
                failed_count += 1
                continue

            original_message = await bot.get_messages(chat_id=cid, message_ids=msg_id)

            if not original_message or original_message.empty:
                LOGGER.warning(f"Auto Resume: Original message {link_id} no longer exists, skipping.")
                # Clean up stale DB entry since the source message is gone
                await db.rm_complete_task(link_id)
                failed_count += 1
                continue

            # Replay exactly as if the user sent /qbleech or /leech again.
            # isQbit/isLeech detection: default to qBit leech since that's
            # the most common path for torrent-based tasks in this bot.
            is_qbit = bool(original_message.document and
                            (original_message.document.file_name or "").lower().endswith(".torrent"))

            await _mirror_leech(bot, original_message, isQbit=is_qbit, isLeech=True)

            LOGGER.info(f"Auto Resume: Resumed task for {tag} | {link_id}")
            resumed_count += 1

            # Remove this entry now that it's been re-queued, so it
            # doesn't get resumed AGAIN on the next restart. A fresh
            # incomplete-task entry will be created for it by the
            # normal add_incomplete_task() flow if it's interrupted again.
            await db.rm_complete_task(link_id)

        except Exception as e:
            LOGGER.error(f"Auto Resume: Failed to resume task {link_id} for {tag}: {e}")
            failed_count += 1

        # Stagger to avoid bursting the download engine at boot
        await sleep(RESUME_STAGGER_SECONDS)

    LOGGER.info(
        f"Auto Resume: Completed. Resumed: {resumed_count}, Failed: {failed_count}"
    )


def _extract_message_id(message_link):
    """
    Parses a Telegram message link (e.g. https://t.me/c/123456789/42)
    and returns the trailing message ID as an int, or None if unparseable.
    """
    if not message_link:
        return None
    try:
        parts = str(message_link).rstrip("/").split("/")
        return int(parts[-1])
    except (ValueError, IndexError):
        return None
  
