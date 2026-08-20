import logging
import re
import os
import json
import traceback
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

DEFAULT_WORKERS = 6

TEAM_DEFAULTS = {
    "שפפים דינמיקה": {"start": "06:00", "end": "14:00", "workers": 6},
}

TEAMS = {
    "דינמיקה":  "שפפים דינמיקה",
    "שפפים":    "שפפים דינמיקה",
    "מגרשי":    "שפפים דינמיקה",
    "מגרש":     "שפפים דינמיקה",
    "ספורט":    "שפפים דינמיקה",
}

DAYS_HE = {
    0: "שני", 1: "שלישי", 2: "רביעי",
    3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"
}

MONTHS_HE = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "מארס": 3, "אפריל": 4,
    "מאי": 5, "יוני": 6, "יולי": 7, "אוגוסט": 8,
    "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}

MONTHS_HE_NAMES = {v: k for k, v in MONTHS_HE.items() if k != "מארס"}

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

def connect_sheet(team_name="שפפים דינמיקה"):
    client = get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(team_name)
    except:
        return spreadsheet.sheet1

def detect_team(text):
    """מזהה איזה צוות מוזכר בהודעה. יש רק צוות אחד: שפפים דינמיקה."""
    for keyword, name in TEAMS.items():
        if keyword in text:
            return name
    return "שפפים דינמיקה"

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

    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    today_str = now.strftime("%d/%m/%Y")

    # אין עובדים — מחק את השורה
    if re.search(r"אין עובדים|לא עבדו|לא עובדים|אין עבודה", text):
        date_str = parse_date(text) or today_str
        return date_str, "DELETE", None, None

    date_str = parse_date(text) or today_str

    # מסיר את חלק התאריך מהטקסט כדי שלא יתפרש כשעה
    text_wo_date = re.sub(r"\d{1,2}[./]\d{1,2}(?:[./]\d{4})?", " ", text)

    start, end, workers = None, None, None

    # מספר עובדים
    w = re.search(r"(\d+)\s*עובדים?", text_wo_date)
    if w:
        workers = int(w.group(1))
        text_wo_date = text_wo_date[:w.start()] + " " + text_wo_date[w.end():]

    # טווח שעות: "07 עד 13" / "07:00 עד 13:00"
    he = re.search(r"(?:מ[־\-]?)?(\d{1,2}(?::\d{2})?)\s+עד\s+(\d{1,2}(?::\d{2})?)", text_wo_date)
    if he:
        start = normalize_hour(he.group(1))
        end = normalize_hour(he.group(2))
    else:
        # שני זמנים ברצף: "07:00 13:00"
        time_pair = re.search(r"(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})", text_wo_date)
        if time_pair:
            start = time_pair.group(1)
            end = time_pair.group(2)
        else:
            # התחלה בלבד: "התחלה 08:00" / "משעה 08" / "מ-08:00"
            start_only = re.search(r"(?:התחלה|מהשעה|משעה|מ[־\-]?)\s*(\d{1,2}(?::\d{2})?)", text_wo_date)
            if start_only:
                start = normalize_hour(start_only.group(1))
            # סיום בלבד: "סיום 15:00" / "עד השעה 15" / "סוף 15:00"
            end_only = re.search(r"(?:סיום|סוף|עד\s*השעה|עד\s*ה[־\-]?)\s*(\d{1,2}(?::\d{2})?)", text_wo_date)
            if end_only:
                end = normalize_hour(end_only.group(1))

            # אם אין התחלה/סיום מפורש ויש רק מספר אחד עם :MM — התייחס אליו כזמן התחלה
            if not start and not end:
                lone_time = re.search(r"(\d{1,2}:\d{2})", text_wo_date)
                if lone_time:
                    start = lone_time.group(1)

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

    # פורמט קצר: "שפפים 5" / "שפפים 5 עובדים" — שעות לפי ברירת מחדל של הצוות
    short = re.search(r"^[^\d]*(\d+)(?:\s*עובדים?)?\s*$", text)
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

def parse_monthly_summary(text):
    """מזהה בקשת סיכום חודשי. מחזיר (month, year) או None."""
    if "סיכום" not in text or "חודש" not in text:
        return None

    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    month = None
    year = now.year

    for name, num in MONTHS_HE.items():
        if name in text:
            month = num
            break

    if month is None:
        m = re.search(r"חודש\s*(\d{1,2})", text)
        if m:
            month = int(m.group(1))

    if month is None:
        return None

    y = re.search(r"(20\d{2})", text)
    if y:
        year = int(y.group(1))

    return month, year


def monthly_summary_for_team(team, month, year):
    """מחזיר (days_count, total_hours) עבור צוות בחודש מסוים."""
    sheet = connect_sheet(team)
    rows = sheet.get_all_values()
    days = 0
    total_hours = 0.0

    for row in rows:
        if not row or len(row) < 6:
            continue
        date_cell = row[0].strip()
        if not date_cell or not re.match(r"\d{1,2}/\d{1,2}/\d{4}", date_cell):
            continue
        try:
            d = datetime.strptime(date_cell, "%d/%m/%Y")
        except ValueError:
            continue
        if d.month != month or d.year != year:
            continue

        try:
            workers = int(row[4]) if row[4] else 0
        except (ValueError, IndexError):
            workers = 0
        if workers == 0:
            continue

        try:
            total = float(row[6]) if len(row) > 6 and row[6] else 0
        except ValueError:
            total = 0

        days += 1
        total_hours += total

    return days, total_hours


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # הפעלה ידנית של העדכון היומי
    if re.search(r"עדכן\s*(עכשיו|היום|ידני)|הפעל\s*עדכון", text):
        try:
            result = await _do_daily_auto_entry(context.bot, force=True)
            if result is None:
                await update.message.reply_text("📴 סוף שבוע — אין עדכון היום. (נלחצת בכל מקרה? שלח 'עדכן עכשיו')")
        except Exception as e:
            await update.message.reply_text(f"❌ עדכן ידני נכשל: {e}")
        return

    summary_req = parse_monthly_summary(text)
    if summary_req:
        month, year = summary_req
        month_name = MONTHS_HE_NAMES.get(month, str(month))
        lines = [f"📊 סיכום {month_name} {year}\n"]
        for team in TEAM_DEFAULTS.keys():
            days, hours = monthly_summary_for_team(team, month, year)
            lines.append(f"🏢 *{team}*")
            lines.append(f"   📅 {days} ימי עבודה")
            lines.append(f"   ⏱️ {hours:.0f} שעות סה\"כ\n")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

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
            "שפפים 5 30/07\n"
            "תיקון שפפים 30/07 5 עובדים\n"
            "תיקון שפפים 10 עובדים"
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

import asyncio


def _is_retryable_error(e):
    """בודק אם השגיאה זמנית וכדאי לנסות שוב."""
    if isinstance(e, gspread.exceptions.APIError):
        code = None
        try:
            code = e.response.status_code
        except Exception:
            pass
        if code is None:
            try:
                code = e.args[0].get("code")
            except Exception:
                pass
        if code is None:
            code_str = str(e)
            for c in ("500", "502", "503", "504", "429", "UNAVAILABLE"):
                if c in code_str:
                    return True
            return False
        return code >= 500 or code == 429
    if isinstance(e, (ConnectionError, TimeoutError)):
        return True
    return False


async def _write_team_with_retry(team, row, retries=5):
    """פותח את הגיליון של הצוות ומוסיף שורה — עם retry על שגיאות זמניות."""
    delay = 5
    last_err = None
    for attempt in range(retries):
        try:
            sheet = connect_sheet(team)
            sheet.append_row(row)
            return
        except Exception as e:
            last_err = e
            if not _is_retryable_error(e):
                raise
            logging.warning(f"Google Sheets error for {team} (attempt {attempt+1}/{retries}): {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    raise last_err


async def _do_daily_auto_entry(bot: Bot, force=False):
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    if not force and now.weekday() in [4, 5]:  # שישי, שבת
        return None

    date_str = now.strftime("%d/%m/%Y")
    day_str = DAYS_HE[now.weekday()]

    lines = []
    for team, defaults in TEAM_DEFAULTS.items():
        start = defaults["start"]
        end = defaults["end"]
        workers = defaults.get("workers", DEFAULT_WORKERS)
        hours = calc_hours(start, end)
        total = hours * workers
        await _write_team_with_retry(team, [date_str, day_str, start, end, workers, hours, total, ""])
        lines.append(f"• {team}: {start}–{end} | {workers} עובדים | {total:.0f} שעות")

    msg = (
        f"🤖 נרשם אוטומטי!\n"
        f"📅 {date_str} ({day_str})\n\n"
        + "\n".join(lines) +
        f"\n\nאם יש שינוי — שלח תיקון 📝\n"
        f"(שפפים דינמיקה)"
    )
    await bot.send_message(chat_id=CHAT_ID, text=msg)
    return msg


async def daily_auto_entry(bot: Bot):
    try:
        await _do_daily_auto_entry(bot)
    except Exception as e:
        logging.error(f"daily_auto_entry failed: {e}")
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ העדכון האוטומטי נכשל: {e}\nשלח 'עדכן עכשיו' כדי לנסות שוב."
            )
        except Exception:
            pass

async def error_handler(update, context):
    err = context.error
    logging.error(f"Exception while handling update: {err}")
    logging.error(traceback.format_exc())


async def heartbeat():
    now = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"💓 Heartbeat OK — {now}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Jerusalem"))
    scheduler.add_job(daily_auto_entry, "cron", hour=7, minute=0, args=[app.bot])
    scheduler.add_job(heartbeat, "interval", minutes=30)
    scheduler.start()

    print("✅ הבוט פועל!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
