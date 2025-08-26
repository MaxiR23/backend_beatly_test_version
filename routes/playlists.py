# routes/playlists.py
from fastapi import APIRouter, Request, Body
from services.supabase_service import db_as_user, supabase_service
from services.cache_service import get_cached, set_cached, del_cached, del_many
from services.jwt_utils import decode_jwt

router = APIRouter(tags=["playlists"])

def _get_user_id(request: Request) -> str | None:
    # 1) si tu middleware ya puso user, usalo
    user = getattr(request.state, "user", None)
    if isinstance(user, dict) and user.get("id"):
        return user["id"]

    # 2) fallback: decodificar JWT del header
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    claims = decode_jwt(token) or {}
    return claims.get("sub")

# POST /api/playlists
@router.post("/")
async def create_playlist(request: Request, body: dict = Body(...)):
    jwt = request.headers.get("Authorization", "").replace("Bearer ", "")
    db = db_as_user(jwt)

    title = body.get("title")
    description = body.get("description")
    is_public = body.get("is_public", False)

    if not title:
        return {"error": "title requerido"}

    owner_id = _get_user_id(request)
    if not owner_id:
        return {"error": "unauthorized"}

    try:
        resp = db.table("playlists").insert({
            "title": title,
            "description": description,
            "is_public": is_public,
            "owner_id": owner_id,
        }).execute()
        del_cached(f"pl:list:{owner_id}")
        return resp.data[0]
    except Exception as e:
        return {"error": "db_error", "detail": str(e)}

# GET /api/playlists
@router.get("/")
async def get_playlists(request: Request):
    jwt = request.headers.get("Authorization", "").replace("Bearer ", "")
    db = db_as_user(jwt)

    owner_id = _get_user_id(request)
    if not owner_id:
        return {"error": "unauthorized"}

    cache_key = f"pl:list:{owner_id}"
    cached = get_cached(cache_key)
    if cached:
        return cached

    try:
        resp = db.table("playlists").select(
            "id,title,description,is_public,created_at,"
            "playlist_tracks(position,tracks(thumbnail_url))"
        ).eq("owner_id", owner_id) \
         .order("created_at", desc=True) \
         .execute()

        payload = resp.data or []

        # 🔀 Ordenar manualmente los tracks por posición
        for pl in payload:
            if "playlist_tracks" in pl and pl["playlist_tracks"]:
                pl["playlist_tracks"].sort(key=lambda t: t.get("position") or 0)

        set_cached(cache_key, payload, 30)
        return payload
    except Exception as e:
        return {"error": "db_error", "detail": str(e)}

# GET /api/playlists/:id
@router.get("/{playlist_id}")
async def get_playlist_by_id(request: Request, playlist_id: str):
    jwt = request.headers.get("Authorization", "").replace("Bearer ", "")
    db = db_as_user(jwt)

    cache_key = f"pl:detail:{playlist_id}"
    cached = get_cached(cache_key)
    if cached:
        return cached

    try:
        playlist_resp = db.table("playlists").select("*").eq("id", playlist_id).single().execute()
        ptracks_resp = db.table("playlist_tracks") \
            .select("position,added_at,tracks(*)") \
            .eq("playlist_id", playlist_id) \
            .order("position") \
            .execute()

        payload = {
            **(playlist_resp.data or {}),
            "tracks": [
                {"position": row["position"], "added_at": row["added_at"], **row["tracks"]}
                for row in (ptracks_resp.data or [])
            ]
        }

        set_cached(cache_key, payload, 30)
        return payload
    except Exception as e:
        return {"error": "db_error", "detail": str(e)}

# POST /api/playlists/:id/tracks
@router.post("/{playlist_id}/tracks")
async def add_track_to_playlist(request: Request, playlist_id: str, body: dict = Body(...)):
    jwt = request.headers.get("Authorization", "").replace("Bearer ", "")
    db = db_as_user(jwt)

    track_id = body.get("track_id")
    if not track_id:
        return {"error": "track_id requerido"}

    added_by = _get_user_id(request)
    if not added_by:
        return {"error": "unauthorized"}

    try:
        # Upsert track (service role)
        track_resp = supabase_service.table("tracks").upsert({
            "track_id": track_id,
            "title": body.get("title"),
            "artist": body.get("artist"),
            "artist_id": body.get("artist_id"),
            "album": body.get("album"),
            "duration_ms": body.get("duration_ms"),
            "thumbnail_url": body.get("thumbnail_url"),
            "extra": body.get("extra"),
        }, on_conflict="track_id").execute()

        # Calcular posición
        pos = body.get("position")
        if pos is None:
            count_resp = db.table("playlist_tracks") \
                .select("*", count="exact", head=True) \
                .eq("playlist_id", playlist_id) \
                .execute()
            pos = (count_resp.count or 0) + 1

        link_resp = db.table("playlist_tracks").insert({
            "playlist_id": playlist_id,
            "track_id": track_resp.data[0]["id"],
            "position": pos,
            "added_by": added_by,
        }).execute()

        del_many([f"pl:list:{added_by}", f"pl:detail:{playlist_id}"])
        return {"ok": True, "track": track_resp.data[0], "link": link_resp.data[0]}
    except Exception as e:
        return {"error": "db_error", "detail": str(e)}

# DELETE /api/playlists/:id/tracks/:trackId
@router.delete("/{playlist_id}/tracks/{track_id}")
async def remove_track_from_playlist(request: Request, playlist_id: str, track_id: str):
    jwt = request.headers.get("Authorization", "").replace("Bearer ", "")
    db = db_as_user(jwt)

    try:
        db.table("playlist_tracks") \
            .delete().eq("playlist_id", playlist_id).eq("track_id", track_id).execute()

        owner_id = _get_user_id(request)
        if owner_id:
            del_many([f"pl:list:{owner_id}", f"pl:detail:{playlist_id}"])

        return {"ok": True}
    except Exception as e:
        return {"error": "db_error", "detail": str(e)}
