#!/usr/bin/env python3
import os
import re
import json
import subprocess
from pathlib import Path
from bot import LOGGER

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".flv"}

# Junk tags stripped out while extracting a clean title.
# Order matters less here since we strip by regex, not position.
JUNK_TAGS = [
    r"\bweb-?dl\b", r"\bweb-?rip\b", r"\bbluray\b", r"\bbrrip\b", r"\bbdrip\b",
    r"\bhdrip\b", r"\bdvdrip\b", r"\bhdtv\b", r"\bcamrip\b", r"\bhdcam\b",
    r"\bx264\b", r"\bx265\b", r"\bh264\b", r"\bh\.264\b", r"\bh265\b", r"\bh\.265\b",
    r"\bhevc\b", r"\bavc\b", r"\bxvid\b",
    r"\besub\b", r"\bmsubs?\b", r"\bsub(?:bed|s)?\b",
    r"\bdual\s?audio\b", r"\bmulti\s?audio\b", r"\bmulti\b",
    r"\b\d{3,4}p\b",  # resolution (also captured separately, stripped from title)
    r"\b\d+(?:\.\d+)?(?:gb|mb)\b",
    r"\bac3\b", r"\bddp?5\.1\b", r"\bddp?2\.0\b", r"\baac\b", r"\bdts\b",
    r"\b10bit\b", r"\b8bit\b",
    r"\[.*?\]", r"\(.*?\)",  # bracketed/parenthesized junk (release group tags etc.)
    r"@\w+",  # telegram handles/usernames in filename
]

LANG_NAMES = {
    "tam": "Tamil", "hin": "Hindi", "tel": "Telugu", "mal": "Malayalam",
    "kan": "Kannada", "eng": "English", "jpn": "Japanese", "chi": "Chinese",
    "zho": "Chinese", "kor": "Korean", "ara": "Arabic", "fre": "French",
    "fra": "French", "ger": "German", "deu": "German", "spa": "Spanish",
    "por": "Portuguese", "rus": "Russian", "ita": "Italian",
}


def probe_media_info(file_path):
    """
    Return dict: {quality, codec, audio_langs (list)}
    Uses ffprobe once for both video + audio streams.
    """
    info = {"quality": None, "codec": None, "audio_langs": []}
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return info
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        for s in streams:
            if s.get("codec_type") == "video" and not info["quality"]:
                height = s.get("height", 0)
                info["quality"] = height_to_quality(height)
                codec_name = s.get("codec_name", "").lower()
                info["codec"] = {
                    "h264": "x264", "hevc": "x265", "h265": "x265",
                    "vp9": "VP9", "av1": "AV1",
                }.get(codec_name, codec_name.upper() if codec_name else None)
            elif s.get("codec_type") == "audio":
                lang = (s.get("tags") or {}).get("language", "").lower().strip()
                if lang and lang != "und":
                    name = LANG_NAMES.get(lang, lang.capitalize())
                    if name not in info["audio_langs"]:
                        info["audio_langs"].append(name)
    except Exception as e:
        LOGGER.error(f"AutoRename ffprobe exception: {e}")
    return info


def height_to_quality(height):
    if not height:
        return None
    if height >= 2000:
        return "4K"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    if height >= 470:
        return "480p"
    if height >= 350:
        return "360p"
    return f"{height}p"


def extract_title(filename):
    """
    Best-effort clean title extraction from a messy filename.
    Returns None if extraction looks unreliable (caller falls back to original name).
    """
    name = Path(filename).stem
    original = name

    # Replace dots/underscores with spaces first (common torrent naming)
    name = re.sub(r"[._]", " ", name)

    # Strip junk tags
    for pattern in JUNK_TAGS:
        name = re.sub(pattern, " ", name, flags=re.IGNORECASE)

    # Collapse multiple spaces/dashes, trim
    name = re.sub(r"[-–—]+", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -_.")

    # Sanity check: if we stripped away almost everything, extraction is unreliable
    if not name or len(name) < 2:
        LOGGER.warning(f"AutoRename: Title extraction failed for '{original}', will fall back to original.")
        return None

    # Sanity check: result shouldn't be mostly numbers/symbols
    alpha_chars = sum(c.isalpha() for c in name)
    if alpha_chars < 2:
        LOGGER.warning(f"AutoRename: Extracted title '{name}' looks unreliable, falling back to original.")
        return None

    return name.strip()


def build_filename(template, original_filename, file_path):
    """
    Fill {title}, {quality}, {codec}, {audio}, {ext} into the template.
    Falls back to the original filename (unchanged) if title extraction fails.
    """
    ext = Path(original_filename).suffix
    is_video = ext.lower() in VIDEO_EXTENSIONS

    title = extract_title(original_filename)
    if title is None:
        # Fallback: keep original filename entirely, untouched
        return original_filename

    media_info = {"quality": None, "codec": None, "audio_langs": []}
    if is_video:
        media_info = probe_media_info(file_path)

    quality = media_info.get("quality") or ""
    codec = media_info.get("codec") or ""
    audio = "+".join(media_info.get("audio_langs", [])) if media_info.get("audio_langs") else ""

    try:
        new_name = template.format(
            title=title,
            quality=quality,
            codec=codec,
            audio=audio,
            ext=ext.lstrip("."),
        )
    except (KeyError, IndexError) as e:
        LOGGER.error(f"AutoRename: Bad template '{template}', error: {e}. Falling back to original.")
        return original_filename

    # Clean up double spaces / empty brackets left by missing metadata e.g. "[]" or "  "
    new_name = re.sub(r"\[\s*\]", "", new_name)
    new_name = re.sub(r"\(\s*\)", "", new_name)
    new_name = re.sub(r"\s{2,}", " ", new_name).strip()

    # Sanitize filesystem-unsafe characters
    new_name = re.sub(r'[\\/:*?"<>|]', "", new_name)

    if not new_name.strip():
        return original_filename

    # Re-attach extension if template didn't include {ext}
    if not new_name.lower().endswith(ext.lower()):
        new_name = f"{new_name}{ext}"

    return new_name


async def auto_rename_leech(up_dir, enabled, template):
    """
    Main entry point called from tasks_listener.

    enabled : bool — user's Auto-Rename toggle
    template: str  — e.g. "{title} [{quality}] [{audio}]"

    Renames every file in up_dir IN PLACE (not just videos — applies to all files,
    per user decision). Returns up_dir.
    """
    if not enabled or not template.strip():
        return up_dir

    all_files = []
    for root, _, files in os.walk(up_dir):
        for f in files:
            if not f.startswith("Sample_"):  # don't rename generated sample clips
                all_files.append(os.path.join(root, f))

    if not all_files:
        return up_dir

    LOGGER.info(f"AutoRename: Processing {len(all_files)} file(s) | Template: {template}")

    for file_path in all_files:
        try:
            dir_path, original_filename = os.path.split(file_path)
            new_filename = build_filename(template, original_filename, file_path)

            if new_filename == original_filename:
                continue

            new_path = os.path.join(dir_path, new_filename)

            # Avoid collisions if two files would rename to the same name
            if os.path.exists(new_path) and new_path != file_path:
                base, ext = os.path.splitext(new_filename)
                counter = 1
                while os.path.exists(new_path):
                    new_path = os.path.join(dir_path, f"{base} ({counter}){ext}")
                    counter += 1

            os.rename(file_path, new_path)
            LOGGER.info(f"AutoRename: '{original_filename}' -> '{os.path.basename(new_path)}'")

        except Exception as e:
            LOGGER.error(f"AutoRename: Failed for '{file_path}': {e}")
            continue

    return up_dir

