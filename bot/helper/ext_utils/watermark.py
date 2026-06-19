#!/usr/bin/env python3
import os
import subprocess
from pathlib import Path
from bot import LOGGER

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".flv"}


def add_watermark_clip(file_path, output_path, watermark_text, duration=10):
    """
    Burn watermark text onto the video, visible only from 00:00 to `duration` seconds,
    positioned at bottom-center (subtitle position).
    """
    # Escape special characters for FFmpeg drawtext filter
    safe_text = (
        watermark_text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )

    drawtext = (
        f"drawtext=text='{safe_text}':"
        f"fontcolor=white:fontsize=24:"
        f"box=1:boxcolor=black@0.5:boxborderw=8:"
        f"x=(w-text_w)/2:y=h-th-40:"
        f"enable='between(t,0,{duration})'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", file_path,
        "-vf", drawtext,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        output_path,
    ]

    LOGGER.info(f"Watermark CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not os.path.exists(output_path):
        LOGGER.error(f"Watermark Error: {result.stderr[-300:]}")
        raise Exception(f"Watermark generation failed: {result.stderr[-200:]}")

    LOGGER.info(f"Watermark Success: {output_path}")
    return output_path


async def auto_watermark_leech(up_dir, watermark_enabled, watermark_text="", duration=10):
    """
    Main entry point called from tasks_listener before TgUploader.

    watermark_enabled: bool, user's Watermark toggle
    watermark_text: str, user's configured text (e.g. channel name)
    duration: int, seconds the watermark is visible from start of video

    Re-encodes each video file IN PLACE (replaces original) with the
    watermark burned in for the first `duration` seconds only.

    Returns up_dir (contents modified in-place).
    """
    if not watermark_enabled or not watermark_text.strip():
        return up_dir

    all_files = []
    for root, _, files in os.walk(up_dir):
        for f in files:
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS and not f.startswith("Sample_"):
                all_files.append(os.path.join(root, f))

    if not all_files:
        return up_dir

    LOGGER.info(
        f"Watermark: Applying '{watermark_text}' for {duration}s to {len(all_files)} file(s)"
    )

    for file_path in all_files:
        try:
            temp_output = file_path + ".watermarked.mp4"
            add_watermark_clip(file_path, temp_output, watermark_text, duration)

            # Replace original with watermarked version
            os.remove(file_path)
            final_path = str(Path(file_path).with_suffix(".mp4"))
            os.rename(temp_output, final_path)

        except Exception as e:
            LOGGER.error(f"Watermark: Failed for '{file_path}': {e}")
            # Clean up failed temp file if it exists
            temp_output = file_path + ".watermarked.mp4"
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except Exception:
                    pass
            continue

    return up_dir

