# LifeOS Backend

Calm AI LifeManager API — FastAPI + Supabase Postgres + Redis + Gemini.

## Quick start

```bash
cp .env.example .env
# Fill DATABASE_URL (Supabase), JWT_SECRET, FERNET_KEY,
# SUPABASE_JWT_SECRET, and optionally Google / Gemini / Stripe keys

python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Redis still used for reminder locks (local docker is fine)
docker compose up -d redis

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# separate terminal — reminders
python -m app.workers.reminder_worker
```

Use `--host 0.0.0.0` so Expo Go on your phone can reach the API over Wi‑Fi.

API docs: http://localhost:8000/docs

## Supabase + Google setup

1. **Create a Supabase project** → Project Settings → Database → copy the connection string.  
   Convert it to asyncpg form, e.g.  
   `postgresql+asyncpg://postgres.YOUR_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres`  
   and set `DATABASE_URL`.

2. **API keys** → Project Settings → API: copy the **JWT Secret** into `SUPABASE_JWT_SECRET`, and the project URL into `SUPABASE_URL`.

3. **Auth → Providers → Google**: enable Google. Create an OAuth client in [Google Cloud Console](https://console.cloud.google.com/) (Web application). Add the Supabase callback URL shown in the dashboard. Put the same Client ID/Secret in Supabase and in LifeOS `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

4. **Redirect URLs** (Supabase Auth): add `lifeos://auth/callback`.

5. **Calendar API**: enable Google Calendar API on the same Google Cloud project. Add authorized redirect URI:  
   `http://127.0.0.1:8000/api/v1/calendar/google/callback`  
   (`GOOGLE_CALENDAR_REDIRECT_URI`). Desktop success redirect: `lifeos://auth/calendar-connected`.

6. On first API boot, tables are created automatically. Existing local Postgres DBs may need a recreate or the startup schema patches will alter columns.

## Auth

- `POST /auth/register` / `POST /auth/login` — email + password → LifeOS JWTs  
- `POST /auth/google` — body `{ "access_token": "<supabase access jwt>" }` → same LifeOS JWTs  
- Google Calendar: `/calendar/google/connect|callback|sync` + `DELETE /calendar/google`

## Modules

| Path | Role |
|------|------|
| `app/api` | HTTP routers (auth, tasks, events, today, chat, billing, calendar, notifications) |
| `app/core` | JWT auth, Fernet encryption, rate limit, Redis client |
| `app/models` | SQLAlchemy models |
| `app/schemas` | Pydantic DTOs |
| `app/services` | Domain logic (incl. Google Calendar sync) |
| `app/workers` | Reminder scanner with Redis lock |

## AI modes

- **hosted** — server `GEMINI_API_KEY`, deducts `credit_balance`
- **byok** — user key encrypted with `FERNET_KEY`, no credit charge
