import importlib.util
import sys
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("colab_pipeline", Path(__file__).parents[1] / "scripts" / "colab_pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = pipeline
spec.loader.exec_module(pipeline)


class Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class FakeClient:
    def __init__(self):
        self.objects = {}
        self.fail_latest = False

    def put_object(self, Bucket, Key, Body, ContentType):
        if self.fail_latest and Key.endswith("LATEST.json"):
            raise RuntimeError("simulated crash before manifest commit")
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        try:
            return {"Body": Body(self.objects[(Bucket, Key)])}
        except KeyError:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error


def store():
    instance = pipeline.WasabiStore.__new__(pipeline.WasabiStore)
    instance.bucket = "bucket"
    instance.prefix = "prefix"
    instance._client = FakeClient()
    return instance


def test_wasabi_manifest_is_commit_point_and_resume_ignores_partial_snapshot():
    remote = store()
    artifact = {"state.json": (b"one", "application/json")}
    remote.commit("songlist", 10, artifact)
    assert remote.load_latest("songlist")["artifacts"]["state.json"] == b"one"

    remote._client.fail_latest = True
    with pytest.raises(RuntimeError, match="simulated crash"):
        remote.commit("songlist", 20, {"state.json": (b"two", "application/json")})
    assert remote.load_latest("songlist")["manifest"]["sequence"] == 10
    assert remote.load_latest("songlist")["artifacts"]["state.json"] == b"one"


def test_final_commit_writes_stable_final_artifact():
    remote = store()
    remote.commit("songlist", 20, {"tamil-movie-songs.md": (b"# done\n", "text/markdown")}, final=True)
    assert remote._client.objects[("bucket", "prefix/final/tamil-movie-songs.md")] == b"# done\n"
    assert remote.load_latest("songlist")["manifest"]["final"] is True
