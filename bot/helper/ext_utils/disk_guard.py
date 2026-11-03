"""Disk guard: low-space alert + cleanup of leftover download folders."""
import asyncio
import html
import os
import shutil
import time

from bot import bot, LOGGER, config_dict, download_dict

_last_disk_alert = 0.0
_last_cleanup_note = 0.0
_last_names = ""


def _cfg(name, default):
    v = config_dict.get(name)
    if v in (None, ""):
        v = os.environ.get(name)
    return default if v in (None, "") else v


def _alert_text_size(b):
    return f"{b / 1e9:.1f}GB"


def _tree_info(path):
    """(total size in bytes, newest modification time) of a folder tree."""
    total, newest = 0, os.path.getmtime(path)
    for dp, dns, fns in os.walk(path):
        for n in fns:
            try:
                st = os.lstat(os.path.join(dp, n))
                total += st.st_size
                newest = max(newest, st.st_mtime)
            except OSError:
                pass
        try:
            newest = max(newest, os.path.getmtime(dp))
        except OSError:
            pass
    return total, newest


def scan(root, stale_secs, active_ids):
    """Leftovers: numeric task folders, no running task, unchanged for stale_secs."""
    out = []
    now = time.time()
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not (name.isdigit() and os.path.isdir(p) and not os.path.islink(p)):
            continue
        if name in active_ids:
            continue
        size, newest = _tree_info(p)
        if now - newest >= stale_secs:
            out.append((name, size, now - newest))
    return sorted(out, key=lambda x: -x[1])


async def _alert(text):
    owner = config_dict.get("OWNER_ID")
    if not owner:
        return
    try:
        await bot.send_message(chat_id=owner, text=text)
    except Exception as e:
        LOGGER.error(f"DiskGuard: alert failed: {e}")


async def disk_guard_job():
    global _last_disk_alert, _last_cleanup_note, _last_names
    root = str(config_dict.get("DOWNLOAD_DIR") or "/usr/src/app/downloads/").rstrip("/")
    if len(root) < 8 or "download" not in root.lower() or not os.path.isdir(root):
        LOGGER.error(f"DiskGuard: unsafe or missing downloads dir {root!r}, skipping")
        return
    now = time.time()

    # 1. low space alert (at most every 6 hours)
    try:
        free = shutil.disk_usage(root).free
        limit = float(_cfg("DISK_ALERT_GB", 20)) * 1e9
        if free < limit and now - _last_disk_alert > 6 * 3600:
            _last_disk_alert = now
            await _alert(
                f"⚠️ Low disk space: {_alert_text_size(free)} free "
                f"(alert limit {_alert_text_size(limit)})."
            )
    except Exception as e:
        LOGGER.error(f"DiskGuard: space check failed: {e}")

    # 2. leftover folders
    mode = str(_cfg("CLEANUP_MODE", "alert")).strip().lower()
    if mode not in ("alert", "delete"):
        return
    try:
        hours = float(_cfg("CLEANUP_HOURS", 12))
    except ValueError:
        hours = 12.0
    active = {str(k) for k in list(download_dict.keys())}
    try:
        found = await asyncio.to_thread(scan, root, hours * 3600, active)
    except Exception as e:
        LOGGER.error(f"DiskGuard: scan failed: {e}")
        return
    if not found:
        return
    total = sum(x[1] for x in found)
    lines = "\n".join(
        f"{html.escape(n)} - {_alert_text_size(s)} - idle {a / 3600:.0f}h"
        for n, s, a in found[:15]
    )
    if mode == "alert":
        names = ",".join(x[0] for x in found)
        if names != _last_names or now - _last_cleanup_note > 24 * 3600:
            _last_names, _last_cleanup_note = names, now
            await _alert(
                f"🧹 Leftover download folders ({len(found)}, {_alert_text_size(total)}) "
                f"- no running task, unchanged for {hours:g}h+. NOT deleted (alert mode):\n"
                f"{lines}\n\nTo delete these automatically, set CLEANUP_MODE = \"delete\" "
                "in config.env and restart."
            )
        return
    deleted, freed = 0, 0
    for name, size, _age in found:
        p = os.path.join(root, name)
        try:
            await asyncio.to_thread(shutil.rmtree, p)
            deleted += 1
            freed += size
        except Exception as e:
            LOGGER.error(f"DiskGuard: could not delete {p}: {e}")
    LOGGER.info(f"DiskGuard: deleted {deleted} folders, freed {_alert_text_size(freed)}")
    await _alert(f"🧹 Deleted {deleted} leftover folder(s), freed {_alert_text_size(freed)}:\n{lines}")
