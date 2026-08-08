from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from scrapling.fetchers import Fetcher, StealthyFetcher


# Fixed notebook values: Run All is sufficient.
ROOT = Path(os.getenv("TAMIL_PIPELINE_ROOT", "/content/tamil-movie-music-run"))
IMDB_DIR = ROOT / "data" / "imdb"
CACHE_DIR = ROOT / "data" / "cache"
SEARCH_CACHE = CACHE_DIR / "imdb-search"
APPLE_CACHE = CACHE_DIR / "apple"
OUTPUT_MD = ROOT / "tamil-movie-songs.md"
OUTPUT_JSON = ROOT / "tamil-movie-songs.json"
CHECKPOINT = ROOT / "tamil-movie-songs.checkpoint.json"
BATCH_SIZE = 10
APPLE_PAUSE_SECONDS = 0.15
MATCH_THRESHOLD = 0.54
# IMDb currently serves reliable pagination at 50 results per page. Larger
# count values can make later start offsets appear empty and truncate the run.
SEARCH_PAGE_SIZE = 50
PIPELINE_VERSION = 3
RUN_LIMIT = int(os.getenv("TAMIL_PIPELINE_LIMIT", "0")) or None
SEARCH_PAGE_LIMIT = int(os.getenv("TAMIL_PIPELINE_SEARCH_PAGE_LIMIT", "0")) or None

# Credentials are deliberately injected by the notebook/Colab Secret
# environment and are never stored in the repository.
WASABI_REGION = os.getenv("WASABI_REGION", "ap-northeast-1")
WASABI_BUCKET = os.getenv("WASABI_BUCKET", "checkpointsvnr")
WASABI_ENDPOINT = os.getenv("WASABI_ENDPOINT", "https://s3.ap-northeast-1.wasabisys.com")
WASABI_PREFIX = os.getenv("WASABI_PREFIX", "tamil-movie-music")


class WasabiStore:
    """Small S3-compatible snapshot store with a last-manifest commit point."""

    def __init__(self, bucket: str, region: str, endpoint: str, access_key: str, secret_key: str, prefix: str):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("boto3 is required for Wasabi checkpoints") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    @classmethod
    def from_environment(cls) -> "WasabiStore":
        access_key = os.getenv("WASABI_ACCESS_KEY") or os.getenv("WASABI_ACCESS_KEY_ID")
        secret_key = os.getenv("WASABI_SECRET_KEY") or os.getenv("WASABI_SECRET_ACCESS_KEY")
        if not access_key or not secret_key:
            raise RuntimeError("Missing WASABI_ACCESS_KEY/WASABI_SECRET_KEY. Add them as Colab Secrets or environment variables.")
        return cls(
            bucket=WASABI_BUCKET,
            region=WASABI_REGION,
            endpoint=WASABI_ENDPOINT,
            access_key=access_key,
            secret_key=secret_key,
            prefix=WASABI_PREFIX,
        )

    def _key(self, *parts: str) -> str:
        return "/".join((self.prefix, *[part.strip("/") for part in parts]))

    def _put(self, key: str, payload: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=payload, ContentType=content_type)

    def _get(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise
        return response["Body"].read()

    def load_latest(self, stage: str) -> dict | None:
        manifest_bytes = self._get(self._key(stage, "LATEST.json"))
        if manifest_bytes is None:
            return None
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("pipeline_version") != PIPELINE_VERSION or manifest.get("stage") != stage:
            return None
        artifacts = {}
        for name, expected_hash in manifest.get("artifacts", {}).items():
            payload = self._get(f"{manifest['snapshot']}/{name}")
            if payload is None or hashlib.sha256(payload).hexdigest() != expected_hash:
                raise RuntimeError(f"Wasabi checkpoint is incomplete or corrupt: {stage}/{name}")
            artifacts[name] = payload
        return {"manifest": manifest, "artifacts": artifacts}

    def commit(self, stage: str, sequence: int, artifacts: dict[str, tuple[bytes, str]], final: bool = False) -> None:
        snapshot = self._key(stage, "snapshots", f"{sequence:012d}-{int(time.time() * 1000)}")
        hashes = {}
        for name, (payload, content_type) in artifacts.items():
            self._put(f"{snapshot}/{name}", payload, content_type)
            hashes[name] = hashlib.sha256(payload).hexdigest()
        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "stage": stage,
            "sequence": sequence,
            "snapshot": snapshot,
            "artifacts": hashes,
            "final": final,
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }
        if final:
            for name, (payload, content_type) in artifacts.items():
                self._put(self._key("final", name), payload, content_type)
        # The manifest is the commit point. A crash before this upload leaves
        # the previous complete snapshot as the resume point. For a final
        # commit, the stable final objects are uploaded before this pointer.
        self._put(self._key(stage, "LATEST.json"), json.dumps(manifest, sort_keys=True).encode(), "application/json")
        print(f"Wasabi committed {stage} sequence={sequence} final={final}")

DATASETS = {
    "akas": "https://datasets.imdbws.com/title.akas.tsv.gz",
    "basics": "https://datasets.imdbws.com/title.basics.tsv.gz",
    "crew": "https://datasets.imdbws.com/title.crew.tsv.gz",
}


@dataclass
class Movie:
    tconst: str
    title: str
    year: int | None
    original_title: str | None


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def download_datasets() -> dict[str, Path]:
    IMDB_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, url in DATASETS.items():
        destination = IMDB_DIR / f"title.{name}.tsv.gz"
        if not destination.exists() or destination.stat().st_size == 0:
            temporary = destination.with_suffix(destination.suffix + ".part")
            request = urllib.request.Request(url, headers={"User-Agent": "tamil-movie-music-colab/1.0"})
            with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temporary.replace(destination)
        result[name] = destination
        print(f"IMDb {name}: {destination} ({destination.stat().st_size / 1024 / 1024:.1f} MB)")
    return result


def imdb_tamil_ids(remote: WasabiStore) -> list[str]:
    SEARCH_CACHE.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    seen: set[str] = set()
    page_number = 1
    click_count = 0
    query = {"title_type": "feature", "languages": "ta", "sort": "year,asc", "page_size": SEARCH_PAGE_SIZE, "pagination": "load-more"}
    latest = remote.load_latest("imdb")
    if latest:
        try:
            state = json.loads(latest["artifacts"]["imdb_state.json"].decode("utf-8"))
            if state.get("query") == query:
                ids = list(dict.fromkeys(state.get("ids", [])))
                seen = set(ids)
                page_number = int(state.get("next_page", 1))
                click_count = int(state.get("next_clicks", max(0, page_number - 1)))
                if state.get("done"):
                    print(f"Restored completed IMDb checkpoint: {len(ids)} IDs")
                    return ids
                print(f"Resuming IMDb checkpoint: {len(ids)} IDs, next load-more page={page_number}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            print("Ignoring invalid IMDb Wasabi checkpoint; starting fresh")
    refetched_pages: set[int] = set()
    while True:
        url = "https://www.imdb.com/search/title/?title_type=feature&languages=ta&sort=year,asc"
        cache_file = SEARCH_CACHE / f"load-more-{page_number:06d}.html"
        if cache_file.exists():
            html = cache_file.read_text(encoding="utf-8")
        else:
            def click_more(page):
                for _ in range(click_count):
                    loaded_before = page.locator('a[href*="/title/tt"]').count()
                    loaded = False
                    for attempt in range(3):
                        button = page.get_by_role("button", name=re.compile(r"50 more", re.IGNORECASE))
                        if not button.count():
                            break
                        button.last.click()
                        try:
                            page.wait_for_function(
                                "n => document.querySelectorAll('a[href*=\\\"/title/tt\\\"]').length > n",
                                arg=loaded_before,
                                timeout=15_000,
                            )
                            loaded = True
                            break
                        except Exception:
                            page.wait_for_timeout(2_000)
                            if page.locator('a[href*="/title/tt"]').count() > loaded_before:
                                loaded = True
                                break
                    if not loaded:
                        raise RuntimeError("IMDb 50-more click did not load additional results after 3 attempts")

            # IMDb's current page uses a dynamic "50 more" button; URL start
            # offsets return the first page again. Rebuild one additional
            # loaded page per checkpoint so every committed batch is resumable.
            page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=90_000, page_action=click_more)
            body = page.body
            html = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
            if "/title/tt" not in html:
                raise RuntimeError(f"IMDb search returned no results at load-more page={page_number}")
            atomic_text(cache_file, html)
        page_ids = list(dict.fromkeys(re.findall(r"/title/(tt\d+)", html)))
        fresh = [item for item in page_ids if item not in seen]
        pagination_start = html.find('data-testid="search-pagination"')
        pagination = html[pagination_start:pagination_start + 6000] if pagination_start >= 0 else ""
        has_more = "50 more" in pagination
        if not fresh and has_more:
            if page_number not in refetched_pages:
                refetched_pages.add(page_number)
                if cache_file.exists():
                    cache_file.replace(cache_file.with_name(cache_file.name + ".stale"))
                print(f"IMDb page {page_number} cache was stale; refetching with click retries")
                continue
            raise RuntimeError(f"IMDb load-more page {page_number} did not add new IDs")
        chunks = [fresh[index:index + 10] for index in range(0, len(fresh), 10)] or [[]]
        for chunk_index, chunk in enumerate(chunks):
            ids.extend(chunk)
            seen.update(chunk)
            page_complete = chunk_index == len(chunks) - 1
            natural_done = page_complete and not has_more
            forced_stop = page_complete and SEARCH_PAGE_LIMIT is not None and page_number >= SEARCH_PAGE_LIMIT
            state = {
                "pipeline_version": PIPELINE_VERSION,
                "query": query,
                "ids": ids,
                "next_page": page_number + 1 if page_complete else page_number,
                "next_clicks": click_count + 1 if page_complete else click_count,
                "done": natural_done,
            }
            remote.commit("imdb", page_number * 1000 + (chunk_index + 1) * 10, {
                "imdb_state.json": (json.dumps(state, ensure_ascii=False).encode(), "application/json"),
                "imdb_ids.json": (json.dumps(ids, ensure_ascii=False).encode(), "application/json"),
                "latest_page.html": (html.encode("utf-8"), "text/html; charset=utf-8"),
            }, final=natural_done)
            print(f"IMDb Tamil search page={page_number}: +{len(chunk)} IDs; total={len(ids)}")
        if natural_done or forced_stop:
            return ids
        page_number += 1
        click_count += 1


def movies_from_basics(path: Path, ids: list[str]) -> list[Movie]:
    wanted = set(ids)
    found: dict[str, Movie] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("titleType") != "movie" or row.get("tconst") not in wanted:
                continue
            year = int(row["startYear"]) if row.get("startYear", "").isdigit() else None
            found[row["tconst"]] = Movie(row["tconst"], row["primaryTitle"], year, None if row.get("originalTitle") in (None, r"\N") else row["originalTitle"])
    ordered = [found[tconst] for tconst in ids if tconst in found]
    print(f"IMDb search IDs: {len(ids)}; basics movie matches: {len(ordered)}")
    return ordered


def normalise(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def year(value: str | None) -> int | None:
    match = re.match(r"(\d{4})", value or "")
    return int(match.group(1)) if match else None


class Apple:
    def __init__(self):
        APPLE_CACHE.mkdir(parents=True, exist_ok=True)

    def get(self, url: str) -> dict:
        path = APPLE_CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        page = Fetcher.get(url, headers={"Accept": "application/json", "User-Agent": "tamil-movie-music-colab/1.0"}, timeout=30)
        body = page.body
        data = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        atomic_text(path, json.dumps(data))
        time.sleep(APPLE_PAUSE_SECONDS)
        return data

    def url(self, endpoint: str, params: dict) -> str:
        return "https://itunes.apple.com/" + endpoint + "?" + urllib.parse.urlencode({"country": "IN", **params})

    def albums(self, movie: Movie) -> list[dict]:
        result: dict[int, dict] = {}
        for term in (movie.title, movie.title + " soundtrack"):
            data = self.get(self.url("search", {"term": term, "media": "music", "entity": "album", "attribute": "albumTerm", "limit": 200}))
            for item in data.get("results", []):
                if item.get("collectionId"):
                    result[int(item["collectionId"])] = item
        movie_name = normalise(movie.title)
        ranked = []
        for item in result.values():
            name = item.get("collectionName", "")
            album_name = normalise(name)
            if not album_name.startswith(movie_name) and f"from {movie_name}" not in album_name:
                continue
            if item.get("trackCount") == 1 and "single" in name.casefold():
                continue
            similarity = 1.0 if album_name.startswith(movie_name) else 0.0
            score = similarity + (0.15 if movie.year and year(item.get("releaseDate")) and abs(movie.year - year(item.get("releaseDate"))) <= 2 else 0)
            if score >= MATCH_THRESHOLD:
                ranked.append((score, item))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:12]]

    def tracks(self, collection_id: int) -> list[dict]:
        data = self.get(self.url("lookup", {"id": collection_id, "entity": "song", "limit": 200}))
        tracks = []
        for item in data.get("results", []):
            if item.get("wrapperType") != "track" or not item.get("trackId") or not item.get("trackViewUrl", "").startswith("https://music.apple.com/"):
                continue
            track_id = int(item["trackId"])
            tracks.append({"track_id": track_id, "name": item.get("trackName", ""), "url": f"https://music.apple.com/in/song/{track_id}"})
        return list({track["track_id"]: track for track in tracks}.values())


def load_state(remote: WasabiStore) -> tuple[set[str], list[dict], list[dict]]:
    latest = remote.load_latest("songlist")
    if latest:
        try:
            state = json.loads(latest["artifacts"]["tamil-movie-songs.checkpoint.json"].decode("utf-8"))
            if state.get("threshold") == MATCH_THRESHOLD:
                for name, payload in latest["artifacts"].items():
                    target = ROOT / name
                    atomic_text(target, payload.decode("utf-8"))
                print(f"Restored songlist checkpoint from Wasabi: {len(state.get('processed', []))} movies")
                return set(state.get("processed", [])), state.get("albums", []), state.get("unmatched", [])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            print("Ignoring invalid songlist Wasabi checkpoint; checking local files")
    if not CHECKPOINT.exists():
        return set(), [], []
    state = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        return set(), [], []
    return set(state.get("processed", [])), state.get("albums", []), state.get("unmatched", [])


def save_outputs(remote: WasabiStore, processed: set[str], albums: list[dict], unmatched: list[dict], final: bool) -> None:
    unique = {album["collection_id"]: album for album in albums}
    ordered = sorted(unique.values(), key=lambda item: (item.get("year") or 0, item["name"].casefold(), item["collection_id"]))
    song_count = sum(len(item["tracks"]) for item in ordered)
    lines = [f"# Tamil movie soundtracks ({len(ordered)} albums, {song_count} songs) — generated {time.strftime('%Y-%m-%d')}", ""]
    for album in ordered:
        suffix = f" ({album['year']})" if album.get("year") else ""
        lines.append(f"{album['name']}{suffix}:")
        lines.extend(f"  {track['url']}" for track in album["tracks"])
        lines.append("")
    state = json.dumps({"pipeline_version": PIPELINE_VERSION, "threshold": MATCH_THRESHOLD, "processed": sorted(processed), "albums": list(unique.values()), "unmatched": unmatched}, ensure_ascii=False, indent=2) + "\n"
    artifacts = {
        "tamil-movie-songs.md": ("\n".join(lines).encode("utf-8"), "text/markdown; charset=utf-8"),
        "tamil-movie-songs.json": (json.dumps(ordered, ensure_ascii=False, indent=2).encode("utf-8") + b"\n", "application/json"),
        "tamil-movie-songs.unmatched.json": (json.dumps(unmatched, ensure_ascii=False, indent=2).encode("utf-8") + b"\n", "application/json"),
        "tamil-movie-songs.checkpoint.json": (state.encode("utf-8"), "application/json"),
    }
    # Upload the complete batch before changing local state. The remote
    # manifest becomes the durable commit point for crash recovery.
    remote.commit("songlist", len(processed), artifacts, final=final)
    for name, (payload, _) in artifacts.items():
        atomic_text(ROOT / name, payload.decode("utf-8"))
    print(f"UPDATED: {len(processed)} movies, {len(ordered)} albums, {song_count} songs")


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    remote = WasabiStore.from_environment()
    paths = download_datasets()
    ids = imdb_tamil_ids(remote)
    movies = movies_from_basics(paths["basics"], ids)
    if RUN_LIMIT is not None:
        movies = movies[:RUN_LIMIT]
        print(f"Smoke limit enabled: {RUN_LIMIT} movies")
    processed, albums, unmatched = load_state(remote)
    apple = Apple()
    pending = 0
    for movie in movies:
        if movie.tconst in processed:
            continue
        try:
            for item in apple.albums(movie):
                tracks = apple.tracks(int(item["collectionId"]))
                if tracks:
                    albums.append({"collection_id": int(item["collectionId"]), "name": item.get("collectionName", movie.title), "artist": item.get("artistName", ""), "year": year(item.get("releaseDate")) or movie.year, "source_movie": asdict(movie), "tracks": tracks})
            if not any(album.get("source_movie", {}).get("tconst") == movie.tconst for album in albums):
                unmatched.append({"tconst": movie.tconst, "title": movie.title, "year": movie.year})
        except Exception as exc:
            print(f"WARNING {movie.tconst} {movie.title}: {exc}")
            unmatched.append({"tconst": movie.tconst, "title": movie.title, "year": movie.year, "error": str(exc)})
        processed.add(movie.tconst)
        pending += 1
        if pending >= BATCH_SIZE:
            save_outputs(remote, processed, albums, unmatched, final=False)
            pending = 0
    save_outputs(remote, processed, albums, unmatched, final=True)
    print(f"DONE: IMDb movies={len(movies)}, processed={len(processed)}, albums={len({a['collection_id'] for a in albums})}, songs={sum(len(a['tracks']) for a in albums)}")


if __name__ == "__main__":
    main()
