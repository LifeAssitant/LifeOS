# LifeOS Backend

Calm AI LifeManager API — FastAPI + Supabase Postgres + Gemini.

## Quick start

```bash
cp .env.example .env
# Fill SUPABASE_DATABASE_URL (Session Pooler for Docker), JWT_SECRET, FERNET_KEY,
# SUPABASE_JWT_SECRET, and optionally Google / Gemini / Razorpay keys

python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Start the production-mode API and reminder worker against Supabase.
# Configure production APP secrets and CORS_ORIGINS in .env first.
docker compose up -d --build api worker

# Optional local Postgres rollback/source:
docker compose --profile local-db up -d db

python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# separate terminal — reminders
python -m app.workers.reminder_worker
```

Use `--host 0.0.0.0` so Expo Go on your phone can reach the API over Wi‑Fi.

API docs: http://localhost:8000/docs

## Windows host + Cloudflare Tunnel

The API container publishes only to the Windows host's loopback interface on
`API_PORT` (default `8001`). In the Cloudflare dashboard, add a **Published
application** route to the existing tunnel:

| Setting | Value |
|---------|-------|
| Hostname | `lifeos-api.brainlesshuman.com` |
| Service | `http://127.0.0.1:8001` |

If you change `API_PORT` in `.env`, change the route's service port to match.
Keep the API bound to `127.0.0.1`; do not expose its port to the LAN or internet.
Cloudflare Tunnel reaches the loopback port through the Windows `cloudflared`
service, so no router port forwarding is required.

On this Windows PC, confirm the `cloudflared` service is set to **Automatic**.
Start the API and worker once with the Compose command above. Both containers
have a `unless-stopped` restart policy and will start again when Docker starts.
In Docker Desktop, enable **Start Docker Desktop when you sign in to your
computer**. This starts the stack after Windows sign-in; Docker Desktop is not
running before a user signs in.

For future products, add each container and its loopback-only host port to the
central Compose deployment, then add a corresponding Published application
route in Cloudflare. Keep this table as the hostname-to-port inventory:

| Product | Hostname | Compose service | Host port |
|---------|----------|-----------------|-----------|
| LifeOS | `lifeos-api.brainlesshuman.com` | `api` | `127.0.0.1:8001` |

Use the one-level `lifeos-api.brainlesshuman.com` hostname for HTTPS clients.
The former `api.lifeos.brainlesshuman.com` route is not used.

The interactive API documentation is available only in development. Production
startup requires `DEBUG=false`, a strong `JWT_SECRET`, a valid Fernet key,
PostgreSQL, and explicit HTTPS `CORS_ORIGINS`. Set
`TRACE_CAPTURE_CONTENT=true` only after approving the privacy implications;
the default is not to export chat content in traces.
Paid checkout is disabled by default (`BILLING_ENABLED=false`). Web and desktop
checkout use Razorpay monthly subscriptions and one-time credit top-ups. Configure
the dashboard plan ID, INR prices, credit quantities, API credentials, and webhook
secret before enabling checkout. Subscription credits refresh weekly and do not
roll over; paid top-up credits do not expire. Do not enable billing until those
terms are final and displayed in the clients.
Configure the Razorpay webhook at
`https://lifeos-api.brainlesshuman.com/api/v1/billing/webhook` for
`payment_link.paid`, `subscription.authenticated`, `subscription.activated`,
`subscription.charged`, `subscription.cancelled`, and `subscription.halted`.
Also enable `subscription.pending` to process failed-renewal state changes.
Web and Windows desktop purchases use Razorpay. Android offers one-time credit
top-ups through Google Play Billing; mobile subscriptions are not offered.
Before enabling Android top-ups, create and activate the managed product in Play
Console, grant the configured service account Android Publisher API access, and
set `GOOGLE_PLAY_BILLING_ENABLED`, `GOOGLE_PLAY_PACKAGE_NAME`,
`GOOGLE_PLAY_TOPUP_PRODUCT_ID`, `GOOGLE_PLAY_TOPUP_CREDITS`,
`GOOGLE_PLAY_SERVICE_ACCOUNT_EMAIL`, and
`GOOGLE_PLAY_SERVICE_ACCOUNT_PRIVATE_KEY` in the backend secret store. The
package name must match the Android release and the product ID must match Play
Console. Test with Play license testers before release.
Email/password recovery is implemented through the static landing page. Configure
the SMTP settings in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, and
provider credentials where required) before launch. Reset links expire after
30 minutes, are single-use, and password changes invalidate existing LifeOS
sessions. Requests have a per-account cooldown and a basic per-process IP limit;
configure equivalent edge/provider rate limits for multi-worker deployments.
Account creation and sign-in require an 18+ self-declaration; the apps do not
collect a date of birth. This policy still requires qualified local legal review.
For production, set `CORS_ORIGINS` to `["https://brainlesshuman.com","null"]`;
the `null` origin is needed by the packaged Electron file renderer.
Database startup removes the legacy onboarding age column and its stored values.
Users can delete their LifeOS account from app settings. Configure
`SUPABASE_SERVICE_ROLE_KEY` as a backend-only secret to also remove linked
Supabase Auth identities; failed provider deletion leaves LifeOS data intact.

## Supabase + Google setup

1. **Create a Supabase project** → Project Settings → Database → copy the connection string.  
   Copy the Session Pooler URI (port 5432) and set `SUPABASE_DATABASE_URL` for Docker.
   URL-encode special characters in the password. For running FastAPI outside Docker,
   set `DATABASE_URL` to the same URI.

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
| `app/core` | JWT auth, Fernet encryption, rate limit |
| `app/models` | SQLAlchemy models |
| `app/schemas` | Pydantic DTOs |
| `app/services` | Domain logic (incl. Google Calendar sync) |
| `app/workers` | Reminder scanner with a PostgreSQL advisory lock |

## AI modes

- **hosted** — server `GEMINI_API_KEY`, deducts `credit_balance`
- **byok** — user key encrypted with `FERNET_KEY`, no credit charge
