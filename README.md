# ECA Quiz Maker Bot â€” Final

Production-oriented Telegram quiz bot for Eternal Civil Academy.

## Required Render Environment Variables

- `BOT_TOKEN`
- `OWNER_USER_ID`
- `GEMINI_API_KEY`

Recommended for quota failover:
- `GEMINI_API_KEYS` â€” comma-separated Gemini keys that are actually usable under separate quotas/projects.
- `DATABASE_URL` â€” a persistent PostgreSQL connection string on Render/another persistent DB provider.

Optional:
- `GEMINI_MODEL` (default `gemini-3.8-flash`)
- `GEMINI_FALLBACK_MODELS` (default `gemini-3.1-flash-lite,gemini-3.5-flash`)
- `PORT` (Render supplies this automatically)

## Render Start Command

```text
python bot.py
```

No `main.py` is required.

## Important

A Gemini `429 RESOURCE_EXHAUSTED` error is a provider quota/rate-limit problem. A different model in the same exhausted project does not create new quota. This version avoids repeated retries, supports multiple configured keys, and never fabricates questions to hide a quota failure.

For persistent Render data, set `DATABASE_URL` to PostgreSQL. SQLite is kept as a local/testing fallback.

## Final quiz workflow

Main menu has exactly two options:

1. `ðŸ¤– AI à¤–à¥à¤¦ Questions Generate à¤•à¤°à¥‡`
2. `ðŸ“š à¤®à¥ˆà¤‚ à¤–à¥à¤¦ Source à¤¦à¥‚à¤à¤—à¤¾`

AI mode: Topic -> Count -> Language -> AI search/verification -> prepared quiz.

Source mode: PDF / Photo / Text / Telegram Poll / URL -> Topic -> Count -> Language -> prepared quiz.

After preparation, questions are not dumped as a batch. The admin chooses Personal or Group, chooses 15 sec / 25 sec / 30 sec / 1 min per question, and the bot sends one native Telegram quiz poll at a time.

Group deep-link flow asks the timer inside the target group before the first poll.
