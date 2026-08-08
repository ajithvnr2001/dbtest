from argparse import Namespace

from tamil_music.cli import _load_checkpoint, _write_outputs
from tamil_music.models import Album, Movie, Track


def test_batch_checkpoint_round_trip(tmp_path):
    output = tmp_path / "songlist.txt"
    args = Namespace(output=output, json_output=None, keep_empty=True, threshold=0.54)
    album = Album(7, "Roja", "A.R. Rahman", 1993, (Track(8, "Song", "https://music.apple.com/in/song/8"),), Movie("tt1", "Roja", 1992), 0.9)

    assert _write_outputs(args, [album], [], {"tt1"}) == 1
    processed, albums, empty = _load_checkpoint(output.with_name("songlist.txt.checkpoint.json"), 0.54)
    assert processed == {"tt1"}
    assert albums[0].tracks[0].track_id == 8
    assert empty == []
