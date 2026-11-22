"""
Render uptime bars from ``ping.log`` as SVG images for the README.

Each image has three rows: one cell per day, per week and per month. A cell is

* green if at least :data:`GREEN_AT` of the checks in it succeeded
* yellow if some, but fewer than :data:`GREEN_AT`, succeeded
* red if every check in it failed
* gray if there were no checks in it

The *service* image counts a run as up if any processor succeeded in it.
"""

from __future__ import annotations

import argparse
import os
import typing as t
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from .ping import DEFAULT_PROCESSORS, LOG_TIME_FORMAT, parse_processors

GREEN_AT = 0.95

DAYS = 30
WEEKS = 26
MONTHS = 12

COLORS = {
    "green": "#2da44e",
    "yellow": "#d4a72c",
    "red": "#cf222e",
    "none": "#8c959f",
}

SERVICE = "service"

WINDOWS = {"Daily": f"{DAYS}d", "Weekly": f"{WEEKS}w", "Monthly": f"{MONTHS}mo"}


@dataclass(frozen=True)
class Check:
    time: datetime
    processor: str
    up: bool


@dataclass
class Cell:
    label: str
    up: int = 0
    total: int = 0

    @property
    def ratio(self) -> float | None:
        return self.up / self.total if self.total else None

    @property
    def color(self) -> str:
        ratio = self.ratio
        if ratio is None:
            return "none"
        if ratio >= GREEN_AT:
            return "green"
        if ratio > 0:
            return "yellow"
        return "red"


def parse_log(path: Path) -> list[Check]:
    checks: list[Check] = []
    if not path.exists():
        return checks
    for line in path.read_text().splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 3:
            continue
        try:
            when = datetime.strptime(parts[0], LOG_TIME_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        checks.append(Check(when, parts[1], parts[2] == "OK"))
    return checks


def service_checks(checks: list[Check], processors: list[str]) -> list[Check]:
    """One check per run: up if any of the processors was up in that run."""
    runs: dict[datetime, bool] = {}
    for check in checks:
        if check.processor in processors:
            runs[check.time] = runs.get(check.time, False) or check.up
    return [Check(when, SERVICE, up) for when, up in sorted(runs.items())]


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def month_start(day: date) -> date:
    return day.replace(day=1)


def prev_month(day: date) -> date:
    return (day - timedelta(days=1)).replace(day=1)


def build_rows(checks: t.Iterable[Check], today: date) -> dict[str, list[Cell]]:
    days = [today - timedelta(days=i) for i in reversed(range(DAYS))]
    weeks = [week_start(today) - timedelta(weeks=i) for i in reversed(range(WEEKS))]
    months = [month_start(today)]
    while len(months) < MONTHS:
        months.insert(0, prev_month(months[0]))

    rows = {
        "Daily": {d: Cell(d.isoformat()) for d in days},
        "Weekly": {w: Cell(f"week of {w.isoformat()}") for w in weeks},
        "Monthly": {m: Cell(m.strftime("%Y-%m")) for m in months},
    }
    keys: dict[str, t.Callable[[date], date]] = {
        "Daily": lambda d: d,
        "Weekly": week_start,
        "Monthly": month_start,
    }
    for check in checks:
        day = check.time.date()
        for name, cells in rows.items():
            if cell := cells.get(keys[name](day)):
                cell.total += 1
                cell.up += check.up
    return {name: list(cells.values()) for name, cells in rows.items()}


def window_uptime(cells: list[Cell]) -> str:
    up = sum(c.up for c in cells)
    total = sum(c.total for c in cells)
    return f"{100 * up / total:.1f}%" if total else "no data"


def render_svg(title: str, rows: dict[str, list[Cell]]) -> str:
    label_w, bar_w, pct_w = 70, 480, 110
    row_h, cell_h, gap = 22, 16, 2
    top = 26
    width = label_w + bar_w + pct_w
    height = top + row_h * len(rows) + 4
    text = (
        'fill="#8c959f" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif"'
    )

    out = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape(title)} uptime">'
        ),
        (
            f'<text x="0" y="16" font-size="14" font-weight="600" {text}>'
            f"{escape(title)}</text>"
        ),
    ]
    for i, (name, cells) in enumerate(rows.items()):
        y = top + i * row_h
        out.append(
            f'<text x="0" y="{y + cell_h - 4}" font-size="12" {text}>{name}</text>'
        )
        step = bar_w / len(cells)
        for j, cell in enumerate(cells):
            ratio = cell.ratio
            tip = (
                f"{cell.label}: {100 * ratio:.1f}% ({cell.up}/{cell.total})"
                if ratio is not None
                else f"{cell.label}: no data"
            )
            opacity = ' fill-opacity="0.25"' if cell.color == "none" else ""
            out.append(
                f'<rect x="{label_w + j * step:.2f}" y="{y}" '
                f'width="{step - gap:.2f}" height="{cell_h}" rx="2" '
                f'fill="{COLORS[cell.color]}"{opacity}><title>{escape(tip)}</title></rect>'
            )
        out.append(
            f'<text x="{width}" y="{y + cell_h - 4}" font-size="12" '
            f'text-anchor="end" {text}>{window_uptime(cells)} / {WINDOWS[name]}</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def render(
    log: Path, out_dir: Path, processors: list[str], today: date | None = None
) -> list[Path]:
    today = today or datetime.now(timezone.utc).date()
    checks = parse_log(log)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    targets = [(SERVICE, "APPS service", service_checks(checks, processors))]
    targets += [
        (proc, proc, [c for c in checks if c.processor == proc]) for proc in processors
    ]
    for stem, title, subset in targets:
        path = out_dir / f"{stem}.svg"
        path.write_text(render_svg(title, build_rows(subset, today)))
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render uptime SVGs from ping.log.")
    parser.add_argument("log", type=Path, help="The ping log to read.")
    parser.add_argument("out", type=Path, help="Directory to write SVGs to.")
    parser.add_argument(
        "--processors",
        type=parse_processors,
        default=parse_processors(
            os.environ.get("APPS_PING_PROCESSORS") or DEFAULT_PROCESSORS
        ),
        help="Comma separated processor labels (env: APPS_PING_PROCESSORS).",
    )
    args = parser.parse_args(argv)
    for path in render(args.log, args.out, args.processors):
        print(f"Wrote {path}")
    return 0
