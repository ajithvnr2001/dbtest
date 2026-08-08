from datetime import date

from tamil_music.models import Album, Movie, Track
from tamil_music.output import render_markdown


def test_markdown_has_stable_working_urls():
    movie = Movie("tt1", "Roja", 1992)
    album = Album(7, "Roja (Original Motion Picture Soundtrack)", "A.R. Rahman", 1993, (Track(8, "Song", "https://music.apple.com/in/song/8"),), movie)
    output = render_markdown([album], generated=date(2026, 8, 8))
    assert output.startswith("# Tamil movie soundtracks (1 albums, 1 songs) — generated 2026-08-08")
    assert "Roja (Original Motion Picture Soundtrack) (1993):" in output
    assert "  https://music.apple.com/in/song/8" in output
