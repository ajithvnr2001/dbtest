# Tamil movie soundtrack index

This project discovers Tamil feature films from IMDb, finds matching Tamil movie soundtracks in Apple’s public catalog, and writes Apple Music song links in oldest-to-newest year order.

The primary deliverable is a self-contained Google Colab notebook: [tamil_movie_music_colab.ipynb](tamil_movie_music_colab.ipynb). The notebook embeds the complete program in a `%%writefile` cell, installs its dependencies, installs Chromium, runs the pipeline, checkpoints to Wasabi, and produces the final files.

## What the pipeline does

1. Downloads IMDb’s `title.akas.tsv.gz`, `title.basics.tsv.gz`, and `title.crew.tsv.gz` files.
2. Uses IMDb’s own Tamil feature-film search as the authoritative title list. IMDb’s current UI uses a dynamic “50 more” button, so the crawler loads that button incrementally instead of relying on URL `start=` offsets that can repeat the first page.
3. Commits IMDb discovery state to Wasabi after every 10 discovered IDs.
4. Joins IMDb IDs to `title.basics.tsv.gz`, retaining actual `movie` records and their years.
5. Searches Apple’s public iTunes catalog for each movie and looks up matching album tracks.
6. Keeps only Apple-provided catalog track IDs and `music.apple.com` URLs.
7. Deduplicates albums, sorts the output by year ascending, and writes Markdown, JSON, and unmatched-title reports.
8. Commits the complete songlist state after every 10 processed movies and performs a final upload when the run ends.

## One-click Colab run

The notebook is designed for GUI **Run all**. It has seven cells:

1. Markdown overview.
2. Install Scrapling fetchers and boto3.
3. Install Chromium and its system dependencies.
4. Load Wasabi values from Colab Secrets.
5. Write the complete self-contained pipeline to `/content/tamil_movie_music_pipeline.py`.
6. Execute it with unbuffered, live log streaming.
7. Print final output diagnostics.

The Wasabi bucket, Tokyo region, and endpoint are fixed in the notebook. Credentials are loaded from Colab Secrets named `WASABI_ACCESS_KEY` and `WASABI_SECRET_KEY`; literal credential values are intentionally excluded from the repository and documentation. This keeps the published Git history safe while requiring only a one-time Secret setup in Colab.

The notebook uses:

- Bucket: `checkpointsvnr`
- Region: `ap-northeast-1` (Tokyo)
- Endpoint: `https://s3.ap-northeast-1.wasabisys.com`
- Prefix: `tamil-movie-music/`

The official Wasabi service URL for the Tokyo region is documented [here](https://docs.wasabi.com/docs/service-urls-for-wasabis-storage-regions).

If Colab crashes, upload/run the same notebook again. The pipeline restores the newest valid Wasabi snapshot and continues; it does not restart the completed IMDb or songlist work.

## Wasabi crash-recovery layout

Every remote batch is uploaded as immutable objects first. The `LATEST.json` manifest is written last and acts as the commit pointer. If a process dies during an upload, the previous manifest still points to a complete snapshot.

The important remote layout is:

```text
tamil-movie-music/
  imdb/
    LATEST.json
    snapshots/<sequence-and-timestamp>/
      imdb_state.json
      imdb_ids.json
      latest_page.html
  songlist/
    LATEST.json
    snapshots/<sequence-and-timestamp>/
      tamil-movie-songs.md
      tamil-movie-songs.json
      tamil-movie-songs.unmatched.json
      tamil-movie-songs.checkpoint.json
  final/
    tamil-movie-songs.md
    tamil-movie-songs.json
    tamil-movie-songs.unmatched.json
    tamil-movie-songs.checkpoint.json
```

IMDb recovery stores the collected ID list, the next dynamic page/click position, and the latest HTML page. Songlist recovery stores the processed IMDb IDs, all discovered album/track records, unmatched records, and the three output files. Each manifest includes SHA-256 hashes; mismatched or incomplete artifacts are rejected.

The raw IMDb bulk downloads and Apple response cache are not required for correctness and are recreated locally after a VM loss. A crash during a movie batch can repeat that incomplete batch safely; completed batches are restored from Wasabi.

## Output files

The notebook writes these files under `/content/tamil-movie-music-run/`:

- `tamil-movie-songs.md` — human-readable song links, sorted oldest year first.
- `tamil-movie-songs.json` — structured album, movie, score, and track metadata.
- `tamil-movie-songs.unmatched.json` — movies for which no qualifying Apple album was found, including errors.
- `tamil-movie-songs.checkpoint.json` — local copy of the latest songlist state.
- `data/imdb/` — IMDb bulk files.
- `data/cache/imdb-search/` — local dynamic IMDb page cache.
- `data/cache/apple/` — local Apple API response cache.

The final Markdown header reports album and song counts. A missing Apple album is recorded as unmatched; it is not silently treated as a successful match.

## Local installation and CLI

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
patchright install --with-deps chromium
```

Run a small local smoke test:

```bash
tamil-music --limit 2 --output sample.md --json-output sample.json --keep-empty
```

Run the local CLI build:

```bash
tamil-music \
  --data-dir data/imdb \
  --cache-dir data/apple-cache \
  --output tamil-movie-songs.md \
  --json-output tamil-movie-songs.json \
  --keep-empty
```

The CLI has local batch checkpoints beside the Markdown output. The Wasabi-backed crash-proof workflow is implemented by the Colab pipeline in `scripts/colab_pipeline.py` and embedded into the notebook by `scripts/build_colab_notebook.py`.

Useful CLI controls:

- `--limit N` — process only the first N movies for a smoke test.
- `--batch-size N` — local output/checkpoint update interval.
- `--threshold 0.54` — Apple album/movie matching threshold.
- `--pause 0.15` — delay between uncached Apple requests.
- `--no-download` — reuse existing IMDb files.
- `--keep-empty` — write unmatched records.

## Tests and validation

Run the automated tests with:

```bash
python -m pytest -q
```

The test suite covers title filtering, album matching, output formatting, local checkpoint round-trips, immutable Wasabi manifest behavior, partial-upload recovery, and final-artifact publication.

The live smoke validation performed during development confirmed:

- IMDb browser access and dynamic pagination through at least six 50-result loads.
- Ten-ID IMDb checkpoint commits.
- Apple search and album-track lookup.
- Wasabi snapshot PUT/GET and manifest verification.
- Songlist final upload.
- A second run restoring IMDb and songlist state from Wasabi.
- Recovery from a stale cached IMDb page.

The complete several-thousand-title production crawl has not been claimed as finished by the development environment; it is the long-running job started by Colab **Run all**.

## Security

The published notebook contains only Secret names, never Wasabi credential values. Keep the Colab Secret values private and rotate them in Wasabi if they are exposed.

No GitHub token is needed by the application itself. GitHub authentication is only used by the development workflow to publish this repository.
