# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Book metadata lookup for screen 1. Provider order:

1. iTunes (entity=audiobook) — the only one with real audiobook editions,
   so its years/covers describe the audio release, not a print edition
2. Google Books
3. Open Library

All free, no API key, stdlib urllib only.

Deliberately fetches ONLY title / author / year / edition text / cover images.
Never chapter names or timings — tracks stay exactly as ripped.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
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


# iTunes allows 20 requests/minute/IP; keep a polite gap between searches.
_ITUNES_MIN_INTERVAL = 3.1
_last_itunes_call = 0.0


def search_itunes(title: str, author: str) -> list[dict]:
    global _last_itunes_call
    wait = _ITUNES_MIN_INTERVAL - (time.monotonic() - _last_itunes_call)
    if wait > 0:
        time.sleep(wait)
    _last_itunes_call = time.monotonic()

    term = f"{title} {author}".strip()
    url = ("https://itunes.apple.com/search?"
           + urllib.parse.urlencode({"term": term, "entity": "audiobook",
                                     "country": "us", "limit": MAX_RESULTS}))
    data = _get_json(url)
    results = []
    for item in (data.get("results") or [])[:MAX_RESULTS]:  # iTunes ignores limit=
        name = (item.get("collectionName") or "").strip()
        # iTunes appends "(Unabridged)"/"(Abridged)": show it as the edition,
        # strip it from the title that goes into the form / folder name.
        m = re.search(r"\s*\((unabridged|abridged)\)\s*$", name, re.IGNORECASE)
        edition = m.group(1).title() if m else ""
        clean_title = name[:m.start()].strip() if m else name
        art = item.get("artworkUrl100") or ""
        results.append({
            "title": clean_title,
            "author": item.get("artistName") or "",
            "year": _year_of(item.get("releaseDate")),
            "edition": edition,
            "thumb_url": art,
            # mzstatic artwork URLs end in "<size>x<size>bb.jpg"; asking for
            # 600x600bb serves a 600px render of the same asset
            "cover_url": re.sub(r"100x100bb", "600x600bb", art) if art else "",
            "source": "iTunes",
        })
    return results


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
    """iTunes -> Google Books -> Open Library. A provider that errors (429
    included) or finds nothing falls through to the next; raises only if
    every provider fails outright."""
    first_error = None
    for provider in (search_itunes, search_google_books, search_open_library):
        try:
            results = provider(title, author)
            if results:
                return results
        except Exception as exc:  # noqa: BLE001 — fall through to next provider
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error
    return []


def lookup_narrator(title: str, author: str, prefer_edition: str = "") -> dict:
    """Narrator (and format) for an audiobook via Audnexus (audnex.us), a
    read-only cache of the Audible catalogue. Audnexus is keyed by ASIN only,
    so the ASIN is resolved first through the public Audible catalog search.
    Misses and errors return empty strings -- the caller types it instead."""
    # Audible often titles an edition without the subtitle iTunes uses
    # ("The Lost World" vs "The Lost World: A Novel"), so search both forms.
    queries = [title]
    if ":" in title:
        queries.append(title.split(":", 1)[0].strip())
    products, seen_asins = [], set()
    for q in queries:
        params = {"title": q, "num_results": "5"}
        if author:
            params["author"] = author
        try:
            found = _get_json("https://api.audible.com/1.0/catalog/products?"
                              + urllib.parse.urlencode(params))
        except Exception:  # noqa: BLE001 -- quiet failure by design
            continue
        for p in found.get("products") or []:
            if p.get("asin") and p["asin"] not in seen_asins:
                seen_asins.add(p["asin"])
                products.append(p)
    if not products:
        return {"narrator": "", "edition": ""}
    # The user may have picked a specific edition (iTunes marks abridgement);
    # prefer the Audible product whose format matches it, since abridged and
    # unabridged recordings usually have different narrators.
    prefer = prefer_edition.strip().lower()
    fallback = None
    for product in products[:8]:
        asin = product.get("asin")
        if not asin:
            continue
        try:
            book = _get_json(f"https://api.audnex.us/books/{asin}")
        except Exception:  # noqa: BLE001
            continue
        narrator = ", ".join(n.get("name", "") for n in book.get("narrators") or []
                             if n.get("name"))
        fmt = (book.get("formatType") or "").lower()
        edition = {"unabridged": "Unabridged", "abridged": "Abridged"}.get(fmt, "")
        if not narrator and not edition:
            continue
        if prefer in ("unabridged", "abridged") and fmt == prefer:
            return {"narrator": narrator, "edition": edition}
        if fallback is None:
            fallback = {"narrator": narrator, "edition": edition}
        if prefer not in ("unabridged", "abridged"):
            return fallback
    return fallback or {"narrator": "", "edition": ""}


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
