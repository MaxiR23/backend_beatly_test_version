# routes/music.py
from fastapi import APIRouter, Query, Path, Body
from fastapi.responses import StreamingResponse, JSONResponse
import time
import requests
import yt_dlp
from innertube import InnerTube
import os

from services.cache_service import get_cached, set_cached
from utils.artist_parser import (
    parse_top_songs,
    parse_albums,
    parse_singles_eps,
    parse_related_artists,
)
from utils.album_parser import parse_album_info, parse_album_tracks

router = APIRouter()

# --- CONFIG ---
CACHE_TTL = 30 * 60   # metadata: 30 min
URL_TTL   = 10 * 60   # direct_url: 10 min
_cache = {}

cookies_path = os.path.join(os.path.dirname(__file__), "..", "cookies.txt")
YDL_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "quiet": True,
    "skip_download": True,
    "cookiefile": cookies_path,
    "noplaylist": True,
    "cachedir": False,
    "concurrent_fragment_downloads": 5,  # descarga fragmentos en paralelo
}
YDL = yt_dlp.YoutubeDL(YDL_OPTS)


# --- AUDIO INFO ---

def get_audio_info(video_id: str):
    """Devuelve info de yt_dlp cacheada (metadata + direct_url)."""
    now = time.time()
    cached = _cache.get(video_id)

    if cached:
        age = now - cached["ts"]
        if age < URL_TTL and cached.get("direct_url"):
            return cached
        elif age < CACHE_TTL and cached.get("info"):
            return cached

    info = YDL.extract_info(
        f"https://www.youtube.com/watch?v={video_id}",
        download=False,
    )
    direct_url = info.get("url")

    data = {
        "info": info,
        "direct_url": direct_url,
        "ts": now,
    }
    _cache[video_id] = data
    return data


def _stream_from_url(url: str) -> StreamingResponse:
    """Crea un StreamingResponse con media_type correcto."""
    r = requests.get(url, stream=True)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    content_type = r.headers.get("Content-Type", "audio/webm")
    content_length = r.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length
    return StreamingResponse(
        r.iter_content(chunk_size=1024 * 64),
        media_type=content_type,
        headers=headers,
    )


# --- AUDIO ENDPOINTS ---

@router.get("/play")
def play_song(id: str = Query(..., description="YouTube video ID")):
    """Devuelve un stream de audio para un video de YouTube."""
    try:
        data = get_audio_info(id)
        audio_url = data.get("direct_url") or data["info"]["url"]
        return _stream_from_url(audio_url)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": "yt-dlp", "detail": str(e), "id": id},
        )


@router.post("/prefetch")
def prefetch_songs(payload: dict = Body(...)):
    """Precarga info + direct_url de varias canciones."""
    raw_ids = payload.get("ids", [])
    ids = list(dict.fromkeys([str(i).strip() for i in raw_ids if i]))[:50]

    if not ids:
        return {"ok": True, "total": 0, "warmed_info": 0, "errors": 0}

    warmed_info = 0
    errors = 0
    for vid in ids:
        try:
            data = get_audio_info(vid)
            if data.get("direct_url"):
                warmed_info += 1
        except Exception:
            errors += 1

    return {
        "ok": True,
        "total": len(ids),
        "warmed_info": warmed_info,
        "errors": errors,
    }


# --- SEARCH ---

@router.get("/search")
def search_music(q: str = Query(..., description="Texto a buscar")):
    cached = get_cached(f"search:{q}")
    if cached:
        return cached

    yt = InnerTube("WEB_REMIX")
    response = yt.search(q)

    artists, songs = [], []

    tabs = (
        response.get("contents", {})
        .get("tabbedSearchResultsRenderer", {})
        .get("tabs", [])
    )

    for tab in tabs:
        section = tab.get("tabRenderer", {}).get("content", {})
        if not section:
            continue

        sections = section.get("sectionListRenderer", {}).get("contents", [])
        for sec in sections:
            card = sec.get("musicCardShelfRenderer")
            if not card or not card.get("title", {}).get("runs"):
                continue

            runs = card["title"]["runs"]

            # artista
            if "browseEndpoint" in runs[0].get("navigationEndpoint", {}):
                browse_id = runs[0]["navigationEndpoint"]["browseEndpoint"]["browseId"]
                subtitle = "".join(run.get("text", "") for run in card.get("subtitle", {}).get("runs", []))
                thumbs = (
                    card.get("thumbnail", {})
                    .get("musicThumbnailRenderer", {})
                    .get("thumbnail", {})
                    .get("thumbnails", [])
                )
                artists.append(
                    {
                        "name": runs[0]["text"],
                        "artistId": browse_id,
                        "subtitle": subtitle,
                        "thumbnails": thumbs,
                    }
                )

            # canción
            elif "watchEndpoint" in runs[0].get("navigationEndpoint", {}):
                title = runs[0]["text"]
                video_id = runs[0]["navigationEndpoint"]["watchEndpoint"]["videoId"]

                if "Official" in title or "Video" in title:
                    continue

                subtitle_runs = card.get("subtitle", {}).get("runs", [])
                artists_list, duration = [], None
                for run in subtitle_runs:
                    if "navigationEndpoint" in run and "browseEndpoint" in run["navigationEndpoint"]:
                        artists_list.append(
                            {
                                "name": run.get("text"),
                                "id": run["navigationEndpoint"]["browseEndpoint"]["browseId"],
                            }
                        )
                    elif ":" in run.get("text", ""):
                        duration = run["text"]

                thumbs = (
                    card.get("thumbnail", {})
                    .get("musicThumbnailRenderer", {})
                    .get("thumbnail", {})
                    .get("thumbnails", [])
                )
                songs.append(
                    {
                        "title": title,
                        "videoId": video_id,
                        "artists": artists_list,
                        "duration": duration,
                        "thumbnails": thumbs,
                    }
                )

    result = {"query": q, "artists": artists, "songs": songs}
    set_cached(f"search:{q}", result)
    return result


# --- ARTIST ---

def _artist_payload(artist_id: str):
    yt = InnerTube("WEB_REMIX")
    response = yt.browse(artist_id)

    header = response.get("header", {}).get("musicImmersiveHeaderRenderer", {})
    name = header.get("title", {}).get("runs", [{}])[0].get("text")
    desc = "".join(run.get("text", "") for run in header.get("description", {}).get("runs", []))
    thumbs = (
        header.get("thumbnail", {})
        .get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails", [])
    )
    listeners = header.get("monthlyListenerCount", {}).get("runs", [{}])[0].get("text")

    contents = (
        response.get("contents", {})
        .get("singleColumnBrowseResultsRenderer", {})
        .get("tabs", [])[0]
        .get("tabRenderer", {})
        .get("content", {})
        .get("sectionListRenderer", {})
        .get("contents", [])
    )

    return {
        "header": {
            "name": name,
            "description": desc,
            "thumbnails": thumbs,
            "monthlyListeners": listeners,
        },
        "topSongs": parse_top_songs(contents[0]) if len(contents) > 0 else [],
        "albums": parse_albums(contents[1]) if len(contents) > 1 else [],
        "singles_eps": parse_singles_eps(contents[2]) if len(contents) > 2 else [],
        "related": parse_related_artists(contents[7]) if len(contents) > 7 else [],
    }


@router.get("/artist")
def get_artist_q(id: str = Query(...)):
    return _artist_payload(id)


@router.get("/artist/{id}")
def get_artist_p(id: str = Path(...)):
    return _artist_payload(id)


# --- ALBUM ---

def _album_payload(album_id: str):
    yt = InnerTube("WEB_REMIX")
    response = yt.browse(album_id)
    payload = {
        "id": album_id,
        "info": parse_album_info(response),
        "tracks": parse_album_tracks(response),
    }
    set_cached(f"album:{album_id}", payload, 30 * 60)
    return payload


@router.get("/album")
def get_album_q(id: str = Query(...)):
    cached = get_cached(f"album:{id}")
    if cached:
        return cached
    return _album_payload(id)


@router.get("/album/{id}")
def get_album_p(id: str = Path(...)):
    cached = get_cached(f"album:{id}")
    if cached:
        return cached
    return _album_payload(id)
