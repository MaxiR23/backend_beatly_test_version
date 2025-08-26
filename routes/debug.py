# routes/debug.py
from fastapi import APIRouter, Query
from innertube import InnerTube
from services.cache_service import get_cached, set_cached


router = APIRouter()

@router.get("/")
def playlists_root():
    return {"message": "DEBUG route OK"}


@router.get("/search_debug")
def search_debug(q: str = Query(..., description="Texto a buscar")):
    """
    Devuelve la respuesta CRUDA de YouTube Music (WEB_REMIX).
    Sirve para debug y ver toda la estructura.
    """
    try:
        yt = InnerTube("WEB_REMIX")
        response = yt.search(q)
        return response   # 🔴 devolvemos TODO, sin filtrar
    except Exception as e:
        return {"error": "search_error", "detail": str(e)}
    
@router.get("/artist_debug")
def artist_debug(id: str = Query(..., description="Artist browseId")):
    try:
        yt = InnerTube("WEB_REMIX")
        response = yt.browse(id)   # 👈 browse con el browseId del artista
        return response
    except Exception as e:
        return {"error": "artist_debug_error", "detail": str(e), "id": id}

@router.get("/artist_debug_contents")
def artist_debug_contents(id: str = Query(..., description="Artist browseId")):
    try:
        yt = InnerTube("WEB_REMIX")
        response = yt.browse(id)

        # navegar directo a contents
        contents = (
            response.get("contents", {})
            .get("singleColumnBrowseResultsRenderer", {})
            .get("tabs", [])[0]
            .get("tabRenderer", {})
            .get("content", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )

        return {"artistId": id, "contents": contents}

    except Exception as e:
        return {"error": "artist_debug_contents_error", "detail": str(e), "id": id}
    
@router.get("/album_debug")
def album_debug(id: str = Query(..., description="Album browseId")):
    """
    Devuelve la respuesta completa de un álbum desde YouTube Music.
    """
    yt = InnerTube("WEB_REMIX")
    response = yt.browse(id)
    return response