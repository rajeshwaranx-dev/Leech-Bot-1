"""Load the extra (TMV / Auto Post) settings from config.env at startup."""
import os
import re

from bot import LOGGER, config_dict

BOOLS = ["AUTO_TMV", "TMV_CAM_PREDVD", "AUTO_POST", "BACKUP_POST",
         "LINKS_BACKUP", "QB_AUTO_POST", "F2L_POST", "AUTO_LEECH"]
INTS = ["TMV_SIZE_LIMIT", "TMV_SCRAPE_INTERVAL", "POST_TIMEOUT"]
IDS = ["TMV_CHAT", "POSTER_CHANNEL", "AUTO_LEECH_CHANNEL",
       "BACKUP_CHANNEL", "LINKS_BACKUP_CHANNEL"]
STRS = ["TMV_LANGUAGE", "TMV_URL", "TMDB_API_KEY", "F2L_CAPTION",
        "POSTER_WATERMARK", "STICKER_1", "STICKER_2", "DTMV_SIZE_MODE"]


def apply_extra_env():
    try:
        from dotenv import load_dotenv
        load_dotenv("config.env", override=False)
    except Exception:
        pass
    loaded = []

    def get(name):
        v = os.environ.get(name)
        if v is None:
            return None
        v = v.strip().strip('"').strip("'").strip()
        return v or None

    for k in BOOLS:
        v = get(k)
        if v is not None:
            config_dict[k] = v.lower() == "true"
            loaded.append(k)
    for k in INTS:
        v = get(k)
        if v is not None and v.isdigit():
            config_dict[k] = int(v)
            loaded.append(k)
    for k in IDS:
        v = get(k)
        if v is not None and re.fullmatch(r"-?\d+", v):
            config_dict[k] = int(v)
            loaded.append(k)
    for k in STRS:
        v = get(k)
        if v is not None:
            config_dict[k] = v
            loaded.append(k)
    LOGGER.info(f"StartupEnv: loaded {len(loaded)} settings: {', '.join(loaded)}")
