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


def name_at(minutes_ago):
    return ping.PREFIX + (NOW - timedelta(minutes=minutes_ago)).strftime(
        ping.NAME_TIME_FORMAT
    )


def sub(id, minutes_ago, state, flags=()):
    return {
        "id": id,
        "name": name_at(minutes_ago),
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
        self.approve_ok = True

    def list_data(self, query=None):
        return list(self.data)

    def upload_gipsyx(self, file, **kwargs):
        if self.upload_error:
            raise self.upload_error
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


def run(apps):
    ping.run(apps, FILE, MAX_AGE, VERIFY_TIMEOUT, now=NOW, poll_interval=0)


def test_ignores_foreign_submissions_and_uploads_when_none():
    apps = FakeAPPS(
        [{"id": "x", "name": "sfer.crx", "state": Data.ERROR}],
        states=[Data.VERIFIED],
    )
    run(apps)
    assert apps.uploads[0]["name"] == ping.PREFIX + "20261121T120000Z"
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


def test_cleanup_keeps_newest_and_recent_errors():
    data = [sub("newest", 60, Data.AVAILABLE)]
    data += [sub(f"ok{i}", 120 + i, Data.AVAILABLE) for i in range(3)]
    data += [sub(f"err{i}", 200 + i, Data.ERROR) for i in range(5)]
    data += [sub("busy", 300, Data.PROCESSING)]
    apps = FakeAPPS(data, states=[Data.VERIFIED])
    run(apps)
    assert sorted(apps.deleted) == ["err3", "err4", "ok0", "ok1", "ok2"]


def test_main_notifies_on_problem(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(ping, "notify", lambda title, msg: sent.append(msg) or True)
    monkeypatch.setattr(
        ping, "APPS", lambda settings_file: FakeAPPS(states=[Data.ERROR])
    )
    monkeypatch.setattr(ping.time, "sleep", lambda s: None)
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert ping.main([str(FILE), "--verify-timeout", "1"]) == 1
    assert "failed verification" in sent[0]
    assert out.read_text() == "notified=true\n"


def test_main_notifies_on_unexpected_error(monkeypatch):
    sent = []
    monkeypatch.setattr(ping, "notify", lambda title, msg: sent.append(msg) or True)

    def bad(settings_file):
        raise ValueError("bad credential")

    monkeypatch.setattr(ping, "APPS", bad)
    assert ping.main([str(FILE)]) == 2
    assert "bad credential" in sent[0]
