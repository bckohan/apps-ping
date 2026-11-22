"""
Hourly end-to-end health check of the APPS PPP service.

Each invocation is short and stateless; all state lives in APPS itself as
submissions whose names start with :data:`PREFIX` and encode their upload time.

1. Find the most recent ping submission and check it:

   * processed successfully -> fine
   * in the Error state, or carrying an error flag -> notify
   * still in progress and older than ``max_age`` -> notify (stuck)
   * still in progress and younger than ``max_age`` -> approve it if it is
     waiting on approval, then exit *without* uploading so we do not pile
     submissions onto a busy service

2. Delete old ping submissions so we stay under the account quota.

3. Upload a fresh submission, wait for it to be verified and approve it. Any
   failure here notifies immediately. Whether it processes is checked by the
   next invocation.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
import typing as t
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# httpsig (a gdgps_apps dependency) imports the deprecated pkg_resources
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

from gdgps_apps.apps import APPS
from gdgps_apps.defines import Data, DataFlag, GIPSYXData
from gdgps_apps.exceptions import DataNotFound

PREFIX = "apps-ping-"
NAME_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
NAME_RE = re.compile(rf"^{re.escape(PREFIX)}(\d{{8}}T\d{{6}}Z)")

NTFY_URL = "https://ntfy.sh"
DEFAULT_NTFY_TOPIC = "apps-ping-usn8"

SUCCESS_STATES = {Data.DONE, Data.AVAILABLE, Data.RETRIEVED}
PRE_VERIFY_STATES = {Data.NASCENT, Data.SUBMITTED}
IN_PROGRESS_STATES = {
    Data.NASCENT,
    Data.SUBMITTED,
    Data.VERIFIED,
    Data.WAITING,
    Data.APPROVED,
    Data.QUEUED,
    Data.PROCESSING,
}

# how many errored ping submissions to keep around for inspection
KEEP_ERRORED = 3


@dataclass
class Ping:
    id: str
    name: str
    uploaded: datetime
    state: str
    flags: list[dict[str, t.Any]]

    @classmethod
    def from_data(cls, data: dict[str, t.Any]) -> Ping | None:
        match = NAME_RE.match(data.get("name") or "")
        if not match:
            return None
        return cls(
            id=data["id"],
            name=data["name"],
            uploaded=datetime.strptime(match.group(1), NAME_TIME_FORMAT).replace(
                tzinfo=timezone.utc
            ),
            state=data.get("state", ""),
            flags=data.get("flags") or [],
        )

    @property
    def state_name(self) -> str:
        try:
            return Data.get_verbose_state(self.state)
        except KeyError:
            return repr(self.state)

    def error_flags(self) -> list[str]:
        return [
            f"{flag.get('header')}: {flag.get('detail')}"
            if flag.get("detail")
            else str(flag.get("header"))
            for flag in self.flags
            if flag.get("level") == DataFlag.ERROR
        ]

    def age(self, now: datetime) -> timedelta:
        return now - self.uploaded


class PingProblem(Exception):
    """A condition that warrants notifying the user."""


def log(msg: str) -> None:
    print(msg, flush=True)


def run_url() -> str | None:
    """Link to the GitHub Actions run, if we're in one."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def notify(title: str, message: str, topic: str | None = None) -> bool:
    """Send a push notification via ntfy.sh. Never raises."""
    topic = topic or os.environ.get("NTFY_TOPIC") or DEFAULT_NTFY_TOPIC
    headers = {"Title": title, "Tags": "satellite,warning", "Priority": "high"}
    if url := run_url():
        headers["Click"] = url
    try:
        requests.post(
            f"{NTFY_URL}/{topic}",
            data=message.encode(),
            headers=headers,
            timeout=10,
        ).raise_for_status()
        return True
    except Exception as err:  # noqa: BLE001 - notification must never raise
        log(f"ntfy notification failed: {err}")
        return False


def list_pings(apps: APPS) -> list[Ping]:
    """All ping submissions, newest first."""
    # the server ignores filter{} queries, so filter client side
    pings = [p for d in apps.list_data() if (p := Ping.from_data(d))]
    return sorted(pings, key=lambda p: p.uploaded, reverse=True)


def check_previous(apps: APPS, prev: Ping, now: datetime, max_age: timedelta) -> bool:
    """
    Check the state of the previous ping submission.

    :return: True if a new submission should be uploaded, False if the previous
        one is still legitimately in progress.
    :raises PingProblem: if the previous submission failed or is stuck
    """
    age_min = int(prev.age(now).total_seconds() // 60)
    log(
        f"Previous ping {prev.name} ({prev.id}) is {prev.state_name}, {age_min} min old"
    )

    if prev.state == Data.ERROR:
        errors = "\n".join(prev.error_flags()) or "(no error flags)"
        raise PingProblem(f"Submission {prev.name} failed to process:\n{errors}")

    if prev.state in SUCCESS_STATES:
        if flagged := prev.error_flags():
            raise PingProblem(
                f"Submission {prev.name} finished with errors:\n" + "\n".join(flagged)
            )
        log("Previous ping processed successfully.")
        return True

    if prev.state in IN_PROGRESS_STATES:
        if prev.age(now) > max_age:
            raise PingProblem(
                f"Submission {prev.name} has not processed after {age_min} minutes "
                f"(stuck in {prev.state_name})."
            )
        if prev.state == Data.VERIFIED:
            # the run that uploaded it timed out before verification finished
            approve(apps, prev.id, prev.name)
        log("Previous ping still in progress, not uploading another.")
        return False

    raise PingProblem(
        f"Submission {prev.name} is in unexpected state {prev.state_name}"
    )


def approve(apps: APPS, data_id: str, name: str) -> None:
    log(f"Approving {name} ({data_id}) for processing.")
    try:
        ok = apps.approve(data_id)
    except Exception as err:
        raise PingProblem(f"Approval of {name} failed: {err}") from err
    if not ok:
        raise PingProblem(f"Approval of {name} was rejected.")


def cleanup(apps: APPS, pings: list[Ping]) -> None:
    """
    Delete old ping submissions to stay under quota. The newest ping is always
    kept (the next run checks it), as are the most recent errored pings.
    Deletion failures are logged but are not fatal.
    """
    errored = 0
    for ping in pings[1:]:
        if ping.state in IN_PROGRESS_STATES:
            continue
        if ping.state == Data.ERROR:
            errored += 1
            if errored <= KEEP_ERRORED:
                continue
        try:
            log(f"Deleting old ping {ping.name} ({ping.state_name}).")
            apps.delete_data(ping.id)
        except Exception as err:  # noqa: BLE001 - cleanup is best effort
            log(f"Unable to delete {ping.name}: {err}")


def upload(
    apps: APPS,
    file: Path,
    now: datetime,
    verify_timeout: timedelta,
    poll_interval: float = 15,
) -> None:
    """
    Upload a new ping submission and approve it once verified.

    If verification does not finish within ``verify_timeout`` we exit quietly:
    the next run approves it or reports it as stuck.

    :raises PingProblem: if the upload, verification or approval fails
    """
    name = f"{PREFIX}{now.strftime(NAME_TIME_FORMAT)}"
    log(f"Uploading {file} as {name}.")
    try:
        resp = apps.upload_gipsyx(
            str(file),
            name=name,
            email_notify=False,
            processing_mode=GIPSYXData.STATIC,
        )
    except Exception as err:
        raise PingProblem(f"Upload of {name} failed: {err}") from err

    data_id = resp.get("id") if isinstance(resp, dict) else None
    if not data_id:
        raise PingProblem(f"Upload of {name} returned no id: {resp}")
    log(f"Uploaded {name} ({data_id}).")

    deadline = time.monotonic() + verify_timeout.total_seconds()
    state = resp.get("state", Data.SUBMITTED)
    detail: dict[str, t.Any] = resp
    while True:
        if state not in PRE_VERIFY_STATES:
            break
        if time.monotonic() >= deadline:
            log(
                f"{name} still {Data.get_verbose_state(state)} after "
                f"{verify_timeout}; leaving it for the next run."
            )
            return
        time.sleep(poll_interval)
        try:
            detail = apps.detail(data_id)
        except DataNotFound:
            # may not have replicated to the portal yet
            continue
        state = detail.get("state", state)

    ping = Ping.from_data({**detail, "id": data_id, "name": name})
    assert ping
    log(f"{name} is {ping.state_name}.")
    if state == Data.ERROR:
        errors = "\n".join(ping.error_flags()) or "(no error flags)"
        raise PingProblem(f"Submission {name} failed verification:\n{errors}")
    if state == Data.VERIFIED:
        approve(apps, data_id, name)
    elif state not in IN_PROGRESS_STATES | SUCCESS_STATES:
        raise PingProblem(f"Submission {name} is in unexpected state {ping.state_name}")


def run(
    apps: APPS,
    file: Path,
    max_age: timedelta,
    verify_timeout: timedelta,
    now: datetime | None = None,
    poll_interval: float = 15,
) -> None:
    """
    Run one ping cycle.

    :raises PingProblem: on any condition the user should be notified of
    """
    now = now or datetime.now(timezone.utc)
    pings = list_pings(apps)
    should_upload = True
    problem: PingProblem | None = None
    if pings:
        try:
            should_upload = check_previous(apps, pings[0], now, max_age)
        except PingProblem as err:
            # report it, but still upload a fresh submission so the next run
            # checks that one instead of re-reporting this one
            problem = err
    else:
        log("No previous ping submission found.")

    cleanup(apps, pings)

    if should_upload:
        try:
            upload(apps, file, now, verify_timeout, poll_interval=poll_interval)
        except PingProblem as err:
            if problem is None:
                raise
            raise PingProblem(f"{problem}\n\nAlso: {err}") from err

    if problem:
        raise problem


def set_output(key: str, value: str) -> None:
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as out:
            out.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the last APPS ping submission and upload a new one."
    )
    parser.add_argument("file", type=Path, help="RINEX file to submit.")
    parser.add_argument(
        "--settings",
        default=os.environ.get("APPS_SETTINGS_FILE", "~/.apps_settings"),
        help="Path to the APPS credential file (env: APPS_SETTINGS_FILE).",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=60,
        help="Minutes a submission may take to process before it is stuck.",
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=10,
        help="Minutes to wait for a new upload to be verified.",
    )
    args = parser.parse_args(argv)

    try:
        if not args.file.is_file():
            raise FileNotFoundError(args.file)
        apps = APPS(settings_file=os.path.expanduser(args.settings))
        run(
            apps,
            args.file,
            max_age=timedelta(minutes=args.max_age),
            verify_timeout=timedelta(minutes=args.verify_timeout),
        )
    except PingProblem as problem:
        log(f"PROBLEM: {problem}")
        set_output("notified", str(notify("APPS ping failed", str(problem))).lower())
        return 1
    except Exception as err:  # noqa: BLE001 - report anything unexpected
        trace = traceback.format_exc()
        log(trace)
        set_output(
            "notified",
            str(notify("APPS ping error", f"{type(err).__name__}: {err}")).lower(),
        )
        return 2
    log("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
