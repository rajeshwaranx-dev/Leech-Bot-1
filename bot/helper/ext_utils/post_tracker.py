"""Tracks all torrents of one movie (topic) so a post can be made when all are done."""
import json
import time
from bot import LOGGER, config_dict

STORE = "post_tracker.json"
_movies = {}
try:
    with open(STORE) as _f:
        _movies = json.load(_f)
    _cut = time.time() - 7 * 86400
    _movies = {k: v for k, v in _movies.items() if v.get("updated", 0) > _cut}
except Exception:
    _movies = {}


def _save():
    try:
        with open(STORE, "w") as f:
            json.dump(_movies, f)
    except Exception as e:
        LOGGER.error(f"PostTracker: save failed: {e}")


def start_movie(topic_url, title):
    """Returns a movie key, or None when Auto Post is OFF."""
    if not config_dict.get("AUTO_POST", False) or not topic_url:
        return None
    m = _movies.get(topic_url)
    if m is None:
        m = {
            "title": title or "",
            "topic_url": topic_url,
            "launched": 0,
            "done": 0,
            "failed": 0,
            "items": [],
            "created": time.time(),
            "updated": time.time(),
            "posted": False,
        }
        _movies[topic_url] = m
    elif title and not m.get("title"):
        m["title"] = title
    return topic_url


def tag_listener(listener, key, torrent_name="", size_mb=0):
    if not key or key not in _movies:
        return
    m = _movies[key]
    if m.get("posted"):
        m.update({"posted": False, "post_tries": 0, "launched": 0,
                  "done": 0, "failed": 0, "items": []})
    elif m.get("post_tries", 0) >= 3:
        m.update({"post_tries": 0, "alerted": False})
    listener.post_key = key
    listener.post_tname = torrent_name
    listener.post_size_mb = size_mb
    m["launched"] += 1
    m["updated"] = time.time()
    _save()
    LOGGER.info(f"PostTracker: launched {m['launched']} for: {m['title'][:50]}")


def record_done(key, name, size, links, tname="", size_mb=0):
    m = _movies.get(key)
    if not m:
        return
    m["done"] += 1
    m["updated"] = time.time()
    m["items"].append(
        {
            "name": name,
            "size": size,
            "links": links,
            "torrent_name": tname,
            "size_mb": size_mb,
        }
    )
    _save()
    LOGGER.info(
        f"PostTracker: {m['done']}/{m['launched']} done "
        f"({m['failed']} failed) for: {m['title'][:50]}"
    )


def record_fail(key, tname=""):
    m = _movies.get(key)
    if not m:
        return
    m["failed"] += 1
    m["updated"] = time.time()
    _save()
    LOGGER.info(
        f"PostTracker: FAILED {tname[:40]} - {m['done']}/{m['launched']} done "
        f"({m['failed']} failed) for: {m['title'][:50]}"
    )


def is_complete(key):
    m = _movies.get(key)
    return bool(m) and m["launched"] > 0 and (m["done"] + m["failed"]) >= m["launched"]


def get_movie(key):
    return _movies.get(key)


def all_movies():
    return _movies


def save():
    _save()
