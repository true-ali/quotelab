"""Build the report of one dataset: figures, ``RESULTS.md``, the HTML dashboard and the README
block of key results, all from the files a backtest wrote and nothing typed by hand.

The README keeps one block per dataset between two HTML comment markers. Rebuilding a dataset's
report replaces only its own block, so the synthetic and real results can sit side by side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

from quotelab.config import REPO_ROOT, resolve_path
from quotelab.report import animation, figures, persian
from quotelab.report.dashboard import render_dashboard
from quotelab.report.document import Section, markdown_document, markdown_row, markdown_table
from quotelab.report.sections import (
    FIGURES,
    all_sections,
    ci,
    frame_table,
    intro,
    market_events_scaled,
    needs_rebate,
    num,
    one_row,
    rung_pnl,
    symbols_in,
    text,
    usd,
)
from quotelab.report.tables import ReportTables, RunOutputs, load_outputs, report_tables

TITLES = {"synthetic": "QuoteLab results: synthetic data", "real": "QuoteLab results: Binance data"}
README = REPO_ROOT / "README.md"
README_FIGURES = {
    "e1": "The baseline's daily PnL as the fill model gets more realistic, rung by rung, beside "
    "the same PnL before maker fees.",
    "e2": "Each quoting model's daily PnL split into spread, inventory, fees, hedging and "
    "funding; the diamond is the total.",
    "e7": "The baseline's daily PnL at each maker fee; the break-even fee is where it crosses "
    "zero.",
}
REPLAY_FIGURE = "figures/replay.gif"


class MissingRuns(RuntimeError):
    """The dataset has not been backtested yet, so there is nothing to report."""


def report_dir(cfg: dict[str, Any], dataset: str) -> Path:
    return resolve_path(cfg["experiments"]["reports_dir"]) / dataset


def write_report(cfg: dict[str, Any], dataset: str) -> Path:
    """Write every output of one dataset's report and return the path of its ``RESULTS.md``."""
    runs = resolve_path(cfg["experiments"]["runs_dir"]) / dataset
    if not (runs / "metrics.parquet").exists():
        command = "make backtest" if dataset == "synthetic" else "make backtest-real"
        raise MissingRuns(f"no backtest results in {runs}; run `{command}` first")
    outputs = load_outputs(runs)
    tables = report_tables(outputs, cfg)
    out = report_dir(cfg, dataset)
    draw_figures(tables, out)
    sections = [replay_section(outputs, out), *all_sections(tables, cfg)]
    paragraphs = intro(tables, cfg, dataset)
    results = out / "RESULTS.md"
    document = markdown_document(TITLES[dataset], paragraphs, sections)
    if dataset == "real":
        document += f"\n## Resume line\n\n{resume_line(tables)}\n"
    results.write_text(document)
    subtitle = f"{', '.join(tables.symbols)} · {len(tables.test_dates)} test days"
    (out / "dashboard.html").write_text(
        render_dashboard(TITLES[dataset], subtitle, paragraphs, sections)
    )
    update_readme(README, dataset, readme_block(tables, dataset, sections))
    update_readme(persian.README_FA, dataset, persian.dataset_block(tables, dataset))
    return results


def draw_figures(t: ReportTables, out: Path) -> None:
    """Draw every figure into ``out`` at the paths the sections refer to."""
    path = {anchor: out / relative for anchor, relative in FIGURES.items()}
    with figures.styled():
        figures.intensity_fit(t.intensity, path["calibration"])
        figures.e1_realism_ladder(t.realism, path["e1"])
        figures.e2_strategies(t.strategies, path["e2"])
        figures.e3_sensitivity(t.sensitivity, path["e3"])
        figures.e4_latency(t.latency, path["e4"])
        figures.e5_regimes(t.regimes, path["e5"])
        figures.e6_event_study(t.event_series, t.windows, path["e6"])
        figures.e7_fee_tiers(t.fee_tiers, t.fee_reruns, path["e7"])
        figures.markouts(t.markouts, path["markouts"])


def replay_section(outputs: RunOutputs, out: Path) -> Section:
    """Draw the animated replay of the baseline run into ``out`` and return the section that
    shows it."""
    symbol, day, run_dir = animation.baseline_run_dir(outputs)
    animation.replay_animation(
        pl.read_parquet(run_dir / "timeseries.parquet"),
        pl.read_parquet(run_dir / "fills.parquet"),
        f"{symbol} {day}: baseline glft, busiest {animation.WINDOW_S} s",
        out / REPLAY_FIGURE,
    )
    paragraph = (
        f"`{symbol}` on {day}, baseline `glft` (the `{animation.BASELINE_VARIANT}` rung of E1): "
        f"the {animation.WINDOW_S} seconds of the day with the most quote fills, drawn from the "
        "run's own files. Top: the market's best bid and ask with our buy and sell fills; the "
        "bid and ask are the run's once-a-second samples, so a fill between two samples can sit "
        "outside them. Middle: our position. Bottom: the PnL since the window opened."
    )
    return Section("replay", "Replay of the baseline run", [paragraph], [], REPLAY_FIGURE)


def readme_block(t: ReportTables, dataset: str, sections: list[Section]) -> str:
    """The README's key results for one dataset: the baseline table, what fill realism and the
    maker fee do to it, three captioned figures, the E1 ladder folded away, and report links."""
    base = f"reports/{dataset}"
    titles = {section.anchor: section.title for section in sections}
    lines = [scale_line(t), "", baseline_table(t), "", *findings(t), ""]
    for anchor, caption in README_FIGURES.items():
        title = titles[anchor]
        lines += [f"**{title}.** {caption}", "", f"![{title}]({base}/{FIGURES[anchor]})", ""]
    ladder = frame_table(
        "E1 realism ladder (USD per day)",
        t.realism,
        [
            ("Rung", text("variant")),
            ("Symbol", text("symbol")),
            ("Mean daily PnL [95% CI]", ci("pnl")),
            ("Change vs previous rung", num("change_vs_previous")),
        ],
    )
    lines += [
        "<details>",
        "<summary>The E1 realism ladder as a table</summary>",
        "",
        markdown_table(ladder),
        "",
        "</details>",
        "",
        f"Every table and figure: [{base}/RESULTS.md]({base}/RESULTS.md). The same report as one "
        f"HTML page to open locally: `{base}/dashboard.html`.",
    ]
    return "\n".join(lines)


def scale_line(t: ReportTables) -> str:
    """How much the dataset's results rest on: runs, test days and market events replayed."""
    events, unit = market_events_scaled(t)
    return (
        f"<sub>{t.n_runs} backtests over {len(t.test_dates)} test days, "
        f"{events} {unit} market events replayed.</sub>"
    )


def baseline_table(t: ReportTables) -> str:
    """The baseline's mean daily PnL per symbol, its three largest attribution parts and the
    maker fee at which it would break even, as a Markdown table with numbers right-aligned."""
    lines = [
        markdown_row(
            ["Symbol", "Mean daily PnL", "Spread", "Inventory", "Fees", "Break-even maker fee"]
        ),
        "|---|--:|--:|--:|--:|--:|",
    ]
    for symbol in symbols_in(t.strategies):
        glft = one_row(t.strategies, symbol=symbol, variant="glft")
        fee = one_row(t.break_even, symbol=symbol)["break_even_maker_bps"]
        cells = [glft["pnl_mean"], glft["attr_spread"], glft["attr_inventory"], glft["attr_fees"]]
        lines.append(markdown_row([f"`{symbol}`", *map(usd, cells), f"{fee:.2f} bps"]))
    return "\n".join(lines)


def findings(t: ReportTables) -> list[str]:
    """Two bullets read off the tables: how the baseline moved from the least to the most
    realistic standard fill model, and whether breaking even needs a maker rebate."""
    first_rung = t.realism["variant"][0]
    moves = "; ".join(
        f"`{symbol}` {rung_pnl(t, symbol, first_rung)} → "
        f"{rung_pnl(t, symbol, animation.BASELINE_VARIANT)}"
        for symbol in symbols_in(t.realism)
    )
    fees = ", ".join(
        f"`{row['symbol']}` {row['break_even_maker_bps']:.2f} bps"
        for row in t.break_even.iter_rows(named=True)
    )
    fee_finding = (
        f"The baseline breaks even only with a maker rebate: {fees}."
        if needs_rebate(t)
        else f"Break-even maker fee: {fees}."
    )
    return [
        f"- **Fill realism.** Mean daily PnL from `{first_rung}` fills to queue-aware fills with "
        f"latency (`{animation.BASELINE_VARIANT}`): {moves}.",
        f"- **Fees.** {fee_finding}",
    ]


def resume_line(t: ReportTables) -> str:
    """A one-sentence project summary for a CV, with its counts and fees taken from this run.

    It states scale and the break-even maker fee, not a profit, because the simulation is not
    evidence of live profitability.
    """
    events, unit = market_events_scaled(t)
    break_even = ", ".join(
        f"{row['symbol']} {row['break_even_maker_bps']:.2f} bps"
        for row in t.break_even.iter_rows(named=True)
    )
    finding = (
        f"found the baseline breaks even only with a maker rebate (break-even maker fee "
        f"{break_even})"
        if needs_rebate(t)
        else f"measured the break-even maker fee ({break_even})"
    )
    return (
        "Built QuoteLab, an event-driven market-making simulator in Python "
        "(asyncio, DuckDB, polars) with GLFT and Avellaneda-Stoikov quoting, a queue-aware fill "
        "model with latency, PnL attribution, markouts, kill switches and a read-only live "
        f"paper-trading mode; ran {t.n_runs} backtests over {len(t.test_dates)} test days of "
        f"Binance {'/'.join(t.symbols)} data ({events} {unit} market events "
        f"replayed in total) and {finding}."
    )


def update_readme(readme: Path, dataset: str, block: str) -> None:
    """Replace this dataset's block between its markers, or append it when there is none yet."""
    start = f"<!-- quotelab:results:{dataset}:start -->"
    end = f"<!-- quotelab:results:{dataset}:end -->"
    marked = f"{start}\n{block}\n{end}"
    current = readme.read_text()
    if start in current and end in current:
        head, rest = current.split(start, 1)
        _, tail = rest.split(end, 1)
        readme.write_text(head + marked + tail)
    else:
        readme.write_text(current.rstrip() + "\n\n" + marked + "\n")
