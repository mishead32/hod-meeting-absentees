"""
HOD Missing Daily Report Reminder - Vercel version
--------------------------------------------------
Every day at ~11:00 AM IST (Vercel Cron), this checks the #all-hods channel
for the PREVIOUS day. Any HOD who did not post any message that day gets a
polite reminder by private DM.

Rules (as specified):
- HODs = all human members of the channel, EXCEPT the IDs in EXCLUDED_USER_IDS.
- "Submitted" = posted any message in the channel during that day (IST).
- If the previous day was Sunday, the run is skipped (Sunday is off).
- Reminder is sent as a private DM to each defaulter.

Deployment notes:
- Vercel Cron calls GET /check (see vercel.json). On the Hobby plan the cron
  fires once per day, sometime within the hour after the scheduled time.
- If a CRON_SECRET env var is set, requests must carry
  "Authorization: Bearer <CRON_SECRET>" (Vercel adds this automatically for
  cron invocations when the env var exists).
- Set DRY_RUN=1 to log who WOULD be reminded without sending any DMs
  (recommended for the first test).
"""

import os
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hod-report-reminder")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
HOD_CHANNEL_ID = os.environ.get("HOD_CHANNEL_ID", "C0BDA3WDTJS")
LOCAL_TIMEZONE = os.environ.get("LOCAL_TIMEZONE", "Asia/Kolkata")

# Slack member IDs that must NEVER receive a reminder (Davinder Bisht,
# Gurmeet Singh, PC). ID matching is exact and immune to profile-name
# changes. Override with a comma-separated EXCLUDED_USER_IDS env var.
EXCLUDED_USER_IDS = {
    u.strip()
    for u in os.environ.get(
        "EXCLUDED_USER_IDS", "U0BBRLUN0UB,U0BBUKGQR7X,U0BBGH2CWGP"
    ).split(",")
    if u.strip()
}

# Display name shown as the sender of the reminder DMs (instead of the app's
# default "Delegation Transcriber"). Requires the chat:write.customize scope.
BOT_SENDER_NAME = os.environ.get("BOT_SENDER_NAME", "Core Team | GCS Group").strip()

# Weekly summary recipients: comma-separated "slack_id:Name" pairs. IDs may be
# user IDs (U...) or DM channel IDs (D...).
WEEKLY_RECIPIENTS = [
    tuple(p.split(":", 1))
    for p in os.environ.get(
        "WEEKLY_RECIPIENTS", "U0BBGH2CWGP:Mehak,U0AKDLX2RP1:Rajinder Singh"
    ).split(",")
    if ":" in p
]

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")

MESSAGE_TEMPLATE = (
    "Dear {name},\n\n"
    "This is with reference to the *Daily HOD Report for {report_date}*, "
    "which has not been received.\n\n"
    "Thank you for your continued efforts and commitment towards maintaining "
    "high academic and operational standards.\n\n"
    "CMD Sir would like to understand if there are any challenges being faced "
    "in submitting the Daily HOD Report as per the prescribed reporting "
    "process. The report plays an important role in ensuring effective "
    "coordination, timely support, and smooth functioning across all "
    "departments.\n\n"
    "Kindly ensure that the Daily HOD Report is submitted within the "
    "stipulated timeline on a daily basis. *As per the reporting system, "
    "non-submission attracts a score of -10;* however, our primary objective "
    "is to maintain consistency, accountability, and effective communication "
    "rather than focus on deductions.\n\n"
    "Your cooperation and discipline in following the reporting process are "
    "highly appreciated and contribute significantly to the overall success "
    "of the institution.\n\n"
    "Regards,\n"
    "Core Team\n"
    "GCS Group"
)

WEEKLY_TEMPLATE = (
    "Dear {name},\n\n"
    "Kindly apply the negative scoring for below employees as they have not "
    "sent daily hod report (From {from_date} To {to_date})\n\n"
    "{defaulter_lines}\n\n"
    "Thanks,\n"
    "Core Team\n"
    "GCS Group"
)

WEEKLY_ALL_CLEAR = (
    "Dear {name},\n\n"
    "Good news -- all HODs submitted their daily reports every day "
    "(From {from_date} To {to_date}). No negative scoring is required this week.\n\n"
    "Thanks,\n"
    "Core Team\n"
    "GCS Group"
)

client = WebClient(token=SLACK_BOT_TOKEN)

# IMPORTANT: Vercel looks for a Flask instance named exactly "app".
app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_channel_members(channel_id):
    """All member user IDs of the channel (paginated)."""
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
    """Returns (is_human, real_name, display_name) for a user id."""
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


def is_excluded(user_id):
    return user_id in EXCLUDED_USER_IDS


def get_posters_between(channel_id, oldest_ts, latest_ts):
    """Set of user IDs who posted any message in the channel in the window."""
    posters = set()
    cursor = None
    while True:
        resp = client.conversations_history(
            channel=channel_id,
            oldest=str(oldest_ts),
            latest=str(latest_ts),
            inclusive=True,
            limit=200,
            cursor=cursor,
        )
        for msg in resp.get("messages", []):
            # Skip join/leave notices etc., but keep normal messages and
            # file shares (subtype "file_share" counts as a report).
            subtype = msg.get("subtype", "")
            if subtype in ("channel_join", "channel_leave", "channel_topic",
                           "channel_purpose", "channel_name", "bot_message"):
                continue
            uid = msg.get("user")
            if uid:
                posters.add(uid)
        cursor = resp.get("response_metadata", {}).get("next_cursor") or None
        if not (resp.get("has_more") and cursor):
            break
    return posters


def send_dm(user_id, name, report_date):
    dm = client.conversations_open(users=user_id)
    channel_id = dm["channel"]["id"]
    kwargs = {
        "channel": channel_id,
        "text": MESSAGE_TEMPLATE.format(name=name, report_date=report_date),
    }
    if BOT_SENDER_NAME:
        # Custom sender name (needs chat:write.customize scope). If the scope
        # is missing, retry without it rather than failing the reminder.
        try:
            client.chat_postMessage(username=BOT_SENDER_NAME, **kwargs)
            return
        except SlackApiError as e:
            if e.response.get("error") != "missing_scope":
                raise
            logger.warning("chat:write.customize scope missing -- sending with default bot name.")
    client.chat_postMessage(**kwargs)


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------
def run_check():
    tz = ZoneInfo(LOCAL_TIMEZONE)
    now = datetime.now(tz)
    report_day = (now - timedelta(days=1)).date()

    if report_day.weekday() == 6:  # Sunday
        logger.info("Previous day %s was Sunday -- skipping (Sunday is off).", report_day)
        return {"status": "skipped", "reason": "previous day was Sunday", "day": str(report_day)}

    report_date_str = report_day.strftime("%A, %d %B %Y")  # e.g. Thursday, 16 July 2026
    day_start = datetime.combine(report_day, dtime.min, tzinfo=tz).timestamp()
    day_end = datetime.combine(report_day, dtime.max, tzinfo=tz).timestamp()

    logger.info("Checking reports for %s in channel %s", report_day, HOD_CHANNEL_ID)
    posters = get_posters_between(HOD_CHANNEL_ID, day_start, day_end)
    logger.info("Users who posted on %s: %s", report_day, sorted(posters))

    reminded = []
    skipped_excluded = []
    submitted = []
    errors = []

    for uid in get_channel_members(HOD_CHANNEL_ID):
        is_human, real_name, display_name = get_user_profile(uid)
        if not is_human:
            continue
        if is_excluded(uid):
            skipped_excluded.append(real_name or display_name)
            continue
        if uid in posters:
            submitted.append(real_name)
            continue
        name = real_name or display_name or "HOD"
        if DRY_RUN:
            logger.info("[DRY RUN] Would DM reminder to %s (%s)", name, uid)
            reminded.append(name + " (dry-run)")
            continue
        try:
            send_dm(uid, name, report_date_str)
            logger.info("Reminder DM sent to %s (%s)", name, uid)
            reminded.append(name)
        except SlackApiError as e:
            logger.error("Failed to DM %s (%s): %s", name, uid, e)
            errors.append(name + ": " + str(e))

    summary = {
        "status": "ok",
        "report_day": str(report_day),
        "submitted": submitted,
        "reminded": reminded,
        "excluded": skipped_excluded,
        "errors": errors,
        "dry_run": DRY_RUN,
    }
    logger.info("Summary: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Weekly summary (Mondays): missing-report counts for last Monday..Saturday
# ---------------------------------------------------------------------------
def _send_text_to(recipient_id, text):
    """Send a plain DM. Accepts a user ID (U.../W...) or DM channel ID (D...)."""
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
    client.chat_postMessage(**kwargs)


def run_weekly():
    tz = ZoneInfo(LOCAL_TIMEZONE)
    today = datetime.now(tz).date()
    # Last completed Mon..Sat block. Run on Monday -> previous Monday.
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_saturday = last_monday + timedelta(days=5)
    days = [last_monday + timedelta(days=i) for i in range(6)]  # Mon..Sat

    from_str = last_monday.strftime("%d-%m-%Y")
    to_str = last_saturday.strftime("%d-%m-%Y")
    logger.info("Weekly summary for %s to %s", from_str, to_str)

    # Poster sets per day.
    posters_by_day = {}
    for d in days:
        start = datetime.combine(d, dtime.min, tzinfo=tz).timestamp()
        end = datetime.combine(d, dtime.max, tzinfo=tz).timestamp()
        posters_by_day[d] = get_posters_between(HOD_CHANNEL_ID, start, end)

    # Count missing days per HOD.
    missing = []  # (name, count)
    for uid in get_channel_members(HOD_CHANNEL_ID):
        is_human, real_name, display_name = get_user_profile(uid)
        if not is_human or is_excluded(uid):
            continue
        count = sum(1 for d in days if uid not in posters_by_day[d])
        if count > 0:
            missing.append((real_name or display_name or uid, count))
    missing.sort(key=lambda x: (-x[1], x[0]))

    if missing:
        lines = "\n".join(
            "%d. %s - %s" % (
                i + 1, name,
                ("%d days reports were missed" % count) if count > 1
                else "1 day report was missed",
            )
            for i, (name, count) in enumerate(missing)
        )
    else:
        lines = ""

    sent = []
    errors = []
    for recipient_id, recipient_name in WEEKLY_RECIPIENTS:
        recipient_id = recipient_id.strip()
        recipient_name = recipient_name.strip()
        template = WEEKLY_TEMPLATE if missing else WEEKLY_ALL_CLEAR
        text = template.format(
            name=recipient_name, from_date=from_str, to_date=to_str,
            defaulter_lines=lines,
        )
        if DRY_RUN:
            logger.info("[DRY RUN] Would send weekly summary to %s (%s)", recipient_name, recipient_id)
            sent.append(recipient_name + " (dry-run)")
            continue
        try:
            _send_text_to(recipient_id, text)
            sent.append(recipient_name)
        except SlackApiError as e:
            logger.error("Weekly summary to %s failed: %s", recipient_name, e)
            errors.append(recipient_name + ": " + str(e))

    summary = {
        "status": "ok",
        "period": from_str + " to " + to_str,
        "defaulters": [{"name": n, "missing_days": c} for n, c in missing],
        "sent_to": sent,
        "errors": errors,
        "dry_run": DRY_RUN,
    }
    logger.info("Weekly summary result: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/check", methods=["GET", "POST"])
def check():
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + CRON_SECRET:
            return jsonify({"status": "unauthorized"}), 401
    try:
        return jsonify(run_check())
    except Exception as e:
        logger.exception("run_check failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/weekly", methods=["GET", "POST"])
def weekly():
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != "Bearer " + CRON_SECRET:
            return jsonify({"status": "unauthorized"}), 401
    try:
        return jsonify(run_weekly())
    except Exception as e:
        logger.exception("run_weekly failed")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/test-dm", methods=["GET"])
def test_dm():
    """Send the reminder DM to ONE person by name, for testing.
    Example: /test-dm?name=Rajinder Singh
    Works even when DRY_RUN=1. Only matches members of the HOD channel."""
    target = (request.args.get("name") or "").strip().lower()
    if not target:
        return jsonify({"status": "error", "error": "add ?name=Full Name to the URL"}), 400
    for uid in get_channel_members(HOD_CHANNEL_ID):
        is_human, real_name, display_name = get_user_profile(uid)
        if not is_human:
            continue
        if real_name.strip().lower() == target or display_name.strip().lower() == target:
            tz = ZoneInfo(LOCAL_TIMEZONE)
            report_date_str = (datetime.now(tz) - timedelta(days=1)).strftime("%A, %d %B %Y")
            try:
                send_dm(uid, real_name or display_name, report_date_str)
                return jsonify({"status": "ok", "sent_to": real_name, "user_id": uid})
            except SlackApiError as e:
                return jsonify({"status": "error", "error": str(e)}), 500
    return jsonify({"status": "error", "error": "no channel member named '" + target + "'"}), 404


@app.route("/", methods=["GET"])
def health():
    return "HOD Report Reminder is running.", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
