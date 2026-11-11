"""Read public channel metrics through an injected YouTube API callback.

No credentials, files, or network clients are accessed here. ``None`` means a
failed refresh. A successful refresh with no eligible videos has a zero sample
count and ``avg_views=None``; actual zero-view samples retain ``avg_views=0``.
"""

from datetime import datetime, timezone
import re
import unicodedata
from urllib.parse import unquote, urlsplit


_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?\Z"
)
_SOURCE = "YouTube Data API v3（最近50条上传中，按发布时间取最近最多6条、时长>180秒的视频）"


def _handle(value):
    if not value.startswith("@") or not 1 <= len(value[1:]) <= 100:
        return None
    # Preserve valid international handles without accepting path/query syntax.
    if all(character in "_.-" or unicodedata.category(character)[0] in "LMN"
           for character in value[1:]):
        return value
    return None


def _channel_reference(homepage):
    if not isinstance(homepage, str):
        return None
    value = homepage.strip()
    if _CHANNEL_ID.fullmatch(value):
        return {"id": value}
    handle = _handle(value)
    if handle:
        return {"forHandle": handle}
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme.lower() not in ("https", "http")
            or parsed.netloc.lower() not in ("youtube.com", "www.youtube.com", "m.youtube.com")
            or parsed.query or parsed.fragment):
        return None
    path = unquote(parsed.path).rstrip("/")
    if path.startswith("/channel/"):
        channel_id = path[len("/channel/"):]
        return {"id": channel_id} if _CHANNEL_ID.fullmatch(channel_id) else None
    handle = _handle(path[1:]) if path.startswith("/") else None
    return {"forHandle": handle} if handle else None


def _items(response):
    if (not isinstance(response, dict) or "error" in response
            or not isinstance(response.get("items"), list)):
        return None
    return response["items"]


def _duration_seconds(value):
    if not isinstance(value, str):
        return None
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groupdict().values()):
        return None
    values = {key: float(number or 0) for key, number in match.groupdict().items()}
    return values["days"] * 86400 + values["hours"] * 3600 + values["minutes"] * 60 + values["seconds"]


def _published_at(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _view_count(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def refresh_channel_metrics(homepage, api_call):
    """Return refreshed public metrics, or ``None`` on any API/data failure.

    ``api_call(endpoint, params)`` must return the decoded API object or None.
    Only one uploads page (at most 50 entries) is scanned. Video publication
    timestamps, rather than playlist insertion order, choose the newest six.
    Country is the channel's declared country, not an audience-country estimate.
    """
    reference = _channel_reference(homepage)
    if reference is None:
        return None
    try:
        channels = _items(api_call("channels", dict(reference, part="contentDetails,snippet")))
        if not channels or not isinstance(channels[0], dict):
            return None
        channel = channels[0]
        channel_id = channel.get("id", "")
        if not isinstance(channel_id, str) or not _CHANNEL_ID.fullmatch(channel_id):
            return None
        if "id" in reference and reference["id"] != channel_id:
            return None
        uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not isinstance(uploads, str) or not uploads:
            return None
        country = channel.get("snippet", {}).get("country", "")
        if not isinstance(country, str):
            return None
        entries = _items(api_call("playlistItems", {
            "part": "contentDetails", "playlistId": uploads, "maxResults": 50,
        }))
        if entries is None:
            return None
        video_ids = []
        for entry in entries[:50]:
            if not isinstance(entry, dict):
                return None
            video_id = entry.get("contentDetails", {}).get("videoId")
            if not isinstance(video_id, str) or not video_id:
                return None
            if video_id not in video_ids:
                video_ids.append(video_id)

        samples = []
        if video_ids:
            videos = _items(api_call("videos", {
                "part": "statistics,contentDetails,snippet", "id": ",".join(video_ids),
            }))
            if videos is None:
                return None
            observed_ids = set()
            for video in videos:
                if not isinstance(video, dict):
                    return None
                video_id = video.get("id")
                if video_id not in video_ids or video_id in observed_ids:
                    return None
                observed_ids.add(video_id)
                duration = _duration_seconds(video.get("contentDetails", {}).get("duration"))
                if duration is None:
                    return None
                if duration <= 180:
                    continue
                published_at = _published_at(video.get("snippet", {}).get("publishedAt"))
                if published_at is None:
                    return None
                samples.append((published_at, video))
        # Deleted/private uploads may be omitted by videos.list. Record the
        # actual public sample size instead of treating those omissions as zero.
        samples.sort(key=lambda item: item[0], reverse=True)
        views = []
        for _, video in samples[:6]:
            count = _view_count(video.get("statistics", {}).get("viewCount"))
            if count is None:
                return None
            views.append(count)
        return {
            "avg_views": sum(views) / len(views) if views else None,
            "sample_count": len(views),
            "country": country,
            "homepage": "https://www.youtube.com/channel/" + channel_id,
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "views_source": _SOURCE,
        }
    except Exception:
        # Wrappers may raise transport errors; no partial sample becomes zero.
        return None
