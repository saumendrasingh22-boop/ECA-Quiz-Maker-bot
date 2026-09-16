# -*- coding: utf-8 -*-
"""
ECA QUIZ MAKER - Production Telegram Quiz Bot
Designed for Render + Telegram + Google Gemini
"""

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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
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
# Configuration & Environment Variables
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0") or "0")

raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()
GEMINI_API_KEYS: list[str] = []
if raw_keys:
    GEMINI_API_KEYS.extend([x.strip() for x in raw_keys.split(",") if x.strip()])
if os.getenv("GEMINI_API_KEY", "").strip():
    GEMINI_API_KEYS.insert(0, os.getenv("GEMINI_API_KEY", "").strip())
GEMINI_API_KEYS = list(dict.fromkeys(GEMINI_API_KEYS))

# Models updated to valid current Gemini API standard identifiers
PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
FALLBACK_MODELS = [
    x.strip()
    for x in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-1.5-flash,gemini-1.5-flash-8b",
    ).split(",")
    if x.strip()
]

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///eca_quiz_v3.db").strip()
PORT = int(os.getenv("PORT", "10000") or "10000")

MAX_QUESTIONS = 100
MAX_SOURCE_TEXT = 120_000
MAX_HISTORY_FOR_PROMPT = 250
MAX_HISTORY_FOR_SIMILARITY = 1200
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

TARGET_BUTTONS = [["Personal Chat", "Group Chat"]]

# Conversation States
(
    STATE_MAIN_MENU,
    STATE_ENTER_TOPIC,
    STATE_ENTER_COUNT,
    STATE_CHOOSE_LANGUAGE,
    STATE_CHOOSE_SOURCE_TYPE,
    STATE_ENTER_SOURCE,
    STATE_CHOOSE_TIME,
    STATE_CHOOSE_TARGET,
) = range(8)

# ============================================================
# Logging Setup
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
# Database Definition
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
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    current_question: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

class QuizPoll(Base):
    __tablename__ = "eca_v3_quiz_polls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)
    quiz_id: Mapped[str] = mapped_column(String(80), nullable=False)
    poll_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    question_no: Mapped[int] = mapped_column(Integer, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)

class PollAnswer(Base):
    __tablename__ = "eca_v3_poll_answers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_name: Mapped[str] = mapped_column(String(300), nullable=False)
    selected_index: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)

connect_args: dict[str, Any] = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db() -> None:
    Base.metadata.create_all(engine)

# ============================================================
# Text Cleaning & Normalization
# ============================================================

def repair_mojibake(value: Any) -> str:
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

def hash_question(question: str) -> str:
    return hashlib.sha256(normalize(question).encode("utf-8")).hexdigest()

# ============================================================
# Gemini API Execution Engine
# ============================================================

GEMINI_CLIENTS = [genai.Client(api_key=k) for k in GEMINI_API_KEYS]

def classify_ai_error(exc: Exception) -> tuple[str, str]:
    text = str(exc).lower()
    msg = str(exc)[:200]
    if any(x in text for x in ("resource_exhausted", "quota", "too many requests", "429")):
        return "quota", msg
    if any(x in text for x in ("503", "502", "504", "temporarily unavailable", "timeout")):
        return "transient", msg
    if any(x in text for x in ("401", "403", "api key", "permission denied", "unauthenticated")):
        return "permanent", msg
    return f"other ({type(exc).__name__})", msg

def generate_mcqs_with_gemini(
    prompt: str,
    system_instruction: str,
    model_name: str = PRIMARY_MODEL
) -> list[dict[str, Any]]:
    models_to_try = [model_name] + [m for m in FALLBACK_MODELS if m != model_name]
    error_logs = []

    for client_idx, client in enumerate(GEMINI_CLIENTS):
        for model in models_to_try:
            try:
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.3,
                    response_mime_type="application/json",
                )
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                if not response.text:
                    raise ValueError("Empty response received from Gemini API.")
                
                data = json.loads(response.text)
                if isinstance(data, dict) and "questions" in data:
                    data = data["questions"]
                
                if isinstance(data, list):
                    return data
                else:
                    raise ValueError("Output format was not a valid JSON array.")

            except Exception as exc:
                err_type, err_msg = classify_ai_error(exc)
                logger.error(f"Gemini Call Failed | Key #{client_idx+1} | Model: {model} | Error: {err_msg}")
                error_logs.append(f"key={client_idx+1}, model={model}, type={err_type}")
                
    raise RuntimeError("; ".join(error_logs))

# ============================================================
# PDF Answer & Explanation Booklet Generator
# ============================================================

TELEGRAM_URL = "https://t.me/EternalCivilAcademy"

def find_unicode_pdf_font() -> tuple[str, Optional[str]]:
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
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
        logger.exception("Font registration failed.")

def escape_pdf_text(value: Any) -> str:
    text = repair_mojibake(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")

def build_answer_explanation_pdf(run_id: str, output_path: str) -> str:
    with SessionLocal() as session:
        run = session.get(QuizRun, run_id)
        if not run:
            raise RuntimeError("Quiz run not found.")
        quiz = session.get(Quiz, run.quiz_id)
        questions = session.scalars(
            select(QuizQuestion).where(QuizQuestion.quiz_id == quiz.id).order_by(QuizQuestion.question_no)
        ).all()

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=22 * mm,
        bottomMargin=16 * mm,
    )

    styles = getSampleStyleSheet()
    brand = ParagraphStyle("ECABrand", fontName=PDF_FONT_BOLD, fontSize=16, leading=20, alignment=TA_CENTER)
    q_style = ParagraphStyle("Question", fontName=PDF_FONT_BOLD, fontSize=10.5, leading=15, spaceBefore=3 * mm)
    ans_style = ParagraphStyle("Answer", fontName=PDF_FONT_NAME, fontSize=9.5, leading=14)

    story = [
        Paragraph("ETERNAL CIVIL ACADEMY", brand),
        Spacer(1, 5 * mm),
        Paragraph(f"<b>QUIZ TITLE:</b> {escape_pdf_text(quiz.title)}", ans_style),
        Spacer(1, 4 * mm),
    ]

    for q in questions:
        story.append(Paragraph(f"<b>Q{q.question_no}.</b> {escape_pdf_text(q.question_text)}", q_style))
        opts = json.loads(q.options_json)
        for idx, opt in enumerate(opts):
            mark = " (Correct)" if idx == q.correct_index else ""
            story.append(Paragraph(f"{chr(65+idx)}. {escape_pdf_text(opt)}{mark}", ans_style))
        story.append(Paragraph(f"<b>Explanation:</b> {escape_pdf_text(q.explanation)}", ans_style))
        story.append(Spacer(1, 3 * mm))

    doc.build(story)
    return str(output)

# ============================================================
# Web Scraping Helper
# ============================================================

def fetch_url_text(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP/HTTPS URLs supported.")
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for s in soup(["script", "style", "nav", "footer"]):
        s.decompose()
    text = SPACE_RE.sub(" ", soup.get_text("\n", strip=True))
    if len(text) < 50:
        raise ValueError("Not enough text extracted from URL.")
    return text[:MAX_SOURCE_TEXT]

# ============================================================
# Authorization / Admin Helpers
# ============================================================

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_USER_ID

def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    with SessionLocal() as session:
        return session.get(Admin, user_id) is not None

# ============================================================
# Conversation Handlers Logic
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Unauthorized user.")
        return ConversationHandler.END

    context.user_data.clear()
    await update.effective_message.reply_text(
        "Welcome to ECA Quiz Maker Bot.\nChoose mode:",
        reply_markup=ReplyKeyboardMarkup(MAIN_BUTTONS, one_time_keyboard=True, resize_keyboard=True)
    )
    return STATE_MAIN_MENU

async def main_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text
    if text == "AI Generate Questions":
        context.user_data["mode"] = "AI"
        await update.effective_message.reply_text("Enter quiz topic:", reply_markup=ReplyKeyboardRemove())
        return STATE_ENTER_TOPIC
    elif text == "I Will Provide Source":
        context.user_data["mode"] = "SOURCE"
        await update.effective_message.reply_text(
            "Select source type:",
            reply_markup=ReplyKeyboardMarkup(SOURCE_TYPE_BUTTONS, one_time_keyboard=True, resize_keyboard=True)
        )
        return STATE_CHOOSE_SOURCE_TYPE
    return STATE_MAIN_MENU

async def source_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["source_type"] = update.effective_message.text
    await update.effective_message.reply_text("Now enter the topic for this source:", reply_markup=ReplyKeyboardRemove())
    return STATE_ENTER_TOPIC

async def enter_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["topic"] = update.effective_message.text.strip()
    if context.user_data.get("mode") == "SOURCE":
        await update.effective_message.reply_text("Send source content (Text/URL/File):")
        return STATE_ENTER_SOURCE
    
    await update.effective_message.reply_text("How many questions do you need? (1-100):")
    return STATE_ENTER_COUNT

async def enter_source_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    source_type = context.user_data.get("source_type")
    
    try:
        if source_type == "URL" and msg.text:
            content = fetch_url_text(msg.text.strip())
        elif msg.text:
            content = msg.text.strip()
        else:
            content = "User attached document/photo source."
        
        context.user_data["source_content"] = content
        await update.effective_message.reply_text("Source Ready!\nHow many questions do you need? (1-100):")
        return STATE_ENTER_COUNT
    except Exception as e:
        await update.effective_message.reply_text(f"Source read error: {e}\nPlease try again.")
        return STATE_ENTER_SOURCE

async def enter_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        count = int(update.effective_message.text.strip())
        if not (1 <= count <= MAX_QUESTIONS):
            raise ValueError()
        context.user_data["count"] = count
    except ValueError:
        await update.effective_message.reply_text("Please enter a valid number between 1 and 100.")
        return STATE_ENTER_COUNT

    await update.effective_message.reply_text(
        "Choose quiz language:",
        reply_markup=ReplyKeyboardMarkup(LANGUAGE_BUTTONS, one_time_keyboard=True, resize_keyboard=True)
    )
    return STATE_CHOOSE_LANGUAGE

async def choose_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    lang = update.effective_message.text.strip()
    context.user_data["language"] = lang
    
    status_msg = await update.effective_message.reply_text(
        "Generating questions via Gemini AI... Please wait.",
        reply_markup=ReplyKeyboardRemove()
    )

    topic = context.user_data.get("topic")
    count = context.user_data.get("count")
    mode = context.user_data.get("mode")
    source_text = context.user_data.get("source_content", "")

    system_instruction = (
        "You are an expert exam setter. Generate multiple choice questions strictly in JSON array format. "
        "Each object must have: 'question' (string), 'options' (list of 4 strings), "
        "'correct_index' (integer 0-3), 'explanation' (string)."
    )

    if mode == "AI":
        prompt = f"Generate {count} MCQs on Topic: '{topic}' in Language: '{lang}'."
    else:
        prompt = f"Generate {count} MCQs on Topic: '{topic}' in Language: '{lang}' based on this Source:\n{source_text[:10000]}"

    try:
        questions_data = generate_mcqs_with_gemini(prompt, system_instruction)
        quiz_id = str(uuid.uuid4())[:8]

        with SessionLocal() as session:
            session.add(Quiz(
                id=quiz_id,
                created_by=update.effective_user.id,
                title=topic,
                question_count=len(questions_data),
                language=lang,
                source_mode=mode,
            ))
            for i, q in enumerate(questions_data, 1):
                session.add(QuizQuestion(
                    quiz_id=quiz_id,
                    question_no=i,
                    question_text=q.get("question", ""),
                    options_json=json.dumps(q.get("options", [])),
                    correct_index=int(q.get("correct_index", 0)),
                    explanation=q.get("explanation", ""),
                    source=SOURCE_FOOTER,
                ))
            session.commit()

        context.user_data["quiz_id"] = quiz_id
        await status_msg.edit_text(f"Successfully generated {len(questions_data)} questions!")
        
        await update.effective_message.reply_text(
            "Select per-question timer:",
            reply_markup=ReplyKeyboardMarkup(TIME_BUTTONS, one_time_keyboard=True, resize_keyboard=True)
        )
        return STATE_CHOOSE_TIME

    except Exception as exc:
        logger.error("Generation failed: %s", exc)
        await status_msg.edit_text(
            f"Error: No valid question could be prepared.\n\nReason: AI generation failed. Details: {exc}"
        )
        return ConversationHandler.END

async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.effective_message.text.strip()
    seconds = TIME_OPTIONS.get(time_str, 30)
    context.user_data["interval_seconds"] = seconds

    await update.effective_message.reply_text(
        "Select target mode:",
        reply_markup=ReplyKeyboardMarkup(TARGET_BUTTONS, one_time_keyboard=True, resize_keyboard=True)
    )
    return STATE_CHOOSE_TARGET

async def choose_target_and_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target = update.effective_message.text.strip()
    quiz_id = context.user_data.get("quiz_id")
    interval = context.user_data.get("interval_seconds", 30)
    
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    chat_id = update.effective_chat.id

    with SessionLocal() as session:
        session.add(QuizRun(
            id=run_id,
            quiz_id=quiz_id,
            target_chat_id=chat_id,
            started_by=update.effective_user.id,
            mode="personal" if target == "Personal Chat" else "group",
            interval_seconds=interval,
        ))
        session.commit()

    await update.effective_message.reply_text(
        f"Starting Quiz Run!\nQuiz ID: {quiz_id}\nInterval: {interval}s per question.",
        reply_markup=ReplyKeyboardRemove()
    )

    asyncio.create_task(execute_quiz_run(context.application, run_id))
    return ConversationHandler.END

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ============================================================
# Quiz Execution Loop & Poll Answer Handling
# ============================================================

async def execute_quiz_run(app: Application, run_id: str) -> None:
    with SessionLocal() as session:
        run = session.get(QuizRun, run_id)
        if not run:
            return
        questions = session.scalars(
            select(QuizQuestion).where(QuizQuestion.quiz_id == run.quiz_id).order_by(QuizQuestion.question_no)
        ).all()

    for q in questions:
        opts = json.loads(q.options_json)
        try:
            msg = await app.bot.send_poll(
                chat_id=run.target_chat_id,
                question=f"Q{q.question_no}. {q.question_text}",
                options=opts,
                type=Poll.QUIZ,
                correct_option_id=q.correct_index,
                explanation=q.explanation[:200] if q.explanation else None,
                is_closed=False,
                open_period=run.interval_seconds,
            )
            with SessionLocal() as session:
                session.add(QuizPoll(
                    run_id=run_id,
                    quiz_id=run.quiz_id,
                    poll_id=msg.poll.id,
                    question_no=q.question_no,
                    correct_index=q.correct_index,
                    question_text=q.question_text,
                ))
                session.commit()
        except Exception as e:
            logger.error("Failed to send poll Q%s: %s", q.question_no, e)

        await asyncio.sleep(run.interval_seconds + 2)

    pdf_path = f"outputs/{run_id}.pdf"
    try:
        build_answer_explanation_pdf(run_id, pdf_path)
        await app.bot.send_document(
            chat_id=run.target_chat_id,
            document=open(pdf_path, "rb"),
            caption="Quiz completed! Here is the Answer & Explanation Booklet."
        )
    except Exception as e:
        logger.error("Failed to build/send PDF: %s", e)

async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ans = update.poll_answer
    poll_id = ans.poll_id
    user = ans.user

    with SessionLocal() as session:
        poll = session.scalar(select(QuizPoll).where(QuizPoll.poll_id == poll_id))
        if not poll:
            return
        
        selected = ans.option_ids[0] if ans.option_ids else -1
        is_corr = (selected == poll.correct_index)
        
        session.add(PollAnswer(
            poll_id=poll_id,
            run_id=poll.run_id,
            user_id=user.id,
            user_name=user.full_name,
            selected_index=selected,
            is_correct=is_corr
        ))
        session.commit()

# ============================================================
# Health Server Component
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
    logger.info("Health server listening on port %s", PORT)
    server.serve_forever()

# ============================================================
# Main Application Entry Point
# ============================================================

def main() -> None:
    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_cmd)],
        states={
            STATE_MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_choice)],
            STATE_CHOOSE_SOURCE_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, source_type_choice)],
            STATE_ENTER_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_topic)],
            STATE_ENTER_SOURCE: [MessageHandler(filters.ALL & ~filters.COMMAND, enter_source_content)],
            STATE_ENTER_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_count)],
            STATE_CHOOSE_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_language)],
            STATE_CHOOSE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_time)],
            STATE_CHOOSE_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_target_and_run)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    application.add_handler(conv_handler)
    application.add_handler(PollAnswerHandler(poll_answer_handler))

    logger.info("Bot starting polling loop...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
