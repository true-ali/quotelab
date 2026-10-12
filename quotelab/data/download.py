"""Download Binance USD-M archives into the raw data directory, verified by SHA-256.

Each archive is written to a ``.part`` file and renamed into place only once its digest matches
the ``.CHECKSUM`` file published next to it. Completed files are recorded in ``checksums.json``
inside the raw directory so that reruns skip archives that are already present and intact.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from quotelab.config import resolve_path

CHUNK_BYTES = 1024 * 1024
REQUEST_TIMEOUT_S = 60
MANIFEST_NAME = "checksums.json"
BYTES_PER_MB = 1e6
BYTES_PER_GB = 1e9

Manifest = dict[str, dict[str, Any]]


class ChecksumError(RuntimeError):
    """A downloaded archive does not match the SHA-256 published next to it."""


class DownloadTooLarge(RuntimeError):
    """The archives still to download exceed ``data.max_download_gb``."""


@dataclass(frozen=True)
class RemoteFile:
    symbol: str
    kind: str
    period: str
    url: str
    dest: Path
    size: int | None


def file_url(base_url: str, symbol: str, kind: str, period: str) -> str:
    root = f"{base_url.rstrip('/')}/data/futures/um"
    if kind == "klines":
        return f"{root}/daily/klines/{symbol}/1m/{symbol}-1m-{period}.zip"
    if kind == "fundingRate":
        return f"{root}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{period}.zip"
    if kind in ("trades", "bookTicker"):
        return f"{root}/daily/{kind}/{symbol}/{symbol}-{kind}-{period}.zip"
    raise ValueError(f"unknown Binance data kind {kind!r}")


def remote_size(url: str) -> int | None:
    """Size in bytes from a HEAD request, or None when the archive is not published.

    A 404 or a transport failure means "missing", so one absent archive never aborts the plan.
    Other HTTP errors propagate because they indicate a real problem, not an absent file.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            length = response.headers.get("Content-Length")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except (urllib.error.URLError, FileNotFoundError):
        return None
    return None if length is None else int(length)


def plan_downloads(cfg: dict[str, Any], raw_dir: Path) -> list[RemoteFile]:
    data = cfg["data"]
    daily = [
        (symbol, kind, date)
        for symbol in data["symbols"]
        for date in data["dates"]
        for kind in data["daily_kinds"]
    ]
    months = sorted({date[:7] for date in data["dates"]})
    monthly = [
        (symbol, kind, month)
        for symbol in data["symbols"]
        for month in months
        for kind in data["monthly_kinds"]
    ]
    return [
        _remote_file(data["base_url"], raw_dir, symbol, kind, period)
        for symbol, kind, period in [*daily, *monthly]
    ]


def _remote_file(base_url: str, raw_dir: Path, symbol: str, kind: str, period: str) -> RemoteFile:
    url = file_url(base_url, symbol, kind, period)
    dest = raw_dir / symbol / kind / url.rsplit("/", 1)[-1]
    return RemoteFile(symbol, kind, period, url, dest, remote_size(url))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(partial(fh.read, CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_checksum(url: str) -> str:
    with urllib.request.urlopen(f"{url}.CHECKSUM", timeout=REQUEST_TIMEOUT_S) as response:
        return response.read().decode().split()[0].lower()


def load_manifest(raw_dir: Path) -> Manifest:
    path = raw_dir / MANIFEST_NAME
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_manifest(raw_dir: Path, manifest: Manifest) -> None:
    """Written through a temporary file so an interrupted save never leaves a truncated manifest."""
    path = raw_dir / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def is_up_to_date(f: RemoteFile, raw_dir: Path, manifest: Manifest) -> bool:
    entry = manifest.get(_manifest_key(f, raw_dir))
    return entry is not None and f.dest.exists() and sha256_file(f.dest) == entry["sha256"]


def download_file(f: RemoteFile, raw_dir: Path, manifest: Manifest) -> None:
    """Publish one archive only if its SHA-256 matches; a mismatched download leaves no trace."""
    expected = fetch_checksum(f.url)
    part = f.dest.with_suffix(".zip.part")
    f.dest.parent.mkdir(parents=True, exist_ok=True)
    actual = _stream_to_file(f.url, part)
    if actual != expected:
        part.unlink(missing_ok=True)
        raise ChecksumError(f"{f.url}: expected sha256 {expected}, got {actual}")
    os.replace(part, f.dest)
    manifest[_manifest_key(f, raw_dir)] = {
        "sha256": actual,
        "size": f.dest.stat().st_size,
        "url": f.url,
    }


def run_download(
    cfg: dict[str, Any],
    *,
    dry_run: bool,
    allow_large: bool,
    raw_dir: Path | None = None,
    out: Callable[[str], object] = print,
) -> list[RemoteFile]:
    """Plan, report and (unless ``dry_run``) fetch every archive named in ``cfg["data"]``.

    Returns the planned archives that exist remotely; missing ones are reported and skipped.
    """
    data = cfg["data"]
    root = resolve_path(data["raw_dir"]) if raw_dir is None else raw_dir
    files = plan_downloads(cfg, root)
    manifest = load_manifest(root)
    statuses = [(f, f.size is not None and is_up_to_date(f, root, manifest)) for f in files]
    for f, is_cached in statuses:
        out(_format_row(f, is_cached))
    pending = [f for f, is_cached in statuses if f.size is not None and not is_cached]
    total_bytes = sum(f.size or 0 for f in pending)
    out(f"to download: {len(pending)} files, {total_bytes / BYTES_PER_GB:.3f} GB")
    limit_gb = float(data["max_download_gb"])
    too_large = total_bytes > limit_gb * BYTES_PER_GB and not allow_large
    message = (
        f"{total_bytes / BYTES_PER_GB:.2f} GB to download exceeds max_download_gb={limit_gb}; "
        "pass --allow-large to download anyway"
    )
    available = [f for f in files if f.size is not None]
    if dry_run:
        if too_large:
            out(message)
        return available
    if too_large:
        raise DownloadTooLarge(message)
    for f in pending:
        download_file(f, root, manifest)
        save_manifest(root, manifest)
        out(f"downloaded {f.dest.name}")
    return available


def _manifest_key(f: RemoteFile, raw_dir: Path) -> str:
    return f.dest.relative_to(raw_dir).as_posix()


def _stream_to_file(url: str, part: Path) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as response, part.open("wb") as fh:
        for chunk in iter(partial(response.read, CHUNK_BYTES), b""):
            fh.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _format_row(f: RemoteFile, is_cached: bool) -> str:
    size = "missing" if f.size is None else f"{f.size / BYTES_PER_MB:,.1f} MB"
    status = "cached" if is_cached else ""
    return f"{f.kind:<12} {f.symbol:<10} {f.period:<10} {size:>12} {status}".rstrip()
