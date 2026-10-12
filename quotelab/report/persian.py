"""The key-results blocks of the Persian README (``README.fa.md``), written from the same tables
as the English blocks so that no number in either README is typed by hand.

Persian is written right to left. A signed amount such as ``-$1,146.23`` placed inside Persian
text would be reordered by the Unicode bidirectional algorithm (the minus sign can end up on the
right), so every number, symbol and code name is wrapped in left-to-right isolate marks.
"""

from __future__ import annotations

from typing import Any

from quotelab.config import REPO_ROOT
from quotelab.report.document import fmt, fmt_pct
from quotelab.report.sections import FIGURES, one_row, symbols_in, usd
from quotelab.report.tables import ReportTables

README_FA = REPO_ROOT / "README.fa.md"
LEFT_TO_RIGHT_ISOLATE = "⁦"
POP_DIRECTIONAL_ISOLATE = "⁩"
TITLES = {
    "synthetic": "نتایج QuoteLab روی داده‌ی مصنوعی",
    "real": "نتایج QuoteLab روی داده‌ی بایننس",
}
FIGURE_CAPTIONS = {
    "e1": "نردبان واقع‌گرایی (E1)",
    "e2": "مقایسه‌ی استراتژی‌ها (E2)",
    "e7": "سطوح کارمزد (E7)",
}


def ltr(text: str) -> str:
    """Keep ``text`` in left-to-right order inside right-to-left Persian text."""
    return f"{LEFT_TO_RIGHT_ISOLATE}{text}{POP_DIRECTIONAL_ISOLATE}"


def dataset_block(t: ReportTables, dataset: str) -> str:
    """The Persian README's key results for one dataset: the baseline per symbol, the break-even
    maker fee, three figures and a link to the full English report."""
    base = f"reports/{dataset}"
    lines = [f"### {TITLES[dataset]}", ""]
    for symbol in symbols_in(t.strategies):
        glft = one_row(t.strategies, symbol=symbol, variant="glft")
        fee = f"{one_row(t.break_even, symbol=symbol)['break_even_maker_bps']:.3f} bps"
        lines.append(
            f"- نماد {ltr(symbol)}، استراتژی پایه‌ی {ltr('glft')} در {glft['n_days']} روز آزمون: "
            f"میانگین سود و زیان روزانه {ltr(usd(glft['pnl_mean']))}، که از آن سهم اسپرد "
            f"{ltr(usd(glft['attr_spread']))}، سهم موجودی {ltr(usd(glft['attr_inventory']))} و "
            f"سهم کارمزد {ltr(usd(glft['attr_fees']))} است. کارمزد میکرِ سربه‌سر {ltr(fee)}."
        )
    lines.append("")
    for anchor, caption in FIGURE_CAPTIONS.items():
        lines += [f"![{caption}]({base}/{FIGURES[anchor]})", ""]
    lines.append(f"گزارش کامل با همه‌ی جدول‌ها (به انگلیسی): [{base}/RESULTS.md]({base}/RESULTS.md)")
    return "\n".join(lines)


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
    return "\n".join(
        [
            "### جلسه‌ی معامله‌ی کاغذی",
            "",
            f"- نماد {ltr(symbol)}، بازه‌ی زنده از {ltr(window[0])} تا {ltr(window[1])} به وقت "
            f"UTC: سود و زیان کاغذی {ltr(usd(paper['pnl']))} با "
            f"{paper['quote_fills']:.0f} معامله‌ی کوت، و بازپخش همان بازه در بک‌تست "
            f"{ltr(usd(replayed['pnl']))} با {replayed['quote_fills']:.0f} معامله.",
            f"- فید: {messages:,} پیام، {stats['reconnects']} اتصال دوباره، "
            f"{ltr(fmt_pct(stats['clamped_share'], 2))} پیام با زمانی عقب‌تر از ساعت موتور، و "
            f"تأخیر میانه‌ی {ltr(delay)} در برابر {assumed_latency_ms:g} میلی‌ثانیه‌ای که موتور "
            "فرض می‌کند.",
            "",
            f"![جلسه‌ی کاغذی در برابر بازپخش]({base}/{figure})",
            "",
            f"مقایسه‌ی کامل (به انگلیسی): [{base}/COMPARISON.md]({base}/COMPARISON.md)",
        ]
    )
