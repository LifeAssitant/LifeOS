# LifeOS Backend

Calm AI LifeManager API — FastAPI + PostgreSQL + Redis + Gemini.

## Quick start

```bash
cp .env.example .env
# Set JWT_SECRET, FERNET_KEY, and optionally GEMINI_API_KEY / Stripe keys

docker compose up -d db redis
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# separate terminal — reminders (needs Redis)
python -m app.workers.reminder_worker
```

Use `--host 0.0.0.0` so Expo Go on your phone can reach the API over Wi‑Fi.

API docs: http://localhost:8000/docs

## Modules

| Path | Role |
|------|------|
| `app/api` | HTTP routers (auth, tasks, events, today, chat, billing, notifications) |
| `app/core` | JWT auth, Fernet encryption, rate limit, Redis client |
| `app/models` | SQLAlchemy models |
| `app/schemas` | Pydantic DTOs |
| `app/services` | Domain logic |
| `app/workers` | Reminder scanner with Redis lock |

## AI modes

- **hosted** — server `GEMINI_API_KEY`, deducts `credit_balance`
- **byok** — user key encrypted with `FERNET_KEY`, no credit charge
