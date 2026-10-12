"""Command-line entry point: ``quotelab <command>``, one subcommand per pipeline step."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quotelab.config import load_config, resolve_path
from quotelab.data.convert import run_prepare
from quotelab.data.download import ChecksumError, DownloadTooLarge, run_download
from quotelab.data.synthetic import synthetic_config, write_synthetic
from quotelab.experiments.runner import DATASETS, calibrate_all, dataset_for, run_experiments
from quotelab.experiments.worker import calibration_path
from quotelab.live.compare import NoSession, write_comparison
from quotelab.live.paper_runner import live_config, new_session, run_session
from quotelab.report.results import MissingRuns, write_report
from quotelab.strategies.calibration import read_calibration


def cmd_download(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        run_download(cfg, dry_run=args.dry_run, allow_large=args.allow_large)
    except DownloadTooLarge as exc:
        print(exc, file=sys.stderr)
        return 2
    except ChecksumError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def cmd_prepare(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    run_prepare(
        resolve_path(cfg["data"]["raw_dir"]),
        resolve_path(cfg["data"]["processed_dir"]),
        force=args.force,
    )
    return 0


def cmd_synth(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    for path in write_synthetic(synthetic_config(cfg), resolve_path(cfg["data"]["processed_dir"])):
        print(f"wrote {path}")
    return 0


def cmd_calibrate(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    dataset = dataset_for(cfg, args.dataset)
    pairs = calibrate_all(cfg, dataset, _workers(cfg, args))
    print("symbol      date        sigma      k          A        R2     ofi_beta")
    for symbol in dataset.symbols:
        for calib_date, _ in pairs:
            cal = read_calibration(calibration_path(dataset, symbol, calib_date))
            print(
                f"{symbol:<11} {calib_date}  {cal.sigma:<9.4f}  {cal.k:<9.4f}  {cal.a:<7.4f}"
                f"  {cal.intensity_r2:<5.3f}  {cal.ofi_beta:.4f}"
            )
    return 0


def cmd_backtest(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    dataset = dataset_for(cfg, args.dataset)
    metrics = run_experiments(cfg, dataset, _workers(cfg, args))
    print(f"{metrics.height} distinct runs written to {dataset.out_dir}")
    return 0


def cmd_report(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        results = write_report(cfg, args.dataset)
    except MissingRuns as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"wrote {results}, its figures and dashboard.html; updated README.md")
    return 0


def cmd_paper(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """Run one paper session on the live public feed. Ctrl-C stops it and still writes output."""
    live = live_config(cfg)
    if args.minutes is not None:
        live = dataclasses.replace(live, minutes=args.minutes)
    out_dir = live.sessions_dir / datetime.now(UTC).strftime("%Y%m%dT%H%MZ")
    session = new_session(cfg, live, out_dir)
    print(
        f"[paper] {live.symbol}: {live.warmup_minutes:g} min warm-up, then {live.minutes:g} min "
        f"of simulated quoting; writing to {out_dir}",
        flush=True,
    )
    try:
        asyncio.run(run_session(session, live))
    except KeyboardInterrupt:
        session.stop_reason = "interrupted by the user"
    finally:
        session.close()
    print(f"[paper] {session.stop_reason}; {session.messages:,} messages recorded in {out_dir}")
    return 0


def cmd_paper_compare(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    session = Path(args.session) if args.session else None
    try:
        document = write_comparison(live_config(cfg), session)
    except NoSession as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"wrote {document} and its figure; updated README.md")
    return 0


def _workers(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    return args.workers if args.workers is not None else int(cfg["experiments"]["workers"])


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="YAML config (default: config/default.yaml)")
    parser = argparse.ArgumentParser(prog="quotelab")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser(
        "download", parents=[common], help="fetch public Binance files into data/raw"
    )
    download.add_argument("--dry-run", action="store_true", help="list files and sizes only")
    download.add_argument("--allow-large", action="store_true", help="exceed max_download_gb")
    download.set_defaults(func=cmd_download)

    prepare = sub.add_parser(
        "prepare", parents=[common], help="convert raw zips to Parquet in data/processed"
    )
    prepare.add_argument("--force", action="store_true", help="rebuild existing outputs")
    prepare.set_defaults(func=cmd_prepare)

    synth = sub.add_parser(
        "synth", parents=[common], help="write the seeded synthetic dataset to data/processed"
    )
    synth.set_defaults(func=cmd_synth)

    runs = argparse.ArgumentParser(add_help=False)
    runs.add_argument("--dataset", choices=DATASETS, default="synthetic")
    runs.add_argument("--workers", type=int, help="worker processes (default: from config)")
    calibrate = sub.add_parser(
        "calibrate", parents=[common, runs], help="fit parameters for every walk-forward day"
    )
    calibrate.set_defaults(func=cmd_calibrate)
    backtest = sub.add_parser(
        "backtest", parents=[common, runs], help="run experiments E1 to E7 on a dataset"
    )
    backtest.set_defaults(func=cmd_backtest)
    report = sub.add_parser(
        "report", parents=[common, runs], help="write figures, RESULTS.md and the dashboard"
    )
    report.set_defaults(func=cmd_report)

    paper = sub.add_parser(
        "paper", parents=[common], help="paper-trade the live public feed (read-only, no orders)"
    )
    paper.add_argument("--minutes", type=float, help="live minutes after warm-up (default: config)")
    paper.set_defaults(func=cmd_paper)
    compare = sub.add_parser(
        "paper-compare", parents=[common], help="replay a paper session and report the differences"
    )
    compare.add_argument("--session", help="session directory (default: the latest)")
    compare.set_defaults(func=cmd_paper_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(load_config(args.config), args)


if __name__ == "__main__":
    raise SystemExit(main())
