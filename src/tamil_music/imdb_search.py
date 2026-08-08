from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from .models import Movie

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError as exc:  # pragma: no cover - install-time error
    StealthyFetcher = None  # type: ignore[assignment]
    _SCRAPLING_ERROR = exc
else:
    _SCRAPLING_ERROR = None


class IMDbFeatureSearch:
    """Collect IMDb feature-film IDs from IMDb's own Tamil search result set."""

    def __init__(self, cache_dir: str | Path, page_size: int = 50):
        if StealthyFetcher is None:
            raise RuntimeError("Scrapling fetchers are required for IMDb search") from _SCRAPLING_ERROR
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # IMDb's current UI uses a 50-result dynamic "50 more" button;
        # URL start offsets can repeat the first page.
        self.page_size = 50

    def collect_ids(self, language: str = "ta") -> list[str]:
        all_ids: list[str] = []
        seen: set[str] = set()
        page_number = 1
        click_count = 0
        while True:
            url = f"https://www.imdb.com/search/title/?title_type=feature&languages={language}&sort=year,asc"
            html = self._fetch(url, click_count, page_number)
            page_ids = list(dict.fromkeys(re.findall(r"/title/(tt\d+)", html)))
            fresh = [tconst for tconst in page_ids if tconst not in seen]
            all_ids.extend(fresh)
            seen.update(fresh)
            marker = html.find('data-testid="search-pagination"')
            pagination = html[marker:marker + 6000] if marker >= 0 else ""
            has_more = "50 more" in pagination
            if not fresh and has_more:
                path = self.cache_dir / f"load-more-{page_number:06d}.html"
                if path.exists():
                    path.replace(path.with_name(path.name + ".stale"))
                    html = self._fetch(url, click_count, page_number)
                    page_ids = list(dict.fromkeys(re.findall(r"/title/(tt\d+)", html)))
                    fresh = [tconst for tconst in page_ids if tconst not in seen]
                    all_ids.extend(fresh)
                    seen.update(fresh)
                if not fresh:
                    raise RuntimeError(f"IMDb load-more page {page_number} did not add new IDs")
            print(f"IMDb Tamil search page={page_number}: {len(fresh)} new IDs; total={len(all_ids)}")
            if not has_more:
                break
            page_number += 1
            click_count += 1
        return all_ids

    def _fetch(self, url: str, click_count: int, page_number: int) -> str:
        path = self.cache_dir / f"load-more-{page_number:06d}.html"
        if path.exists() and path.stat().st_size:
            return path.read_text(encoding="utf-8")
        # Scrapling's browser timeout is expressed in milliseconds.
        def click_more(browser_page):
            for _ in range(click_count):
                loaded_before = browser_page.locator('a[href*="/title/tt"]').count()
                loaded = False
                for attempt in range(3):
                    button = browser_page.get_by_role("button", name=re.compile(r"50 more", re.IGNORECASE))
                    if not button.count():
                        break
                    button.last.click()
                    try:
                        browser_page.wait_for_function(
                            "n => document.querySelectorAll('a[href*=\\\"/title/tt\\\"]').length > n",
                            arg=loaded_before,
                            timeout=15_000,
                        )
                        loaded = True
                        break
                    except Exception:
                        browser_page.wait_for_timeout(2_000)
                        if browser_page.locator('a[href*="/title/tt"]').count() > loaded_before:
                            loaded = True
                            break
                if not loaded:
                    raise RuntimeError("IMDb 50-more click did not load additional results after 3 attempts")

        page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=90_000, page_action=click_more)
        body = page.body
        html = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
        if "/title/tt" not in html:
            raise RuntimeError(f"IMDb search returned no title results: {url}")
        path.write_text(html, encoding="utf-8")
        return html


def iter_movies_for_ids(basics_path: str | Path, tconsts: list[str], limit: int | None = None) -> Iterator[Movie]:
    wanted = set(tconsts)
    found = 0
    import gzip
    import csv

    with gzip.open(basics_path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("titleType") != "movie" or row.get("tconst") not in wanted:
                continue
            year_text = row.get("startYear", "")
            yield Movie(
                tconst=row["tconst"],
                title=row["primaryTitle"],
                original_title=None if row.get("originalTitle") in (None, r"\N") else row["originalTitle"],
                year=int(year_text) if year_text.isdigit() else None,
            )
            found += 1
            if limit is not None and found >= limit:
                return
