import logging
import re
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from google.oauth2.service_account import Credentials

# ===== הגדרות =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8632792737:AAG3uiTT9CQRk9nq5cutsVGk5k3qceB_am4")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1Yq3U4miWIGG763O_jY2oen2GFlhYe_U_1S62JurER3Q")
CHAT_ID = 338759206

DEFAULT_WORKERS = 4

TEAM_DEFAULTS = {
    "מגרשי ספורט": {"start": "06:00", "end": "14:00"},
    "שפפים":        {"start": "07:00", "end": "13:00"},
}

TEAMS = {
    "שפפים":  "שפפים",
    "מגרשי":  "מגרשי ספורט",
    "מגרש":   "מגרשי ספורט",
    "ספורט":  "מגרשי ספורט",
}

DAYS_HE = {
    0: "שני", 1: "שלישי", 2: "רביעי",
    3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"
}

logging.basicConfig(level=logging.INFO)

def get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    return gspread.authorize(creds)

def connect_sheet(team_name="שפפים"):
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(team_name)
    except:
        return spreadsheet.sheet1

def detect_team(text):
    """מזהה איזה צוות מוזכר בהודעה. ברירת מחדל: שפפים"""
    for keyword, name in TEAMS.items():
        if keyword in text:
            return name
    return "שפפים"

def normalize_hour(h):
    h = h.replace(":", "")
    if len(h) <= 2:
        return f"{int(h):02d}:00"
    return f"{h[:2]}:{h[2:4]}"

def parse_date(text):
    match = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else datetime.now(ZoneInfo("Asia/Jerusalem")).year
        return f"{day:02d}/{month:02d}/{year}"
    return None

def parse_correction(text):
    text = text.strip()
    if not re.search(r"תיקון|תקן|עדכון|עדכן", text):
        return None

    # אין עובדים — מחק את השורה
    if re.search(r"אין עובדים|לא עבדו|לא עובדים|אין עבודה", text):
        date_str = parse_date(text)
        if not date_str:
            now = datetime.now(ZoneInfo("Asia/Jerusalem"))
            date_str = now.strftime("%d/%m/%Y")
        return date_str, "DELETE", None, None

    date_str = parse_date(text)
    if not date_str:
        return None

    start, end, workers = None, None, None

    he = re.search(r"(?:מ[־\-]?)?(\d{1,2}(?::\d{2})?)\s+עד\s+(\d{1,2}(?::\d{2})?)", text)
    if he:
        start = normalize_hour(he.group(1))
        end = normalize_hour(he.group(2))

    time_pat = re.search(r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})", text)
    if time_pat and not start:
        start = time_pat.group(1)
        end = time_pat.group(2)

    w = re.search(r"(\d+)\s*עובדים?", text)
    if w:
        workers = int(w.group(1))

    return date_str, start, end, workers

def parse_message(text, team=None):
    text = text.strip()

    # פורמט מלא עם שעות: "שפפים 07 13 4" או "06:00 עד 14:00 3"
    he_pattern = r"(?:מ[־\-]?)?(\d{1,2}(?::\d{2})?)\s+עד\s+(\d{1,2}(?::\d{2})?)\s+(\d+)"
    match = re.search(he_pattern, text)
    if match:
        return normalize_hour(match.group(1)), normalize_hour(match.group(2)), int(match.group(3))

    pattern = r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+(\d+)"
    match = re.search(pattern, text)
    if match:
        return match.group(1), match.group(2), int(match.group(3))

    simple = r"^[^\d]*(\d{1,2})\s+(\d{1,2})\s+(\d+)$"
    match = re.search(simple, text)
    if match:
        return normalize_hour(match.group(1)), normalize_hour(match.group(2)), int(match.group(3))

    # פורמט קצר: "שפפים 5" או "מגרשי 3" — שעות לפי ברירת מחדל של הצוות
    short = re.search(r"^[^\d]*(\d+)\s*$", text)
    if short and team and team in TEAM_DEFAULTS:
        workers = int(short.group(1))
        start = TEAM_DEFAULTS[team]["start"]
        end = TEAM_DEFAULTS[team]["end"]
        return start, end, workers

    return None

def calc_hours(start, end):
    fmt = "%H:%M"
    t1 = datetime.strptime(start, fmt)
    t2 = datetime.strptime(end, fmt)
    if t2 < t1:
        t2 += timedelta(days=1)
    return (t2 - t1).seconds / 3600

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    team = detect_team(text)

    # בדוק אם זה תיקון
    correction = parse_correction(text)
    if correction:
        date_str, start, end, workers = correction
        sheet = connect_sheet(team)
        rows = sheet.get_all_values()

        row_index = None
        for i, row in enumerate(rows):
            if row and row[0] == date_str:
                row_index = i + 1
                break

        if not row_index:
            await update.message.reply_text(f"❌ לא מצאתי שורה עם תאריך {date_str} בגיליון {team}")
            return

        if start == "DELETE":
            sheet.delete_rows(row_index)
            await update.message.reply_text(f"🗑️ השורה של {date_str} נמחקה מגיליון {team}")
            return

        existing = rows[row_index - 1]
        cur_start   = existing[2] if len(existing) > 2 else "00:00"
        cur_end     = existing[3] if len(existing) > 3 else "00:00"
        cur_workers = int(existing[4]) if len(existing) > 4 and existing[4] else 0

        if start: cur_start = start
        if end: cur_end = end
        if workers: cur_workers = workers

        hours = calc_hours(cur_start, cur_end)
        total = hours * cur_workers

        sheet.update(f"C{row_index}:G{row_index}", [[cur_start, cur_end, cur_workers, hours, total]])

        await update.message.reply_text(
            f"✏️ תוקן בהצלחה! ({team})\n"
            f"📅 {date_str}\n"
            f"🕐 {cur_start} עד {cur_end}\n"
            f"👷 {cur_workers} עובדים\n"
            f"📊 סה\"כ: {total:.1f} שעות"
        )
        return

    # הערה
    if re.match(r"^הערה", text):
        note = re.sub(r"^הערה\s*", "", text).strip()
        specific_date = parse_date(text)
        now = datetime.now(ZoneInfo("Asia/Jerusalem"))
        date_str = specific_date if specific_date else now.strftime("%d/%m/%Y")

        sheet = connect_sheet(team)
        rows = sheet.get_all_values()
        row_index = None
        for i, row in enumerate(rows):
            if row and row[0] == date_str:
                row_index = i + 1
                break

        if not row_index:
            await update.message.reply_text(f"❌ לא מצאתי שורה עם תאריך {date_str} בגיליון {team}")
            return

        sheet.update(f"H{row_index}", [[note]])
        await update.message.reply_text(
            f"📝 הערה נשמרה! ({team})\n"
            f"📅 {date_str}\n"
            f"💬 {note}"
        )
        return

    # הודעה רגילה
    parsed = parse_message(text, team)
    if not parsed:
        await update.message.reply_text(
            "❌ לא הבנתי. דוגמאות:\n"
            "שפפים 5\n"
            "מגרשי 3\n"
            "שפפים 5 30.3\n"
            "תיקון שפפים 30.3 5 עובדים"
        )
        return

    start, end, workers = parsed
    hours = calc_hours(start, end)
    total = hours * workers

    # תאריך — אם צוין בהודעה, אחרת היום
    specific_date = parse_date(text)
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    if specific_date:
        date_str = specific_date
        day_num = datetime.strptime(specific_date, "%d/%m/%Y").weekday()
        day_str = DAYS_HE[day_num]
    else:
        date_str = now.strftime("%d/%m/%Y")
        day_str = DAYS_HE[now.weekday()]

    sheet = connect_sheet(team)
    rows = sheet.get_all_values()
    row_index = None
    for i, row in enumerate(rows):
        if row and row[0] == date_str:
            row_index = i + 1
            break

    if row_index:
        sheet.update(f"C{row_index}:G{row_index}", [[start, end, workers, hours, total]])
    else:
        sheet.append_row([date_str, day_str, start, end, workers, hours, total, ""])

    await update.message.reply_text(
        f"✅ נרשם! ({team})\n"
        f"📅 {date_str} ({day_str})\n"
        f"🕐 {start} עד {end}\n"
        f"👷 {workers} עובדים\n"
        f"📊 סה\"כ: {total:.1f} שעות"
    )

async def daily_auto_entry(bot: Bot):
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    if now.weekday() in [4, 5]:  # שישי, שבת
        return

    date_str = now.strftime("%d/%m/%Y")
    day_str = DAYS_HE[now.weekday()]

    lines = []
    for team, defaults in TEAM_DEFAULTS.items():
        start = defaults["start"]
        end = defaults["end"]
        hours = calc_hours(start, end)
        total = hours * DEFAULT_WORKERS
        sheet = connect_sheet(team)
        sheet.append_row([date_str, day_str, start, end, DEFAULT_WORKERS, hours, total, ""])
        lines.append(f"• {team}: {start}–{end} | {DEFAULT_WORKERS} עובדים | {total:.0f} שעות")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            f"🤖 נרשם אוטומטי לשני הצוותים!\n"
            f"📅 {date_str} ({day_str})\n\n"
            + "\n".join(lines) +
            f"\n\nאם יש שינוי — שלח תיקון 📝\n"
            f"(שפפים / מגרשי)"
        )
    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Jerusalem"))
    scheduler.add_job(daily_auto_entry, "cron", hour=9, minute=30, args=[app.bot])
    scheduler.start()

    print("✅ הבוט פועל!")
    app.run_polling()

if __name__ == "__main__":
    main()
