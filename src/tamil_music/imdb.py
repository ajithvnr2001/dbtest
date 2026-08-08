from __future__ import annotations

import csv
import gzip
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from .models import Movie

DATASET_URLS = {
    "akas": "https://datasets.imdbws.com/title.akas.tsv.gz",
    "basics": "https://datasets.imdbws.com/title.basics.tsv.gz",
    "crew": "https://datasets.imdbws.com/title.crew.tsv.gz",
}

# A Tamil alternate title alone can represent a dubbed/translated title for a
# non-Tamil film.  These explicit alternate-title labels are excluded while
# keeping IMDb's primary display/original rows.
NON_PRIMARY_TITLE_TYPES = {
    "alternative",
    "alternative spelling",
    "dubbed version",
    "literal title",
    "poster title",
    "transliterated title",
    "video box title",
}


def _title_key(value: str) -> str:
    return " ".join("".join(char if char.isalnum() else " " for char in value.casefold()).split())


def _has_tamil_script(value: str) -> bool:
    return any("\u0b80" <= char <= "\u0bff" for char in value)


def _is_tamil_movie_title(aka_title: str, movie_title: str, original_title: str | None) -> bool:
    """Reject Tamil translations of unrelated films.

    IMDb's AKA file describes localized titles, not the title's spoken
    language. A Tamil AKA is accepted only when it is the movie's matching
    primary/original title or is actually written in Tamil script. This keeps
    titles such as a Tamil translation of *The War of the Worlds* out of the
    movie set.
    """
    aka = _title_key(aka_title)
    return bool(aka) and (aka in {_title_key(movie_title), _title_key(original_title or "")} or _has_tamil_script(aka_title))


class DatasetDownloader:
    """Download IMDb's current bulk files with resumable local files."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def download(self, name: str, retries: int = 3) -> Path:
        if name not in DATASET_URLS:
            raise ValueError(f"unknown IMDb dataset: {name}")
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"title.{name}.tsv.gz"
        if destination.exists() and destination.stat().st_size > 0:
            return destination
        temporary = destination.with_suffix(destination.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                request = urllib.request.Request(
                    DATASET_URLS[name],
                    headers={"User-Agent": "tamil-movie-music/0.1 (research indexer)"},
                )
                with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                os.replace(temporary, destination)
                return destination
            except Exception as exc:  # pragma: no cover - network-dependent
                last_error = exc
                temporary.unlink(missing_ok=True)
                if attempt + 1 < retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"could not download {name} from {DATASET_URLS[name]}") from last_error

    def download_all(self) -> dict[str, Path]:
        return {name: self.download(name) for name in DATASET_URLS}


def _rows(path: str | Path) -> Iterator[dict[str, str]]:
    # DictReader keeps this streaming: the IMDb files are too large to load into memory.
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle, delimiter="\t")


def tamil_title_ids(akas_path: str | Path, region: str | None = "IN") -> set[str]:
    """Return title IDs that IMDb marks as Tamil.

    Language plus region is the practical signal available in these three
    files.  Pass ``region=None`` to include every region carrying a Tamil
    alternate title.
    """
    ids: set[str] = set()
    for row in _rows(akas_path):
        if (
            row.get("language") == "ta"
            and (region is None or row.get("region") == region)
            and row.get("types") not in NON_PRIMARY_TITLE_TYPES
            and row.get("attributes") not in NON_PRIMARY_TITLE_TYPES
        ):
            ids.add(row["titleId"])
    return ids


def iter_tamil_movies(
    basics_path: str | Path,
    akas_path: str | Path,
    *,
    limit: int | None = None,
    region: str | None = "IN",
) -> Iterator[Movie]:
    # A current title.akas file is large enough that an in-memory set can be
    # surprisingly expensive.  Build a small on-disk membership index once.
    region_tag = region or "all"
    index_path = Path(akas_path).with_suffix(f".ta-{region_tag}.sqlite")
    _build_tamil_index(akas_path, index_path, region=region)
    connection = sqlite3.connect(index_path)
    found = 0
    try:
        for row in _rows(basics_path):
            if row.get("titleType") != "movie":
                continue
            matches = connection.execute("SELECT title FROM tamil_titles WHERE tconst = ?", (row.get("tconst"),)).fetchall()
            original_title = None if row.get("originalTitle") in (None, r"\N") else row["originalTitle"]
            if not any(_is_tamil_movie_title(item[0], row["primaryTitle"], original_title) for item in matches):
                continue
            year_text = row.get("startYear", "")
            year = int(year_text) if year_text.isdigit() else None
            yield Movie(
                tconst=row["tconst"],
                title=row["primaryTitle"],
                original_title=original_title,
                year=year,
            )
            found += 1
            if limit is not None and found >= limit:
                return
    finally:
        connection.close()


def _build_tamil_index(akas_path: str | Path, index_path: str | Path, *, region: str | None) -> None:
    akas = Path(akas_path)
    index = Path(index_path)
    source_signature = (3, akas.stat().st_size, akas.stat().st_mtime_ns, region)
    if index.exists():
        try:
            with sqlite3.connect(index) as connection:
                signature = connection.execute("SELECT value FROM metadata WHERE key = 'source_signature'").fetchone()
                if signature and signature[0] == repr(source_signature):
                    return
        except sqlite3.Error:
            index.unlink(missing_ok=True)

    temporary = index.with_suffix(index.suffix + ".part")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as connection:
        connection.execute("CREATE TABLE tamil_titles (tconst TEXT, title TEXT, PRIMARY KEY (tconst, title))")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        batch: list[tuple[str, str]] = []
        # IMDb's akas header is stable: titleId is column 0 and language is
        # column 4.  Avoiding a dict allocation for every row matters here.
        with gzip.open(akas, "rt", encoding="utf-8", newline="") as handle:
            next(handle, None)
            for line in handle:
                fields = line.rstrip("\n").split("\t")
                title_type = fields[5] if len(fields) > 5 else ""
                if (
                    len(fields) > 4
                    and fields[4] == "ta"
                    and (region is None or fields[3] == region)
                    and title_type not in NON_PRIMARY_TITLE_TYPES
                ):
                    attributes = fields[6] if len(fields) > 6 else ""
                    if title_type in NON_PRIMARY_TITLE_TYPES or attributes in NON_PRIMARY_TITLE_TYPES:
                        continue
                    batch.append((fields[0], fields[2]))
                if len(batch) >= 10_000:
                    connection.executemany("INSERT OR IGNORE INTO tamil_titles VALUES (?, ?)", batch)
                    batch.clear()
        if batch:
            connection.executemany("INSERT OR IGNORE INTO tamil_titles VALUES (?, ?)", batch)
        connection.execute("CREATE INDEX tamil_titles_tconst ON tamil_titles (tconst)")
        connection.execute("INSERT INTO metadata VALUES ('source_signature', ?)", (repr(source_signature),))
        connection.commit()
    os.replace(temporary, index)


def count_rows(path: str | Path) -> int:
    return sum(1 for _ in _rows(path))


class WikidataTamilVerifier:
    """Verify spoken Tamil for IMDb IDs whose AKA rows are ambiguous.

    IMDb's non-commercial bulk files expose localized AKA languages, not a
    dependable original/spoken-language field. Wikidata's P364 original
    language property provides the final precision check for this use case.
    Unknown titles are rejected rather than allowing obvious foreign films
    into the generated list.
    """

    endpoint = "https://query.wikidata.org/sparql"

    def __init__(self, cache_path: str | Path):
        self.cache_path = Path(cache_path)
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.cache: dict[str, bool] = {str(key): bool(item) for key, item in value.items()}
        except (OSError, json.JSONDecodeError, AttributeError):
            self.cache = {}

    def verify(self, movies: list[Movie]) -> set[str]:
        ids = [movie.tconst for movie in movies]
        pending = [tconst for tconst in ids if tconst not in self.cache]
        for start in range(0, len(pending), 50):
            chunk = pending[start:start + 50]
            accepted = self._query(chunk)
            for tconst in chunk:
                self.cache[tconst] = tconst in accepted
            self._save()
        return {tconst for tconst in ids if self.cache.get(tconst, False)}

    def _query(self, ids: list[str]) -> set[str]:
        values = " ".join(json.dumps(tconst) for tconst in ids)
        query = (
            "SELECT DISTINCT ?imdb WHERE { "
            f"VALUES ?imdb {{ {values} }} "
            "?item wdt:P345 ?imdb ; wdt:P364 wd:Q5885 . }"
        )
        url = self.endpoint + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/sparql-results+json", "User-Agent": "tamil-movie-music/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network-dependent
            raise RuntimeError("Wikidata Tamil-language verification failed") from exc
        return {
            binding["imdb"]["value"]
            for binding in payload.get("results", {}).get("bindings", [])
            if binding.get("imdb", {}).get("value")
        }

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".part")
        temporary.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.cache_path)


def iter_verified_tamil_movies(movies: Iterator[Movie], verifier: WikidataTamilVerifier, batch_size: int = 50) -> Iterator[Movie]:
    batch: list[Movie] = []
    for movie in movies:
        batch.append(movie)
        if len(batch) >= batch_size:
            accepted = verifier.verify(batch)
            yield from (movie for movie in batch if movie.tconst in accepted)
            batch.clear()
    if batch:
        accepted = verifier.verify(batch)
        yield from (movie for movie in batch if movie.tconst in accepted)
