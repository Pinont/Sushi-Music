import asyncio
import logging
import re
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("sushimusic.ytdl")

# ── yt-dlp base options ────────────────────────────────────────────────────────
_BASE_OPTS = {
    # Prefer highest quality audio: opus 160kbps+ or m4a 128kbps+, fallback to bestaudio
    "format": "bestaudio[ext=webm][abr>=160]/bestaudio[ext=m4a][abr>=128]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": False,
    "postprocessors": [],   # no post-processing — stream directly
}

# ── Stream URL (no download) ───────────────────────────────────────────────────
async def stream_url_for_query(query: str) -> tuple[str | None, dict | None]:
    """
    Return (stream_url, info_dict) for the best audio match on YouTube Music.
    No file is written to disk. Returns (None, None) on failure.
    """
    try:
        import yt_dlp
    except ImportError:
        log.error("yt-dlp not installed.")
        return None, None

    is_url = query.startswith("http://") or query.startswith("https://")

    ydl_opts = {
        **_BASE_OPTS,
        # For plain search terms use ytsearch1 (reliable) on YouTube
        # For YouTube Music URLs pass them directly
        "default_search": "ytsearch1",
    }

    search_query = query if is_url else f"ytsearch1:{query}"

    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if not info:
                return None, None
            if "entries" in info:
                entries = info.get("entries") or []
                if not entries:
                    log.warning(f"No results for: {query}")
                    return None, None
                info = entries[0]
            if not info:
                return None, None
            url = info.get("url")
            return url, info

    try:
        url, info = await loop.run_in_executor(None, _extract)
        return url, info
    except Exception as e:
        log.error(f"yt-dlp extract failed: {e}")
        return None, None


# ── Metadata only (no download, no stream URL) ────────────────────────────────
async def get_info(query: str) -> dict | None:
    """
    Get metadata (title, artist, album, duration, thumbnail) without downloading.
    Returns a dict or None on failure.
    """
    try:
        import yt_dlp
    except ImportError:
        return None

    is_url = query.startswith("http://") or query.startswith("https://")
    search_query = query if is_url else f"ytsearch1:{query}"

    ydl_opts = {
        **_BASE_OPTS,
        "default_search": "ytsearch1",
        "skip_download": True,
    }

    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if not info:
                return None
            if "entries" in info:
                entries = info.get("entries") or []
                if not entries:
                    return None
                info = entries[0]
            if not info:
                return None
            raw_title = info.get("track") or info.get("title", "")
            return {
                "title": _clean_title(raw_title),
                "artist": info.get("artist") or info.get("uploader") or "YouTube",
                "album": info.get("album") or "YouTube",
                "duration": int(info.get("duration") or 0),
                "thumbnail": info.get("thumbnail"),
                "webpage_url": info.get("webpage_url"),
            }

    try:
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        log.error(f"get_info failed: {e}")
        return None


# ── Search — return multiple candidates ───────────────────────────────────────
async def search_tracks(query: str, limit: int = 5) -> list[dict]:
    """
    Search YouTube for `limit` tracks matching query.
    Returns list of info dicts: {title, artist, album, duration, thumbnail, webpage_url, _url}
    No files are downloaded.
    """
    try:
        import yt_dlp
    except ImportError:
        return []

    ydl_opts = {
        **_BASE_OPTS,
        "default_search": f"ytsearch{limit}",
        "skip_download": True,
    }

    search_query = f"ytsearch{limit}:{query}"

    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if not info:
                return []
            entries = info.get("entries") or []
            results = []
            for e in entries:
                if not e:
                    continue
                raw_title = e.get("track") or e.get("title", "")
                results.append({
                    "title": _clean_title(raw_title),
                    "artist": e.get("artist") or e.get("uploader") or "YouTube",
                    "album": e.get("album") or "YouTube",
                    "duration": int(e.get("duration") or 0),
                    "thumbnail": e.get("thumbnail"),
                    "webpage_url": e.get("webpage_url") or e.get("url"),
                })
            return results

    try:
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        log.error(f"search_tracks failed: {e}")
        return []


# ── Playlist / Radio ──────────────────────────────────────────────────────────
async def get_playlist_tracks(url: str, limit: int = 25) -> list[dict]:
    """
    Extract tracks from a YouTube playlist or Radio mix.
    Returns list of {title, artist, album, duration, webpage_url} dicts.
    No files downloaded.
    """
    try:
        import yt_dlp
    except ImportError:
        return []

    ydl_flat = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlistend": limit,
    }

    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(ydl_flat) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return []
            entries = info.get("entries") or []
            results = []
            for e in entries[:limit]:
                if not e:
                    continue
                vid_id = e.get("id") or ""
                vid_url = (
                    f"https://www.youtube.com/watch?v={vid_id}"
                    if vid_id
                    else e.get("url", "")
                )
                raw_title = e.get("track") or e.get("title", "")
                results.append({
                    "title": _clean_title(raw_title),
                    "artist": e.get("artist") or e.get("uploader") or "YouTube",
                    "album": e.get("album") or "YouTube",
                    "duration": int(e.get("duration") or 0),
                    "webpage_url": vid_url,
                })
            return results

    try:
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        log.error(f"get_playlist_tracks failed: {e}")
        return []


# ── URL helpers ───────────────────────────────────────────────────────────────
def _is_radio_url(url: str) -> bool:
    if not url.startswith("http"):
        return False
    qs = parse_qs(urlparse(url).query)
    list_id = qs.get("list", [""])[0]
    return list_id.startswith("RD") or list_id.startswith("RDAMVM")


def _is_playlist_url(url: str) -> bool:
    if not url.startswith("http"):
        return False
    qs = parse_qs(urlparse(url).query)
    return bool(qs.get("list", [""])[0])


# ── Title cleaner ─────────────────────────────────────────────────────────────
_MV_SUFFIXES = re.compile(
    r"[\[\(]?\s("
    r"official\s(music\s)?video|official\saudio|music\svideo|"
    r"official\smv|mv|lyric\svideo|lyrics?|audio|visualizer|"
    r"performance\svideo|official\sperformance|teaser|m/v"
    r")\s[\]\)]?",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    title = _MV_SUFFIXES.sub("", title)
    title = re.sub(r"[\s\-\|:]+$", "", title).strip()
    return title