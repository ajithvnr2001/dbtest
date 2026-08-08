from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .apple_music import AppleMusicScraper, ScraplingJsonClient
from .imdb import DatasetDownloader
from .imdb_search import IMDbFeatureSearch, iter_movies_for_ids
from .models import Album, Movie, Track
from .output import write_markdown


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Build a Tamil IMDb movie to Apple Music soundtrack index.")
    command.add_argument("--data-dir", type=Path, default=Path("data/imdb"), help="IMDb .tsv.gz download directory")
    command.add_argument("--cache-dir", type=Path, default=Path("data/apple-cache"), help="Apple API response cache")
    command.add_argument("--output", type=Path, default=Path("tamil-movie-songs.md"), help="Markdown output path")
    command.add_argument("--json-output", type=Path, help="Optional machine-readable output path")
    command.add_argument("--limit", type=int, help="Process only the first N movies (useful for smoke tests)")
    command.add_argument("--threshold", type=float, default=0.54, help="Album/movie matching score, 0..1")
    command.add_argument("--pause", type=float, default=0.15, help="Seconds between uncached Apple requests")
    command.add_argument("--batch-size", type=int, default=10, help="Update the output and checkpoint after this many movies")
    command.add_argument("--no-download", action="store_true", help="Use existing IMDb files")
    command.add_argument("--keep-empty", action="store_true", help="Write a report containing movies with no matching album")
    return command


def _checkpoint_path(output: Path) -> Path:
    return output.with_name(output.name + ".checkpoint.json")


def _album_dict(album: Album) -> dict:
    return asdict(album)


def _public_album_dict(album: Album) -> dict:
    return {
        "collection_id": album.collection_id,
        "album": album.name,
        "artist": album.artist,
        "year": album.year,
        "source_movie": asdict(album.source_movie),
        "score": round(album.score, 4),
        "tracks": [asdict(track) for track in album.tracks],
    }


def _album_from_dict(data: dict) -> Album:
    source = Movie(**data["source_movie"])
    tracks = tuple(Track(**track) for track in data["tracks"])
    return Album(
        collection_id=data["collection_id"],
        name=data["name"],
        artist=data["artist"],
        year=data["year"],
        tracks=tracks,
        source_movie=source,
        score=data.get("score", 0.0),
    )


def _load_checkpoint(path: Path, threshold: float) -> tuple[set[str], list[Album], list[dict]]:
    if not path.exists():
        return set(), [], []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != 6 or state.get("threshold") != threshold:
            return set(), [], []
        albums = [_album_from_dict(item) for item in state.get("albums", [])]
        return set(state.get("processed_movies", [])), albums, state.get("unmatched", [])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return set(), [], []


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_outputs(args: argparse.Namespace, albums: list[Album], empty: list[dict], processed: set[str]) -> int:
    unique = {album.collection_id: album for album in albums}
    write_markdown(args.output, unique.values())
    if args.json_output:
        _write_json(args.json_output, [_public_album_dict(album) for album in sorted(unique.values(), key=lambda item: item.collection_id)])
    if args.keep_empty:
        _write_json(args.output.with_name(args.output.stem + ".unmatched.json"), empty)
    _write_json(_checkpoint_path(args.output), {
        "version": 6,
        "threshold": args.threshold,
        "processed_movies": sorted(processed),
        "albums": [_album_dict(album) for album in unique.values()],
        "unmatched": empty,
    })
    return len(unique)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    downloader = DatasetDownloader(args.data_dir)
    paths = downloader.download_all() if not args.no_download else {name: args.data_dir / f"title.{name}.tsv.gz" for name in ("akas", "basics", "crew")}
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        print("Missing IMDb dataset(s): " + ", ".join(missing), file=sys.stderr)
        return 2
    if args.batch_size < 1:
        print("--batch-size must be at least 1", file=sys.stderr)
        return 2

    scraper = AppleMusicScraper(ScraplingJsonClient(args.cache_dir, pause=args.pause))
    processed, albums, empty = _load_checkpoint(_checkpoint_path(args.output), args.threshold)
    starting_processed = len(processed)
    pending = 0
    imdb_ids = IMDbFeatureSearch(args.cache_dir / "imdb-search").collect_ids(language="ta")
    print(f"IMDb language search returned {len(imdb_ids)} feature-film IDs", file=sys.stderr)
    movies = iter_movies_for_ids(paths["basics"], imdb_ids, limit=args.limit)
    for movie in movies:
        if movie.tconst in processed:
            continue
        try:
            matches = scraper.find_albums(movie, threshold=args.threshold)
        except Exception as exc:  # keep a long crawl resumable when one title fails
            print(f"warning: {movie.tconst} {movie.title!r}: {exc}", file=sys.stderr)
            empty.append({"tconst": movie.tconst, "title": movie.title, "year": movie.year, "error": str(exc)})
        else:
            albums.extend(matches)
            if not matches:
                empty.append({"tconst": movie.tconst, "title": movie.title, "year": movie.year})
        processed.add(movie.tconst)
        pending += 1
        if pending >= args.batch_size:
            album_count = _write_outputs(args, albums, empty, processed)
            print(f"updated {args.output}: {len(processed)} movies, {album_count} albums", file=sys.stderr)
            pending = 0

    if pending or starting_processed == 0:
        _write_outputs(args, albums, empty, processed)
    unique = {album.collection_id: album for album in albums}
    print(f"done: {len(processed)} movies, {len(unique)} albums / {sum(len(a.tracks) for a in unique.values())} songs in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
