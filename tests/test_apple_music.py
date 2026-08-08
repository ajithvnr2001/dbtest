from tamil_music.apple_music import album_belongs_to_movie, album_score, normalise
from tamil_music.models import Movie


def test_normalise_removes_release_suffixes():
    assert normalise("Roja (Original Motion Picture Soundtrack)") == "roja"


def test_album_score_prefers_same_movie():
    movie = Movie("tt1", "Roja", 1992)
    assert album_score(movie, "Roja (Original Motion Picture Soundtrack)", 1993) > album_score(movie, "Coolie (Original Motion Picture Soundtrack)", 2025)


def test_album_match_does_not_accept_title_only_as_suffix():
    movie = Movie("tt1", "Nayakan", 1987)
    assert album_belongs_to_movie(movie, "Nayakan (Original Motion Picture Soundtrack)")
    assert not album_belongs_to_movie(movie, "Brahmanda Nayakan")
