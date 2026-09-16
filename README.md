# ECA Quiz Maker Bot

Production-oriented Telegram quiz maker for Eternal Civil Academy.

## Final flow

1. `/start`
2. Choose:
   - ðŸ¤– AI à¤–à¥à¤¦ Questions Generate à¤•à¤°à¥‡
   - ðŸ“š à¤®à¥ˆà¤‚ à¤–à¥à¤¦ Source à¤¦à¥‚à¤à¤—à¤¾
3. AI mode: AI finds authoritative sources and verifies facts.
4. Source mode accepts PDF, photo, text, Telegram poll, or URL.
5. Choose topic, question count, and language:
   - à¤¹à¤¿à¤‚à¤¦à¥€
   - English
   - Bilingual
6. Bot generates and validates the questions but **does not publish them all at once**.
7. Bot shows a quiz-ready summary and link.
8. Choose:
   - ðŸ‘¤ Personally
   - ðŸ‘¥ Group
9. Choose time per question:
   - 15 à¤¸à¥‡à¤•à¤‚à¤¡
   - 25 à¤¸à¥‡à¤•à¤‚à¤¡
   - 30 à¤¸à¥‡à¤•à¤‚à¤¡
   - 1 à¤®à¤¿à¤¨à¤Ÿ
10. The bot sends exactly one Telegram quiz poll at a time. When its timer ends, the next question is sent automatically.
11. At completion, the bot sends the result/leaderboard.

## Environment variables

- `BOT_TOKEN`
- `OWNER_USER_ID`
- `GEMINI_API_KEY`
- `GEMINI_MODEL` (optional)
- `GEMINI_FALLBACK_MODELS` (optional)
- `DATABASE_URL` (optional; SQLite default)
- `PORT` (optional; Render default is usually supplied)

## Render

Build command:
```text
pip install -r requirements.txt
```

Start command:
```text
python bot.py
```

## Admin

Owner can authorize another Telegram user without putting IDs into source code:

- Reply to the user's message with `/addadmin`
- `/removeadmin` by replying to an admin message
- `/admins`
- `/whoami`
