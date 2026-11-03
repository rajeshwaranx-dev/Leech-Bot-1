"""Auto Post: poster + sticker + files (size order) + sticker, per channel."""
import asyncio
import difflib
import html
import io
import json
import re
import time

import requests

from bot import bot, LOGGER, config_dict
from bot.helper.ext_utils import post_tracker as pt

try:
    from pyrogram.errors import FloodWait
except Exception:
    class FloodWait(Exception):
        value = 5

QUIET = 180  # seconds with no activity after all files finished
STICKER_FILE = "post_stickers.json"
_lock = asyncio.Lock()
_dump_lock = asyncio.Lock()

ABBR = {"tam": "Tamil", "tel": "Telugu", "hin": "Hindi", "mal": "Malayalam",
        "kan": "Kannada", "eng": "English"}
LANGS = {"tamil", "telugu", "hindi", "malayalam", "kannada", "english",
         "bengali", "marathi", "punjabi", "korean", "japanese", "chinese",
         "thai", "spanish", "french"}
QUALITY = [
    ("Proper HD", r"proper\s*hd"),
    ("BluRay", r"blu-?ray"),
    ("TRUE WEB-DL", r"true\s*web-?dl"),
    ("WEB-DL", r"web-?dl"),
    ("WEBRip", r"web-?rip"),
    ("HQ HDRip", r"hq\s*hd-?rip"),
    ("HDRip", r"hd-?rip"),
    ("PreDVD", r"pre-?dvd"),
    ("HDTC", r"\bhdtc\b"),
    ("DVDScr", r"dvd-?scr"),
    ("HDCAM", r"hd-?cam|\bcam\b"),
    ("HDTV", r"hdtv"),
]
FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def save_sticker(n, file_id):
    """Keep stickers in a file too, so they survive restarts without a database."""
    try:
        try:
            with open(STICKER_FILE) as f:
                d = json.load(f)
        except Exception:
            d = {}
        d[str(n)] = file_id
        with open(STICKER_FILE, "w") as f:
            json.dump(d, f)
    except Exception as e:
        LOGGER.error(f"AutoPost: sticker save failed: {e}")


def get_sticker(n):
    v = config_dict.get(f"STICKER_{n}")
    if v:
        return v
    try:
        with open(STICKER_FILE) as f:
            return json.load(f).get(str(n), "")
    except Exception:
        return ""


def parse_info(title):
    t = re.sub(r"^[#\s]+", "", title or "").strip()
    year = ""
    m = re.search(r"\((\d{4})\)", t)
    if m:
        year = m.group(1)
        name = t[: m.start()]
        rest = t[m.end():]
    else:
        parts = re.split(r"\s[-|]\s|\[", t, maxsplit=1)
        name = parts[0]
        rest = t[len(name):]
    name = name.strip(" -|:")
    langs = []
    for w in re.findall(r"[A-Za-z]+", rest):
        lw = w.lower()
        full = ABBR.get(lw) or (w.title() if lw in LANGS else None)
        if full and full not in langs:
            langs.append(full)
    audio = " + ".join(langs) if langs else "N/A"
    quality = "N/A"
    for label, pat in QUALITY:
        if re.search(pat, t, flags=re.I):
            quality = label
            break
    return name or t, year, audio, quality


def add_badge(img_bytes, username):
    """Draw a dark pill with a Telegram icon + @username at the bottom right."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        im = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        W, H = im.size
        fs = max(18, int(H * 0.045))
        font = None
        for f in FONTS:
            try:
                font = ImageFont.truetype(f, fs)
                break
            except Exception:
                pass
        if font is None:
            font = ImageFont.load_default(size=fs)
        text = username.strip()
        d0 = ImageDraw.Draw(im)
        tb = d0.textbbox((0, 0), text, font=font)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        pad = int(fs * 0.55)
        ic = int(fs * 1.5)
        bw, bh = pad + ic + pad + tw + pad * 2, ic + pad
        x1, y1 = W - int(W * 0.02), H - int(H * 0.04)
        x0, y0 = x1 - bw, y1 - bh
        ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        d.rounded_rectangle((x0, y0, x1, y1), radius=bh // 4,
                            fill=(18, 24, 33, 215),
                            outline=(255, 255, 255, 60), width=2)
        cx, cy = x0 + pad + ic // 2, y0 + bh // 2
        r = ic // 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(41, 160, 220, 255))
        s = r / 10.0
        pts = [(cx - 6.5 * s, cy - 0.5 * s), (cx + 6.5 * s, cy - 5 * s),
               (cx + 3 * s, cy + 6 * s), (cx - 0.5 * s, cy + 2.5 * s),
               (cx - 2.5 * s, cy + 5 * s), (cx - 2.5 * s, cy + 1.5 * s)]
        d.polygon(pts, fill=(255, 255, 255, 255))
        d.text((x0 + pad + ic + pad, y0 + bh // 2 - th // 2 - tb[1]), text,
               font=font, fill=(255, 255, 255, 255))
        out = Image.alpha_composite(im, ov).convert("RGB")
        bio = io.BytesIO()
        out.save(bio, "JPEG", quality=92)
        return bio.getvalue()
    except Exception as e:
        LOGGER.error(f"AutoPost: badge failed: {e}")
        return img_bytes


def _norm(x):
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()


def _name_ok(ours, res):
    """Accept a TMDB result only when its title really matches ours."""
    a = _norm(ours)
    if not a:
        return False
    for k in ("title", "original_title", "name", "original_name"):
        raw = res.get(k) or ""
        b = _norm(raw)
        if not b:
            continue
        if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
            return True
        if re.match(re.escape(ours.strip()) + r"\s*(:|-|\u2013)\s", raw, re.I):
            return True
    return False


def _tmdb_image(name, year, key, is_series=False):
    headers = {}
    base = {}
    if len(key) > 40:
        headers["Authorization"] = f"Bearer {key}"
    else:
        base["api_key"] = key
    if is_series:
        tries = [("tv", year), ("tv", ""), ("movie", year)]
    else:
        tries = [("movie", year), ("movie", ""), ("tv", "")]
    for kind, yr in tries:
        params = dict(base, query=name)
        if yr:
            params["first_air_date_year" if kind == "tv" else "year"] = yr
        r = requests.get(f"https://api.themoviedb.org/3/search/{kind}",
                         params=params, headers=headers, timeout=15)
        for res in (r.json().get("results") or [])[:8]:
            if res.get("backdrop_path") and _name_ok(name, res):
                img = requests.get(
                    "https://image.tmdb.org/t/p/w1280" + res["backdrop_path"],
                    timeout=30)
                if img.status_code == 200 and img.content:
                    return img.content
    return None


def parse_series(title, names):
    """Returns (season, episodes) like ('S01', 'E05-E08'); '' when not a series."""
    blob = " ".join([title or ""] + list(names or []))
    season = ""
    sm = re.search(r"\bS(\d{1,3})(?=E\d|\b)", blob, flags=re.I)
    if sm:
        season = f"S{int(sm.group(1)):02d}"
    eps = sorted({int(x) for x in re.findall(r"S\d{1,3}E(\d{1,3})", blob, flags=re.I)})
    label = ""
    if eps:
        label = f"E{eps[0]:02d}" if len(eps) == 1 else f"E{eps[0]:02d}-E{eps[-1]:02d}"
    else:
        rm = re.search(r"\bEP\s*\(\s*(\d{1,3})\s*[-\u2013]\s*(\d{1,3})\s*\)", blob, flags=re.I)
        if rm:
            label = f"E{int(rm.group(1)):02d}-E{int(rm.group(2)):02d}"
        else:
            em = re.search(r"\bEP\s*(\d{1,3})\b", blob, flags=re.I)
            if em:
                label = f"E{int(em.group(1)):02d}"
    return season, label


async def _alert(text):
    """Failure alerts go to the owner's PM."""
    owner = config_dict.get("OWNER_ID")
    if not owner:
        return
    try:
        await bot.send_message(chat_id=owner, text=text)
    except Exception as e:
        LOGGER.error(f"AutoPost: alert failed: {e}")


async def _copy(dest, cid, mid):
    for _ in range(2):
        try:
            await bot.copy_message(chat_id=dest, from_chat_id=cid,
                                   message_id=mid, reply_markup=None)
            return True
        except FloodWait as e:
            await asyncio.sleep(getattr(e, "value", 5) + 1)
        except Exception as e:
            LOGGER.error(f"AutoPost: copy {cid}/{mid} to {dest} failed: {e}")
            return False
    return False


async def _sticker(dest, n):
    fid = get_sticker(n)
    if not fid:
        return
    try:
        await bot.send_sticker(chat_id=dest, sticker=fid)
    except Exception as e:
        LOGGER.error(f"AutoPost: sticker {n} to {dest} failed: {e}")


async def dump_files(msgs):
    """Copy freshly uploaded files to the raw dump channel (Auto Leech Channel)."""
    dest = config_dict.get("AUTO_LEECH_CHANNEL")
    if not dest or not msgs:
        return
    async with _dump_lock:
        seen = set()
        for pair in msgs:
            tup = (pair[0], pair[1])
            if tup in seen:
                continue
            seen.add(tup)
            await _copy(dest, tup[0], tup[1])
            await asyncio.sleep(2)


async def _post_to(t, cap_base, img, msgs, m):
    """Poster+caption, sticker 1, files in order, sticker 2 -> one channel.
    Retries never repeat the poster or files that were already sent."""
    hk, ck = f"head_{t}", f"copied_{t}"
    if not m.get(hk):
        uname = ""
        try:
            uname = (await bot.get_chat(t)).username or ""
        except Exception as e:
            LOGGER.error(f"AutoPost: get_chat {t} failed: {e}")
        cap = cap_base + (f"\n📥 Upload : @{uname}" if uname else "")
        if img:
            wm = (config_dict.get("POSTER_WATERMARK") or get_sticker("wm") or "").strip()
            data = add_badge(img, wm) if wm else img
            bio = io.BytesIO(data)
            bio.name = "poster.jpg"
            await bot.send_photo(chat_id=t, photo=bio, caption=cap)
        else:
            await bot.send_message(chat_id=t, text=cap)
        await _sticker(t, 1)
        m[hk] = True
    done = m.setdefault(ck, [])
    ok = 0
    for cid, mid in msgs:
        if [cid, mid] in done:
            ok += 1
            continue
        if await _copy(t, cid, mid):
            done.append([cid, mid])
            ok += 1
        await asyncio.sleep(2)
    if ok == len(msgs) or (ok > 0 and m.get("post_tries", 0) >= 3):
        await _sticker(t, 2)
    return ok


async def post_movie(key, m):
    dest = config_dict.get("POSTER_CHANNEL")
    name, year, audio, quality = parse_info(m.get("title", ""))
    items = sorted(m["items"], key=lambda i: i.get("size") or 0)
    names = [i.get("name", "") for i in items]
    if audio == "N/A" or quality == "N/A":
        _n, _y, a2, q2 = parse_info(" ".join(names))
        audio = a2 if audio == "N/A" else audio
        quality = q2 if quality == "N/A" else quality
    seen, msgs = set(), []
    for it in items:
        for pair in it.get("links") or []:
            tup = (pair[0], pair[1])
            if tup not in seen:
                seen.add(tup)
                msgs.append(tup)
    if not msgs:
        m["post_error"] = "no uploaded files were saved for this movie"
        LOGGER.error(f"AutoPost: no saved messages for {name}, nothing to post")
        return False

    season, eps = parse_series(m.get("title", ""), names)
    cap_base = (
        f"🎬 Title : {html.escape(name)}\n"
        f"📅 Year : {html.escape(year or 'N/A')}\n"
    )
    if season:
        cap_base += f"📺 Season : {season}\n"
    if eps:
        cap_base += f"🎞 Episode : {eps}\n"
    cap_base += (
        f"🔊 Audio : {html.escape(audio)}\n"
        f"💿 Quality : {html.escape(quality)}"
    )
    img = None
    key_t = (config_dict.get("TMDB_API_KEY") or "").strip()
    if key_t:
        try:
            img = await asyncio.to_thread(_tmdb_image, name, year, key_t, bool(season))
        except Exception as e:
            LOGGER.error(f"AutoPost: TMDB failed: {e}")
    if not img:
        LOGGER.info(f"AutoPost: no matching poster for {name}, text only")

    ok_count = await _post_to(dest, cap_base, img, msgs, m)
    m["ok_files"], m["total_files"] = ok_count, len(msgs)
    bk = config_dict.get("BACKUP_CHANNEL")
    if config_dict.get("BACKUP_POST") and bk:
        try:
            await _post_to(bk, cap_base, img, msgs, m)
        except Exception as e:
            LOGGER.error(f"AutoPost: backup post failed: {e}")
            m["backup_error"] = str(e)[:100]
    LOGGER.info(f"AutoPost: posted {name} ({ok_count}/{len(msgs)} files)")
    if ok_count == 0:
        m["post_error"] = "no file could be copied to the Poster Channel"
    return ok_count > 0


async def auto_post_job():
    if not config_dict.get("AUTO_POST") or not config_dict.get("POSTER_CHANNEL"):
        return
    if _lock.locked():
        return
    async with _lock:
        now = time.time()
        timeout = int(config_dict.get("POST_TIMEOUT") or 60) * 60
        for key, m in list(pt.all_movies().items()):
            if m.get("posted") or m.get("post_tries", 0) >= 3 or m["done"] < 1:
                continue
            idle = now - m["updated"]
            if not ((pt.is_complete(key) and idle >= QUIET) or idle >= timeout):
                continue
            m["post_tries"] = m.get("post_tries", 0) + 1
            m["post_error"] = ""
            title = html.escape(m.get("title", "")[:60])
            try:
                if await post_movie(key, m):
                    m["posted"] = True
                    pending = m["launched"] - m["done"] - m["failed"]
                    problems = []
                    if m["failed"] > 0:
                        problems.append(f"{m['failed']} download/upload failed")
                    if pending > 0:
                        problems.append(f"{pending} not finished (posted after timeout)")
                    if m.get("ok_files", 0) < m.get("total_files", 0):
                        problems.append(f"only {m['ok_files']} of {m['total_files']} files copied")
                    if m.get("backup_error"):
                        problems.append("Backup Channel post failed")
                    if problems:
                        await _alert(f"⚠️ Posted with problems: {title}\n" + "\n".join(problems))
            except Exception as e:
                m["post_error"] = str(e)[:150]
                LOGGER.error(f"AutoPost: post failed for {m.get('title','')[:40]}: {e}")
            if not m.get("posted") and m.get("post_tries", 0) >= 3 and not m.get("alerted"):
                m["alerted"] = True
                await _alert(
                    f"❌ Auto Post failed: {title}\n"
                    f"Reason: {html.escape(m.get('post_error') or 'unknown')}\n"
                    f"Files: {m['done']} done, {m['failed']} failed of {m['launched']}. "
                    "It will not retry."
                )
            pt.save()
