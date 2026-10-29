# Level Up Replay Automation

Auto-posts each Wednesday's "Level Up with Matt" replay link to a Squarespace page
(suggested: bianutrition.com/lu-replays). Sibling of `../ph-replays` (Power Hour),
`../track-thrive-replays` (Track and Thrive) and `../elevation-replays`.

## How it works

1. `replays.json` is the source of truth (seeded with the first call, 2026-10-07).
2. A GitHub Action runs Wednesdays at 21:00, 21:30, 22:00, and 23:00 UTC (covers 5 PM ET
   year-round regardless of daylight saving, plus built-in retries). The call itself
   runs Wednesdays 3:00-4:15 PM ET, so the first slot lands ~45 minutes after it ends.
3. The script POSTs to the Grain v2 API for a recording from the **most recent
   Wednesday (ET)** whose title contains the phrase "Level Up" (the real title is
   "Level Up with Matt"), pulls its `recording_url` share link, and appends
   `{date, url}` to the JSON. "Most recent Wednesday" rather than "today" because
   GitHub fires cron on low-traffic repos hours late.
4. The workflow commits the change; the Squarespace code block fetches the raw JSON
   and renders the list. Page updates within ~5 minutes of the commit (GitHub raw CDN cache).

The script is idempotent: if that Wednesday's entry already exists, later runs exit quietly.
Extra cron slots are free retries in case Grain hasn't finished processing by 5 PM.

## Stream Deck button (manual run)

`workflow_dispatch` runs (the Stream Deck button in `../streamdeck-actions`, or
"Run workflow" in the Actions tab) set `MANUAL=true`, which changes two things:

- It looks for the newest call in the **last 6 days** instead of the most recent
  Wednesday, so a press any time from Wednesday evening through Monday works.
- "Nothing found" is a **failure** (exit 1, Slack `:hourglass:` note) instead of a
  quiet no-op. That's deliberate: the `check-manual-run` job makes the scheduled
  slots stand down when a *successful* manual run happened in the last 12 hours,
  so a press that came before Grain finished processing must not count.

Two layers stop a double run after a button press:

1. `check-manual-run` skips every scheduled slot for 12h after a successful press.
2. Even past 12h, a scheduled run bails with "Already posted" when that Wednesday's
   date is already in `replays.json`.

Every run ends with a `RESULT: …` line.

## Setup (one time)

Repo lives at **https://github.com/MattBia/level-up-replays** (public, so the
Squarespace fetch works). Steps that need Matt:

1. Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `GRAIN_API_TOKEN_V2` — Matt's personal Grain token. **Required.**
   - `GRAIN_API_TOKEN_V2_MICHELLE` — Michelle's personal Grain token. Optional but
     needed whenever Michelle's recorder captures the call (see below).
   - `SLACK_WEBHOOK_URL` — same value as in `track-thrive-replays`. Optional.
2. Create the Squarespace page (suggested slug `/lu-replays`), add a Code Block, and
   paste the whole of `squarespace-embed.html`. If a different slug is used, update
   `PAGE_URL` in `scripts/update_replays.py` so Slack notes point at the right page.
3. Test: Actions tab → "Level Up Replay Updater" → Run workflow. Expected
   today: "already on the page and Grain has nothing newer".

### Whose recorder joined the call?

A personal token only sees recordings its owner recorded or was shared, so the
script queries every configured token and merges the results. On 2026-10-28 the
call was recorded by Michelle's recorder and Matt's token couldn't see it, which is
why `GRAIN_API_TOKEN_V2_MICHELLE` exists. To add it: Michelle creates a personal
token (Account settings → Integrations → Personal API), then
`gh secret set GRAIN_API_TOKEN_V2_MICHELLE` in this repo. To cover another
recorder, add a line to `GRAIN_TOKEN_ENVS` in the script and the matching
`secrets.` line in the workflow.

### Token visibility check

BIA is on Grain's Starter plan, so `GRAIN_API_TOKEN_V2` is somebody's *personal*
token and only sees recordings that user owns or was shared. The 2026-10-07 call is
an "internal" recording (clients joined with display names only). If the test run
above reports *"Grain has no Level Up recording from the last 6 days yet"* even
though the call is in Grain, the token can't see Matt's recordings. Fix: create a
personal API token under Matt's Grain account (Account settings → Integrations →
Personal API) and set it as this repo's `GRAIN_API_TOKEN_V2` instead.

## Notes / failure modes

- If the recording title doesn't contain "Level Up", nothing posts and (if Slack is
  configured) you get pinged after the final 7 PM attempt. Manual fallback: add a
  line to `replays.json` and commit.
- If the recording isn't shared publicly in Grain, the script fails with a clear
  message rather than posting a dead link.
- The JSON contains only share links that are already public on the website, so
  nothing sensitive is exposed by the public repo.
