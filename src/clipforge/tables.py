"""Data tables from Japanese Wikipedia articles, with the article as their source.

The text of these articles is licensed CC BY-SA 4.0 (older revisions CC BY-SA 3.0 and
GFDL). Each table keeps its article URL, credit and licence in the clip's ledger, and the
credit is shown on screen. Only plain wikitables are used: merged cells are skipped.
"""

from html.parser import HTMLParser
import re
import urllib.parse

from .assets import _json
from .storage import PipelineError

API = "https://ja.wikipedia.org/w/api.php"
ARTICLE = "https://ja.wikipedia.org/wiki/"
LICENSE = "CC BY-SA 4.0"
MIN_ROWS, MAX_ROWS = 3, 12
MIN_COLUMNS, MAX_COLUMNS = 2, 4
CELL_CHARS = 24
REFERENCE = re.compile(r"\[[^\]]*\]")


def _clean(text):
    text = re.sub(r"\s+", " ", REFERENCE.sub("", text)).strip()
    return text if len(text) <= CELL_CHARS else text[: CELL_CHARS - 1] + "…"


class _TableParser(HTMLParser):
    """Collects the rows of each top-level wikitable; tables with merged cells are dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._depth = 0
        self._table = None
        self._bad = False
        self._row = None
        self._cell = None
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "table":
            self._depth += 1
            if self._depth == 1 and "wikitable" in (values.get("class") or "").split():
                self._table, self._bad = [], False
            elif self._table is not None:
                self._bad = True  # a table nested inside a wikitable
        elif self._table is None:
            return
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            for key in ("colspan", "rowspan"):
                span = (values.get(key) or "1").strip()
                if not span.isdigit() or int(span) > 1:
                    self._bad = True
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag in ("sup", "style", "script"):
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag == "table":
            if self._table is not None and self._depth == 1:
                if not self._bad:
                    self.tables.append(self._table)
                self._table = None
            self._depth = max(0, self._depth - 1)
        elif self._table is None:
            return
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag in ("sup", "style", "script"):
            self._hidden = max(0, self._hidden - 1)

    def handle_data(self, data):
        if self._cell is not None and not self._hidden:
            self._cell.append(data)


def _usable(rows):
    """Rectangular, small enough to read on screen, and containing at least one number."""
    if not MIN_ROWS <= len(rows) <= MAX_ROWS:
        return False
    widths = {len(row) for row in rows}
    if len(widths) != 1 or not MIN_COLUMNS <= widths.pop() <= MAX_COLUMNS:
        return False
    return any(ch.isdigit() for row in rows for cell in row for ch in cell)


def parse_tables(page_html):
    """Every wikitable in the HTML, as lists of cleaned cell texts."""
    parser = _TableParser()
    parser.feed(page_html)
    parser.close()
    return parser.tables


def article_titles(query, limit):
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srnamespace": 0,
        "srlimit": limit,
        "format": "json",
        "formatversion": 2,
    }
    data = _json(API + "?" + urllib.parse.urlencode(params))
    return [item["title"] for item in data.get("query", {}).get("search", [])]


def article_tables(title):
    params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "redirects": 1,
        "format": "json",
        "formatversion": 2,
    }
    data = _json(API + "?" + urllib.parse.urlencode(params))
    try:
        page_html = data["parse"]["text"]
    except (KeyError, TypeError) as error:
        raise PipelineError("Wikipedia article could not be read") from error
    return parse_tables(page_html)


def search(query, limit=3):
    """Usable tables from the top articles for query, in search-rank order.

    Each entry carries the article URL, the credit line and the licence for the ledger.
    A failing article is skipped; a failing search returns what it can.
    """
    found = []
    try:
        titles = article_titles(query, limit)
    except PipelineError:
        return found
    for title in titles:
        try:
            tables = article_tables(title)
        except PipelineError:
            continue
        for rows in tables:
            if _usable(rows):
                found.append(
                    {
                        "provider": "wikipedia-ja",
                        "title": title,
                        "rows": rows,
                        "source_url": ARTICLE
                        + urllib.parse.quote(title.replace(" ", "_")),
                        "source_credit": f"日本語版Wikipedia「{title}」の表 / {LICENSE}",
                        "license": LICENSE,
                    }
                )
                break
    return found
