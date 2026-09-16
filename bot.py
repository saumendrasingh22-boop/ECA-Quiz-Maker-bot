import os
import json
import logging
from dataclasses import dataclass
from typing import Dict, List

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

SOURCE_TEXT = "Source: @EternalCivilAcademy"

# Conversation states
TOPIC, QUESTION_COUNT, LANGUAGE = range(3)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# QUIZ DATA STRUCTURES
# ============================================================

@dataclass
class Question:
    question: str
    options: List[str]
    correct_index: int

    # Short explanation shown by Telegram quiz
    explanation: str

    # Full learning explanation for later message
    full_explanation: str

    source: str


# Temporary in-memory storage.
# Later we will replace this with SQLite/PostgreSQL.
quiz_sessions: Dict[int, dict] = {}


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    quiz_sessions[user.id] = {
        "topic": None,
        "question_count": None,
        "language": None,
        "questions": [],
        "current_question": 0,
    }

    await update.message.reply_text(
        "ECA Quiz Maker Bot में आपका स्वागत है।\n\n"
        "सबसे पहले Quiz का Topic भेजिए।\n\n"
        "उदाहरण:\n"
        "भारतीय संविधान — मौलिक अधिकार"
    )

    return TOPIC


# ============================================================
# TOPIC
# ============================================================

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    topic = update.message.text.strip()

    if len(topic) < 2:
        await update.message.reply_text(
            "कृपया एक valid topic भेजिए।"
        )
        return TOPIC

    quiz_sessions[user_id]["topic"] = topic

    await update.message.reply_text(
        f"Topic: {topic}\n\n"
        "अब कितने प्रश्न चाहिए?\n"
        "उदाहरण: 50"
    )

    return QUESTION_COUNT


# ============================================================
# QUESTION COUNT
# ============================================================

async def receive_question_count(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    try:
        count = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text(
            "कृपया केवल संख्या डालिए।\nउदाहरण: 25 या 50"
        )
        return QUESTION_COUNT

    if count < 1 or count > 100:
        await update.message.reply_text(
            "अभी 1 से 100 तक प्रश्न चुनिए।"
        )
        return QUESTION_COUNT

    quiz_sessions[user_id]["question_count"] = count

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
    user_id = update.effective_user.id

    language = update.message.text.strip()

    allowed = {
        "हिंदी": "Hindi",
        "English": "English",
        "Bilingual": "Bilingual",
    }

    if language not in allowed:
        await update.message.reply_text(
            "कृपया नीचे दिए गए तीन विकल्पों में से एक चुनिए:\n"
            "हिंदी / English / Bilingual"
        )
        return LANGUAGE

    language = allowed[language]

    quiz_sessions[user_id]["language"] = language

    data = quiz_sessions[user_id]

    await update.message.reply_text(
        "Quiz configuration तैयार है।\n\n"
        f"📚 Topic: {data['topic']}\n"
        f"🔢 Questions: {data['question_count']}\n"
        f"🌐 Language: {language}\n\n"
        "अब Question Generation शुरू होगी।\n\n"
        "ध्यान रहे: Questions authentic/primary sources "
        "के आधार पर बनाए जाएंगे और factual verification के "
        "बाद ही quiz में भेजे जाएंगे।"
    )

    # अभी AI generation connect नहीं किया गया है।
    # अगले चरण में यही function वास्तविक AI + source retrieval से जुड़ेगा.
    questions = await generate_questions(
        topic=data["topic"],
        count=data["question_count"],
        language=language,
    )

    if not questions:
        await update.message.reply_text(
            "Question generation अभी configured नहीं है।\n\n"
            "अगले चरण में हम AI + authentic source retrieval "
            "जोड़ेंगे।"
        )
        return ConversationHandler.END

    quiz_sessions[user_id]["questions"] = questions
    quiz_sessions[user_id]["current_question"] = 0

    await update.message.reply_text(
        f"✅ {len(questions)} questions तैयार हैं।\n"
        "अब quiz शुरू की जा सकती है।"
    )

    return ConversationHandler.END


# ============================================================
# AI QUESTION GENERATOR
# ============================================================

async def generate_questions(
    topic: str,
    count: int,
    language: str,
) -> List[Question]:

    """
    IMPORTANT:

    यह अभी placeholder है।

    अगले चरण में इसे वास्तविक AI/source-retrieval layer से जोड़ा जाएगा।

    AI को निम्न नियम दिए जाएंगे:

    1. Authentic/primary sources को प्राथमिकता।
    2. Constitution / Acts / Rules / official government
       documents / NCERT / PIB / official reports आदि,
       topic के अनुसार।
    3. Source verification अनिवार्य।
    4. Ambiguous question reject।
    5. केवल एक objectively correct answer।
    6. Question को केवल Telegram limit में fit करने के लिए
       अनावश्यक रूप से छोटा नहीं करना।
    7. Hindi / English / Bilingual instruction follow करना।
    8. प्रत्येक question के साथ source metadata save करना।
    9. प्रत्येक wrong option का short factual explanation।
    10. कोई unverifiable fact होने पर question reject।
    """

    # अभी empty list लौट रही है।
    # वास्तविक AI integration अगले चरण में आएगा।

    return []


# ============================================================
# SEND QUIZ POLL
# ============================================================

async def send_quiz_poll(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
    question: Question,
):
    """
    Native Telegram Quiz Poll.

    Non-anonymous रखा जाएगा ताकि participant answers
    leaderboard के लिए track किए जा सकें.
    """

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

        # Telegram quiz explanation की limit 200 characters है,
        # इसलिए यहां केवल short explanation जाएगा.
        explanation=question.explanation,

        # Requested ECA source attribution.
        description=SOURCE_TEXT,
    )


# ============================================================
# NEGATIVE MARKING
# ============================================================

def calculate_raw_marks(correct: int, wrong: int) -> float:
    """
    1 mark per correct answer.
    -1/3 for every wrong answer.
    Unattempted = 0.
    """

    return correct - (wrong / 3)


# ============================================================
# RANKING
# ============================================================

def make_ranking(participants: List[dict]) -> List[dict]:
    """
    Ranking rules:

    1. Raw Marks descending.
    2. Time is NOT considered.
    3. Equal Raw Marks = equal rank.
    4. Competition ranking:

       1
       2
       3
       3
       3
       6
    """

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
# LEADERBOARD FORMAT
# ============================================================

def format_leaderboard(participants: List[dict]) -> str:

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
        "ECA Quiz Maker Bot\n\n"
        "/start — नया Quiz बनाएँ\n"
        "/help — Help\n"
        "/cancel — Current Quiz cancel करें"
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    quiz_sessions.pop(user_id, None)

    await update.message.reply_text(
        "Quiz creation cancelled."
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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    conversation_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start)
        ],

        states={

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

    application.add_handler(conversation_handler)

    application.add_handler(
        CommandHandler("help", help_command)
    )

    print("ECA Quiz Maker Bot is running...")

    application.run_polling()


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":
    main()
