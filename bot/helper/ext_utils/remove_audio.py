#!/usr/bin/env python3
import os
import json
import subprocess
from pathlib import Path
from bot import LOGGER

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".flv"}

# Common language code aliases so users can type naturally
LANG_ALIASES = {
    "tam": "tam", "tamil": "tam",
    "hin": "hin", "hindi": "hin",
    "tel": "tel", "telugu": "tel",
    "mal": "mal", "malayalam": "mal",
    "kan": "kan", "kannada": "kan",
    "eng": "eng", "english": "eng",
    "jpn": "jpn", "japanese": "jpn",
    "chi": "chi", "zho": "zho", "chinese": "chi",
    "kor": "kor", "korean": "kor",
    "ara": "ara", "arabic": "ara",
    "fre": "fre", "fra": "fra", "french": "fre",
    "ger": "ger", "deu": "deu", "german": "ger",
    "spa": "spa", "spanish": "spa",
    "por": "por", "portuguese": "por",
    "rus": "rus", "russian": "rus",
    "ita": "ita", "italian": "ita",
}


def get_audio_tracks(file_path):
    """
    Run ffprobe and return list of audio stream dicts:
    [{"index": 1, "lang": "tam", "codec": "ac3", "channels": 2, "title": "..."}, ...]
    stream index = position among ALL streams (video=0, audio=1,2,3...)
    audio_index = position among audio-only streams (0,1,2...)
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "a",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            LOGGER.error(f"RemoveAudio ffprobe error: {result.stderr[-200:]}")
            return []
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        tracks = []
        for audio_idx, s in enumerate(streams):
            lang = (s.get("tags") or {}).get("language", "und").lower().strip()
            title = (s.get("tags") or {}).get("title", "")
            tracks.append({
                "stream_index": s.get("index", -1),
                "audio_index": audio_idx,
                "lang": lang,
                "codec": s.get("codec_name", "unknown"),
                "channels": s.get("channels", 0),
                "title": title,
            })
        return tracks
    except Exception as e:
        LOGGER.error(f"RemoveAudio ffprobe exception: {e}")
        return []


def remove_audio_tracks(file_path, output_path, lang_codes):
    """
    Remove audio tracks whose language tag matches any code in lang_codes.
    Uses stream copy — no re-encoding. Fast regardless of file size.

    lang_codes: list of normalised 3-letter codes, e.g. ["hin", "eng"]
    Returns output_path on success, raises Exception on failure.
    """
    tracks = get_audio_tracks(file_path)
    if not tracks:
        LOGGER.warning(f"RemoveAudio: No audio tracks found in '{file_path}', skipping.")
        return None  # Caller treats None as "nothing to do"

    # Decide which audio streams to KEEP
    keep_audio = []
    removed_langs = []
    for t in tracks:
        if t["lang"] in lang_codes or t["lang"] == "und" and "und" in lang_codes:
            removed_langs.append(f"Track {t['audio_index']} [{t['lang']}] {t['codec']}")
        else:
            keep_audio.append(t["audio_index"])

    if not removed_langs:
        LOGGER.info(f"RemoveAudio: No matching language tracks found in '{Path(file_path).name}'. Codes checked: {lang_codes}")
        return None  # Nothing to remove

    if not keep_audio:
        LOGGER.warning(
            f"RemoveAudio: Removing ALL audio tracks from '{Path(file_path).name}' "
            f"(all tracks matched the remove list). File will have no audio."
        )

    LOGGER.info(f"RemoveAudio: Removing from '{Path(file_path).name}': {removed_langs}")

    # Build ffmpeg map args
    # Start with: map all video + subtitle + other non-audio streams
    cmd = [
        "ffmpeg", "-y",
        "-i", file_path,
        "-map", "0:v",        # all video streams
        "-map", "0:s?",       # all subtitle streams (? = optional, won't fail if none)
        "-map", "0:t?",       # all attachments (chapter fonts etc.)
        "-map", "0:d?",       # data streams
    ]

    # Map only the audio streams we want to keep
    for idx in keep_audio:
        cmd += ["-map", f"0:a:{idx}"]

    # Copy everything — no re-encoding
    cmd += ["-c", "copy", output_path]

    LOGGER.info(f"RemoveAudio CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0 or not os.path.exists(output_path):
        LOGGER.error(f"RemoveAudio FFmpeg error: {result.stderr[-300:]}")
        raise Exception(f"RemoveAudio failed: {result.stderr[-200:]}")

    LOGGER.info(f"RemoveAudio Success: {output_path}")
    return output_path


async def auto_remove_audio_leech(up_dir, enabled, lang_input):
    """
    Main entry point called from tasks_listener before TgUploader.

    enabled   : bool — user's Remove Audio toggle
    lang_input: str  — comma-separated language codes, e.g. "hin,eng" or "hindi,tamil"

    Processes every video file in up_dir IN PLACE (replaces original).
    Returns up_dir.
    """
    if not enabled or not lang_input.strip():
        return up_dir

    # Normalise input → set of 3-letter codes
    raw_codes = [c.strip().lower() for c in lang_input.split(",") if c.strip()]
    lang_codes = set()
    for c in raw_codes:
        normalised = LANG_ALIASES.get(c, c)  # fallback: use as-is
        lang_codes.add(normalised)

    if not lang_codes:
        return up_dir

    all_files = []
    for root, _, files in os.walk(up_dir):
        for f in files:
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS and not f.startswith("Sample_"):
                all_files.append(os.path.join(root, f))

    if not all_files:
        return up_dir

    LOGGER.info(
        f"RemoveAudio: Processing {len(all_files)} file(s) | Remove langs: {lang_codes}"
    )

    for file_path in all_files:
        try:
            ext = Path(file_path).suffix.lower()
            temp_output = file_path + ".rmaudio" + ext
            result = remove_audio_tracks(file_path, temp_output, lang_codes)

            if result is None:
                # Nothing matched or no audio — skip, keep original
                if os.path.exists(temp_output):
                    os.remove(temp_output)
                continue

            # Replace original with cleaned version
            os.remove(file_path)
            os.rename(temp_output, file_path)

        except Exception as e:
            LOGGER.error(f"RemoveAudio: Failed for '{file_path}': {e}")
            temp_output = file_path + ".rmaudio" + Path(file_path).suffix.lower()
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass
            continue

    return up_dir

