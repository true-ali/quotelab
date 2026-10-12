"""The key-results blocks of the Persian README (``README.fa.md``), written from the same tables
as the English blocks so that no number in either README is typed by hand.

Persian is written right to left. A signed amount such as ``-$1,146.23`` placed inside Persian
text would be reordered by the Unicode bidirectional algorithm (the minus sign can end up on the
right), so every number, symbol and code name is wrapped in left-to-right isolate marks.
"""

from __future__ import annotations

from typing import Any

from quotelab.config import REPO_ROOT
from quotelab.report.animation import BASELINE_VARIANT
from quotelab.report.document import fmt, fmt_pct, markdown_row
from quotelab.report.sections import (
    FIGURES,
    market_events_scaled,
    needs_rebate,
    one_row,
    rung_pnl,
    symbols_in,
    usd,
)
from quotelab.report.tables import ReportTables

README_FA = REPO_ROOT / "README.fa.md"
LEFT_TO_RIGHT_ISOLATE = "⁦"
POP_DIRECTIONAL_ISOLATE = "⁩"
FIGURE_CAPTIONS = {
    "e1": (
        "نردبان واقع‌گرایی (E1)",
        "سود و زیان روزانه‌ی استراتژی پایه وقتی مدل پرشدن سفارش پله‌پله واقع‌گرایانه‌تر می‌شود، "
        "در کنار همان سود و زیان پیش از کارمزد میکر.",
    ),
    "e2": (
        "مقایسه‌ی استراتژی‌ها (E2)",
        "سود و زیان روزانه‌ی هر مدل کوت‌دهی به تفکیک اسپرد، موجودی، کارمزد، پوشش ریسک و فاندینگ؛ "
        "لوزی سیاه مجموع است.",
    ),
    "e7": (
        "سطوح کارمزد (E7)",
        "سود و زیان روزانه‌ی استراتژی پایه در هر سطح کارمزد میکر؛ کارمزد سربه‌سر جایی است که خط از "
        "صفر می‌گذرد.",
    ),
}
BASELINE_HEADER = (
    "| نماد | میانگین سود و زیان روزانه | اسپرد | موجودی | کارمزد | کارمزد میکرِ سربه‌سر |"
)
NUMBERS_RULE = "|---|--:|--:|--:|--:|--:|"
UNITS = {"billion": "میلیارد", "million": "میلیون"}


def ltr(text: str) -> str:
    """Keep ``text`` in left-to-right order inside right-to-left Persian text."""
    return f"{LEFT_TO_RIGHT_ISOLATE}{text}{POP_DIRECTIONAL_ISOLATE}"


def dataset_block(t: ReportTables, dataset: str) -> str:
    """The Persian README's key results for one dataset: the baseline table, what fill realism
    and the maker fee do to it, three captioned figures and a link to the full English report."""
    base = f"reports/{dataset}"
    events, unit = market_events_scaled(t)
    lines = [
        f"<sub>{ltr(str(t.n_runs))} بک‌تست روی {ltr(str(len(t.test_dates)))} روز آزمون، "
        f"{ltr(events)} {UNITS[unit]} رویداد بازار.</sub>",
        "",
        BASELINE_HEADER,
        NUMBERS_RULE,
    ]
    for symbol in symbols_in(t.strategies):
        glft = one_row(t.strategies, symbol=symbol, variant="glft")
        fee = one_row(t.break_even, symbol=symbol)["break_even_maker_bps"]
        cells = [glft["pnl_mean"], glft["attr_spread"], glft["attr_inventory"], glft["attr_fees"]]
        amounts = [ltr(usd(cell)) for cell in cells]
        lines.append(markdown_row([ltr(symbol), *amounts, ltr(f"{fee:.2f} bps")]))
    lines += ["", *findings(t), ""]
    for anchor, (title, caption) in FIGURE_CAPTIONS.items():
        lines += [f"**{title}.** {caption}", "", f"![{title}]({base}/{FIGURES[anchor]})", ""]
    lines.append(f"گزارش کامل با همه‌ی جدول‌ها (به انگلیسی): [{base}/RESULTS.md]({base}/RESULTS.md)")
    return "\n".join(lines)


def findings(t: ReportTables) -> list[str]:
    """The Persian form of the two findings under the English baseline table."""
    first_rung = t.realism["variant"][0]
    moves = "؛ ".join(
        f"{ltr(symbol)} از {ltr(rung_pnl(t, symbol, first_rung))} به "
        f"{ltr(rung_pnl(t, symbol, BASELINE_VARIANT))}"
        for symbol in symbols_in(t.realism)
    )
    fees = "، ".join(
        ltr(row["symbol"]) + " " + ltr(f"{row['break_even_maker_bps']:.2f} bps")
        for row in t.break_even.iter_rows(named=True)
    )
    fee_finding = (
        f"استراتژی پایه فقط با ریبیت میکر سربه‌سر می‌شود: {fees}."
        if needs_rebate(t)
        else f"کارمزد میکرِ سربه‌سر: {fees}."
    )
    return [
        f"- **واقع‌گرایی پرشدن سفارش.** میانگین سود و زیان روزانه از پرشدن {ltr(first_rung)} تا "
        f"پرشدن با صف و تأخیر ({ltr(BASELINE_VARIANT)}): {moves}.",
        f"- **کارمزد.** {fee_finding}",
    ]


def paper_block(
    symbol: str,
    window: tuple[str, str],
    rows: dict[str, dict[str, Any]],
    stats: dict[str, Any],
    assumed_latency_ms: float,
    base: str,
    figure: str,
) -> str:
    """The Persian README's summary of the latest paper session; ``window`` holds the UTC start
    and end as text and ``base`` is the report directory relative to the README."""
    paper, replayed = rows["paper"], rows["replay"]
    messages = stats["book_messages"] + stats["trade_messages"]
    delay = f"{fmt(stats['lateness_p50_ms'], 1)} ms"
    assumed = f"{assumed_latency_ms:g} ms"
    paper_fills = f"{paper['quote_fills']:,.0f}"
    replay_fills = f"{replayed['quote_fills']:,.0f}"
    return "\n".join(
        [
            f"نماد {ltr(symbol)}، بازه‌ی زنده از {ltr(window[0])} تا {ltr(window[1])} به وقت UTC.",
            "",
            "| | معامله‌ی کاغذی (فید زنده) | بازپخش (بک‌تست) |",
            "|---|--:|--:|",
            f"| سود و زیان | {ltr(usd(paper['pnl']))} | {ltr(usd(replayed['pnl']))} |",
            f"| معامله‌های کوت | {ltr(paper_fills)} | {ltr(replay_fills)} |",
            "",
            f"- **فید.** {ltr(f'{messages:,}')} پیام، "
            f"{ltr(str(stats['reconnects']))} اتصال دوباره، "
            f"{ltr(fmt_pct(stats['clamped_share'], 2))} پیام با زمانی عقب‌تر از ساعت موتور، و "
            f"تأخیر میانه‌ی {ltr(delay)} در برابر {ltr(assumed)} که موتور "
            "فرض می‌کند.",
            "",
            f"![جلسه‌ی کاغذی در برابر بازپخش]({base}/{figure})",
            "",
            f"مقایسه‌ی کامل (به انگلیسی): [{base}/COMPARISON.md]({base}/COMPARISON.md)",
        ]
    )
