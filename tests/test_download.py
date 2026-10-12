import hashlib
from pathlib import Path

import pytest

from quotelab.data import download
from quotelab.data.download import (
    ChecksumError,
    DownloadTooLarge,
    load_manifest,
    run_download,
)

SYMBOL = "BTCUSDT"
TRADES_NAME = "BTCUSDT-trades-2024-01-15.zip"
FUNDING_NAME = "BTCUSDT-fundingRate-2024-01.zip"
TRADES_REL = f"data/futures/um/daily/trades/{SYMBOL}/{TRADES_NAME}"
FUNDING_REL = f"data/futures/um/monthly/fundingRate/{SYMBOL}/{FUNDING_NAME}"
TRADES_PAYLOAD = b"trades archive bytes" * 200
FUNDING_PAYLOAD = b"funding archive bytes" * 200


def publish(mirror: Path, rel: str, payload: bytes, checksum: str | None = None) -> None:
    path = mirror / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    digest = checksum or hashlib.sha256(payload).hexdigest()
    path.with_name(f"{path.name}.CHECKSUM").write_text(f"{digest}  {path.name}\n")


def make_cfg(base_url: str, raw_dir: Path, **data_overrides) -> dict:
    data = {
        "base_url": base_url,
        "raw_dir": str(raw_dir),
        "symbols": [SYMBOL],
        "dates": ["2024-01-15"],
        "daily_kinds": ["trades"],
        "monthly_kinds": ["fundingRate"],
        "max_download_gb": 3.0,
    }
    data.update(data_overrides)
    return {"data": data}


@pytest.fixture
def mirror(tmp_path) -> Path:
    root = tmp_path / "mirror"
    publish(root, TRADES_REL, TRADES_PAYLOAD)
    publish(root, FUNDING_REL, FUNDING_PAYLOAD)
    return root


def test_downloads_files_and_records_sha256(tmp_path, mirror):
    raw = tmp_path / "raw"
    files = run_download(
        make_cfg(mirror.as_uri(), raw), dry_run=False, allow_large=False, out=lambda _: None
    )

    assert len(files) == 2
    assert (raw / SYMBOL / "trades" / TRADES_NAME).read_bytes() == TRADES_PAYLOAD
    assert (raw / SYMBOL / "fundingRate" / FUNDING_NAME).read_bytes() == FUNDING_PAYLOAD
    manifest = load_manifest(raw)
    assert (
        manifest[f"{SYMBOL}/trades/{TRADES_NAME}"]["sha256"]
        == hashlib.sha256(TRADES_PAYLOAD).hexdigest()
    )
    assert not list(raw.rglob("*.part"))


def test_second_run_downloads_nothing(tmp_path, mirror, monkeypatch):
    cfg = make_cfg(mirror.as_uri(), tmp_path / "raw")
    run_download(cfg, dry_run=False, allow_large=False, out=lambda _: None)

    downloaded = []
    real_download_file = download.download_file

    def counting_download_file(f, raw_dir, manifest):
        downloaded.append(f)
        real_download_file(f, raw_dir, manifest)

    monkeypatch.setattr(download, "download_file", counting_download_file)
    run_download(cfg, dry_run=False, allow_large=False, out=lambda _: None)

    assert downloaded == []


def test_missing_remote_file_is_reported_not_fatal(tmp_path, mirror):
    lines = []
    files = run_download(
        make_cfg(mirror.as_uri(), tmp_path / "raw", dates=["2024-01-15", "2024-01-16"]),
        dry_run=False,
        allow_large=False,
        out=lines.append,
    )

    assert len(files) == 2
    assert any("missing" in line and "2024-01-16" in line for line in lines)


def test_checksum_mismatch_raises_and_leaves_no_files(tmp_path):
    mirror = tmp_path / "mirror"
    publish(mirror, TRADES_REL, TRADES_PAYLOAD, checksum="0" * 64)
    raw = tmp_path / "raw"

    with pytest.raises(ChecksumError):
        run_download(
            make_cfg(mirror.as_uri(), raw, monthly_kinds=[]),
            dry_run=False,
            allow_large=False,
            out=lambda _: None,
        )

    trades_dir = raw / SYMBOL / "trades"
    assert not (trades_dir / TRADES_NAME).exists()
    assert not (trades_dir / f"{TRADES_NAME}.part").exists()
    assert load_manifest(raw) == {}


def test_dry_run_downloads_nothing(tmp_path, mirror):
    raw = tmp_path / "raw"
    lines = []
    files = run_download(
        make_cfg(mirror.as_uri(), raw), dry_run=True, allow_large=False, out=lines.append
    )

    assert len(files) == 2
    assert not raw.exists()
    assert any(line.startswith("to download:") for line in lines)


def test_too_large_requires_allow_large(tmp_path, mirror):
    raw = tmp_path / "raw"
    cfg = make_cfg(mirror.as_uri(), raw, max_download_gb=1e-9)

    with pytest.raises(DownloadTooLarge, match="--allow-large"):
        run_download(cfg, dry_run=False, allow_large=False, out=lambda _: None)
    assert not raw.exists()

    run_download(cfg, dry_run=False, allow_large=True, out=lambda _: None)
    assert (raw / SYMBOL / "trades" / TRADES_NAME).exists()
