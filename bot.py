# -*- coding: utf-8 -*-
""" ECA QUIZ MAKER - Production Telegram Quiz Bot Designed for Render + Telegram + Google Gemini. Core flow --------- 1) AI Generate Questions 2) I Will Provide Source AI mode: Topic -> Count -> Language -> Gemini searches/grounds sources -> original MCQs -> strict validation -> prepared quiz. Source mode: PDF / Photo / Text / Telegram Poll / URL -> Topic -> Count -> Language -> original MCQs based on supplied material. Prepared quiz is NEVER dumped as a batch of Telegram polls. Admin chooses Personal or Group, then chooses per-question time: 15 sec / 25 sec / 30 sec / 1 min. Exactly one native Telegram quiz poll is published at a time. Important quota behavior ------------------------ A 429 RESOURCE_EXHAUSTED error is a provider quota problem. Changing models inside the same Google project does NOT magically create quota. This bot: - avoids repeated hammering of the same key/model; - supports multiple GEMINI_API_KEYS (comma-separated); - uses a single bounded retry for transient failures; - records/returns partial valid output rather than fabricating questions; - never bypasses validation just to reach the requested count. For actual multi-key quota failover, keys should belong to separately usable projects/quotas. Never paste keys into Telegram/GitHub. """

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from google import genai
from google.genai import types
from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from telegram import Poll, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
)


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0") or "0")

# Primary Gemini key + optional comma-separated additional keys.
# Example (Render Environment Variables):
# GEMINI_API_KEY=key1
# GEMINI_API_KEYS=key1,key2,key3
raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()
GEMINI_API_KEYS: list[str] = []
if raw_keys:
    GEMINI_API_KEYS.extend([x.strip() for x in raw_keys.split(",") if x.strip()])
if os.getenv("GEMINI_API_KEY", "").strip():
    GEMINI_API_KEYS.insert(0, os.getenv("GEMINI_API_KEY", "").strip())
# Remove duplicates while preserving order.
GEMINI_API_KEYS = list(dict.fromkeys(GEMINI_API_KEYS))

PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
FALLBACK_MODELS = [
    x.strip()
    for x in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-2.5-flash-lite",
    ).split(",")
    if x.strip()
]

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///eca_quiz_v3.db").strip()
PORT = int(os.getenv("PORT", "10000") or "10000")

MAX_QUESTIONS = 100
MAX_SOURCE_TEXT = 120_000
MAX_HISTORY_FOR_PROMPT = 250
MAX_HISTORY_FOR_SIMILARITY = 1200
AI_MAX_CALLS_PER_REQUEST = 8
AI_TRANSIENT_RETRY_COUNT = 1
AI_RETRY_DELAY = 2.0
SOURCE_FOOTER = "Source: @EternalCivilAcademy"

LANGUAGE_LABELS = {
    "Hindi": "Hindi",
    "English": "English",
    "Bilingual": "Bilingual",
}
LANGUAGE_BUTTONS = [["Hindi", "English"], ["Bilingual"]]

TIME_OPTIONS = {
    "15 seconds": 15,
    "25 seconds": 25,
    "30 seconds": 30,
    "1 minute": 60,
}
TIME_BUTTONS = [["15 seconds", "25 seconds"], ["30 seconds", "1 minute"]]

MAIN_BUTTONS = [
    ["AI Generate Questions"],
    ["I Will Provide Source"],
]

SOURCE_TYPE_BUTTONS = [
    ["PDF", "Photo"],
    ["Text", "Telegram Poll"],
    ["URL"],
]


# ============================================================
# Logging / validation
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("eca_quiz_bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")
if OWNER_USER_ID == 0:
    raise RuntimeError("OWNER_USER_ID environment variable is missing.")
if not GEMINI_API_KEYS:
    raise RuntimeError("GEMINI_API_KEY or GEMINI_API_KEYS environment variable is missing.")


# ============================================================
# Database
# ============================================================

class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "eca_v3_admins"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    added_by: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class QuestionHistory(Base):
    __tablename__ = "eca_v3_question_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class Quiz(Base):
    __tablename__ = "eca_v3_quizzes"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(30), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    source_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class QuizQuestion(Base):
    __tablename__ = "eca_v3_quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quiz_id: Mapped[str] = mapped_column(String(80), nullable=False)
    question_no: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("quiz_id", "question_no", name="uq_eca_v3_quiz_question_no"),
    )


class QuizRun(Base):
    __tablename__ = "eca_v3_quiz_runs"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    quiz_id: Mapped[str] = mapped_column(String(80), nullable=False)
    target_chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    started_by: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)  # personal/group
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    current_question: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    leaderboard_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class QuizPoll(Base):
    __tablename__ = "eca_v3_quiz_polls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)
    quiz_id: Mapped[str] = mapped_column(String(80), nullable=False)
    poll_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    question_no: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PollAnswer(Base):
    __tablename__ = "eca_v3_poll_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_name: Mapped[str] = mapped_column(String(300), nullable=False)
    selected_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", name="uq_eca_v3_poll_user_answer"),
    )


connect_args: dict[str, Any] = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    if DATABASE_URL.startswith("sqlite"):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        except Exception:
            logger.exception("SQLite PRAGMA setup failed; continuing.")


# ============================================================
# Render health server
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ECA Quiz Maker Bot is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def start_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info("Health server listening on 0.0.0.0:%s", PORT)
    server.serve_forever()


# ============================================================
# Gemini client pool / quota handling
# ============================================================

GEMINI_CLIENTS = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]

# Short in-process cooldown after a full provider quota failure, preventing
# accidental rapid-fire admin retries from hammering the same quota.
AI_COOLDOWN_UNTIL = 0.0
AI_COOLDOWN_LOCK = threading.Lock()


class AIQuotaError(RuntimeError):
    pass


class AITransientError(RuntimeError):
    pass


class AIPermanentError(RuntimeError):
    pass


def classify_ai_error(exc: Exception) -> str:
    text = str(exc).lower()
    if any(x in text for x in ("resource_exhausted", "quota", "too many requests", "429")):
        return "quota"
    if any(x in text for x in ("503", "502", "504", "temporarily unavailable", "service unavailable", "timeout")):
        return "transient"
    if any(x in text for x in ("401", "403", "api key", "permission denied", "unauthenticated", "invalid argument")):
        return "permanent"
    return "other"


def cooldown_seconds_remaining() -> int:
    with AI_COOLDOWN_LOCK:
        remaining = AI_COOLDOWN_UNTIL - time.time()
    return max(0, int(remaining))


def set_ai_cooldown(seconds: int = 45) -> None:
    global AI_COOLDOWN_UNTIL
    with AI_COOLDOWN_LOCK:
        AI_COOLDOWN_UNTIL = max(AI_COOLDOWN_UNTIL, time.time() + seconds)


# ============================================================
# Text helpers / duplicate detection
# ============================================================

MOJIBAKE_MARKERS = ("Ãƒ", "Ã‚", "Ã°", "Ã¢", "Ã Â¤", "ï¿½")

def repair_mojibake(value: Any) -> str:
    """Repair common UTF-8 mojibake defensively without altering normal Unicode text."""
    text = str(value or "")
    best = text

    def bad_score(s: str) -> int:
        return sum(s.count(marker) for marker in ("Ãƒ", "Ã‚", "Ã°", "Ã¢", "Ã Â¤", "ï¿½"))

    for _ in range(3):
        candidates = [best]
        for encoding in ("cp1252", "latin1"):
            try:
                candidates.append(best.encode(encoding).decode("utf-8"))
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        candidate = min(candidates, key=bad_score)
        if bad_score(candidate) >= bad_score(best):
            break
        best = candidate

    return best


PUNCT_RE = re.compile(r"[^\w\s\u0900-\u097F]", flags=re.UNICODE)
SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = repair_mojibake(text).strip().lower()
    text = PUNCT_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def tokens(text: str) -> set[str]:
    return {x for x in normalize(text).split() if len(x) > 1}


def question_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / max(1, min(len(ta), len(tb)))


def hash_question(question: str) -> str:
    return hashlib.sha256(normalize(question).encode("utf-8")).hexdigest()


def exact_duplicate_exists(question: str) -> bool:
    q_hash = hash_question(question)
    with SessionLocal() as session:
        row = session.scalar(
            select(QuestionHistory.id)
            .where(QuestionHistory.question_hash == q_hash)
            .limit(1)
        )
    return row is not None


def similar_previous_question(question: str, threshold: float = 0.84) -> bool:
    q_tokens = tokens(question)
    if len(q_tokens) < 4:
        return exact_duplicate_exists(question)

    with SessionLocal() as session:
        rows = session.scalars(
            select(QuestionHistory.normalized_question)
            .order_by(QuestionHistory.id.desc())
            .limit(MAX_HISTORY_FOR_SIMILARITY)
        ).all()

    for old in rows:
        old_tokens = set(old.split())
        if not old_tokens:
            continue
        score = len(q_tokens & old_tokens) / max(1, min(len(q_tokens), len(old_tokens)))
        if score >= threshold:
            return True
    return False


def recent_history(limit: int = MAX_HISTORY_FOR_PROMPT) -> list[str]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(QuestionHistory.question)
            .order_by(QuestionHistory.id.desc())
            .limit(limit)
        ).all()
    return list(reversed(rows))


def save_history(question: dict[str, Any]) -> None:
    text = repair_mojibake(question.get("question", "")).strip()
    if not text:
        return
    q_hash = hash_question(text)
    with SessionLocal() as session:
        exists = session.scalar(
            select(QuestionHistory.id)
            .where(QuestionHistory.question_hash == q_hash)
            .limit(1)
        )
        if exists:
            return
        session.add(
            QuestionHistory(
                question_hash=q_hash,
                question=text,
                normalized_question=normalize(text),
                topic=str(question.get("topic", ""))[:500],
                source=str(question.get("source", ""))[:5000],
            )
        )
        session.commit()


# ============================================================
# Authorization / admin commands
# ============================================================


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_USER_ID


def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    with SessionLocal() as session:
        return session.get(Admin, user_id) is not None


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else "-"
    await update.effective_message.reply_text(
        f"Telegram User ID\n{user.id}\n\nUsername: {username}"
    )


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("Only the Owner can add admins.")
        return

    target_id: Optional[int] = None
    target_name = ""
    reply = update.effective_message.reply_to_message
    if reply and reply.from_user:
        target_id = reply.from_user.id
        target_name = reply.from_user.full_name
    elif context.args:
        try:
            target_id = int(context.args[0])
            target_name = str(target_id)
        except ValueError:
            target_id = None

    if not target_id:
        await update.effective_message.reply_text(
            "Reply to the user message and send /addadmin.\n\n"
            "Or use /addadmin USER_ID"
        )
        return

    if target_id == OWNER_USER_ID:
        await update.effective_message.reply_text("This user is already the Owner.")
        return

    with SessionLocal() as session:
        if session.get(Admin, target_id):
            await update.effective_message.reply_text(f"This user is already an Admin.\nID: {target_id}")
            return
        session.add(Admin(user_id=target_id, added_by=OWNER_USER_ID))
        session.commit()

    await update.effective_message.reply_text(
        f"Admin authorized\n\nName: {target_name}\nUser ID: {target_id}"
    )


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("Only the Owner can remove admins.")
        return

    target_id: Optional[int] = None
    reply = update.effective_message.reply_to_message
    if reply and reply.from_user:
        target_id = reply.from_user.id
    elif context.args:
        try:
            target_id = int(context.args[0])
        except ValueError:
            target_id = None

    if not target_id:
        await update.effective_message.reply_text("Reply to the admin message with /removeadmin.")
        return
    if target_id == OWNER_USER_ID:
        await update.effective_message.reply_text("The Owner cannot be removed.")
        return

    with SessionLocal() as session:
        admin = session.get(Admin, target_id)
        if not admin:
            await update.effective_message.reply_text("This user is not an Admin.")
            return
        session.delete(admin)
        session.commit()

    await update.effective_message.reply_text(f"Admin access removed\nUser ID: {target_id}")


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text("Only the Owner can view the admin list.")
        return

    with SessionLocal() as session:
        rows = session.scalars(select(Admin).order_by(Admin.added_at)).all()

    lines = ["ECA QUIZ MAKER ADMINS", "", f"Owner: {OWNER_USER_ID}", "", "Authorized Admins:"]
    if not rows:
        lines.append("No additional Admins.")
    else:
        lines.extend(f"{i}. {row.user_id}" for i, row in enumerate(rows, 1))
    await update.effective_message.reply_text("\n".join(lines))


# ============================================================
# URL / file utilities
# ============================================================


def fetch_url_text(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP/HTTPS URLs are supported.")

    response = requests.get(
        url,
        timeout=25,
        allow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ECAQuizMaker/3.0; +https://t.me/EternalCivilAcademy)"
        },
    )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" not in content_type and "text/plain" not in content_type:
        raise ValueError("The URL did not return readable HTML/text content.")

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = SPACE_RE.sub(" ", text)
    if len(text) < 100:
        raise ValueError("The webpage did not expose enough readable text.")
    return text[:MAX_SOURCE_TEXT]


async def download_message_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str]:
    message = update.effective_message
    if message.photo:
        tg_file = await context.bot.get_file(message.photo[-1].file_id)
        suffix = ".jpg"
    elif message.document:
        tg_file = await context.bot.get_file(message.document.file_id)
        suffix = Path(message.document.file_name or "source").suffix.lower() or ".bin"
    else:
        raise ValueError("No supported file found.")

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    path = temp.name
    temp.close()
    await tg_file.download_to_drive(custom_path=path)
    return path, suffix


def cleanup_file(path: Optional[str]) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        logger.warning("Could not clean temporary file: %s", path)



# ============================================================
# PDF answer & explanation booklet
# ============================================================

TELEGRAM_URL = "https://t.me/EternalCivilAcademy"


def find_unicode_pdf_font() -> tuple[str, Optional[str]]:
    """Find a Unicode TTF available in the hosting environment. DejaVu Sans is commonly present on Debian/Ubuntu-based Render images and covers the Latin + Devanagari text needed by the ECA booklet. We also check a few other common system locations before falling back to Helvetica. """
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        ),
        (
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        ),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular):
            return regular, bold if os.path.exists(bold) else None
    return "", None


PDF_FONT_NAME = "Helvetica"
PDF_FONT_BOLD = "Helvetica-Bold"
_pdf_regular, _pdf_bold = find_unicode_pdf_font()

if _pdf_regular:
    try:
        pdfmetrics.registerFont(TTFont("ECAUnicode", _pdf_regular))
        PDF_FONT_NAME = "ECAUnicode"
        if _pdf_bold:
            pdfmetrics.registerFont(TTFont("ECAUnicodeBold", _pdf_bold))
            PDF_FONT_BOLD = "ECAUnicodeBold"
        else:
            PDF_FONT_BOLD = PDF_FONT_NAME
    except Exception:
        logger.exception("Could not register Unicode PDF font; using Helvetica.")


def escape_pdf_text(value: Any) -> str:
    """Normalize/repair text before placing it in a ReportLab Paragraph."""
    text = repair_mojibake(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def get_quiz_test_number(quiz_id: str) -> int:
    """Return a stable sequential test number based on quiz creation order."""
    with SessionLocal() as session:
        quizzes = session.scalars(
            select(Quiz.id).order_by(Quiz.created_at, Quiz.id)
        ).all()
    try:
        return quizzes.index(quiz_id) + 1
    except ValueError:
        return len(quizzes) + 1


def format_quiz_datetime(value: Optional[datetime]) -> str:
    if not value:
        return "Not available"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local_time = value.astimezone(timezone(timedelta(hours=5, minutes=30)))
    return local_time.strftime("%d %B %Y, %I:%M %p")


def format_time_per_question(seconds: int) -> str:
    seconds = int(seconds)
    if seconds == 60:
        return "1 Minute"
    return f"{seconds} Seconds"


def _pdf_header_footer(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4

    canvas.setFont(PDF_FONT_BOLD, 9)
    canvas.drawCentredString(
        width / 2,
        height - 12 * mm,
        "Eternal Civil Academy"
    )
    canvas.setFont(PDF_FONT_NAME, 7.5)
    link_text = "Telegram: @EternalCivilAcademy"
    x = width / 2
    y = 9 * mm
    text_width = pdfmetrics.stringWidth(link_text, PDF_FONT_NAME, 7.5)
    canvas.drawString(x - text_width / 2, y, link_text)
    canvas.linkURL(
        TELEGRAM_URL,
        (x - text_width / 2, y - 1.5 * mm, x + text_width / 2, y + 3.0 * mm),
        relative=0,
    )
    canvas.setFont(PDF_FONT_NAME, 7)
    canvas.drawRightString(
        width - 15 * mm,
        9 * mm,
        f"Page {doc.page}"
    )
    canvas.restoreState()


def build_answer_explanation_pdf(run_id: str, output_path: str) -> str:
    """Create the requested ECA answer + explanation booklet. The PDF contains: - Test number - Quiz title/topic - Total questions - Actual quiz start date/time - Time per question - Quiz language - ECA branding + Telegram link - Every question - Correct answer - Explanation It intentionally does NOT contain student-wise marks, rank, or attempts. """
    with SessionLocal() as session:
        run = session.get(QuizRun, run_id)
        if not run:
            raise RuntimeError("Quiz run not found.")

        quiz = session.get(Quiz, run.quiz_id)
        if not quiz:
            raise RuntimeError("Quiz not found.")

        questions = session.scalars(
            select(QuizQuestion)
            .where(QuizQuestion.quiz_id == quiz.id)
            .order_by(QuizQuestion.question_no)
        ).all()

        if not questions:
            raise RuntimeError("No quiz questions found.")

        test_number = get_quiz_test_number(quiz.id)
        topic = repair_mojibake(quiz.title)
        language = repair_mojibake(quiz.language)

        # Preserve the actual quiz start time and configured per-question time.
        started_at = run.started_at
        interval_seconds = int(run.interval_seconds)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=22 * mm,
        bottomMargin=16 * mm,
        title=f"Eternal Civil Academy - Test No. {test_number:02d}",
        author="Eternal Civil Academy",
        subject="Quiz Answer and Explanation Booklet",
    )

    styles = getSampleStyleSheet()

    brand = ParagraphStyle(
        "ECABrand",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=16,
        leading=20,
        alignment=TA_CENTER,
        spaceAfter=2 * mm,
    )
    tagline = ParagraphStyle(
        "ECATagline",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=9.5,
        leading=12,
        alignment=TA_CENTER,
        spaceAfter=5 * mm,
    )
    title_style = ParagraphStyle(
        "QuizTitle",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=14,
        leading=18,
        alignment=TA_CENTER,
        spaceAfter=5 * mm,
    )
    meta = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=9.2,
        leading=14,
        spaceAfter=1.2 * mm,
    )
    q_style = ParagraphStyle(
        "Question",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=10.5,
        leading=15,
        spaceBefore=3 * mm,
        spaceAfter=2 * mm,
    )
    answer_style = ParagraphStyle(
        "Answer",
        parent=styles["Normal"],
        fontName=PDF_FONT_BOLD,
        fontSize=9.8,
        leading=14,
        spaceAfter=2 * mm,
    )
    expl_style = ParagraphStyle(
        "Explanation",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=9.5,
        leading=14,
        spaceAfter=2 * mm,
    )
    option_style = ParagraphStyle(
        "Option",
        parent=styles["Normal"],
        fontName=PDF_FONT_NAME,
        fontSize=9.4,
        leading=13,
        leftIndent=5 * mm,
    )

    story = [
        Paragraph("ETERNAL CIVIL ACADEMY", brand),
        Paragraph("Your Success, Our Commitment", tagline),
        Paragraph(f"TEST NO. {test_number:02d}", title_style),
        Paragraph(
            f"<b>QUIZ TITLE:</b> {escape_pdf_text(topic)}",
            meta,
        ),
        Paragraph(
            f"<b>TOTAL QUESTIONS:</b> {len(questions)}",
            meta,
        ),
        Paragraph(
            f"<b>QUIZ DATE &amp; TIME:</b> {escape_pdf_text(format_quiz_datetime(started_at))}",
            meta,
        ),
        Paragraph(
            f"<b>TIME PER QUESTION:</b> {escape_pdf_text(format_time_per_question(interval_seconds))}",
            meta,
        ),
        Paragraph(
            f"<b>LANGUAGE:</b> {escape_pdf_text(language)}",
            meta,
        ),
        Paragraph(
            f"<b>TELEGRAM:</b> <link href=\"{TELEGRAM_URL}\">{escape_pdf_text(TELEGRAM_URL)}</link>",
            meta,
        ),
        Spacer(1, 4 * mm),
    ]

    for question in questions:
        question_text = repair_mojibake(question.question_text)
        options = json.loads(question.options_json)
        correct_index = int(question.correct_index)
        explanation = repair_mojibake(question.explanation)

        story.append(
            Paragraph(
                f"<b>Question {question.question_no}.</b> {escape_pdf_text(question_text)}",
                q_style,
            )
        )

        for idx, option in enumerate(options):
            label = chr(65 + idx)
            story.append(
                Paragraph(
                    f"{label}. {escape_pdf_text(option)}",
                    option_style,
                )
            )

        correct_text = ""
        if 0 <= correct_index < len(options):
            correct_text = f"{chr(65 + correct_index)}. {repair_mojibake(options[correct_index])}"
        else:
            correct_text = "Not available"

        story.append(
            Spacer(1, 1.5 * mm)
        )
        story.append(
            Paragraph(
                f"<b>Correct Answer:</b> {escape_pdf_text(correct_text)}",
                answer_style,
            )
        )
        story.append(
            Paragraph(
                f"<b>Explanation:</b> {escape_pdf_text(explanation)}",
                expl_style,
            )
        )

    doc.build(story, onFirstPage=_pdf_header_footer, onLaterPages=_pdf_header_footer)
    return str(output)


async def send_answer_explanation_pdf( context: ContextTypes.DEFAULT_TYPE, run_id: str, chat_id: int, ) -> None:
    """Build and send the completed quiz answer/explanation PDF."""
    pdf_path = Path(tempfile.gettempdir()) / f"eca_test_{run_id.replace('/', '_')}.pdf"
    try:
        await asyncio.to_thread(
            build_answer_explanation_pdf,
            run_id,
            str(pdf_path),
        )
        with pdf_path.open("rb") as fh:
            await context.bot.send_document(
                chat_id=chat_id,
                document=fh,
                filename=pdf_path.name,
                caption="Eternal Civil Academy - Answer & Explanation Booklet",
            )
    except Exception:
        logger.exception("Could not generate/send answer explanation PDF for run=%s", run_id)
        await safe_send_message(
            context.bot,
            chat_id,
            "The quiz is complete, but the Answer & Explanation PDF could not be generated.",
        )
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Question schema / prompt
# ============================================================

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "source": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": [
                    "question",
                    "options",
                    "correct_index",
                    "explanation",
                    "source",
                    "topic",
                ],
            },
        }
    },
    "required": ["questions"],
}


SYSTEM_INSTRUCTION = """ You are the senior competitive-exam question setter for Eternal Civil Academy (ECA). Target: UPSC, UPPSC/PCS, BPSC, MPPSC, State PCS and similarly serious exams. NON-NEGOTIABLE: - Create ORIGINAL MCQs. Never copy or closely paraphrase a source question. - Never repeat previous ECA questions or obvious near-duplicates. - Exactly 4 options and exactly 1 correct option. - Questions must be objective, unambiguous, exam-standard and factually supportable. - Never invent an Article, Act, rule, committee, judgment, report, scheme, statistic, date or institutional fact. - Prefer primary/authoritative sources for AI-search mode: government, Parliament, ministries, constitutional/legal texts, RBI, SEBI, UPSC, NCERT, ECI, official reports, UN/World Bank etc. as appropriate. - Spread questions across meaningful subtopics, chronology, concepts, provisions, cause-effect, comparison, application and analytical angles. - Avoid repeating one narrow factual template. At most 1-2 questions from one narrow subtopic in a batch. - Keep each question <=300 characters. - Keep each option <=100 characters. - Keep each explanation <=200 characters. - Explanation must state why the correct option is correct and briefly distinguish the other three options. - Quality is more important than count. Return fewer valid questions rather than weak/fabricated ones. - Preserve the requested language exactly. - Source field must identify the factual basis actually used. Never fabricate a URL. - Return only the requested JSON structure. LANGUAGE: Hindi = standard exam Hindi; English technical terms may be placed in brackets. English = clear exam-standard English. Bilingual = concise Hindi + English without sacrificing factual quality. """


def build_prompt( topic: str, count: int, language: str, mode: str, source_text: str = "", history: Optional[list[str]] = None, already_generated: Optional[list[str]] = None, ) -> str:
    history = history or []
    already_generated = already_generated or []

    history_block = "\n".join(f"- {q[:450]}" for q in history[-MAX_HISTORY_FOR_PROMPT:]) or "(No prior questions available.)"
    current_block = "\n".join(f"- {q[:450]}" for q in already_generated[-50:]) or "(No questions in this request yet.)"

    if mode == "ai":
        research_block = """ You MUST use Google Search grounding before finalizing factual claims. Prefer official/primary sources. Cross-check important facts when appropriate. Do not rely on unsupported memory for current or legal/official facts. """
    else:
        research_block = """ The supplied source is the PRIMARY basis. Use only the supplied material for facts unless an additional verification is genuinely necessary. Do not replace it with unrelated information. Do not copy any source MCQ. """

    source_block = ""
    if source_text:
        source_block = (
            "\nPRIMARY SOURCE MATERIAL\n"
            "---------------- START ----------------\n"
            f"{source_text[:MAX_SOURCE_TEXT]}\n"
            "---------------- END ----------------\n"
        )

    return f""" Create up to {count} ORIGINAL MCQs. TOPIC: {topic} REQUESTED LANGUAGE: {language} {research_block} PREVIOUS ECA QUESTIONS - DO NOT REPEAT OR CLOSELY PARAPHRASE: {history_block} QUESTIONS ALREADY CREATED IN THIS REQUEST - DO NOT REPEAT: {current_block} DIVERSITY: Use different meaningful subtopics/angles. Do not make a list of the same factual pattern. If the topic is narrow, return fewer questions. STRICT OUTPUT: Exactly four options. Exactly one correct index (0-3). No ambiguity. Every source field must describe the real factual basis. {source_block} Return only a single valid JSON object matching the supplied schema. Do not use Markdown code fences or add commentary before or after the JSON. """


# ============================================================
# Gemini calls
# ============================================================


def _generate_with_client( client: genai.Client, model_name: str, prompt: str, use_search: bool, source_files: Optional[list[str]] = None, ) -> dict[str, Any]:
    tools: list[Any] = []
    if use_search:
        tools.append(types.Tool(google_search=types.GoogleSearch()))

    config_kwargs: dict[str, Any] = {
        "system_instruction": SYSTEM_INSTRUCTION,
        "temperature": 0.25,
        "max_output_tokens": 12000,
        "tools": tools or None,
    }

    # Source mode can use Gemini structured output directly. AI/search mode
    # intentionally uses plain text JSON: built-in Google Search grounding
    # must not be combined with legacy structured-output enforcement on
    # Gemini 2.5. The prompt still requires one exact JSON object, and the
    # parser below accepts minor Markdown wrapping without weakening quiz
    # validation.
    if not use_search:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = QUESTION_SCHEMA

    config = types.GenerateContentConfig(**config_kwargs)

    contents: list[Any] = [prompt]
    for path in source_files or []:
        uploaded = client.files.upload(file=path)
        contents.append(uploaded)

    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=config,
    )

    # With built-in Google Search, the response can contain tool/executable
    # parts before the final text part. Accessing response.text directly can
    # therefore raise ValueError even though a valid text answer is present.
    # Collect all text parts explicitly instead.
    text_parts: list[str] = []
    try:
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                part_text = getattr(part, "text", None)
                if part_text:
                    text_parts.append(str(part_text))
    except Exception:
        logger.debug("Could not read Gemini candidate text parts.", exc_info=True)

    if not text_parts:
        # Safe fallback for responses that expose a direct text property.
        try:
            direct_text = getattr(response, "text", None)
        except Exception:
            direct_text = None
        if direct_text:
            text_parts.append(str(direct_text))

    text = "\n".join(text_parts).strip()
    if not text:
        raise RuntimeError("Gemini returned no text output after Google Search/tool execution.")

    def parse_json_response(raw: str) -> dict[str, Any]:
        raw = raw.strip()
        candidates = [raw]

        fenced = re.search(
            r"```(?:json)?\s*(.*?)\s*```",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fenced:
            candidates.insert(0, fenced.group(1).strip())

        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidates.append(raw[start:end + 1])

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        raise RuntimeError("Gemini returned a response that could not be parsed as JSON.")

    data = parse_json_response(text)

    # AI/search mode may return the factual source evidence in grounding
    # metadata rather than repeating it inside every JSON item. Convert a
    # defensible subset of that metadata into a compact source label so the
    # existing source requirement remains intact. No source URLs are invented.
    if use_search:
        grounding_titles: list[str] = []
        try:
            for candidate in getattr(response, "candidates", []) or []:
                metadata = getattr(candidate, "grounding_metadata", None)
                for chunk in getattr(metadata, "grounding_chunks", []) or []:
                    web_data = getattr(chunk, "web", None)
                    title = getattr(web_data, "title", None) if web_data else None
                    uri = getattr(web_data, "uri", None) if web_data else None
                    label = title or uri
                    if label and label not in grounding_titles:
                        grounding_titles.append(str(label))
        except Exception:
            logger.debug("Could not extract Gemini grounding metadata.", exc_info=True)

        fallback_source = "Google Search grounding"
        if grounding_titles:
            fallback_source = "Google Search grounding: " + "; ".join(grounding_titles[:3])

        items = data.get("questions")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    if not str(item.get("source", "")).strip():
                        item["source"] = fallback_source
                    if not str(item.get("topic", "")).strip():
                        item["topic"] = "AI-generated topic"

    return data


async def call_ai( prompt: str, use_search: bool, source_files: Optional[list[str]] = None, ) -> dict[str, Any]:
    remaining = cooldown_seconds_remaining()
    if remaining > 0:
        raise AIQuotaError(f"AI is temporarily cooling down after a quota failure. Retry in about {remaining}s.")

    models = []
    for name in [PRIMARY_MODEL] + FALLBACK_MODELS:
        if name and name not in models:
            models.append(name)

    last_errors: list[str] = []
    attempts = 0
    max_attempts = max(1, AI_MAX_CALLS_PER_REQUEST)

    for key_index, client in enumerate(GEMINI_CLIENTS):
        for model_name in models:
            attempts += 1
            if attempts > max_attempts:
                break
            try:
                result = await asyncio.to_thread(
                    _generate_with_client,
                    client,
                    model_name,
                    prompt,
                    use_search,
                    source_files,
                )
                logger.info("Gemini success key=%s model=%s", key_index + 1, model_name)
                return result
            except Exception as exc:
                category = classify_ai_error(exc)
                last_errors.append(f"key={key_index + 1}, model={model_name}, type={category}, error={str(exc)[:240]}")
                logger.warning(
                    "Gemini error key=%s model=%s type=%s error=%s",
                    key_index + 1,
                    model_name,
                    category,
                    str(exc)[:500],
                )

                if category == "transient":
                    await asyncio.sleep(AI_RETRY_DELAY)
                    try:
                        result = await asyncio.to_thread(
                            _generate_with_client,
                            client,
                            model_name,
                            prompt,
                            use_search,
                            source_files,
                        )
                        logger.info("Gemini retry success key=%s model=%s", key_index + 1, model_name)
                        return result
                    except Exception as retry_exc:
                        last_errors.append(
                            f"key={key_index + 1}, model={model_name}, retry_type={classify_ai_error(retry_exc)}, error={str(retry_exc)[:240]}"
                        )
                        logger.warning("Gemini retry failed: %s", str(retry_exc)[:500])
                elif category in ("quota", "permanent"):
                    continue
                else:
                    await asyncio.sleep(1)

        if attempts > max_attempts:
            break

    quota_seen = any("type=quota" in x or "retry_type=quota" in x for x in last_errors)
    if quota_seen:
        set_ai_cooldown(45)
        raise AIQuotaError(
            "Gemini quota/rate limit was exhausted across the configured models/keys."
        )
    raise AIPermanentError("AI generation failed. Details: " + "; ".join(last_errors[-8:]))


# ============================================================
# Validation / generation
# ============================================================


def valid_question(item: dict[str, Any]) -> bool:
    try:
        question = repair_mojibake(item.get("question", "")).strip()
        options = [repair_mojibake(x).strip() for x in item.get("options", [])]
        correct_index = int(item.get("correct_index"))
        explanation = repair_mojibake(item.get("explanation", "")).strip()
        source = repair_mojibake(item.get("source", "")).strip()
        topic = repair_mojibake(item.get("topic", "")).strip()

        if not question or len(question) > 300:
            return False
        if len(options) != 4:
            return False
        if any(not x or len(x) > 100 for x in options):
            return False
        if len({normalize(x) for x in options}) != 4:
            return False
        if correct_index not in (0, 1, 2, 3):
            return False
        if not explanation or len(explanation) > 200:
            return False
        if not source or not topic:
            return False
        item["question"] = question
        item["options"] = options
        item["explanation"] = explanation
        item["source"] = source
        item["topic"] = topic
        return True
    except Exception:
        return False


def post_validate( candidates: list[dict[str, Any]], accepted: Optional[list[dict[str, Any]]] = None, ) -> list[dict[str, Any]]:
    accepted = list(accepted or [])
    subtopic_counts: dict[str, int] = {}

    for old in accepted:
        key = normalize(str(old.get("topic", "")))
        subtopic_counts[key] = subtopic_counts.get(key, 0) + 1

    valid: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict) or not valid_question(item):
            continue
        question = repair_mojibake(item["question"]).strip()

        if exact_duplicate_exists(question) or similar_previous_question(question):
            continue
        if any(question_similarity(question, str(x["question"])) >= 0.84 for x in accepted + valid):
            continue

        subtopic = normalize(str(item.get("topic", "")))
        if subtopic_counts.get(subtopic, 0) >= 2:
            continue
        subtopic_counts[subtopic] = subtopic_counts.get(subtopic, 0) + 1
        valid.append(item)

    return valid


async def make_ai_contents( topic: str, count: int, language: str, mode: str, source_text: str, source_files: Optional[list[str]], history: list[str], already_generated: list[str], ) -> tuple[str, bool, list[str]]:
    prompt = build_prompt(
        topic=topic,
        count=count,
        language=language,
        mode=mode,
        source_text=source_text,
        history=history,
        already_generated=already_generated,
    )
    return prompt, mode == "ai", list(source_files or [])


async def generate_questions( topic: str, count: int, language: str, mode: str, source_text: str = "", source_files: Optional[list[str]] = None, progress_callback: Optional[Any] = None, ) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Return (questions, terminal_error_message). We deliberately return valid partial output when the provider quota is hit, rather than fabricating or forcing invalid questions. """
    generated: list[dict[str, Any]] = []
    source_files = list(source_files or [])

    try:
        history = recent_history()
        batch_size = min(20, count)
        calls = 0
        while len(generated) < count and calls < AI_MAX_CALLS_PER_REQUEST:
            remaining = count - len(generated)
            requested = min(batch_size, remaining)
            calls += 1

            if progress_callback:
                await progress_callback(f"{len(generated)}/{count} valid questions are ready...")

            prompt, use_search, source_files_for_call = await make_ai_contents(
                topic=topic,
                count=requested,
                language=language,
                mode=mode,
                source_text=source_text,
                source_files=source_files,
                history=history,
                already_generated=[x["question"] for x in generated],
            )

            try:
                result = await call_ai(prompt, use_search, source_files_for_call)
            except AIQuotaError as exc:
                logger.warning("Quota hit after %s calls: %s", calls, exc)
                if generated:
                    return generated[:count], str(exc)
                return [], str(exc)
            except Exception as exc:
                logger.exception("AI generation call failed.")
                if generated:
                    return generated[:count], str(exc)
                return [], str(exc)

            candidates = result.get("questions", [])
            if not isinstance(candidates, list):
                candidates = []

            validated = post_validate(candidates, generated)
            generated.extend(validated[:remaining])
            history.extend([str(x["question"]) for x in validated])

            # After the first successful batch, smaller second batches allow the
            # validator to fill gaps without issuing huge outputs.
            batch_size = min(12, max(4, remaining))
            if not validated:
                # No valid new questions from this call. One more call can try a
                # stronger prompt, but never spin indefinitely.
                batch_size = min(8, remaining)

            if progress_callback and generated:
                await progress_callback(f" {len(generated)}/{count} valid questions are ready...")

            if not candidates:
                break

        return generated[:count], None
    finally:
        for path in source_files:
            cleanup_file(path)


# ============================================================
# Conversation state helpers
# ============================================================

STATE_MODE = 0
STATE_SOURCE_TYPE = 1
STATE_SOURCE_CONTENT = 2
STATE_TOPIC = 3
STATE_COUNT = 4
STATE_LANGUAGE = 5
STATE_START_LOCATION = 6
STATE_TIME = 7
STATE_GROUP_START_TIME = 8


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not is_admin(update.effective_user.id):
            if update.effective_message:
                await update.effective_message.reply_text(
                    " Quiz creation is available only to the Owner/authorized Admins."
                )
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ============================================================
# /start and workflow
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return ConversationHandler.END

    if not is_admin(user.id):
        await message.reply_text(
            "ECA QUIZ MAKER\n\n"
            "Quiz creation is available only to the Owner/authorized Admins."
        )
        return ConversationHandler.END

    args = context.args or []
    if args and args[0].startswith("quiz_"):
        quiz_id = args[0][5:]
        with SessionLocal() as session:
            quiz = session.get(Quiz, quiz_id)
        if not quiz:
            await message.reply_text("Error: This quiz link is invalid or the prepared quiz is no longer available.")
            return ConversationHandler.END

        context.user_data["prepared_quiz_id"] = quiz_id
        context.user_data["topic"] = quiz.title
        context.user_data["question_count"] = quiz.question_count
        context.user_data["language"] = quiz.language

        # Deep-link into an actual group: ask time there, not in private chat.
        if message.chat.type in ("group", "supergroup"):
            context.user_data["run_mode"] = "group"
            context.user_data["target_chat_id"] = message.chat.id
            await message.reply_text(
                "QUIZ READY\n\n"
                f" Topic: {quiz.title}\n"
                f" Questions: {quiz.question_count}\n"
                f" Language: {quiz.language}\n\n"
                "Time: Choose the time allowed for each question:",
                reply_markup=ReplyKeyboardMarkup(TIME_BUTTONS, resize_keyboard=True, one_time_keyboard=True),
            )
            return STATE_GROUP_START_TIME

        await message.reply_text(
            "QUIZ READY\n\n"
            f" Topic: {quiz.title}\n"
            f" Questions: {quiz.question_count}\n"
            f" Language: {quiz.language}\n\n"
            "The quiz is prepared but not published yet. Choose where to start it:",
            reply_markup=ReplyKeyboardMarkup(
                [["Personally", "Group"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return STATE_START_LOCATION

    await message.reply_text(
        "ETERNAL CIVIL ACADEMY\n"
        "QUIZ MAKER\n\n"
        "What would you like to do?",
        reply_markup=ReplyKeyboardMarkup(
            MAIN_BUTTONS,
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    return STATE_MODE


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    choice = update.effective_message.text.strip()

    if choice == "AI Generate Questions":
        context.user_data["mode"] = "ai"
        await update.effective_message.reply_text(
            "Enter the topic.\n\n"
            "The AI will find authoritative sources, verify facts, and create original questions."
        )
        return STATE_TOPIC

    if choice == "I Will Provide Source":
        context.user_data["mode"] = "source"
        await update.effective_message.reply_text(
            "Choose the source type:",
            reply_markup=ReplyKeyboardMarkup(
                SOURCE_TYPE_BUTTONS,
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return STATE_SOURCE_TYPE

    await update.effective_message.reply_text("Please choose one of the available options.")
    return STATE_MODE


async def choose_source_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    choice = update.effective_message.text.strip()
    mapping = {
        "PDF": "pdf",
        "Photo": "photo",
        "Text": "text",
        "Telegram Poll": "poll",
        "URL": "url",
    }
    source_type = mapping.get(choice)
    if not source_type:
        await update.effective_message.reply_text("Please choose a source type.")
        return STATE_SOURCE_TYPE

    context.user_data["source_type"] = source_type
    if source_type == "text":
        await update.effective_message.reply_text("Send the source text now.")
    elif source_type == "url":
        await update.effective_message.reply_text("Send an accessible webpage URL now.")
    elif source_type == "poll":
        await update.effective_message.reply_text(
            "Forward or send Telegram Polls here. You can send more than one; press Source Ready when finished.",
            reply_markup=ReplyKeyboardMarkup([["Source Ready"]], resize_keyboard=True, one_time_keyboard=False),
        )
    elif source_type == "pdf":
        await update.effective_message.reply_text(
            "Send the PDF files here. You can send more than one; press Source Ready when finished.",
            reply_markup=ReplyKeyboardMarkup([["Source Ready"]], resize_keyboard=True, one_time_keyboard=False),
        )
    else:
        await update.effective_message.reply_text(
            "Send clear page photos here. You can send more than one; press Source Ready when finished.",
            reply_markup=ReplyKeyboardMarkup([["Source Ready"]], resize_keyboard=True, one_time_keyboard=False),
        )
    return STATE_SOURCE_CONTENT


async def receive_source_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.effective_message.text.strip()
    source_type = context.user_data.get("source_type")

    if source_type in ("pdf", "photo", "poll"):
        if text == "Source Ready":
            if source_type in ("pdf", "photo") and not context.user_data.get("source_files"):
                await update.effective_message.reply_text("Send at least one file first.")
                return STATE_SOURCE_CONTENT
            if source_type == "poll" and not context.user_data.get("source_text"):
                await update.effective_message.reply_text("Send at least one Telegram Poll first.")
                return STATE_SOURCE_CONTENT
            await update.effective_message.reply_text("Now enter the topic.", reply_markup=ReplyKeyboardRemove())
            return STATE_TOPIC
        await update.effective_message.reply_text("Keep sending source files/polls. Press Source Ready when finished.")
        return STATE_SOURCE_CONTENT

    if source_type == "url":
        if not re.match(r"^https?://", text, flags=re.I):
            await update.effective_message.reply_text("Send a valid http/https URL.")
            return STATE_SOURCE_CONTENT
        await update.effective_message.reply_text(" Reading URL content...")
        try:
            content = await asyncio.to_thread(fetch_url_text, text)
        except Exception as exc:
            logger.exception("URL fetch failed.")
            await update.effective_message.reply_text(f"The URL could not be read.\n\nReason: {str(exc)[:500]}")
            return STATE_SOURCE_CONTENT
        context.user_data["source_text"] = content
        context.user_data["source_summary"] = text
        await update.effective_message.reply_text("Now enter the topic.", reply_markup=ReplyKeyboardRemove())
        return STATE_TOPIC

    # plain text source
    if len(text) < 20:
        await update.effective_message.reply_text("The source is too short. Please provide more source material.")
        return STATE_SOURCE_CONTENT
    context.user_data["source_text"] = text[:MAX_SOURCE_TEXT]
    context.user_data["source_summary"] = "User-supplied text"
    await update.effective_message.reply_text("Now enter the topic.", reply_markup=ReplyKeyboardRemove())
    return STATE_TOPIC


async def receive_source_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    source_type = context.user_data.get("source_type")

    if source_type == "poll" and update.effective_message.poll:
        poll: Poll = update.effective_message.poll
        options = [x.text for x in poll.options]
        correct_index = getattr(poll, "correct_option_id", None)
        explanation = getattr(poll, "explanation", None)
        parts = [f"Question: {poll.question}", "", "Options:"]
        parts.extend(f"{chr(65+i)}. {opt}" for i, opt in enumerate(options))
        if correct_index is not None:
            parts.append(f"Available correct option: {chr(65 + correct_index)}")
        if explanation:
            parts.append(f"Explanation: {explanation}")
        old = context.user_data.get("source_text", "")
        joined = (old + "\n\n--- Telegram Poll Source ---\n" + "\n".join(parts)).strip()
        context.user_data["source_text"] = joined[:MAX_SOURCE_TEXT]
        context.user_data["source_summary"] = "One or more Telegram Poll sources"
        await update.effective_message.reply_text(
            "Poll source received.\n\nYou can send more Polls. Press Source Ready when finished.",
            reply_markup=ReplyKeyboardMarkup([["Source Ready"]], resize_keyboard=True, one_time_keyboard=False),
        )
        return STATE_SOURCE_CONTENT

    if source_type == "pdf":
        if not update.effective_message.document or (Path(update.effective_message.document.file_name or "").suffix.lower() != ".pdf"):
            await update.effective_message.reply_text("PDF mode accepts PDF files only.")
            return STATE_SOURCE_CONTENT
    elif source_type == "photo":
        if not update.effective_message.photo:
            await update.effective_message.reply_text("Photo mode accepts image files only.")
            return STATE_SOURCE_CONTENT
    else:
        await update.effective_message.reply_text("Send the correct source type.")
        return STATE_SOURCE_CONTENT

    try:
        path, suffix = await download_message_file(update, context)
    except Exception:
        logger.exception("File download failed.")
        await update.effective_message.reply_text("The file could not be received.")
        return STATE_SOURCE_CONTENT

    paths = context.user_data.setdefault("source_files", [])
    paths.append(path)
    context.user_data["source_summary"] = f"{len(paths)} {source_type} source file(s)"
    await update.effective_message.reply_text(
        f"Received {len(paths)} source file(s).\n\nYou can send more pages/files. Press Source Ready when finished.",
        reply_markup=ReplyKeyboardMarkup([["Source Ready"]], resize_keyboard=True, one_time_keyboard=False),
    )
    return STATE_SOURCE_CONTENT


async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    topic = update.effective_message.text.strip()
    if len(topic) < 2:
        await update.effective_message.reply_text("Error: Please enter a valid topic.")
        return STATE_TOPIC
    context.user_data["topic"] = topic
    await update.effective_message.reply_text("How many questions do you need?\n\nEnter a number from 1 to 100.")
    return STATE_COUNT


async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    try:
        count = int(update.effective_message.text.strip())
    except ValueError:
        await update.effective_message.reply_text("Enter a number only.")
        return STATE_COUNT
    if not 1 <= count <= MAX_QUESTIONS:
        await update.effective_message.reply_text(f"Error: The number must be between 1 and {MAX_QUESTIONS}.")
        return STATE_COUNT
    context.user_data["question_count"] = count
    await update.effective_message.reply_text(
        "Choose quiz language:",
        reply_markup=ReplyKeyboardMarkup(LANGUAGE_BUTTONS, resize_keyboard=True, one_time_keyboard=True),
    )
    return STATE_LANGUAGE


async def receive_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    language = LANGUAGE_LABELS.get(update.effective_message.text.strip())
    if not language:
        await update.effective_message.reply_text("Choose Hindi, English, or Bilingual.")
        return STATE_LANGUAGE
    context.user_data["language"] = language
    return await prepare_quiz(update, context)


# ============================================================
# Prepare quiz, but do not publish questions
# ============================================================

async def prepare_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message = update.effective_message
    topic = context.user_data.get("topic", "")
    count = int(context.user_data.get("question_count", 0))
    language = context.user_data.get("language", "")
    mode = context.user_data.get("mode", "ai")
    source_text = context.user_data.get("source_text", "")
    source_files = list(context.user_data.get("source_files", []))

    status = await message.reply_text(
        "Preparing the quiz...\n"
        "Questions will not be published yet.\n\n"
        "Running source verification, originality, and duplicate checks..."
    )

    async def progress(text: str) -> None:
        try:
            await status.edit_text(text)
        except Exception:
            pass

    try:
        questions, terminal_error = await generate_questions(
            topic=topic,
            count=count,
            language=language,
            mode=mode,
            source_text=source_text,
            source_files=source_files,
            progress_callback=progress,
        )

        if not questions:
            if terminal_error and "quota" in terminal_error.lower():
                await status.edit_text(
                    "Error: Gemini quota is currently unavailable.\n\n"
                    "All configured Gemini models/keys returned a quota/rate-limit error.\n"
                    "The bot did not continue with fake or unchecked questions."
                )
            else:
                detail = terminal_error or "The AI response was empty or failed the validation checks."
                detail = repair_mojibake(detail)[:900]
                await status.edit_text(
                    "Error: No valid question could be prepared.\n\n"
                    "The quality/verification checks did not pass, so the quiz was not published.\n\n"
                    f"Reason: {detail}"
                )
            return ConversationHandler.END

        quiz_id = f"eca-{uuid.uuid4().hex[:24]}"
        source_summary = str(context.user_data.get("source_summary", ""))[:5000]
        with SessionLocal() as session:
            session.add(
                Quiz(
                    id=quiz_id,
                    created_by=user.id,
                    title=topic,
                    question_count=len(questions),
                    language=language,
                    source_mode=mode,
                    source_summary=source_summary,
                )
            )
            for idx, item in enumerate(questions, 1):
                session.add(
                    QuizQuestion(
                        quiz_id=quiz_id,
                        question_no=idx,
                        question_text=repair_mojibake(item["question"]),
                        options_json=json.dumps([repair_mojibake(x) for x in item["options"]], ensure_ascii=False),
                        correct_index=int(item["correct_index"]),
                        explanation=repair_mojibake(item["explanation"]),
                        source=repair_mojibake(item["source"]),
                    )
                )
            session.commit()

        for item in questions:
            try:
                save_history(item)
            except Exception:
                logger.exception("Question history save failed for a generated question.")

        context.user_data["prepared_quiz_id"] = quiz_id

        bot = await context.bot.get_me()
        personal_link = f"https://t.me/{bot.username}?start=quiz_{quiz_id}"

        partial = ""
        if len(questions) < count:
            partial = (
                f"\n\n Requested: {count}\n"
                f" Verified/valid: {len(questions)}\n"
                "Fewer questions were kept because quality/verification was not bypassed."
            )
            if terminal_error and "quota" in terminal_error.lower():
                partial += "\n\nGemini quota stopped further generation."

        await status.edit_text(
            "QUIZ READY\n\n"
            f" Topic: {topic}\n"
            f" Questions: {len(questions)}\n"
            f" Language: {language}"
            "Prepared Quiz Link:\n"
            "Questions have not been published as a batch.\n"
            "Choose the start location first, then the time per question.\n\n"
            f"Prepared Quiz Link:\n{personal_link}"
        )

        await message.reply_text(
            "Where should the quiz start?",
            reply_markup=ReplyKeyboardMarkup(
                [["Personally", "Group"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )
        return STATE_START_LOCATION
    except Exception:
        logger.exception("Quiz preparation failed.")
        await status.edit_text("An unexpected error occurred while preparing the quiz. Check the Render logs for technical details.")
        return ConversationHandler.END
    finally:
        # generate_questions() itself cleans the source file.
        context.user_data.pop("source_files", None)


# ============================================================
# Start location / timer
# ============================================================

async def choose_start_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    choice = update.effective_message.text.strip()
    quiz_id = context.user_data.get("prepared_quiz_id")
    if not quiz_id:
        await update.effective_message.reply_text("Prepared quiz not found. Start again with /start.")
        return ConversationHandler.END

    if choice == "Personally":
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text("Personal mode must be started in the bot's private chat.")
            return STATE_START_LOCATION
        context.user_data["run_mode"] = "personal"
        context.user_data["target_chat_id"] = update.effective_chat.id

        await update.effective_message.reply_text(
            "Choose the time allowed for each question.\n\n"
            "The next question will start automatically when the time ends.",
            reply_markup=ReplyKeyboardMarkup(TIME_BUTTONS, resize_keyboard=True, one_time_keyboard=True),
        )
        return STATE_TIME

    if choice == "Group":
        if update.effective_chat.type in ("group", "supergroup"):
            context.user_data["run_mode"] = "group"
            context.user_data["target_chat_id"] = update.effective_chat.id
            await update.effective_message.reply_text(
                "Choose the time allowed for each question.",
                reply_markup=ReplyKeyboardMarkup(TIME_BUTTONS, resize_keyboard=True, one_time_keyboard=True),
            )
            return STATE_TIME

        bot = await context.bot.get_me()
        group_link = f"https://t.me/{bot.username}?startgroup=quiz_{quiz_id}"
        await update.effective_message.reply_text(
            "Group mode selected.\n\n"
            "Add the bot to the group where you want to run the quiz and use the link below.\n\n"
            f"Group Start Link:\n{group_link}\n\n"
            "When the quiz starts in the group, the timer will be selected there.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    await update.effective_message.reply_text("Please choose Personally or Group.")
    return STATE_START_LOCATION


async def choose_question_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    seconds = TIME_OPTIONS.get(update.effective_message.text.strip())
    if not seconds:
        await update.effective_message.reply_text("Choose 15 seconds, 25 seconds, 30 seconds, or 1 minute.")
        return STATE_TIME

    return await start_run_after_timer(update, context, seconds)


async def choose_group_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    seconds = TIME_OPTIONS.get(update.effective_message.text.strip())
    if not seconds:
        await update.effective_message.reply_text("Choose 15 seconds, 25 seconds, 30 seconds, or 1 minute.")
        return STATE_GROUP_START_TIME
    return await start_run_after_timer(update, context, seconds)


async def start_run_after_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, seconds: int) -> int:
    quiz_id = context.user_data.get("prepared_quiz_id")
    mode = context.user_data.get("run_mode")
    target_chat_id = context.user_data.get("target_chat_id")

    if not quiz_id or mode not in ("personal", "group") or not target_chat_id:
        await update.effective_message.reply_text("Quiz run details are incomplete. Start again with /start.")
        return ConversationHandler.END

    # Make sure quiz still exists and has questions.
    with SessionLocal() as session:
        quiz = session.get(Quiz, quiz_id)
        q_count = session.scalar(select(QuizQuestion.id).where(QuizQuestion.quiz_id == quiz_id).limit(1))
    if not quiz or q_count is None:
        await update.effective_message.reply_text("This prepared quiz is no longer available.")
        return ConversationHandler.END

    run_id = f"{quiz_id}-run-{uuid.uuid4().hex[:18]}"
    with SessionLocal() as session:
        session.add(
            QuizRun(
                id=run_id,
                quiz_id=quiz_id,
                target_chat_id=int(target_chat_id),
                started_by=update.effective_user.id,
                mode=mode,
                interval_seconds=seconds,
                current_question=0,
                active=True,
                started_at=datetime.now(timezone.utc),
                leaderboard_sent=False,
            )
        )
        session.commit()

    await update.effective_message.reply_text(
        "QUIZ STARTING\n\n"
        f"Time: Time per question: {seconds} seconds\n"
        "Info: The next question starts automatically when time expires.\n"
        "Error: Questions will not be sent all at once.",
        reply_markup=ReplyKeyboardRemove(),
    )

    context.user_data.clear()
    try:
        await send_next_question(context, run_id)
    except Exception:
        logger.exception("Initial quiz question failed for run=%s", run_id)
        await update.effective_message.reply_text("The quiz could not be started.")
    return ConversationHandler.END


# ============================================================
# One-question-at-a-time scheduler
# ============================================================

async def send_next_question(context: ContextTypes.DEFAULT_TYPE, run_id: str) -> None:
    with SessionLocal() as session:
        run = session.get(QuizRun, run_id)
        if not run or not run.active:
            return
        quiz = session.get(Quiz, run.quiz_id)
        if not quiz:
            run.active = False
            session.commit()
            return

        next_no = run.current_question + 1
        question = session.scalar(
            select(QuizQuestion)
            .where(QuizQuestion.quiz_id == run.quiz_id, QuizQuestion.question_no == next_no)
            .limit(1)
        )

        if not question:
            run.active = False
            run.completed_at = datetime.now(timezone.utc)
            session.commit()
            # Finish outside DB transaction.
            target_chat_id = run.target_chat_id
            completed_text = await asyncio.to_thread(build_leaderboard, run_id)
            await safe_send_message(
                context.bot,
                target_chat_id,
                "QUIZ COMPLETED\n\n"
                "All questions have been completed.\n\n"
                f"{completed_text}",
            )
            await send_answer_explanation_pdf(context, run_id, target_chat_id)
            return

        options = json.loads(question.options_json)
        if not isinstance(options, list) or len(options) != 4:
            run.active = False
            session.commit()
            await safe_send_message(
                context.bot,
                run.target_chat_id,
                f"Error: Question {next_no} could not be published because its stored options are invalid.",
            )
            return

        now = datetime.now(timezone.utc)
        closes = now + timedelta(seconds=run.interval_seconds)
        target = run.target_chat_id
        interval = run.interval_seconds
        correct_index = question.correct_index
        explanation = repair_mojibake(question.explanation)
        question_text = repair_mojibake(question.question_text)
        topic = repair_mojibake(quiz.title)

    try:
        message = await context.bot.send_poll(
            chat_id=target,
            question=question_text[:300],
            options=[str(x)[:100] for x in options],
            type="quiz",
            is_anonymous=False,
            allows_multiple_answers=False,
            allows_revoting=False,
            correct_option_id=correct_index,
            explanation=explanation[:200],
            description=(
                f" ECA QUIZ | {topic}\n"
                f"Question {next_no}\n\n"
                f"{SOURCE_FOOTER}"
            )[:1024],
            open_period=interval,
        )
    except (RetryAfter, TimedOut, NetworkError) as exc:
        logger.warning("Telegram transient send_poll failure run=%s: %s", run_id, exc)
        # Retry once after RetryAfter-provided delay if available.
        delay = float(getattr(exc, "retry_after", 2)) if isinstance(exc, RetryAfter) else 2.0
        await asyncio.sleep(min(delay, 10.0))
        message = await context.bot.send_poll(
            chat_id=target,
            question=question_text[:300],
            options=[str(x)[:100] for x in options],
            type="quiz",
            is_anonymous=False,
            allows_multiple_answers=False,
            allows_revoting=False,
            correct_option_id=correct_index,
            explanation=explanation[:200],
            description=(
                f" ECA QUIZ | {topic}\n"
                f"Question {next_no}\n\n"
                f"{SOURCE_FOOTER}"
            )[:1024],
            open_period=interval,
        )
    except (Forbidden, BadRequest) as exc:
        logger.exception("Telegram rejected poll for run=%s", run_id)
        with SessionLocal() as session:
            run = session.get(QuizRun, run_id)
            if run:
                run.active = False
                session.commit()
        await safe_send_message(
            context.bot,
            target,
            "Telegram rejected the quiz poll.\n\n"
            "Give the bot the required group permissions, or try Personal mode.",
        )
        return

    if not message.poll:
        raise RuntimeError("Telegram send_poll succeeded but returned no Poll object.")

    with SessionLocal() as session:
        run = session.get(QuizRun, run_id)
        if not run or not run.active:
            return
        session.add(
            QuizPoll(
                run_id=run_id,
                quiz_id=run.quiz_id,
                poll_id=message.poll.id,
                question_no=next_no,
                correct_index=correct_index,
                question_text=question_text,
                opened_at=now,
                closes_at=closes,
            )
        )
        run.current_question = next_no
        session.commit()

    if context.job_queue is not None:
        context.job_queue.run_once(
            send_next_question_job,
            when=interval + 0.5,
            data={"run_id": run_id},
            name=f"eca-next-{run_id}-{next_no}",
        )
    else:
        # Fallback so the quiz still advances if JobQueue is not installed.
        async def advance_after_delay() -> None:
            await asyncio.sleep(interval + 0.5)
            await send_next_question(context, run_id)

        asyncio.create_task(advance_after_delay())


async def send_next_question_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data if context.job else None
    if not data:
        return
    await send_next_question(context, str(data["run_id"]))


# ============================================================
# Poll answers / leaderboard
# ============================================================

async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = update.poll_answer
    if not answer or not answer.user:
        return
    # Empty option_ids means vote was retracted. Since revoting is disabled,
    # simply ignore it.
    if not answer.option_ids:
        return
    selected_index = int(answer.option_ids[0])

    with SessionLocal() as session:
        poll_row = session.scalar(select(QuizPoll).where(QuizPoll.poll_id == answer.poll_id).limit(1))
        if not poll_row:
            return
        now = datetime.now(timezone.utc)
        if now > poll_row.closes_at:
            return

        is_correct = selected_index == poll_row.correct_index
        user = answer.user
        user_name = user.full_name or user.username or str(user.id)

        existing = session.scalar(
            select(PollAnswer)
            .where(PollAnswer.poll_id == answer.poll_id, PollAnswer.user_id == user.id)
            .limit(1)
        )
        if existing:
            # Safety fallback if Telegram still sends an update after a client-side
            # retry. Keep first answer to preserve exam semantics.
            return

        session.add(
            PollAnswer(
                poll_id=answer.poll_id,
                run_id=poll_row.run_id,
                user_id=user.id,
                user_name=user_name,
                selected_index=selected_index,
                is_correct=is_correct,
                answered_at=now,
            )
        )
        session.commit()


def build_leaderboard(run_id: str) -> str:
    """Build the final leaderboard for a completed quiz run. Participant rule: - Only students who attempted at least one question are listed. - If more than 50 students attempted, only the first 50 sorted by correct answers are shown. Ranking rule requested by ECA: - Rank is based on number of correct answers. - Students with the same number of correct answers receive the same rank. - Raw marks are displayed separately as Correct - Wrong/3. """
    with SessionLocal() as session:
        run = session.get(QuizRun, run_id)
        if not run:
            return "Quiz run not found."
        quiz = session.get(Quiz, run.quiz_id)
        if not quiz:
            return "Quiz not found."

        answers = session.scalars(
            select(PollAnswer).where(PollAnswer.run_id == run_id)
        ).all()

    participant_map: dict[int, dict[str, Any]] = {}
    for ans in answers:
        item = participant_map.setdefault(
            ans.user_id,
            {"name": ans.user_name, "correct": 0, "wrong": 0},
        )
        item["name"] = ans.user_name
        if ans.is_correct:
            item["correct"] += 1
        else:
            item["wrong"] += 1

    rows: list[dict[str, Any]] = []
    total_questions = int(quiz.question_count)
    for user_id, item in participant_map.items():
        correct = int(item["correct"])
        wrong = int(item["wrong"])
        answered = correct + wrong
        unattempted = max(0, total_questions - answered)
        raw_marks = correct - (wrong / 3.0)
        rows.append(
            {
                "user_id": user_id,
                "name": repair_mojibake(item["name"]),
                "correct": correct,
                "wrong": wrong,
                "unattempted": unattempted,
                "raw_marks": raw_marks,
            }
        )

    # Primary order is correct answers; raw marks is a deterministic tie-breaker
    # within the same correct-answer count, but rank remains identical.
    rows.sort(
        key=lambda x: (-x["correct"], -x["raw_marks"], x["name"].lower(), x["user_id"])
    )

    previous_correct: Optional[int] = None
    rank = 0
    for index, row in enumerate(rows, start=1):
        correct_key = int(row["correct"])
        if previous_correct is None or correct_key != previous_correct:
            rank = index
        row["rank"] = rank
        previous_correct = correct_key

    lines = [
        "ECA LIVE QUIZ - LEADERBOARD",
        "",
        f"Quiz: {repair_mojibake(quiz.title)}",
        "",
    ]

    if not rows:
        lines.append("No student attempted any question.")
        return "\n".join(lines)

    for row in rows[:50]:
        # Requested compact format: right Error:wrong â­•unattempted [Raw marks-X] rank
        raw = row["raw_marks"]
        raw_text = f"{raw:.2f}".rstrip("0").rstrip(".")
        lines.append(
            f"{row['name'][:45]} ({row['user_id']}) - "
            f"Correct:{row['correct']} Wrong:{row['wrong']} Unattempted:{row['unattempted']} "
            f"[Raw marks-{raw_text}] {row['rank']}"
        )

    return "\n".join(lines)


async def safe_send_message(bot: Any, chat_id: int, text: str) -> None:
    try:
        text = repair_mojibake(text)
        await bot.send_message(chat_id=chat_id, text=text[:4096])
    except Exception:
        logger.exception("Could not send message to chat=%s", chat_id)


# ============================================================
# Recovery after Render restart
# ============================================================

async def recover_active_runs(application: Application) -> None:
    """Recover persisted active runs after a process restart. We use the DB as the source of truth. If the last poll's scheduled job was lost during restart, schedule the next poll from the stored closes_at. """
    with SessionLocal() as session:
        active_runs = session.scalars(
            select(QuizRun).where(QuizRun.active == True)  # noqa: E712
        ).all()

    now = datetime.now(timezone.utc)
    recovered = 0
    for run in active_runs:
        with SessionLocal() as session:
            last_poll = session.scalar(
                select(QuizPoll)
                .where(QuizPoll.run_id == run.id)
                .order_by(QuizPoll.question_no.desc())
                .limit(1)
            )

        if not last_poll:
            delay = 0.5
        else:
            delay = max(0.5, (last_poll.closes_at - now).total_seconds())

        application.job_queue.run_once(
            send_next_question_job,
            when=delay,
            data={"run_id": run.id},
            name=f"eca-recover-{run.id}",
        )
        recovered += 1
    logger.info("Recovered %s active quiz runs after startup.", recovered)


# ============================================================
# Cancel/help/error/startup
# ============================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for path in list(context.user_data.get("source_files", [])):
        cleanup_file(path)
    # Backward-compatible cleanup if an old state left a single source_file.
    cleanup_file(context.user_data.get("source_file"))
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Operation cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "ECA QUIZ MAKER\n\n"
        "/start - Quiz Maker\n"
        "/cancel - Cancel current operation\n"
        "/whoami - Show Telegram User ID\n\n"
        "OWNER COMMANDS\n"
        "/addadmin - reply to a user message\n"
        "/removeadmin - reply to an admin message\n"
        "/admins - Show admin list\n\n"
        "MAIN MODES\n"
        "AI Generate Questions\n"
        "I Will Provide Source"
    )


async def post_init(application: Application) -> None:
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook failed.")
    me = await application.bot.get_me()
    logger.info("Started as @%s", me.username)
    await recover_active_runs(application)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


# ============================================================
# Main
# ============================================================


def main() -> None:
    init_db()

    threading.Thread(
        target=start_health_server,
        name="eca-health-server",
        daemon=True,
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_mode)],
            STATE_SOURCE_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_source_type)],
            STATE_SOURCE_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_source_text),
                MessageHandler(filters.PHOTO | filters.Document.ALL | filters.POLL, receive_source_file),
            ],
            STATE_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
            STATE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_count)],
            STATE_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_language)],
            STATE_START_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_start_location)],
            STATE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_question_time)],
            STATE_GROUP_START_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_group_start_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conversation)
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("removeadmin", remove_admin))
    application.add_handler(CommandHandler("admins", list_admins))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(PollAnswerHandler(poll_answer_handler))
    application.add_error_handler(error_handler)

    logger.info("ECA Quiz Maker Bot is running...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
