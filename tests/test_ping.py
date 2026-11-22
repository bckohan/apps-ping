import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gdgps_apps.defines import Data, DataFlag
from gdgps_apps.exceptions import DataNotFound

from apps_ping import ping
from apps_ping.ping import PingProblem

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(minutes=60)
VERIFY_TIMEOUT = timedelta(seconds=0.05)
FILE = Path(__file__)
PROC = "apps01ca"


def name_at(minutes_ago, processor=PROC, now=NOW):
    stamp = (now - timedelta(minutes=minutes_ago)).strftime(ping.NAME_TIME_FORMAT)
    return f"{ping.PREFIX}{processor}-{stamp}"


def sub(id, minutes_ago, state, flags=(), processor=PROC):
    return {
        "id": id,
        "name": name_at(minutes_ago, processor),
        "state": state,
        "flags": list(flags),
    }


ERROR_FLAG = {"level": DataFlag.ERROR, "header": "Data Editing Failed.", "detail": "x"}
OK_FLAG = {"level": DataFlag.SUCCESS, "header": "PPP Successful", "detail": None}


class FakeAPPS:
    def __init__(self, data=(), upload_state=Data.SUBMITTED, states=()):
        self.data = list(data)
        self.upload_state = upload_state
        self.states = list(states)  # successive states returned by detail()
        self.uploads = []
        self.approved = []
        self.deleted = []
        self.upload_error = None
        self.upload_errors = {}  # processor -> exception
        self.approve_ok = True

    def list_data(self, query=None):
        return list(self.data)

    def upload_gipsyx(self, file, **kwargs):
        if err := self.upload_error or self.upload_errors.get(kwargs["processor"]):
            raise err
        self.uploads.append(kwargs)
        return {"id": "new", "name": kwargs["name"], "state": self.upload_state}

    def detail(self, uuid, query=None):
        state = self.states.pop(0) if self.states else Data.SUBMITTED
        if state is DataNotFound:
            raise DataNotFound("nope", uuid=uuid)
        flags = [ERROR_FLAG] if state == Data.ERROR else []
        return {"id": uuid, "state": state, "flags": flags}

    def approve(self, uuid):
        self.approved.append(uuid)
        return self.approve_ok

    def delete_data(self, uuid):
        self.deleted.append(uuid)
        return True


def run(apps, processor=PROC):
    pings = ping.pings_for(ping.list_pings(apps), processor)
    ping.run(
        apps,
        FILE,
        MAX_AGE,
        VERIFY_TIMEOUT,
        processor=processor,
        pings=pings,
        now=NOW,
        poll_interval=0,
    )


def test_ignores_foreign_submissions_and_uploads_when_none():
    apps = FakeAPPS(
        [{"id": "x", "name": "sfer.crx", "state": Data.ERROR}],
        states=[Data.VERIFIED],
    )
    run(apps)
    assert apps.uploads[0]["name"] == "apps-ping-apps01ca-20261121T120000Z"
    assert apps.uploads[0]["processor"] == PROC
    assert apps.uploads[0]["email_notify"] is False
    assert apps.approved == ["new"]
    assert apps.deleted == []


def test_previous_success_uploads_and_approves():
    apps = FakeAPPS(
        [sub("a", 60, Data.AVAILABLE, [OK_FLAG])],
        states=[DataNotFound, Data.SUBMITTED, Data.VERIFIED],
    )
    run(apps)
    assert len(apps.uploads) == 1
    assert apps.approved == ["new"]


def test_previous_error_notifies_but_still_uploads():
    apps = FakeAPPS([sub("a", 60, Data.ERROR, [ERROR_FLAG])], states=[Data.VERIFIED])
    with pytest.raises(PingProblem, match="Data Editing Failed"):
        run(apps)
    assert len(apps.uploads) == 1
    assert apps.approved == ["new"]


def test_success_with_error_flag_is_a_problem():
    apps = FakeAPPS(
        [sub("a", 60, Data.AVAILABLE, [ERROR_FLAG])], states=[Data.VERIFIED]
    )
    with pytest.raises(PingProblem, match="finished with errors"):
        run(apps)


def test_young_in_progress_skips_upload():
    apps = FakeAPPS([sub("a", 30, Data.PROCESSING)])
    run(apps)
    assert apps.uploads == []
    assert apps.approved == []


def test_young_verified_is_approved_without_new_upload():
    apps = FakeAPPS([sub("a", 30, Data.VERIFIED)])
    run(apps)
    assert apps.uploads == []
    assert apps.approved == ["a"]


def test_stuck_notifies_and_uploads_fresh():
    apps = FakeAPPS([sub("a", 61, Data.QUEUED)], states=[Data.VERIFIED])
    with pytest.raises(PingProblem, match="not processed after 61 minutes"):
        run(apps)
    assert len(apps.uploads) == 1


def test_upload_failure_notifies():
    apps = FakeAPPS([sub("a", 60, Data.AVAILABLE)])
    apps.upload_error = RuntimeError("boom")
    with pytest.raises(PingProblem, match="Upload of .* failed: boom"):
        run(apps)


def test_combined_problems_are_both_reported():
    apps = FakeAPPS([sub("a", 60, Data.ERROR, [ERROR_FLAG])])
    apps.upload_error = RuntimeError("boom")
    with pytest.raises(PingProblem, match="(?s)failed to process.*Also: Upload"):
        run(apps)


def test_verification_error_notifies():
    apps = FakeAPPS(states=[Data.ERROR])
    with pytest.raises(PingProblem, match="failed verification"):
        run(apps)


def test_approval_rejected_notifies():
    apps = FakeAPPS(states=[Data.VERIFIED])
    apps.approve_ok = False
    with pytest.raises(PingProblem, match="rejected"):
        run(apps)


def test_slow_verification_is_left_for_next_run():
    apps = FakeAPPS(states=[Data.SUBMITTED] * 1000)
    run(apps)
    assert len(apps.uploads) == 1
    assert apps.approved == []


def test_auto_approved_upload_is_fine():
    apps = FakeAPPS(upload_state=Data.APPROVED)
    run(apps)
    assert apps.approved == []


def test_ping_name_parsing():
    legacy = ping.Ping.from_data(
        {"id": "a", "name": "apps-ping-20261121T120000Z", "location": "apps01ca"}
    )
    assert legacy and legacy.processor == "apps01ca"
    assert legacy.uploaded == NOW
    new = ping.Ping.from_data(
        {"id": "b", "name": "apps-ping-apps02-x-20261121T120000Z (1)", "location": "z"}
    )
    assert new and new.processor == "apps02-x"
    assert ping.Ping.from_data({"id": "c", "name": "apps-ping-foo"}) is None


def test_other_processors_pings_are_ignored():
    apps = FakeAPPS(
        [sub("other", 10, Data.PROCESSING, processor="apps02")],
        states=[Data.VERIFIED],
    )
    run(apps)
    assert len(apps.uploads) == 1
    assert apps.deleted == []


def test_cleanup_keeps_newest_and_recent_errors():
    data = [sub("newest", 60, Data.AVAILABLE)]
    data += [sub(f"ok{i}", 120 + i, Data.AVAILABLE) for i in range(3)]
    data += [sub(f"err{i}", 200 + i, Data.ERROR) for i in range(5)]
    data += [sub("busy", 300, Data.PROCESSING)]
    apps = FakeAPPS(data, states=[Data.VERIFIED])
    run(apps)
    assert sorted(apps.deleted) == ["err3", "err4", "ok0", "ok1", "ok2"]


def main(monkeypatch, tmp_path, apps, *args, notified=None):
    monkeypatch.setattr(ping, "APPS", lambda settings_file: apps)
    monkeypatch.setattr(ping.time, "sleep", lambda s: None)
    sent = notified if notified is not None else []
    monkeypatch.setattr(
        ping, "notify", lambda title, msg: sent.append((title, msg)) or True
    )
    log = tmp_path / "ping.log"
    code = ping.main([str(FILE), "--log", str(log), *args])
    return code, log.read_text() if log.exists() else ""


STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def test_main_ok_logs_each_processor(monkeypatch, tmp_path):
    sent = []
    code, log = main(
        monkeypatch,
        tmp_path,
        FakeAPPS(upload_state=Data.APPROVED),
        "--processors",
        "apps01ca, apps02",
        notified=sent,
    )
    assert code == 0
    assert sent == []
    lines = log.splitlines()
    assert re.fullmatch(rf"{STAMP} apps01ca OK", lines[0])
    assert re.fullmatch(rf"{STAMP} apps02 OK", lines[1])
    assert lines[0][:20] == lines[1][:20]


def test_main_processors_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPS_PING_PROCESSORS", "p1,p2")
    apps = FakeAPPS(upload_state=Data.APPROVED)
    code, log = main(monkeypatch, tmp_path, apps)
    assert code == 0
    assert [u["processor"] for u in apps.uploads] == ["p1", "p2"]


def test_main_one_processor_down(monkeypatch, tmp_path):
    sent = []
    apps = FakeAPPS(upload_state=Data.APPROVED)
    apps.upload_errors["apps02"] = RuntimeError("unreachable")
    code, log = main(
        monkeypatch, tmp_path, apps, "--processors", "apps01ca,apps02", notified=sent
    )
    assert code == 1
    assert " apps01ca OK" in log
    assert re.search(r" apps02 FAIL Upload of apps-ping-apps02-.* unreachable", log)
    assert sent[0][0] == "APPS ping failed: apps02"
    assert sent[0][1].startswith("[apps02] Upload of")


def test_main_all_processors_down(monkeypatch, tmp_path):
    sent = []
    apps = FakeAPPS()
    apps.upload_error = RuntimeError("unreachable")
    code, _ = main(
        monkeypatch, tmp_path, apps, "--processors", "apps01ca,apps02", notified=sent
    )
    assert code == 1
    assert len(sent) == 1
    assert sent[0][0] == "APPS service DOWN"
    assert "[apps01ca]" in sent[0][1] and "[apps02]" in sent[0][1]


def test_main_sets_notified_output(monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    code, log = main(monkeypatch, tmp_path, FakeAPPS(states=[Data.ERROR]))
    assert code == 1
    assert re.search(r" apps01ca FAIL Submission .* failed verification:\n$", log)
    assert out.read_text() == "notified=true\n"


def test_main_waiting_note(monkeypatch, tmp_path):
    data = [
        {
            "id": "a",
            "name": name_at(0, now=datetime.now(timezone.utc)),
            "state": Data.PROCESSING,
        }
    ]
    code, log = main(monkeypatch, tmp_path, FakeAPPS(data))
    assert code == 0
    assert log.endswith(" apps01ca OK previous ping still processing\n")


def test_main_unexpected_error_marks_all_processors(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(ping, "notify", lambda t, m: sent.append((t, m)) or True)

    def bad(settings_file):
        raise ValueError("bad\ncredential")

    monkeypatch.setattr(ping, "APPS", bad)
    log = tmp_path / "ping.log"
    assert ping.main([str(FILE), "--log", str(log), "--processors", "a,b"]) == 1
    lines = log.read_text().splitlines()
    assert lines[0].endswith(" a ERROR ValueError: bad")
    assert lines[1].endswith(" b ERROR ValueError: bad")
    assert sent[0][0] == "APPS service DOWN"
