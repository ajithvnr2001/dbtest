from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from .models import Album, Movie, Track

try:
    from scrapling.fetchers import Fetcher
except ImportError as exc:  # pragma: no cover - exercised only before install
    Fetcher = None  # type: ignore[assignment]
    _SCRAPLING_ERROR = exc
else:
    _SCRAPLING_ERROR = None


def normalise(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def album_score(movie: Movie, album_name: str, release_year: int | None) -> float:
    movie_name = normalise(movie.title)
    album = normalise(album_name)
    if not movie_name or not album:
        return 0.0
    movie_tokens = set(movie_name.split())
    album_tokens = set(album.split())
    overlap = len(movie_tokens & album_tokens) / max(len(movie_tokens), 1)
    similarity = SequenceMatcher(None, movie_name, album).ratio()
    exact_prefix = 1.0 if album.startswith(movie_name) or movie_name.startswith(album) else 0.0
    year_bonus = 0.15 if movie.year and release_year and abs(movie.year - release_year) <= 2 else 0.0
    return min(1.0, 0.42 * overlap + 0.33 * similarity + 0.25 * exact_prefix + year_bonus)


def album_belongs_to_movie(movie: Movie, album_name: str) -> bool:
    """Reject albums that merely contain a movie title as a suffix/token."""
    movie_name = normalise(movie.title)
    album = normalise(album_name)
    return bool(movie_name and (album.startswith(movie_name) or f"from {movie_name}" in album))


class JsonCache:
    def __init__(self, directory: str | Path | None):
        self.directory = Path(directory) if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path | None:
        if not self.directory:
            return None
        return self.directory / (hashlib.sha256(url.encode()).hexdigest() + ".json")

    def get(self, url: str) -> dict | None:
        path = self._path(url)
        if not path or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, url: str, data: dict) -> None:
        path = self._path(url)
        if path:
            temporary = path.with_suffix(".part")
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.replace(path)


class ScraplingJsonClient:
    """Fetch Apple's public catalog JSON using Scrapling's HTTP fetcher."""

    def __init__(self, cache_dir: str | Path | None = None, pause: float = 0.15, retries: int = 3):
        if Fetcher is None:
            raise RuntimeError("Scrapling is required; install dependencies with `pip install -e .`") from _SCRAPLING_ERROR
        self.cache = JsonCache(cache_dir)
        self.pause = pause
        self.retries = retries

    def get(self, url: str) -> dict:
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                page = Fetcher.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "tamil-movie-music/0.1 (research indexer)",
                    },
                    timeout=30,
                )
                body = page.body
                if isinstance(body, bytes):
                    body = body.decode("utf-8")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("Apple returned a non-object JSON response")
                self.cache.put(url, data)
                if self.pause:
                    time.sleep(self.pause)
                return data
            except Exception as exc:  # pragma: no cover - network-dependent
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Apple Music request failed: {url}") from last_error


class AppleMusicScraper:
    def __init__(self, client: ScraplingJsonClient, country: str = "IN", max_album_candidates: int = 12):
        self.client = client
        self.country = country
        self.max_album_candidates = max_album_candidates

    def _url(self, endpoint: str, params: dict[str, str | int]) -> str:
        query = urllib.parse.urlencode({"country": self.country, **params})
        return f"https://itunes.apple.com/{endpoint}?{query}"

    def search_albums(self, movie: Movie) -> list[dict]:
        # The second query catches releases where Apple has appended “soundtrack”.
        candidates: dict[int, dict] = {}
        for term in (movie.title, f"{movie.title} soundtrack"):
            data = self.client.get(self._url("search", {"term": term, "media": "music", "entity": "album", "attribute": "albumTerm", "limit": 200}))
            for item in data.get("results", []):
                collection_id = item.get("collectionId")
                if collection_id is not None:
                    candidates[int(collection_id)] = item
        return sorted(
            candidates.values(),
            key=lambda item: album_score(movie, item.get("collectionName", ""), _year(item.get("releaseDate"))),
            reverse=True,
        )

    def album_tracks(self, collection_id: int) -> tuple[Track, ...]:
        data = self.client.get(self._url("lookup", {"id": collection_id, "entity": "song", "limit": 200}))
        tracks: list[Track] = []
        for item in data.get("results", []):
            if item.get("wrapperType") != "track" or not item.get("trackId"):
                continue
            track_id = int(item["trackId"])
            # The ID came from Apple's catalog lookup. Apple's short song URL
            # redirects to the same catalog-backed page and matches the output
            # format requested by the project.
            if item.get("trackViewUrl", "").startswith("https://music.apple.com/"):
                url = canonical_song_url(track_id, self.country)
                tracks.append(Track(track_id=track_id, name=item.get("trackName", ""), url=url))
        return tuple(dict((track.track_id, track) for track in tracks).values())

    def find_albums(self, movie: Movie, threshold: float = 0.54) -> list[Album]:
        found: list[Album] = []
        for item in self.search_albums(movie)[: self.max_album_candidates]:
            if item.get("collectionType") == "Single" or (
                item.get("trackCount") == 1 and "single" in item.get("collectionName", "").casefold()
            ):
                continue
            if not album_belongs_to_movie(movie, item.get("collectionName", "")):
                continue
            score = album_score(movie, item.get("collectionName", ""), _year(item.get("releaseDate")))
            if score < threshold:
                continue
            tracks = self.album_tracks(int(item["collectionId"]))
            if not tracks:
                continue
            found.append(Album(
                collection_id=int(item["collectionId"]),
                name=item.get("collectionName", movie.title),
                artist=item.get("artistName", ""),
                year=_year(item.get("releaseDate")) or movie.year,
                tracks=tracks,
                source_movie=movie,
                score=score,
            ))
        return found


def _year(value: str | None) -> int | None:
    match = re.match(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


def canonical_song_url(track_id: int, country: str = "IN") -> str:
    """Build a locale URL only for an ID already returned by Apple's API."""
    return f"https://music.apple.com/{country.lower()}/song/{track_id}"
