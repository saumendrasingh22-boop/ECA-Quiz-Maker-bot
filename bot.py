import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# ============================================================
# ECA QUIZ MAKER BOT
# ============================================================
# Owner + Authorized Admin system
#
# OWNER:
# - OWNER_USER_ID वाला व्यक्ति permanent Owner है
# - केवल Owner नए Admin जोड़/हटा सकता है
#
# ADMIN:
# - Authorized Admin Quiz बना सकता है
# - Student Quiz create नहीं कर सकता
#
# QUIZ RULES:
# - AI Automatic Quiz
# - My Source
# - AI Find Source
# - Hindi / English / Bilingual
# - Original questions
# - Duplicate questions नहीं
# - Same narrow topic/concept से max 1–2 questions
# - पुराने ECA questions repeat नहीं
# - Authentic source verification
# - Source: @EternalCivilAcademy
#
# SCORING:
# Correct = +1
# Wrong = -1/3
# Unattempted = 0
#
# RANKING:
# Raw Marks के आधार पर
# Time का कोई role नहीं
# Equal Raw Marks = Equal Rank
#
# RENDER:
# Health server 0.0.0.0:$PORT पर चलता है
# ============================================================


BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

DB_FILE = "eca_quiz.db"

SOURCE_TEXT = "Source: @EternalCivilAcademy"

PORT = int(os.getenv("PORT", "10000"))


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


MODE, SOURCE_MODE, TOPIC, QUESTION_COUNT, LANGUAGE = range(5)


# ============================================================
# QUESTION DATA STRUCTURE
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

        if self.path in ("/", "/health", "/healthz"):

            body = b"ECA Quiz Maker Bot is running"

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(body)

        else:

            self.send_response(404)

            self.end_headers()


    def log_message(self, format, *args):

        return


def start_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    logger.info(
        "Render health server listening on 0.0.0.0:%s",
        PORT
    )

    server.serve_forever()


# ============================================================
# DATABASE
# ============================================================

def db():

    return sqlite3.connect(DB_FILE)


def init_db():

    conn = db()

    cur = conn.cursor()


    # Authorized admins

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (

            user_id INTEGER PRIMARY KEY,

            added_by INTEGER NOT NULL,

            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )
    """)


    # Question history

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


    # Participants

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


# ============================================================
# AUTHENTICATION
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
        (user_id,)
    )

    result = cur.fetchone()

    conn.close()

    return result is not None


# ============================================================
# ADD ADMIN
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
        (
            new_admin_id,
            user.id
        )
    )


    conn.commit()

    conn.close()


    await update.message.reply_text(
        f"✅ Admin authorized.\n\n"
        f"User ID: {new_admin_id}"
    )


# ============================================================
# REMOVE ADMIN
# ============================================================

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


# ============================================================
# LIST ADMINS
# ============================================================

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
        """
        SELECT user_id
        FROM admins
        ORDER BY added_at
        """
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

        for index, (admin_id,) in enumerate(
            rows,
            1
        ):

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


    # Students cannot create quizzes

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

            resize_keyboard=True

        )

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


    # --------------------------------------------------------
    # AI AUTOMATIC
    # --------------------------------------------------------

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

                one_time_keyboard=True

            )

        )


        return SOURCE_MODE


    # --------------------------------------------------------
    # MY QUESTIONS
    # --------------------------------------------------------

    if choice == "📝 My Questions Quiz":

        keyboard = [

            ["📷 Photo से Questions"],

            ["📄 PDF से Questions"],

            ["⌨️ Text से Questions"],

        ]


        await update.message.reply_text(

            "📝 My Questions Quiz\n\n"

            "आप अपने existing questions किस रूप में "
            "देना चाहते हैं?",

            reply_markup=ReplyKeyboardMarkup(

                keyboard,

                resize_keyboard=True,

                one_time_keyboard=True

            )

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


    # --------------------------------------------------------
    # USER PROVIDES SOURCE
    # --------------------------------------------------------

    if choice == "📤 My Source":

        context.user_data["source_mode"] = (
            "provided_source"
        )


        await update.message.reply_text(

            "📤 My Source selected.\n\n"

            "इस mode में book/notes की Photo, PDF "
            "या content दिया जाएगा।\n\n"

            "AI उसी material को पढ़कर original MCQs बनाएगा।\n\n"

            "Existing questions copy नहीं किए जाएंगे।\n"

            "एक ही narrow topic से बार-बार questions "
            "नहीं बनाए जाएंगे।"

        )


    # --------------------------------------------------------
    # AI FINDS SOURCE
    # --------------------------------------------------------

    elif choice == "🔎 AI Find Source":

        context.user_data["source_mode"] = (
            "ai_source"
        )


        await update.message.reply_text(

            "🔎 AI Find Source selected.\n\n"

            "AI topic के लिए authentic और reliable "
            "sources खोजेगा।\n\n"

            "Facts verify करके original MCQs बनाए जाएंगे।\n\n"

            "एक ही narrow topic/concept को unnecessarily "
            "repeat नहीं किया जाएगा।"

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

        "उदाहरण:\n"
        "20\n"
        "50\n"
        "100"

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

            "अभी 1 से 100 questions के बीच "
            "संख्या डालिए।"

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

            one_time_keyboard=True

        )

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


    if source_mode == "provided_source":

        source_name = (
            "आपके दिए हुए source"
        )

    else:

        source_name = (
            "AI द्वारा खोजे गए authentic sources"
        )


    await update.message.reply_text(

        "⚙️ QUIZ CONFIGURATION\n\n"

        f"📚 Topic: {topic}\n"

        f"🔢 Questions: {count}\n"

        f"🌐 Language: {language}\n"

        f"📖 Source: {source_name}\n\n"

        "QUESTION RULES\n"

        "✓ Original questions only\n"

        "✓ Source के existing questions copy नहीं होंगे\n"

        "✓ केवल wording बदलकर duplicate नहीं बनाया जाएगा\n"

        "✓ पुराने ECA questions repeat नहीं होंगे\n"

        "✓ एक narrow topic/concept से अधिकतम 1–2 questions\n"

        "✓ अलग subtopics/concepts/angles को priority\n"

        "✓ Authentic source verification\n"

        "✓ Doubtful/ambiguous questions reject\n"

        "✓ Time ranking में इस्तेमाल नहीं होगा\n\n"

        "अब AI engine question generation शुरू करेगा।"

    )


    questions = await generate_questions(

        topic=topic,

        count=count,

        language=language,

        source_mode=source_mode

    )


    if not questions:

        await update.message.reply_text(

            "⚠️ Quiz configuration successfully save हो गई है।\n\n"

            "लेकिन अभी वास्तविक AI/OCR/source-search "
            "engine connect नहीं किया गया है।\n\n"

            "अगले चरण में AI engine जोड़ने के बाद "
            "bot वास्तविक questions generate करेगा।"

        )

        return ConversationHandler.END


    context.user_data["questions"] = questions


    await update.message.reply_text(

        f"✅ {len(questions)} questions तैयार हैं।"

    )


    return ConversationHandler.END


# ============================================================
# AI QUESTION GENERATION ENGINE
# ============================================================

async def generate_questions(
    topic: str,
    count: int,
    language: str,
    source_mode: str
) -> List[Question]:

    """
    ============================================================
    REAL AI ENGINE WILL BE CONNECTED HERE
    ============================================================

    AUTOMATIC MODE:

    1. provided_source
       - Admin Photo/PDF/content देगा
       - OCR/text extraction होगा
       - Source से facts निकाले जाएंगे

    2. ai_source
       - AI authentic sources खोजेगा
       - Reliable/official sources को priority
       - Facts cross-check होंगे

    ============================================================
    ORIGINALITY RULES
    ============================================================

    - Existing source questions copy नहीं
    - Near-copy नहीं
    - सिर्फ wording बदलकर question नहीं
    - Previous ECA questions repeat नहीं
    - Same concept को unnecessarily repeat नहीं

    ============================================================
    TOPIC DIVERSITY
    ============================================================

    एक narrow topic/concept से maximum 1–2 questions।

    Questions को अलग-अलग:
    - subtopics
    - concepts
    - dimensions
    - factual angles
    - analytical angles

    में distribute किया जाएगा।

    ============================================================
    SOURCE RULE
    ============================================================

    हर generated question के लिए authentic source
    preserve किया जाएगा।

    Poll description में:

    Source: @EternalCivilAcademy

    जरूर रहेगा।

    ============================================================
    LANGUAGE
    ============================================================

    Hindi
    English
    Bilingual

    ============================================================
    TELEGRAM LIMITS
    ============================================================

    Question:
    maximum 300 characters

    Options:
    maximum 100 characters each

    Explanation:
    maximum 200 characters

    Poll Description:
    maximum 1024 characters

    ============================================================
    EXPLANATION RULE
    ============================================================

    Explanation में:

    1. Correct option क्यों सही है
    2. बाकी तीन options क्या हैं / क्यों गलत हैं

    short और informative तरीके से बताया जाएगा।

    ============================================================
    CURRENTLY PLACEHOLDER
    ============================================================
    """

    return []


# ============================================================
# SEND QUIZ POLL
# ============================================================

async def send_quiz_poll(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    question: Question
):

    # --------------------------------------------------------
    # TELEGRAM NATIVE POLL DESCRIPTION
    # MAXIMUM = 1024 CHARACTERS
    # --------------------------------------------------------

    description = (

        f"{question.source}\n\n"

        f"{SOURCE_TEXT}"

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

        # Current python-telegram-bot format
        correct_option_ids=[
            question.correct_index
        ],

        # Telegram explanation limit
        explanation=question.explanation[:200],

        # Telegram native description
        description=description[:1024]

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

    return correct - (
        wrong / 3
    )


# ============================================================
# RANKING
# ============================================================

def make_ranking(
    participants: List[dict]
) -> List[dict]:

    """

    Ranking ONLY by Raw Marks.

    Time का कोई role नहीं।

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

        reverse=True

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


# ============================================================
# LEADERBOARD
# ============================================================

def format_leaderboard(
    participants: List[dict]
) -> str:

    ranked = make_ranking(
        participants
    )


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

        reply_markup=ReplyKeyboardRemove()

    )


    return ConversationHandler.END


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # CHECK BOT TOKEN
    # --------------------------------------------------------

    if not BOT_TOKEN:

        raise RuntimeError(

            "BOT_TOKEN environment variable नहीं मिला।"

        )


    # --------------------------------------------------------
    # CHECK OWNER ID
    # --------------------------------------------------------

    if OWNER_USER_ID == 0:

        raise RuntimeError(

            "OWNER_USER_ID environment variable नहीं मिला।"

        )


    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    init_db()


    # --------------------------------------------------------
    # TELEGRAM APPLICATION
    # --------------------------------------------------------

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
                start
            )

        ],

        states={

            MODE: [

                MessageHandler(

                    filters.TEXT
                    & ~filters.COMMAND,

                    receive_mode

                )

            ],


            SOURCE_MODE: [

                MessageHandler(

                    filters.TEXT
                    & ~filters.COMMAND,

                    receive_source_mode

                )

            ],


            TOPIC: [

                MessageHandler(

                    filters.TEXT
                    & ~filters.COMMAND,

                    receive_topic

                )

            ],


            QUESTION_COUNT: [

                MessageHandler(

                    filters.TEXT
                    & ~filters.COMMAND,

                    receive_question_count

                )

            ],


            LANGUAGE: [

                MessageHandler(

                    filters.TEXT
                    & ~filters.COMMAND,

                    receive_language

                )

            ],

        },


        fallbacks=[

            CommandHandler(
                "cancel",
                cancel
            )

        ]

    )


    application.add_handler(
        conversation
    )


    # --------------------------------------------------------
    # OWNER COMMANDS
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # START HEALTH SERVER
    # --------------------------------------------------------

    logger.info(
        "ECA Quiz Maker Bot is running..."
    )


    health_thread = threading.Thread(

        target=start_health_server,

        name="render-health-server",

        daemon=True

    )


    health_thread.start()


    # --------------------------------------------------------
    # START TELEGRAM POLLING
    # --------------------------------------------------------

    application.run_polling()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
