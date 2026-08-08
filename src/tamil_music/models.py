from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Movie:
    tconst: str
    title: str
    year: int | None
    original_title: str | None = None


@dataclass(frozen=True)
class Track:
    track_id: int
    name: str
    url: str


@dataclass(frozen=True)
class Album:
    collection_id: int
    name: str
    artist: str
    year: int | None
    tracks: tuple[Track, ...]
    source_movie: Movie
    score: float = field(compare=False, default=0.0)
