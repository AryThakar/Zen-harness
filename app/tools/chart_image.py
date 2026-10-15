"""Charts as files: a chart a tool made (make_chart, forecast, find_patterns' unusual periods) or a script made
(chart() in the sandbox) is a chart file - charts/<its title>.chart.json, the data and how to draw it. The page
draws it live (Chart.js: animated, values on hover); where a picture is needed - a Word document or a slide deck
(files' write: ![caption](charts/sales.chart.json)), a download - it is drawn from that same file by matplotlib,
and kept only as a cache beside it. Measured before: every chart was also kept as a PNG item (10 of a workspace's
39 pictures), and answers showed the chart twice.

The same chart made again from the same table (the same title) is the same file, its new version.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from .core import jsonable
from .image import file_name
from .workspace import Workspace

log = logging.getLogger(__name__)
SUFFIX = ".chart.json"
DRAWING = 2  # raised when the drawing changes: every cached picture is drawn again (2: a box chart's axis titles)

SERIES = ["#5a51c7", "#e07b26", "#1f9e74", "#d9468f", "#1f8fcf", "#d64545"]
DATA, SOFT, INK, LINE = "#5a51c7", "#8f8f8f", "#1d1d1f", "#e3e3e8"
MAX_TICKS = 12


def save_chart(ws: Workspace, artifact: dict, parent: str | None = None) -> str:
    """The chart kept as its chart file; returns its key (the card's id, so the page knows it once). The same
    title from the same table again is the same file, its next version."""
    spec = jsonable({k: v for k, v in artifact.items() if k not in ("type", "tool", "id")})
    data = json.dumps(spec, ensure_ascii=False).encode("utf-8")
    path = "charts/" + file_name(str(spec.get("title") or "chart"), SUFFIX.lstrip("."))
    meta = {"chart": spec.get("chart"), "title": spec.get("title"), "points": _points(spec)}
    same = ws.find_path(path)
    if same and ws.entry(same).get("parent") == parent and not ws.by_user(same):
        ws.put("", data, key=same, meta=meta)
        return same
    return ws.put(path, data, kind="file", parent=parent, meta=meta).key


def is_chart(name: str) -> bool:
    return str(name).lower().endswith(SUFFIX)


def describe_chart(ws: Workspace, key: str) -> bool:
    """A chart file a script wrote, described like a tool's (its kind, title and points); False when it isn't a
    chart the page can draw."""
    try:
        spec = json.loads(ws.path(key).read_text("utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(spec, dict) or not spec.get("chart"):
        return False
    for name, value in (("chart", spec["chart"]), ("title", spec.get("title")), ("points", _points(spec))):
        ws.set_meta(key, name, value)
    return True


def spec_of(ws: Workspace, key: str) -> dict:
    """A chart file's chart: the artifact the page draws."""
    return {"type": "chart", **json.loads(ws.path(key).read_text("utf-8")), "id": key}


def picture_of(ws: Workspace, key: str) -> Path | None:
    """The chart file drawn as a PNG (for a document, a slide or a download), cached beside it until the chart
    changes; None when it can't be drawn."""
    source = ws.path(key)
    cached = ws.zen / "cache" / f"{key}.v{DRAWING}.png"
    if cached.exists() and cached.stat().st_mtime >= source.stat().st_mtime:
        return cached
    try:
        png = draw(spec_of(ws, key))
    except Exception:  # noqa: BLE001 - a chart without its picture is still a chart
        log.exception("chart %s could not be drawn as a picture", key)
        return None
    if png is None:
        return None
    cached.write_bytes(png)
    return cached


def table_of(a: dict) -> tuple[list[str], list[list]]:
    """A chart's numbers as a table - (columns, rows) - for its CSV download: what each kind draws, one row a point
    (a category, a period, a bin, a box, a heatmap row)."""
    kind, x = a.get("chart"), a.get("x") or []
    first = str(a.get("x_label") or ("period" if kind in ("forecast", "unusual") else "x"))
    value = str(a.get("y_label") or "value") + (f" ({a['unit']})" if a.get("unit") else "")
    series = a.get("series") or []
    if kind == "box":
        stats = ("min", "q1", "median", "q3", "max")
        return [first, *stats], [[n, *(b.get(s) for s in stats)] for n, b in zip(x, a.get("boxes") or [])]
    if kind == "heatmap":
        return [str(a.get("y_label") or ""), *map(str, x)], [[r, *row] for r, row in zip(a.get("rows") or [], a.get("values") or [])]
    if kind == "scatter":
        groups = a.get("groups")
        return ([first, value] + ([str(a.get("group") or "group")] if groups else []),
                [[px, py, *([groups[i]] if groups else [])] for i, (px, py) in enumerate(zip(x, a.get("y") or []))])
    if kind == "unusual":
        flagged = set(a.get("flagged") or [])
        return [first, value, "expected", "unusual"], [[d, v, e, "yes" if d in flagged else ""]
                                                         for d, v, e in zip(x, a.get("y") or [], a.get("expected") or [])]
    if kind == "forecast":
        lines = series or [{"name": str(a.get("y_label") or "value"),
                            **{k: a.get(k) for k in ("x", "y", "forecast_x", "forecast", "low", "high")}}]
        periods = sorted({d for s in lines for d in (s.get("x") or []) + (s.get("forecast_x") or [])})
        columns, cells = [first], []
        for s in lines:
            name = str(s.get("name") or "value")
            columns += [name, f"{name} forecast"] + ([f"{name} low", f"{name} high"] if s.get("low") else [])
            seen = dict(zip(s.get("x") or [], s.get("y") or []))
            ahead = {d: i for i, d in enumerate(s.get("forecast_x") or [])}
            cells.append(lambda d, s=s, seen=seen, ahead=ahead: [
                seen.get(d), (s.get("forecast") or [None] * len(ahead))[ahead[d]] if d in ahead else None,
                *([s["low"][ahead[d]] if d in ahead else None, s["high"][ahead[d]] if d in ahead else None]
                  if s.get("low") else [])])
        return columns, [[d, *(v for cell in cells for v in cell(d))] for d in periods]
    if series:  # line, area, bar, histogram with groups: one column a group
        return [first, *(str(s.get("name")) for s in series)], [[d, *((s.get("y") or [None] * len(x))[i] for s in series)]
                                                              for i, d in enumerate(x)]
    return [first, value], [list(p) for p in zip(x, a.get("y") or [])]


def _points(spec: dict) -> int:
    for k in ("y", "x", "values", "boxes"):
        if isinstance(spec.get(k), list):
            return len(spec[k])
    return sum(len(s.get("y") or []) for s in spec.get("series") or [])


def draw(a: dict) -> bytes | None:
    """PNG bytes of a chart artifact, or None for a kind this doesn't know."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    kind = a.get("chart")
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    try:
        drawn = {"line": _line, "area": _area, "bar": _bar, "scatter": _scatter, "histogram": _histogram, "box": _box,
                 "heatmap": _heatmap, "pie": _pie, "forecast": _forecast, "unusual": _unusual}.get(kind)
        if drawn is None:
            return None
        drawn(ax, a)
        ax.set_title(str(a.get("title") or ""), loc="left", fontsize=12, color=INK, pad=12)
        if kind not in ("pie", "heatmap"):
            _style(ax, a)
        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format="png", facecolor="white")
        return out.getvalue()
    finally:
        plt.close(fig)


def _style(ax, a: dict) -> None:
    ax.spines[["top", "right"]].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(LINE)
    ax.tick_params(colors=SOFT, labelsize=8)
    sideways = a.get("chart") == "box"  # drawn lying down, as on the page: the values run along the bottom
    ax.grid(axis="x" if sideways else "y", color=LINE, linewidth=0.8)
    ax.set_axisbelow(True)
    groups, values = ax.set_ylabel if sideways else ax.set_xlabel, ax.set_xlabel if sideways else ax.set_ylabel
    if a.get("x_label"):
        groups(str(a["x_label"]), color=SOFT, fontsize=9)
    if a.get("y_label"):
        values(str(a["y_label"]) + (f" ({a['unit']})" if a.get("unit") else ""), color=SOFT, fontsize=9)
    if len(ax.get_legend_handles_labels()[0]) > 1:
        ax.legend(frameon=False, fontsize=8)


def _labels(ax, labels: list) -> None:
    """Category or date labels, thinned to at most MAX_TICKS."""
    step = max(1, -(-len(labels) // MAX_TICKS))
    ax.set_xticks(range(0, len(labels), step), [str(v)[:16] for v in labels[::step]],
                  rotation=30 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")


def _values(v: list) -> list[float]:
    return [float("nan") if x is None else x for x in v]


def _line(ax, a):
    x = a.get("x") or []
    for i, s in enumerate(a.get("series") or [{"name": a.get("y_label") or "value", "y": a.get("y") or []}]):
        ax.plot(range(len(x)), _values(s["y"]), color=SERIES[i % 6] if a.get("series") else DATA, linewidth=2,
                label=str(s["name"]))
    _labels(ax, x)


def _bar(ax, a):
    x = a.get("x") or []
    series = a.get("series")
    if not series:
        ax.bar(range(len(x)), _values(a.get("y") or []), color=DATA, width=0.7)
    else:
        bottom = [0.0] * len(x)
        width = 0.8 / (1 if a.get("stacked") else len(series))
        for i, s in enumerate(series):
            y = [0.0 if v is None else v for v in s["y"]]
            if a.get("stacked"):
                ax.bar(range(len(x)), y, bottom=bottom, color=SERIES[i % 6], width=0.7, label=str(s["name"]))
                bottom = [b + v for b, v in zip(bottom, y)]
            else:
                ax.bar([k - 0.4 + width * (i + 0.5) for k in range(len(x))], y, width=width, color=SERIES[i % 6],
                       label=str(s["name"]))
    _labels(ax, x)


def _scatter(ax, a):
    x, y = a.get("x") or [], a.get("y") or []
    groups, of = a.get("groups"), a.get("group")
    if groups and of:
        for g, name in enumerate(groups):
            pts = [(xv, yv) for xv, yv, k in zip(x, y, of) if k == g]
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=12, alpha=0.7, label=str(name),
                       color=SOFT if name == "other" else SERIES[g % 6])
    else:
        ax.scatter(x, y, s=12, alpha=0.7, color=DATA)
    if a.get("trend"):
        (x0, y0), (x1, y1) = a["trend"]
        ax.plot([x0, x1], [y0, y1], color=SOFT, linestyle="--", linewidth=1.2)


def _area(ax, a):
    x = a.get("x") or []
    series = a.get("series") or [{"name": a.get("y_label") or "value", "y": a.get("y") or []}]
    ys = [[0.0 if v is None else v for v in s["y"]] for s in series]
    if a.get("series"):
        ax.stackplot(range(len(x)), *ys, labels=[str(s["name"]) for s in series], colors=SERIES[:len(ys)], alpha=0.8)
    else:
        ax.fill_between(range(len(x)), ys[0], color=DATA, alpha=0.3)
        ax.plot(range(len(x)), ys[0], color=DATA, linewidth=2)
    _labels(ax, x)


def _histogram(ax, a):
    edges, width = a.get("x") or [], a.get("bin_width") or 1
    if a.get("series"):  # one spread per group, the same bins, drawn over each other
        for i, s in enumerate(a["series"]):
            ax.bar(edges, s["y"], width=width, align="edge", color=SERIES[i % 6], alpha=0.55, label=str(s["name"]),
                   edgecolor="white", linewidth=0.5)
        return
    ax.bar(edges, a.get("y") or [], width=width, align="edge", color=DATA, edgecolor="white", linewidth=0.5)


def _box(ax, a):
    boxes = a.get("boxes") or []
    stats = [{"med": b["median"], "q1": b["q1"], "q3": b["q3"], "whislo": b["min"], "whishi": b["max"],
              "fliers": b.get("outliers") or [], "label": str(n)[:16]} for n, b in zip(a.get("x") or [], boxes)]
    ax.bxp(stats, orientation="horizontal", patch_artist=True, boxprops={"facecolor": "#e6e4fb", "edgecolor": DATA},
           medianprops={"color": INK}, flierprops={"markerfacecolor": SERIES[1], "markeredgecolor": "none", "markersize": 4})


def _heatmap(ax, a):
    values = [_values(row) for row in a.get("values") or []]
    image = ax.imshow(values, cmap="Purples", aspect="auto")
    cols, rows = a.get("x") or [], a.get("rows") or []
    ax.set_xticks(range(len(cols)), [str(c)[:12] for c in cols], rotation=45 if len(cols) > 6 else 0,
                  ha="right" if len(cols) > 6 else "center", fontsize=8)
    ax.set_yticks(range(len(rows)), [str(r)[:16] for r in rows], fontsize=8)
    ax.figure.colorbar(image, ax=ax, shrink=0.8)


def _pie(ax, a):
    x, y = a.get("x") or [], a.get("y") or []
    colors = [SOFT if n == "other" else SERIES[i % 6] for i, n in enumerate(x)]
    ax.pie(y, labels=[str(n)[:20] for n in x], colors=colors, autopct="%1.0f%%", startangle=90,
           wedgeprops={"width": 0.4, "edgecolor": "white"}, textprops={"fontsize": 8})
    ax.axis("equal")


def _forecast(ax, a):
    series = a.get("series") or [{"name": a.get("y_label") or "value", **{k: a.get(k) for k in
                                                                         ("x", "y", "forecast_x", "forecast", "low", "high")}}]
    labels = sorted({d for s in series for d in (s.get("x") or []) + (s.get("forecast_x") or [])})
    at = {d: i for i, d in enumerate(labels)}
    for i, s in enumerate(series):
        color = SERIES[i % 6] if a.get("series") else DATA
        hx, fx = s.get("x") or [], s.get("forecast_x") or []
        ax.plot([at[d] for d in hx], _values(s.get("y") or []), color=SOFT if not a.get("series") else color,
                linewidth=1.6, label=str(s["name"]) if a.get("series") else "actual")
        if fx:
            joined = ([at[hx[-1]]] if hx else []) + [at[d] for d in fx]
            ax.plot(joined, ([s["y"][-1]] if hx else []) + _values(s.get("forecast") or []), color=color,
                    linestyle="--", linewidth=2, label=None if a.get("series") else "forecast")
            if s.get("low") and s.get("high") and not a.get("series"):
                ax.fill_between([at[d] for d in fx], _values(s["low"]), _values(s["high"]), color=color, alpha=0.15,
                                linewidth=0, label="80% range")
    _labels(ax, labels)


def _unusual(ax, a):
    x, y = a.get("x") or [], _values(a.get("y") or [])
    ax.plot(range(len(x)), y, color=DATA, linewidth=1.6, label="actual")
    ax.plot(range(len(x)), _values(a.get("expected") or []), color=SOFT, linestyle="--", linewidth=1.2, label="expected")
    flagged = set(a.get("flagged") or [])
    pts = [(i, v) for i, (d, v) in enumerate(zip(x, y)) if d in flagged]
    if pts:
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], color="#d64545", s=30, zorder=3, label="unusual")
    _labels(ax, x)
