def parse_top_songs(section):
    songs = []
    for item in section.get("musicShelfRenderer", {}).get("contents", []):
        r = item.get("musicResponsiveListItemRenderer")
        if not r:
            continue

        video_id = (
            r.get("overlay", {})
            .get("musicItemThumbnailOverlayRenderer", {})
            .get("content", {})
            .get("musicPlayButtonRenderer", {})
            .get("playNavigationEndpoint", {})
            .get("watchEndpoint", {})
            .get("videoId")
        )

        title_runs = r["flexColumns"][0]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
        title = title_runs[0]["text"] if title_runs else None

        artist_runs = r["flexColumns"][1]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
        artist_name = artist_runs[0]["text"] if artist_runs else None
        artist_id = artist_runs[0].get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")

        album_runs = r["flexColumns"][3]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
        album_title = album_runs[0]["text"] if album_runs else None
        album_id = album_runs[0].get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId")

        thumbs = (
            r.get("thumbnail", {})
            .get("musicThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )

        songs.append({
            "id": video_id,
            "artistId": artist_id,
            "title": title,
            "albumId": album_id,
            "thumbnail": thumbs[0]["url"] if thumbs else None,
            "artistName": artist_name,
            "duration": None,
            "durationSeconds": None,
        })
    return songs

def parse_albums(section):
    albums = []
    for item in section.get("musicCarouselShelfRenderer", {}).get("contents", []):
        r = item.get("musicTwoRowItemRenderer")
        if not r:
            continue

        # ID del álbum
        browse = (
            r.get("title", {})
            .get("runs", [{}])[0]
            .get("navigationEndpoint", {})
            .get("browseEndpoint", {})
        )
        album_id = browse.get("browseId")

        # Título
        title = r.get("title", {}).get("runs", [{}])[0].get("text")

        # Año (del subtitle)
        subtitle_runs = r.get("subtitle", {}).get("runs", [])
        year = None
        if subtitle_runs:
            year = subtitle_runs[0].get("text")

        # Thumbnails
        thumbs = (
            r.get("thumbnailRenderer", {})
            .get("musicThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )

        albums.append({
            "id": album_id,
            "title": title,
            "year": year,
            "thumbnails": thumbs
        })

    return albums

def parse_singles_eps(section):
    singles = []
    for item in section.get("musicCarouselShelfRenderer", {}).get("contents", []):
        r = item.get("musicTwoRowItemRenderer")
        if not r:
            continue

        # ID
        browse = (
            r.get("title", {})
            .get("runs", [{}])[0]
            .get("navigationEndpoint", {})
            .get("browseEndpoint", {})
        )
        single_id = browse.get("browseId")

        # Título
        title = r.get("title", {}).get("runs", [{}])[0].get("text")

        # Subtitle → [ "Single", " • ", "2025" ]
        subtitle_runs = r.get("subtitle", {}).get("runs", [])
        release_type = None
        year = None
        if subtitle_runs:
            release_type = subtitle_runs[0].get("text")
            if len(subtitle_runs) > 2:
                year = subtitle_runs[2].get("text")

        # Thumbnails
        thumbs = (
            r.get("thumbnailRenderer", {})
            .get("musicThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )

        singles.append({
            "id": single_id,
            "title": title,
            "type": release_type,  # "Single" o "EP"
            "year": year,
            "thumbnails": thumbs
        })

    return singles

def parse_featured_on(section):
    featured = []
    for item in section.get("musicCarouselShelfRenderer", {}).get("contents", []):
        r = item.get("musicTwoRowItemRenderer")
        if not r:
            continue

        # ID de playlist/album (browseId)
        browse = (
            r.get("title", {})
            .get("runs", [{}])[0]
            .get("navigationEndpoint", {})
            .get("browseEndpoint", {})
        )
        playlist_id = browse.get("browseId")

        # Título de la playlist
        title = r.get("title", {}).get("runs", [{}])[0].get("text")

        # Subtitle → normalmente "Playlist • YouTube Music"
        subtitle_runs = r.get("subtitle", {}).get("runs", [])
        subtitle = "".join(run.get("text", "") for run in subtitle_runs)

        # Thumbnails
        thumbs = (
            r.get("thumbnailRenderer", {})
            .get("musicThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )

        featured.append({
            "id": playlist_id,
            "title": title,
            "subtitle": subtitle,
            "thumbnails": thumbs
        })

    return featured

def parse_related_artists(section: dict):
    """
    Devuelve artistas relacionados.
    """
    related = []
    carousel = section.get("musicCarouselShelfRenderer", {})
    items = carousel.get("contents", [])

    for item in items:
        r = item.get("musicTwoRowItemRenderer")
        if not r:
            continue

        # Artist ID
        browse = (
            r.get("title", {})
            .get("runs", [{}])[0]
            .get("navigationEndpoint", {})
            .get("browseEndpoint", {})
        )
        artist_id = browse.get("browseId")

        # Nombre
        name = (
            r.get("title", {})
            .get("runs", [{}])[0]
            .get("text")
        )

        # Subtitle → "X monthly audience"
        subtitle_runs = r.get("subtitle", {}).get("runs", [])
        subtitle = "".join(run.get("text", "") for run in subtitle_runs)

        # Thumbnails
        thumbs = (
            r.get("thumbnailRenderer", {})
            .get("musicThumbnailRenderer", {})
            .get("thumbnail", {})
            .get("thumbnails", [])
        )

        if artist_id and name:
            related.append({
                "id": artist_id,
                "name": name,
                "subtitle": subtitle,
                "thumbnails": thumbs
            })

    return related