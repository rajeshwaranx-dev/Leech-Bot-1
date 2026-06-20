#!/usr/bin/env python3
"""
Attachment Feature — embeds a user-provided cover image into leeched
video files.

Behavior by container format:
  .mkv          -> true MKV attachment stream (cover.jpg), shown in
                   VLC/MPV file info, fully reliable.
  .mp4/.m4v     -> embedded thumbnail stream (like audio album art),
                   works in most modern players, not guaranteed in 100%
                   of file managers.
  other formats -> skipped silently (no native cover/attachment support).

This is independent from the existing Leech Thumbnail setting — a user
can have both set to different images, or only one, or neither.
"""

import os
import subprocess
from pathlib import Path
import requests
from bot import LOGGER

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".flv"}
MKV_EXTENSIONS = {".mkv"}
MP4_THUMB_EXTENSIONS = {".mp4", ".m4v"}


def download_attachment_image(url, dest_path, timeout=20):
    """Download the user's attachment image URL to a local temp file."""
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            LOGGER.warning(f"Attachment: URL did not return an image (Content-Type: {content_type})")
            return None

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return dest_path
    except Exception as e:
        LOGGER.error(f"Attachment: Failed to download image from URL: {e}")
        return None


def embed_mkv_attachment(file_path, image_path, output_path):
    """
    Embed image as a true MKV attachment (cover.jpg), stream-copied,
    no re-encoding of video/audio — fast and lossless.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", file_path,
        "-attach", image_path,
        "-metadata:s:t", "mimetype=image/jpeg",
        "-metadata:s:t", "filename=cover.jpg",
        "-map", "0",
        "-c", "copy",
        output_path,
    ]

    LOGGER.info(f"Attachment (MKV) CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not os.path.exists(output_path):
        LOGGER.error(f"Attachment (MKV) Error: {result.stderr[-300:]}")
        raise Exception(f"MKV attachment embed failed: {result.stderr[-200:]}")

    return output_path


def embed_mp4_thumbnail(file_path, image_path, output_path):
    """
    Embed image as an MP4 thumbnail stream (like audio cover art).
    Stream-copied, no re-encoding.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", file_path,
        "-i", image_path,
        "-map", "0",
        "-map", "1",
        "-c", "copy",
        "-c:v:1", "mjpeg",
        "-disposition:v:1", "attached_pic",
        output_path,
    ]

    LOGGER.info(f"Attachment (MP4) CMD: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0 or not os.path.exists(output_path):
        LOGGER.error(f"Attachment (MP4) Error: {result.stderr[-300:]}")
        raise Exception(f"MP4 thumbnail embed failed: {result.stderr[-200:]}")

    return output_path


async def auto_attachment_leech(up_dir, attachment_enabled, attachment_url=""):
    """
    Main entry point called from tasks_listener before TgUploader.

    attachment_enabled: bool, user's Attachment toggle
    attachment_url: str, direct URL to the cover image

    For .mkv files: embeds a true attachment stream.
    For .mp4/.m4v files: embeds a thumbnail stream.
    For all other formats: skipped silently.

    Returns up_dir (files modified in-place, replacing originals).
    """
    if not attachment_enabled or not attachment_url.strip():
        return up_dir

    all_files = []
    for root, _, files in os.walk(up_dir):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in VIDEO_EXTENSIONS and not f.startswith("Sample_"):
                all_files.append(os.path.join(root, f))

    if not all_files:
        return up_dir

    # Download the attachment image once, reuse for all files in this batch
    temp_image = os.path.join(up_dir, "_attachment_cover.jpg")
    downloaded = download_attachment_image(attachment_url, temp_image)

    if not downloaded:
        LOGGER.warning("Attachment: Could not download cover image, skipping attachment step.")
        return up_dir

    LOGGER.info(f"Attachment: Applying cover to {len(all_files)} file(s)")

    for file_path in all_files:
        ext = Path(file_path).suffix.lower()

        try:
            if ext in MKV_EXTENSIONS:
                temp_output = file_path + ".attached.mkv"
                embed_mkv_attachment(file_path, downloaded, temp_output)
                os.remove(file_path)
                os.rename(temp_output, file_path)
                LOGGER.info(f"Attachment: Embedded (MKV) into {file_path}")

            elif ext in MP4_THUMB_EXTENSIONS:
                temp_output = file_path + ".attached.mp4"
                embed_mp4_thumbnail(file_path, downloaded, temp_output)
                os.remove(file_path)
                os.rename(temp_output, file_path)
                LOGGER.info(f"Attachment: Embedded (MP4 thumb) into {file_path}")

            else:
                LOGGER.info(f"Attachment: Skipped '{file_path}' (format not supported)")
                continue

        except Exception as e:
            LOGGER.error(f"Attachment: Failed for '{file_path}': {e}")
            # Clean up failed temp file if it exists
            for suffix in [".attached.mkv", ".attached.mp4"]:
                temp_output = file_path + suffix
                if os.path.exists(temp_output):
                    try:
                        os.remove(temp_output)
                    except Exception:
                        pass
            continue

    # Clean up the downloaded cover image temp file
    try:
        if os.path.exists(temp_image):
            os.remove(temp_image)
    except Exception:
        pass

    return up_dir

