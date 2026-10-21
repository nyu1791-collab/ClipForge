import pytest

from clipforge import tables
from clipforge.storage import PipelineError

PAGE = """
<div><table class="wikitable"><tbody>
<tr><th>年</th><th>人口<sup>[1]</sup></th></tr>
<tr><td>2020</td><td>1,234万人[2]</td></tr>
<tr><td>2021</td><td><br/>1,200万人</td></tr>
<tr><td>2022</td><td>1,100万人</td></tr>
</tbody></table></div>
<table class="wikitable"><tr><td colspan="2">結合</td></tr><tr><td>a</td><td>b</td></tr></table>
<table class="infobox"><tr><td>関係ない</td></tr></table>
<table class="wikitable"><tr><td>外側<table class="wikitable"><tr><td>内側</td></tr></table></td></tr></table>
"""


def test_wikitables_are_read_with_references_and_merged_tables_dropped():
    parsed = tables.parse_tables(PAGE)
    assert parsed == [
        [
            ["年", "人口"],
            ["2020", "1,234万人"],
            ["2021", "1,200万人"],
            ["2022", "1,100万人"],
        ]
    ]


def test_usable_tables_are_small_rectangular_and_numeric():
    assert tables._usable([["年", "値"], ["1", "2"], ["3", "4"]])
    assert not tables._usable([["年", "値"], ["1", "2"]])  # too few rows
    assert not tables._usable([["a", "b"], ["c"], ["d", "e"]])  # ragged
    assert not tables._usable([["a", "b"], ["c", "d"], ["e", "f"]])  # no numbers
    assert not tables._usable([["x"] * 5, ["1"] * 5, ["2"] * 5])  # too wide


def test_long_cells_are_shortened_for_the_screen():
    parsed = tables.parse_tables(
        '<table class="wikitable"><tr><td>' + "あ" * 40 + "1</td><td>2</td></tr>"
        "<tr><td>3</td><td>4</td></tr><tr><td>5</td><td>6</td></tr></table>"
    )
    assert len(parsed[0][0][0]) == tables.CELL_CHARS


def test_search_records_article_credit_and_licence(monkeypatch):
    rows = [["年", "人口"], ["2020", "1"], ["2021", "2"]]
    monkeypatch.setattr(
        tables, "article_titles", lambda query, limit: ["富士山", "壊れた記事"]
    )

    def article_tables(title):
        if title == "壊れた記事":
            raise PipelineError("not readable")
        return [[["x"]], rows]

    monkeypatch.setattr(tables, "article_tables", article_tables)
    found = tables.search("富士山")
    assert len(found) == 1
    entry = found[0]
    assert entry["rows"] == rows
    assert (
        entry["source_url"]
        == "https://ja.wikipedia.org/wiki/%E5%AF%8C%E5%A3%AB%E5%B1%B1"
    )
    assert entry["license"] == "CC BY-SA 4.0"
    assert (
        "CC BY-SA 4.0" in entry["source_credit"] and "富士山" in entry["source_credit"]
    )


def test_search_without_results_or_with_a_failing_search_is_empty(monkeypatch):
    def unavailable(query, limit):
        raise PipelineError("offline")

    monkeypatch.setattr(tables, "article_titles", unavailable)
    assert tables.search("富士山") == []
    monkeypatch.setattr(tables, "article_titles", lambda query, limit: [])
    assert tables.search("富士山") == []


def test_article_without_parse_output_is_refused(monkeypatch):
    monkeypatch.setattr(
        tables, "_json", lambda url: {"error": {"code": "missingtitle"}}
    )
    with pytest.raises(PipelineError, match="could not be read"):
        tables.article_tables("存在しない記事")
