import logging
import re
import os
import json
from datetime import datetime, timedelta
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
    """ממיר מספר שעה לפורמט HH:MM"""
    h = h.replace(":", "")
    if len(h) <= 2:
        return f"{int(h):02d}:00"
    return f"{h[:2]}:{h[2:4]}"

def parse_message(text):
    """
    מבין פורמטים שונים:
    - 06:00 14:00 4
    - 6 14 4
    - עבדו מ6 עד 14 4 עובדים
    - מ7 עד 15 3 עובדים
    """
    text = text.strip()

    # פורמט עברי: מ-X עד Y Z עובדים
    he_pattern = r"מ[־\-]?(\d{1,2}(?::\d{2})?)\s+עד\s+(\d{1,2}(?::\d{2})?)\s+(\d+)"
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
    diff = (t2 - t1).seconds / 3600
    return diff

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parsed = parse_message(text)

    if not parsed:
        await update.message.reply_text(
            "❌ לא הבנתי. שלח בפורמט:\n06:00 14:00 4\n(שעת התחלה, שעת סיום, כמות עובדים)"
        )
        return

    start, end, workers = parsed
    hours = calc_hours(start, end)
    total = hours * workers

    now = datetime.now()
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
