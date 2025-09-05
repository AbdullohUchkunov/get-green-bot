# bot.py
import os
import re
import json
import logging
from typing import List, Tuple

from dotenv import load_dotenv
load_dotenv()

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ----------------------------- LOGGING -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("get-green-bot")

# ----------------------------- CONFIG ------------------------------
BOT_TOKEN       = os.environ["BOT_TOKEN"]
SPREADSHEET_ID  = os.environ["SPREADSHEET_ID"]  # Google Sheets URL’dagi /d/<ID>/

SHEET_SUN       = "sun hightech baza"
SHEET_GREEN     = "Фактура и заявка"

# A=1 ... O=15, P=16, Q=17, R=18, S=19, AA=27
COL_Q_SUN       = 17      # SunHightech qidiruv ustuni (Q)
COL_P_GREEN     = 16      # GET-GREEN qidiruv ustuni (P)

# SunHightech: B..L + O..Q + S  (15 ta qiymat)
SUN_RANGES: List[Tuple[str, str]] = [("B", "L"), ("O", "Q"), ("S", "S")]

# GET-GREEN: B..H + R + S + AA (10 ta qiymat)
GREEN_RANGES: List[Tuple[str, str]] = [("B", "H"), ("R", "R"), ("S", "S"), ("AA", "AA")]

# Ixtiyoriy: ruxsatli ID'lar (bo'sh bo'lsa hamma ishlatadi)
ALLOWED_IDS = set(os.getenv("ALLOWED_IDS", "").split())

# ------------------------- GOOGLE CREDENTIALS ----------------------
def _build_creds() -> Credentials:
    """
    Credentiallarni ENV'dan o‘qish:
      - GOOGLE_CREDENTIALS_JSON_B64  (base64 tavsiya)
      - yoki GOOGLE_CREDENTIALS_JSON (minified JSON yoki JSON fayl yo‘li)
    """
    c_b64 = os.getenv("GOOGLE_CREDENTIALS_JSON_B64", "").strip()
    c_j   = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

    c_dict = None
    if c_b64:
        import base64
        raw = base64.b64decode(c_b64)
        c_dict = json.loads(raw.decode("utf-8"))
    elif c_j:
        if c_j.startswith("{"):
            c_dict = json.loads(c_j)
        else:
            with open(c_j, "r", encoding="utf-8") as f:
                c_dict = json.load(f)
    else:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON_B64 yoki GOOGLE_CREDENTIALS_JSON kerak.")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    return Credentials.from_service_account_info(c_dict, scopes=scopes)

def gspread_client() -> gspread.Client:
    return gspread.authorize(_build_creds())

def ws(sheet_name: str) -> gspread.Worksheet:
    gc = gspread_client()
    return gc.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)

# ------------------------------ HELPERS ----------------------------
def digits_only(s: str) -> str:
    return re.sub(r"\D+", "", str(s or ""))

def col_to_a1(col_idx: int) -> str:
    """1-based ustun indeksini (1=A) A1 harflariga aylantiradi."""
    s = ""
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        s = chr(65 + rem) + s
    return s

def esc(s: str) -> str:
    return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def a1(row: int, c1: str, c2: str) -> str:
    return f"{c1}{row}:{c2}{row}"

def flatten(blocks: List[List[List[str]]]) -> List[str]:
    out: List[str] = []
    for b in blocks:
        if b and b[0]:
            out.extend(b[0])
    return out

def find_rows_in_col(
    sheet: gspread.Worksheet,
    col_idx: int,
    query: str,
    ranges: List[Tuple[str, str]]
) -> List[List[str]]:
    """
    col_idx ustunida (FORMATTED_VALUE) bo‘yicha raqam qidiradi,
    topilgan qatordagi 'ranges' bloklarini qaytaradi.
    """
    qd = digits_only(query)
    if not qd:
        return []

    col_letter = col_to_a1(col_idx)                 # 16 -> 'P', 17 -> 'Q'
    rng = f"{col_letter}2:{col_letter}"             # masalan 'P2:P'
    got = sheet.get(rng, value_render_option="FORMATTED_VALUE")

    # get() → 2D massiv; bo'sh bo'lsa [], har qatorda 1 ustun
    col_vals = [(r[0] if r else "") for r in got]
    hits = [i + 2 for i, v in enumerate(col_vals) if digits_only(v) == qd]

    rows: List[List[str]] = []
    for r in hits:
        blocks = sheet.batch_get(
            [a1(r, s, e) for (s, e) in ranges],
            value_render_option="FORMATTED_VALUE",
            major_dimension="ROWS"
        )
        rows.append([v or "" for v in flatten(blocks)])
    return rows

# ---------------------- FORMAT (GROUPED) MESSAGES ------------------
def format_green_grouped(rows: List[List[str]]) -> str:
    """
    rows: [B..H, R, S, AA] =
      [0:Договор,1:Группа,2:Наим,3:Ед.изм,4:Кол-во,5:Цена,6:Сумма,7:Дата,8:Менеджер,9:Status]
    Tepada: Договор, Дата, Менеджер, Factura status (faqat 1 marta)
    Pastda: har pozitsiya uchun tafsilotlar.
    """
    if not rows:
        return "🔎 GET-GREEN: topilmadi."

    head = rows[0]
    parts = [
        f"<b>Договор:</b> {esc(head[0])}",
        f"<b>Дата:</b> {esc(head[7])}",
        f"<b>Менеджер:</b> {esc(head[8])}",
        f"<b>Factura status:</b> {esc(head[9])}",
        ""
    ]
    for r in rows:
        parts += [
            f"<b>Группа товар:</b> {esc(r[1])}",
            f"<b>Наименование товаро:</b> {esc(r[2])}",
            f"<b>Ед.изм:</b> {esc(r[3])}",
            f"<b>Кол-во:</b> {esc(r[4])}",
            f"<b>Цена:</b> {esc(r[5])}",
            f"<b>Сумма:</b> {esc(r[6])}",
            ""
        ]
    return "\n".join(parts).strip()

def format_sun_grouped(rows: List[List[str]]) -> str:
    """
    rows: [B..L, O..Q, S] =
      [0:Дата,1:Кувват,2:Компания,3:Шарт №,4:Шарт сана,5:СТИР,6:МАНЗИЛ,
       7:Группа,8:Маҳсулот,9:Ўлчов,10:Миқдори,11:Цена,12:Сумма,13:Номер,14:Статус]
    Tepada: umumiy maydonlar (faqat 1 marta),
    Pastda: har pozitsiya tafsilotlari.
    """
    if not rows:
        return "🔎 SunHightech: topilmadi."

    head = rows[0]
    parts = [
        f"<b>Дата:</b> {esc(head[0])}",
        f"<b>Кувват:</b> {esc(head[1])}",
        f"<b>Компания номи:</b> {esc(head[2])}",
        f"<b>Шартнома раками:</b> {esc(head[3])}",
        f"<b>Шартнома санаси:</b> {esc(head[4])}",
        f"<b>СТИР:</b> {esc(head[5])}",
        f"<b>МАНЗИЛ:</b> {esc(head[6])}",
        f"<b>Номер:</b> {esc(head[13])}",
        f"<b>Статус:</b> {esc(head[14])}",
        ""
    ]
    for r in rows:
        parts += [
            f"<b>Группа товар:</b> {esc(r[7])}",
            f"<b>Махсулот номи:</b> {esc(r[8])}",
            f"<b>Миқдори:</b> {esc(r[10])}",
            f"<b>Цена:</b> {esc(r[11])}",
            f"<b>Сумма:</b> {esc(r[12])}",
            ""
        ]
    return "\n".join(parts).strip()

async def send_grouped(update: Update, rows: List[List[str]], mode: str):
    """Bitta yirik xabar ko'rinishida yuborish (uzun bo'lsa bo'lib yuboradi)."""
    text = format_green_grouped(rows) if mode == "green" else format_sun_grouped(rows)
    if len(text) <= 4000:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)
        return
    # Juda uzun bo'lsa, bo'lib yuboramiz (Telegram limiti ~4096)
    chunk, size = [], 0
    for line in text.split("\n"):
        if size + len(line) + 1 > 3900:
            await update.effective_chat.send_message("\n".join(chunk), parse_mode=ParseMode.HTML)
            chunk, size = [], 0
        chunk.append(line); size += len(line) + 1
    if chunk:
        await update.effective_chat.send_message("\n".join(chunk), parse_mode=ParseMode.HTML)

def allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    cid = str(update.effective_chat.id)
    uid = str(update.effective_user.id)
    return (cid in ALLOWED_IDS) or (uid in ALLOWED_IDS)

# ------------------------------ HANDLERS ---------------------------
KB = ReplyKeyboardMarkup(
    [[KeyboardButton("SunHightech")], [KeyboardButton("GET-GREEN")]],
    resize_keyboard=True
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("❌ Ruxsat yo‘q.")
        return
    context.user_data["mode"] = None
    await update.message.reply_text(
        "✅ Bot ishga tushdi.\nQuyidagidan birini tanlang, so‘ng raqam yuboring:",
        reply_markup=KB
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧾 /start — menyu\n"
        "SunHightech → Q ustunidan qidiradi (B:L, O:Q, S)\n"
        "GET-GREEN → P ustunidan qidiradi (B:H, R, S, AA)"
    )

async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("❌ Ruxsat yo‘q.")
        return

    txt = (update.message.text or "").strip()
    low = txt.lower()

    if low in {"sunhightech", "sun hightech", "sun"}:
        context.user_data["mode"] = "sun"
        await update.message.reply_text("✅ SunHightech tanlandi. Endi raqam yuboring.")
        return
    if low in {"get-green", "getgreen", "green"}:
        context.user_data["mode"] = "green"
        await update.message.reply_text("✅ GET-GREEN tanlandi. Endi raqam yuboring.")
        return

    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("Avval menyudan tanlang (SunHightech yoki GET-GREEN).")
        return

    q = digits_only(txt)
    if not q:
        await update.message.reply_text("🔢 Iltimos, faqat raqam yuboring. Masalan: 1163")
        return

    try:
        if mode == "sun":
            rows = find_rows_in_col(ws(SHEET_SUN), COL_Q_SUN, q, SUN_RANGES)
            await send_grouped(update, rows, "sun")
        else:
            rows = find_rows_in_col(ws(SHEET_GREEN), COL_P_GREEN, q, GREEN_RANGES)
            await send_grouped(update, rows, "green")
    except Exception as e:
        log.exception("router error")
        await update.message.reply_text(f"⚠️ Xatolik: {esc(e)}", parse_mode=ParseMode.HTML)

# ------------------------------- MAIN -----------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))

    mode = os.getenv("MODE", "polling").lower()
    if mode == "webhook":
        port = int(os.getenv("PORT", "8080"))
        base = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN"))
        if base and not base.startswith("http"):
            base = "https://" + base
        base = os.getenv("WEBHOOK_BASE_URL", base)
        if not base:
            raise RuntimeError("WEBHOOK_BASE_URL yoki RENDER_EXTERNAL_URL/RAILWAY_PUBLIC_DOMAIN kerak.")
        webhook_url = f"{base}/webhook/{BOT_TOKEN}"
        log.info("Running in webhook mode. %s", webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            allowed_updates=["message"],
            drop_pending_updates=False,
        )
    else:
        log.info("Running in polling mode.")
        app.run_polling(allowed_updates=["message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()
