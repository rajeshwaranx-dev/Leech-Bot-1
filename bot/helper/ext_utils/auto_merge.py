#!/usr/bin/env python3
import re
import os
import subprocess
from pathlib import Path
from natsort import natsorted
from bot import LOGGER


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".flv"}


def detect_series_groups(file_list):
    """Group video files by series name using episode pattern detection."""
    groups = {}
    pattern = re.compile(
        r"(.*?)[.\s_\-]*(?:s\d+e\d+|ep?\s*\d+|episode\s*\d+|part\s*\d+|\d+of\d+|\b\d{2,3}\b)",
        re.IGNORECASE,
    )
    for file in file_list:
        name = Path(file).stem
        match = pattern.match(name)
        series_key = match.group(1).strip(" ._-") if match else name
        if not series_key:
            series_key = name
        groups.setdefault(series_key, []).append(file)

    # Only return groups with 2+ files (actual series)
    return {k: natsorted(v) for k, v in groups.items() if len(v) > 1}


def create_concat_file(file_list, concat_path):
    """Write FFmpeg concat list file."""
    with open(concat_path, "w") as f:
        for file in file_list:
            escaped = file.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    return concat_path


def merge_files_ffmpeg(file_list, output_path):
    """Merge video files using FFmpeg concat demuxer (no re-encoding)."""
    concat_path = output_path + "_concat.txt"
    create_concat_file(file_list, concat_path)

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_path,
        "-map", "0",
        "-c", "copy",
        output_path,
    ]

    LOGGER.info(f"FFmpeg Merge CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    try:
        os.remove(concat_path)
    except Exception:
        pass

    if result.returncode != 0:
        LOGGER.error(f"FFmpeg Merge Error: {result.stderr}")
        raise Exception(f"FFmpeg merge failed: {result.stderr[-200:]}")

    LOGGER.info(f"FFmpeg Merge Success: {output_path}")
    return output_path


async def auto_merge_leech(up_dir, merge_mode):
    """
    Main entry point called from tasks_listener before TgUploader.

    merge_mode:
        'off'   -> do nothing, return original up_dir unchanged
        'merge' -> merge episodes, delete originals, upload only merged
        'both'  -> merge episodes, keep originals, upload both

    Returns up_dir (same path, contents modified in-place).
    """
    if merge_mode == "off":
        return up_dir

    # Collect all video files recursively
    all_files = []
    for root, _, files in os.walk(up_dir):
        for f in files:
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                all_files.append(os.path.join(root, f))

    if len(all_files) < 2:
        LOGGER.info("Auto Merge: Less than 2 video files found, skipping merge.")
        return up_dir

    groups = detect_series_groups(all_files)

    if not groups:
        LOGGER.info("Auto Merge: No series groups detected, skipping merge.")
        return up_dir

    LOGGER.info(f"Auto Merge: Detected {len(groups)} series group(s): {list(groups.keys())}")

    for series_name, episodes in groups.items():
        # Use .mkv as output container (most compatible)
        safe_name = re.sub(r'[<>:"/\\|?*]', "_", series_name).strip()
        output_path = os.path.join(up_dir, f"{safe_name}_merged.mkv")

        LOGGER.info(f"Auto Merge: Merging '{series_name}' ({len(episodes)} episodes) -> {output_path}")

        try:
            merge_files_ffmpeg(episodes, output_path)

            # If mode is 'merge', delete original episode files
            if merge_mode == "merge":
                for ep in episodes:
                    try:
                        os.remove(ep)
                        LOGGER.info(f"Auto Merge: Deleted original: {ep}")
                    except Exception as e:
                        LOGGER.warning(f"Auto Merge: Could not delete {ep}: {e}")

        except Exception as e:
            LOGGER.error(f"Auto Merge: Failed for '{series_name}': {e}")
            # Don't crash the upload — just continue with originals
            continue

    return up_dir
