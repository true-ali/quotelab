"""A self-contained HTML dashboard for the report sections, with no network or external assets.

It renders the same ``Section`` list as ``RESULTS.md``, so the two views can never disagree. The
page opens straight from disk, so all CSS and JavaScript are inlined, and every text value is
escaped before it reaches the markup.
"""

from __future__ import annotations

import html
import re

from quotelab.report.document import Section, Table

_OVERVIEW = "overview"

_NUMBER = r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?%?"
_NUMERIC = re.compile(rf"(?:{_NUMBER}(?: \[{_NUMBER}, {_NUMBER}\])?|n/a)")
_CODE = re.compile(r"`([^`]+)`")

_CSS = """
:root {
  color-scheme: light;
  --bg: #ffffff;
  --surface: #f2f4f7;
  --zebra: #f9fafc;
  --text: #1d2530;
  --muted: #5d6675;
  --border: #d9dee6;
  --accent: #2d5f8b;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.55 var(--font);
}
.wrap { max-width: 1400px; margin: 0 auto; padding-inline: 16px; }
.site-header { border-bottom: 1px solid var(--border); }
.masthead { padding-block: 24px 16px; }
h1 { margin: 0; font-size: 1.5rem; line-height: 1.25; overflow-wrap: anywhere; }
.subtitle { margin: 4px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.tab {
  font: inherit;
  font-size: 0.9rem;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
}
.tab:hover { border-color: var(--accent); }
.tab[aria-selected="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: #ffffff;
}
.tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.content { padding-block: 24px 48px; }
.panel h2 { margin: 0 0 12px; font-size: 1.25rem; line-height: 1.3; overflow-wrap: anywhere; }
.panel p { max-width: 90ch; margin: 0 0 12px; overflow-wrap: anywhere; }
code {
  font-family: var(--mono);
  font-size: 0.9em;
  background: var(--surface);
  padding: 0.1em 0.35em;
  border-radius: 4px;
  overflow-wrap: anywhere;
}
.figure { margin: 16px 0 24px; }
.figure a { display: block; }
.figure img {
  display: block;
  max-width: 100%;
  height: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.table-wrap {
  max-height: 70vh;
  overflow: auto;
  margin: 16px 0 28px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 0.9rem;
  font-variant-numeric: tabular-nums;
}
caption { caption-side: top; padding: 10px 12px; font-weight: 600; text-align: left; }
th, td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}
th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface);
  font-weight: 600;
}
tbody tr:nth-child(even) td { background: var(--zebra); }
td.num { text-align: right; white-space: nowrap; }
@media (max-width: 600px) { h1 { font-size: 1.3rem; } }
"""

_JS = """
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  function anchorFromHash() {
    var anchor = location.hash.slice(1);
    var known = tabs.some(function (tab) { return tab.dataset.anchor === anchor; });
    return known ? anchor : tabs[0].dataset.anchor;
  }
  function select(anchor) {
    tabs.forEach(function (tab) {
      var active = tab.dataset.anchor === anchor;
      tab.setAttribute("aria-selected", String(active));
      document.getElementById("panel-" + tab.dataset.anchor).hidden = !active;
    });
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      select(tab.dataset.anchor);
      history.replaceState(null, "", "#" + tab.dataset.anchor);
    });
  });
  window.addEventListener("hashchange", function () { select(anchorFromHash()); });
  select(anchorFromHash());
})();
"""


def _inline(text: str) -> str:
    """Escape ``text`` for HTML, then turn backtick spans into ``code``; nothing else is markup."""
    return _CODE.sub(r"<code>\1</code>", html.escape(text))


def _paragraphs_html(paragraphs: list[str]) -> str:
    """Paragraph elements for ``paragraphs``, each one escaped."""
    return "".join(f"<p>{_inline(paragraph)}</p>" for paragraph in paragraphs)


def _cell_html(cell: str) -> str:
    """A table cell, right-aligned when its text is a number, a number with a CI, or ``n/a``."""
    align = ' class="num"' if _NUMERIC.fullmatch(cell.strip()) else ""
    return f"<td{align}>{_inline(cell)}</td>"


def _table_html(table: Table) -> str:
    """A table in a scrollable wrapper, with its title as caption and a sticky header row."""
    head = "".join(f'<th scope="col">{_inline(column)}</th>' for column in table.columns)
    body = "".join(
        "<tr>" + "".join(_cell_html(cell) for cell in row) + "</tr>" for row in table.rows
    )
    return (
        '<div class="table-wrap"><table>'
        f"<caption>{_inline(table.title)}</caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _figure_html(section: Section) -> str:
    """The section figure as a link to the full-size image; empty when the section has none."""
    if not section.figure:
        return ""
    src = html.escape(section.figure)
    alt = html.escape(section.title)
    return (
        f'<div class="figure"><a href="{src}" target="_blank" rel="noopener">'
        f'<img src="{src}" alt="{alt}"></a></div>'
    )


def _section_body(section: Section) -> str:
    """Heading, paragraphs, figure and tables of a section, in reading order."""
    heading = f"<h2>{_inline(section.title)}</h2>"
    tables = "".join(_table_html(table) for table in section.tables)
    return heading + _paragraphs_html(section.paragraphs) + _figure_html(section) + tables


def _panel_html(anchor: str, body: str, selected: bool) -> str:
    """A tab panel labelled by its tab; unselected panels start hidden so one shows at a time."""
    key = html.escape(anchor)
    hidden = "" if selected else " hidden"
    return (
        f'<section class="panel" role="tabpanel" id="panel-{key}" '
        f'aria-labelledby="tab-{key}"{hidden}>{body}</section>'
    )


def _tab_html(anchor: str, label: str, selected: bool) -> str:
    """A tab button linked to its panel; the raw anchor sits in ``data-anchor`` for the script."""
    key = html.escape(anchor)
    chosen = "true" if selected else "false"
    return (
        f'<button type="button" class="tab" role="tab" id="tab-{key}" data-anchor="{key}" '
        f'aria-controls="panel-{key}" aria-selected="{chosen}">{_inline(label)}</button>'
    )


def render_dashboard(title: str, subtitle: str, intro: list[str], sections: list[Section]) -> str:
    """A complete HTML5 page: header, then an Overview tab with ``intro`` and one tab per section.

    The result depends only on the arguments, so rendering the same report twice gives identical
    text, and the page needs no network access to open.
    """
    tabs = [_tab_html(_OVERVIEW, "Overview", selected=True)]
    panels = [_panel_html(_OVERVIEW, _paragraphs_html(intro), selected=True)]
    for section in sections:
        tabs.append(_tab_html(section.anchor, section.title, selected=False))
        panels.append(_panel_html(section.anchor, _section_body(section), selected=False))
    tab_buttons = "".join(tabs)
    panel_html = "".join(panels)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="site-header">
<div class="wrap masthead">
<h1>{_inline(title)}</h1>
<p class="subtitle">{_inline(subtitle)}</p>
<div class="tabs" role="tablist" aria-label="Report sections">{tab_buttons}</div>
</div>
</header>
<main class="wrap content">{panel_html}</main>
<script>{_JS}</script>
</body>
</html>
"""
