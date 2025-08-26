# utils/album_parser.py
def parse_album_info(response: dict):
    """Extrae la información general de un álbum desde microformat."""
    micro = (
        response.get("microformat", {})
        .get("microformatDataRenderer", {})
    )

    if not micro:
        return {}

    title = micro.get("title")
    description = micro.get("description")
    url = micro.get("urlCanonical")
    thumbnails = micro.get("thumbnail", {}).get("thumbnails", [])
    site_name = micro.get("siteName")

    payload = {
        "title": title,
        "description": description,
        "url": url,
        "thumbnails": thumbnails,
        "siteName": site_name,
        # dejamos los enlaces por si sirven después
        "links": {
            "applinksWeb": micro.get("urlApplinksWeb"),
            "applinksIos": micro.get("urlApplinksIos"),
            "applinksAndroid": micro.get("urlApplinksAndroid"),
        }
    }

    return payload

def parse_album_thumbnails_from_background(node: dict):
    """
    Extrae TODOS los thumbnails de un track/album desde 'background.musicThumbnailRenderer.thumbnail.thumbnails'.
    Ejemplo: track['overlay']['musicItemThumbnailOverlayRenderer']['background']
    """
    background = (
        node.get("musicThumbnailRenderer", {})
        .get("thumbnail", {})
        .get("thumbnails", [])
    )
    return background or []

def parse_album_tracks(album_response: dict):
    """
    Extrae las canciones de un álbum desde la respuesta de InnerTube.
    """
    contents = (
        album_response.get("contents", {})
        .get("twoColumnBrowseResultsRenderer", {})
        .get("secondaryContents", {})
        .get("sectionListRenderer", {})
        .get("contents", [])
    )

    tracks = []
    if not contents:
        return tracks

    # El primer sectionListRenderer normalmente contiene las canciones
    for sec in contents:
        shelf = sec.get("musicShelfRenderer")
        if not shelf:
            continue

        for item in shelf.get("contents", []):
            renderer = item.get("musicResponsiveListItemRenderer")
            if not renderer:
                continue

            # Video ID
            video_id = (
                renderer.get("playlistItemData", {}).get("videoId")
                or renderer.get("navigationEndpoint", {})
                .get("watchEndpoint", {})
                .get("videoId")
            )

            # Título
            title_runs = (
                renderer.get("flexColumns", [])[0]
                .get("musicResponsiveListItemFlexColumnRenderer", {})
                .get("text", {})
                .get("runs", [])
            )
            title = title_runs[0]["text"] if title_runs else None

            # Artistas
            artists = []
            if len(renderer.get("flexColumns", [])) > 1:
                runs = (
                    renderer["flexColumns"][1]
                    .get("musicResponsiveListItemFlexColumnRenderer", {})
                    .get("text", {})
                    .get("runs", [])
                )
                for run in runs:
                    if "navigationEndpoint" in run:
                        browse = run["navigationEndpoint"].get("browseEndpoint", {})
                        if browse.get("browseId"):
                            artists.append({
                                "id": browse["browseId"],
                                "name": run.get("text")
                            })

            # Plays
            plays = None
            if len(renderer.get("flexColumns", [])) > 2:
                plays_runs = renderer["flexColumns"][2] \
                    .get("musicResponsiveListItemFlexColumnRenderer", {}) \
                    .get("text", {}).get("runs", [])
                if plays_runs:
                    plays = plays_runs[0].get("text")

            # Duración
            duration = None
            fixed_cols = renderer.get("fixedColumns", [])
            if fixed_cols:
                runs = (
                    fixed_cols[0]
                    .get("musicResponsiveListItemFixedColumnRenderer", {})
                    .get("text", {})
                    .get("runs", [])
                )
                if runs:
                    duration = runs[0].get("text")

            # Index
            index = None
            if "index" in renderer:
                runs = renderer["index"].get("runs", [])
                if runs:
                    index = runs[0].get("text")

            tracks.append({
                "title": title,
                "videoId": video_id,
                "artists": artists,
                "plays": plays,
                "duration": duration,
                "index": index,
            })

    return tracks