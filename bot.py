import os
import sqlite3
import logging
from dataclasses import dataclass
from typing import List

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

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# केवल Admin इस bot से quiz create कर सकेगा.
# Render Environment Variable में अपना Telegram numeric User ID डालें.
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

SOURCE_TEXT = "Source: @EternalCivilAcademy"

DB_FILE = "eca_quiz.db"

# Conversation states
MODE, SOURCE_MODE, TOPIC, QUESTION_COUNT, LANGUAGE = range(5)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Generated / used questions का permanent history
    cursor.execute("""
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

    # Quiz participants
    cursor.execute("""
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
# DATA STRUCTURES
# ============================================================

@dataclass
class Question:
    question: str
    options: List[str]
    correct_index: int

    # Telegram's short explanation field
    explanation: str

    # Full explanation for future detailed explanation message
    full_explanation: str

    source: str

    topic: str


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id: int) -> bool:
    return ADMIN_USER_ID != 0 and user_id == ADMIN_USER_ID


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
            "यह Quiz Maker केवल ECA Admin के लिए उपलब्ध है।"
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
            one_time_keyboard=True,
        ),
    )

    return MODE


# ============================================================
# MODE SELECTION
# ============================================================

async def receive_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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

        await update.message.reply_text(
            "📝 My Questions Quiz\n\n"
            "यह mode आगे Photo / PDF / Text से आपके "
            "existing questions को quiz में बदलेगा.\n\n"
            "अभी foundation तैयार है।"
        )

        return ConversationHandler.END

    await update.message.reply_text(
        "कृपया दिए गए दो options में से एक चुनिए।"
    )

    return MODE


# ============================================================
# AUTOMATIC SOURCE MODE
# ============================================================

async def receive_source_mode(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    choice = update.message.text.strip()

    if choice == "📤 My Source":

        context.user_data["source_mode"] = "provided_source"

        await update.message.reply_text(
            "📤 My Source चुना गया है।\n\n"
            "अगले version में आप यहाँ:\n"
            "• Book/Notes की Photo\n"
            "• PDF\n"
            "• Multiple pages\n\n"
            "भेज सकेंगे। AI उस material को पढ़कर "
            "original questions बनाएगा।"
        )

    elif choice == "🔎 AI Find Source":

        context.user_data["source_mode"] = "ai_source"

        await update.message.reply_text(
            "🔎 AI Find Source चुना गया है।\n\n"
            "AI topic के लिए authentic sources खोजेगा "
            "और उन्हीं के आधार पर original questions बनाएगा।"
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

    try:
        count = int(update.message.text.strip())
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
        else "AI द्वारा खोजे गए authentic sources"
    )

    await update.message.reply_text(
        "⚙️ Quiz Configuration\n\n"
        f"📚 Topic: {topic}\n"
        f"🔢 Questions: {count}\n"
        f"🌐 Language: {language}\n"
        f"📖 Source: {source_name}\n\n"
        "अब question-generation engine काम करेगा।\n\n"
        "महत्वपूर्ण नियम:\n"
        "• Questions original होंगे\n"
        "• Source के questions copy नहीं होंगे\n"
        "• पुराने ECA questions repeat नहीं होंगे\n"
        "• एक narrow topic से 1–2 से अधिक questions नहीं\n"
        "• Authentic source verification होगी\n"
        "• Ambiguous questions reject होंगे",
        reply_markup=ReplyKeyboardRemove(),
    )

    # --------------------------------------------------------
    # AI ENGINE PLACEHOLDER
    # --------------------------------------------------------
    #
    # अगले चरण में यहाँ:
    #
    # 1. Source retrieval
    # 2. OCR / PDF extraction
    # 3. AI generation
    # 4. Originality check
    # 5. Duplicate detection
    # 6. Topic diversity check
    # 7. Factual verification
    #
    # जोड़ा जाएगा.
    #
    questions = await generate_questions(
        topic=topic,
        count=count,
        language=language,
        source_mode=source_mode,
    )

    if not questions:

        await update.message.reply_text(
            "✅ Configuration सफलतापूर्वक save हो गई है।\n\n"
            "अभी AI/source engine connect नहीं किया गया है।\n"
            "अगले चरण में इसी bot में वास्तविक AI question "
            "generation और source verification जोड़ेंगे।"
        )

        return ConversationHandler.END

    context.user_data["questions"] = questions

    await update.message.reply_text(
        f"✅ {len(questions)} original questions तैयार हैं।"
    )

    return ConversationHandler.END


# ============================================================
# QUESTION GENERATION ENGINE
# ============================================================

async def generate_questions(
    topic: str,
    count: int,
    language: str,
    source_mode: str,
) -> List[Question]:

    """
    FINAL AI RULES
    ==============

    SOURCE MODES
    ------------

    1. provided_source:
       Admin द्वारा दिए गए Photo/PDF/Content से
       questions बनाए जाएँ।

    2. ai_source:
       AI खुद authentic/primary sources खोजे,
       verify करे और questions बनाए।

    ORIGINALITY
    -----------

    Source में मौजूद existing questions को:
    - copy नहीं करना
    - मामूली शब्द बदलकर reproduce नहीं करना
    - paraphrase नहीं करना
    - coaching/test-series questions reproduce नहीं करना

    Source केवल factual/conceptual grounding के लिए होगा।

    REPETITION CONTROL
    ------------------

    पूरे ECA question history से:
    - exact duplicates reject
    - near duplicates reject
    - substantially similar questions reject

    SAME-SOURCE DIVERSITY
    ---------------------

    एक narrow topic/concept से maximum 1–2 questions.

    नए questions को:
    - अलग subtopics
    - अलग concepts
    - अलग facts
    - अलग angles
    - अलग difficulty
    में distribute करना है।

    QUALITY
    -------

    - केवल एक objectively correct answer
    - ambiguous questions reject
    - factual verification
    - exam-level language
    - unnecessary shortening नहीं
    - Telegram limits के कारण quality compromise नहीं

    LANGUAGE
    --------

    Hindi / English / Bilingual

    Bilingual में लंबे question को जबरदस्ती छोटा
    नहीं करना है।
    """

    # अभी AI integration नहीं है.
    return []


# ============================================================
# TELEGRAM QUIZ POLL
# ============================================================

async def send_quiz_poll(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
    question: Question,
):

    # Telegram Quiz Poll
    #
    # Question: max 300 characters
    # Option: max 100 characters
    # Explanation: max 200 characters
    # Description: max 1024 characters

    await context.bot.send_poll(
        chat_id=chat_id,

        question=question.question,

        options=question.options,

        type="quiz",

        is_anonymous=False,

        allows_multiple_answers=False,

        allows_revoting=False,

        shuffle_options=False,

        correct_option_ids=[question.correct_index],

        explanation=question.explanation[:200],

        description=SOURCE_TEXT,
    )


# ============================================================
# SCORING
# ============================================================

def calculate_raw_marks(
    correct: int,
    wrong: int,
) -> float:

    # +1 correct
    # -1/3 wrong
    # 0 unattempted

    return correct - (wrong / 3)


# ============================================================
# RANKING
# ============================================================

def make_ranking(
    participants: List[dict]
) -> List[dict]:

    # IMPORTANT:
    # Time is NEVER used.

    participants = sorted(
        participants,
        key=lambda x: x["raw_marks"],
        reverse=True,
    )

    previous_marks = None
    current_rank = 0

    for index, participant in enumerate(participants):

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

    ranked = make_ranking(participants)

    lines = [
        "🏆 ECA LIVE QUIZ — TOP 50",
        "",
    ]

    for participant in ranked[:50]:

        rank = participant["rank"]
        name = participant["name"]
        correct = participant["correct"]
        wrong = participant["wrong"]
        raw_marks = participant["raw_marks"]

        lines.append(
            f"{rank}. {name} — "
            f"✅{correct} ❌{wrong} | "
            f"RM {raw_marks:.2f}"
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
        "/cancel — Current operation cancel"
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

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    conversation = ConversationHandler(

        entry_points=[
            CommandHandler("start", start)
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
            CommandHandler("cancel", cancel)
        ],
    )

    application.add_handler(conversation)

    application.add_handler(
        CommandHandler("help", help_command)
    )

    logger.info("ECA Quiz Maker Bot is running...")

    application.run_polling()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
