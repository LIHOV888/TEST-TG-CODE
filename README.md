# Telegram Video Downloader Bot

This bot downloads Facebook/TikTok videos with a clean Telegram dashboard UI.

## Environment Variables

Create `.env` locally:

```env
BOT_TOKEN=your_telegram_bot_token
OWNER_CHAT_ID=7092765705
# Alternative formats:
# OWNER_USER_ID=7092765705
# OWNER_IDS=7092765705,-1001234567890
# Optional local single-instance lock port
BOT_LOCK_PORT=47200
```

Notes:
- `OWNER_CHAT_ID` optional.
- If set: owner commands/UI are enabled (`/owner`, `/panel`, `/broadcast`).
- If not set: bot still works for all users, but owner panel is disabled.
- Use `/id` in Telegram to see your current User ID and Chat ID.
- If owner commands will be used in a group, `OWNER_CHAT_ID` must be that group chat ID (often a negative number).

## User Dashboard (All Users)

Commands:
- `/start` or `/menu`: open user dashboard
- `/help`: show help
- `/id`: show your current Telegram user/chat ID and owner match

Dashboard features:
- Per-user quality options (`Best`, `HD 720p`, `SD 480p`)
- Settings saved by user ID (kept in `bot_state.pkl`)
- If user does not change settings, same option is used for next downloads

Accepted input:
- Text URL only
- Supported domains: `facebook.com`, `fb.watch`, `tiktok.com`

Unsupported input (image/voice/video/file/wrong text) gets a clear error message.

## Owner Dashboard

Set `OWNER_CHAT_ID`, then use from owner chat:
- `/owner` or `/panel`: open owner panel
- Pause/resume users from inline buttons
- `/broadcast your message`: send message to known users

## Local Run (Windows)

```powershell
cd Bot-Telegram-main
.\run_local.ps1
```

`run_local.ps1` will:
- stop duplicate `bot.py` processes
- ensure `.venv` exists
- install `requirements.txt`
- run one bot instance

## Render Deploy

Use **Web Service** or **Background Worker**.

### Option A: Render Web Service
1. Push project to GitHub/GitLab.
2. Create **Web Service** on Render.
3. Runtime: Docker (use this repo `Dockerfile`).
4. Set env vars:
   - `BOT_TOKEN`
   - `OWNER_CHAT_ID` (optional)
5. Deploy.

App exposes health endpoint on `PORT`:
- `/`
- `/healthz`

### Option B: Render Background Worker
1. Create **Background Worker**.
2. Runtime: Docker.
3. Set env vars:
   - `BOT_TOKEN`
   - `OWNER_CHAT_ID` (optional)
4. Deploy.

## Important

Telegram allows one active polling process per token.

Run in one place only per token:
- local machine, or
- Render service

If both run with same token, Telegram returns `409 Conflict`.
