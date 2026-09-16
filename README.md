# ECA Quiz Maker Bot

Production-oriented Telegram Quiz Maker for Eternal Civil Academy.

## Required environment variables

- `BOT_TOKEN`
- `OWNER_USER_ID`
- `GEMINI_API_KEY`

Optional:

- `GEMINI_MODEL`
- `GEMINI_FALLBACK_MODELS`
- `QUIZ_DURATION_MINUTES`
- `DATABASE_URL`
- `PORT`

## Render start command

```text
python bot.py
```

## Main user flow

- AI à¤–à¥à¤¦ Questions Generate à¤•à¤°à¥‡
- à¤®à¥ˆà¤‚ à¤–à¥à¤¦ Source à¤¦à¥‚à¤à¤—à¤¾
  - PDF
  - Photo
  - Text
  - Telegram Poll
  - URL

## Important

For persistent admin/question/quiz data on Render, configure a persistent
PostgreSQL `DATABASE_URL`. The code falls back to SQLite for local testing.
