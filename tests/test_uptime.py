from datetime import date, datetime, timedelta, timezone
from xml.etree import ElementTree

from apps_ping import uptime
from apps_ping.uptime import Cell, Check

TODAY = date(2026, 11, 22)


def at(day, hour=0):
    return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(
        hours=hour
    )


def test_cell_colors():
    assert Cell("x").color == "none"
    assert Cell("x", up=24, total=24).color == "green"
    assert Cell("x", up=23, total=24).color == "green"  # 95.8%
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


def test_build_rows_buckets():
    checks = [Check(at(TODAY, h), "a", h != 0) for h in range(24)]
    checks.append(Check(at(TODAY - timedelta(days=1)), "a", False))
    checks.append(Check(at(date(2026, 1, 1)), "a", True))  # out of every window
    rows = uptime.build_rows(checks, TODAY)
    daily, weekly, monthly = rows["Daily"], rows["Weekly"], rows["Monthly"]
    assert len(daily) == uptime.DAYS
    assert len(weekly) == uptime.WEEKS
    assert len(monthly) == uptime.MONTHS
    assert (daily[-1].up, daily[-1].total) == (23, 24)
    assert daily[-1].color == "green"
    assert daily[-2].color == "red"
    assert daily[0].color == "none"
    # 2026-11-22 is a Sunday, so both days fall in the week of the 16th
    assert weekly[-1].label == "week of 2026-11-16"
    assert (weekly[-1].up, weekly[-1].total) == (23, 25)
    assert weekly[-1].color == "yellow"
    assert monthly[-1].label == "2026-11"
    assert monthly[0].label == "2025-12"
    assert monthly[-1].total == 25


def test_render_writes_valid_svgs(tmp_path):
    log = tmp_path / "ping.log"
    log.write_text(
        "2026-11-22T05:17:00Z apps01ca OK\n2026-11-22T05:17:00Z apps02 FAIL boom\n"
    )
    written = uptime.render(log, tmp_path / "out", ["apps01ca", "apps02"], TODAY)
    assert [p.name for p in written] == ["service.svg", "apps01ca.svg", "apps02.svg"]
    for path in written:
        root = ElementTree.fromstring(path.read_text())
        rects = root.findall("{http://www.w3.org/2000/svg}rect")
        assert len(rects) == uptime.DAYS + uptime.WEEKS + uptime.MONTHS
    assert uptime.COLORS["green"] in written[0].read_text()
    assert uptime.COLORS["red"] in written[2].read_text()
