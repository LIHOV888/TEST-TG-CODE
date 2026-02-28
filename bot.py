import asyncio
import logging
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit, urlunsplit

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)
from yt_dlp.utils import DownloadError

SUPPORTED_VIDEO_DOMAINS = (
    'facebook.com',
    'fb.watch',
    'tiktok.com',
)

MAX_TELEGRAM_BYTES = 50 * 1024 * 1024
PERSISTENCE_FILE = 'bot_state.pkl'

QUALITY_LABELS = {
    'best': 'Best',
    '720': 'HD 720p',
    '480': 'SD 480p',
}

QUALITY_FORMATS = {
    'best': 'best[ext=mp4]/best',
    '720': 'best[ext=mp4][height<=720]/best[height<=720]/best',
    '480': 'best[ext=mp4][height<=480]/best[height<=480]/best',
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
# Avoid logging full Telegram API URLs (which include bot token).
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


class YtDlpLogger:
    def debug(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        logging.warning('yt-dlp: %s', message)

    def error(self, message: str) -> None:
        logging.warning('yt-dlp: %s', message)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ('/', '/healthz'):
            body = b'ok'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def start_health_server_if_needed() -> ThreadingHTTPServer | None:
    raw_port = os.getenv('PORT')
    if not raw_port:
        return None

    try:
        port = int(raw_port)
    except ValueError:
        logging.warning('Invalid PORT value "%s". Health server disabled.', raw_port)
        return None

    server = ThreadingHTTPServer(('0.0.0.0', port), HealthHandler)
    thread = Thread(target=server.serve_forever, name='health-server', daemon=True)
    thread.start()
    logging.info('Health server listening on 0.0.0.0:%s', port)
    return server


def acquire_local_instance_lock() -> socket.socket:
    lock_port = int(os.getenv('BOT_LOCK_PORT', '47200'))
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    lock_socket.bind(('127.0.0.1', lock_port))
    lock_socket.listen(1)
    return lock_socket


def get_owner_ids() -> set[int]:
    owner_ids: set[int] = set()
    owner_sources = [
        os.getenv('OWNER_CHAT_ID', ''),
        os.getenv('OWNER_USER_ID', ''),
        os.getenv('OWNER_IDS', ''),
        os.getenv('ALLOWED_CHAT_ID', ''),
    ]

    for raw in owner_sources:
        for match in re.findall(r'-?\d+', raw or ''):
            with suppress(ValueError):
                owner_ids.add(int(match))

    return owner_ids


def get_active_owner_ids(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    env_owner_ids = get_owner_ids()
    if env_owner_ids:
        context.application.bot_data['owner_ids'] = set(env_owner_ids)
        return set(env_owner_ids)

    owner_ids = context.application.bot_data.get('owner_ids')
    normalized: set[int] = set()
    if isinstance(owner_ids, (set, list, tuple)):
        for value in owner_ids:
            with suppress(TypeError, ValueError):
                normalized.add(int(value))
    elif isinstance(owner_ids, str):
        for match in re.findall(r'-?\d+', owner_ids):
            with suppress(ValueError):
                normalized.add(int(match))

    context.application.bot_data['owner_ids'] = normalized
    return normalized


def is_owner(update: Update, owner_ids: set[int] | None) -> bool:
    if not owner_ids:
        return False

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else None
    chat_id = chat.id if chat else None
    return user_id in owner_ids or chat_id in owner_ids


def is_maintenance_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.application.bot_data.get('maintenance_mode', False))


def get_user_prefs(context: ContextTypes.DEFAULT_TYPE, user_id: int | None) -> dict[str, str]:
    all_prefs = context.application.bot_data.setdefault('user_prefs', {})
    if user_id is None:
        return {'quality': 'best'}

    prefs = all_prefs.get(user_id)
    if not isinstance(prefs, dict):
        prefs = {'quality': 'best'}

    quality = prefs.get('quality', 'best')
    if quality not in QUALITY_FORMATS:
        quality = 'best'

    normalized = {'quality': quality}
    all_prefs[user_id] = normalized
    return normalized


def set_user_quality(context: ContextTypes.DEFAULT_TYPE, user_id: int | None, quality: str) -> None:
    if user_id is None:
        return
    if quality not in QUALITY_FORMATS:
        return
    prefs = get_user_prefs(context, user_id)
    prefs['quality'] = quality
    context.application.bot_data.setdefault('user_prefs', {})[user_id] = prefs


def ensure_stats(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    stats = context.application.bot_data.get('stats')
    if not isinstance(stats, dict):
        stats = {
            'downloads_ok': 0,
            'downloads_failed': 0,
            'invalid_input': 0,
        }
        context.application.bot_data['stats'] = stats

    stats['downloads_ok'] = int(stats.get('downloads_ok', 0))
    stats['downloads_failed'] = int(stats.get('downloads_failed', 0))
    stats['invalid_input'] = int(stats.get('invalid_input', 0))
    return stats


def mark_user_seen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat:
        known_users = context.application.bot_data.setdefault('known_users', set())
        known_users.add(chat.id)

    user = update.effective_user
    if user:
        get_user_prefs(context, user.id)


def extract_valid_url(message_text: str) -> str | None:
    match = re.search(r'https?://[^\s<>"\']+', message_text, flags=re.IGNORECASE)
    if not match:
        return None

    candidate = match.group(0).strip("()[]{}<>,.;!?\"'")
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https') or not parsed.netloc:
        return None

    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def is_supported_video_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    if not host:
        return False
    return any(host == domain or host.endswith(f'.{domain}') for domain in SUPPORTED_VIDEO_DOMAINS)


def build_owner_keyboard(maintenance_mode: bool) -> InlineKeyboardMarkup:
    toggle_label = 'Resume Users' if maintenance_mode else 'Pause Users'
    toggle_action = 'owner:resume' if maintenance_mode else 'owner:pause'
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_label, callback_data=toggle_action)],
            [InlineKeyboardButton('Refresh Status', callback_data='owner:panel')],
            [InlineKeyboardButton('Show Stats', callback_data='owner:stats')],
        ]
    )


def build_user_keyboard(selected_quality: str) -> InlineKeyboardMarkup:
    def q_label(code: str) -> str:
        mark = '[x]' if selected_quality == code else '[ ]'
        return f'{mark} {QUALITY_LABELS[code]}'

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(q_label('best'), callback_data='user:q:best'),
                InlineKeyboardButton(q_label('720'), callback_data='user:q:720'),
            ],
            [
                InlineKeyboardButton(q_label('480'), callback_data='user:q:480'),
            ],
            [
                InlineKeyboardButton('Refresh', callback_data='user:panel'),
                InlineKeyboardButton('Reset', callback_data='user:reset'),
            ],
            [
                InlineKeyboardButton('How To Use', callback_data='user:help'),
            ],
        ]
    )


def format_uptime(started_at: float) -> str:
    seconds = max(0, int(time.time() - started_at))
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    return f'{hours:02d}:{minutes:02d}:{sec:02d}'


def format_bytes(num_bytes: float | int | None) -> str:
    if num_bytes is None:
        return '?'
    value = float(num_bytes)
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f'{value:.1f}{unit}' if unit != 'B' else f'{int(value)}B'
        value /= 1024
    return f'{value:.1f}TB'


def format_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return '?'
    total = max(0, int(seconds))
    mins, sec = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f'{hours:d}h {mins:02d}m'
    if mins:
        return f'{mins:d}m {sec:02d}s'
    return f'{sec:d}s'


def owner_panel_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    stats = ensure_stats(context)
    known_users = context.application.bot_data.get('known_users', set())
    maintenance = is_maintenance_mode(context)
    started_at = context.application.bot_data.get('started_at', time.time())
    owner_ids = get_active_owner_ids(context)
    owner_display = ', '.join(str(x) for x in sorted(owner_ids)) if owner_ids else 'not-configured'

    return (
        'Owner Control Panel\n'
        f'Owner ID(s): {owner_display}\n'
        f'Maintenance Mode: {"ON" if maintenance else "OFF"}\n'
        f'Known Users: {len(known_users)}\n'
        f'Download Success: {stats["downloads_ok"]}\n'
        f'Download Failed: {stats["downloads_failed"]}\n'
        f'Invalid/Unsupported Input: {stats["invalid_input"]}\n'
        f'Uptime: {format_uptime(started_at)}'
    )


def user_dashboard_text(user_id: int | None, quality: str) -> str:
    q_label = QUALITY_LABELS.get(quality, QUALITY_LABELS['best'])
    user_display = str(user_id) if user_id is not None else 'unknown'
    return (
        'Download Dashboard\n'
        f'User ID: {user_display}\n'
        f'Default Quality: {q_label}\n\n'
        'Send a Facebook/TikTok URL as text to download video.\n'
        'Your quality option is saved per user until you change it.'
    )


def user_help_text(user_id: int | None, quality: str) -> str:
    q_label = QUALITY_LABELS.get(quality, QUALITY_LABELS['best'])
    return (
        'How To Use\n'
        f'Current Quality: {q_label}\n\n'
        '1. Choose quality buttons in dashboard.\n'
        '2. Send only Facebook/TikTok video URL as text.\n'
        '3. Bot downloads and sends video file.\n\n'
        f'Your user ID: {user_id if user_id is not None else "unknown"}'
    )


async def safe_answer_query(
    update: Update,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    query = update.callback_query
    if query is None:
        return

    try:
        await query.answer(text=text, show_alert=show_alert)
    except BadRequest as exc:
        message = str(exc).lower()
        if 'query is too old' in message or 'query id is invalid' in message:
            return
        raise


async def safe_edit_query_message(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    query = update.callback_query
    if query is None:
        return

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as exc:
        message = str(exc).lower()
        # Happens when user presses the same button repeatedly.
        if 'message is not modified' in message:
            return
        # Fallback for stale/unedited messages.
        if 'message to edit not found' in message or "message can't be edited" in message:
            if query.message is not None:
                with suppress(Exception):
                    await query.message.reply_text(text=text, reply_markup=reply_markup)
            return
        raise


async def send_user_dashboard_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    prefs = get_user_prefs(context, user_id)
    await update.message.reply_text(
        user_dashboard_text(user_id, prefs['quality']),
        reply_markup=build_user_keyboard(prefs['quality']),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    if is_maintenance_mode(context) and not is_owner(update, owner_ids):
        await update.message.reply_text('Bot is temporarily paused by owner. Please try again later.')
        return

    await send_user_dashboard_message(update, context)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    if is_maintenance_mode(context) and not is_owner(update, owner_ids):
        await update.message.reply_text('Bot is temporarily paused by owner. Please try again later.')
        return

    await send_user_dashboard_message(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    lines = [
        'Commands:',
        '/start - Open user dashboard',
        '/menu - Open user dashboard',
        '/help - Show help',
        '/id - Show your user/chat ID and owner status',
        'Send a Facebook/TikTok URL as text to download video.',
    ]
    if is_owner(update, owner_ids):
        lines.extend(
            [
                '/owner or /panel - Open owner panel',
                '/broadcast <message> - Send message to known users',
            ]
        )
    await update.message.reply_text('\n'.join(lines))


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    owner_match = is_owner(update, owner_ids)
    owner_display = ', '.join(str(x) for x in sorted(owner_ids)) if owner_ids else 'not-configured'
    script_path = context.application.bot_data.get('script_path', 'unknown')
    process_id = context.application.bot_data.get('process_id', 'unknown')

    await update.message.reply_text(
        f'User ID: {user_id}\n'
        f'Chat ID: {chat_id}\n'
        f'Configured Owner ID(s): {owner_display}\n'
        f'Owner Match: {"YES" if owner_match else "NO"}\n'
        f'Process ID: {process_id}\n'
        f'Script: {script_path}'
    )


async def owner_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    if not owner_ids:
        await update.message.reply_text(
            'Owner commands are disabled. Set OWNER_CHAT_ID or OWNER_USER_ID or OWNER_IDS in .env, '
            'restart bot, then use /id.'
        )
        return
    if not is_owner(update, owner_ids):
        await update.message.reply_text('Owner-only command.')
        return

    await update.message.reply_text(
        owner_panel_text(context),
        reply_markup=build_owner_keyboard(is_maintenance_mode(context)),
    )


async def owner_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await safe_answer_query(update)

    owner_ids = get_active_owner_ids(context)
    if not owner_ids:
        await safe_edit_query_message(
            update,
            'Owner commands are disabled. Set OWNER_CHAT_ID or OWNER_USER_ID or OWNER_IDS in .env, restart bot, then use /id.',
        )
        return
    if not is_owner(update, owner_ids):
        await safe_edit_query_message(update, 'Owner-only action.')
        return

    action = query.data or ''
    if action == 'owner:pause':
        context.application.bot_data['maintenance_mode'] = True
    elif action == 'owner:resume':
        context.application.bot_data['maintenance_mode'] = False

    await safe_edit_query_message(
        update,
        owner_panel_text(context),
        reply_markup=build_owner_keyboard(is_maintenance_mode(context)),
    )


async def user_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    user = update.effective_user
    user_id = user.id if user else None
    owner_ids = get_active_owner_ids(context)

    if is_maintenance_mode(context) and not is_owner(update, owner_ids):
        await safe_answer_query(update, 'Bot is paused by owner.', show_alert=True)
        return

    prefs = get_user_prefs(context, user_id)
    action = query.data or ''

    if action.startswith('user:q:'):
        quality = action.split(':', 2)[2]
        if quality in QUALITY_FORMATS:
            set_user_quality(context, user_id, quality)
            prefs = get_user_prefs(context, user_id)
        await safe_answer_query(update, f'Quality set: {QUALITY_LABELS[prefs["quality"]]}')
        await safe_edit_query_message(
            update,
            user_dashboard_text(user_id, prefs['quality']),
            reply_markup=build_user_keyboard(prefs['quality']),
        )
        return

    if action == 'user:reset':
        set_user_quality(context, user_id, 'best')
        prefs = get_user_prefs(context, user_id)
        await safe_answer_query(update, 'Reset to default quality.')
        await safe_edit_query_message(
            update,
            user_dashboard_text(user_id, prefs['quality']),
            reply_markup=build_user_keyboard(prefs['quality']),
        )
        return

    if action == 'user:help':
        await safe_answer_query(update)
        await safe_edit_query_message(
            update,
            user_help_text(user_id, prefs['quality']),
            reply_markup=build_user_keyboard(prefs['quality']),
        )
        return

    await safe_answer_query(update)
    await safe_edit_query_message(
        update,
        user_dashboard_text(user_id, prefs['quality']),
        reply_markup=build_user_keyboard(prefs['quality']),
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    if not owner_ids:
        await update.message.reply_text(
            'Owner commands are disabled. Set OWNER_CHAT_ID or OWNER_USER_ID or OWNER_IDS in .env, '
            'restart bot, then use /id.'
        )
        return
    if not is_owner(update, owner_ids):
        await update.message.reply_text('Owner-only command.')
        return
    if not context.args:
        await update.message.reply_text('Usage: /broadcast your message here')
        return

    message = ' '.join(context.args).strip()
    if not message:
        await update.message.reply_text('Usage: /broadcast your message here')
        return

    known_users = context.application.bot_data.get('known_users', set())
    sent = 0
    failed = 0

    for chat_id in known_users:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f'Broadcast completed. Sent: {sent}, Failed: {failed}')


async def download_and_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    stats = ensure_stats(context)

    if is_maintenance_mode(context) and not is_owner(update, owner_ids):
        await update.message.reply_text('Bot is temporarily paused by owner. Please try again later.')
        return

    message_text = (update.message.text or '').strip()
    url = extract_valid_url(message_text)
    if not url:
        stats['invalid_input'] += 1
        await update.message.reply_text('Please send a full valid URL starting with http:// or https://')
        return
    if not is_supported_video_url(url):
        stats['invalid_input'] += 1
        await update.message.reply_text('Unsupported URL. Send only Facebook or TikTok video links.')
        return

    user_id = update.effective_user.id if update.effective_user else None
    prefs = get_user_prefs(context, user_id)
    selected_quality = prefs['quality']

    status_msg = await update.message.reply_text(
        f'Downloading video. Quality: {QUALITY_LABELS[selected_quality]}. Please wait...'
    )

    progress_state: dict[str, float | None | str] = {
        'stage': 'starting',
        'downloaded': 0.0,
        'total': None,
        'speed': None,
        'eta': None,
        'percent': None,
    }
    progress_done = asyncio.Event()

    def progress_text() -> str:
        stage = str(progress_state.get('stage') or 'starting')
        if stage == 'downloading':
            percent = progress_state.get('percent')
            downloaded = progress_state.get('downloaded')
            total = progress_state.get('total')
            speed = progress_state.get('speed')
            eta = progress_state.get('eta')
            pct_text = f'{float(percent):.1f}%' if percent is not None else '...'
            return (
                f'Downloading video ({QUALITY_LABELS[selected_quality]})\n'
                f'Progress: {pct_text}\n'
                f'Data: {format_bytes(downloaded)} / {format_bytes(total)}\n'
                f'Speed: {format_bytes(speed)}/s\n'
                f'ETA: {format_eta(eta)}'
            )
        if stage == 'uploading':
            return 'Download finished. Uploading to Telegram...'
        if stage == 'done':
            return 'Done. Video sent successfully.'
        if stage == 'too_large':
            return (
                'Downloaded file is over 50MB and cannot be sent by bot.\n'
                'Open /menu and choose SD 480p, then send the URL again.'
            )
        if stage == 'download_error':
            return 'Invalid or unsupported URL. Send a direct Facebook/TikTok video link.'
        if stage == 'failed':
            return 'Cannot download this video. Check the link/privacy and try again.'
        return f'Preparing download ({QUALITY_LABELS[selected_quality]})...'

    async def progress_updater() -> None:
        last_text = ''
        while not progress_done.is_set():
            text = progress_text()
            if text != last_text:
                with suppress(BadRequest):
                    await status_msg.edit_text(text)
                last_text = text
            await asyncio.sleep(1.2)

        final_text = progress_text()
        if final_text != last_text:
            with suppress(BadRequest):
                await status_msg.edit_text(final_text)

    output_template = os.path.join(tempfile.gettempdir(), f'video_{uuid.uuid4()}.%(ext)s')
    ydl_opts = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'noprogress': False,
        'logger': YtDlpLogger(),
    }

    downloaded_file = None
    progress_task = asyncio.create_task(progress_updater())

    try:
        loop = asyncio.get_running_loop()
        run_formats = [QUALITY_FORMATS[selected_quality]]
        if selected_quality != 'best':
            run_formats.append(QUALITY_FORMATS['best'])

        def to_float(value: object) -> float | None:
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        def hook(status: dict[str, object]) -> None:
            state = status.get('status')
            if state == 'downloading':
                downloaded = to_float(status.get('downloaded_bytes')) or 0.0
                total = to_float(status.get('total_bytes')) or to_float(status.get('total_bytes_estimate'))
                speed = to_float(status.get('speed'))
                eta = to_float(status.get('eta'))
                progress_state['stage'] = 'downloading'
                progress_state['downloaded'] = downloaded
                progress_state['total'] = total
                progress_state['speed'] = speed
                progress_state['eta'] = eta
                progress_state['percent'] = ((downloaded / total) * 100.0) if total else None
            elif state == 'finished':
                progress_state['stage'] = 'uploading'
                total = to_float(status.get('total_bytes'))
                if total is not None:
                    progress_state['downloaded'] = total
                    progress_state['total'] = total
                progress_state['speed'] = None
                progress_state['eta'] = 0.0
                progress_state['percent'] = 100.0

        def run_download() -> str:
            last_error: DownloadError | None = None
            for fmt in run_formats:
                opts = dict(ydl_opts)
                opts['format'] = fmt
                opts['progress_hooks'] = [hook]
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        return ydl.prepare_filename(info)
                except DownloadError as exc:
                    last_error = exc

            if last_error is not None:
                raise last_error
            raise DownloadError('No downloadable format found.')

        downloaded_file = await loop.run_in_executor(None, run_download)

        if not os.path.exists(downloaded_file):
            raise FileNotFoundError('Downloaded file not found on disk.')

        file_size = os.path.getsize(downloaded_file)
        if file_size > MAX_TELEGRAM_BYTES:
            stats['downloads_failed'] += 1
            progress_state['stage'] = 'too_large'
            return

        progress_state['stage'] = 'uploading'
        if update.effective_chat is not None:
            with suppress(Exception):
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VIDEO)
        with open(downloaded_file, 'rb') as video_file:
            await update.message.reply_video(video=video_file, caption='Download complete.By @henglihov')
        stats['downloads_ok'] += 1
        progress_state['stage'] = 'done'

    except DownloadError:
        stats['downloads_failed'] += 1
        progress_state['stage'] = 'download_error'
    except Exception:
        stats['downloads_failed'] += 1
        logging.exception('Error while downloading/sending video')
        progress_state['stage'] = 'failed'

    finally:
        with suppress(Exception):
            progress_done.set()
            await progress_task
        if downloaded_file and os.path.exists(downloaded_file):
            with suppress(OSError):
                os.remove(downloaded_file)


async def unsupported_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_user_seen(update, context)
    owner_ids = get_active_owner_ids(context)
    stats = ensure_stats(context)
    if is_maintenance_mode(context) and not is_owner(update, owner_ids):
        await update.message.reply_text('Bot is temporarily paused by owner. Please try again later.')
        return

    stats['invalid_input'] += 1
    await update.message.reply_text(
        'Unsupported input type. Send only a Facebook or TikTok video URL as text.'
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logging.error(
            'Telegram Conflict: another bot instance is using this token. '
            'Run only one process for this token (local OR hosting, not both).'
        )
        return

    logging.exception('Unhandled exception', exc_info=context.error)


def build_application(token: str, owner_ids: set[int]) -> Application:
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)

    async def post_init_runtime(application: Application) -> None:
        # Persistence is loaded during initialize; enforce runtime owner IDs after that.
        runtime_owner_ids = get_owner_ids() or set(owner_ids)
        application.bot_data['owner_ids'] = set(runtime_owner_ids)
        application.bot_data['started_at'] = time.time()
        application.bot_data['script_path'] = os.path.abspath(__file__)
        application.bot_data['process_id'] = os.getpid()
        logging.info(
            'Active owner IDs: %s',
            ', '.join(str(x) for x in sorted(runtime_owner_ids)) if runtime_owner_ids else 'none',
        )

    application = (
        ApplicationBuilder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init_runtime)
        .build()
    )

    # Normalize persisted fields.
    known_users = application.bot_data.get('known_users')
    if not isinstance(known_users, set):
        application.bot_data['known_users'] = set(known_users or [])

    user_prefs = application.bot_data.get('user_prefs')
    if not isinstance(user_prefs, dict):
        application.bot_data['user_prefs'] = {}

    stats = application.bot_data.get('stats')
    if not isinstance(stats, dict):
        application.bot_data['stats'] = {
            'downloads_ok': 0,
            'downloads_failed': 0,
            'invalid_input': 0,
        }
    else:
        application.bot_data['stats']['downloads_ok'] = int(application.bot_data['stats'].get('downloads_ok', 0))
        application.bot_data['stats']['downloads_failed'] = int(application.bot_data['stats'].get('downloads_failed', 0))
        application.bot_data['stats']['invalid_input'] = int(application.bot_data['stats'].get('invalid_input', 0))

    application.bot_data['owner_ids'] = set(owner_ids)
    application.bot_data['maintenance_mode'] = bool(application.bot_data.get('maintenance_mode', False))
    application.bot_data['started_at'] = float(application.bot_data.get('started_at', time.time()))
    application.bot_data['script_path'] = os.path.abspath(__file__)
    application.bot_data['process_id'] = os.getpid()

    application.add_error_handler(on_error)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('menu', menu_command))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('id', id_command))
    application.add_handler(CommandHandler('owner', owner_panel_command))
    application.add_handler(CommandHandler('panel', owner_panel_command))
    application.add_handler(CommandHandler('broadcast', broadcast_command))
    application.add_handler(CallbackQueryHandler(owner_panel_callback, pattern=r'^owner:'))
    application.add_handler(CallbackQueryHandler(user_panel_callback, pattern=r'^user:'))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), download_and_send_video))
    application.add_handler(MessageHandler((~filters.TEXT) & (~filters.COMMAND), unsupported_input))
    return application


def main() -> None:
    script_dir = os.path.dirname(__file__)
    env_path = os.path.join(script_dir, '.env')
    load_dotenv(dotenv_path=env_path, override=True)
    # Fallback for users editing .env in parent folders by mistake.
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(script_dir), '.env'), override=True)
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(script_dir)), '.env'), override=True)

    token = os.getenv('BOT_TOKEN')
    if not token:
        print('Error: BOT_TOKEN not found in .env')
        sys.exit(1)

    owner_ids = get_owner_ids()
    if not owner_ids:
        logging.warning('Owner IDs are not set. Use OWNER_CHAT_ID or OWNER_USER_ID or OWNER_IDS in .env.')
    else:
        logging.info('Resolved owner IDs: %s', ', '.join(str(x) for x in sorted(owner_ids)))

    health_server = start_health_server_if_needed()

    try:
        lock_socket = acquire_local_instance_lock()
    except OSError:
        logging.error('Another local bot.py instance is already running. Stop it before starting a new one.')
        sys.exit(3)

    logging.info('Bot is running. Owner controls: %s', 'enabled' if owner_ids else 'disabled')
    restart_delay = max(1, int(os.getenv('RESTART_DELAY_SECONDS', '4')))
    unexpected_exit_seconds = max(1, int(os.getenv('UNEXPECTED_EXIT_SECONDS', '20')))

    try:
        while True:
            # Python 3.14 may not have a current loop; ensure one exists and is open for retries.
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError('Event loop is closed')
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            application = build_application(token, owner_ids)
            started_at = time.time()

            try:
                application.run_polling(
                    drop_pending_updates=True,
                    bootstrap_retries=-1,
                    timeout=30,
                    close_loop=False,
                )
            except Conflict:
                logging.error(
                    'Bot stopped due to token conflict. '
                    'Stop other instances with same BOT_TOKEN (local/server) and keep only one.'
                )
            except Exception:
                logging.exception('Bot polling crashed unexpectedly. Will retry.')

            runtime = time.time() - started_at
            if runtime >= unexpected_exit_seconds:
                logging.info(
                    'Polling loop ended after %.1f seconds. Assuming intentional stop; exiting.',
                    runtime,
                )
                break

            logging.warning(
                'Polling ended too quickly after %.1f seconds. Restarting in %s seconds.',
                runtime,
                restart_delay,
            )
            time.sleep(restart_delay)
    finally:
        with suppress(Exception):
            lock_socket.close()
        if health_server is not None:
            with suppress(Exception):
                health_server.shutdown()
            with suppress(Exception):
                health_server.server_close()


if __name__ == '__main__':
    main()

