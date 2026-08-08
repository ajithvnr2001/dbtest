from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
pipeline = (ROOT / "scripts" / "colab_pipeline.py").read_text(encoding="utf-8")

install = '''%pip install "scrapling[fetchers]" boto3'''

browser = '''import pathlib, shutil, subprocess, sys
patchright = shutil.which("patchright") or str(pathlib.Path(sys.executable).parent / "patchright")
subprocess.run([patchright, "install", "--with-deps", "chromium"], check=True)
print("Scrapling and Chromium are ready.")'''

credentials = '''import os

# Credentials come from Colab Secrets and are never stored in this notebook.
try:
    from google.colab import userdata
except ImportError:
    userdata = None

def secret(name):
    value = os.getenv(name)
    if value:
        return value
    if userdata is not None:
        try:
            return userdata.get(name)
        except Exception:
            return None
    return None

os.environ["WASABI_REGION"] = "ap-northeast-1"
os.environ["WASABI_BUCKET"] = "checkpointsvnr"
os.environ["WASABI_ENDPOINT"] = "https://s3.ap-northeast-1.wasabisys.com"
os.environ["WASABI_PREFIX"] = "tamil-movie-music"
os.environ["WASABI_ACCESS_KEY"] = secret("WASABI_ACCESS_KEY") or ""
os.environ["WASABI_SECRET_KEY"] = secret("WASABI_SECRET_KEY") or ""
if not os.environ["WASABI_ACCESS_KEY"] or not os.environ["WASABI_SECRET_KEY"]:
    raise RuntimeError("Create Colab Secrets named WASABI_ACCESS_KEY and WASABI_SECRET_KEY, then Run all again.")
print("Wasabi checkpoint destination: Tokyo / checkpointsvnr / tamil-movie-music")'''

writefile = "%%writefile /content/tamil_movie_music_pipeline.py\n" + pipeline

run = '''import os, subprocess, sys

os.environ["PYTHONUNBUFFERED"] = "1"
command = [sys.executable, "-u", "/content/tamil_movie_music_pipeline.py"]
print("Starting:", " ".join(command), flush=True)
process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
assert process.stdout is not None
for line in process.stdout:
    print(line, end="", flush=True)
return_code = process.wait()
if return_code:
    raise RuntimeError(f"Pipeline failed with exit code {return_code}. The complete error is printed above.")
print("Pipeline completed successfully.")'''

diagnostics = '''from pathlib import Path
import re
output = Path("/content/tamil-movie-music-run/tamil-movie-songs.md")
text = output.read_text(encoding="utf-8")
print(text.splitlines()[0])
print("Album headings:", sum(line.endswith(":") for line in text.splitlines()))
print("Song links:", len(re.findall(r"https://music\\.apple\\.com/.*/song/", text)))
print("Output:", output)'''

def code(source: str) -> dict:
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": source.splitlines(True)}


notebook = {
    "cells": [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# Tamil movie → Apple Music index\n",
                "\n",
                "Run All. Create Colab Secrets named `WASABI_ACCESS_KEY` and `WASABI_SECRET_KEY` once. The complete pipeline is written with `%%writefile`, then it downloads IMDb data, checkpoints IMDb/search and songlist state to Wasabi, resumes after a crash, and uploads the final songlist.\n",
            ],
        },
        code(install),
        code(browser),
        code(credentials),
        code(writefile),
        code(run),
        code(diagnostics),
    ],
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

(ROOT / "tamil_movie_music_colab.ipynb").write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("wrote tamil_movie_music_colab.ipynb")
