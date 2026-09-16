# Updated ECA Quiz Maker Bot
# Owner + Authorized Admin system
# Telegram native Poll Description supported (0-1024 chars)

# IMPORTANT:
# इस version में:
# 1. केवल Owner और Authorized Admins quiz बना सकते हैं
# 2. Students केवल quizzes attempt करेंगे
# 3. /addadmin, /removeadmin, /admins
# 4. AI Automatic में My Source और AI Find Source
# 5. Original questions / anti-repetition rules
# 6. Topic diversity: एक narrow topic से max 1-2 questions
# 7. Poll description में Source: @EternalCivilAcademy
# 8. Explanation में correct answer + बाकी options का short explanation
# 9. Leaderboard: Correct, Wrong और RM (Raw Marks)
# 10. Ranking केवल Raw Marks पर; time का कोई role नहीं
# 11. Equal marks = equal rank

import os
import sqlite3
import logging
from dataclasses import dataclass
from typing import List, Optional

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

DB_FILE = "eca_quiz.db"
SOURCE_FOOTER = "Source: @EternalCivilAcademy"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

MODE, SOURCE_MODE, TOPIC, QUESTION_COUNT, LANGUAGE = range(5)


@dataclass
class Question:
    question: str
    options: List[str]
    correct_index: int
    explanation: str
    source: str
    topic: str


# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS question_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            question TEXT NOT NULL,
            normalized_question TEXT NOT NULL,
            source TEXT,
            quiz_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            unattempted INTEGER DEFAULT 0,
            raw_marks REAL DEFAULT 0,
            UNIQUE(quiz_id, user_id)
        )
    """)

    conn.commit()
    conn.close()


def is_owner(user_id: int) -> bool:
    return OWNER_USER_ID != 0 and user_id == OWNER_USER_ID


def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM admins WHERE user_id = ?",
        (user_id,)
    )

    result = cur.fetchone()
    conn.close()

    return result is not None


# ============================================================
# ADMIN MANAGEMENT
# ============================================================

async def add_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text(
            "❌ केवल Bot Owner नए Admin authorize कर सकता है।"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/addadmin TELEGRAM_USER_ID\n\n"
            "उदाहरण:\n"
            "/addadmin 123456789"
        )
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Telegram User ID केवल numeric होना चाहिए।"
        )
        return

    if new_admin_id == OWNER_USER_ID:
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
        (new_admin_id, user.id),
    )

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Admin authorized.\n\nUser ID: {new_admin_id}"
    )


async def remove_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text(
            "❌ केवल Bot Owner Admin remove कर सकता है।"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/removeadmin TELEGRAM_USER_ID"
        )
        return

    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ User ID numeric होना चाहिए।"
        )
        return

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM admins WHERE user_id = ?",
        (admin_id,)
    )

    removed = cur.rowcount

    conn.commit()
    conn.close()

    if removed:
        await update.message.reply_text(
            f"✅ Admin access removed.\n\n"
            f"User ID: {admin_id}"
        )
    else:
        await update.message.reply_text(
            "यह User ID authorized admin list में नहीं मिली।"
        )


async def list_admins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text(
            "❌ केवल Bot Owner authorized admins देख सकता है।"
        )
        return

    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id FROM admins ORDER BY added_at"
    )

    rows = cur.fetchall()
    conn.close()

    lines = [
        "👑 ECA Quiz Maker Admins",
        "",
        f"Owner: {OWNER_USER_ID}",
        "",
        "Authorized Admins:",
    ]

    if not rows:
        lines.append(
            "कोई additional admin नहीं है।"
        )
    else:
        for index, (admin_id,) in enumerate(rows, 1):
            lines.append(
                f"{index}. {admin_id}"
            )

    await update.message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "📚 ECA Quiz Maker\n\n"
            "यह bot केवल ECA द्वारा बनाए गए quizzes "
            "को attempt करने के लिए उपलब्ध है।\n\n"
            "Quiz creation access केवल Owner और "
            "authorized Admins के पास है।"
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
# MAIN MENU
# ============================================================

async def receive_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    choice = update.message.text.strip()

    if choice == "🤖 AI Automatic Quiz":

        keyboard = [
            ["📤 My Source"],
            ["🔎 AI Find Source"],
        ]

        await update.message.reply_text(
            "🤖 AI Automatic Quiz\n\n"
            "Source कैसे लेना है?",
            reply_markup=ReplyKeyboardMarkup(
                keyboard,
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

        return SOURCE_MODE

    if choice == "📝 My Questions Quiz":

        keyboard = [
            ["📷 Photo से Questions"],
            ["📄 PDF से Questions"],
            ["⌨️ Text से Questions"],
        ]

        await update.message.reply_text(
            "📝 My Questions Quiz\n\n"
            "आप अपने existing questions किस रूप में देना चाहते हैं?",
            reply_markup=ReplyKeyboardMarkup(
                keyboard,
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
        )

        return ConversationHandler.END

    await update.message.reply_text(
        "कृपया menu से option चुनिए।"
    )

    return MODE


# ============================================================
# AUTOMATIC SOURCE MODE
# ============================================================

async def receive_source_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    choice = update.message.text.strip()

    if choice == "📤 My Source":

        context.user_data["source_mode"] = "provided_source"

        await update.message.reply_text(
            "📤 My Source selected.\n\n"
            "इस mode में book/notes की Photo, PDF या "
            "content दिया जाएगा।\n"
            "AI उसी material को पढ़कर original MCQs बनाएगा।\n\n"
            "Actual Photo/PDF ingestion engine अगले चरण में "
            "जोड़ा जाएगा।"
        )

    elif choice == "🔎 AI Find Source":

        context.user_data["source_mode"] = "ai_source"

        await update.message.reply_text(
            "🔎 AI Find Source selected.\n\n"
            "AI topic के लिए अच्छे authentic sources खोजेगा, "
            "facts verify करेगा और original MCQs बनाएगा।"
        )

    else:

        await update.message.reply_text(
            "कृपया My Source या AI Find Source चुनिए।"
        )

        return SOURCE_MODE

    await update.message.reply_text(
        "अब Quiz का Topic बताइए।"
    )

    return TOPIC


# ============================================================
# TOPIC
# ============================================================

async def receive_topic(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    topic = update.message.text.strip()

    if len(topic) < 2:

        await update.message.reply_text(
            "कृपया valid topic भेजिए।"
        )

        return TOPIC

    context.user_data["topic"] = topic

    await update.message.reply_text(
        "अब कितने questions चाहिए?\n\n"
        "उदाहरण: 20 / 50 / 100"
    )

    return QUESTION_COUNT


# ============================================================
# QUESTION COUNT
# ============================================================

async def receive_question_count(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    try:
        count = int(
            update.message.text.strip()
        )

    except ValueError:

        await update.message.reply_text(
            "कृपया केवल संख्या डालिए।"
        )

        return QUESTION_COUNT

    if count < 1 or count > 100:

        await update.message.reply_text(
            "अभी 1 से 100 questions के बीच संख्या डालिए।"
        )

        return QUESTION_COUNT

    context.user_data["question_count"] = count

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
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
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

    context.user_data["language"] = language

    topic = context.user_data["topic"]
    count = context.user_data["question_count"]
    source_mode = context.user_data["source_mode"]

    source_name = (
        "आपके दिए हुए source"
        if source_mode == "provided_source"
        else
        "AI द्वारा खोजे गए authentic sources"
    )

    await update.message.reply_text(
        "⚙️ Quiz Configuration\n\n"
        f"📚 Topic: {topic}\n"
        f"🔢 Questions: {count}\n"
        f"🌐 Language: {language}\n"
        f"📖 Source: {source_name}\n\n"

        "Question-generation rules:\n"
        "✓ Original questions only\n"
        "✓ Source के existing questions copy नहीं होंगे\n"
        "✓ केवल wording बदलकर duplicate नहीं बनाया जाएगा\n"
        "✓ पुराने ECA questions repeat नहीं होंगे\n"
        "✓ एक narrow topic/concept से अधिकतम 1–2 questions\n"
        "✓ अलग subtopics/concepts/angles को priority\n"
        "✓ Authentic source verification\n"
        "✓ Ambiguous/factually doubtful questions reject\n"
        "✓ Time ranking में इस्तेमाल नहीं होगा\n\n"

        "AI engine next stage में connect किया जाएगा।"
    )

    questions = await generate_questions(
        topic=topic,
        count=count,
        language=language,
        source_mode=source_mode,
    )

    if not questions:

        await update.message.reply_text(
            "✅ Configuration save हो गई है।\n\n"
            "Real AI/OCR/source-search engine अभी connect "
            "नहीं है। अगले चरण में इसे जोड़ा जाएगा।"
        )

        return ConversationHandler.END

    context.user_data["questions"] = questions

    await update.message.reply_text(
        f"✅ {len(questions)} questions तैयार हैं।"
    )

    return ConversationHandler.END


# ============================================================
# AI ENGINE
# ============================================================

async def generate_questions(
    topic: str,
    count: int,
    language: str,
    source_mode: str,
) -> List[Question]:

    """
    वास्तविक AI engine अगले चरण में connect होगा।

    HARD RULES:

    1. Automatic source modes:
       - provided_source
       - ai_source

    2. Originality:
       - source के existing questions copy नहीं
       - near-copy/paraphrase नहीं
       - previous ECA questions repeat नहीं

    3. Topic diversity:
       - same narrow topic/concept से max 1–2 questions
       - अलग subtopics/concepts/angles को priority

    4. Verification:
       - factual verification
       - ambiguous questions reject

    5. Language:
       - Hindi
       - English
       - Bilingual

    6. Telegram limits:
       - Question <= 300 characters
       - Option <= 100 characters
       - Explanation <= 200 characters
       - Description <= 1024 characters

    Return:
        List[Question]
    """

    return []


# ============================================================
# SEND TELEGRAM QUIZ
# ============================================================

async def send_quiz_poll(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    question: Question,
):

    # Native Telegram Poll Description
    # Maximum: 1024 characters

    description = (
        f"{question.source}\n\n"
        f"{SOURCE_FOOTER}"
    )

    await context.bot.send_poll(
        chat_id=chat_id,

        # Telegram question limit
        question=question.question[:300],

        # Telegram option limit
        options=[
            option[:100]
            for option in question.options
        ],

        type="quiz",

        is_anonymous=False,

        allows_multiple_answers=False,

        correct_option_id=question.correct_index,

        # Telegram quiz explanation limit
        explanation=question.explanation[:200],

        # Native Telegram poll description
        description=description[:1024],
    )


# ============================================================
# SCORING
# ============================================================

def calculate_raw_marks(
    correct: int,
    wrong: int
) -> float:

    """
    Correct = +1
    Wrong = -1/3
    Unattempted = 0
    """

    return correct - (wrong / 3)


# ============================================================
# RANKING
# ============================================================

def make_ranking(
    participants: List[dict]
) -> List[dict]:

    """
    Ranking ONLY by Raw Marks.

    Time is NOT considered.

    Equal Raw Marks = Equal Rank.

    Example:

    1. A — RM 39
    2. B — RM 38
    2. C — RM 38
    4. D — RM 37
    """

    participants = sorted(
        participants,
        key=lambda x: x["raw_marks"],
        reverse=True,
    )

    previous_marks: Optional[float] = None
    current_rank = 0

    for index, participant in enumerate(
        participants
    ):

        marks = participant["raw_marks"]

        if previous_marks is None:

            current_rank = 1

        elif marks != previous_marks:

            current_rank = index + 1

        participant["rank"] = current_rank

        previous_marks = marks

    return participants


def format_leaderboard(
    participants: List[dict]
) -> str:

    ranked = make_ranking(participants)

    lines = [
        "🏆 ECA LIVE QUIZ — TOP 50",
        "",
    ]

    for participant in ranked[:50]:

        lines.append(
            f'{participant["rank"]}. '
            f'{participant["name"]} — '
            f'✅{participant["correct"]} '
            f'❌{participant["wrong"]} | '
            f'RM {participant["raw_marks"]:.2f}'
        )

    return "\n".join(lines)


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📚 ECA Quiz Maker\n\n"

        "/start — नया Quiz\n"
        "/help — Help\n"
        "/cancel — Current operation cancel\n\n"

        "Owner commands:\n"
        "/addadmin USER_ID\n"
        "/removeadmin USER_ID\n"
        "/admins"
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    await update.message.reply_text(
        "Operation cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return ConversationHandler.END


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

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    conversation = ConversationHandler(

        entry_points=[
            CommandHandler(
                "start",
                start
            )
        ],

        states={

            MODE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_mode,
                )
            ],

            SOURCE_MODE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_source_mode,
                )
            ],

            TOPIC: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_topic,
                )
            ],

            QUESTION_COUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_question_count,
                )
            ],

            LANGUAGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_language,
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel
            )
        ],
    )

    application.add_handler(
        conversation
    )

    application.add_handler(
        CommandHandler(
            "addadmin",
            add_admin
        )
    )

    application.add_handler(
        CommandHandler(
            "removeadmin",
            remove_admin
        )
    )

    application.add_handler(
        CommandHandler(
            "admins",
            list_admins
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    logger.info(
        "ECA Quiz Maker Bot is running..."
    )

    application.run_polling()


if __name__ == "__main__":
    main()
