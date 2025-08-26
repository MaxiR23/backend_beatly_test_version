# routes/music.py
from fastapi import APIRouter, Query, Path, Body, Request
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
CACHE_TTL = 30 * 60    # metadata: 30 min
URL_TTL   = 120        # URL directa: 2 min (corto para evitar expiradas)
_cache = {}

cookies_path = os.path.join(os.path.dirname(__file__), "..", "cookies.txt")

# Reusamos sesión HTTP para que no se corte el keep-alive
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
    "Connection": "keep-alive",
})

def _ydl_for(client: str) -> yt_dlp.YoutubeDL:
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": [client],
            },
            # 👇 CORRECTO: usar la clave con guion
            "youtubepot-bgutilhttp": {
                 "base_url": ["https://bgutil-ytdlp-pot-provider-latest.onrender.com"],#LOCAL http://127.0.0.1:4416
            }, 
        },
    }
    opts["cookiefile"] = cookies_path
    return yt_dlp.YoutubeDL(opts)

def _extract_best_url(video_id: str):
    """
    Intenta con clientes que suelen traer URL directa.
    Orden por desempeño/estabilidad: ANDROID -> IOS -> WEB
    """
    for client in ("mweb", "web"):
        try:
            ydl = _ydl_for(client)
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            direct_url = info.get("url")
            if direct_url and direct_url.startswith("http"):
                return info, direct_url, client
        except Exception:
            # probamos siguiente cliente
            continue
    raise RuntimeError("no_audio_format")

def get_audio_info(video_id: str):
    """
    Devuelve info cacheada; si la URL no está o venció, re-extrae
    con clientes no-WEB para evitar SABR (sin URL).
    """
    now = time.time()
    cached = _cache.get(video_id)

    # 1) Si hay URL fresca, usala
    if cached and cached.get("direct_url") and (now - cached["ts"] < URL_TTL):
        return cached

    # 2) Re-extraer siempre que no tengamos URL válida fresca
    info, direct_url, client = _extract_best_url(video_id)
    data = {
        "info": info,
        "direct_url": direct_url,
        "client": client,
        "ts": now,
    }
    _cache[video_id] = data
    return data

def _probe_url(url: str, range_header: str | None) -> bool:
    """
    Sonda rápida: verifica si la URL sigue viva (200/206).
    Usa GET stream con timeout corto y cierra enseguida.
    """
    headers = {}
    if range_header:
        headers["Range"] = range_header
    try:
        r = _SESSION.get(url, headers=headers, stream=True, timeout=(4, 8), allow_redirects=True)
        ok = r.status_code in (200, 206)
        r.close()
        return ok
    except Exception:
        return False

def _stream_from_url(url: str, range_header: str | None) -> StreamingResponse:
    """
    Crea un StreamingResponse pasándole Range si el cliente lo pidió.
    Propaga Content-Type, Content-Length / Content-Range y status (200/206).
    """
    headers = {}
    if range_header:
        headers["Range"] = range_header

    r = _SESSION.get(url, headers=headers, stream=True, timeout=(5, 30), allow_redirects=True)

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Connection": "keep-alive",
    }

    # Propagamos cabeceras útiles desde upstream
    ct = r.headers.get("Content-Type")
    if ct:
        media_type = ct
    else:
        media_type = "audio/webm"

    cl = r.headers.get("Content-Length")
    if cl:
        resp_headers["Content-Length"] = cl

    cr = r.headers.get("Content-Range")
    if cr:
        resp_headers["Content-Range"] = cr

    return StreamingResponse(
        r.iter_content(chunk_size=1024 * 256),
        media_type=media_type,
        headers=resp_headers,
        status_code=r.status_code,  # respeta 206 si upstream respondió parcial
    )

# --- AUDIO ENDPOINTS ---

@router.get("/play")
def play_song(request: Request, id: str = Query(..., description="YouTube video ID")):
    """
    Devuelve stream de audio con soporte Range y refresh de URL si expiró.
    """
    try:
        data = get_audio_info(id)
        audio_url = data["direct_url"]  # garantizado por get_audio_info

        # Si la URL cayó (403/404/expired), refrescamos una vez
        range_hdr = request.headers.get("Range")
        if not _probe_url(audio_url, range_hdr):
            data = get_audio_info(id)  # re-extrae y actualiza cache
            audio_url = data["direct_url"]

        # Log mínimo para diagnosticar cliente usado
        print(f"[play] id={id} via client={data.get('client')} ttl={URL_TTL}s")

        return _stream_from_url(audio_url, range_hdr)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": "no_stream", "detail": str(e), "id": id},
        )

@router.post("/prefetch")
def prefetch_songs(payload: dict = Body(...)):
    """
    Precarga info + direct_url de varias canciones.
    Nunca deja en cache un 'info' sin URL.
    """
    raw_ids = payload.get("ids", [])
    ids = list(dict.fromkeys([str(i).strip() for i in raw_ids if i]))[:50]

    if not ids:
        return {"ok": True, "total": 0, "warmed_info": 0, "errors": 0}

    warmed_info = 0
    errors = 0
    for vid in ids:
        try:
            data = get_audio_info(vid)  # ya fuerza URL válida
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

            elif "watchEndpoint" in runs[0].get("navigationEndpoint", {}):
                title = runs[0]["text"]
                video_id = runs[0]["navigationEndpoint"]["watchEndpoint"]["videoId"]

                # Evitamos videoclips oficiales para priorizar audio
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