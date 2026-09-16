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
# Configuration
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

# Fixed standard Gemini model identifiers
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
AI_MAX_CALLS_PER_REQUEST = 8
AI_TRANSIENT_RETRY_COUNT = 1
AI_RETRY_DELAY = 2.0
SOURCE_FOOTER = "Source: @EternalCivilAcademy"

# ============================================================
# Logging
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

connect_args: dict[str, Any] = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db() -> None:
    Base.metadata.create_all(engine)

# ============================================================
# Gemini API Execution Helper with Detailed Error Logging
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
    """Calls Gemini API using google-genai SDK enforcing JSON output and detailed error tracking."""
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
                error_logs.append(f"key={client_idx+1}, model={model}, type={err_type}, detail={err_msg}")
                
    raise RuntimeError("; ".join(error_logs))

# ============================================================
# Health Server
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

if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()
    logger.info("ECA Quiz Bot script loaded successfully.")
