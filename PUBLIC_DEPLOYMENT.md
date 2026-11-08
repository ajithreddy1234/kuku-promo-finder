# Public deployment

The hosted app runs the same React/FastAPI source and evidence pipeline. No Ollama process is needed on the server. No user account registration is required. Browser sessions isolate project listings, source, exports and model-response caches.

## Render Free

Create a Web Service from this repository. Select Docker, the `main` branch, and **Free**. The repository's Dockerfile builds React and serves it through FastAPI. Health check: `/api/health`.

Set these environment variables in Render:

- `PROMO_PUBLIC=1`
- `PROMO_SHARED_KEY=1` when deliberately sharing the owner's Groq allowance
- `GROQ_API_KEY` as a Render secret, never a repository value or a Vite variable

With shared mode disabled, visitors can enter a Groq key for their own session. Keys entered in the UI remain in server memory; the shared owner key is a server environment variable. Changing or adding keys does not increase an organization's rate limits.

The public app starts empty. No supplied company scripts, saved company cuts, raw company responses or local `.env` are included in the Docker image or repository. Only invented source examples are included for format testing; no example is automatically loaded.

New uploads always open the processing step with no inherited cut results. The app and its cut exports use only the selected run's completed reviews. Historical exact-source cut lookup is not enabled by any app endpoint. Public request caches are scoped to the individual upload/run, so uploading identical files again does not replay another upload's model responses. Resuming the same run retains its own successful work. The progress display separates episodes analyzed, audience reviews and source checks; zero reviews cannot be presented as completed promo verification.

## Storage and clear

The prototype uses private ephemeral files, not an external database. Every project belongs to an opaque HttpOnly browser session. Other sessions cannot enumerate or fetch it. Clear-session deletes that session's project files and response cache, while retaining usage accounting. Download cut sheets and private raw outputs before clearing. Render's free filesystem is lost on restart/redeploy/idle spin-down; it is not durable storage.

## Limits

Free hosting does not make inference unlimited. The owner approved a shared Groq key, so all shared-key users consume the same organization allowance. Processing is bounded and resumable while this server instance survives. One AI job is active at a time on this free prototype. Large runs may pause; incomplete candidates are not promoted to approved cuts. Full multi-worker durability and paid/high-capacity inference are future scale work.

The brief asks for promo moments and boundaries, not rendered video. Downloads contain exact episode/time ranges and selected screenplay text with original AI/editor statuses.
