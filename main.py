"""
HOD Meeting Absentee Notifier - Vercel version
----------------------------------------------
Every Mon-Sat at ~10:30 AM IST (Vercel Cron), this reads who actually joined
the daily 9:50 AM HOD meeting on Google Meet, then:
- sends a private Slack DM to every HOD who did not join at all, and
- sends an absentee summary to the admins (Mehak + Rajinder Singh).

Roster = human members of the all-hods Slack channel, minus EXCLUDED_USER_IDS.
Attendance is matched by comparing Google Meet display names with Slack names
(case-insensitive). Use MEET_NAME_ALIASES for people whose names differ.

Setup requires (see README):
- Google Meet REST API enabled in the Google Cloud project.
- One-time OAuth token from the account that joins the meeting daily
  (run get_meet_token.py) -> MEET_OAUTH_* env vars.
- MEET_MEETING_CODE env var = the fixed meeting code (abc-defg-hij).
- Set DRY_RUN=1 for the first test: nothing is sent, JSON shows the result.
"""

import os
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests as http_requests
from flask import Flask, request, jsonify
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as UserCredentials
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hod-meeting-absentees")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
HOD_CHANNEL_ID = os.environ.get("HOD_CHANNEL_ID", "C0BDA3WDTJS")
LOCAL_TIMEZONE = os.environ.get("LOCAL_TIMEZONE", "Asia/Kolkata")

# Never DM these people, and never list them as absent (Davinder Bisht,
# Gurmeet Singh, PC/Mehak).
EXCLUDED_USER_IDS = {
    u.strip()
    for u in os.environ.get(
        "EXCLUDED_USER_IDS", "U0BBRLUN0UB,U0BBUKGQR7X,U0BBGH2CWGP"
    ).split(",")
    if u.strip()
}

# Admins who receive the daily absentee summary: "slack_id:Name" pairs.
SUMMARY_RECIPIENTS = [
    tuple(p.split(":", 1))
    for p in os.environ.get(
        "SUMMARY_RECIPIENTS", "U0BBGH2CWGP:Mehak,U0AKDLX2RP1:Rajinder Singh"
    ).split(",")
    if ":" in p
]

# Fixed meeting code from https://meet.google.com/abc-defg-hij
MEET_MEETING_CODE = os.environ.get("MEET_MEETING_CODE", "").strip().replace(" ", "")

# One-time OAuth values from get_meet_token.py (account that joins daily).
MEET_OAUTH_CLIENT_ID = os.environ.get("MEET_OAUTH_CLIENT_ID", "").strip()
MEET_OAUTH_CLIENT_SECRET = os.environ.get("MEET_OAUTH_CLIENT_SECRET", "").strip()
MEET_OAUTH_REFRESH_TOKEN = os.environ.get("MEET_OAUTH_REFRESH_TOKEN", "").strip()

# "Meet Name=Slack Name" pairs for people whose Google name differs from Slack.
MEET_NAME_ALIASES = {}
for _pair in os.environ.get(
    "MEET_NAME_ALIASES",
    "Arun Sharma=Arun,Santosh K=HR Santosh,Priyanka Thakur=EA",
).split(","):
    if "=" in _pair:
        _meet, _slack = _pair.split("=", 1)
        MEET_NAME_ALIASES[_meet.strip().lower()] = _slack.strip().lower()

# Meet participants who are NOT HODs (boss, guests, non-members) -- they are
# simply ignored, and not flagged in the admin note.
MEET_IGNORED_NAMES = {
    n.strip().lower()
    for n in os.environ.get(
        "MEET_IGNORED_NAMES",
        "Gurmeet Singh,Gurmeet Singh Arora,Lakhshmi Sharma,Lakshmi Sharma,Sakshi",
    ).split(",")
    if n.strip()
}

MEETING_TIME_LABEL = os.environ.get("MEETING_TIME_LABEL", "9:50 AM")
BOT_SENDER_NAME = os.environ.get("BOT_SENDER_NAME", "Core Team | GCS Group").strip()
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")

ABSENT_TEMPLATE = (
    "Dear {name},\n\n"
    "Our records indicate that you did not join the *Daily HOD Meeting held "
    "at {time} today ({date})* on Google Meet.\n\n"
    "CMD Sir would like to understand if there are any challenges being "
    "faced in attending the daily meeting. The meeting plays an important "
    "role in ensuring effective coordination and smooth functioning across "
    "all departments.\n\n"
    "Kindly ensure regular and timely attendance of the Daily HOD Meeting.\n\n"
    "Regards,\n"
    "Core Team\n"
    "GCS Group"
)

SUMMARY_TEMPLATE = (
    "Dear {name},\n\n"
    "Following HODs did not join the Daily HOD Meeting ({time}) on {date}:\n\n"
    "{absent_lines}\n\n"
    "Thanks,\n"
    "Core Team\n"
    "GCS Group"
)

ALL_PRESENT_TEMPLATE = (
    "Dear {name},\n\n"
    "Good news -- all HODs joined the Daily HOD Meeting ({time}) on {date}.\n\n"
    "Thanks,\n"
    "Core Team\n"
    "GCS Group"
)

NO_RECORD_TEMPLATE = (
    "Dear {name},\n\n"
    "No Google Meet record was found for today's {time} HOD meeting ({date}). "
    "Either the meeting did not take place, or the meeting code / Google "
    "account is not set up correctly.\n\n"
    "Thanks,\n"
    "Core Team\n"
    "GCS Group"
)

client = WebClient(token=SLACK_BOT_TOKEN)

# IMPORTANT: Vercel looks for a Flask instance named exactly "app".
app = Flask(__name__)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------
def get_channel_members(channel_id):
    members = []
    cursor = None
    while True:
        resp = client.conversations_members(channel=channel_id, cursor=cursor, limit=200)
        members.extend(resp["members"])
        cursor = resp.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
    return members


def get_user_profile(user_id):
    try:
        u = client.users_info(user=user_id)["user"]
    except SlackApiError as e:
        logger.warning("users_info failed for %s: %s", user_id, e)
        return False, user_id, user_id
    if u.get("is_bot") or u.get("deleted") or u.get("id") == "USLACKBOT":
        return False, "", ""
    profile = u.get("profile", {})
    real_name = (u.get("real_name") or profile.get("real_name") or "").strip()
    display_name = (profile.get("display_name") or "").strip()
    return True, real_name or u.get("name", user_id), display_name


def send_text_to(recipient_id, text):
    """DM a user ID (U.../W...) or post to a DM channel ID (D...)."""
    channel_id = recipient_id
    if recipient_id.startswith(("U", "W")):
        channel_id = client.conversations_open(users=recipient_id)["channel"]["id"]
    kwargs = {"channel": channel_id, "text": text}
    if BOT_SENDER_NAME:
        try:
            client.chat_postMessage(username=BOT_SENDER_NAME, **kwargs)
            return
        except SlackApiError as e:
            if e.response.get("error") != "missing_scope":
                raise
            logger.warning("chat:write.customize missing -- sending with default name.")
    client.chat_postMessage(**kwargs)


# ---------------------------------------------------------------------------
# Google Meet helpers
# ---------------------------------------------------------------------------
_meet_creds = None


def _get_meet_access_token():
    global _meet_creds
    if not (MEET_OAUTH_CLIENT_ID and MEET_OAUTH_CLIENT_SECRET and MEET_OAUTH_REFRESH_TOKEN):
        raise RuntimeError("MEET_OAUTH_* env vars are not set (run get_meet_token.py)")
    if _meet_creds is None:
        _meet_creds = UserCredentials(
            token=None,
            refresh_token=MEET_OAUTH_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=MEET_OAUTH_CLIENT_ID,
            client_secret=MEET_OAUTH_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
        )
    if not _meet_creds.valid:
        _meet_creds.refresh(GoogleAuthRequest())
    return _meet_creds.token


def _meet_api(path, params=None):
    token = _get_meet_access_token()
    resp = http_requests.get(
        "https://meet.googleapis.com/v2/" + path,
        headers={"Authorization": "Bearer " + token},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_todays_meet_participants():
    """Returns (set of lowercase display names who joined today, record_found)."""
    tz = ZoneInfo(LOCAL_TIMEZONE)
    today = datetime.now(tz).date()
    day_start_utc = (
        datetime.combine(today, dtime.min, tzinfo=tz)
        .astimezone(ZoneInfo("UTC"))
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    space = _meet_api("spaces/" + MEET_MEETING_CODE)
    space_name = space["name"]

    records = _meet_api(
        "conferenceRecords",
        {"filter": 'space.name = "%s" AND start_time >= "%s"' % (space_name, day_start_utc)},
    ).get("conferenceRecords", [])
    if not records:
        return set(), False

    attendees = set()
    for rec in records:
        page_token = None
        while True:
            params = {"pageSize": 250}
            if page_token:
                params["pageToken"] = page_token
            data = _meet_api(rec["name"] + "/participants", params)
            for p in data.get("participants", []):
                who = (
                    p.get("signedinUser", {}).get("displayName")
                    or p.get("anonymousUser", {}).get("displayName")
                    or p.get("phoneUser", {}).get("displayName")
                    or ""
                ).strip().lower()
                if who:
                    attendees.add(MEET_NAME_ALIASES.get(who, who))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return attendees, True


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------
def run_meeting_check():
    tz = ZoneInfo(LOCAL_TIMEZONE)
    now = datetime.now(tz)
    if now.weekday() == 6:  # Sunday
        return {"status": "skipped", "reason": "Sunday is off"}
    if not MEET_MEETING_CODE:
        return {"status": "skipped", "reason": "MEET_MEETING_CODE not set"}

    date_str = now.strftime("%A, %d %B %Y")
    attendees, record_found = get_todays_meet_participants()
    logger.info("Meet attendees today: %s (record_found=%s)", sorted(attendees), record_found)

    summary_extra = ""
    absentees = []  # (uid, name)
    matched_meet_names = set()
    if record_found:
        for uid in get_channel_members(HOD_CHANNEL_ID):
            is_human, real_name, display_name = get_user_profile(uid)
            if not is_human or uid in EXCLUDED_USER_IDS:
                continue
            rn = (real_name or "").strip().lower()
            dn = (display_name or "").strip().lower()
            if rn in attendees or dn in attendees:
                matched_meet_names.add(rn if rn in attendees else dn)
                continue
            absentees.append((uid, real_name or display_name))
        unmatched = attendees - matched_meet_names - MEET_IGNORED_NAMES
        if unmatched:
            summary_extra = (
                "\n\nNote (admin): these Meet names could not be matched to "
                "Slack members: " + ", ".join(sorted(unmatched))
                + ". If any of them is an HOD, add a MEET_NAME_ALIASES entry."
            )

    sent_dms = []
    errors = []
    if record_found and not DRY_RUN:
        for uid, name in absentees:
            text = ABSENT_TEMPLATE.format(name=name, time=MEETING_TIME_LABEL, date=date_str)
            try:
                send_text_to(uid, text)
                sent_dms.append(name)
            except SlackApiError as e:
                logger.error("Absent DM to %s failed: %s", name, e)
                errors.append(name + ": " + str(e))

    if not record_found:
        template = NO_RECORD_TEMPLATE
        lines = ""
    elif absentees:
        template = SUMMARY_TEMPLATE
        lines = "\n".join("%d. %s" % (i + 1, name) for i, (_, name) in enumerate(absentees))
    else:
        template = ALL_PRESENT_TEMPLATE
        lines = ""

    sent_summaries = []
    for rid, rname in SUMMARY_RECIPIENTS:
        rid, rname = rid.strip(), rname.strip()
        text = template.format(
            name=rname, time=MEETING_TIME_LABEL, date=date_str, absent_lines=lines,
        ) + summary_extra
        if DRY_RUN:
            sent_summaries.append(rname + " (dry-run)")
            continue
        try:
            send_text_to(rid, text)
            sent_summaries.append(rname)
        except SlackApiError as e:
            logger.error("Summary to %s failed: %s", rname, e)
            errors.append(rname + ": " + str(e))

    result = {
        "status": "ok",
        "date": date_str,
        "meeting_record_found": record_found,
        "attendees_seen": sorted(attendees),
        "absentees": [name for _, name in absentees],
        "absent_dms_sent": sent_dms,
        "summary_sent_to": sent_summaries,
        "errors": errors,
        "dry_run": DRY_RUN,
    }
    logger.info("Meeting check result: %s", result)
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/meeting", methods=["GET", "POST"])
def meeting():
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + CRON_SECRET:
            return jsonify({"status": "unauthorized"}), 401
    try:
        return jsonify(run_meeting_check())
    except Exception as e:
        logger.exception("run_meeting_check failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    return "HOD Meeting Absentee Notifier is running.", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
