#!/bin/bash
cd "$(dirname "$0")"
echo "מתקין..."
pip3 install python-telegram-bot gspread google-auth
echo ""
echo "✅ ההתקנה הסתיימה!"
echo "עכשיו פתח את הקובץ הפעלה.command"
read -p "לחץ Enter לסיום..."
