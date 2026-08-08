# Development progress

This document records the complete work completed so far, the decisions that shaped the implementation, the current crash-recovery design, and the remaining operational step.

No Wasabi access key, secret key, GitHub token, or other credential is printed here. The one-click notebook contains the requested private Wasabi configuration, but this progress record deliberately uses only redacted names and paths.

## Objective

Build a Tamil movie soundtrack index that:

- discovers Tamil feature films from IMDb;
- handles IMDb’s anti-bot/browser behavior;
- finds matching Apple Music albums and tracks;
- writes an oldest-year-first Markdown song list;
- updates progress continuously;
- survives Colab VM/kernel crashes;
- resumes from remote checkpoints on the next Run all;
- uploads intermediate and final artifacts to Wasabi;
- runs without manual configuration in the final notebook.

## Important corrections made

### The original 79-title result was not the Tamil movie total

The first implementation used a strict secondary verification path and produced 79 accepted titles. That was a verification subset, not the full IMDb Tamil feature-film search result. It was removed as the primary source of truth.

The current source of truth is IMDb’s own feature-film search:

```text
https://www.imdb.com/search/title/?title_type=feature&languages=ta&sort=year,asc
```

IMDb’s current web UI renders a dynamic **50 more** button. URL `start=` offsets were tested and could return the first page repeatedly, so the crawler now opens the page with Scrapling/Chromium and clicks the dynamic control. The crawler waits for the DOM result count to grow and retries a click up to three times.

### The old 250-result pagination failure

Using `count=250` and URL offsets appeared to work for the first page but later offsets could be empty or duplicate the first page. The pipeline now loads 50-result UI pages and commits each page’s newly discovered IDs in 10-ID units.

### The page-6 failure and stale-cache fix

During the GUI run, page 5 committed 250 IDs successfully. Page 6 failed because one dynamic click had not completed, and the resulting duplicate HTML was cached. The pipeline was changed to:

1. wait for the number of title links to increase;
2. retry the click up to three times;
3. recognize a page that contains the “50 more” control but no new IDs;
4. rename the stale cached page with a `.stale` suffix;
5. refetch the page using the retrying browser action;
6. continue from the existing remote checkpoint.

The saved page-5 Wasabi checkpoint is therefore reusable; users should not delete it before rerunning the updated notebook.

## Current implementation

### Repository structure

```text
README.md                         User runbook and architecture summary
progress.md                       This detailed development record
pyproject.toml                    Package metadata and dependencies
scripts/colab_pipeline.py         Self-contained one-click pipeline
scripts/build_colab_notebook.py   Generates the notebook from the pipeline
tamil_movie_music_colab.ipynb     Generated Run all notebook
src/tamil_music/                  Local CLI/library implementation
tests/                            Automated unit and recovery tests
```

### Data sources

IMDb bulk files downloaded by both implementations:

- `title.akas.tsv.gz`
- `title.basics.tsv.gz`
- `title.crew.tsv.gz`

IMDb search identifies the Tamil feature-film IDs. `title.basics.tsv.gz` supplies the primary title, original title, year, and movie type. Only `titleType == movie` records are converted into processing candidates.

Apple’s public iTunes Search and Lookup endpoints provide album and track metadata. The matcher normalizes titles, removes soundtrack suffixes, applies a configurable score threshold, and only emits Apple catalog tracks with stable `music.apple.com` links.

## Checkpoint design

The remote store is Wasabi’s S3-compatible API in the configured Tokyo region. The configured private prefix is `tamil-movie-music/`. The published pipeline reads credentials from Colab Secret/environment names; no literal credential is stored in the repository or this document.

### Commit protocol

Every checkpoint is uploaded to a unique immutable snapshot path:

```text
<prefix>/<stage>/snapshots/<sequence>-<timestamp>/<artifact>
```

The pipeline uploads all artifacts and verifies their SHA-256 hashes in a manifest. It writes:

```text
<prefix>/<stage>/LATEST.json
```

last. `LATEST.json` is the commit point. A crash before the manifest upload leaves the previous manifest pointing at the last complete snapshot. A crash after the manifest upload but before local files are written is safe because the next run restores the committed remote snapshot.

### IMDb discovery stage

Every 50-result dynamic page is divided into five 10-ID checkpoint units. Each unit uploads:

- `imdb_state.json` — query identity, collected IDs, next page, next click count, and completion flag;
- `imdb_ids.json` — the ordered collected IMDb IDs;
- `latest_page.html` — the HTML used for the current checkpoint.

The remote IMDb manifest is read before discovery begins. If its query identity matches the current dynamic-search implementation, the crawler restores the ordered IDs and resumes at the saved page/click position. A completed IMDb manifest avoids repeating discovery entirely.

### Songlist stage

After each 10 newly processed movies, the pipeline builds one complete state containing:

- processed IMDb IDs;
- all deduplicated album records;
- Apple track records and URLs;
- unmatched/error records;
- Markdown output;
- JSON output;
- unmatched JSON output;
- checkpoint JSON.

The entire batch is uploaded as one `songlist` snapshot. Only after the remote commit succeeds are the local files atomically replaced.

At normal completion, the same complete artifact set is committed with `final: true`. The stable final copies are written under:

```text
<prefix>/final/
```

This means the final songlist remains available even if the Colab VM disappears after completion.

### Crash cases

| Crash point | Next-run behavior |
| --- | --- |
| During IMDb browser fetch | Refetches the current page; prior 10-ID commits remain safe. |
| After a stale IMDb HTML cache is written | Detects duplicate/no-progress HTML, renames it, and refetches. |
| During an IMDb artifact upload | `LATEST.json` remains on the previous valid snapshot. |
| After IMDb manifest commit but before local write | Restores the committed remote state. |
| During Apple processing of one movie | That incomplete movie is not in the committed processed set and is retried. |
| During a songlist artifact upload | Previous songlist `LATEST.json` remains the resume point. |
| After a songlist manifest commit but before local output write | Remote checkpoint restores the complete batch. |
| After final snapshot upload | Stable final objects and the final manifest remain remote. |
| Entire Colab VM is lost | A new Run all redownloads reproducible source/cache inputs and restores remote checkpoints. |

## Notebook behavior

The generated notebook contains seven cells:

1. Markdown overview.
2. `%pip install "scrapling[fetchers]" boto3`.
3. `patchright install --with-deps chromium`.
4. Colab Secret loading for the two credential names.
5. `%%writefile /content/tamil_movie_music_pipeline.py` containing the complete self-contained program.
6. A streaming subprocess runner using `python -u`, so the GUI shows progress and the real traceback.
7. Final Markdown diagnostics.

The Run cell deliberately streams stdout and stderr together. A failure now reports the actual pipeline exception rather than only a generic `CalledProcessError` wrapper.

## Validation completed

Automated validation currently passes:

```text
10 passed
```

Covered areas include:

- Tamil title filtering and movie type handling;
- foreign translated-title rejection;
- album/title matching;
- stable Markdown output and Apple URLs;
- local checkpoint round-trip;
- Wasabi immutable manifest behavior;
- partial upload recovery;
- final stable artifact upload.

Live development smoke tests also completed:

- Scrapling/Chromium successfully fetched IMDb.
- Dynamic IMDb pages 1–6 produced 300 distinct IDs.
- IMDb checkpoint commits occurred in 10-ID units.
- Apple search and lookup returned live albums/tracks.
- Wasabi PUT, GET, hash verification, and manifest restore succeeded.
- A one-movie run produced 10 albums and 51 songs.
- A second run restored the IMDb and songlist state from Wasabi.
- A stale page-6 cache was recovered and advanced to 300 IDs.

Temporary validation prefixes were removed after testing. No production checkpoint prefix was deleted by the validation cleanup.

The full several-thousand-title production crawl has not been completed by the development environment. It is intentionally a long-running Colab operation and should be started by the user with the final notebook’s Run all.

## Current operating instructions

1. Add Colab Secrets named `WASABI_ACCESS_KEY` and `WASABI_SECRET_KEY` once.
2. Use the regenerated [tamil_movie_music_colab.ipynb](tamil_movie_music_colab.ipynb), replacing any older Drive copy.
3. Run all cells from a fresh or existing Colab session.
4. If the session crashes, run the same notebook again without deleting the Wasabi prefix or local checkpoint.
5. Watch the streamed log for `Resuming IMDb checkpoint`/`Restored songlist checkpoint` messages and batch commit messages.
6. Download the final Markdown and JSON from `/content/tamil-movie-music-run/`, or use the stable `final/` objects in Wasabi.

## Remaining work

- Run the complete production crawl to exhaustion and record the final IMDb, movie, album, and song counts.
- Inspect unmatched titles and tune the Apple matching threshold only if the resulting false-positive/false-negative tradeoff requires it.
- Rotate the Wasabi credentials if the Colab Secret values have been exposed.
- Keep the generated notebook and README synchronized whenever the embedded pipeline changes; use `python scripts/build_colab_notebook.py` after editing `scripts/colab_pipeline.py`.
