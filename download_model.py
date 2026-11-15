"""Download and verify a trained checkpoint from this project's GitHub release."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parent


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urllib.parse.urlparse(new_url).scheme != "https":
            raise ValueError("Checkpoint downloads require HTTPS")
        redirected = super().redirect_request(
            request, fp, code, message, headers, new_url
        )
        if redirected and urllib.parse.urlparse(new_url).netloc != urllib.parse.urlparse(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    if shutil.which("gh"):
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    if shutil.which("git"):
        try:
            result = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True, text=True, env=env, timeout=15,
            )
            fields = dict(
                line.split("=", 1) for line in result.stdout.splitlines()
                if "=" in line
            )
            return fields.get("password")
        except subprocess.TimeoutExpired:
            pass
    return None


def valid_model(folder, files):
    return all(
        (folder / name).is_file()
        and (folder / name).stat().st_size == info["size_bytes"]
        and sha256(folder / name) == info["sha256"]
        for name, info in files.items()
    )


def download_model(model_name=None, destination=None):
    manifest = json.loads((ROOT / "model_manifest.json").read_text(encoding="utf-8"))
    model_name = model_name or manifest["default_model"]
    if model_name not in manifest["models"]:
        raise ValueError(f"Unknown model: {model_name}")
    info = manifest["models"][model_name]
    if any(Path(name).name != name for name in info["files"]):
        raise ValueError("Invalid checkpoint filename")
    folder = Path(destination).resolve() if destination else ROOT / model_name
    if valid_model(folder, info["files"]):
        print(f"Checkpoint ready: {folder}")
        return folder

    token = github_token()
    opener = urllib.request.build_opener(SafeRedirect())

    def open_github(url, accept="application/vnd.github+json"):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise ValueError("Expected an HTTPS GitHub API URL")
        headers = {"Accept": accept, "User-Agent": "rut5-slang-transfer"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return opener.open(urllib.request.Request(url, headers=headers), timeout=120)

    release_url = (
        f"https://api.github.com/repos/{manifest['repository']}/releases/tags/"
        f"{manifest['release_tag']}"
    )
    try:
        with open_github(release_url) as response:
            release = json.load(response)
        asset = next(
            item for item in release["assets"] if item["name"] == info["asset"]
        )
        folder.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".rut5-download-", dir=folder.parent) as temporary:
            staging = Path(temporary).resolve()
            assert staging.is_relative_to(folder.parent.resolve())
            archive = staging / "checkpoint.zip"
            print(f"Downloading {info['asset']} ({info['size_bytes'] / 1024**2:.0f} MiB)...")
            with open_github(asset["url"], "application/octet-stream") as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            if archive.stat().st_size != info["size_bytes"] or sha256(archive) != info["sha256"]:
                raise ValueError("Archive checksum mismatch; download was not installed")
            expected = {f"{model_name}/{name}" for name in info["files"]}
            with zipfile.ZipFile(archive) as package:
                if set(package.namelist()) != expected:
                    raise ValueError("Unexpected archive contents")
                for name in info["files"]:
                    with package.open(f"{model_name}/{name}") as source, (staging / name).open("wb") as output:
                        shutil.copyfileobj(source, output)
            if not valid_model(staging, info["files"]):
                raise ValueError("Checkpoint checksum mismatch")
            folder.mkdir(parents=True, exist_ok=True)
            for name in info["files"]:
                if Path(name).name != name:
                    raise ValueError("Invalid checkpoint filename")
                os.replace(staging / name, folder / name)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"GitHub returned HTTP {error.code}. For a private repository, "
            "run 'gh auth login' as an account with access, or set GH_TOKEN."
        ) from None
    except StopIteration:
        raise RuntimeError(f"Release asset {info['asset']} was not found") from None
    print(f"Checkpoint ready: {folder}")
    return folder


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Checkpoint name from model_manifest.json")
    parser.add_argument("--destination", type=Path, help="Override the output directory")
    args = parser.parse_args()
    download_model(args.model, args.destination)
