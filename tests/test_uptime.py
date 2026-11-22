from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from apps_ping import uptime
from apps_ping.uptime import Cell, Check

TODAY = datetime(2026, 11, 22, tzinfo=timezone.utc)


def at(day, hour=0):
    return day + timedelta(hours=hour)


def test_cell_colors():
    assert Cell("x").color == "none"
    assert Cell("x", up=24, total=24).color == "green"
    assert Cell("x", up=23, total=24).color == "yellow"
    assert Cell("x", up=22, total=24).color == "yellow"  # 91.7%
    assert Cell("x", up=1, total=24).color == "yellow"
    assert Cell("x", up=0, total=24).color == "red"


def test_parse_log_skips_junk(tmp_path):
    log = tmp_path / "ping.log"
    log.write_text(
        "2026-11-22T04:39:50Z OK\n"  # legacy line without a processor
        "garbage line here\n"
        "2026-11-22T05:17:00Z apps01ca OK previous ping still processing\n"
        "2026-11-22T06:17:00Z apps01ca FAIL Upload failed\n"
    )
    checks = uptime.parse_log(log)
    assert [(c.processor, c.up) for c in checks] == [
        ("apps01ca", True),
        ("apps01ca", False),
    ]
    assert uptime.parse_log(tmp_path / "missing.log") == []


def test_service_is_up_if_any_processor_is_up():
    t1, t2, t3 = at(TODAY, 1), at(TODAY, 2), at(TODAY, 3)
    checks = [
        Check(t1, "a", False),
        Check(t1, "b", True),
        Check(t2, "a", False),
        Check(t2, "b", False),
        Check(t3, "a", True),
        Check(t3, "gone", False),  # not configured any more
    ]
    service = uptime.service_checks(checks, ["a", "b"])
    assert [c.up for c in service] == [True, False, True]


NOW = datetime(2026, 11, 22, 5, 30, tzinfo=timezone.utc)


def test_build_rows_buckets():
    checks = [
        Check(NOW - timedelta(minutes=10), "a", True),  # 05:20, current hour
        Check(NOW - timedelta(hours=1), "a", False),  # 04:30
        Check(NOW - timedelta(hours=23), "a", True),  # 06:30 yesterday
        Check(NOW - timedelta(hours=24), "a", True),  # 05:30 yesterday, too old
        Check(NOW - timedelta(days=6, hours=23), "a", False),  # oldest 6h cell
        Check(NOW - timedelta(days=29), "a", True),  # oldest day cell
        Check(NOW - timedelta(days=31), "a", True),  # out of every window
    ]
    rows = uptime.build_rows(checks, NOW)
    day, week, month = rows["Last Day"], rows["Last Week"], rows["Last Month"]
    assert (len(day), len(week), len(month)) == (24, 28, 30)

    assert day[-1].label == "2026-11-22 05:00-06:00 UTC"
    assert (day[-1].up, day[-1].total) == (1, 1)
    assert day[-2].color == "red"
    assert day[0].label == "2026-11-21 06:00-07:00 UTC"
    assert day[0].color == "green"
    assert sum(c.total for c in day) == 3

    assert week[-1].label == "2026-11-22 00:00-06:00 UTC"
    assert (week[-1].up, week[-1].total) == (1, 2)
    assert week[-1].color == "yellow"
    assert week[0].label == "2026-11-15 06:00-12:00 UTC"
    assert week[0].color == "red"

    assert month[-1].label == "2026-11-22 UTC"
    assert month[0].label == "2026-10-24 UTC"
    assert month[0].color == "green"
    assert sum(c.total for c in month) == 6


def test_render_writes_valid_svgs(tmp_path):
    log = tmp_path / "ping.log"
    log.write_text(
        "2026-11-22T05:17:00Z apps01ca OK\n2026-11-22T05:17:00Z apps02 FAIL boom\n"
    )
    written = uptime.render(log, tmp_path / "out", ["apps01ca", "apps02"], NOW)
    assert [p.name for p in written] == ["service.svg", "apps01ca.svg", "apps02.svg"]
    for path in written:
        root = ElementTree.fromstring(path.read_text())
        rects = root.findall("{http://www.w3.org/2000/svg}rect")
        assert len(rects) == 24 + 28 + 30
    assert uptime.COLORS["green"] in written[0].read_text()
    assert uptime.COLORS["red"] in written[2].read_text()
