import logging
import re
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials

# ===== הגדרות =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8632792737:AAG3uiTT9CQRk9nq5cutsVGk5k3qceB_am4")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1Yq3U4miWIGG763O_jY2oen2GFlhYe_U_1S62JurER3Q")

DAYS_HE = {
    0: "שני", 1: "שלישי", 2: "רביעי",
    3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"
}

logging.basicConfig(level=logging.INFO)

def connect_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds_info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1

def normalize_hour(h):
    h = h.replace(":", "")
    if len(h) <= 2:
        return f"{int(h):02d}:00"
    return f"{h[:2]}:{h[2:4]}"

def parse_date(text):
    """מנסה לחלץ תאריך מהטקסט בפורמט DD.M או DD/M או DD.MM.YYYY"""
    match = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else datetime.now(ZoneInfo("Asia/Jerusalem")).year
        return f"{day:02d}/{month:02d}/{year}"
    return None

def parse_correction(text):
    """
    מזהה הודעת תיקון:
    - תיקון 30.3 5 עובדים
    - תיקון 30.3 07:00 15:00
    - תיקון 30.3 6 עד 14 5 עובדים
    מחזיר (תאריך, start/None, end/None, workers/None)
    """
    text = text.strip()
    if not re.search(r"תיקון|תקן|עדכון|עדכן", text):
        return None

    date_str = parse_date(text)
    if not date_str:
        return None

    start, end, workers = None, None, None

    # שעות
    he = re.search(r"(?:מ[־\-]?)?(\d{1,2}(?::\d{2})?)\s+עד\s+(\d{1,2}(?::\d{2})?)", text)
    if he:
        start = normalize_hour(he.group(1))
        end = normalize_hour(he.group(2))

    time_pat = re.search(r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})", text)
    if time_pat and not start:
        start = time_pat.group(1)
        end = time_pat.group(2)

    # עובדים
    w = re.search(r"(\d+)\s*עובדים?", text)
    if w:
        workers = int(w.group(1))

    return date_str, start, end, workers

def parse_message(text):
    text = text.strip()

    # פורמט עברי: מ-X עד Y Z עובדים (עם או בלי "מ")
    he_pattern = r"(?:מ[־\-]?)?(\d{1,2}(?::\d{2})?)\s+עד\s+(\d{1,2}(?::\d{2})?)\s+(\d+)"
    match = re.search(he_pattern, text)
    if match:
        return normalize_hour(match.group(1)), normalize_hour(match.group(2)), int(match.group(3))

    # פורמט רגיל עם נקודותיים: 06:00 14:00 4
    pattern = r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s+(\d+)"
    match = re.search(pattern, text)
    if match:
        return match.group(1), match.group(2), int(match.group(3))

    # פורמט מספרים בלבד: 6 14 4
    simple = r"^(\d{1,2})\s+(\d{1,2})\s+(\d+)$"
    match = re.search(simple, text)
    if match:
        return normalize_hour(match.group(1)), normalize_hour(match.group(2)), int(match.group(3))

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

    # בדוק אם זה תיקון
    correction = parse_correction(text)
    if correction:
        date_str, start, end, workers = correction
        sheet = connect_sheet()
        rows = sheet.get_all_values()

        # חפש שורה עם אותו תאריך (עמודה A)
        row_index = None
        for i, row in enumerate(rows):
            if row and row[0] == date_str:
                row_index = i + 1  # gspread מתחיל מ-1
                break

        if not row_index:
            await update.message.reply_text(f"❌ לא מצאתי שורה עם תאריך {date_str}")
            return

        # קרא את הערכים הקיימים
        existing = rows[row_index - 1]
        cur_start   = existing[2] if len(existing) > 2 else "00:00"
        cur_end     = existing[3] if len(existing) > 3 else "00:00"
        cur_workers = int(existing[4]) if len(existing) > 4 and existing[4] else 0

        if start:
            cur_start = start
        if end:
            cur_end = end
        if workers:
            cur_workers = workers

        hours = calc_hours(cur_start, cur_end)
        total = hours * cur_workers

        sheet.update(f"C{row_index}:G{row_index}", [[cur_start, cur_end, cur_workers, hours, total]])

        await update.message.reply_text(
            f"✏️ תוקן בהצלחה!\n"
            f"📅 תאריך: {date_str}\n"
            f"🕐 שעות: {cur_start} עד {cur_end}\n"
            f"👷 עובדים: {cur_workers}\n"
            f"⏱ שעות ביום: {hours:.1f}\n"
            f"📊 סה\"כ שעות: {total:.1f}"
        )
        return

    # הודעה רגילה
    parsed = parse_message(text)
    if not parsed:
        await update.message.reply_text(
            "❌ לא הבנתי. שלח בפורמט:\n06:00 14:00 4\n(שעת התחלה, שעת סיום, כמות עובדים)\n\nלתיקון: תיקון 30.3 5 עובדים"
        )
        return

    start, end, workers = parsed
    hours = calc_hours(start, end)
    total = hours * workers

    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    date_str = now.strftime("%d/%m/%Y")
    day_str = DAYS_HE[now.weekday()]

    sheet = connect_sheet()
    sheet.append_row([date_str, day_str, start, end, workers, hours, total, ""])

    await update.message.reply_text(
        f"✅ נרשם בהצלחה!\n"
        f"📅 תאריך: {date_str} ({day_str})\n"
        f"🕐 שעות: {start} עד {end}\n"
        f"👷 עובדים: {workers}\n"
        f"⏱ שעות ביום: {hours:.1f}\n"
        f"📊 סה\"כ שעות: {total:.1f}"
    )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ הבוט פועל! שלח הודעה בטלגרם.")
    app.run_polling()

if __name__ == "__main__":
    main()
