# routes/music.py
from fastapi import APIRouter, Query, Path, Body, Request
from fastapi.responses import StreamingResponse, JSONResponse, RedirectResponse
import time
import requests
import yt_dlp
from innertube import InnerTube
import os
import urllib.parse  # <- NUEVO

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
URL_TTL   = 15 * 60    # fallback si no podemos leer expire (antes 120s era muy corto)
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
    # Clientes "mobile" NO soportan cookies en yt-dlp
    mobile_client = client.lower() in ("android", "ios")

    opts = {
        # Preferí bestaudio y dejá m4a como preferencia, no como requisito duro
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": [client],
            },
            # Si usás este provider, dejalo; si android/ios ya resuelve bien, podés quitarlo
            "youtubepot-bgutilhttp": {
                "base_url": ["https://bgutil-ytdlp-pot-provider-latest.onrender.com"],  # LOCAL http://127.0.0.1:4416
            },
        },
        # Opcional: menos retries si querés respuestas más rápidas ante SABR
        "retries": 1,
        "extractor_retries": 1,
    }

    # 👇 Sólo pasamos cookies para web/mweb (NO para android/ios)
    if not mobile_client and os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path

    return yt_dlp.YoutubeDL(opts)

def _ttl_from_url(u: str) -> int:
    """Deriva TTL real de la URL googlevideo leyendo 'expire' o 'x-goog-expires'."""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        now = int(time.time())
        exp = 0
        if "expire" in qs:
            exp = int(qs["expire"][0])
            ttl = max(60, min(90*60, exp - now - 120))  # margen de 120s
            return ttl
        if "x-goog-expires" in qs:
            ttl = int(qs["x-goog-expires"][0])
            return max(60, min(90*60, ttl - 120))
    except Exception:
        pass
    return URL_TTL

def _extract_best_url(video_id: str):
    """
    Intenta con clientes que suelen traer URL directa rápido.
    Orden por desempeño/estabilidad: ANDROID -> IOS -> WEB
    """
    for client in ("web_music", "mweb", "web"):
        try:
            ydl = _ydl_for(client)
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            direct_url = info.get("url")
            if direct_url and direct_url.startswith("http"):
                return info, direct_url, client
        except Exception:
            continue
    raise RuntimeError("no_audio_format")

def get_audio_info(video_id: str):
    """
    Devuelve info cacheada; si la URL no está o venció, re-extrae.
    Usa TTL derivado de la URL para evitar re-extracciones innecesarias.
    """
    now = time.time()
    cached = _cache.get(video_id)

    if cached and cached.get("direct_url"):
        ttl = cached.get("ttl", URL_TTL)
        if now - cached["ts"] < ttl:
            return cached

    info, direct_url, client = _extract_best_url(video_id)
    data = {
        "info": info,
        "direct_url": direct_url,
        "client": client,
        "ts": now,
        "ttl": _ttl_from_url(direct_url),
    }
    _cache[video_id] = data
    return data

def _probe_url(url: str) -> bool:
    """
    Sonda rápida: pide el primer byte (Range 0-1) para validar 200/206.
    """
    headers = {"Range": "bytes=0-1"}
    try:
        r = _SESSION.get(url, headers=headers, stream=True, timeout=(2, 5), allow_redirects=True)
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

    ct = r.headers.get("Content-Type")
    media_type = ct if ct else "audio/webm"

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
        status_code=r.status_code,
    )

# --- AUDIO ENDPOINTS ---

@router.get("/play")
def play_song(
    request: Request,
    id: str = Query(..., description="YouTube video ID"),
    redir: int = Query(0, description="Si 1, redirige al CDN en vez de proxyear")
):
    """
    Devuelve stream de audio con soporte Range y refresh de URL si expiró.
    Si redir=1, devuelve 307 Redirect al CDN (menos latencia).
    """
    try:
        data = get_audio_info(id)
        audio_url = data["direct_url"]

        # Si la URL cayó (403/404/expired), refrescamos una vez
        if not _probe_url(audio_url):
            data = get_audio_info(id)  # re-extrae y actualiza cache
            audio_url = data["direct_url"]

        approx_ttl = data.get("ttl", URL_TTL)
        print(f"[play] id={id} via client={data.get('client')} ttl≈{approx_ttl}s")

        if redir == 1:
            return RedirectResponse(url=audio_url, status_code=307)

        range_hdr = request.headers.get("Range")
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