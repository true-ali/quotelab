"""The report as plain data (sections of paragraphs, tables and a figure) and its Markdown form.

``RESULTS.md`` and the HTML dashboard render the same ``Section`` list, so they can never show
different numbers. Cells are formatted strings by the time they reach a ``Table``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Table:
    """A titled table of already formatted cells."""

    title: str
    columns: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class Section:
    """One block of the report. ``figure`` is a path relative to the report directory."""

    anchor: str
    title: str
    paragraphs: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    figure: str | None = None


def fmt(value: float | None, digits: int = 2) -> str:
    """A number with fixed decimals and thousands separators; ``n/a`` for missing values."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:,.{digits}f}"


def fmt_ci(mean: float, lo: float, hi: float, digits: int = 2) -> str:
    """``mean [lo, hi]``, or just the mean when the interval is undefined (one day)."""
    if math.isnan(lo) or math.isnan(hi):
        return fmt(mean, digits)
    return f"{fmt(mean, digits)} [{fmt(lo, digits)}, {fmt(hi, digits)}]"


def fmt_pct(share: float | None, digits: int = 1) -> str:
    return "n/a" if share is None or math.isnan(share) else f"{share * 100:.{digits}f}%"


def markdown_row(cells: list[str]) -> str:
    """One Markdown table row; a literal ``|`` in a cell is escaped so it cannot split the cell."""
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def markdown_table(table: Table) -> str:
    header = markdown_row(table.columns)
    rule = "|" + "|".join("---" for _ in table.columns) + "|"
    body = [markdown_row(row) for row in table.rows]
    return "\n".join([f"**{table.title}**", "", header, rule, *body])


def markdown_section(section: Section, level: int = 2) -> str:
    parts = [f"{'#' * level} {section.title}", ""]
    for paragraph in section.paragraphs:
        parts += [paragraph, ""]
    if section.figure:
        parts += [f"![{section.title}]({section.figure})", ""]
    for table in section.tables:
        parts += [markdown_table(table), ""]
    return "\n".join(parts)


def markdown_document(title: str, intro: list[str], sections: list[Section]) -> str:
    parts = [f"# {title}", ""]
    for paragraph in intro:
        parts += [paragraph, ""]
    parts += ["## Contents", ""]
    parts += [f"- [{section.title}](#{section.anchor})" for section in sections]
    parts.append("")
    for section in sections:
        parts += [f'<a id="{section.anchor}"></a>', "", markdown_section(section)]
    return "\n".join(parts).rstrip() + "\n"
