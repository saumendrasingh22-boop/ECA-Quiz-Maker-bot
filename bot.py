import os
import re
import json
import uuid
import sqlite3
import threading
import logging
import tempfile
from dataclasses import dataclass
from typing import List, Optional

from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types


# ============================================================
# ECA QUIZ MAKER BOT
# ============================================================
#
# FEATURES
# ------------------------------------------------------------
# OWNER
#   /addadmin  -> reply to a user's message
#   /removeadmin -> reply to a user's message
#   /admins
#   /whoami
#
# ADMIN
#   AI Automatic Quiz
#       -> My Source
#           -> Photo / PDF / Text
#       -> AI Find Source
#
# AI
#   Gemini API
#   Google Search grounding
#   Original MCQs
#   Duplicate prevention
#   ECA question history
#   Source verification instructions
#
# RENDER
#   Health server on 0.0.0.0:$PORT
#
# ============================================================


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_USER_ID = int(
    os.getenv("OWNER_USER_ID", "0").strip() or "0"
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# You can change this later from Render Environment Variables.
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash"
).strip()

PORT = int(
    os.getenv("PORT", "10000").strip() or "10000"
)

DB_FILE = os.getenv(
    "ECA_DB_FILE",
    "eca_quiz.db"
).strip()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("ECA-Quiz-Maker")


# ============================================================
# GEMINI CLIENT
# ============================================================

gemini_client = None

if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(
            api_key=GEMINI_API_KEY
        )
        logger.info("Gemini client initialized.")
    except Exception:
        logger.exception(
            "Could not initialize Gemini client."
        )


# ============================================================
# CONVERSATION STATES
# ============================================================

MODE = 0
SOURCE_MODE = 1
SOURCE_INPUT = 2
TOPIC = 3
QUESTION_COUNT = 4
LANGUAGE = 5


# ============================================================
# DATA CLASS
# ============================================================

@dataclass
class Question:
    question: str
    options: List[str]
    correct_index: int
    explanation: str
    source: str
    topic: str


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path in (
            "/",
            "/health",
            "/healthz",
        ):

            body = b"ECA Quiz Maker Bot is running"

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(body)

        else:

            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():

    try:

        server = HTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler,
        )

        logger.info(
            "Health server listening on port %s",
            PORT,
        )

        server.serve_forever()

    except Exception:
        logger.exception(
            "Health server stopped."
        )


# ============================================================
# DATABASE
# ============================================================

def db():

    return sqlite3.connect(
        DB_FILE,
        timeout=30,
    )


def init_db():

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS question_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            question TEXT NOT NULL,
            normalized_question TEXT NOT NULL,
            source TEXT,
            quiz_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


# ============================================================
# USER / ADMIN AUTH
# ============================================================

def is_owner(user_id: int) -> bool:

    return (
        OWNER_USER_ID != 0
        and user_id == OWNER_USER_ID
    )


def is_admin(user_id: int) -> bool:

    if is_owner(user_id):
        return True

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM admins WHERE user_id = ?",
        (user_id,),
    )

    result = cur.fetchone()

    conn.close()

    return result is not None


# ============================================================
# QUESTION NORMALIZATION
# ============================================================

def normalize_question(text: str) -> str:

    text = text.lower().strip()

    text = re.sub(
        r"[^\w\s]",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def question_exists(question: str) -> bool:

    normalized = normalize_question(question)

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT 1
        FROM question_history
        WHERE normalized_question = ?
        LIMIT 1
        """,
        (normalized,),
    )

    result = cur.fetchone()

    conn.close()

    return result is not None


def save_question_history(
    question: Question,
    quiz_id: str,
):

    normalized = normalize_question(
        question.question
    )

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO question_history
        (
            topic,
            question,
            normalized_question,
            source,
            quiz_id
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            question.topic,
            question.question,
            normalized,
            question.source,
            quiz_id,
        ),
    )

    conn.commit()
    conn.close()


def get_recent_questions(
    limit: int = 150
) -> List[str]:

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT question
        FROM question_history
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cur.fetchall()

    conn.close()

    return [
        row[0]
        for row in rows
    ]


# ============================================================
# OWNER COMMANDS
# ============================================================

async def whoami(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    await update.message.reply_text(
        "🆔 Your Telegram User ID:\n\n"
        f"{user.id}\n\n"
        f"Username: @{user.username}"
        if user.username
        else
        "🆔 Your Telegram User ID:\n\n"
        f"{user.id}"
    )


async def add_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not is_owner(user.id):

        await update.message.reply_text(
            "❌ केवल Bot Owner नया Admin authorize कर सकता है।"
        )

        return

    target_id = None
    target_name = None

    # Preferred method:
    # Owner replies to target user's message.
    if update.message.reply_to_message:

        target_user = (
            update.message.reply_to_message.from_user
        )

        if target_user:

            target_id = target_user.id

            target_name = (
                target_user.full_name
                or target_user.username
                or str(target_id)
            )

    # Numeric fallback
    elif context.args:

        try:

            target_id = int(
                context.args[0]
            )

            target_name = str(
                target_id
            )

        except ValueError:

            await update.message.reply_text(
                "❌ User ID numeric होना चाहिए।"
            )

            return

    else:

        await update.message.reply_text(
            "Admin authorize करने का सबसे आसान तरीका:\n\n"
            "1. उस user का कोई message आने दें।\n"
            "2. उसके message पर Reply करें।\n"
            "3. उसी reply में /addadmin भेजें।\n\n"
            "या:\n"
            "/addadmin USER_ID"
        )

        return

    if target_id == OWNER_USER_ID:

        await update.message.reply_text(
            "यह user पहले से Owner है।"
        )

        return

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO admins
        (user_id, added_by)
        VALUES (?, ?)
        """,
        (
            target_id,
            user.id,
        ),
    )

    inserted = cur.rowcount

    conn.commit()
    conn.close()

    if inserted:

        await update.message.reply_text(
            "✅ Admin successfully authorized.\n\n"
            f"Name: {target_name}\n"
            f"User ID: {target_id}\n\n"
            "अब यह user /start करके quiz बना सकता है।"
        )

    else:

        await update.message.reply_text(
            "ℹ️ यह user पहले से Admin है।\n\n"
            f"User ID: {target_id}"
        )


async def remove_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not is_owner(user.id):

        await update.message.reply_text(
            "❌ केवल Bot Owner Admin remove कर सकता है।"
        )

        return

    target_id = None

    if update.message.reply_to_message:

        target_user = (
            update.message.reply_to_message.from_user
        )

        if target_user:
            target_id = target_user.id

    elif context.args:

        try:
            target_id = int(context.args[0])
        except ValueError:

            await update.message.reply_text(
                "❌ User ID numeric होना चाहिए।"
            )

            return

    else:

        await update.message.reply_text(
            "जिस Admin को remove करना है, "
            "उसके message पर reply करके /removeadmin भेजें।"
        )

        return

    if target_id == OWNER_USER_ID:

        await update.message.reply_text(
            "❌ Owner को remove नहीं किया जा सकता।"
        )

        return

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM admins WHERE user_id = ?",
        (target_id,),
    )

    removed = cur.rowcount

    conn.commit()
    conn.close()

    if removed:

        await update.message.reply_text(
            "✅ Admin access removed.\n\n"
            f"User ID: {target_id}"
        )

    else:

        await update.message.reply_text(
            "ℹ️ यह user authorized Admin list में नहीं था।"
        )


async def list_admins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not is_owner(user.id):

        await update.message.reply_text(
            "❌ केवल Owner Admin list देख सकता है।"
        )

        return

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT user_id, added_at
        FROM admins
        ORDER BY added_at
        """
    )

    rows = cur.fetchall()

    conn.close()

    lines = [
        "👑 ECA QUIZ MAKER ADMINS",
        "",
        f"Owner ID: {OWNER_USER_ID}",
        "",
        "Authorized Admins:",
    ]

    if not rows:

        lines.append(
            "कोई additional Admin नहीं है।"
        )

    else:

        for index, row in enumerate(
            rows,
            start=1,
        ):

            lines.append(
                f"{index}. {row[0]}"
            )

    await update.message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    context.user_data.clear()

    if not is_admin(user.id):

        await update.message.reply_text(
            "📚 ECA Quiz Maker\n\n"
            "यह bot केवल ECA के authorized "
            "Owner/Admins के लिए Quiz creation के लिए है।\n\n"
            "आपके account को quiz बनाने की permission नहीं है।"
        )

        return ConversationHandler.END

    keyboard = [
        ["🤖 AI Automatic Quiz"],
        ["📝 My Questions Quiz"],
    ]

    await update.message.reply_text(
        "📚 ECA QUIZ MAKER\n\n"
        "आप क्या करना चाहते हैं?",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
        ),
    )

    return MODE


# ============================================================
# MAIN MODE
# ============================================================

async def receive_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    choice = update.message.text.strip()

    if choice == "🤖 AI Automatic Quiz":

        keyboard = [
            ["📤 My Source"],
            ["🔎 AI Find Source"],
        ]

        await update.message.reply_text(
            "🤖 AI Automatic Quiz\n\n"
            "Questions के लिए source कैसे लेना है?",
            reply_markup=ReplyKeyboardMarkup(
                keyboard,
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

        return SOURCE_MODE

    if choice == "📝 My Questions Quiz":

        await update.message.reply_text(
            "📝 My Questions Quiz अभी अगले module में "
            "enable किया जाएगा।\n\n"
            "अभी AI Automatic Quiz पूरी तरह active है।",
            reply_markup=ReplyKeyboardRemove(),
        )

        return ConversationHandler.END

    await update.message.reply_text(
        "कृपया menu से option चुनिए।"
    )

    return MODE


# ============================================================
# SOURCE MODE
# ============================================================

async def receive_source_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    choice = update.message.text.strip()

    if choice == "📤 My Source":

        context.user_data[
            "source_mode"
        ] = "provided_source"

        await update.message.reply_text(
            "📤 MY SOURCE\n\n"
            "अब अपना source भेजिए:\n\n"
            "📷 Photo / Image\n"
            "📄 PDF\n"
            "⌨️ Text\n\n"
            "Source भेजने के बाद मैं Topic पूछूँगा।"
        )

        return SOURCE_INPUT

    if choice == "🔎 AI Find Source":

        context.user_data[
            "source_mode"
        ] = "ai_source"

        await update.message.reply_text(
            "🔎 AI FIND SOURCE\n\n"
            "अब Topic भेजिए।\n\n"
            "Gemini Google Search grounding का इस्तेमाल "
            "करके relevant और authentic web sources से "
            "information verify करेगा।"
        )

        return TOPIC

    await update.message.reply_text(
        "कृपया My Source या AI Find Source चुनिए।"
    )

    return SOURCE_MODE


# ============================================================
# SAVE TEXT SOURCE
# ============================================================

async def receive_source_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    text = update.message.text

    if not text or len(text.strip()) < 20:

        await update.message.reply_text(
            "❌ Source बहुत छोटा है। "
            "कृपया पर्याप्त source text भेजिए।"
        )

        return SOURCE_INPUT

    context.user_data[
        "source_text"
    ] = text.strip()

    context.user_data[
        "source_type"
    ] = "text"

    await update.message.reply_text(
        "✅ Source text प्राप्त हो गया।\n\n"
        "अब Quiz का Topic बताइए।"
    )

    return TOPIC


# ============================================================
# DOWNLOAD TELEGRAM FILE
# ============================================================

async def download_telegram_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message

    if message.photo:

        telegram_file = await context.bot.get_file(
            message.photo[-1].file_id
        )

        suffix = ".jpg"

    elif message.document:

        telegram_file = await context.bot.get_file(
            message.document.file_id
        )

        filename = (
            message.document.file_name
            or "source"
        )

        suffix = os.path.splitext(
            filename
        )[1].lower()

        if not suffix:
            suffix = ".bin"

    else:

        return None

    temp = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix,
    )

    temp_path = temp.name
    temp.close()

    await telegram_file.download_to_drive(
        custom_path=temp_path
    )

    return temp_path


# ============================================================
# PHOTO / PDF SOURCE
# ============================================================

async def receive_source_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    if not (
        update.message.photo
        or update.message.document
    ):

        await update.message.reply_text(
            "कृपया Photo या PDF भेजिए।"
        )

        return SOURCE_INPUT

    filename = ""

    if update.message.document:

        filename = (
            update.message.document.file_name
            or ""
        ).lower()

        if not filename.endswith(".pdf"):

            await update.message.reply_text(
                "❌ अभी My Source में PDF या Image भेजिए।"
            )

            return SOURCE_INPUT

    await update.message.reply_text(
        "⏳ Source receive हो रहा है..."
    )

    try:

        path = await download_telegram_file(
            update,
            context,
        )

        if not path:

            raise RuntimeError(
                "File download failed."
            )

        context.user_data[
            "source_file"
        ] = path

        context.user_data[
            "source_type"
        ] = (
            "pdf"
            if filename.endswith(".pdf")
            else "image"
        )

        await update.message.reply_text(
            "✅ Source successfully प्राप्त हो गया।\n\n"
            "अब Quiz का Topic बताइए।"
        )

        return TOPIC

    except Exception:

        logger.exception(
            "Source file processing failed."
        )

        await update.message.reply_text(
            "❌ Source receive करने में समस्या हुई। "
            "कृपया फिर से Photo/PDF भेजिए।"
        )

        return SOURCE_INPUT


# ============================================================
# TOPIC
# ============================================================

async def receive_topic(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    topic = update.message.text.strip()

    if len(topic) < 2:

        await update.message.reply_text(
            "❌ कृपया valid Topic भेजिए।"
        )

        return TOPIC

    context.user_data[
        "topic"
    ] = topic

    await update.message.reply_text(
        "अब कितने questions चाहिए?\n\n"
        "1 से 100 के बीच संख्या भेजिए।\n\n"
        "उदाहरण: 10"
    )

    return QUESTION_COUNT


# ============================================================
# QUESTION COUNT
# ============================================================

async def receive_question_count(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    try:

        count = int(
            update.message.text.strip()
        )

    except ValueError:

        await update.message.reply_text(
            "❌ केवल संख्या भेजिए।\n\n"
            "उदाहरण: 20"
        )

        return QUESTION_COUNT

    if count < 1 or count > 100:

        await update.message.reply_text(
            "❌ संख्या 1 से 100 के बीच होनी चाहिए।"
        )

        return QUESTION_COUNT

    context.user_data[
        "question_count"
    ] = count

    keyboard = [
        ["हिंदी", "English"],
        ["Bilingual"],
    ]

    await update.message.reply_text(
        "Quiz की language चुनिए:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )

    return LANGUAGE


# ============================================================
# LANGUAGE
# ============================================================

async def receive_language(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(
        update.effective_user.id
    ):

        return ConversationHandler.END

    language_map = {
        "हिंदी": "Hindi",
        "English": "English",
        "Bilingual": "Bilingual",
    }

    choice = update.message.text.strip()

    if choice not in language_map:

        await update.message.reply_text(
            "हिंदी, English या Bilingual में से चुनिए।"
        )

        return LANGUAGE

    language = language_map[choice]

    context.user_data[
        "language"
    ] = language

    topic = context.user_data[
        "topic"
    ]

    count = context.user_data[
        "question_count"
    ]

    source_mode = context.user_data[
        "source_mode"
    ]

    source_label = (
        "आपके दिए हुए source"
        if source_mode == "provided_source"
        else "AI द्वारा Google Search से verified sources"
    )

    await update.message.reply_text(
        "⚙️ QUIZ GENERATION STARTED\n\n"
        f"📚 Topic: {topic}\n"
        f"🔢 Questions: {count}\n"
        f"🌐 Language: {language}\n"
        f"📖 Source: {source_label}\n\n"
        "AI rules:\n"
        "✓ Original questions\n"
        "✓ Existing questions copy नहीं\n"
        "✓ केवल wording बदलकर duplicate नहीं\n"
        "✓ पुराने ECA questions repeat नहीं\n"
        "✓ अलग subtopics/concepts को priority\n"
        "✓ Ambiguous questions reject\n"
        "✓ Fact verification\n"
        "✓ Quality over quantity\n\n"
        "⏳ कृपया प्रतीक्षा करें..."
    )

    try:

        questions = await generate_questions(
            topic=topic,
            count=count,
            language=language,
            source_mode=source_mode,
            source_file=context.user_data.get(
                "source_file"
            ),
            source_text=context.user_data.get(
                "source_text"
            ),
        )

        if not questions:

            await update.message.reply_text(
                "❌ इस request पर valid questions generate नहीं हो सके।\n\n"
                "Topic/source थोड़ा अधिक specific करके फिर कोशिश करें।",
                reply_markup=ReplyKeyboardRemove(),
            )

            return ConversationHandler.END

        quiz_id = uuid.uuid4().hex[:12]

        await update.message.reply_text(
            f"✅ {len(questions)} questions तैयार हैं।\n\n"
            "अब Telegram Quiz शुरू किया जा रहा है..."
        )

        for question in questions:

            try:

                await send_quiz_poll(
                    context=context,
                    chat_id=update.effective_chat.id,
                    question=question,
                )

                save_question_history(
                    question,
                    quiz_id,
                )

            except Exception:

                logger.exception(
                    "Could not send quiz question."
                )

        await update.message.reply_text(
            "🎯 ECA Quiz generation complete.",
            reply_markup=ReplyKeyboardRemove(),
        )

    except Exception as exc:

        logger.exception(
            "Question generation failed."
        )

        await update.message.reply_text(
            "❌ AI question generation में error आया।\n\n"
            f"Technical error: {str(exc)[:500]}",
            reply_markup=ReplyKeyboardRemove(),
        )

    finally:

        cleanup_source_file(
            context.user_data.get(
                "source_file"
            )
        )

        context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# CLEAN TEMP SOURCE
# ============================================================

def cleanup_source_file(
    path: Optional[str]
):

    if not path:
        return

    try:

        if os.path.exists(path):
            os.remove(path)

    except Exception:

        logger.warning(
            "Could not delete temp file: %s",
            path,
        )


# ============================================================
# GEMINI SCHEMA
# ============================================================

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string"
                    },
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        }
                    },
                    "correct_index": {
                        "type": "integer"
                    },
                    "explanation": {
                        "type": "string"
                    },
                    "source": {
                        "type": "string"
                    },
                    "topic": {
                        "type": "string"
                    }
                },
                "required": [
                    "question",
                    "options",
                    "correct_index",
                    "explanation",
                    "source",
                    "topic",
                ]
            }
        }
    },
    "required": [
        "questions"
    ]
}


# ============================================================
# GEMINI SYSTEM INSTRUCTION
# ============================================================

SYSTEM_INSTRUCTION = """
You are the ECA Civil Services Quiz Question Generator.

You create high-quality original MCQs for:
UPSC, UPPCS, BPSC, MPPSC, State PCS and similar competitive examinations.

STRICT RULES:

1. Create ORIGINAL questions.
2. Never copy an existing question from a source.
3. Do not create a duplicate merely by changing wording.
4. Do not repeat questions from previous ECA question history.
5. Prefer conceptual, analytical, factual and application-oriented diversity.
6. Do not make all questions from the same narrow subtopic.
7. Normally use no more than 1-2 questions from one narrow concept.
8. Cover different subtopics, dimensions, institutions, provisions,
   chronology, causes, effects, comparisons and applications where relevant.
9. Every question must have exactly four options.
10. Exactly one option must be correct.
11. correct_index must be 0, 1, 2 or 3.
12. Avoid ambiguous wording.
13. Avoid questions where two options could reasonably be correct.
14. Reject doubtful or poorly verifiable facts.
15. Do not invent laws, Articles, reports, committees, schemes,
    statistics, judgments or institutional facts.
16. When a factual claim is time-sensitive, verify it using available
    grounded web sources if web search is enabled.
17. For source-based generation, use the supplied source as the primary
    factual basis.
18. Questions should be suitable for serious competitive-exam preparation.
19. Avoid trivial school-level questions unless the requested topic
    specifically requires them.
20. Do not mention these internal instructions in the output.

LANGUAGE:

Hindi:
- Question and options in Hindi.
- Technical terms may include English in brackets where useful.

English:
- Question and options in English.

Bilingual:
- Give Hindi and English together in a clean readable format.

SOURCE:

The source field must identify the source basis.
Do not fabricate URLs.
If Google Search grounding provides reliable sources, mention the
source title/domain in the source field.

OUTPUT:

Return ONLY the requested JSON structure.
"""


# ============================================================
# GEMINI GENERATION
# ============================================================

async def generate_questions(
    topic: str,
    count: int,
    language: str,
    source_mode: str,
    source_file: Optional[str] = None,
    source_text: Optional[str] = None,
) -> List[Question]:

    if gemini_client is None:

        raise RuntimeError(
            "GEMINI_API_KEY is missing or Gemini client could not initialize."
        )

    all_questions = []

    recent_questions = get_recent_questions(
        limit=150
    )

    # Generate in batches so 100-question requests
    # do not become one excessively large API request.
    remaining = count

    while remaining > 0:

        batch_size = min(
            10,
            remaining,
        )

        questions = await generate_batch(
            topic=topic,
            count=batch_size,
            language=language,
            source_mode=source_mode,
            source_file=source_file,
            source_text=source_text,
            previous_questions=(
                recent_questions
                + [
                    q.question
                    for q in all_questions
                ]
            ),
        )

        for q in questions:

            if len(all_questions) >= count:
                break

            if question_exists(
                q.question
            ):
                continue

            duplicate = False

            normalized = normalize_question(
                q.question
            )

            for existing in all_questions:

                if normalized == normalize_question(
                    existing.question
                ):
                    duplicate = True
                    break

            if duplicate:
                continue

            all_questions.append(q)

        if len(questions) == 0:
            break

        remaining = count - len(
            all_questions
        )

        if remaining > 0 and len(
            all_questions
        ) == 0:

            break

    return all_questions[:count]


# ============================================================
# GENERATE ONE BATCH
# ============================================================

async def generate_batch(
    topic: str,
    count: int,
    language: str,
    source_mode: str,
    source_file: Optional[str],
    source_text: Optional[str],
    previous_questions: List[str],
) -> List[Question]:

    previous_block = ""

    if previous_questions:

        # Keep prompt reasonably sized.
        previous_block = "\n".join(
            f"- {q[:500]}"
            for q in previous_questions[-150:]
        )

    prompt_parts = []

    prompt_parts.append(
        f"""
Generate {count} original MCQs on this topic:

TOPIC:
{topic}

LANGUAGE:
{language}

IMPORTANT:
Create exactly {count} questions if enough valid material exists.

Do NOT copy source questions.

Do NOT merely paraphrase existing questions.

Do NOT repeat the previous ECA questions listed below.

PREVIOUS ECA QUESTIONS:
{previous_block}

Question diversity is mandatory.
Use different subtopics/concepts/angles.
"""
    )

    # --------------------------------------------------------
    # SOURCE TEXT
    # --------------------------------------------------------

    if source_mode == "provided_source":

        if source_text:

            prompt_parts.append(
                """
SOURCE MATERIAL PROVIDED BY ADMIN:

---------------- SOURCE START ----------------
"""
                + source_text[:100000]
                + """
---------------- SOURCE END ----------------

Use this source as the primary factual basis.
"""
            )

        elif source_file:

            uploaded = None

            try:

                uploaded = gemini_client.files.upload(
                    file=source_file
                )

                prompt_parts.append(
                    """
A source file has been supplied by the admin.
Read and understand the complete supplied file.
Generate questions from its factual/conceptual content.
Do not copy any existing questions contained in it.
"""
                )

                contents = [
                    "\n".join(
                        prompt_parts
                    ),
                    uploaded,
                ]

                return await call_gemini(
                    contents=contents,
                    use_search=False,
                    topic=topic,
                )

            except Exception:

                logger.exception(
                    "Gemini source file processing failed."
                )

                raise

        else:

            raise RuntimeError(
                "My Source selected but no source was received."
            )

    # --------------------------------------------------------
    # AI FIND SOURCE
    # --------------------------------------------------------

    if source_mode == "ai_source":

        prompt_parts.append(
            """
Use Google Search grounding.

Search for authoritative and reliable sources
relevant to the topic.

Prefer:
- Government websites
- Constitutional/legal primary sources
- Official ministry/department websites
- Official reports
- Parliament/legislative sources
- RBI/SEBI/UPSC/NCERT/WHO/UN or other relevant
  official institutional sources where appropriate
- Reputed primary institutional sources

Verify factual claims before constructing questions.

Do not rely on a random low-quality website when an
authoritative source is available.
"""
        )

    prompt = "\n".join(
        prompt_parts
    )

    return await call_gemini(
        contents=prompt,
        use_search=(
            source_mode == "ai_source"
        ),
        topic=topic,
    )


# ============================================================
# CALL GEMINI
# ============================================================

async def call_gemini(
    contents,
    use_search: bool,
    topic: str,
) -> List[Question]:

    tools = []

    if use_search:

        tools.append(
            types.Tool(
                google_search=types.GoogleSearch()
            )
        )

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.45,
        max_output_tokens=12000,
        response_mime_type="application/json",
        response_schema=QUESTION_SCHEMA,
        tools=tools if tools else None,
    )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=config,
    )

    if not response.text:

        raise RuntimeError(
            "Gemini returned an empty response."
        )

    try:

        data = json.loads(
            response.text
        )

    except json.JSONDecodeError:

        logger.error(
            "Gemini returned invalid JSON: %s",
            response.text[:2000],
        )

        raise RuntimeError(
            "Gemini returned invalid JSON."
        )

    raw_questions = data.get(
        "questions",
        []
    )

    results = []

    for item in raw_questions:

        try:

            question = str(
                item["question"]
            ).strip()

            options = [
                str(x).strip()
                for x in item["options"]
            ]

            correct_index = int(
                item["correct_index"]
            )

            explanation = str(
                item.get(
                    "explanation",
                    ""
                )
            ).strip()

            source = str(
                item.get(
                    "source",
                    "Gemini verified source"
                )
            ).strip()

            item_topic = str(
                item.get(
                    "topic",
                    topic
                )
            ).strip()

            if len(options) != 4:
                continue

            if correct_index not in (
                0,
                1,
                2,
                3,
            ):
                continue

            if not question:
                continue

            if not explanation:
                explanation = (
                    "Correct answer is based on the verified "
                    "factual/conceptual basis of the question."
                )

            results.append(
                Question(
                    question=question,
                    options=options,
                    correct_index=correct_index,
                    explanation=explanation,
                    source=source,
                    topic=item_topic or topic,
                )
            )

        except Exception:

            logger.warning(
                "Invalid question object skipped."
            )

    return results


# ============================================================
# SEND TELEGRAM QUIZ POLL
# ============================================================

async def send_quiz_poll(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    question: Question,
):

    source_text = question.source.strip()

    description = (
        f"📚 ECA\n"
        f"Topic: {question.topic}\n\n"
        f"Source: {source_text}\n\n"
        "Eternal Civil Academy"
    )

    # Telegram quiz question max length is handled here.
    poll_question = question.question[:300]

    options = [
        option[:100]
        for option in question.options
    ]

    explanation = (
        question.explanation[:200]
    )

    await context.bot.send_poll(
        chat_id=chat_id,
        question=poll_question,
        options=options,
        type="quiz",
        is_anonymous=False,
        allows_multiple_answers=False,
        correct_option_id=question.correct_index,
        explanation=explanation,
    )

    # Send source separately because Telegram's native
    # poll description support can vary by Bot API/client.
    await context.bot.send_message(
        chat_id=chat_id,
        text=description[:1024],
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "📚 ECA QUIZ MAKER\n\n"
        "/start — Quiz Maker\n"
        "/help — Help\n"
        "/cancel — Current operation cancel\n"
        "/whoami — अपना Telegram User ID\n\n"
        "OWNER COMMANDS\n"
        "/addadmin — किसी user के message पर reply करके\n"
        "               /addadmin भेजें\n\n"
        "/removeadmin — Admin के message पर reply करके\n"
        "                  /removeadmin भेजें\n\n"
        "/admins — Authorized Admins list"
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    cleanup_source_file(
        context.user_data.get(
            "source_file"
        )
    )

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Current operation cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return ConversationHandler.END


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Unhandled Telegram error:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable नहीं मिला।"
        )

    if OWNER_USER_ID == 0:

        raise RuntimeError(
            "OWNER_USER_ID environment variable नहीं मिला।"
        )

    if not GEMINI_API_KEY:

        raise RuntimeError(
            "GEMINI_API_KEY environment variable नहीं मिला।"
        )

    if gemini_client is None:

        raise RuntimeError(
            "Gemini client initialize नहीं हुआ।"
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # CONVERSATION
    # --------------------------------------------------------

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "start",
                start,
            )
        ],

        states={

            MODE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_mode,
                )
            ],

            SOURCE_MODE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_source_mode,
                )
            ],

            SOURCE_INPUT: [

                MessageHandler(
                    filters.PHOTO
                    | filters.Document.PDF,
                    receive_source_file,
                ),

                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_source_text,
                ),
            ],

            TOPIC: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_topic,
                )
            ],

            QUESTION_COUNT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_question_count,
                )
            ],

            LANGUAGE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    receive_language,
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel,
            )
        ],

        allow_reentry=True,
    )

    application.add_handler(
        conversation
    )

    # --------------------------------------------------------
    # OWNER / GENERAL COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "addadmin",
            add_admin,
        )
    )

    application.add_handler(
        CommandHandler(
            "removeadmin",
            remove_admin,
        )
    )

    application.add_handler(
        CommandHandler(
            "admins",
            list_admins,
        )
    )

    application.add_handler(
        CommandHandler(
            "whoami",
            whoami,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # START RENDER HEALTH SERVER
    # --------------------------------------------------------

    health_thread = threading.Thread(
        target=start_health_server,
        name="eca-health-server",
        daemon=True,
    )

    health_thread.start()

    logger.info(
        "ECA Quiz Maker Bot is starting..."
    )

    logger.info(
        "Gemini model: %s",
        GEMINI_MODEL,
    )

    # drop_pending_updates=True prevents old Telegram
    # updates from a previous deployment from entering
    # the new conversation state.
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
