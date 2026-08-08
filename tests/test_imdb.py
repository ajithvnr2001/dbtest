import csv
import gzip

from tamil_music.imdb import _is_tamil_movie_title, iter_tamil_movies, iter_verified_tamil_movies, tamil_title_ids
from tamil_music.models import Movie


def write_tsv(path, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def test_tamil_movie_filter_is_streaming_and_type_aware(tmp_path):
    akas = tmp_path / "title.akas.tsv.gz"
    basics = tmp_path / "title.basics.tsv.gz"
    write_tsv(akas, [
        {"titleId": "tt1", "ordering": "1", "title": "தமிழ்", "region": "IN", "language": "ta", "types": "", "attributes": "", "isOriginalTitle": "0"},
        {"titleId": "tt3", "ordering": "1", "title": "Translated", "region": "IN", "language": "ta", "types": "alternative", "attributes": "", "isOriginalTitle": "0"},
        {"titleId": "tt2", "ordering": "1", "title": "Other", "region": "US", "language": "en", "types": "", "attributes": "", "isOriginalTitle": "0"},
    ])
    write_tsv(basics, [
        {"tconst": "tt1", "titleType": "movie", "primaryTitle": "Tamil Film", "originalTitle": "Tamil Film", "isAdult": "0", "startYear": "2020", "endYear": "\\N", "runtimeMinutes": "120", "genres": "Drama"},
        {"tconst": "tt2", "titleType": "movie", "primaryTitle": "English Film", "originalTitle": "English Film", "isAdult": "0", "startYear": "2020", "endYear": "\\N", "runtimeMinutes": "120", "genres": "Drama"},
        {"tconst": "tt1", "titleType": "tvSeries", "primaryTitle": "Series", "originalTitle": "Series", "isAdult": "0", "startYear": "2020", "endYear": "\\N", "runtimeMinutes": "", "genres": "Drama"},
    ])
    assert tamil_title_ids(akas) == {"tt1"}
    movies = list(iter_tamil_movies(basics, akas))
    assert [(movie.tconst, movie.title, movie.year) for movie in movies] == [("tt1", "Tamil Film", 2020)]


def test_tamil_filter_rejects_translated_foreign_title():
    assert not _is_tamil_movie_title("Ulakaṅkaḷiṉ pōr", "The War of the Worlds", "The War of the Worlds")
    assert _is_tamil_movie_title("தமிழ் படம்", "Tamil Film", "Tamil Film")


def test_verified_iterator_keeps_only_verified_ids():
    class FakeVerifier:
        def verify(self, movies):
            return {"tt2"}

    movies = [Movie("tt1", "Foreign", 2000), Movie("tt2", "Tamil", 2001)]
    assert list(iter_verified_tamil_movies(iter(movies), FakeVerifier(), batch_size=2)) == [movies[1]]
