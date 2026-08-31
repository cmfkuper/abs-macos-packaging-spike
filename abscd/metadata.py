"""Book metadata lookup for screen 1: Google Books first, Open Library as
fallback. Both free, no API key, stdlib urllib only.

Deliberately fetches ONLY title / author / year / edition text / cover images.
Never chapter names or timings — tracks stay exactly as ripped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .settings import config_dir

TIMEOUT = 8
HEADERS = {"User-Agent": "AudiobookBob/1.0 (personal audiobook CD ripper)"}
MAX_RESULTS = 8


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _get_json(url: str) -> dict:
    return json.loads(_get(url).decode("utf-8"))


def _year_of(text: str) -> str:
    m = re.search(r"\d{4}", text or "")
    return m.group(0) if m else ""


def search_google_books(title: str, author: str) -> list[dict]:
    q = f'intitle:"{title}"'
    if author:
        q += f' inauthor:"{author}"'
    url = ("https://www.googleapis.com/books/v1/volumes?q="
           + urllib.parse.quote(q) + f"&maxResults={MAX_RESULTS}&printType=books")
    data = _get_json(url)
    results = []
    for item in data.get("items") or []:
        info = item.get("volumeInfo") or {}
        links = info.get("imageLinks") or {}
        thumb = (links.get("thumbnail") or links.get("smallThumbnail") or "")
        thumb = thumb.replace("http://", "https://").replace("&edge=curl", "")
        results.append({
            "title": info.get("title") or "",
            "author": ", ".join(info.get("authors") or []),
            "year": _year_of(info.get("publishedDate")),
            "edition": info.get("subtitle") or "",
            "thumb_url": thumb,
            # zoom=2 is the largest size Google serves reliably without a key
            "cover_url": thumb.replace("zoom=1", "zoom=2") if thumb else "",
            "source": "Google Books",
        })
    return results


def search_open_library(title: str, author: str) -> list[dict]:
    params = {"title": title, "limit": str(MAX_RESULTS),
              "fields": "title,author_name,first_publish_year,cover_i"}
    if author:
        params["author"] = author
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    data = _get_json(url)
    results = []
    for doc in (data.get("docs") or [])[:MAX_RESULTS]:
        cover_id = doc.get("cover_i")
        results.append({
            "title": doc.get("title") or "",
            "author": ", ".join(doc.get("author_name") or []),
            "year": str(doc.get("first_publish_year") or ""),
            "edition": "",
            "thumb_url": f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
                         if cover_id else "",
            "cover_url": f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
                         if cover_id else "",
            "source": "Open Library",
        })
    return results


def search(title: str, author: str) -> list[dict]:
    """Google Books first; Open Library if Google errors or finds nothing.
    Raises only if BOTH providers fail outright."""
    google_error = None
    try:
        results = search_google_books(title, author)
        if results:
            return results
    except Exception as exc:  # noqa: BLE001 — provider errors fall through
        google_error = exc
    try:
        return search_open_library(title, author)
    except Exception:
        if google_error is not None:
            raise google_error
        raise


def _data_uri(raw: bytes) -> str:
    mime = "image/png" if raw[:8].startswith(b"\x89PNG") else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def fetch_thumbs(results: list[dict]) -> None:
    """Download each result's thumbnail and attach it as a data URI ('thumb'),
    so the UI layer itself never makes a network request."""
    def one(r):
        if not r.get("thumb_url"):
            r["thumb"] = ""
            return
        try:
            r["thumb"] = _data_uri(_get(r["thumb_url"]))
        except Exception:  # noqa: BLE001 — a missing thumb is not an error
            r["thumb"] = ""
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, results))


def covers_dir() -> Path:
    d = config_dir() / "covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_cover(result: dict) -> Path | None:
    """Download the best available cover once into the local cache.
    Returns the cached file path, or None if nothing could be fetched."""
    for url in (result.get("cover_url"), result.get("thumb_url")):
        if not url:
            continue
        dest = covers_dir() / (hashlib.sha1(url.encode()).hexdigest() + ".img")
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        try:
            raw = _get(url)
            if len(raw) < 1000:  # provider placeholder / error stub
                continue
            dest.write_bytes(raw)
            return dest
        except Exception:  # noqa: BLE001 — fall through to the next size
            continue
    return None
