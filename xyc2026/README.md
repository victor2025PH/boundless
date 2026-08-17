# TrustCheck Bot (信用查)

Telegram bot for credit/risk check before trading. [@xyc2026_bot](https://t.me/xyc2026_bot)

## MVP scope

- **Bot**: `/start`, `/check <id|@username|wallet>`, `/mycredit`, `/help`, Report button on result, `/stats` (admin)
- **API**: `GET /v1/check`, `GET /v1/mycredit`, `POST /v1/report`, `GET /v1/admin/stats`
- **DB**: SQLite (default), optional PostgreSQL via `DATABASE_URL`
- **Scoring**: Rule-based 0–100 score, risk level (low/medium/high/extreme), tips

## Setup

1. **Python 3.10+**

2. **Env**
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   - `BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
   - `ADMIN_TG_IDS` — your Telegram ID(s), comma-separated (for `/stats`)
   - Optional: `API_INTERNAL_KEY` for protecting `/v1/admin/stats`; Bot sends `Authorization: Bearer <key>` when calling admin API.

3. **Install**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run**
   - Terminal 1 (API):
     ```bash
     python run_api.py
     ```
   - Terminal 2 (Bot):
     ```bash
     python run_bot.py
     ```

## Security

- **Do not commit `.env` or real `BOT_TOKEN`.** Use `.env.example` as template only.
- If token was ever committed, revoke it in BotFather and use a new one.

## API examples

```bash
# Check by Telegram ID
curl "http://127.0.0.1:8000/v1/check?user_id=123456789"

# Check by username
curl "http://127.0.0.1:8000/v1/check?username=@durov"

# My credit
curl "http://127.0.0.1:8000/v1/mycredit?tg_id=123456789"

# Report
curl -X POST "http://127.0.0.1:8000/v1/report" -H "Content-Type: application/json" \
  -d '{"reporter_tg_id":111,"target_tg_id":222,"reason":"Scam"}'
```

## Next phases (from roadmap)

- **V1.1**: Subscribe alerts, feedback flow, i18n Tagalog, rate limit (Redis), audit log
- **Mini App**: Credit card UI, radar chart, link to escrow
- **AI**: Risk model, rule/model switch

## Doc

See `信用查机器人-多视角优化方案.md` for full product and tech design.
