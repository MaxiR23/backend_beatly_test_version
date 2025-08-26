# routes/music.py
from fastapi import APIRouter, Query, Path
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
from fastapi import Body

router = APIRouter()

# Cache simple en memoria
_cache = {}
CACHE_TTL = 30 * 60  # 30 minutos


def get_audio_info(video_id: str):
    """Devuelve info de yt_dlp cacheada por 30 minutos."""
    now = time.time()
    cached = _cache.get(video_id)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["info"]

    cookies_path = os.path.join(os.path.dirname(__file__), "..", "cookies.txt")

    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "skip_download": True,
        "cookiefile": cookies_path,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}",
            download=False,
        )

    _cache[video_id] = {"info": info, "ts": now}
    return info


def _stream_from_url(url: str) -> StreamingResponse:
    """Crea un StreamingResponse con media_type correcto."""
    r = requests.get(url, stream=True)
    content_type = r.headers.get("Content-Type", "audio/webm")
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    content_length = r.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length

    # chunk grande para menos overhead
    return StreamingResponse(r.iter_content(chunk_size=1024 * 64), media_type=content_type, headers=headers)


# --- AUDIO ---

@router.get("/play")
def play_song(id: str = Query(..., description="YouTube video ID")):
    """Devuelve un stream de audio para un video de YouTube usando yt_dlp."""
    try:
        info = get_audio_info(id)
        audio_url = info["url"]
        return _stream_from_url(audio_url)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "yt-dlp", "detail": str(e), "id": id})


@router.post("/prefetch")
def prefetch_songs(payload: dict = Body(...)):
    """Precarga info de varias canciones para que estén listas en cache."""
    raw_ids = payload.get("ids", [])
    ids = list(dict.fromkeys([str(i).strip() for i in raw_ids if i]))[:50]

    if not ids:
        return {"ok": True, "total": 0, "warmed_info": 0, "errors": 0}

    warmed_info = 0
    errors = 0
    for vid in ids:
        try:
            get_audio_info(vid)
            warmed_info += 1
        except Exception:
            errors += 1

    return {"ok": True, "total": len(ids), "warmed_info": warmed_info, "errors": errors}


# --- SEARCH ---

@router.get("/search")
def search_music(q: str = Query(..., description="Texto a buscar")):
    # 1) Intentar cache primero
    cached = get_cached(f"search:{q}")
    if cached:
        return cached

    yt = InnerTube("WEB_REMIX")
    response = yt.search(q)

    artists = []
    songs = []

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
            # ARTISTAS
            card = sec.get("musicCardShelfRenderer")
            if card and card.get("title", {}).get("runs"):
                # card de artista → browseEndpoint
                if "browseEndpoint" in card["title"]["runs"][0].get("navigationEndpoint", {}):
                    title_runs = card["title"]["runs"]
                    name = title_runs[0]["text"]
                    browse_id = title_runs[0]["navigationEndpoint"]["browseEndpoint"]["browseId"]

                    subtitle_runs = card.get("subtitle", {}).get("runs", [])
                    subtitle = "".join(run.get("text", "") for run in subtitle_runs)

                    thumbs = (
                        card.get("thumbnail", {})
                        .get("musicThumbnailRenderer", {})
                        .get("thumbnail", {})
                        .get("thumbnails", [])
                    )

                    artists.append(
                        {
                            "name": name,
                            "artistId": browse_id,
                            "subtitle": subtitle,
                            "thumbnails": thumbs,
                        }
                    )

                # card de SONG → watchEndpoint con videoId
                elif "watchEndpoint" in card["title"]["runs"][0].get("navigationEndpoint", {}):
                    title_runs = card["title"]["runs"]
                    title = title_runs[0]["text"]
                    video_id = card["title"]["runs"][0]["navigationEndpoint"]["watchEndpoint"]["videoId"]

                    subtitle_runs = card.get("subtitle", {}).get("runs", [])
                    artists_list = []
                    duration = None
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

    # 2) Guardar resultado en cache
    result = {"query": q, "artists": artists, "songs": songs}
    set_cached(f"search:{q}", result)

    return result


# --- ARTIST ---

def _artist_payload(artist_id: str):
    yt = InnerTube("WEB_REMIX")
    response = yt.browse(artist_id)

    # HEADER
    header = response.get("header", {}).get("musicImmersiveHeaderRenderer", {})
    name = header.get("title", {}).get("runs", [{}])[0].get("text")
    desc_runs = header.get("description", {}).get("runs", [])
    description = "".join(run.get("text", "") for run in desc_runs)
    thumbs = (
        header.get("thumbnail", {})
        .get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails", [])
    )
    listeners = header.get("monthlyListenerCount", {}).get("runs", [{}])[0].get("text")

    # CONTENTS → Top Songs está en contents[0]
    contents = (
        response.get("contents", {})
        .get("singleColumnBrowseResultsRenderer", {})
        .get("tabs", [])[0]
        .get("tabRenderer", {})
        .get("content", {})
        .get("sectionListRenderer", {})
        .get("contents", [])
    )

    top_songs = parse_top_songs(contents[0]) if len(contents) > 0 else []
    albums = parse_albums(contents[1]) if len(contents) > 1 else []
    singles_eps = parse_singles_eps(contents[2]) if len(contents) > 2 else []
    related = parse_related_artists(contents[7]) if len(contents) > 7 else []

    return {
        "header": {
            "name": name,
            "description": description,
            "thumbnails": thumbs,
            "monthlyListeners": listeners,
        },
        "topSongs": top_songs,
        "albums": albums,
        "singles_eps": singles_eps,
        "related": related,
    }


@router.get("/artist")           # sigue funcionando ?id=...
def get_artist_q(id: str = Query(..., description="Artist browseId")):
    return _artist_payload(id)


@router.get("/artist/{id}")      # NUEVO: path param /artist/:id
def get_artist_p(id: str = Path(..., description="Artist browseId")):
    return _artist_payload(id)


# --- ALBUM ---

def _album_payload(album_id: str):
    yt = InnerTube("WEB_REMIX")
    response = yt.browse(album_id)

    album_info = parse_album_info(response)
    tracks = parse_album_tracks(response)

    payload = {"id": album_id, "info": album_info, "tracks": tracks}
    set_cached(f"album:{album_id}", payload, 30 * 60)
    return payload


@router.get("/album")            # sigue funcionando ?id=...
def get_album_q(id: str = Query(..., description="Album browseId")):
    # cache
    cached = get_cached(f"album:{id}")
    if cached:
        return cached
    return _album_payload(id)


@router.get("/album/{id}")       # NUEVO: path param /album/:id
def get_album_p(id: str = Path(..., description="Album browseId")):
    cached = get_cached(f"album:{id}")
    if cached:
        return cached
    return _album_payload(id)