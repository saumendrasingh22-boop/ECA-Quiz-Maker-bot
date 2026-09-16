""" ECA Quiz Bot - standalone production-oriented Telegram quiz bot. Interface language: English only. Quiz content language: Hindi / English / Bilingual. AI: Google Gemini API via google-genai. PDF: ReportLab with Devanagari font support when available. Storage: SQLite (no external database required). Required environment variables: BOT_TOKEN=Telegram bot token GEMINI_API_KEY=Google Gemini API key ADMIN_IDS=comma-separated Telegram numeric user IDs QUIZ_CHAT_ID=optional Telegram chat/channel ID for publishing prepared quiz links ECA_TELEGRAM_URL=https://t.me/EternalCivilAcademy Optional: DEFAULT_QUESTION_TIME=30 MAX_QUESTIONS=100 GEMINI_MODELS=gemini-3.6-flash,gemini-3.6-flash,gemini-3.5-flash-lite PDF_DIR=data/pdfs DB_PATH=data/quiz.db Install: pip install -r requirements.txt Run: python bot.py """

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PDF_DIR = Path(os.getenv("PDF_DIR", str(DATA_DIR / "pdfs")))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "quiz.db")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PDF_DIR.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ECA_TELEGRAM_URL = os.getenv(
    "ECA_TELEGRAM_URL", "https://t.me/EternalCivilAcademy"
).strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DEFAULT_QUESTION_TIME = max(5, int(os.getenv("DEFAULT_QUESTION_TIME", "30")))
MAX_QUESTIONS = min(100, max(1, int(os.getenv("MAX_QUESTIONS", "100"))))

# Current stable Flash model first; fallbacks are only attempted when needed.
GEMINI_MODELS = [
    x.strip()
    for x in os.getenv(
        "GEMINI_MODELS",
        "gemini-3.6-flash,gemini-3.6-flash,gemini-3.5-flash-lite",
    ).split(",")
    if x.strip()
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("eca_quiz_bot")

# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

DB_LOCK = asyncio.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """ CREATE TABLE IF NOT EXISTS quizzes ( id TEXT PRIMARY KEY, title TEXT NOT NULL, source_name TEXT, source_text TEXT, language TEXT NOT NULL, question_time INTEGER NOT NULL, questions_json TEXT NOT NULL, created_at TEXT NOT NULL, quiz_date TEXT NOT NULL, quiz_time TEXT NOT NULL ); CREATE TABLE IF NOT EXISTS attempts ( quiz_id TEXT NOT NULL, user_id INTEGER NOT NULL, username TEXT, display_name TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, current_index INTEGER NOT NULL DEFAULT 0, started_at TEXT, completed_at TEXT, PRIMARY KEY (quiz_id, user_id) ); CREATE TABLE IF NOT EXISTS answers ( quiz_id TEXT NOT NULL, user_id INTEGER NOT NULL, q_index INTEGER NOT NULL, selected INTEGER, correct INTEGER NOT NULL DEFAULT 0, answered_at TEXT NOT NULL, PRIMARY KEY (quiz_id, user_id, q_index) ); CREATE INDEX IF NOT EXISTS idx_answers_quiz_user ON answers(quiz_id, user_id); CREATE TABLE IF NOT EXISTS poll_map ( poll_id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, q_index INTEGER NOT NULL ); """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    # Render normally runs UTC. Telegram timestamps in the PDF are labelled UTC
    # only if TZ is unavailable; we prefer the process local timezone.
    return datetime.now().astimezone()


def safe_text(value: Any) -> str:
    return str(value or "").strip()


def clean_ui_text(text: str) -> str:
    """UI text is English-only and intentionally ASCII-safe."""
    text = safe_text(text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\x20-\x7E\n\r\t]", "", text)
    return text.strip()


def normalize_language(value: str) -> str:
    v = safe_text(value).lower()
    if "bilingual" in v:
        return "Bilingual"
    if "english" in v:
        return "English"
    return "Hindi"


def user_display_name(user) -> str:
    name = " ".join(
        x for x in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if x
    ).strip()
    return name or getattr(user, "username", None) or f"User {user.id}"


def user_label(user_id: int, username: Optional[str], display_name: str) -> str:
    # User asked for user ID in brackets if available. Telegram numeric ID is
    # always available; username is included when present.
    if username:
        return f"{display_name} (@{username})"
    return f"{display_name} [{user_id}]"


def raw_marks(right: int, wrong: int, total: int) -> float:
    return round(right - (wrong / 3.0), 2)


def rank_from_rows(rows: list[dict], position: int) -> int:
    if position == 0:
        return 1
    prev = rows[position - 1]
    cur = rows[position]
    if cur["right"] == prev["right"]:
        return rows[position - 1]["rank"]
    return position + 1


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

PDF_FONT_REGULAR = None
PDF_FONT_BOLD = None


def register_pdf_fonts():
    global PDF_FONT_REGULAR, PDF_FONT_BOLD
    candidates = [
        (
            BASE_DIR / "fonts" / "NotoSansDevanagari-Regular.ttf",
            BASE_DIR / "fonts" / "NotoSansDevanagari-Bold.ttf",
        ),
        (
            Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf"),
        ),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            try:
                pdfmetrics.registerFont(TTFont("ECADevanagari", str(regular)))
                pdfmetrics.registerFont(TTFont("ECADevanagariBold", str(bold)))
                PDF_FONT_REGULAR = "ECADevanagari"
                PDF_FONT_BOLD = "ECADevanagariBold"
                return
            except Exception as exc:
                log.warning("Could not register Devanagari font: %s", exc)

    PDF_FONT_REGULAR = "Helvetica"
    PDF_FONT_BOLD = "Helvetica-Bold"


def pdf_escape(text: str) -> str:
    return (
        safe_text(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_quiz_pdf(quiz: dict) -> Path:
    register_pdf_fonts()

    path = PDF_DIR / f"{quiz['id']}.pdf"
    styles = getSampleStyleSheet()
    base_font = PDF_FONT_REGULAR
    bold_font = PDF_FONT_BOLD

    title = ParagraphStyle(
        "ECATitle",
        parent=styles["Title"],
        fontName=bold_font,
        fontSize=18,
        leading=22,
        alignment=TA_CENTER,
        spaceAfter=4 * mm,
    )
    subtitle = ParagraphStyle(
        "ECASubtitle",
        parent=styles["Normal"],
        fontName=base_font,
        fontSize=9.5,
        leading=13,
        alignment=TA_CENTER,
        spaceAfter=2 * mm,
    )
    heading = ParagraphStyle(
        "ECAHeading",
        parent=styles["Heading2"],
        fontName=bold_font,
        fontSize=12,
        leading=16,
        spaceBefore=5 * mm,
        spaceAfter=2 * mm,
    )
    body = ParagraphStyle(
        "ECABody",
        parent=styles["BodyText"],
        fontName=base_font,
        fontSize=10.5,
        leading=15,
        spaceAfter=2.5 * mm,
    )
    small = ParagraphStyle(
        "ECASmall",
        parent=styles["BodyText"],
        fontName=base_font,
        fontSize=8.5,
        leading=12,
        alignment=TA_CENTER,
    )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=17 * mm,
        leftMargin=17 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{quiz['title']} - ETERNAL CIVIL ACADEMY",
        author="ETERNAL CIVIL ACADEMY",
    )

    story = []
    story.append(Paragraph("ETERNAL CIVIL ACADEMY", title))
    story.append(Paragraph("Your Success, Our Commitment", subtitle))
    story.append(Spacer(1, 2 * mm))
    story.append(HRFlowable(width="100%", thickness=0.8))
    story.append(Spacer(1, 4 * mm))

    meta = [
        ["TEST NO.", quiz.get("test_no", "01")],
        ["QUIZ TITLE", quiz["title"]],
        ["TOTAL QUESTIONS", str(len(quiz["questions"]))],
        ["QUIZ DATE", quiz["quiz_date"]],
        ["QUIZ TIME", quiz["quiz_time"]],
        ["TIME PER QUESTION", f"{quiz['question_time']} Seconds"],
        ["LANGUAGE", quiz["language"]],
    ]
    table_data = [
        [
            Paragraph(f"<b>{pdf_escape(a)}</b>", body),
            Paragraph(pdf_escape(str(b)), body),
        ]
        for a, b in meta
    ]
    t = Table(table_data, colWidths=[45 * mm, 125 * mm])
    t.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, None),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t)
    story.append(Spacer(1, 5 * mm))

    story.append(
        Paragraph(
            f'Telegram: <link href="{pdf_escape(ECA_TELEGRAM_URL)}">{pdf_escape(ECA_TELEGRAM_URL)}</link>',
            small,
        )
    )
    story.append(Spacer(1, 6 * mm))

    for i, q in enumerate(quiz["questions"], start=1):
        story.append(
            Paragraph(
                f"<b>Question {i}</b><br/>{pdf_escape(q['question'])}",
                body,
            )
        )

        options = q.get("options", [])
        correct = int(q.get("correct_index", 0))
        correct_text = options[correct] if 0 <= correct < len(options) else ""

        story.append(
            Paragraph(
                f"<b>Right Answer:</b> {pdf_escape(correct_text)}",
                body,
            )
        )
        story.append(
            Paragraph(
                f"<b>Explanation:</b> {pdf_escape(q.get('explanation', ''))}",
                body,
            )
        )
        if i != len(quiz["questions"]):
            story.append(Spacer(1, 2 * mm))

    story.append(Spacer(1, 7 * mm))
    story.append(HRFlowable(width="100%", thickness=0.6))
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            f"ETERNAL CIVIL ACADEMY | {pdf_escape(ECA_TELEGRAM_URL)}",
            small,
        )
    )

    doc.build(story)
    return path


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

GEMINI_CLIENT = None


def get_gemini_client():
    global GEMINI_CLIENT
    if GEMINI_CLIENT is None:
        if not genai:
            raise RuntimeError("google-genai package is not installed.")
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is missing.")
        GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    return GEMINI_CLIENT


def strip_json_fence(text: str) -> str:
    text = safe_text(text)
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def validate_questions(data: Any, requested: int) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list):
        raise ValueError("AI did not return a question list.")

    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        question = safe_text(item.get("question"))
        options = item.get("options")
        explanation = safe_text(item.get("explanation"))
        if not question or not isinstance(options, list) or len(options) != 4:
            continue
        options = [safe_text(x) for x in options]
        if any(not x for x in options):
            continue
        try:
            ci = int(item.get("correct_index"))
        except Exception:
            continue
        if ci not in range(4):
            continue
        if not explanation:
            continue

        # Reject malformed duplicates.
        if len({x.casefold() for x in options}) != 4:
            continue

        out.append(
            {
                "question": question,
                "options": options,
                "correct_index": ci,
                "explanation": explanation,
            }
        )
        if len(out) >= requested:
            break

    if len(out) < requested:
        raise ValueError(
            f"Only {len(out)} valid questions were returned; {requested} required."
        )
    return out


def generation_prompt( title: str, requested: int, language: str, source_text: str, ) -> str:
    language_rule = {
        "Hindi": "Write the question, four options, and explanation in natural Hindi using Devanagari.",
        "English": "Write the question, four options, and explanation in clear English.",
        "Bilingual": "Write each question, its four options, and explanation in a clean Hindi-English bilingual form. Do not use any third language.",
    }[language]

    source_block = source_text.strip()
    if len(source_block) > 45000:
        source_block = source_block[:45000]

    return f""" You are the examination-question engine for ETERNAL CIVIL ACADEMY. Create exactly {requested} high-quality single-correct-answer MCQs. Topic: {title} Language: {language} LANGUAGE RULE: {language_rule} IMPORTANT: - Never use emojis. - Never use mojibake, corrupted Unicode, or a third script. - Do not invent facts when a source is supplied. - Prefer the supplied source over general knowledge. - Each question must have exactly 4 distinct options. - correct_index must be 0, 1, 2, or 3. - Explanation must clearly justify the correct answer. - Avoid duplicate questions and duplicate options. - Do not include question numbering in the question text. - Return JSON only, with this exact structure: {{ "questions": [ {{ "question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0, "explanation": "..." }} ] }} SOURCE MATERIAL: {source_block if source_block else "[No source supplied. Use reliable general knowledge about the topic.]"} """.strip()


async def generate_questions( title: str, requested: int, language: str, source_text: str, ) -> tuple[list[dict], str]:
    """ Uses current Gemini models with bounded retry/backoff. 429 does not cause an immediate quiz crash; another configured model is attempted. We never claim unlimited quota. """
    client = get_gemini_client()
    prompt = generation_prompt(title, requested, language, source_text)
    errors = []

    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                if genai_types:
                    cfg = genai_types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                    )
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=prompt,
                        config=cfg,
                    )
                else:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model,
                        contents=prompt,
                    )

                text = getattr(response, "text", None)
                if not text:
                    raise ValueError("Gemini returned an empty response.")

                parsed = json.loads(strip_json_fence(text))
                questions = validate_questions(parsed, requested)
                return questions, model

            except Exception as exc:
                msg = str(exc)
                errors.append(f"model={model}, attempt={attempt+1}: {msg[:600]}")
                # Retry only transient-looking failures. A 404 means the model
                # is unavailable and should move immediately to the next model.
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    await asyncio.sleep(2 ** attempt)
                elif "500" in msg or "503" in msg or "UNAVAILABLE" in msg:
                    await asyncio.sleep(1.5 * (attempt + 1))
                else:
                    break

    raise RuntimeError("AI generation failed after configured models/retries: " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------

def extract_source_bytes(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        if not PdfReader:
            raise RuntimeError("pypdf is not installed.")
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts).strip()
        if not text:
            raise ValueError("No readable text was found in the PDF.")
        return text

    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding).strip()
        except Exception:
            pass
    raise ValueError("The source file could not be decoded as text.")


# ---------------------------------------------------------------------------
# Quiz state
# ---------------------------------------------------------------------------

@dataclass
class BuildState:
    source_name: Optional[str] = None
    source_text: str = ""
    title: str = ""
    count: int = 0
    language: str = ""
    start_mode: str = "Personally"
    question_time: int = DEFAULT_QUESTION_TIME


BUILD_STATES: dict[int, BuildState] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------

def admin_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Create Quiz"), KeyboardButton("Help")],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    args = context.args or []
    if args and args[0].startswith("quiz_"):
        quiz_id = args[0][5:]
        conn = db()
        row = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        conn.close()
        if not row:
            await update.message.reply_text("This quiz link is invalid or expired.")
            return
        quiz = dict(row)
        quiz["questions"] = json.loads(quiz["questions_json"])
        await start_attempt(update, context, quiz)
        return

    if is_admin(user.id):
        await update.message.reply_text(
            "ECA Quiz Bot is ready.\n\n"
            "Interface language: English only.\n"
            "Quiz content: Hindi / English / Bilingual.\n\n"
            "Use Create Quiz to prepare a new quiz.",
            reply_markup=admin_menu(),
        )
    else:
        await update.message.reply_text(
            "Welcome to ETERNAL CIVIL ACADEMY Quiz Bot.\n"
            "Use the quiz link provided by the academy to start a quiz."
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "Use the prepared quiz link to attempt a quiz."
        )
        return
    await update.message.reply_text(
        "Admin commands:\n"
        "/newquiz - create a quiz\n"
        "/cancel - cancel the current setup\n"
        "/publish QUIZ_ID - publish a prepared quiz link to QUIZ_CHAT_ID\n\n"
        "The bot UI is English-only. Quiz content can be Hindi, English or Bilingual."
    )


async def newquiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    BUILD_STATES[update.effective_user.id] = BuildState()
    await update.message.reply_text(
        "Send the source PDF/text file first.\n"
        "If you do not want to use a source, send: NO SOURCE",
        reply_markup=ReplyKeyboardRemove(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        BUILD_STATES.pop(update.effective_user.id, None)
    await update.message.reply_text("Operation cancelled.", reply_markup=admin_menu())


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    state = BUILD_STATES.get(user.id)
    if not state:
        return

    doc = update.message.document
    if not doc:
        return

    filename = doc.file_name or "source.txt"
    if not filename.lower().endswith((".pdf", ".txt", ".md")):
        await update.message.reply_text("Please send a PDF, TXT or MD source file.")
        return

    tg_file = await context.bot.get_file(doc.file_id)
    data = await tg_file.download_as_bytearray()

    try:
        text = extract_source_bytes(filename, bytes(data))
    except Exception as exc:
        await update.message.reply_text(f"Source could not be read: {exc}")
        return

    state.source_name = filename
    state.source_text = text
    await update.message.reply_text(
        "Source Ready\n\nNow enter the topic."
    )


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return

    text = safe_text(update.message.text)
    if text == "Create Quiz":
        await newquiz_cmd(update, context)
        return
    if text == "Help":
        await help_cmd(update, context)
        return

    state = BUILD_STATES.get(user.id)
    if not state:
        return

    # Step 1: source
    if not state.title and not state.source_name and not state.source_text:
        if text.upper() == "NO SOURCE":
            state.source_text = ""
            state.source_name = None
            await update.message.reply_text("Now enter the topic.")
        return

    # Step 2: topic
    if not state.title:
        state.title = text[:300]
        await update.message.reply_text(
            "How many questions do you need?\n\nEnter a number from 1 to 100."
        )
        return

    # Step 3: count
    if state.count == 0:
        if not text.isdigit() or not (1 <= int(text) <= MAX_QUESTIONS):
            await update.message.reply_text(
                f"Enter a valid number from 1 to {MAX_QUESTIONS}."
            )
            return
        state.count = int(text)
        await update.message.reply_text(
            "Choose quiz language:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    [KeyboardButton("Hindi"), KeyboardButton("English")],
                    [KeyboardButton("Bilingual")],
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    # Step 4: language
    if not state.language:
        lang = normalize_language(text)
        if lang not in {"Hindi", "English", "Bilingual"}:
            await update.message.reply_text("Choose Hindi, English or Bilingual.")
            return
        state.language = lang
        await update.message.reply_text(
            "Where should the quiz start?",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("Personally"), KeyboardButton("Group/Channel")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return

    # Step 5: start mode
    if state.start_mode == "Personally":
        # We need a sentinel because Personally is the default.
        state.start_mode = ""
    if state.start_mode == "":
        if text not in {"Personally", "Group/Channel"}:
            await update.message.reply_text(
                "Choose Personally or Group/Channel."
            )
            return
        state.start_mode = text
        await update.message.reply_text(
            "Choose the time allowed for each question.\n\n"
            "Enter seconds, for example: 30"
        )
        return

    # Step 6: timer
    if state.question_time == DEFAULT_QUESTION_TIME:
        if not text.isdigit() or not (5 <= int(text) <= 600):
            await update.message.reply_text(
                "Enter a valid time from 5 to 600 seconds."
            )
            return
        state.question_time = int(text)
        await prepare_quiz(update, context, state)
        BUILD_STATES.pop(user.id, None)


async def prepare_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, state: BuildState):
    await update.message.reply_text(
        "Preparing quiz...\n"
        "Generating, validating and checking all questions before publication."
    )

    try:
        questions, model = await generate_questions(
            state.title,
            state.count,
            state.language,
            state.source_text,
        )

        created = now_local()
        quiz_id = uuid.uuid4().hex[:12]
        quiz = {
            "id": quiz_id,
            "test_no": next_test_number(),
            "title": state.title,
            "source_name": state.source_name,
            "language": state.language,
            "question_time": state.question_time,
            "questions": questions,
            "created_at": created.isoformat(),
            "quiz_date": created.strftime("%d %B %Y"),
            "quiz_time": created.strftime("%I:%M %p"),
        }

        conn = db()
        conn.execute(
            """ INSERT INTO quizzes (id,title,source_name,source_text,language,question_time, questions_json,created_at,quiz_date,quiz_time) VALUES (?,?,?,?,?,?,?,?,?,?) """,
            (
                quiz_id,
                quiz["title"],
                quiz["source_name"],
                state.source_text,
                quiz["language"],
                quiz["question_time"],
                json.dumps(questions, ensure_ascii=False),
                quiz["created_at"],
                quiz["quiz_date"],
                quiz["quiz_time"],
            ),
        )
        conn.commit()
        conn.close()

        deep_link = f"https://t.me/{context.bot.username}?start=quiz_{quiz_id}"

        await update.message.reply_text(
            "QUIZ READY\n\n"
            f"Topic: {state.title}\n"
            f"Questions: {len(questions)}\n"
            f"Language: {state.language}\n"
            f"Model: {model}\n\n"
            "Questions have passed the preparation checks.\n"
            "Prepared Quiz Link:\n"
            f"{deep_link}",
            disable_web_page_preview=True,
            reply_markup=admin_menu(),
        )

    except Exception as exc:
        log.exception("Quiz preparation failed")
        await update.message.reply_text(
            "Error: The quiz could not be prepared.\n\n"
            "No incomplete quiz was published.\n\n"
            f"Reason: {clean_ui_text(str(exc))[:3500]}",
            reply_markup=admin_menu(),
        )


def next_test_number() -> str:
    conn = db()
    row = conn.execute("SELECT COUNT(*) AS n FROM quizzes").fetchone()
    conn.close()
    return f"{int(row['n']) + 1:02d}"


# ---------------------------------------------------------------------------
# Quiz attempting
# ---------------------------------------------------------------------------

async def handle_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    args = context.args or []
    if not args or not args[0].startswith("quiz_"):
        return

    quiz_id = args[0][5:]
    conn = db()
    row = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    conn.close()

    if not row:
        await update.message.reply_text("This quiz link is invalid or expired.")
        return

    quiz = dict(row)
    quiz["questions"] = json.loads(quiz["questions_json"])

    await start_attempt(update, context, quiz)


async def start_attempt(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz: dict):
    user = update.effective_user
    conn = db()
    existing = conn.execute(
        "SELECT * FROM attempts WHERE quiz_id=? AND user_id=?",
        (quiz["id"], user.id),
    ).fetchone()

    if existing and existing["completed"]:
        conn.close()
        await update.message.reply_text(
            "You have already completed this quiz."
        )
        return

    if not existing:
        conn.execute(
            """ INSERT INTO attempts (quiz_id,user_id,username,display_name,completed,current_index,started_at) VALUES (?,?,?,?,0,0,?) """,
            (
                quiz["id"],
                user.id,
                user.username,
                user_display_name(user),
                now_local().isoformat(),
            ),
        )
        conn.commit()
    conn.close()

    await update.message.reply_text(
        f"QUIZ STARTING\n\n"
        f"Time per question: {quiz['question_time']} seconds\n"
        "The next question starts automatically when time expires.\n"
        "Questions are sent one at a time.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_next_question(context, quiz["id"], user.id)


async def send_next_question(context: ContextTypes.DEFAULT_TYPE, quiz_id: str, user_id: int):
    conn = db()
    qrow = conn.execute(
        "SELECT * FROM attempts WHERE quiz_id=? AND user_id=?",
        (quiz_id, user_id),
    ).fetchone()
    quizrow = conn.execute(
        "SELECT * FROM quizzes WHERE id=?", (quiz_id,)
    ).fetchone()
    conn.close()

    if not qrow or not quizrow:
        return

    quiz = dict(quizrow)
    quiz["questions"] = json.loads(quiz["questions_json"])
    idx = int(qrow["current_index"])

    if idx >= len(quiz["questions"]):
        await complete_attempt(context, quiz, user_id)
        return

    q = quiz["questions"][idx]
    poll = await context.bot.send_poll(
        chat_id=user_id,
        question=f"ECA QUIZ | {quiz['title']}\nQuestion {idx + 1}",
        options=q["options"],
        type="quiz",
        correct_option_id=int(q["correct_index"]),
        is_anonymous=False,
        explanation=q.get("explanation", "")[:200],
    )

    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO poll_map(poll_id,quiz_id,q_index) VALUES(?,?,?)",
        (poll.poll.id, quiz_id, idx),
    )
    conn.commit()
    conn.close()

    # One timer job per user/question.
    context.job_queue.run_once(
        question_timeout,
        when=quiz["question_time"],
        data={"quiz_id": quiz_id, "user_id": user_id, "q_index": idx},
        name=f"timeout:{quiz_id}:{user_id}:{idx}",
    )


async def question_timeout(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    quiz_id = data["quiz_id"]
    user_id = data["user_id"]
    q_index = data["q_index"]

    conn = db()
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE quiz_id=? AND user_id=?",
        (quiz_id, user_id),
    ).fetchone()
    already = conn.execute(
        "SELECT 1 FROM answers WHERE quiz_id=? AND user_id=? AND q_index=?",
        (quiz_id, user_id, q_index),
    ).fetchone()

    if not attempt or int(attempt["completed"]) or already:
        conn.close()
        return

    conn.execute(
        """ INSERT INTO answers (quiz_id,user_id,q_index,selected,correct,answered_at) VALUES (?,?,?,?,?,?) """,
        (quiz_id, user_id, q_index, None, 0, now_local().isoformat()),
    )
    conn.execute(
        """ UPDATE attempts SET current_index=current_index+1 WHERE quiz_id=? AND user_id=? """,
        (quiz_id, user_id),
    )
    conn.commit()
    conn.close()

    await send_next_question(context, quiz_id, user_id)


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer:
        return

    conn = db()
    mapping = conn.execute(
        "SELECT * FROM poll_map WHERE poll_id=?", (answer.poll_id,)
    ).fetchone()
    if not mapping:
        conn.close()
        return

    quiz_id = mapping["quiz_id"]
    q_index = int(mapping["q_index"])
    user_id = answer.user.id

    quizrow = conn.execute(
        "SELECT * FROM quizzes WHERE id=?", (quiz_id,)
    ).fetchone()
    if not quizrow:
        conn.close()
        return

    questions = json.loads(quizrow["questions_json"])
    q = questions[q_index]
    selected = answer.option_ids[0] if answer.option_ids else None
    correct = int(selected == int(q["correct_index"])) if selected is not None else 0

    exists = conn.execute(
        "SELECT 1 FROM answers WHERE quiz_id=? AND user_id=? AND q_index=?",
        (quiz_id, user_id, q_index),
    ).fetchone()

    if not exists:
        conn.execute(
            """ INSERT INTO answers (quiz_id,user_id,q_index,selected,correct,answered_at) VALUES (?,?,?,?,?,?) """,
            (
                quiz_id,
                user_id,
                q_index,
                selected,
                correct,
                now_local().isoformat(),
            ),
        )
        conn.execute(
            """ UPDATE attempts SET current_index=current_index+1 WHERE quiz_id=? AND user_id=? AND current_index=? """,
            (quiz_id, user_id, q_index),
        )
        conn.commit()

    conn.close()
    await send_next_question(context, quiz_id, user_id)


# ---------------------------------------------------------------------------
# Results / leaderboard
# ---------------------------------------------------------------------------

def get_leaderboard(quiz_id: str) -> list[dict]:
    conn = db()
    attempts = conn.execute(
        """ SELECT * FROM attempts WHERE quiz_id=? AND completed=1 """,
        (quiz_id,),
    ).fetchall()

    rows = []
    quizrow = conn.execute(
        "SELECT questions_json FROM quizzes WHERE id=?", (quiz_id,)
    ).fetchone()
    total = len(json.loads(quizrow["questions_json"])) if quizrow else 0

    for a in attempts:
        stats = conn.execute(
            """ SELECT SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS right_count, SUM(CASE WHEN selected IS NOT NULL AND correct=0 THEN 1 ELSE 0 END) AS wrong_count, SUM(CASE WHEN selected IS NULL THEN 1 ELSE 0 END) AS unattempted FROM answers WHERE quiz_id=? AND user_id=? """,
            (quiz_id, a["user_id"]),
        ).fetchone()
        right = int(stats["right_count"] or 0)
        wrong = int(stats["wrong_count"] or 0)
        unattempted = int(stats["unattempted"] or 0)
        rows.append(
            {
                "user_id": a["user_id"],
                "username": a["username"],
                "display_name": a["display_name"],
                "right": right,
                "wrong": wrong,
                "unattempted": unattempted,
                "raw": raw_marks(right, wrong, total),
            }
        )
    conn.close()

    # Requested ranking rule: equal number correct = same rank.
    rows.sort(key=lambda x: (-x["right"], x["raw"], x["display_name"].casefold()))
    for i in range(len(rows)):
        rows[i]["rank"] = rank_from_rows(rows, i)

    # Show all if <=50; otherwise top 50.
    return rows if len(rows) <= 50 else rows[:50]


def format_leaderboard(quiz: dict, rows: list[dict]) -> str:
    lines = [
        "ECA LIVE QUIZ - LEADERBOARD",
        f"Quiz: {clean_ui_text(quiz['title'])}",
        "",
    ]
    for r in rows:
        name = clean_ui_text(r["display_name"])
        identity = f"{name} [{r['user_id']}]"
        lines.append(
            f"{identity} "
            f"✅{r['right']} ❌{r['wrong']} ⭕{r['unattempted']} "
            f"[Raw marks - {r['raw']:.2f}] {r['rank']}"
        )
    if not rows:
        lines.append("No completed attempts yet.")
    return "\n".join(lines)


async def complete_attempt(context: ContextTypes.DEFAULT_TYPE, quiz: dict, user_id: int):
    conn = db()
    conn.execute(
        """ UPDATE attempts SET completed=1, completed_at=? WHERE quiz_id=? AND user_id=? """,
        (now_local().isoformat(), quiz["id"], user_id),
    )
    conn.commit()
    conn.close()

    rows = get_leaderboard(quiz["id"])
    leaderboard = format_leaderboard(quiz, rows)

    pdf_path = await asyncio.to_thread(build_quiz_pdf, quiz)

    await context.bot.send_message(
        chat_id=user_id,
        text="QUIZ COMPLETED\n\n" + leaderboard,
    )
    await context.bot.send_document(
        chat_id=user_id,
        document=pdf_path.open("rb"),
        caption=(
            "Complete Quiz PDF\n"
            f"Test No. {quiz['test_no']} | {quiz['quiz_date']} | {quiz['quiz_time']}\n"
            "ETERNAL CIVIL ACADEMY"
        ),
    )


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

async def publish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /publish QUIZ_ID")
        return

    chat_id = os.getenv("QUIZ_CHAT_ID", "").strip()
    if not chat_id:
        await update.message.reply_text(
            "QUIZ_CHAT_ID is not configured. The prepared quiz link can still be used personally."
        )
        return

    quiz_id = context.args[0]
    conn = db()
    row = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("Quiz ID not found.")
        return

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=quiz_{quiz_id}"
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"ETERNAL CIVIL ACADEMY\n\n"
            f"Quiz: {clean_ui_text(row['title'])}\n"
            f"Questions: {len(json.loads(row['questions_json']))}\n\n"
            f"Attempt Quiz:\n{link}"
        ),
        disable_web_page_preview=True,
    )
    await update.message.reply_text("Quiz link published.")


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    init_db()
    register_pdf_fonts()

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured.")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is not configured.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newquiz", newquiz_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("publish", publish_cmd))

    # Deep-link start is handled before ordinary start behavior by inspecting args.
    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_admin_document,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_admin_text,
        )
    )
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_error_handler(error_handler)

    log.info("ECA Quiz Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
