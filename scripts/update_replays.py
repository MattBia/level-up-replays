#!/usr/bin/env python3
"""
Level Up replay updater.

Runs Wednesdays via GitHub Actions. Finds this week's "Level Up with Matt"
recording in BIA's Grain workspace, grabs its share URL, and
upserts it into replays.json (keyed by date, so re-runs are harmless).

Env vars:
  GRAIN_API_TOKEN_V2  required - Matt's personal Grain token
  GRAIN_API_TOKEN_V2_MICHELLE
                      optional - Michelle's personal Grain token. On Grain's
                      Starter plan a personal token only sees recordings its
                      owner recorded or was shared, so when Michelle's
                      recorder captures the call (2026-10-28) Matt's token
                      can't find it. Every configured token is queried and
                      the results merged.
  SLACK_WEBHOOK_URL   optional - posts a note on success or final failure
  FINAL_ATTEMPT       optional - "true" on the last cron slot of the day;
                      controls whether a miss is treated as a failure
  MANUAL              optional - "true" when triggered by hand (Stream Deck /
                      "Run workflow"). Looks for the newest call in the last
                      MANUAL_LOOKBACK_DAYS instead of the most recent Wednesday,
                      and treats "nothing found" as a failure so the scheduled
                      runs know they still have work to do.

Scheduled runs target the most recent Wednesday (ET) rather than "today" because
GitHub fires cron jobs hours late on low-traffic repos.

Exit codes:
  0 = entry added, or already present, or not-found on a non-final attempt
  1 = hard error, not-found on the final attempt, or not-found on a manual run

The last line of output is always "RESULT: ..." so a caller can show it
without parsing the whole log.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Grain v2 public API. Recording search is a POST with a JSON filter body;
# this matches the contract BIA's client_onboarding.py already uses in prod.
GRAIN_RECORDINGS_URL = "https://api.grain.com/_/public-api/v2/recordings"
GRAIN_API_VERSION = "2025-10-31"
# (label, env var). The first is required; the rest are optional extra personal
# tokens whose owners might have recorded the call. Add a line here plus the
# matching secret in the workflow to cover another recorder.
GRAIN_TOKEN_ENVS = [
    ("matt", "GRAIN_API_TOKEN_V2"),
    ("michelle", "GRAIN_API_TOKEN_V2_MICHELLE"),
]
REPLAYS_PATH = Path(__file__).resolve().parent.parent / "replays.json"
# Real Grain title is "Level Up with Matt". The server-side search narrows the
# list; the local check requires the phrase "level up" (whitespace-normalised,
# case-insensitive) so a title that merely contains "up" can't slip in.
GRAIN_TITLE_SEARCH = "Level Up"
TITLE_PHRASE = "level up"
PAGE_URL = "bianutrition.com/lu-replays"  # shown in Slack notes only
CALL_NAME = "Level Up"
ET = ZoneInfo("America/New_York")
WEDNESDAY = 2
# Manual runs look back this many days (inclusive). Deliberately under a week
# so a press earlier in the week can never "find" last week's replay and
# report success before this week's call has happened.
MANUAL_LOOKBACK_DAYS = 6


def notify_slack(message: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        r = requests.post(url, json={"text": message}, timeout=10)
        print(f"Slack notified ({r.status_code}).")
    except requests.RequestException as e:
        print(f"Slack notify failed: {e}")  # notifications are best-effort


def load_replays() -> dict:
    with open(REPLAYS_PATH) as f:
        return json.load(f)


def save_replays(data: dict) -> None:
    data["replays"].sort(key=lambda r: r["date"], reverse=True)
    with open(REPLAYS_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _recording_date_et(recording: dict) -> str | None:
    """Return the recording's start date (YYYY-MM-DD) in ET, or None."""
    raw = recording.get("start_datetime")
    if not raw:
        return None
    # Grain v2 returns ISO-8601, e.g. "2026-10-06T16:00:34Z".
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def most_recent_wednesday(today: date) -> date:
    """The most recent Wednesday on or before `today` (today itself if Wednesday)."""
    return today - timedelta(days=(today.weekday() - WEDNESDAY) % 7)


def title_matches(title: str | None) -> bool:
    t = " ".join((title or "").lower().split())
    return TITLE_PHRASE in t


def grain_tokens() -> list[tuple[str, str]]:
    """Configured (label, token) pairs. The first env var is required."""
    tokens = []
    for label, env in GRAIN_TOKEN_ENVS:
        raw = os.environ.get(env, "").strip()
        if raw:
            tokens.append((label, raw))
        elif env == GRAIN_TOKEN_ENVS[0][1]:
            raise RuntimeError(f"{env} is not set")
        else:
            print(f"No {env} set; not checking {label}'s recordings.")
    return tokens


def list_recordings() -> list[dict]:
    """
    Union of the title-search results across every configured token, deduped
    by recording id. A token that Grain rejects (expired, revoked) is reported
    and skipped as long as at least one token worked; if none work, the last
    error is raised so the run fails loudly.
    """
    seen: dict[str, dict] = {}
    worked = 0
    last_error: Exception | None = None
    for label, token in grain_tokens():
        headers = {
            "Authorization": f"Bearer {token}",
            "Public-Api-Version": GRAIN_API_VERSION,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                GRAIN_RECORDINGS_URL,
                headers=headers,
                json={"filter": {"title_search": GRAIN_TITLE_SEARCH}},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Grain query with {label}'s token failed: {e}", file=sys.stderr)
            last_error = e
            continue
        worked += 1
        recs = resp.json().get("recordings", [])
        print(f"{label}'s token sees {len(recs)} '{GRAIN_TITLE_SEARCH}' recording(s).")
        for r in recs:
            key = str(r.get("id") or r.get("recording_url") or id(r))
            seen.setdefault(key, r)
    if not worked and last_error is not None:
        raise last_error
    return list(seen.values())


def fetch_call(start: date, end: date) -> dict | None:
    """Return the newest matching Grain recording dated start..end (ET), or None."""
    recordings = list_recordings()

    lo, hi = start.isoformat(), end.isoformat()
    matches = []
    for r in recordings:
        if not title_matches(r.get("title")):
            continue
        d = _recording_date_et(r)
        if d and lo <= d <= hi:
            matches.append(r)
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("start_datetime") or "", reverse=True)
    return matches[0]


def extract_share_url(recording: dict) -> str | None:
    """
    Pull the public share URL off the recording object. Grain's v2 recording
    objects expose it as `recording_url` (a .../share/recording/<id>/<token>
    link); older/related endpoints have used `url`. Check known keys and
    prefer an actual /share/ link so we never post an auth-gated workspace URL.
    """
    candidates = [
        recording.get(k)
        for k in ("recording_url", "url", "share_url", "public_url")
    ]
    candidates = [c for c in candidates if c and "grain.com" in c]
    if not candidates:
        return None
    for c in candidates:
        if "/share/" in c:
            return c
    return candidates[0]


def result(msg: str) -> None:
    """Final status line."""
    print(f"RESULT: {msg}")


def main() -> int:
    now_et = datetime.now(ET)
    today = now_et.date()
    final_attempt = os.environ.get("FINAL_ATTEMPT", "").lower() == "true"
    manual = os.environ.get("MANUAL", "").lower() == "true"

    data = load_replays()
    posted = {r["date"] for r in data["replays"]}

    if manual:
        start = today - timedelta(days=MANUAL_LOOKBACK_DAYS)
        label = f"the last {MANUAL_LOOKBACK_DAYS} days"
    else:
        start = most_recent_wednesday(today)
        label = start.isoformat()
        # Idempotency: bail if the target Wednesday is already posted (an earlier
        # cron slot or a button press got it).
        if label in posted:
            print(f"Entry for {label} already exists. Nothing to do.")
            result(f"Already posted ({label}). Nothing to do.")
            return 0

    try:
        recording = fetch_call(start, today)
    except requests.RequestException as e:
        print(f"Grain API error: {e}", file=sys.stderr)
        notify_slack(f":warning: {CALL_NAME} updater hit a Grain API error: {e}")
        result(f"Grain API error: {e}")
        return 1

    if recording is None:
        msg = f"No {CALL_NAME} recording found in Grain for {label} yet."
        print(msg)
        if manual:
            # Fail so the scheduled runs don't stand down on our account.
            notify_slack(
                f":hourglass: {CALL_NAME} button pressed, but Grain has no {CALL_NAME} "
                f"recording from {label} yet. Try again in a few minutes, or let "
                "the scheduled Wednesday checks pick it up."
            )
            result("No new recording in Grain yet. Try again in a few minutes.")
            return 1
        if final_attempt:
            notify_slack(
                f":x: {CALL_NAME} replay was NOT posted for {label} - "
                "no matching recording found in Grain by the last check. "
                "Check the recording title contains 'Level Up', that whoever recorded it "
                "has a token configured (GRAIN_API_TOKEN_V2 / _MICHELLE), or add the link manually."
            )
            result(f"NOT posted for {label} - no recording found by the final check.")
            return 1
        result(f"Nothing for {label} yet; a later slot will retry.")
        return 0  # a later cron slot will retry

    rec_date = _recording_date_et(recording)
    if rec_date in posted:
        # Manual path: the newest recording in the window is already on the page.
        print(f"Entry for {rec_date} already exists. Nothing to do.")
        notify_slack(
            f":information_source: {CALL_NAME} button: the {rec_date} replay is already on "
            f"{PAGE_URL} and Grain has nothing newer. Nothing to do."
        )
        result(f"Already posted ({rec_date}). Nothing new in Grain.")
        return 0

    share_url = extract_share_url(recording)
    if not share_url:
        rec_id = recording.get("id", "unknown")
        msg = (
            f"Found recording {rec_id} for {rec_date} but it has no public share URL. "
            "The recording likely needs sharing enabled in Grain."
        )
        print(msg, file=sys.stderr)
        notify_slack(f":x: {CALL_NAME} updater: {msg}")
        result(f"Found {rec_date} but it has no public share link - enable sharing in Grain.")
        return 1

    data["replays"].append({"date": rec_date, "url": share_url})
    save_replays(data)
    print(f"Added {rec_date} -> {share_url}")
    notify_slack(
        f":white_check_mark: {CALL_NAME} replay for {rec_date} posted to "
        f"{PAGE_URL}\n{share_url}"
    )
    result(f"Posted {rec_date} to {PAGE_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
