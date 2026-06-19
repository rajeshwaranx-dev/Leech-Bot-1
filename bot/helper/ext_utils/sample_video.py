#!/usr/bin/env python3
import os
import subprocess
from pathlib import Path
from bot import LOGGER

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".flv"}


def get_video_duration(file_path):
    """Return duration of video in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def create_sample_clip(file_path, output_path, sample_duration=30):
    """
    Extract a clip from the MIDDLE of the video using FFmpeg.
    sample_duration: length of sample clip in seconds.
    """
    total_duration = get_video_duration(file_path)
    if not total_duration or total_duration <= sample_duration:
        LOGGER.warning(f"Sample Video: '{file_path}' too short or duration unknown, skipping.")
        return None

    start_time = max(0, (total_duration / 2) - (sample_duration / 2))

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-i", file_path,
        "-t", str(sample_duration),
        "-map", "0",
        "-c", "copy",
        output_path,
    ]

    LOGGER.info(f"Sample Video CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not os.path.exists(output_path):
        LOGGER.error(f"Sample Video Error (copy mode): {result.stderr[-300:]}")
        # Fallback: some streams can't be stream-copied at arbitrary start points, re-encode instead
        cmd_reencode = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", file_path,
            "-t", str(sample_duration),
            "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac",
            output_path,
        ]
        LOGGER.info(f"Sample Video Retry (re-encode) CMD: {' '.join(cmd_reencode)}")
        result2 = subprocess.run(cmd_reencode, capture_output=True, text=True)
        if result2.returncode != 0 or not os.path.exists(output_path):
            LOGGER.error(f"Sample Video Error (re-encode): {result2.stderr[-300:]}")
            raise Exception("Sample video generation failed in both copy and re-encode modes.")

    LOGGER.info(f"Sample Video Success: {output_path}")
    return output_path


async def auto_sample_leech(up_dir, sample_enabled, sample_duration=30):
    """
    Main entry point called from tasks_listener before TgUploader.

    sample_enabled: bool, user's Sample Video toggle
    sample_duration: int, seconds, user's configured sample length

    For every video file found, generates a 'Sample_<name>.ext' clip
    in the same directory. Originals are NEVER deleted.

    Returns up_dir (contents modified in-place, sample files added).
    """
    if not sample_enabled:
        return up_dir

    all_files = []
    for root, _, files in os.walk(up_dir):
        for f in files:
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS and not f.startswith("Sample_"):
                all_files.append(os.path.join(root, f))

    if not all_files:
        return up_dir

    LOGGER.info(f"Sample Video: Generating samples for {len(all_files)} file(s), duration={sample_duration}s")

    for file_path in all_files:
        try:
            file_dir = os.path.dirname(file_path)
            file_name = os.path.basename(file_path)
            output_path = os.path.join(file_dir, f"Sample_{file_name}")

            create_sample_clip(file_path, output_path, sample_duration)

        except Exception as e:
            LOGGER.error(f"Sample Video: Failed for '{file_path}': {e}")
            continue

    return up_dir
  
