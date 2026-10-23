"""Charts: the figure on the bar, not only in the legend (issue #265).

The dashboard's and Analytics' charts had their text equivalents in the
legend and the table under them; someone who can't use the colour, or who
prints in grey, went from the bar to the legend and back. Every bar, slice
and point now carries its figure as text; what would land on another
label, or outside the chart, is left off and the table has it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "static" / "js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="no node")


def _probe(theme="light"):
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "chart_figures_probe.js"), theme],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_compact_money_is_the_size_a_label_can_hold():
    got = _probe()["compact"]
    assert got == ["$1.2k", "$12k", "$3.4M", "-$850", "$45.50", "$999", "$0", "$12M"]


@needs_node
def test_bars_carry_their_figure_past_the_end_or_inside_when_there_is_no_room():
    bars = _probe()["bars"]
    by = {b["text"]: b for b in bars}
    # 150: above the bar's top (y 120), in the theme's ink
    assert by["$150"]["y"] < 120 and by["$150"]["fill"] == "#1a1a28"
    # -40: below the bar's end (y 180)
    assert by["-$40"]["y"] > 180
    # 9999: the top is at y 3, no room above — inside the bar, in text that
    # reads on the bar's dark blue
    assert by["$10k"]["y"] > 3 and by["$10k"]["fill"] == "#ffffff"
    # a zero bar has nothing to say
    assert len(bars) == 3


@needs_node
def test_a_stacked_segment_is_labelled_only_when_it_can_hold_the_text():
    got = _probe()
    stacked = got["stacked"]
    assert [s["text"] for s in stacked] == ["$2.8k"]
    assert stacked[0]["x"] == 160 and stacked[0]["y"] == 50  # the segment's middle
    assert stacked[0]["fill"] == "#1a1a28"  # dark text on the pale segment
    [h] = got["hbar"]
    assert h["text"] == "$640" and h["x"] > 120 and h["y"] == 80  # past the end


@needs_node
def test_a_bar_thinner_than_the_text_carries_no_figure():
    got = _probe()
    assert got["thin"] == [] and got["cramped"] == []


@needs_node
def test_slices_carry_their_figure_except_one_too_thin_to_hold_it():
    slices = _probe()["slices"]
    assert [(s["text"], s["fill"]) for s in slices] == [
        ("$7k", "#ffffff"),  # white on the dark slice
        ("$2.7k", "#1a1a28"),  # dark on the pale one
    ]


@needs_node
def test_a_figure_that_cannot_reach_aa_on_its_mark_is_left_off():
    """The doughnut's red gives ink 4.39:1 and white less; a figure drawn
    there failed AA (NEW-75). Neither reaches 4.5, so the slice keeps its
    figure in the table, as the dashboard's aging bar does; the dark slice
    beside it is labelled. A stacked segment in that colour is left bare
    too."""
    got = _probe()
    assert [(s["text"], s["fill"]) for s in got["lowcontrast"]] == [("$7k", "#ffffff")]
    assert got["lowcontrast_bar"] == []


@needs_node
def test_a_point_on_the_edge_or_the_top_keeps_its_figure():
    """The trend's first and last months sit on the area's edges and the
    axis maximum on its top; their labels fell outside and were left off,
    and the card had no table (NEW-81 / W-5). A point's label slides in
    from the edge; the top point's goes below it."""
    got = _probe()["edges"]
    by = {p["text"]: (p["x"], p["y"]) for p in got}
    assert [p["text"] for p in sorted(got, key=lambda p: p["x"])] == [
        "$1k",
        "$2.5k",
        "$1k",
    ]
    xs = sorted(p["x"] for p in got)
    assert (
        xs[0] == 11 and xs[-1] == 389
    )  # slid in from the edges (6px per character + 4)
    assert by["$2.5k"] == (200, 12)  # below the point on the top edge, inside the area
    assert all(
        p["y"] < 40 for p in got if p["text"] == "$1k"
    )  # the edge points' stay above


@needs_node
def test_points_are_labelled_where_the_labels_do_not_collide():
    lines = _probe()["lines"]
    texts = [(p["text"], p["x"], p["y"]) for p in lines]
    # the first line's labels sit above, the second's below; at x 40 and 60
    # the two "$1k" labels would overlap (6px per character), so the second
    # moves to its own side's corner (x 81) rather than vanishing, and the
    # second line's does the same below — neither crosses to the other side
    above = [t for t in texts if t[0] == "$1k"]
    below = [t for t in texts if t[0] == "$2k"]
    assert [t[1] for t in above] == [40, 81, 200] and all(t[2] < 100 for t in above)
    assert [t[1] for t in below] == [40, 81, 200] and all(t[2] > 104 for t in below)


@needs_node
def test_a_figure_never_sits_on_its_own_line():
    """A steep rise into a last-point maximum with no room above (W-6: the
    label under the peak sat on the line rising into it). Above is off the
    chart and below is crossed, so the label goes beside the point, to its
    left, clear of the line; the middle point's own line crosses the place
    straight above it, so its label takes the corner above; the first
    point's stays above. When the line's curve bulges through every place
    beside the peak, the label is left off and the table has it."""
    got = _probe()
    peak = {p["text"]: (p["x"], p["y"]) for p in got["peak"]}
    assert peak["$32"] == (379, 32)  # beside the point: 400 − 11 (half the text) − 10
    assert peak["$272"] == (176, 260) and peak["$290"] == (14, 278)
    bulge = {p["text"]: (p["x"], p["y"]) for p in got["bulge"]}
    assert "$32" not in bulge and bulge["$272"] == (200, 260)


@needs_node
def test_a_hidden_dataset_and_the_dark_theme():
    assert _probe()["hidden"] == []
    dark = _probe("dark")["bars"]
    assert {b["text"]: b["fill"] for b in dark}["$150"] == "#e6e6f0"


def test_every_chart_draws_its_figures():
    dash = (JS / "dashboard.js").read_text(encoding="utf-8")
    ana = (JS / "analytics.js").read_text(encoding="utf-8")
    for src, name in ((dash, "dashboard.js"), (ana, "analytics.js")):
        charts = len(re.findall(r"new Chart\(", src))
        assert charts == src.count("plugins: [chartFigures()]"), name
    assert dash.count("new Chart(") == 1 and ana.count("new Chart(") == 4
    # the CSS-drawn dashboard bars too: the aging segments and monthly revenue
    assert (
        "const figure = v / owed >= 0.14 && text.ratio >= 4.5 ? compactMoney(v) : '';"
        in dash
    )
    assert "figureTextOn(cssVar(`--aging-${key}`), dark)" in dash
    assert "${m.amount ? compactMoney(m.amount) : ''}" in dash
    # the trend's card grew to hold its labels
    assert 'height:200px"><canvas id="chart-bs-trend">' in dash
    # a theme toggle repaints the cards, so the aging figures pick their ink again (NEW-60)
    listener = dash[dash.index("document.addEventListener('slowbooks:themechange'") :]
    assert (
        "DashboardPage._repaint();" in listener
        and "DashboardPage._renderBsTrendChart();" in listener
    )
    # the legend's swatches are squares, not "■" text (NEW-62)
    assert (
        '<span aria-hidden="true" class="aging-swatch" style="background:var(--aging-current)"></span>'
        in dash
    )
    assert "■" not in dash
    # the aging charts name every customer and vendor, and grow to do it (W-6)
    assert "ticks: { color: theme.muted, autoSkip: false }" in ana
    assert "wrap.style.height = `${Math.max(220, names.length * 24 + 80)}px`;" in ana


def test_the_open_item_is_closed_in_the_statement():
    docs = (ROOT / "docs/accessibility.md").read_text(encoding="utf-8")
    assert (
        "text equivalents in the legend"
        not in docs.split("## What we know is still open")[1]
    )
    assert "carries its figure" in docs
