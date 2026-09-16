import os
import re
import json
import hashlib
import sqlite3
import logging
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from google import genai
from google.genai import types


# =========================================================
# ECA QUIZ MAKER BOT
# Eternal Civil Academy
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "0").strip() or 0)

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.8-flash"
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    "eca_quiz.db"
)

MAX_QUESTIONS = 20

SOURCE_LINE = "Source: @EternalCivilAcademy"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("eca-quiz")


# =========================================================
# ENVIRONMENT CHECK
# =========================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing")


# =========================================================
# GEMINI CLIENT
# =========================================================

ai = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================================================
# DATABASE
# =========================================================

def get_db():

    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            q_hash TEXT UNIQUE,
            question TEXT NOT NULL,
            topic TEXT,
            source TEXT,
            created_by INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    return conn


get_db().close()


# =========================================================
# ADMIN AUTHENTICATION
# =========================================================

def is_authorized(user_id: int) -> bool:

    if user_id == OWNER_ID:
        return True

    if user_id in ADMIN_IDS:
        return True

    conn = get_db()

    row = conn.execute(
        "SELECT 1 FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone()

    conn.close()

    return bool(row)


def add_admin(user_id: int, added_by: int):

    conn = get_db()

    conn.execute(
        """
        INSERT OR IGNORE INTO admins(user_id, added_by)
        VALUES (?, ?)
        """,
        (user_id, added_by)
    )

    conn.commit()
    conn.close()

    ADMIN_IDS.add(user_id)


def remove_admin(user_id: int):

    if user_id == OWNER_ID:
        return False

    conn = get_db()

    conn.execute(
        "DELETE FROM admins WHERE user_id=?",
        (user_id,)
    )

    conn.commit()
    conn.close()

    ADMIN_IDS.discard(user_id)

    return True


# =========================================================
# QUESTION DUPLICATE SYSTEM
# =========================================================

def normalize(text: str) -> str:

    text = text.lower().strip()

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    text = re.sub(
        r"[^\w\s\u0900-\u097f]",
        "",
        text
    )

    return text


def question_hash(question: str):

    return hashlib.sha256(
        normalize(question).encode("utf-8")
    ).hexdigest()


def get_previous_questions(limit=500):

    conn = get_db()

    rows = conn.execute(
        """
        SELECT question, topic
        FROM questions
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()

    conn.close()

    return rows


def save_question(
    question_data,
    user_id,
    source
):

    q_hash = question_hash(
        question_data["question"]
    )

    conn = get_db()

    try:

        conn.execute(
            """
            INSERT INTO questions
            (
                q_hash,
                question,
                topic,
                source,
                created_by
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                q_hash,
                question_data["question"],
                question_data.get("topic", ""),
                source,
                user_id,
            )
        )

        conn.commit()

        success = True

    except sqlite3.IntegrityError:

        success = False

    conn.close()

    return success


# =========================================================
# TELEGRAM MENUS
# =========================================================

def main_menu():

    return InlineKeyboardMarkup(
        [

            [
                InlineKeyboardButton(
                    "Automatic",
                    callback_data="mode_auto"
                ),

                InlineKeyboardButton(
                    "Manual",
                    callback_data="mode_manual"
                ),
            ],

            [
                InlineKeyboardButton(
                    "AI खुद Source खोजे",
                    callback_data="auto_search"
                ),

                InlineKeyboardButton(
                    "Source भेजें",
                    callback_data="auto_source"
                ),
            ],

            [
                InlineKeyboardButton(
                    "Book/Page Photo OCR",
                    callback_data="ocr"
                )
            ],

        ]
    )


def language_menu():

    return InlineKeyboardMarkup(
        [

            [
                InlineKeyboardButton(
                    "हिंदी",
                    callback_data="lang_hi"
                ),

                InlineKeyboardButton(
                    "English",
                    callback_data="lang_en"
                ),
            ],

            [
                InlineKeyboardButton(
                    "Bilingual",
                    callback_data="lang_bi"
                )
            ],

        ]
    )


# =========================================================
# BASIC HELP
# =========================================================

async def deny(update: Update):

    await update.effective_message.reply_text(
        "यह सुविधा केवल Owner और authorized Admins के लिए है।"
    )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    if not is_authorized(
        update.effective_user.id
    ):

        await update.effective_message.reply_text(
            "ECA Quiz Maker में आपका स्वागत है।\n\n"
            "Quiz generation केवल Owner/authorized Admins के लिए उपलब्ध है।"
        )

        return

    await update.effective_message.reply_text(
        "ECA Quiz Maker\n\n"
        "Mode चुनें:",
        reply_markup=main_menu()
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()

    await update.effective_message.reply_text(
        "Current operation cancelled."
    )


# =========================================================
# ADMIN COMMANDS
# =========================================================

async def addadmin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != OWNER_ID:

        await deny(update)

        return

    if (
        not context.args
        or not context.args[0].lstrip("-").isdigit()
    ):

        await update.effective_message.reply_text(
            "Use:\n/addadmin NUMERIC_USER_ID"
        )

        return

    user_id = int(
        context.args[0]
    )

    add_admin(
        user_id,
        update.effective_user.id
    )

    await update.effective_message.reply_text(
        f"Admin authorized successfully.\n\nID: {user_id}"
    )


async def deladmin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != OWNER_ID:

        await deny(update)

        return

    if (
        not context.args
        or not context.args[0].lstrip("-").isdigit()
    ):

        await update.effective_message.reply_text(
            "Use:\n/deladmin NUMERIC_USER_ID"
        )

        return

    user_id = int(
        context.args[0]
    )

    if remove_admin(user_id):

        await update.effective_message.reply_text(
            f"Admin removed successfully.\n\nID: {user_id}"
        )

    else:

        await update.effective_message.reply_text(
            "Owner को remove नहीं किया जा सकता।"
        )


async def admins(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user.id != OWNER_ID:

        await deny(update)

        return

    conn = get_db()

    rows = conn.execute(
        "SELECT user_id FROM admins ORDER BY user_id"
    ).fetchall()

    conn.close()

    ids = sorted(
        set(
            [OWNER_ID]
            +
            [row[0] for row in rows]
        )
    )

    text = "Authorized Admin IDs:\n\n"

    text += "\n".join(
        str(x)
        for x in ids
    )

    await update.effective_message.reply_text(
        text
    )


# =========================================================
# MODE COMMANDS
# =========================================================

async def manual_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        await deny(update)

        return

    context.user_data.clear()

    context.user_data["mode"] = "manual"

    await update.effective_message.reply_text(
        "Manual Quiz Mode\n\n"

        "अपने questions इस format में भेजें:\n\n"

        "Q: Question\n"
        "A: Option 1\n"
        "B: Option 2\n"
        "C: Option 3\n"
        "D: Option 4\n"
        "ANS: A\n"
        "EXP: Explanation\n\n"

        "एक से अधिक questions भी भेज सकते हैं।"
    )


async def auto_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        await deny(update)

        return

    await update.effective_message.reply_text(
        "Automatic Mode:",
        reply_markup=InlineKeyboardMarkup(
            [

                [
                    InlineKeyboardButton(
                        "Source भेजें",
                        callback_data="auto_source"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "AI खुद Source खोजे",
                        callback_data="auto_search"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "Book/Page Photo OCR",
                        callback_data="ocr"
                    )
                ],

            ]
        )
    )


async def source_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        await deny(update)

        return

    context.user_data.clear()

    context.user_data["mode"] = "source"

    await update.effective_message.reply_text(
        "अब source भेजें:\n\n"
        "• Text\n"
        "• PDF\n"
        "• Document\n"
        "• Book/Page Photo"
    )


async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        await deny(update)

        return

    context.user_data.clear()

    context.user_data["mode"] = "search"

    await update.effective_message.reply_text(
        "Topic भेजें।\n\n"
        "AI reliable/official sources खोजकर "
        "उस topic पर original questions बनाएगा।"
    )


async def ocr_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        await deny(update)

        return

    context.user_data.clear()

    context.user_data["mode"] = "ocr"

    await update.effective_message.reply_text(
        "अब book/page की clear photo भेजें।"
    )


# =========================================================
# AI PROMPT
# =========================================================

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

                    "topic": {
                        "type": "string"
                    },

                },

                "required": [
                    "question",
                    "options",
                    "correct_index",
                    "explanation",
                    "topic"
                ],

            }
        }

    },

    "required": [
        "questions"
    ],
}


def create_prompt(
    content,
    language,
    count,
    mode
):

    previous = get_previous_questions()

    previous_text = "\n".join(
        f"- {question} [topic: {topic}]"
        for question, topic
        in previous[:250]
    )

    if not previous_text:

        previous_text = (
            "(No previous ECA questions available.)"
        )

    language_rule = {

        "hi":
        "Question, options और explanation स्पष्ट Hindi में लिखो.",

        "en":
        "Write question, options and explanation in clear English.",

        "bi":
        "Question और options Hindi + English bilingual format में लिखो. "
        "Explanation concise रखो.",

    }[language]

    if mode == "search":

        source_rule = """

Use Google Search grounding.

Prefer:
• Government websites
• Official reports
• Constitutional/legal texts
• Parliament/Ministry sources
• RBI/SEBI/UPSC/ECI/UN/World Bank etc. official sources
• Other authoritative primary sources

Do not invent facts.

If information cannot be verified confidently,
DO NOT create a question from it.
"""

    else:

        source_rule = """

Use only the supplied source/content.

Do not introduce unrelated facts.

Do not invent missing information.
"""

    return f"""

You are the senior question setter for
ETERNAL CIVIL ACADEMY (ECA).

Generate up to {count} ORIGINAL competitive-exam MCQs.

LANGUAGE:
{language_rule}

STRICT ECA QUESTION RULES:

1. Every question must be ORIGINAL.

2. Never copy an existing question from the source.

3. Never create a duplicate merely by changing wording.

4. Avoid questions substantially similar to previous ECA questions.

5. From ONE narrow topic/concept,
   maximum 1-2 questions.

6. Prefer different:
   • subtopics
   • concepts
   • dimensions
   • analytical angles

7. Exactly 4 options.

8. Exactly ONE correct option.

9. Reject ambiguous questions.

10. Reject doubtful or poorly supported facts.

11. Do not create trivia merely to reach the requested count.

12. UPSC/PCS/State PCS level quality should be preferred.

13. The explanation must:
   • first explain why the correct answer is correct
   • then briefly tell what the other three options represent
     or why they are incorrect

14. Explanation MUST remain within 200 characters.

15. Do not mention these internal rules.

16. Return ONLY valid JSON.

{source_rule}

PREVIOUS ECA QUESTIONS
which MUST be avoided:

{previous_text}

SOURCE / TOPIC:

{content[:50000]}
"""


# =========================================================
# AI GENERATION
# =========================================================

async def generate_questions(
    content,
    language,
    count,
    mode
):

    prompt = create_prompt(
        content,
        language,
        min(count, MAX_QUESTIONS),
        mode
    )

    config = types.GenerateContentConfig(

        response_mime_type="application/json",

        response_schema=QUESTION_SCHEMA,

        temperature=0.7,
    )

    if mode == "search":

        config.tools = [
            types.Tool(
                google_search=types.GoogleSearch()
            )
        ]

    response = ai.models.generate_content(

        model=MODEL,

        contents=prompt,

        config=config,
    )

    data = json.loads(
        response.text
    )

    return data.get(
        "questions",
        []
    )


# =========================================================
# QUESTION VALIDATION
# =========================================================

def validate_question(question_data):

    question = str(
        question_data.get(
            "question",
            ""
        )
    ).strip()

    options = [
        str(x).strip()
        for x in question_data.get(
            "options",
            []
        )
    ]

    correct_index = question_data.get(
        "correct_index"
    )

    explanation = str(
        question_data.get(
            "explanation",
            ""
        )
    ).strip()

    topic = str(
        question_data.get(
            "topic",
            ""
        )
    ).strip()

    if not question:
        return None

    if len(question) > 300:
        return None

    if len(options) != 4:
        return None

    if any(
        not option
        or len(option) > 100
        for option in options
    ):

        return None

    normalized_options = [
        normalize(option)
        for option in options
    ]

    if len(set(normalized_options)) != 4:
        return None

    if (
        not isinstance(correct_index, int)
        or correct_index < 0
        or correct_index > 3
    ):

        return None

    if not explanation:
        return None

    question_data["question"] = question

    question_data["options"] = options

    question_data["correct_index"] = correct_index

    question_data["explanation"] = explanation[:200]

    question_data["topic"] = (
        topic
        if topic
        else "General"
    )

    return question_data


# =========================================================
# PUBLISH QUIZZES
# =========================================================

async def publish_questions(
    update,
    questions,
    source
):

    published = 0

    topic_counter = {}

    for raw_question in questions:

        question = validate_question(
            raw_question
        )

        if not question:
            continue

        topic_key = normalize(
            question["topic"]
        )

        topic_counter[topic_key] = (
            topic_counter.get(
                topic_key,
                0
            ) + 1
        )

        # Maximum 2 questions from one narrow topic
        if topic_counter[topic_key] > 2:
            continue

        # Exact duplicate protection
        if not save_question(
            question,
            update.effective_user.id,
            source
        ):

            continue

        try:

            await update.effective_chat.send_poll(

                question=question["question"],

                options=question["options"],

                type="quiz",

                correct_option_ids=[
                    question["correct_index"]
                ],

                is_anonymous=False,

                explanation=question["explanation"],

                description=SOURCE_LINE,

            )

            published += 1

        except Exception as error:

            log.exception(
                "send_poll failed: %s",
                error
            )

    await update.effective_message.reply_text(

        "ECA Quiz generation complete.\n\n"

        f"Published: {published}\n\n"

        f"{SOURCE_LINE}"
    )


# =========================================================
# MANUAL QUESTION PARSER
# =========================================================

def parse_manual_questions(text):

    blocks = re.split(
        r"\n\s*\n+",
        text.strip()
    )

    result = []

    for block in blocks:

        q = re.search(
            r"(?im)^Q:\s*(.+)$",
            block
        )

        options = re.findall(
            r"(?im)^[A-D]:\s*(.+)$",
            block
        )

        answer = re.search(
            r"(?im)^ANS:\s*([A-D])\s*$",
            block
        )

        explanation = re.search(
            r"(?im)^EXP:\s*(.+)$",
            block
        )

        if (
            not q
            or len(options) != 4
            or not answer
        ):

            continue

        result.append({

            "question":
                q.group(1).strip(),

            "options":
                [
                    option.strip()
                    for option in options
                ],

            "correct_index":
                ord(
                    answer.group(1).upper()
                ) - 65,

            "explanation":
                (
                    explanation.group(1).strip()
                    if explanation
                    else
                    "Answer supplied by ECA Admin."
                ),

            "topic":
                "Manual",

        })

    return result


# =========================================================
# CALLBACKS
# =========================================================

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if not is_authorized(
        query.from_user.id
    ):

        await query.edit_message_text(
            "यह सुविधा केवल Owner/authorized Admins के लिए है।"
        )

        return

    data = query.data


    # -------------------------
    # AUTOMATIC
    # -------------------------

    if data == "mode_auto":

        await query.edit_message_text(

            "Automatic Mode चुनें:",

            reply_markup=InlineKeyboardMarkup(

                [

                    [
                        InlineKeyboardButton(
                            "Source भेजें",
                            callback_data="auto_source"
                        )
                    ],

                    [
                        InlineKeyboardButton(
                            "AI खुद Source खोजे",
                            callback_data="auto_search"
                        )
                    ],

                    [
                        InlineKeyboardButton(
                            "Book/Page Photo OCR",
                            callback_data="ocr"
                        )
                    ],

                ]
            )
        )

        return


    # -------------------------
    # MANUAL
    # -------------------------

    if data == "mode_manual":

        context.user_data.clear()

        context.user_data["mode"] = "manual"

        await query.edit_message_text(

            "Manual Quiz Mode\n\n"

            "Format:\n\n"

            "Q: Question\n"
            "A: Option 1\n"
            "B: Option 2\n"
            "C: Option 3\n"
            "D: Option 4\n"
            "ANS: A\n"
            "EXP: Explanation"
        )

        return


    # -------------------------
    # SOURCE
    # -------------------------

    if data == "auto_source":

        context.user_data.clear()

        context.user_data["mode"] = "source"

        await query.edit_message_text(

            "अब source भेजें:\n\n"
            "Text / PDF / Document / Photo"
        )

        return


    # -------------------------
    # AI SEARCH
    # -------------------------

    if data == "auto_search":

        context.user_data.clear()

        context.user_data["mode"] = "search"

        await query.edit_message_text(

            "जिस topic पर AI reliable sources "
            "खोजे, वह topic भेजें।"
        )

        return


    # -------------------------
    # OCR
    # -------------------------

    if data == "ocr":

        context.user_data.clear()

        context.user_data["mode"] = "ocr"

        await query.edit_message_text(

            "अब book/page की clear photo भेजें।"
        )

        return


    # -------------------------
    # LANGUAGE
    # -------------------------

    if data.startswith("lang_"):

        language = data.split(
            "_",
            1
        )[1]

        context.user_data["language"] = language

        content = context.user_data.get(
            "content"
        )

        count = context.user_data.get(
            "count"
        )

        if content and count:

            await query.edit_message_text(
                "AI question generation शुरू हो गया है…"
            )

            try:

                mode = context.user_data.get(
                    "source_mode",
                    context.user_data.get(
                        "mode",
                        "source"
                    )
                )

                questions = await generate_questions(

                    content,

                    language,

                    count,

                    mode
                )

                source = (

                    "Google Search grounded sources"

                    if mode == "search"

                    else

                    "ECA supplied source"
                )

                await publish_questions(

                    update,

                    questions,

                    source
                )

            except Exception as error:

                log.exception(
                    "generation failed: %s",
                    error
                )

                await query.edit_message_text(

                    "AI generation में error आया।\n\n"
                    "Render logs और GEMINI_API_KEY check करें।"
                )

            finally:

                context.user_data.clear()

        else:

            await query.edit_message_text(
                "Content/topic भेजें।"
            )


# =========================================================
# TEXT HANDLER
# =========================================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        return

    text = (
        update.effective_message.text
        or ""
    ).strip()

    mode = context.user_data.get(
        "mode"
    )


    # -------------------------
    # MANUAL
    # -------------------------

    if mode == "manual":

        questions = parse_manual_questions(
            text
        )

        if not questions:

            await update.effective_message.reply_text(

                "Question format समझ नहीं आया।\n\n"

                "Q: Question\n"
                "A: Option 1\n"
                "B: Option 2\n"
                "C: Option 3\n"
                "D: Option 4\n"
                "ANS: A\n"
                "EXP: Explanation"
            )

            return

        await publish_questions(

            update,

            questions,

            "Admin supplied questions"
        )

        context.user_data.clear()

        return


    # -------------------------
    # SOURCE / SEARCH
    # -------------------------

    if mode in (
        "source",
        "search"
    ):

        context.user_data["content"] = text

        context.user_data["source_mode"] = mode

        await update.effective_message.reply_text(

            "कितने questions चाहिए?\n\n"
            "1 से 20 के बीच संख्या भेजें।"
        )

        context.user_data[
            "await_count"
        ] = True

        return


    # -------------------------
    # COUNT
    # -------------------------

    if context.user_data.get(
        "await_count"
    ):

        if (
            not text.isdigit()
            or not 1 <= int(text) <= MAX_QUESTIONS
        ):

            await update.effective_message.reply_text(
                "1 से 20 के बीच संख्या भेजें।"
            )

            return

        context.user_data[
            "count"
        ] = int(text)

        context.user_data[
            "await_count"
        ] = False

        await update.effective_message.reply_text(

            "Language चुनें:",

            reply_markup=language_menu()
        )

        return


    await update.effective_message.reply_text(
        "नई quiz बनाने के लिए /start दबाएँ।"
    )


# =========================================================
# FILE / PHOTO PROCESSING
# =========================================================

async def process_file_with_gemini(

    update,

    context,

    local_path
):

    mode = context.user_data.get(
        "mode",
        "source"
    )

    try:

        uploaded = ai.files.upload(
            file=local_path
        )

        extraction_prompt = """

Read this educational source carefully.

Extract the relevant educational content faithfully.

Do NOT invent missing text.

Preserve:
• facts
• dates
• names
• definitions
• concepts
• relationships
• tables where meaningful

Create a clean textual representation that can be used
for high-quality competitive-exam MCQ generation.
"""

        response = ai.models.generate_content(

            model=MODEL,

            contents=[
                uploaded,
                extraction_prompt
            ]
        )

        context.user_data[
            "content"
        ] = response.text

        context.user_data[
            "source_mode"
        ] = mode

        await update.effective_message.reply_text(

            "Source/OCR content successfully read.\n\n"

            "अब कितने questions चाहिए?\n"
            "1 से 20 के बीच संख्या भेजें।"
        )

        context.user_data[
            "await_count"
        ] = True

    except Exception as error:

        log.exception(
            "File processing failed: %s",
            error
        )

        await update.effective_message.reply_text(

            "File/OCR processing में error आया।\n\n"
            "कृपया clear photo/PDF भेजकर फिर कोशिश करें।"
        )


async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        return

    if context.user_data.get(
        "mode"
    ) not in (
        "source",
        "ocr"
    ):

        await update.effective_message.reply_text(
            "पहले /source या /ocr चुनें।"
        )

        return

    photo = update.effective_message.photo[-1]

    telegram_file = await context.bot.get_file(
        photo.file_id
    )

    path = (
        f"/tmp/eca_"
        f"{update.effective_user.id}_"
        f"{photo.file_unique_id}.jpg"
    )

    await telegram_file.download_to_drive(
        path
    )

    await process_file_with_gemini(
        update,
        context,
        path
    )

    try:

        Path(path).unlink(
            missing_ok=True
        )

    except Exception:
        pass


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not is_authorized(
        update.effective_user.id
    ):

        return

    if context.user_data.get(
        "mode"
    ) != "source":

        await update.effective_message.reply_text(
            "पहले /source चुनें।"
        )

        return

    document = (
        update.effective_message.document
    )

    telegram_file = await context.bot.get_file(
        document.file_id
    )

    suffix = (
        Path(
            document.file_name
            or "source.bin"
        ).suffix
        or ".bin"
    )

    path = (
        f"/tmp/eca_"
        f"{update.effective_user.id}_"
        f"{document.file_unique_id}"
        f"{suffix}"
    )

    await telegram_file.download_to_drive(
        path
    )

    await process_file_with_gemini(
        update,
        context,
        path
    )

    try:

        Path(path).unlink(
            missing_ok=True
        )

    except Exception:
        pass


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        body = b"ECA Quiz Maker is running"

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def log_message(
        self,
        format,
        *args
    ):

        return


def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            port
        ),
        HealthHandler
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True
    )

    thread.start()

    log.info(
        "Health server listening on port %s",
        port
    )


# =========================================================
# BOT STARTUP
# =========================================================

async def post_init(
    application
):

    # Prevent Telegram getUpdates conflict
    # when an old webhook exists.

    await application.bot.delete_webhook(
        drop_pending_updates=True
    )

    me = await application.bot.get_me()

    log.info(
        "ECA Quiz Maker started as @%s",
        me.username
    )


# =========================================================
# MAIN
# =========================================================

def main():

    start_health_server()

    application = (

        Application.builder()

        .token(
            BOT_TOKEN
        )

        .post_init(
            post_init
        )

        .build()
    )


    # Commands

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel
        )
    )

    application.add_handler(
        CommandHandler(
            "addadmin",
            addadmin
        )
    )

    application.add_handler(
        CommandHandler(
            "deladmin",
            deladmin
        )
    )

    application.add_handler(
        CommandHandler(
            "admins",
            admins
        )
    )

    application.add_handler(
        CommandHandler(
            "manual",
            manual_command
        )
    )

    application.add_handler(
        CommandHandler(
            "auto",
            auto_command
        )
    )

    application.add_handler(
        CommandHandler(
            "source",
            source_command
        )
    )

    application.add_handler(
        CommandHandler(
            "search",
            search_command
        )
    )

    application.add_handler(
        CommandHandler(
            "ocr",
            ocr_command
        )
    )


    # Buttons

    application.add_handler(
        CallbackQueryHandler(
            callback
        )
    )


    # Photo

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo
        )
    )


    # Documents

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document
        )
    )


    # Text

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )


    log.info(
        "ECA Quiz Maker Bot is running..."
    )


    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":

    main()
