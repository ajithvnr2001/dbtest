from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path

from .models import Album


def render_markdown(albums: Iterable[Album], generated: date | None = None) -> str:
    albums = list(albums)
    tracks = sum(len(album.tracks) for album in albums)
    when = generated or date.today()
    lines = [f"# Tamil movie soundtracks ({len(albums)} albums, {tracks} songs) — generated {when.isoformat()}", ""]
    seen_names: dict[str, int] = {}
    for album in sorted(albums, key=lambda item: ((item.year or 0), item.name.casefold(), item.collection_id)):
        heading = album.name.strip() or album.source_movie.title
        seen_names[heading] = seen_names.get(heading, 0) + 1
        if seen_names[heading] > 1:
            heading = f"{heading} [{album.source_movie.title}]"
        year = f" ({album.year})" if album.year else ""
        lines.append(f"{heading}{year}:")
        lines.extend(f"  {track.url}" for track in album.tracks)
        lines.append("")
    return "\n".join(lines)


def write_markdown(path: str | Path, albums: Iterable[Album]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.write_text(render_markdown(albums), encoding="utf-8")
    temporary.replace(destination)
