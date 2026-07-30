import argparse
import base64
import io
import json
import mimetypes
import os
import re
import email.utils
import secrets
import smtplib
import ssl
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.message import EmailMessage
from functools import wraps

try:
    import requests as _requests
except ImportError:
    _requests = None
from flask import (
    Flask,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
    jsonify,
    send_from_directory,
    send_file,
)
from datetime import datetime, timezone, timedelta
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception

try:
    import pyodbc
except ImportError:
    pyodbc = None

try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from pypdf import PdfWriter
except ImportError:
    PdfWriter = None

try:
    from reportlab.pdfgen import canvas as _sig_rl_canvas
    from reportlab.lib.utils import ImageReader as _sig_ImageReader
except ImportError:
    _sig_rl_canvas = None
    _sig_ImageReader = None

try:
    import easyocr
except ImportError:
    easyocr = None

try:
    from flask_socketio import SocketIO, join_room, leave_room, emit
except ImportError:
    SocketIO = None
    join_room = leave_room = emit = None




app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY must be set in your environment/.env file.")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB upload limit
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 365  # 1 year

# ── Realtime layer (WebSockets via Flask-SocketIO) ──────────────────────────
# Replaces 4s/15s/30s HTTP polling for messages/notifications with push
# events. Falls back to a safe no-op if flask-socketio isn't installed yet,
# so the app still runs on plain Waitress until you
# `pip install flask-socketio eventlet` and switch the run command at the
# bottom of this file.
if SocketIO is not None:
    # async_mode="threading" (NOT eventlet/gevent) is deliberate: this app
    # makes heavy synchronous pyodbc calls from request handlers and
    # background threads. eventlet/gevent monkey-patch the stdlib so I/O
    # cooperates with their event loop, but pyodbc is a C extension that
    # calls the native ODBC driver directly — under eventlet that call
    # blocks the *entire* process (every other socket/request) until the
    # query returns, which quietly wrecks concurrency instead of fixing it.
    # "threading" gives each connection a real OS thread, same execution
    # model this app already uses with Waitress today, just with
    # WebSocket support added on top.
    socketio = SocketIO(
        app,
        async_mode="threading",
        cors_allowed_origins="*",
        manage_session=True,
        allow_upgrades=False,  # threading mode can't do a true WS upgrade under
                               # Waitress — disabling this stops wasted upgrade
                               # attempts/console noise; falls straight to
                               # long-polling, which any WSGI server handles fine
        message_queue=None,  # set to a Redis URL here if you ever run >1 worker process
    )
else:
    socketio = None


def sio_emit(event, data, room=None):
    """Safe emit wrapper — no-ops if flask-socketio isn't installed, so every
    call site below stays simple instead of littering None-checks everywhere."""
    if socketio is None:
        return
    try:
        socketio.emit(event, data, room=room)
    except Exception as exc:
        print(f"[socketio] emit failed for event={event}: {exc}")


def _room_for_user(user_id):
    return f"user_{user_id}"


def _room_for_group(group_id):
    return f"msg_group_{group_id}"


if socketio is not None:
    @socketio.on("connect")
    def _sio_on_connect():
        uid = session.get("user_id")
        if uid:
            join_room(_room_for_user(uid))

    @socketio.on("join_group")
    def _sio_on_join_group(data):
        """Client asks to join a message-group room. We re-check membership
        server-side using the session — never trust the client's group_id
        alone, same rule as every /api/messages/groups/<id> route."""
        uid = session.get("user_id")
        group_id = (data or {}).get("group_id")
        if not uid or not group_id:
            return
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            if _user_in_group(cursor, int(group_id), uid):
                join_room(_room_for_group(group_id))
        except Exception as exc:
            print(f"[socketio] join_group failed: {exc}")
        finally:
            if conn:
                conn.close()

    @socketio.on("leave_group")
    def _sio_on_leave_group(data):
        group_id = (data or {}).get("group_id")
        if group_id:
            leave_room(_room_for_group(group_id))

    @socketio.on("typing")
    def _sio_on_typing(data):
        """Live typing indicator — no DB write at all now (previously an
        HTTP POST hitting an in-memory dict on the server). Broadcast
        straight to the other members of the room."""
        uid = session.get("user_id")
        group_id = (data or {}).get("group_id")
        if not uid or not group_id:
            return
        emit(
            "typing",
            {"group_id": group_id, "user_id": uid},
            room=_room_for_group(group_id),
            include_self=False,
        )

# ── Email / SMTP config ──────────────────────────────────────────────────
# NEVER hardcode SMTP_PASSWORD here — set it in your .env file or server
# environment instead (SMTP_USERNAME / SMTP_PASSWORD). A hardcoded mailbox
# password in this file becomes a live credential the moment it's committed
# or shared.
SMTP_SERVER    = os.environ.get("SMTP_SERVER", "smtp.office365.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME  = os.environ.get("SMTP_USERNAME", "")   # e.g. noreply@yourcompany.com
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "")   # O365 app password — from .env only
SMTP_FROM      = os.environ.get("SMTP_FROM", SMTP_USERNAME)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "DocPortal System")

# ── Per-user email identity (optional) ───────────────────────────────────
# Users can set their own real mailbox + password in Settings → Email.
# When present, we log into O365 SMTP AS THAT USER and send From their own
# address (real per-user identity, no Reply-To workaround needed).
# When absent, we fall back to the single shared SMTP_USERNAME account.
#
# The password is encrypted at rest with Fernet (symmetric). Generate a key
# ONCE with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# and put it in .env as APP_ENCRYPTION_KEY. Never commit it or change it
# after users have saved passwords — losing the key means every stored
# password becomes undecryptable and users must re-enter them.
APP_ENCRYPTION_KEY = os.environ.get("APP_ENCRYPTION_KEY", "")
_fernet = Fernet(APP_ENCRYPTION_KEY.encode()) if (Fernet and APP_ENCRYPTION_KEY) else None


def _encrypt_secret(plaintext):
    if not _fernet:
        raise RuntimeError(
            "Per-user email requires APP_ENCRYPTION_KEY to be set in .env "
            "(and the 'cryptography' package installed)."
        )
    return _fernet.encrypt(plaintext.encode()).decode()


def _decrypt_secret(ciphertext):
    if not _fernet:
        return None
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


# ── Shared Mail Settings (Control Panel → Mail Settings, admin-only) ─────
# Single source of truth for the SHARED/default outbound mailbox — the one
# every user falls back to when they haven't set up their own personal
# "Send Documents As Yourself" email.
#
# Storage: NOT a new table. Piggybacks on the same Sys_User.SmtpEmail /
# SmtpPasswordEnc columns already used for per-user email, plus new
# SmtpServer / SmtpPort / SmtpUseSSL columns. A new IsSharedMailAccount BIT
# flag marks which single Sys_User row is currently "the shared mailbox" —
# whichever admin last saved Mail Settings owns that row.
#
# IMPORTANT TRADE-OFF: because the shared config reuses the same columns as
# personal per-user email, saving Mail Settings overwrites that admin's own
# personal SmtpEmail/SmtpPasswordEnc if they had one set. This is the
# accepted trade-off of not adding a separate table — the admin UI surfaces
# whose row currently holds the shared config so this isn't a silent
# surprise.
#
# Resolution order everywhere in the app: the Sys_User row flagged
# IsSharedMailAccount = 1 takes priority; .env is the fallback when no row
# is flagged yet.

# Server/port/SSL presets for the admin UI's provider dropdown, and for
# inferring which preset (if any) a saved smtp_server matches. "custom"
# has no preset — the admin fills in server/port/SSL themselves.
MAIL_PROVIDER_PRESETS = {
    "office365": {"smtp_server": "smtp.office365.com", "smtp_port": 587, "use_ssl": True},
    "gmail":     {"smtp_server": "smtp.gmail.com",      "smtp_port": 587, "use_ssl": True},
}


def _infer_mail_provider(smtp_server):
    """Match a saved smtp_server back to a known preset name, else 'custom'."""
    if smtp_server:
        s = smtp_server.strip().lower()
        for name, preset in MAIL_PROVIDER_PRESETS.items():
            if preset["smtp_server"] == s:
                return name
    return "custom"


def _get_shared_mail_row():
    """
    Returns the raw Sys_User row currently flagged as the shared mailbox:
    (USER_ID, SmtpEmail, SmtpPasswordEnc, SmtpServer, SmtpPort, SmtpUseSSL,
     USER_FULLNAME, USER_NAME), or None if no row is flagged yet.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT USER_ID, SmtpEmail, SmtpPasswordEnc, SmtpServer, SmtpPort, SmtpUseSSL,
                   USER_FULLNAME, USER_NAME
            FROM dbo.Sys_User
            WHERE IsSharedMailAccount = 1
            """
        )
        return cursor.fetchone()
    except Exception:
        return None
    finally:
        if conn: conn.close()


def get_shared_smtp_config():
    """
    THE single source of truth for outbound SMTP connection details.
    The Sys_User row flagged IsSharedMailAccount = 1 takes priority; .env
    is the fallback when no row is flagged yet. Nothing else in the app
    should read SMTP_SERVER / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD
    directly.

    Returns (smtp_server, smtp_port, sender_email, app_password, use_ssl).
    app_password is the decrypted plaintext, or None if unset/undecryptable.
    """
    row = _get_shared_mail_row()
    if row and row[1]:
        password = _decrypt_secret(row[2]) if row[2] else None
        return (
            row[3] or SMTP_SERVER,
            int(row[4] or SMTP_PORT or 587),
            row[1],
            password,
            bool(row[5]) if row[5] is not None else True,
        )
    return (SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, True)


def get_shared_from_email():
    """
    The shared/default From address — used as the Graph send-as mailbox
    and as the fallback From for SMTP. The flagged Sys_User row first,
    then .env (SMTP_FROM, falling back further to SMTP_USERNAME).
    """
    row = _get_shared_mail_row()
    if row and row[1]:
        return row[1]
    return SMTP_FROM or SMTP_USERNAME


def _get_user_email_config(user_id):
    """
    Returns (smtp_email, smtp_password, smtp_server, smtp_port, use_ssl) for
    this user's OWN "send as yourself" config, or (None, None, None, None,
    None) if not set/undecryptable. Each user's SmtpServer/SmtpPort/
    SmtpUseSSL are their own — a Gmail user logs into smtp.gmail.com, an
    Office 365 user into smtp.office365.com, entirely independent of
    whatever the admin's shared Mail Settings use.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SmtpEmail, SmtpPasswordEnc, SmtpServer, SmtpPort, SmtpUseSSL "
            "FROM dbo.Sys_User WHERE USER_ID = ?",
            user_id,
        )
        row = cursor.fetchone()
        if not row or not row[0] or not row[1]:
            return None, None, None, None, None
        password = _decrypt_secret(row[1])
        if not password:
            return None, None, None, None, None
        server = row[2] or _infer_smtp_server_for_email(row[0]) or SMTP_SERVER
        port = int(row[3] or 587)
        use_ssl = bool(row[4]) if row[4] is not None else True
        return row[0], password, server, port, use_ssl
    except Exception:
        return None, None, None, None, None
    finally:
        if conn: conn.close()


def _infer_smtp_server_for_email(email_addr):
    """
    Best-effort guess of a provider preset from an email's domain, used
    only as a fallback when a user hasn't explicitly picked/saved a
    provider. Returns None if the domain isn't a recognized consumer
    provider (e.g. a company's own domain) — callers should fall back to
    SMTP_SERVER or ask the user to pick "Custom" and enter it themselves.
    """
    if not email_addr or "@" not in email_addr:
        return None
    domain = email_addr.rsplit("@", 1)[-1].strip().lower()
    if domain in ("gmail.com", "googlemail.com"):
        return MAIL_PROVIDER_PRESETS["gmail"]["smtp_server"]
    if domain in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
        return MAIL_PROVIDER_PRESETS["office365"]["smtp_server"]
    return None


def _get_user_send_email(user_id):
    """
    Returns just the user's configured send-as address (SmtpEmail), or None.
    Unlike _get_user_email_config, this does NOT require a saved password —
    Microsoft Graph sends as this mailbox using the app's own credentials,
    so no per-user password is needed at all.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT SmtpEmail FROM dbo.Sys_User WHERE USER_ID = ?", user_id)
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None
    finally:
        if conn: conn.close()


# ── Microsoft Graph (OAuth2, app-only) mail sending ──────────────────────
# Replaces per-user SMTP login entirely. The app authenticates as ITSELF
# (client credentials flow) using an Azure AD app registration that has
# been granted the Mail.Send *application* permission (admin-consented),
# then calls /users/{mailbox}/sendMail to send genuinely as any mailbox
# in the tenant — no per-user password, no MFA/app-password headaches.
#
# Setup (done once by a Microsoft 365 admin):
#   1. Azure Portal -> Microsoft Entra ID -> App registrations -> New registration
#   2. Certificates & secrets -> New client secret -> copy the VALUE
#   3. API permissions -> Add permission -> Microsoft Graph -> Application
#      permissions -> Mail.Send -> Grant admin consent
#   4. (Recommended) Exchange Online PowerShell: New-ApplicationAccessPolicy
#      to restrict which mailboxes this app is allowed to send as.
#
# Put the three values below in .env — never commit them or paste them
# anywhere outside the server's own environment.
GRAPH_TENANT_ID     = os.environ.get("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID     = os.environ.get("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")
GRAPH_ENABLED = bool(GRAPH_TENANT_ID and GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET)
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

_graph_token_cache = {"token": None, "expires_at": 0}


def _get_graph_token():
    """
    Returns a valid app-only Graph access token, requesting a new one only
    when the cached token is missing or about to expire. Tokens are cached
    in-process (not persisted), which is fine across a single server run.
    """
    if _requests is None:
        raise RuntimeError("The 'requests' package is required for Microsoft Graph mail sending.")
    if not GRAPH_ENABLED:
        raise RuntimeError(
            "Microsoft Graph is not configured. Set GRAPH_TENANT_ID, GRAPH_CLIENT_ID, "
            "and GRAPH_CLIENT_SECRET in the server's .env file."
        )

    now = time.time()
    if _graph_token_cache["token"] and now < _graph_token_cache["expires_at"] - 60:
        return _graph_token_cache["token"]

    resp = _requests.post(
        f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": GRAPH_CLIENT_ID,
            "client_secret": GRAPH_CLIENT_SECRET,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Graph token request failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    token = payload["access_token"]
    _graph_token_cache["token"] = token
    _graph_token_cache["expires_at"] = now + int(payload.get("expires_in", 3600))
    return token


def _send_graph_mail(subject, html_body, recipients, from_email, reply_to=None, attachments=None):
    """
    Sends mail via Microsoft Graph as `from_email`, authenticated as the
    app itself (no per-user password involved).
    attachments: list of dicts {filename, content_bytes, content_type}
    Raises on failure — caller is expected to catch and report it.
    """
    if not from_email:
        raise RuntimeError("from_email is required to send via Microsoft Graph.")

    token = _get_graph_token()

    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
    }
    if reply_to:
        message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": a["filename"],
                "contentType": a.get("content_type") or "application/octet-stream",
                "contentBytes": base64.b64encode(a["content_bytes"]).decode("ascii"),
            }
            for a in attachments
        ]

    resp = _requests.post(
        f"https://graph.microsoft.com/v1.0/users/{from_email}/sendMail",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"message": message, "saveToSentItems": "true"},
        timeout=30,
    )
    if resp.status_code == 401:
        raise PermissionError("Graph rejected the app token — check the client ID/secret and tenant ID.")
    if resp.status_code == 403:
        raise PermissionError(
            f"Graph refused to send as {from_email} — confirm Mail.Send has admin consent, "
            "and that any ApplicationAccessPolicy includes this mailbox."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph sendMail failed ({resp.status_code}): {resp.text[:300]}")


# All mail goes out through ONE authenticated mailbox (SMTP_USERNAME).
# Every user in the portal can still send, but the "From" address is
# always this shared account — SMTP AUTH has no concept of "log in as A,
# send as B". We set Reply-To to the actual sender's own address so
# replies still land with the right person even though From is shared.
# This also matters for deliverability: From MUST match the authenticated
# mailbox or Gmail/Outlook will flag SPF/DKIM/DMARC alignment failures and
# bounce or spam-box the message.
# NOTE: this whole SMTP path is now the FALLBACK, only used when Graph
# isn't configured (GRAPH_ENABLED is False) — see api_email_send() below.



def _send_smtp_mail(subject, text_body, html_body, recipients, reply_to=None, attachments=None,
                     from_email=None, from_password=None, from_name=None,
                     smtp_server=None, smtp_port=None, use_ssl=None):
    """
    attachments: list of dicts {filename, content_bytes, content_type}
    from_email/from_password: if given, log in and send AS this mailbox
      instead of the shared account (true per-user identity).
    smtp_server/smtp_port/use_ssl: override the shared Mail Settings — used
      by the admin "Send Test Email" flow to test unsaved values before
      they're saved. Leave None to use the saved/​.env shared config.
    Raises on any failure — caller is expected to catch and report it.
    """
    shared_server, shared_port, shared_sender, shared_password, shared_ssl = get_shared_smtp_config()

    login_email = from_email or shared_sender
    login_password = from_password or shared_password
    display_name = from_name if from_name is not None else SMTP_FROM_NAME
    host = smtp_server or shared_server
    port = int(smtp_port or shared_port or 587)
    ssl_on = shared_ssl if use_ssl is None else bool(use_ssl)

    if not login_email or not login_password:
        raise RuntimeError(
            "Email is not configured. Set it up in Control Panel \u2192 Mail Settings, "
            "or set SMTP_USERNAME/SMTP_PASSWORD in the server's .env file."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{display_name} <{login_email}>" if display_name else login_email
    msg["To"] = ", ".join(recipients)
    # Real Date + Message-ID headers matter for Gmail/Outlook spam scoring —
    # without them some providers treat mail as low-quality/bulk.
    msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = email.utils.make_msgid(domain=(login_email.split("@")[-1] if "@" in login_email else None))
    if reply_to:
        msg["Reply-To"] = reply_to

    msg.set_content(text_body)                       # plain-text fallback
    msg.add_alternative(html_body, subtype="html")   # rendered version most clients show

    for a in (attachments or []):
        ctype = a.get("content_type") or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            a["content_bytes"],
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=a["filename"],
        )

    # timeout + explicit EHLO/login sequence so failures surface as a clear
    # exception instead of hanging the request. Three modes, matching the
    # "EnableSSL" + port combination configured in Mail Settings:
    #   - port 465 + SSL on  -> implicit TLS from the first byte (SMTP_SSL)
    #   - any port + SSL on  -> plaintext connect, then upgrade via STARTTLS
    #   - SSL off            -> no encryption at all (only for trusted
    #                           internal relays — most providers reject this)
    if ssl_on and port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
            server.login(login_email, login_password)
            server.send_message(msg)
    elif ssl_on:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(login_email, login_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.login(login_email, login_password)
            server.send_message(msg)

# Most SMTP servers (incl. O365) reject messages well before the nominal
# 25MB limit once attachments are base64-encoded (~33% overhead) plus
# headers. Cap the raw attachment total we'll bundle into one email;
# anything pushed past this falls back to a share link instead.
MAX_EMAIL_ATTACH_TOTAL_BYTES = 15 * 1024 * 1024

# ── Share-link token store ───────────────────────────────────────────────
# Simple JSON file on disk, no new DB tables — matches the OCR cache pattern
# just below. token -> { att_id, file_url, expires_at }.
SHARE_TOKENS_FILE = os.path.join(app.root_path, "share_tokens.json")
_share_tokens_lock = threading.Lock()
SHARE_TOKEN_TTL_DAYS = int(os.environ.get("SHARE_TOKEN_TTL_DAYS", "30"))


def _load_share_tokens() -> dict:
    with _share_tokens_lock:
        if not os.path.isfile(SHARE_TOKENS_FILE):
            return {}
        try:
            with open(SHARE_TOKENS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"[share tokens] read failed: {exc}")
            return {}


def _save_share_tokens(tokens: dict) -> None:
    with _share_tokens_lock:
        try:
            with open(SHARE_TOKENS_FILE, "w", encoding="utf-8") as f:
                json.dump(tokens, f)
        except Exception as exc:
            print(f"[share tokens] write failed: {exc}")


def _prune_expired_share_tokens(tokens: dict) -> dict:
    now = datetime.utcnow()
    pruned = {}
    for tok, info in tokens.items():
        try:
            if datetime.fromisoformat(info["expires_at"]) > now:
                pruned[tok] = info
        except Exception:
            continue
    return pruned


def _create_share_token(att_id, file_url: str, expires_days: int = SHARE_TOKEN_TTL_DAYS) -> str:
    """
    Generate an unguessable (256-bit) share token for one attachment.
    The token itself is the access control — same model as a Dropbox/Google
    Drive share link — since this endpoint intentionally has no login so
    external recipients can open it.
    """
    token = secrets.token_urlsafe(32)
    tokens = _prune_expired_share_tokens(_load_share_tokens())
    tokens[token] = {
        "att_id": att_id,
        "file_url": file_url,
        "expires_at": (datetime.utcnow() + timedelta(days=expires_days)).isoformat(),
    }
    _save_share_tokens(tokens)
    return token


QR_TOKEN_TTL_DAYS = 90  # QR codes get a longer life than a one-off email share link


def esc(s):
    """Minimal HTML-escape for the public QR scan page (no Jinja template used there)."""
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _create_doc_qr_token(doc_id: int, expires_days: int = QR_TOKEN_TTL_DAYS) -> str:
    """
    Generate an unguessable token for a document-level QR code. Reuses the
    same on-disk token store as attachment share links (distinguished by the
    'doc_id' key instead of 'att_id') — no new database table involved.
    """
    token = secrets.token_urlsafe(32)
    tokens = _prune_expired_share_tokens(_load_share_tokens())
    tokens[token] = {
        "doc_id": doc_id,
        "expires_at": (datetime.utcnow() + timedelta(days=expires_days)).isoformat(),
    }
    _save_share_tokens(tokens)
    return token


# ── OCR master switch ────────────────────────────────────────────────────────────────────────────
# OCR is opt-in: the engine only runs when the user explicitly clicks
# "Extract Text" in the UI. It never runs automatically on every save/edit.
# Set to False only to fully disable the /api/ocr endpoint.
OCR_ENABLED = True

UPLOAD_DIR = os.path.join(app.root_path, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
# Root for legacy attachment paths stored in File_URL (UNC or local paths from the old app)
ATTACHMENT_FILES_ROOT = (os.getenv("ATTACHMENT_FILES_ROOT") or "").strip()

# Where new uploads are physically saved.
# If ATTACHMENT_FILES_ROOT is configured (e.g. C:\Files), use that so files
# survive server restarts and work in production. Otherwise fall back to the
# local uploads/ folder (dev only).
FILE_SAVE_DIR = ATTACHMENT_FILES_ROOT if ATTACHMENT_FILES_ROOT else UPLOAD_DIR
os.makedirs(FILE_SAVE_DIR, exist_ok=True)

# ── Local OCR text cache (NOT SQL Server) ──────────────────────────────────
# OCR text is intentionally never written to the SQL Server database.
# Instead it's cached locally on disk, one JSON file per attachment, so
# Document Content search doesn't have to re-OCR a file every time someone
# searches. A manifest file tracks which attachment IDs have a cache entry
# so search can skip straight to relevant files instead of listing the
# whole directory.
# NOTE: if the number of OCR'd documents grows into the thousands, swap this
# for a local SQLite file (still not SQL Server) for indexed lookups —
# the read/write functions below are the only things that would need to change.
OCR_CACHE_DIR = os.path.join(app.root_path, "ocr_cache")
os.makedirs(OCR_CACHE_DIR, exist_ok=True)
OCR_CACHE_MANIFEST = os.path.join(OCR_CACHE_DIR, "_manifest.json")
_ocr_cache_lock = threading.Lock()


def _ocr_cache_path(attachment_id: int) -> str:
    return os.path.join(OCR_CACHE_DIR, f"{attachment_id}.json")


def ocr_cache_write(attachment_id: int, file_name: str, ocr_text: str) -> None:
    """Persist OCR text for one attachment to a local JSON file (not SQL Server)."""
    with _ocr_cache_lock:
        try:
            entry = {
                "attachment_id": attachment_id,
                "file_name": file_name,
                "ocr_text": ocr_text,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(_ocr_cache_path(attachment_id), "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)

            manifest = {}
            if os.path.isfile(OCR_CACHE_MANIFEST):
                try:
                    with open(OCR_CACHE_MANIFEST, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                except Exception:
                    manifest = {}
            manifest[str(attachment_id)] = entry["extracted_at"]
            with open(OCR_CACHE_MANIFEST, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False)

            # Free the in-memory manifest/entry now that both are on disk —
            # entry["ocr_text"] can be large, no need to keep it in RAM.
            manifest.clear()
            del manifest, entry
        except Exception as exc:
            print(f"[OCR cache] write failed for attachment {attachment_id}: {exc}")


def ocr_cache_read(attachment_id: int) -> str:
    """Return cached OCR text for one attachment, or '' if not cached."""
    try:
        with open(_ocr_cache_path(attachment_id), "r", encoding="utf-8") as f:
            return (json.load(f).get("ocr_text") or "")
    except Exception:
        return ""


def ocr_cache_search(query: str) -> set:
    """
    Return the set of attachment IDs whose cached OCR text contains `query`
    (case-insensitive substring match). Only scans attachments listed in the
    manifest — i.e. only ones that have actually been OCR'd.
    """
    if not query:
        return set()
    q = query.strip().lower()
    matches = set()
    try:
        if not os.path.isfile(OCR_CACHE_MANIFEST):
            return matches
        with open(OCR_CACHE_MANIFEST, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception:
        return matches

    for att_id_str in manifest:
        try:
            att_id = int(att_id_str)
            text = ocr_cache_read(att_id)
            if text and q in text.lower():
                matches.add(att_id)
        except Exception:
            continue
    return matches


_SUMMARY_STOPWORDS_EN = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "been", "with", "at", "by", "this", "that", "it",
    "as", "from", "will", "shall", "has", "have", "had", "not", "no", "do",
    "does", "did", "which", "who", "whom", "its", "into", "than", "then",
}
_SUMMARY_STOPWORDS_AR = {
    "في", "من", "على", "إلى", "الى", "و", "أو", "او", "هذا", "هذه", "ذلك",
    "التي", "الذي", "كان", "كانت", "لم", "لن", "قد", "ما", "لا", "أن", "ان",
    "مع", "عن", "بعد", "قبل", "كل", "بين",
}

def _text_quality_ok(text: str, min_ratio: float = 0.35, min_words: int = 8) -> bool:
    """
    Heuristic check: is this text substantial enough (mostly real words, not
    a numeric table/form) to bother summarizing? Academic reports, invoices,
    and other heavily-tabular scans often OCR into strings like
    "00 00 used 00 used" — technically non-empty text, but nothing an
    extractive summary can turn into a sentence worth reading.
    Returns False when the text is too short or too numeric/short-token-heavy.
    """
    words = re.findall(r"[\w\u0600-\u06FF]+", (text or "").lower())
    if len(words) < min_words:
        return False
    substantial = [w for w in words if len(w) >= 3 and not w.isdigit()]
    return (len(substantial) / len(words)) >= min_ratio


def _summarize_text(text: str, max_sentences: int = 3) -> str:
    """
    Simple extractive summary: score each sentence by the frequency of its
    (non-stopword) words across the whole document, then keep the top-N
    sentences in their original order. No external NLP library or AI API —
    just word-frequency scoring, good enough for a quick chatbot preview of
    long OCR'd text. Works reasonably for both English and Arabic.
    """
    text = (text or "").strip()
    if not text:
        return ""

    # Split on sentence-ending punctuation (Latin + Arabic variants) and newlines.
    sentences = [s.strip() for s in re.split(r"[.!?؟\n]+", text) if s.strip()]
    if not sentences:
        return ""
    if len(sentences) <= max_sentences:
        return ". ".join(sentences)

    stopwords = _SUMMARY_STOPWORDS_EN | _SUMMARY_STOPWORDS_AR
    word_freq = {}
    for sentence in sentences:
        for word in re.findall(r"[\w\u0600-\u06FF]+", sentence.lower()):
            if word in stopwords or len(word) < 2:
                continue
            word_freq[word] = word_freq.get(word, 0) + 1

    if not word_freq:
        return ". ".join(sentences[:max_sentences])

    scored = []
    for idx, sentence in enumerate(sentences):
        words = re.findall(r"[\w\u0600-\u06FF]+", sentence.lower())
        if not words:
            continue
        score = sum(word_freq.get(w, 0) for w in words) / len(words)
        scored.append((idx, score, sentence))

    top = sorted(scored, key=lambda x: x[1], reverse=True)[:max_sentences]
    top.sort(key=lambda x: x[0])  # restore original reading order
    return ". ".join(s[2] for s in top)



def get_save_dir(folder_id=None, cursor=None) -> str:
    """
    If folder_id is provided and a cursor is available, creates and returns
    a subfolder: FILE_SAVE_DIR / <dept_name> / <folder_name>
    Falls back to FILE_SAVE_DIR if anything goes wrong.
    """
    if folder_id and cursor:
        try:
            # Resolve column names dynamically (same pattern used elsewhere)
            dept_col = (os.getenv("SYS_DEPARTMENT_NAME_COLUMN") or "").strip() or "Folder_Name"
            adco_col = (os.getenv("ADCO_FOLDER_NAME_COLUMN") or "").strip() or "Folder_Name"
            cursor.execute(f"""
                SELECT ISNULL(d.[{dept_col}], ''), ISNULL(af.[{adco_col}], '')
                FROM dbo.Adco_Folder af
                LEFT JOIN dbo.Sys_Department d ON d.ID = af.Dept_ID
                WHERE af.ID = ? AND af.IsDeleted = 0
            """, folder_id)
            row = cursor.fetchone()
            if row:
                # Sanitize names — strip characters illegal on Windows and Linux paths
                def _safe(name):
                    return re.sub(r'[\\/:*?"<>|]', '_', (name or '').strip()) or '_'
                dept_name = _safe(row[0])
                folder_name = _safe(row[1])
                subfolder = os.path.join(FILE_SAVE_DIR, dept_name, folder_name)
                os.makedirs(subfolder, exist_ok=True)
                return subfolder
        except Exception:
            pass  # graceful fallback — never break an upload over a path issue
    return FILE_SAVE_DIR


# ── Schema introspection cache ────────────────────────────────────────────────
# Populated lazily on first DB call; maps (table, preferred_col) -> actual_col
_SCHEMA_CACHE: dict = {}


def _detect_column(cursor, table: str, candidates: list[str], fallback: str) -> str:
    """
    Return the first column from `candidates` that actually exists in `table`.
    Uses INFORMATION_SCHEMA so it works across any SQL Server schema.
    Falls back to `fallback` if none match (will surface as a clear DB error).
    Result is cached in _SCHEMA_CACHE keyed by (table, fallback).
    """
    cache_key = (table, fallback)
    if cache_key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[cache_key]
    try:
        placeholders = ",".join(f"'{c}'" for c in candidates)
        schema, tbl = ("dbo", table.split(".")[-1]) if "." in table else ("dbo", table)
        cursor.execute(f"""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = '{schema}'
              AND TABLE_NAME   = '{tbl}'
              AND COLUMN_NAME  IN ({placeholders})
        """)
        found = {row[0] for row in cursor.fetchall()}
        for c in candidates:
            if c in found:
                _SCHEMA_CACHE[cache_key] = c
                return c
    except Exception:
        pass
    _SCHEMA_CACHE[cache_key] = fallback
    return fallback


def _resolved_name_col(cursor, table: str, env_key: str, candidates: list[str], fallback: str) -> str:
    """
    Env override wins if set and valid; otherwise auto-detect from schema.
    """
    raw = (os.getenv(env_key) or "").strip()
    if raw and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        return raw
    return _detect_column(cursor, table, candidates, fallback)


def _sql_ident(name: str, default: str) -> str:
    """Safe SQL Server identifier (no quoting injection). Letters, digits, underscore only."""
    raw = (name or "").strip()
    if raw and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        return raw
    return default


def dept_folder_name_col(cursor=None) -> str:
    """Display name column on dbo.Sys_Department."""
    if cursor is not None:
        return _resolved_name_col(
            cursor, "dbo.Sys_Department", "SYS_DEPARTMENT_NAME_COLUMN",
            ["Name", "Folder_Name", "Dep_Name", "Department_Name", "DeptName"],
            "Folder_Name",
        )
    return _sql_ident(os.getenv("SYS_DEPARTMENT_NAME_COLUMN", ""), "Folder_Name")


def adco_folder_name_col(cursor=None) -> str:
    """Display name column on dbo.Adco_Folder."""
    if cursor is not None:
        return _resolved_name_col(
            cursor, "dbo.Adco_Folder", "ADCO_FOLDER_NAME_COLUMN",
            ["Folder_Name", "Name", "FolderName"],
            "Folder_Name",
        )
    return _sql_ident(os.getenv("ADCO_FOLDER_NAME_COLUMN", ""), "Folder_Name")


def _dept_id_col(env_key: str, default: str) -> str:
    """Department link column; env __id__ means use table ID as the dept key."""
    raw = (os.getenv(env_key) or default).strip()
    if raw.lower() == "__id__":
        return "ID"
    return _sql_ident(raw, default)


# Candidate column names for the dept-link column, in preference order.
_DEPT_ID_CANDIDATES = ["Dept_ID", "DeptID", "Dep_ID", "DepID", "Department_ID"]


def sys_department_dept_col(cursor=None) -> str:
    """
    The dept-key column on Sys_Department.
    Sys_Department has no separate Dept_ID column — its PK (ID) IS the dept key,
    and Adco_Folder.Dept_ID values equal Sys_Department.ID.
    An explicit SYS_DEPARTMENT_DEPT_ID_COLUMN env var overrides this default.
    """
    raw = (os.getenv("SYS_DEPARTMENT_DEPT_ID_COLUMN") or "__id__").strip()
    if raw.lower() == "__id__":
        return "ID"
    # Explicit env override — validate and use it
    return _sql_ident(raw, "ID")


# Keep old name as alias so nothing outside this file breaks
def sys_department_dept_select_expr(cursor=None) -> str:
    return sys_department_dept_col(cursor)


def sys_department_dept_insert_col(cursor=None):
    """INSERT column name, or None if dept key is the row ID (__id__)."""
    raw = (os.getenv("SYS_DEPARTMENT_DEPT_ID_COLUMN") or "").strip()
    if raw.lower() in ("__id__", "__none__"):
        return None
    if raw and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        return raw
    if cursor is not None:
        return sys_department_dept_col(cursor)
    return "Dept_ID"


def adco_folder_dept_col(cursor=None) -> str:
    """Filter/insert column linking Adco_Folder to a main folder dept key."""
    if cursor is not None:
        return _resolved_name_col(
            cursor, "dbo.Adco_Folder", "ADCO_FOLDER_DEPT_ID_COLUMN",
            _DEPT_ID_CANDIDATES, "Dept_ID",
        )
    return _dept_id_col("ADCO_FOLDER_DEPT_ID_COLUMN", "Dept_ID")


def _attachments_active_sql(alias: str = "") -> str:
    """
    The legacy data has IsDeleted=1 on valid old attachments (the old app set
    IsDeleted=1 on every row after migrating).  Show ALL attachments that have
    a File_URL — filtering by IsDeleted hides real files.
    File_URL must be non-empty to be useful.
    """
    p = f"{alias}." if alias else ""
    return f"({p}File_URL IS NOT NULL AND {p}File_URL != '')"


def _attachment_is_signed(file_url: str) -> bool:
    """A file is currently signed iff its pre-signature backup exists on
    disk (see api_sign_attachment / api_unsign_attachment below). Used so
    the UI only offers "Remove signature" on documents that actually have
    one, instead of showing it unconditionally regardless of state."""
    try:
        path = resolve_attachment_disk_path(file_url)
        return bool(path and os.path.isfile(path + ".unsigned.bak"))
    except Exception:
        return False


def format_attachment_row(row) -> dict:
    """Row: ID, Transaction_ID, File_Name, File_Description, File_URL, File_Size."""
    att_id = row[0]
    return {
        "id": att_id,
        "file_name": row[2] or "",
        "description": row[3] or "",
        "file_url": row[4] or "",
        "file_size": row[5],
        "preview_url": url_for("api_attachment_preview", attachment_id=att_id),
        "download_url": url_for("api_attachment_download", attachment_id=att_id),
        "is_signed": _attachment_is_signed(row[4]),
        "can_sign": True,
    }


def load_attachments_for_transactions(cursor, transaction_ids: list) -> dict:
    """One-to-many: Adco_Transactions.ID -> Adco_Transactions_Attachments.Transaction_ID."""
    ids = list({int(i) for i in transaction_ids if i is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    cursor.execute(
        f"""
        SELECT ID, Transaction_ID, File_Name, File_Description, File_URL, File_Size
        FROM dbo.Adco_Transactions_Attachments
        WHERE Transaction_ID IN ({placeholders})
          AND {_attachments_active_sql()}
        ORDER BY Transaction_ID, ID
        """,
        *ids,
    )
    by_tx = {}
    for row in cursor.fetchall():
        by_tx.setdefault(row[1], []).append(format_attachment_row(row))
    return by_tx


def load_folder_names(cursor, folder_ids: list) -> dict:
    """
    Load folder display names for the given IDs.
    Adco_Transactions.Foldes_ID -> Adco_Folder.ID (same rows as Sys_Department).
    We query both tables and merge so we always get a name.
    Uses auto-detected column names to handle schema variations.
    """
    ids = list({int(i) for i in folder_ids if i is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    result = {}
    ncol_adco = adco_folder_name_col(cursor)
    ncol_dept = dept_folder_name_col(cursor)
    for table, col in (
            ("dbo.Adco_Folder", ncol_adco),
            ("dbo.Sys_Department", ncol_dept),
    ):
        try:
            cursor.execute(
                f"SELECT ID, [{col}] FROM {table} WHERE ID IN ({placeholders})",
                *ids,
            )
            for row in cursor.fetchall():
                if row[0] not in result and row[1]:
                    result[row[0]] = row[1]
        except Exception as exc:
            print(f"load_folder_names [{table}] warning: {exc}")
    return result


def find_folder_by_name(cursor, name_query: str):
    """
    Case-insensitive substring match for a folder name, checked against both
    dbo.Adco_Folder and dbo.Sys_Department (same two name sources
    load_folder_names() merges). Used by bulk chatbot intents like "email all
    documents in <folder>" where the user types a folder name instead of an ID.
    Returns (folder_id, folder_name) for the first/best match, or (None, None).
    """
    name_query = (name_query or "").strip()
    if not name_query:
        return None, None
    like = f"%{name_query}%"
    ncol_adco = adco_folder_name_col(cursor)
    ncol_dept = dept_folder_name_col(cursor)
    for table, col in (("dbo.Adco_Folder", ncol_adco), ("dbo.Sys_Department", ncol_dept)):
        try:
            cursor.execute(
                f"""SELECT TOP 1 ID, [{col}] FROM {table}
                    WHERE [{col}] LIKE ? AND (IsDeleted = 0 OR IsDeleted IS NULL)
                    ORDER BY CASE WHEN [{col}] = ? THEN 0 ELSE 1 END, LEN([{col}])""",
                like, name_query,
            )
            row = cursor.fetchone()
            if row:
                return row[0], row[1]
        except Exception as exc:
            print(f"find_folder_by_name [{table}] warning: {exc}")
    return None, None


def _safe_filename_stem(original_name: str, fallback: str = "file") -> str:
    """Sanitize a user-supplied upload filename before it's used to build an
    on-disk path. os.path.splitext() alone does NOT protect against a
    filename like '../../../etc/whatever' — the '..' segments survive into
    base_name and let os.path.join()/os.rename() escape FILE_SAVE_DIR
    entirely (path traversal / arbitrary file write). werkzeug's
    secure_filename() strips path separators and '..' and normalizes the
    name to a safe ASCII-ish token. We only need the *stem* here since the
    caller re-appends its own extension/ID suffix.
    Returns `fallback` if sanitization leaves nothing usable (e.g. a name
    that was entirely path separators or unsafe characters).
    """
    stem = os.path.splitext(secure_filename(original_name or ""))[0]
    return stem or fallback


def resolve_attachment_disk_path(file_url: str):
    """Map File_URL from DB to a readable path on this server.

    New files store filename.ID.ext e.g. تصفية الموظف.8345.pdf
    Old legacy files store e.g. /Files/null/2259/012347082301filename.pdf
    Dev files store /uploads/filename.pdf
    In all cases basename is extracted and looked up in ATTACHMENT_FILES_ROOT (TempFiles).
    """
    if not file_url:
        return None
    url = file_url.strip()

    # ── 1. Full absolute path stored directly (new files) ────────────────────
    if os.path.isabs(url) and os.path.isfile(url):
        return url

    # ── 2. Dev /uploads/filename ─────────────────────────────────────────────
    if url.startswith("/uploads/"):
        local = os.path.join(UPLOAD_DIR, url[len("/uploads/"):].lstrip("/"))
        if os.path.isfile(local):
            return local

    # ── 3. Extract basename and look in ATTACHMENT_FILES_ROOT (TempFiles) ────
    #    Works for both legacy /Files/null/2257/filename.pdf and new filename.ID.ext
    basename = os.path.basename(url.replace("/", os.sep))
    if basename and ATTACHMENT_FILES_ROOT:
        candidate = os.path.join(ATTACHMENT_FILES_ROOT, basename)
        if os.path.isfile(candidate):
            return candidate

    # ── 4. Basename fallback in FILE_SAVE_DIR (dev / fallback) ───────────────
    if basename and FILE_SAVE_DIR and FILE_SAVE_DIR != ATTACHMENT_FILES_ROOT:
        candidate = os.path.join(FILE_SAVE_DIR, basename)
        if os.path.isfile(candidate):
            return candidate

    return None



# File extensions OCR can run on.
_OCR_IMAGE_EXTS = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"}
_OCR_PDF_EXTS = {"pdf"}

# Tesseract language(s). Default: English + Arabic if data is available.
OCR_LANGS = (os.getenv("OCR_LANGS") or "eng+ara").strip()

# Max pages of a PDF to OCR (avoid huge multi-hundred page docs blocking uploads).
OCR_MAX_PDF_PAGES = int(os.getenv("OCR_MAX_PDF_PAGES", "10"))

# Max characters of OCR text stored/returned.
OCR_MAX_CHARS = int(os.getenv("OCR_MAX_CHARS", "100000"))

# DPI for rasterising PDF pages before OCR. Lower = faster, higher = sharper
# (helps with small Arabic text) at the cost of speed.
OCR_PDF_DPI = int(os.getenv("OCR_PDF_DPI", "200"))

# EasyOCR: primary OCR engine when enabled (better Arabic+English accuracy
# than Tesseract on average). Falls back to Tesseract if unavailable/disabled.
USE_EASYOCR = (os.getenv("USE_EASYOCR", "0").strip() == "1") and (easyocr is not None)
USE_EASYOCR_GPU = os.getenv("USE_EASYOCR_GPU", "0").strip() == "1"

# EasyOCR language codes (different convention from Tesseract: 'ar' not 'ara').
_EASYOCR_LANG_MAP = {"eng": "en", "ara": "ar"}
OCR_EASYOCR_LANGS = [
    _EASYOCR_LANG_MAP.get(part.strip(), part.strip())
    for part in OCR_LANGS.split("+") if part.strip()
] or ["en"]

# Lazily-initialized EasyOCR reader (loading models is slow — do it once).
_easyocr_reader = None
_easyocr_init_lock = threading.Lock()

# Cap PyTorch's per-call thread usage so N parallel backfill workers don't
# each try to fan out across every CPU core at once (oversubscription).
# Without this, 4 worker threads on an 8-core box can run SLOWER than 1
# worker, because each readtext() call fights the others for the same cores.
# Override with OCR_TORCH_THREADS in .env if you want to tune it manually.
_OCR_TORCH_THREADS = int(os.getenv("OCR_TORCH_THREADS", "0"))  # 0 = auto


def _get_easyocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None and easyocr is not None:
        with _easyocr_init_lock:
            if _easyocr_reader is None:  # re-check inside the lock
                if _OCR_TORCH_THREADS > 0:
                    try:
                        import torch
                        torch.set_num_threads(_OCR_TORCH_THREADS)
                        print(f"[OCR] torch.set_num_threads({_OCR_TORCH_THREADS})")
                    except Exception as exc:
                        print(f"[OCR] could not cap torch threads: {exc}")
                print(f"[OCR] Loading EasyOCR reader (langs={OCR_EASYOCR_LANGS}, gpu={USE_EASYOCR_GPU})...")
                _easyocr_reader = easyocr.Reader(OCR_EASYOCR_LANGS, gpu=USE_EASYOCR_GPU)
                print("[OCR] EasyOCR reader ready.")
    return _easyocr_reader

# Explicit path to Poppler's binaries (pdftoppm, pdfinfo) for pdf2image.
# Needed when the app's subprocess PATH doesn't include Poppler
# (common when running via an IDE "Run" button rather than a shell).
# Auto-detects common Homebrew locations on macOS if not set via env.
def _detect_poppler_path() -> str | None:
    env_path = (os.getenv("OCR_POPPLER_PATH") or "").strip()
    if env_path:
        return env_path
    for candidate in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.isfile(os.path.join(candidate, "pdftoppm")):
            return candidate
    return None  # rely on PATH (typical on Linux/Windows servers)


OCR_POPPLER_PATH = _detect_poppler_path()


# Explicit path to the tesseract binary for pytesseract.
# Needed when the app's subprocess PATH doesn't include it
# (common when running via an IDE "Run" button rather than a shell).
# Auto-detects common Homebrew locations on macOS if not set via env.
def _detect_tesseract_cmd() -> str | None:
    env_cmd = (os.getenv("OCR_TESSERACT_CMD") or "").strip()
    if env_cmd:
        return env_cmd
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if os.path.isfile(candidate):
            return candidate
    return None  # rely on PATH (typical on Linux/Windows servers)


OCR_TESSERACT_CMD = _detect_tesseract_cmd()
if pytesseract is not None and OCR_TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = OCR_TESSERACT_CMD


def _extract_pdf_text_direct(file_path: str) -> str:
    """Extract embedded text directly from a digital (non-scanned) PDF."""
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(file_path)
        parts = []
        for page in reader.pages[:OCR_MAX_PDF_PAGES]:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
        return "\n".join(parts).strip()
    except Exception as exc:
        print(f"Direct PDF text extraction failed for {file_path}: {exc}")
        return ""


def _ocr_image_text(img) -> str:
    """
    Run OCR on a single PIL Image and return extracted text.
    Uses EasyOCR if enabled and available, otherwise Tesseract.
    Raises on failure (caller handles/logs).
    """
    if USE_EASYOCR:
        reader = _get_easyocr_reader()
        if reader is not None:
            import numpy as np
            arr = np.array(img.convert("RGB"))
            results = reader.readtext(arr, detail=0, paragraph=True)
            return "\n".join(results)
        # EasyOCR enabled but failed to init — fall back to Tesseract below.

    if pytesseract is None:
        raise RuntimeError("No OCR engine available (EasyOCR and Tesseract both unavailable)")
    return pytesseract.image_to_string(img, lang=OCR_LANGS)


def _extract_snippet(text: str, keyword: str, context_chars: int = 120) -> str:
    """Return a short excerpt of text around the first occurrence of keyword."""
    if not text or not keyword:
        return ""
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - context_chars // 2)
    end = min(len(text), idx + len(keyword) + context_chars // 2)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def extract_ocr_text_debug(file_path: str, file_ext: str) -> tuple[str, str]:
    """
    Same as extract_ocr_text, but also returns a short reason string
    describing how the text was obtained or why none was found —
    used by /api/ocr to give the user actionable feedback.
    Returns (text, reason).
    """
    if not file_path or not os.path.isfile(file_path):
        return "", "file_not_found"

    ext = (file_ext or "").lower().lstrip(".")

    if ext in _OCR_PDF_EXTS:
        direct_text = _extract_pdf_text_direct(file_path)
        if direct_text:
            if len(direct_text) > OCR_MAX_CHARS:
                direct_text = direct_text[:OCR_MAX_CHARS]
            return direct_text, "direct_pdf_text"
        # No embedded text — fall through to OCR (scanned/image-only PDF).

    if not USE_EASYOCR and (pytesseract is None or Image is None):
        return "", "ocr_unavailable"
    if Image is None:
        return "", "ocr_unavailable"

    text_parts = []
    engine_used = "easyocr" if USE_EASYOCR else "tesseract"

    try:
        if ext in _OCR_IMAGE_EXTS:
            with Image.open(file_path) as img:
                text_parts.append(_ocr_image_text(img))

        elif ext in _OCR_PDF_EXTS:
            if convert_from_path is None:
                return "", "pdf2image_unavailable"
            try:
                pages = convert_from_path(
                    file_path,
                    dpi=OCR_PDF_DPI,
                    first_page=1,
                    last_page=OCR_MAX_PDF_PAGES,
                    poppler_path=OCR_POPPLER_PATH,
                )
            except Exception as exc:
                print(f"pdf2image conversion failed for {file_path}: {exc}")
                return "", f"pdf_render_error: {exc}"
            for page_img in pages:
                text_parts.append(_ocr_image_text(page_img))
                page_img.close()
        else:
            return "", "unsupported_type"
    except Exception as exc:
        print(f"OCR extraction failed for {file_path}: {exc}")
        return "", f"ocr_error: {exc}"

    full_text = "\n".join(t for t in text_parts if t).strip()
    if len(full_text) > OCR_MAX_CHARS:
        full_text = full_text[:OCR_MAX_CHARS]
    reason = f"{engine_used}_text" if full_text else "no_text_detected"
    return full_text, reason


def _parse_ocr_requested_files(data) -> set:
    """
    Parse the 'ocr_requested_files' field sent alongside a document save —
    a JSON array of original filenames the user clicked "Extract Text" on.
    OCR is opt-in: only these filenames get OCR'd and stored; anything else
    the user merely attached (without using OCR) is saved as a plain file.
    """
    raw = (data.get("ocr_requested_files") or "").strip() if data else ""
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {str(name) for name in parsed if name}
    except Exception:
        pass
    return set()


def _parse_ocr_extracted_text(data) -> dict:
    """
    Parse the optional 'ocr_extracted_text' field — a JSON object mapping
    original filename -> text the user already saw in the "Extract Text"
    preview box before saving. When a filename has an entry here, we skip
    re-running OCR for it entirely during save and just store this text
    directly (it's already been computed once for the preview, no need to
    pay for it again in the background). Filenames with no entry fall back
    to the normal background-OCR path, e.g. older clients that don't send
    this field yet.
    """
    raw = (data.get("ocr_extracted_text") or "").strip() if data else ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                str(name): str(text)[:OCR_MAX_CHARS]
                for name, text in parsed.items()
                if name and text
            }
    except Exception:
        pass
    return {}


def extract_ocr_text(file_path: str, file_ext: str) -> str:
    """
    Best-effort text extraction for an image or PDF file.
    For PDFs, tries direct embedded-text extraction first (fast, accurate
    for digitally-generated PDFs), and falls back to OCR via Tesseract
    if no embedded text is found (e.g. scanned/image-only PDFs).
    Returns '' on any failure or if libraries are unavailable —
    never raises, so it can't break an upload.
    """
    text, _reason = extract_ocr_text_debug(file_path, file_ext)
    return text


def _run_ocr_jobs_async(jobs: list[tuple[int, str, str]]) -> None:
    """
    Run OCR extraction + the resulting DB update on a background thread.

    OCR (especially EasyOCR on scanned PDFs) can take 2-3+ minutes per file.
    Running it inline during a save/update request held the HTTP request
    (and the SQL transaction) open the whole time. This is only ever called
    AFTER the caller's transaction has committed, so the attachment row
    already exists; we just fill in its OCR text a bit later using a fresh
    connection (the request's connection will already be closed by then).

    `jobs` is a list of (attachment_id, file_path, file_ext) tuples — only
    for attachments the user explicitly clicked "Extract Text" on.
    """
    if not jobs:
        return

    def _worker():
        for att_id, file_path, file_ext in jobs:
            try:
                ocr_text = extract_ocr_text(file_path, file_ext)
                if ocr_text:
                    # OCR text is cached locally on disk, never written to SQL Server.
                    ocr_cache_write(att_id, os.path.basename(file_path), ocr_text)
            except Exception as exc:
                print(f"[OCR async] failed for attachment {att_id}: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


# ── Backfill: extract text for existing attachments (CLI only) ─────────────
# Run with: python app.py backfill-ocr [--limit N] [--workers N]
# Safe to stop and re-run any time — skips attachments already cached, only
# touches image/PDF attachments, and never modifies SQL Server (text is
# cached locally on disk, same as normal usage). Uses the same extraction
# path as regular uploads: direct embedded-text first (fast, no OCR engine),
# OCR fallback only for scanned/image-only files.
def _ocr_backfill_pending_attachments(cursor, limit=None):
    """Return (att_id, file_url, file_ext) for attachments not yet cached."""
    active_sql = _attachments_active_sql()
    cursor.execute(f"""
        SELECT ID, File_URL, File_Type_ID
        FROM dbo.Adco_Transactions_Attachments
        WHERE {active_sql}
        ORDER BY ID DESC
    """)
    rows = cursor.fetchall()

    extractable_exts = _OCR_IMAGE_EXTS | _OCR_PDF_EXTS
    pending = []
    for att_id, file_url, file_ext in rows:
        ext = (file_ext or "").lower().lstrip(".")
        if ext not in extractable_exts:
            continue
        try:
            already_cached = bool(ocr_cache_read(att_id))
        except Exception:
            already_cached = False
        if already_cached:
            continue
        pending.append((att_id, file_url, ext))
        if limit and len(pending) >= limit:
            break
    return pending


def _ocr_backfill_process_one(att_id: int, file_url: str, ext: str) -> tuple[int, str]:
    """Returns (att_id, status) where status is one of:
    'cached', 'no_text', 'file_missing', 'error'."""
    try:
        file_path = resolve_attachment_disk_path(file_url)
        if not file_path:
            return att_id, "file_missing"

        text = extract_ocr_text(file_path, ext)
        if text:
            ocr_cache_write(att_id, os.path.basename(file_path), text)
            return att_id, "cached"
        return att_id, "no_text"
    except Exception as exc:
        print(f"  ! attachment {att_id} failed: {exc}", file=sys.stderr)
        return att_id, "error"


def run_ocr_backfill(limit=None, workers=1):
    """Extract text for every existing attachment not yet in the cache."""
    if not OCR_ENABLED:
        print("OCR_ENABLED is False — nothing to do.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    pending = _ocr_backfill_pending_attachments(cursor, limit=limit)
    conn.close()

    total = len(pending)
    print(f"Found {total} attachment(s) needing text extraction.")
    if not total:
        return

    counts = {"cached": 0, "no_text": 0, "file_missing": 0, "error": 0}
    start = time.time()

    # Load the EasyOCR model ONCE, before spawning workers — otherwise every
    # worker thread races to load it independently on the first batch.
    if USE_EASYOCR:
        _get_easyocr_reader()

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_ocr_backfill_process_one, *job): job[0] for job in pending}
            for i, future in enumerate(as_completed(futures), 1):
                att_id, status = future.result()
                counts[status] += 1
                if i % 10 == 0 or i == total:
                    elapsed_so_far = time.time() - start
                    rate = i / elapsed_so_far * 60  # items/min
                    remaining_min = (total - i) / (i / elapsed_so_far) / 60 if i else 0
                    print(f"  [{i}/{total}] {rate:.1f}/min, ~{remaining_min:.0f} min left "
                          f"(last: attachment {att_id} -> {status})")
    else:
        for i, job in enumerate(pending, 1):
            att_id, status = _ocr_backfill_process_one(*job)
            counts[status] += 1
            if i % 10 == 0 or i == total:
                elapsed_so_far = time.time() - start
                rate = i / elapsed_so_far * 60
                remaining_min = (total - i) / (i / elapsed_so_far) / 60 if i else 0
                print(f"  [{i}/{total}] {rate:.1f}/min, ~{remaining_min:.0f} min left "
                      f"(last: attachment {att_id} -> {status})")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s — cached: {counts['cached']}, "
          f"no text found: {counts['no_text']}, "
          f"file missing: {counts['file_missing']}, "
          f"errors: {counts['error']}")


def adco_folder_desc_sql(prefix: str = "", cursor=None) -> str:
    """
    SQL expression for folder description in SELECT lists.
    Env ADCO_FOLDER_DESC_COLUMN: real column name, default Folder_Desc, or __none__ if absent.
    With a cursor: auto-detects via INFORMATION_SCHEMA if env not set.
    Set ADCO_FOLDER_DESC_COLUMN=__none__ if the column does not exist.
    """
    raw = (os.getenv("ADCO_FOLDER_DESC_COLUMN", "") or "").strip()
    if raw.lower() == "__none__":
        return "CAST(N'' AS NVARCHAR(4000))"
    if raw and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        col = raw
    elif cursor is not None:
        col = _detect_column(
            cursor, "dbo.Adco_Folder",
            ["Folder_Desc", "Description", "Desc", "FolderDesc", "Notes"],
            "__none__",
        )
        if col == "__none__":
            return "CAST(N'' AS NVARCHAR(4000))"
    else:
        col = "Folder_Desc"
    p = f"{prefix}." if prefix else ""
    return f"{p}[{col}]"


def get_db_connection():
    if pyodbc is None:
        raise RuntimeError("pyodbc is not installed.")
    # Full ODBC string overrides individual settings (use for Trusted_Connection, etc.)
    conn_str = (os.getenv("SQLSERVER_CONNSTRING") or "").strip()
    if conn_str:
        return pyodbc.connect(conn_str)
    driver = os.getenv("SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server")
    server = os.getenv("SQLSERVER_SERVER", "localhost,1433")
    database = os.getenv("SQLSERVER_DATABASE", "ArchiveDB")
    uid = os.getenv("SQLSERVER_UID", os.getenv("SQLSERVER_USER", ""))
    pwd = os.getenv("SQLSERVER_PWD", os.getenv("SQLSERVER_PASSWORD", ""))
    if not uid or not pwd:
        raise RuntimeError("SQLSERVER_UID/SQLSERVER_PWD must be set in your environment/.env file.")
    return pyodbc.connect(
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={uid};"
        f"PWD={pwd};"
        f"TrustServerCertificate=yes;"
    )


def parse_dep_id_from(raw) -> set[int]:
    """
    Single source of truth for parsing Sys_User.Dep_ID_From into a set of
    department IDs (ints). Tolerant of stray whitespace, empty segments,
    trailing commas, and non-numeric junk — anything that doesn't cleanly
    parse as an int is skipped rather than silently breaking the whole field.
    """
    result = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def get_current_role():
    # Admin: username == 'admin', USER_ID == 1, or USER_FULLNAME == 'admin'
    user_name = str(session.get("user_name", "")).strip().lower()
    if user_name == "admin":
        return "admin"
    try:
        if int(session.get("user_id", -1)) == 1:
            return "admin"
    except (TypeError, ValueError):
        pass
    user_full = str(session.get("user_full", "")).strip().lower()
    if user_full == "admin":
        return "admin"
    return "manager"


def get_allowed_dep_ids() -> list[int] | None:
    """
    Returns the list of Sys_Department.ID values the current user may access,
    derived from Sys_User.Dep_ID_From (comma-separated).
    Returns None for admins (no restriction — full access).
    Returns an empty list if the user has no departments assigned.

    Reads fresh from the database on every call so that admin changes to a
    user's department access take effect immediately (without requiring the
    user to log out and back in).
    """
    if get_current_role() == "admin":
        return None  # admin: unrestricted

    user_id = session.get("user_id")
    if not user_id:
        return []

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Dep_ID_From FROM dbo.Sys_User WHERE USER_ID = ? AND IsDeleted = 0",
            user_id,
        )
        row = cursor.fetchone()
        if not row:
            return []
        return sorted(parse_dep_id_from(row[0]))
    except Exception:
        # Fallback to session cache if DB is unreachable
        return session.get("allowed_dep_ids", [])
    finally:
        if conn:
            conn.close()


def _current_user_doc_access_clause(cursor, alias="t"):
    """
    Build the document visibility rule for the current user.
    Admins see everything.
    Regular users see:
      - documents in departments they have been granted via Dep_ID_From
    """
    allowed = get_allowed_dep_ids()
    if allowed is None:
        return "1=1", []

    parts = [f"({alias}.IsDeleted = 0 OR {alias}.IsDeleted IS NULL)"]
    params = []

    if not allowed:
        return "1=0", []

    placeholders = ",".join("?" * len(allowed))
    dcol = adco_folder_dept_col(cursor)
    dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
    parts.append(
        f"{alias}.Foldes_ID IN ("
        f"SELECT ID FROM dbo.Adco_Folder WHERE {dept_bracket} IN ({placeholders})"
        f" AND IsDeleted = 0)"
    )
    params.extend(allowed)
    return " AND ".join(parts), params



def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_name" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


@app.route("/")
def home():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT USER_ID, USER_FULLNAME, USER_NAME, USER_TYPE_ID, Dep_ID, Dep_ID_From
                FROM dbo.Sys_User
                WHERE USER_NAME = ?
                  AND USER_PASSWORD = ?
                  AND IsDeleted = 0
                """,
                username, password,
            )
            user = cursor.fetchone()
            if user:
                session.permanent = True
                session["user_id"] = user[0]
                session["user_full"] = user[1]
                session["user_name"] = user[2]
                session["user_type"] = user[3]
                session["dep_id"] = user[4]
                # Parse Dep_ID_From: comma-separated dept IDs the user can access
                # e.g. "46,53,60" → [46, 53, 60]
                session["allowed_dep_ids"] = sorted(parse_dep_id_from(user[5]))
                audit_log("LOGIN", page_id=None, notes=f"User '{username}' logged in", user_id=user[0])
                return redirect(url_for("dashboard"))
            error = "Invalid username or password"
            audit_log("LOGIN_FAILED", page_id=None, notes=f"Failed login attempt for username '{username}'", user_id=None)
        except Exception as exc:
            error = f"Connection error: {exc}"
        finally:
            if conn: conn.close()
    return render_template("login.html", error=error)


@app.route("/dashboard")
def dashboard():
    if "user_name" not in session:
        return redirect(url_for("login"))
    # Always read allowed dept IDs fresh from DB so that admin permission
    # changes take effect immediately without requiring a user re-login.
    # get_allowed_dep_ids() returns None for admins (unrestricted) and a
    # fresh list from Sys_User.Dep_ID_From for regular users.
    fresh_allowed = get_allowed_dep_ids()  # reads DB every time
    if fresh_allowed is not None:
        # Keep session in sync so other code that falls back to session cache
        # (e.g. on DB outage) also has the latest value.
        session["allowed_dep_ids"] = fresh_allowed
        allowed_deps_str = ",".join(str(d) for d in fresh_allowed)
    else:
        allowed_deps_str = ""  # admin: no restriction, empty string = show all
    return render_template(
        "dashboard.html",
        current_role=get_current_role(),
        user_full=session.get("user_full"),
        user_name=session.get("user_name"),
        user_type=session.get("user_type"),
        dep_id=session.get("dep_id"),
        allowed_deps=allowed_deps_str,
    )


# ══════════════════════════════════════════════════════════════════════════════
# For understanding of how data connects in database DB
# REAL SCHEMA (from actual DB data):
#   Sys_Department  — main/top-level folders (one row per department group)
#     ID            = primary key (row PK)
#     Folder_Name   = display name  ← NOT "Name"
#     Dept_ID       = numeric dept key (e.g. 46, 53, 153); used by Adco_Folder
#     Parent_ID     = 0 for top-level
#     IsDeleted, Enable, ParentType
#
#   Adco_Folder     — subfolders (and mirrored top-level stubs)
#     ID            = primary key / folder ID used in transactions
#     Folder_Name   = display name
#     Dept_ID       = links to Sys_Department.Dept_ID (NOT .ID)
#     Parent_ID     = 0 = top-level stub, >0 = real subfolder
#     IsDeleted
#
#   Adco_Transactions
#     Foldes_ID     = Adco_Folder.ID  (the subfolder the doc lives in)
#
# UI model:
#   "Entity"  = a Sys_Department row, shown by Folder_Name
#   The tree:
#     [Sys_Department rows]  ← top-level entities shown as main folders
#       └─ [Adco_Folder rows where Dept_ID == Sys_Department.Dept_ID]
#
#   The key that links them: Sys_Department.Dept_ID == Adco_Folder.Dept_ID
#   We pass Sys_Department.Dept_ID to /api/folders to load children.
# ══════════════════════════════════════════════════════════════════════════════


# ── API: Load entities (Sys_Department = real departments) ───────────────
# Sys_Department columns: ID, Name, Parent_Id, IsDeleted
# These are the top-level groups. Adco_Folder rows link to them via Dept_ID.
@app.route("/api/entities")
@login_required
def api_entities():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ncol = dept_folder_name_col(cursor)  # e.g. Folder_Name, Name, Dep_Name
        dcol = sys_department_dept_col(cursor)  # e.g. Dept_ID, Dep_ID
        force_all = request.args.get("all") == "1" and get_current_role() == "admin"
        allowed = None if force_all else get_allowed_dep_ids()  # None = admin (no filter), list = restricted
        if allowed is None:
            # Admin: see all departments
            cursor.execute(f"""
                SELECT DISTINCT d.ID, d.[{ncol}], d.[{dcol}]
                FROM dbo.Sys_Department d
                WHERE (d.Isdel = 0 OR d.Isdel IS NULL)
                  AND d.[{ncol}] IS NOT NULL AND d.[{ncol}] != ''
                ORDER BY d.[{ncol}]
            """)
        elif not allowed:
            # User has no departments assigned — return empty
            return jsonify([])
        else:
            placeholders = ",".join("?" * len(allowed))
            cursor.execute(f"""
                SELECT DISTINCT d.ID, d.[{ncol}], d.[{dcol}]
                FROM dbo.Sys_Department d
                WHERE (d.Isdel = 0 OR d.Isdel IS NULL)
                  AND d.[{ncol}] IS NOT NULL AND d.[{ncol}] != ''
                  AND d.ID IN ({placeholders})
                ORDER BY d.[{ncol}]
            """, *allowed)
        rows = cursor.fetchall()
        # id = Sys_Department.ID (row PK)
        # dept_id = Sys_Department.<dcol>  (the value used in Adco_Folder dept col)
        return jsonify([{
            "id": r[0],
            "name": r[1],
            "dept_id": r[2] if r[2] is not None else r[0],
        } for r in rows])
    except Exception as e:
        import traceback;
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


def _auto_grant_dept_access(cursor, user_id, dept_id):
    """
    Ensure the given Sys_Department.ID is in the user's Dep_ID_From list.
    If it's already there, this is a no-op. Otherwise, appends it.
    Uses the cursor's existing connection/transaction.
    Admins are skipped (they have unrestricted access).
    """
    try:
        cursor.execute(
            "SELECT Dep_ID_From FROM dbo.Sys_User WHERE USER_ID = ? AND IsDeleted = 0",
            user_id,
        )
        row = cursor.fetchone()
        if not row:
            return
        existing = parse_dep_id_from(row[0])
        if int(dept_id) in existing:
            return  # already has access
        existing.add(int(dept_id))
        new_val = ",".join(str(d) for d in sorted(existing))
        cursor.execute(
            "UPDATE dbo.Sys_User SET Dep_ID_From = ? WHERE USER_ID = ?",
            new_val, user_id,
        )
    except Exception as exc:
        print(f"[auto-grant] WARNING: could not grant dept {dept_id} to user {user_id}: {exc}")


# ── API: Create main folder (Sys_Department) ──────────────────────────────
@app.route("/api/entities", methods=["POST"])
@login_required
def api_create_entity():
    conn = None
    try:
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        dept_id = data.get("dept_id") or session.get("dep_id") or 0
        if not name:
            return jsonify({"error": "Missing folder name"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()

        # Check for duplicate name
        ncol_dept = dept_folder_name_col(cursor)
        cursor.execute(
            f"SELECT COUNT(*) FROM dbo.Sys_Department WHERE [{ncol_dept}] = ? AND (Isdel = 0 OR Isdel IS NULL)",
            name
        )
        if cursor.fetchone()[0] > 0:
            return jsonify({"error": f"A main folder named '{name}' already exists"}), 409

        # Get next folder code
        cursor.execute("""
                       SELECT ISNULL(MAX(Code), 0) + 1
                       FROM dbo.Sys_Department
                       WHERE (Isdel = 0 OR Isdel IS NULL)
                       """)
        next_code = cursor.fetchone()[0] or 1

        # Insert into Sys_Department
        cursor.execute(f"""
            INSERT INTO dbo.Sys_Department
                (Code, [{ncol_dept}], Parent_Id, Isdel, CreatedBy, CreatedOn)
            VALUES (?, ?, 0, 0, ?, GETDATE())
        """, next_code, name, session["user_id"])

        # ── Fix #8: SCOPE_IDENTITY() is broken by triggers; use MAX(ID) filtered
        # by user + tight timestamp window instead (same pattern as api_save_document).
        cursor.execute("""
                       SELECT MAX(ID)
                       FROM dbo.Sys_Department
                       WHERE CreatedBy = ?
                         AND (Isdel = 0 OR Isdel IS NULL)
                         AND CreatedOn >= DATEADD(SECOND, -3, GETDATE())
                       """, session["user_id"])
        row = cursor.fetchone()
        new_id = row[0] if row and row[0] else None
        if not new_id:
            raise RuntimeError("Could not retrieve new Sys_Department ID after INSERT")

        # Auto-grant the creator access to the new folder
        _auto_grant_dept_access(cursor, session["user_id"], new_id)

        conn.commit()
        return jsonify({"success": True, "id": new_id, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Rename main folder (Sys_Department) ──────────────────────────────
@app.route("/api/entities/<int:entity_id>", methods=["PATCH"])
@login_required
def api_rename_entity(entity_id):
    conn = None
    try:
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Missing name"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        ncol = dept_folder_name_col(cursor)
        cursor.execute(
            f"SELECT ID FROM dbo.Sys_Department WHERE ID = ? AND (Isdel = 0 OR Isdel IS NULL)",
            entity_id
        )
        if not cursor.fetchone():
            return jsonify({"error": "Department not found"}), 404
        cursor.execute(
            f"UPDATE dbo.Sys_Department SET [{ncol}] = ? WHERE ID = ?",
            name, entity_id
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Get Fe1Name–Fe7Name for a Sys_Department row ─────────────────────
@app.route("/api/entities/<int:entity_id>/fields", methods=["GET"])
@login_required
def api_get_entity_fields(entity_id):
    """Return the 7 custom field name labels stored in Sys_Department.Fe1Name–Fe7Name."""
    conn = None
    try:
        # Ensure columns exist (no-op if already present, uses its own DDL connection)
        _ensure_fe_columns_sys_department()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT Fe1Name, Fe2Name, Fe3Name, Fe4Name, Fe5Name, Fe6Name, Fe7Name
            FROM dbo.Sys_Department
            WHERE ID = ? AND (Isdel = 0 OR Isdel IS NULL)
        """, entity_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Department not found"}), 404
        return jsonify({
            "Fe1Name": row[0] or "", "Fe2Name": row[1] or "", "Fe3Name": row[2] or "",
            "Fe4Name": row[3] or "", "Fe5Name": row[4] or "", "Fe6Name": row[5] or "",
            "Fe7Name": row[6] or "",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Save Fe1Name–Fe7Name for a Sys_Department row ────────────────────
@app.route("/api/entities/<int:entity_id>/fields", methods=["POST"])
@login_required
def api_save_entity_fields(entity_id):
    """Save the 7 custom field labels to Sys_Department.Fe1Name–Fe7Name."""
    conn = None
    try:
        data = request.get_json() or {}

        # Step 1: ensure the columns exist in both tables (DDL on its own autocommit connection)
        # Run this FIRST and independently — even if it partially fails, still try the UPDATE
        try:
            _ensure_fe_columns_sys_department()
        except Exception as ddl_err:
            print(f"[Fe migration] WARNING ensuring Sys_Department columns: {ddl_err}")
        try:
            _ensure_fe_columns_adco_transactions()
        except Exception as ddl_err:
            print(f"[Fe migration] WARNING ensuring Adco_Transactions columns: {ddl_err}")

        # Step 2: update the field name labels on the Sys_Department row
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ID FROM dbo.Sys_Department WHERE ID = ? AND (Isdel = 0 OR Isdel IS NULL)",
            entity_id
        )
        if not cursor.fetchone():
            return jsonify({"error": "Department not found"}), 404
        cursor.execute("""
            UPDATE dbo.Sys_Department
            SET Fe1Name = ?, Fe2Name = ?, Fe3Name = ?, Fe4Name = ?,
                Fe5Name = ?, Fe6Name = ?, Fe7Name = ?
            WHERE ID = ?
        """,
            data.get("Fe1Name") or None, data.get("Fe2Name") or None,
            data.get("Fe3Name") or None, data.get("Fe4Name") or None,
            data.get("Fe5Name") or None, data.get("Fe6Name") or None,
            data.get("Fe7Name") or None,
            entity_id
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


def _ensure_sys_dp_dl_table(cursor):
    """Create Sys_DP_DL if it doesn't exist."""
    cursor.execute("""
        IF NOT EXISTS (
            SELECT 1 FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_DP_DL'
        )
        BEGIN
            CREATE TABLE dbo.Sys_DP_DL (
                ID           INT IDENTITY(1,1) PRIMARY KEY,
                Dept_Id      INT            NOT NULL,
                FieldName    NVARCHAR(255)  NOT NULL,
                OptionOrder  INT            NOT NULL,
                OptionValue  NVARCHAR(500)  NOT NULL
            )
        END
    """)


# ── API: Get dropdown options from Sys_DP_DL ──────────────────────────────
@app.route("/api/entities/<int:entity_id>/dropdown-options", methods=["GET"])
@login_required
def api_get_dropdown_options(entity_id):
    """
    Return dropdown options for a department.
    FieldName in Sys_DP_DL is the actual label stored in Fe1Name/Fe2Name/Fe3Name.
    Response: { "Fe1": { "label": "Document Type", "options": ["opt1","opt2"] }, ... }
    """
    conn = None
    try:
        conn = get_db_connection()
        conn.autocommit = True
        cursor = conn.cursor()
        _ensure_sys_dp_dl_table(cursor)

        # Get the field labels from Sys_Department
        cursor.execute("""
            SELECT Fe1Name, Fe2Name, Fe3Name
            FROM dbo.Sys_Department
            WHERE ID = ? AND (Isdel = 0 OR Isdel IS NULL)
        """, entity_id)
        row = cursor.fetchone()
        labels = {
            "Fe1": (row[0] or "").strip() if row else "",
            "Fe2": (row[1] or "").strip() if row else "",
            "Fe3": (row[2] or "").strip() if row else "",
        }

        # Get options — FieldName is the label text
        cursor.execute("""
            SELECT FieldName, OptionOrder, OptionValue
            FROM dbo.Sys_DP_DL
            WHERE Dept_Id = ?
            ORDER BY FieldName, OptionOrder
        """, entity_id)

        # Map label → options list
        label_to_options = {}
        for field_name, option_order, option_value in cursor.fetchall():
            if field_name not in label_to_options:
                label_to_options[field_name] = []
            label_to_options[field_name].append(option_value)

        # Build response keyed by Fe1/Fe2/Fe3
        result = {}
        for fe_key, label in labels.items():
            result[fe_key] = {
                "label": label,
                "options": label_to_options.get(label, [])
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Save dropdown options to Sys_DP_DL ───────────────────────────────
@app.route("/api/entities/<int:entity_id>/dropdown-options", methods=["POST"])
@login_required
def api_save_dropdown_options(entity_id):
    """
    Save dropdown options to Sys_DP_DL.
    FieldName stored = the actual label from Fe1Name/Fe2Name/Fe3Name in Sys_Department.
    Payload: { "Fe1": ["opt1","opt2"], "Fe2": [...], "Fe3": [...] }
    """
    conn = None
    try:
        data = request.get_json() or {}
        conn = get_db_connection()
        conn.autocommit = True
        cursor = conn.cursor()
        _ensure_sys_dp_dl_table(cursor)

        # Get current field labels from Sys_Department
        cursor.execute("""
            SELECT Fe1Name, Fe2Name, Fe3Name
            FROM dbo.Sys_Department
            WHERE ID = ? AND (Isdel = 0 OR Isdel IS NULL)
        """, entity_id)
        row = cursor.fetchone()
        labels = {
            "Fe1": (row[0] or "").strip() if row else "",
            "Fe2": (row[1] or "").strip() if row else "",
            "Fe3": (row[2] or "").strip() if row else "",
        }

        # Delete existing options for this department
        cursor.execute("DELETE FROM dbo.Sys_DP_DL WHERE Dept_Id = ?", entity_id)

        # Insert fresh — FieldName = the actual label text
        for fe_key, options in data.items():
            label = labels.get(fe_key, "").strip()
            if not label or not isinstance(options, list):
                continue
            for order, value in enumerate(options, start=1):
                value = (value or "").strip()
                if not value:
                    continue
                cursor.execute("""
                    INSERT INTO dbo.Sys_DP_DL (Dept_Id, FieldName, OptionOrder, OptionValue)
                    VALUES (?, ?, ?, ?)
                """, entity_id, label, order, value)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


def _get_ddl_connection():
    """
    Return a pyodbc connection with autocommit=True for DDL statements.
    SQL Server requires DDL (ALTER TABLE) to run outside a transaction —
    the only safe way with pyodbc is to pass autocommit=True at connect time.
    """
    if pyodbc is None:
        raise RuntimeError("pyodbc is not installed.")
    conn_str = (os.getenv("SQLSERVER_CONNSTRING") or "").strip()
    if conn_str:
        return pyodbc.connect(conn_str, autocommit=True)
    driver   = os.getenv("SQLSERVER_DRIVER",   "ODBC Driver 17 for SQL Server")
    server   = os.getenv("SQLSERVER_SERVER",   "localhost,1433")
    database = os.getenv("SQLSERVER_DATABASE", "ArchiveDB")
    uid      = os.getenv("SQLSERVER_UID",      os.getenv("SQLSERVER_USER",     ""))
    pwd      = os.getenv("SQLSERVER_PWD",      os.getenv("SQLSERVER_PASSWORD", ""))
    if not uid or not pwd:
        raise RuntimeError("SQLSERVER_UID/SQLSERVER_PWD must be set in your environment/.env file.")
    return pyodbc.connect(
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={uid};"
        f"PWD={pwd};"
        f"TrustServerCertificate=yes;",
        autocommit=True,
    )


def _ensure_fe_columns_sys_department():
    """
    Add Fe1Name–Fe7Name (NVARCHAR 255 NULL) to Sys_Department if missing.
    Uses a dedicated autocommit connection so ALTER TABLE always succeeds.
    Returns a list of columns that were actually added (empty = all existed).
    """
    added = []
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
              AND TABLE_NAME   = 'Sys_Department'
              AND COLUMN_NAME  IN (
                  'Fe1Name','Fe2Name','Fe3Name','Fe4Name','Fe5Name','Fe6Name','Fe7Name')
        """)
        existing = {r[0] for r in cur.fetchall()}
        for i in range(1, 8):
            col = f"Fe{i}Name"
            if col not in existing:
                cur.execute(f"ALTER TABLE dbo.Sys_Department ADD [{col}] NVARCHAR(255) NULL")
                added.append(col)
                print(f"[Fe migration] Added dbo.Sys_Department.[{col}]")
    finally:
        conn.close()
    return added


def _ensure_fe_columns_adco_transactions():
    """
    Add Fe1–Fe7 (NVARCHAR 500 NULL) to Adco_Transactions if missing.
    Uses a dedicated autocommit connection so ALTER TABLE always succeeds.
    Returns a list of columns that were actually added (empty = all existed).
    """
    added = []
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
              AND TABLE_NAME   = 'Adco_Transactions'
              AND COLUMN_NAME  IN ('Fe1','Fe2','Fe3','Fe4','Fe5','Fe6','Fe7')
        """)
        existing = {r[0] for r in cur.fetchall()}
        for i in range(1, 8):
            col = f"Fe{i}"
            if col not in existing:
                cur.execute(f"ALTER TABLE dbo.Adco_Transactions ADD [{col}] NVARCHAR(500) NULL")
                added.append(col)
                print(f"[Fe migration] Added dbo.Adco_Transactions.[{col}]")
    finally:
        conn.close()
    return added


def _ensure_smtp_columns_sys_user():
    """
    Add SmtpEmail (NVARCHAR 255 NULL) and SmtpPasswordEnc (NVARCHAR 1000 NULL)
    to Sys_User if missing, so each user can send email as themselves.
    The password is stored ENCRYPTED (Fernet, app-side) — never plaintext.
    Uses a dedicated autocommit connection so ALTER TABLE always succeeds.
    Returns a list of columns that were actually added (empty = all existed).
    """
    added = []
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
              AND TABLE_NAME   = 'Sys_User'
              AND COLUMN_NAME  IN ('SmtpEmail', 'SmtpPasswordEnc')
        """)
        existing = {r[0] for r in cur.fetchall()}
        if 'SmtpEmail' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD SmtpEmail NVARCHAR(255) NULL")
            added.append('SmtpEmail')
            print("[SMTP migration] Added dbo.Sys_User.[SmtpEmail]")
        if 'SmtpPasswordEnc' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD SmtpPasswordEnc NVARCHAR(1000) NULL")
            added.append('SmtpPasswordEnc')
            print("[SMTP migration] Added dbo.Sys_User.[SmtpPasswordEnc]")
    finally:
        conn.close()
    return added


def _ensure_shared_mail_columns_sys_user():
    """
    Adds the columns needed for Control Panel -> Mail Settings (the shared/
    default mailbox), piggybacking on the SmtpEmail/SmtpPasswordEnc columns
    already added by _ensure_smtp_columns_sys_user() above:
      - SmtpServer          NVARCHAR(255) NULL
      - SmtpPort            INT           NULL
      - SmtpUseSSL          BIT           NULL
      - IsSharedMailAccount BIT           NOT NULL DEFAULT 0
    Whichever single row has IsSharedMailAccount = 1 is "the shared mailbox"
    — that row's SmtpEmail/SmtpPasswordEnc double as both its owner's
    personal send-as config (if any) AND the shared fallback used by
    everyone else. See get_shared_smtp_config() for the read side.
    Uses a dedicated autocommit connection so ALTER TABLE always succeeds.
    Returns a list of columns that were actually added (empty = all existed).
    """
    added = []
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo'
              AND TABLE_NAME   = 'Sys_User'
              AND COLUMN_NAME  IN ('SmtpServer', 'SmtpPort', 'SmtpUseSSL', 'IsSharedMailAccount')
        """)
        existing = {r[0] for r in cur.fetchall()}
        if 'SmtpServer' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD SmtpServer NVARCHAR(255) NULL")
            added.append('SmtpServer')
            print("[Mail Settings migration] Added dbo.Sys_User.[SmtpServer]")
        if 'SmtpPort' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD SmtpPort INT NULL")
            added.append('SmtpPort')
            print("[Mail Settings migration] Added dbo.Sys_User.[SmtpPort]")
        if 'SmtpUseSSL' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD SmtpUseSSL BIT NULL")
            added.append('SmtpUseSSL')
            print("[Mail Settings migration] Added dbo.Sys_User.[SmtpUseSSL]")
        if 'IsSharedMailAccount' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD IsSharedMailAccount BIT NOT NULL DEFAULT 0")
            added.append('IsSharedMailAccount')
            print("[Mail Settings migration] Added dbo.Sys_User.[IsSharedMailAccount]")
    finally:
        conn.close()
    return added


def _ensure_wf_expiry_alert_column_sys_user():
    """
    Adds dbo.Sys_User.WfExpiryAlertDays INT NULL — each user's own
    "alert me X days before expiry" preference for the Workflow expiry
    banner on the Archive page. NULL means the user hasn't opted in yet
    (feature off for them); 0 is a valid "alert on the day it expires".
    Uses a dedicated autocommit connection so ALTER TABLE always succeeds.
    Returns a list of columns that were actually added (empty = already existed).
    """
    added = []
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'Sys_User'
              AND COLUMN_NAME = 'WfExpiryAlertDays'
        """)
        existing = {r[0] for r in cur.fetchall()}
        if 'WfExpiryAlertDays' not in existing:
            cur.execute("ALTER TABLE dbo.Sys_User ADD WfExpiryAlertDays INT NULL")
            added.append('WfExpiryAlertDays')
            print("[Workflow migration] Added dbo.Sys_User.[WfExpiryAlertDays]")
    finally:
        conn.close()
    return added


def _run_fe_startup_migration():
    """
    Called once at startup: adds Fe columns to both tables if they don't exist,
    and ensures Sys_AccR is created and seeded.
    Never crashes the server — errors are printed and swallowed.
    """
    if pyodbc is None:
        print("[Fe migration] Skipped — pyodbc not installed.")
        return
    try:
        print("[Fe migration] Running startup column check…")
        dept_added = _ensure_fe_columns_sys_department()
        txn_added  = _ensure_fe_columns_adco_transactions()
        if not dept_added and not txn_added:
            print("[Fe migration] All Fe columns already exist — nothing to do.")
        else:
            print(f"[Fe migration] Done. Added: {dept_added + txn_added}")
    except Exception as exc:
        print(f"[Fe migration] ERROR during startup migration: {exc}")
    try:
        _ensure_accr_table()
    except Exception as exc:
        print(f"[AccR migration] ERROR during startup: {exc}")
    try:
        smtp_added = _ensure_smtp_columns_sys_user()
        if not smtp_added:
            print("[SMTP migration] SmtpEmail/SmtpPasswordEnc already exist — nothing to do.")
        else:
            print(f"[SMTP migration] Done. Added: {smtp_added}")
    except Exception as exc:
        print(f"[SMTP migration] ERROR during startup: {exc}")
    try:
        shared_mail_added = _ensure_shared_mail_columns_sys_user()
        if not shared_mail_added:
            print("[Mail Settings migration] Shared mail columns already exist — nothing to do.")
        else:
            print(f"[Mail Settings migration] Done. Added: {shared_mail_added}")
    except Exception as exc:
        print(f"[Mail Settings migration] ERROR during startup: {exc}")
    try:
        wf_expiry_added = _ensure_wf_expiry_alert_column_sys_user()
        if not wf_expiry_added:
            print("[Workflow migration] WfExpiryAlertDays already exists — nothing to do.")
        else:
            print(f"[Workflow migration] Done. Added: {wf_expiry_added}")
    except Exception as exc:
        print(f"[Workflow migration] ERROR during startup: {exc}")


# NOTE (audit, verified): this route has no UI caller anywhere in
# dashboard.js/html by design — it's a manual, admin-triggered migration
# tool meant to be hit directly by URL (see docstring below), not wired to
# a button. Confirmed intentional, not dead code. Leave as-is.
@app.route("/api/admin/run-fe-migration")
@login_required
def api_run_fe_migration():
    """
    Admin utility: manually trigger the Fe column migration and return
    a detailed JSON result so you can see exactly what happened / what failed.
    Hit this in your browser: /api/admin/run-fe-migration
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    result = {"dept_added": [], "txn_added": [], "errors": []}
    try:
        result["dept_added"] = _ensure_fe_columns_sys_department()
    except Exception as e:
        result["errors"].append(f"Sys_Department: {e}")
    try:
        result["txn_added"] = _ensure_fe_columns_adco_transactions()
    except Exception as e:
        result["errors"].append(f"Adco_Transactions: {e}")
    result["success"] = len(result["errors"]) == 0
    return jsonify(result)


# ── API: Delete main folder (Sys_Department) ──────────────────────────────
@app.route("/api/entities/<int:entity_id>", methods=["DELETE"])
@login_required
def api_delete_entity(entity_id):
    conn = None
    try:
        if get_current_role() != "admin":
            return jsonify({"error": "Forbidden: only admin can delete main folders"}), 403

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT ID FROM dbo.Sys_Department WHERE ID = ? AND (Isdel = 0 OR Isdel IS NULL)",
            entity_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Department not found"}), 404

        # Soft-delete this Sys_Department row
        cursor.execute(
            "UPDATE dbo.Sys_Department SET Isdel=1 WHERE ID=?",
            entity_id
        )

        # Soft-delete all Adco_Folder rows under the same Dept_ID
        # (walk the tree via recursive CTE for nested subfolders)
        cursor.execute("""
                       WITH FolderTree AS (SELECT ID
                                           FROM dbo.Adco_Folder
                                           WHERE ID = ?
                                             AND IsDeleted = 0
                                           UNION ALL
                                           SELECT c.ID
                                           FROM dbo.Adco_Folder c
                                                    INNER JOIN FolderTree p ON c.Parent_ID = p.ID
                                           WHERE c.IsDeleted = 0)
                       UPDATE f
                       SET f.IsDeleted=1,
                           f.Enable=0 FROM dbo.Adco_Folder f INNER JOIN FolderTree t
                       ON t.ID = f.ID
                       """, entity_id)
        folders_affected = cursor.rowcount

        conn.commit()
        return jsonify({"success": True, "deleted_subfolder_count": folders_affected})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Load subfolders by Dept_ID ───────────────────────────────────────
# Called with the Dept_ID value (e.g. 46, 53) — NOT the Sys_Department.ID
# The frontend sends entity.dept_id which IS the Dept_ID column value
@app.route("/api/folders/<int:dept_id>")
@login_required
def api_folders(dept_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ncol = adco_folder_name_col(cursor)
        dcol = adco_folder_dept_col(cursor)
        dsql = adco_folder_desc_sql(cursor=cursor)
        dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"

        # Access control: non-admin users may only see folders in their allowed depts
        allowed = get_allowed_dep_ids()
        if allowed is not None and dept_id not in allowed:
            return jsonify([])  # dept not in user's access list

        cursor.execute(f"""
            SELECT ID, [{ncol}], Parent_ID, {dsql}
            FROM dbo.Adco_Folder
            WHERE {dept_bracket} = ?
              AND IsDeleted = 0
              AND [{ncol}] IS NOT NULL
              AND [{ncol}] != ''
            ORDER BY Parent_ID, [{ncol}]
        """, dept_id)
        rows = cursor.fetchall()
        return jsonify([{
            "id": r[0],
            "name": r[1],
            "parent_id": r[2] if r[2] else 0,
            "desc": r[3] or ""
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Create subfolder (Adco_Folder) ───────────────────────────────────
@app.route("/api/folders", methods=["POST"])
@login_required
def api_create_folder():
    conn = None
    try:
        data = request.get_json() or {}
        folder_name = (data.get("name") or "").strip()
        dept_id = data.get("dept_id")  # This is the Dept_ID value (46, 53...)
        parent_id = data.get("parent_id") or 0
        if not folder_name or not dept_id:
            return jsonify({"error": "Missing name or dept_id"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        ncol = adco_folder_name_col(cursor)
        dcol = adco_folder_dept_col(cursor)

        # Task 1: Prevent duplicate names within the same parent+dept scope
        cursor.execute(f"""
            SELECT COUNT(*) FROM dbo.Adco_Folder
            WHERE [{ncol}] = ? AND [{dcol}] = ? AND Parent_ID = ? AND IsDeleted = 0
        """, folder_name, int(dept_id), int(parent_id))
        if cursor.fetchone()[0] > 0:
            return jsonify({"error": f"A folder named '{folder_name}' already exists here"}), 409

        cursor.execute(f"""
            INSERT INTO dbo.Adco_Folder
                ([{ncol}], Parent_ID, Enable, CreatedBy, CreatedOn, IsDeleted, [{dcol}], ParentType)
            VALUES (?, ?, 1, ?, GETDATE(), 0, ?, 'C')
        """, folder_name, int(parent_id), session["user_id"], int(dept_id))

        cursor.execute("""
                       SELECT MAX(ID)
                       FROM dbo.Adco_Folder
                       WHERE CreatedBy = ?
                         AND IsDeleted = 0
                         AND CreatedOn >= DATEADD(SECOND, -3, GETDATE())
                       """, session["user_id"])
        row = cursor.fetchone()
        new_id = row[0] if row and row[0] else None
        if not new_id:
            raise RuntimeError("Could not retrieve new folder ID after INSERT")
        # Auto-grant the creator access to the parent department
        # dept_id here is the Adco_Folder.Dept_ID value which equals Sys_Department.ID
        _auto_grant_dept_access(cursor, session["user_id"], dept_id)

        conn.commit()
        audit_log("FOLDER_CREATE", page_id=3, notes=f"Created folder '{folder_name}' (ID {new_id}, dept_id={dept_id})")
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Delete subfolder (Adco_Folder) ───────────────────────────────────
@app.route("/api/folders/<int:folder_id>", methods=["DELETE"])
@login_required
def api_delete_folder(folder_id):
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only admin can delete folders"}), 403
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       WITH FolderTree AS (SELECT ID
                                           FROM dbo.Adco_Folder
                                           WHERE ID = ?
                                             AND IsDeleted = 0
                                           UNION ALL
                                           SELECT c.ID
                                           FROM dbo.Adco_Folder c
                                                    INNER JOIN FolderTree p ON c.Parent_ID = p.ID
                                           WHERE c.IsDeleted = 0)
                       UPDATE f
                       SET f.IsDeleted=1,
                           f.Enable=0 FROM dbo.Adco_Folder f INNER JOIN FolderTree t
                       ON t.ID=f.ID
                       """, folder_id)
        affected = cursor.rowcount
        conn.commit()
        if affected <= 0:
            return jsonify({"error": "Folder not found"}), 404
        audit_log("FOLDER_DELETE", page_id=3, notes=f"Deleted folder ID {folder_id} ({affected} node(s) soft-deleted)")
        return jsonify({"success": True, "deleted_count": affected})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── Gregorian → Hijri conversion (pure Python, no external libs) ──────────
def gregorian_to_hijri(year: int, month: int, day: int) -> tuple[int, int, int]:
    """
    Converts a Gregorian date to a Hijri (Islamic) date.
    Algorithm: Julian Day Number method.
    Returns (hijri_year, hijri_month, hijri_day).
    """
    if month < 1 or month > 12 or day < 1 or day > 31:
        return (0, 0, 0)
    # Julian Day Number for the Gregorian date
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    # Convert JDN to Hijri
    l = jdn - 1948440 + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = ((10985 - l) // 5316) * ((50 * l) // 17719) + (l // 5670) * ((43 * l) // 15238)
    l = l - ((10985 - l) // 5316) * ((179 * j) // 2 - 2) - (l // 5670) * (
                (199 * j) // 2 - 99)  # noqa: E501 (long line intentional for readability)
    d = l % 30 + 1
    j2 = l // 30 + 1
    hy = 30 * n + j - 30
    return (hy, j2, d)


def _h_date_normalized_sql(alias: str = "t") -> str:
    """H_Date as YYYY/MM/DD text (works on SQL Server 2008+ without TRY_CONVERT)."""
    h = f"LTRIM(RTRIM(CAST({alias}.H_Date AS NVARCHAR(50))))"
    return f"REPLACE(REPLACE({h}, '-', '/'), '.', '/')"


def _append_reg_date_from(where: list, params: list, alias: str, reg_date_from: str) -> None:
    """Filter rows on H_Date string or CreatedOn when H_Date is empty."""
    norm = _h_date_normalized_sql(alias)
    where.append(
        f"(({norm} IS NOT NULL AND {norm} <> '' AND LEFT({norm}, 10) >= ?) "
        f"OR (({norm} IS NULL OR {norm} = '') AND CAST({alias}.CreatedOn AS date) >= CONVERT(date, ?, 111)))"
    )
    params.extend([reg_date_from, reg_date_from])


def _append_reg_date_to(where: list, params: list, alias: str, reg_date_to: str) -> None:
    norm = _h_date_normalized_sql(alias)
    where.append(
        f"(({norm} IS NOT NULL AND {norm} <> '' AND LEFT({norm}, 10) <= ?) "
        f"OR (({norm} IS NULL OR {norm} = '') AND CAST({alias}.CreatedOn AS date) <= CONVERT(date, ?, 111)))"
    )
    params.extend([reg_date_to, reg_date_to])


def _append_browse_date_window(where: list, params: list, alias: str,
                               reg_date_from: str, reg_date_to: str) -> None:
    """Default inquiries list: match rows either registered (CreatedOn) OR
    last edited (ModifiedOn) within the window, so a document whose date was
    changed via edit still surfaces under the date it was edited, not only
    under its original registration date."""
    where.append(
        f"("
        f"(CAST({alias}.CreatedOn AS date) >= CONVERT(date, ?, 111) AND CAST({alias}.CreatedOn AS date) <= CONVERT(date, ?, 111))"
        f" OR "
        f"({alias}.ModifiedOn IS NOT NULL AND CAST({alias}.ModifiedOn AS date) >= CONVERT(date, ?, 111) AND CAST({alias}.ModifiedOn AS date) <= CONVERT(date, ?, 111))"
        f")"
    )
    params.extend([reg_date_from, reg_date_to, reg_date_from, reg_date_to])


def _form_date_normalized_sql(alias: str = "t") -> str:
    """Form_Date as YYYY/MM/DD text. Mirrors _h_date_normalized_sql — string
    comparison instead of CAST/CONVERT to date, because Form_Date can hold
    values (empty strings, stray whitespace, legacy formats) that make
    CAST(... AS date) throw a SQL Server conversion error rather than just
    failing the comparison."""
    f = f"LTRIM(RTRIM(CAST({alias}.Form_Date AS NVARCHAR(50))))"
    return f"REPLACE(REPLACE({f}, '-', '/'), '.', '/')"


def _append_form_date_window(where: list, params: list, alias: str,
                              date_from: str, date_to: str) -> None:
    """Filter the inquiries/browse list by the document's own date
    (Form_Date) instead of when it was registered/edited. Documents with no
    usable Form_Date fall back to CreatedOn so they aren't silently excluded."""
    norm = _form_date_normalized_sql(alias)
    where.append(
        f"("
        f"({norm} IS NOT NULL AND {norm} <> '' AND LEFT({norm}, 10) >= ? AND LEFT({norm}, 10) <= ?)"
        f" OR "
        f"(({norm} IS NULL OR {norm} = '') AND CAST({alias}.CreatedOn AS date) >= CONVERT(date, ?, 111) AND CAST({alias}.CreatedOn AS date) <= CONVERT(date, ?, 111))"
        f")"
    )
    params.extend([date_from, date_to, date_from, date_to])


def _normalize_filter_date(value: str) -> str:
    """YYYY-MM-DD or YYYY/MM/DD → YYYY/MM/DD for date comparisons."""
    return (value or "").strip().replace("-", "/")


REG_NUMBER_PREFIX = os.environ.get("REG_NUMBER_PREFIX", "DOC")


def _normalize_reg_number(raw: str) -> str:
    """Strip the configured registration-number prefix (e.g. 'DOC-') and any
    non-digits, so 'DOC-88070' and '88070' both normalize to the same value."""
    s = (raw or "").strip()
    prefix = REG_NUMBER_PREFIX.upper()
    if s.upper().startswith(prefix + "-"):
        s = s[len(prefix) + 1:].strip()
    elif s.upper().startswith(prefix):
        s = re.sub(rf"^{re.escape(prefix)}[-\s]*", "", s, flags=re.I).strip()
    digits = re.sub(r"\D", "", s)
    return digits if digits else s


def parse_hijri_date_string(gregorian_str: str) -> str:
    """
    Given a Gregorian date string (YYYY/MM/DD or YYYY-MM-DD),
    returns the Hijri date as 'HY/HM/HD' string.
    Falls back to empty string on parse error.
    """
    try:
        clean = gregorian_str.replace("/", "-")
        parts = clean.split("-")
        if len(parts) != 3:
            return ""
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        hy, hm, hd = gregorian_to_hijri(y, m, d)
        if hy == 0:
            return ""
        return f"{hy}/{str(hm).zfill(2)}/{str(hd).zfill(2)}"
    except Exception:
        return ""


# ── Task 5: Delivery_Method_ID / Transaction_ID sync helper ───────────────
def _ensure_delivery_method_sync(cursor, tx_id: int) -> None:
    """
    Sets Delivery_Method_ID = 1 on every transaction (fixed business rule).
    Validates the write succeeded — raises ValueError if not.
    """
    # Enforce the value
    # Delivery_Method_ID is always 1 — fixed business rule.
    cursor.execute(
        "UPDATE dbo.Adco_Transactions SET Delivery_Method_ID = 1 WHERE ID = ?",
        tx_id,
    )

    # Validate — read back and confirm
    cursor.execute(
        "SELECT Delivery_Method_ID FROM dbo.Adco_Transactions WHERE ID = ?",
        tx_id,
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError(f"Transaction {tx_id} not found during delivery-method sync check")
    actual = row[0]
    if actual != 1:
        raise ValueError(
            f"Delivery_Method_ID mismatch on transaction {tx_id}: "
            f"expected 1, found {actual}. Rolling back."
        )


# ── API: OCR a file before saving (scan/upload modal) ──────────────────────
@app.route("/api/ocr", methods=["POST"])
@login_required
def api_ocr_extract():
    """
    Accepts an uploaded file (multipart/form-data, field name 'file'),
    extracts its text — directly for digital PDFs (pypdf), or via
    Tesseract OCR for images / scanned PDFs (pdf2image + pytesseract) —
    and returns the extracted text as JSON. Does not persist anything.
    """
    if not OCR_ENABLED:
        return jsonify({"error": "OCR is temporarily disabled", "reason": "ocr_disabled"}), 503

    uf = request.files.get("file")
    if not uf or not uf.filename:
        return jsonify({"error": "No file provided"}), 400

    _, ext = os.path.splitext(uf.filename)
    file_ext = ext.lstrip(".").lower()
    if file_ext not in _OCR_IMAGE_EXTS and file_ext not in _OCR_PDF_EXTS:
        return jsonify({"error": f"Unsupported file type for OCR: .{file_ext}"}), 400

    # Images always need an OCR engine (EasyOCR or Tesseract); PDFs only need
    # one if there's no embedded text.
    ocr_available = USE_EASYOCR or (pytesseract is not None and Image is not None)
    if file_ext in _OCR_IMAGE_EXTS and not ocr_available:
        return jsonify({"error": "OCR is not available on this server (no OCR engine installed)"}), 503

    tmp_path = None
    try:
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        tmp_name = f"_ocr_{session.get('user_id', 'u')}_{ts}{ext or '.bin'}"
        tmp_path = os.path.join(UPLOAD_DIR, tmp_name)
        uf.save(tmp_path)
        saved_size = os.path.getsize(tmp_path)
        print(f"[OCR DEBUG] saved temp file: {tmp_path} ({saved_size} bytes)")
        print(f"[OCR DEBUG] OCR_POPPLER_PATH={OCR_POPPLER_PATH!r}")
        print(f"[OCR DEBUG] OCR_TESSERACT_CMD={OCR_TESSERACT_CMD!r}")

        text, reason = extract_ocr_text_debug(tmp_path, file_ext)
        print(f"[OCR DEBUG] reason={reason!r} text_len={len(text)}")
        audit_log("OCR_EXTRACT", page_id=None,
                  notes=f"OCR on '{uf.filename}' — reason={reason}, chars={len(text)}")
        return jsonify({
            "success": True,
            "text": text,
            "length": len(text),
            "reason": reason,
            "debug_saved_size": saved_size,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass



# ── API: Save document (Adco_Transactions + attachment) ───────────────────
@app.route("/api/documents", methods=["POST"])
@login_required
def api_save_document():
    if not _check_accr(2, "Can_Add"):
        return jsonify({"error": "Access denied: you do not have permission to add documents."}), 403
    conn = None
    try:
        is_multipart = request.content_type and "multipart/form-data" in request.content_type
        data = request.form if is_multipart else (request.get_json() or {})

        subject = (data.get("subject") or "").strip()
        folder_id = data.get("folder_id")
        if not subject or not folder_id:
            return jsonify({"error": "Missing required fields: subject and folder_id"}), 400

        type_id = 3  # always 3 — ignored if sent by frontend
        cat_id = int(data.get("cat_id") or 1)
        importance_id = int(data.get("importance_id") or 1)
        secret_id = int(data.get("secret_id") or 1)

        # backend always ensures a valid date; frontend pre-fills today
        today_str = datetime.now().strftime("%Y/%m/%d")
        h_date = (data.get("date") or "").strip() or today_str

        #  compute Hijri from the Gregorian reg date
        g_date = parse_hijri_date_string(h_date)

        keywords = (data.get("keywords") or "").strip()
        notes = (data.get("file_description") or "").strip()  # Statement → Detailes

        # doc_number → Form_No; form_date is the document date field
        form_no = (data.get("doc_number") or "").strip() or None
        form_date = (data.get("form_date") or "").strip() or None  # separate doc date

        _raw_folder_dept = (data.get("folder_dept_id") or "").strip()
        to_dept = int(_raw_folder_dept) if _raw_folder_dept and _raw_folder_dept != "0" else None
        from_dept = session.get("dep_id") or None

        # is_need_reply defaults to 0 always
        is_need_reply = 0

        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        if to_dept is None:
            dcol = adco_folder_dept_col(cursor)
            dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
            try:
                cursor.execute(
                    f"SELECT {dept_bracket} FROM dbo.Adco_Folder WHERE ID = ? AND IsDeleted = 0",
                    int(folder_id),
                )
                _dept_row = cursor.fetchone()
                if _dept_row and _dept_row[0]:
                    to_dept = int(_dept_row[0])
            except Exception:
                pass  # graceful degradation
        # Final fallback to the user's own department
        if to_dept is None:
            to_dept = from_dept

        # Code is globally sequential across ALL folders (matches real data: 1, 2, 3, 4 globally)
        try:
            cursor.execute(
                "SELECT ISNULL(MAX(Code), 0) + 1 FROM dbo.Adco_Transactions WHERE IsDeleted = 0"
            )
            next_code = cursor.fetchone()[0] or 1
        except Exception:
            next_code = 1

        # Ensure Fe columns exist before inserting
        _ensure_fe_columns_adco_transactions()

        # Read Fe1–Fe7 values sent by the frontend
        fe_vals = [data.get(f"Fe{i}") or None for i in range(1, 8)]

        # pyodbc executes one statement at a time; triggers may break OUTPUT/SCOPE_IDENTITY().
        cursor.execute("""
                       INSERT INTO dbo.Adco_Transactions
                       (Code, Type_ID, Cat_ID, H_Date, G_Date,
                        Importance_Degree_ID, Secret_Degree_ID,
                        Transaction_Type_ID,
                        Subject, Keywords, Detailes,
                        Foldes_ID, From_Dep_ID, To_Dep_ID,
                        CreatedBy, CreatedOn, IsDeleted, Status_ID,
                        Form_No, Form_Date, Is_Need_Reply,
                        Fe1, Fe2, Fe3, Fe4, Fe5, Fe6, Fe7)
                       VALUES (?, ?, ?, ?, ?,
                               ?, ?,
                               ?,
                               ?, ?, ?,
                               ?, ?, ?,
                               ?, GETDATE(), 0, 1,
                               ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?)
                       """,
                       next_code, type_id, cat_id, h_date, g_date,
                       importance_id, secret_id,
                       1,
                       subject, keywords, notes,
                       int(folder_id), from_dept, to_dept,
                       session["user_id"],
                       form_no, form_date, is_need_reply,
                       *fe_vals)

        new_id = None
        try:
            cursor.execute("""
                           SELECT ID
                           FROM dbo.Adco_Transactions WITH (UPDLOCK, HOLDLOCK)
                           WHERE Code = ? AND CreatedBy = ? AND IsDeleted = 0
                           """, next_code, session["user_id"])
            row = cursor.fetchone()
            new_id = row[0] if row else None
        except Exception:
            pass
        if not new_id:
            cursor.execute("""
                           SELECT MAX(ID)
                           FROM dbo.Adco_Transactions WITH (UPDLOCK, HOLDLOCK)
                           WHERE CreatedBy = ?
                             AND IsDeleted = 0
                             AND CreatedOn >= DATEADD(SECOND
                               , -5
                               , GETDATE())
                           """, session["user_id"])
            row = cursor.fetchone()
            new_id = row[0] if row and row[0] else None
        if not new_id:
            raise RuntimeError("Could not retrieve new transaction ID after INSERT")

        # Task 5: Delivery_Method_ID is always 1 — helper sets and validates.
        _ensure_delivery_method_sync(cursor, new_id)

        # Collect files — support multi-file key "files" and legacy single "file"
        uploaded_files = []
        if is_multipart:
            uploaded_files = [f for f in request.files.getlist("files") if f and f.filename]
            if not uploaded_files:
                single = request.files.get("file")
                if single and single.filename:
                    uploaded_files = [single]

        # OCR is opt-in: only run/store OCR text for files the user actually
        # clicked "Extract Text" on in the upload modal (tracked client-side
        # and sent here as a JSON array of original filenames). Files no one
        # ran OCR on are saved as plain attachments with no OCR text stored.
        ocr_requested_files = _parse_ocr_requested_files(data)
        ocr_extracted_text = _parse_ocr_extracted_text(data)

        file_desc = notes
        attachment_count = 0
        save_dir = get_save_dir(folder_id, cursor)
        ocr_jobs: list[tuple[int, str, str]] = []

        for idx, uf in enumerate(uploaded_files):
            original_name = uf.filename or "file"
            base_name, ext = os.path.splitext(original_name)
            ext = ext or ".bin"
            file_ext = ext.lstrip(".").lower()
            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
            temp_name = f"_tmp_{new_id}_{ts}_{idx}{ext}"
            temp_path = os.path.join(FILE_SAVE_DIR, temp_name)
            uf.save(temp_path)
            file_size = os.path.getsize(temp_path)

            cursor.execute("""
                           INSERT INTO dbo.Adco_Transactions_Attachments
                           (Transaction_ID, File_Name, File_Description, File_URL,
                            File_Size, File_Type_ID, CreatedBy, CreatedOn, IsDeleted)
                           VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 0)
                           """, new_id, uf.filename, file_desc,
                           temp_path, file_size, file_ext, session["user_id"])

            cursor.execute("""
                           SELECT MAX(ID) FROM dbo.Adco_Transactions_Attachments
                           WHERE Transaction_ID = ? AND CreatedBy = ?
                             AND IsDeleted = 0
                             AND CreatedOn >= DATEADD(SECOND, -5, GETDATE())
                           """, new_id, session["user_id"])
            att_row = cursor.fetchone()
            att_id = att_row[0] if att_row and att_row[0] else ts

            # base_name comes from the client's original filename — sanitize
            # before using it to build a disk path (path traversal guard).
            # The real/original name is preserved separately in File_Name.
            safe_base_name = _safe_filename_stem(base_name)
            final_name = f"{safe_base_name}.{att_id}{ext}"
            final_path = os.path.join(FILE_SAVE_DIR, final_name)
            os.rename(temp_path, final_path)
            file_url = final_name

            cursor.execute("""
                           UPDATE dbo.Adco_Transactions_Attachments
                           SET File_URL = ? WHERE ID = ?
                           """, file_url, att_id)

            # Text extraction runs automatically for every image/PDF attachment
            # in the background AFTER commit (see _run_ocr_jobs_async), so it
            # never blocks this save request — direct embedded-text extraction
            # is tried first (fast, no OCR engine needed) and only falls back
            # to real OCR for scanned/image-only files. If the user already
            # ran "Extract Text" and previewed the result, reuse that instead
            # of re-running extraction a second time.
            if OCR_ENABLED and (file_ext in _OCR_IMAGE_EXTS or file_ext in _OCR_PDF_EXTS):
                preextracted = ocr_extracted_text.get(original_name) if original_name in ocr_requested_files else None
                if preextracted:
                    try:
                        # Cached locally on disk, never written to SQL Server.
                        ocr_cache_write(att_id, final_name, preextracted)
                    except Exception as exc:
                        print(f"OCR save warning: {exc}")
                else:
                    ocr_jobs.append((att_id, final_path, file_ext))

            attachment_count += 1

        conn.commit()
        _run_ocr_jobs_async(ocr_jobs)
        audit_log("ADD", page_id=2, notes=f"Added document ID {new_id} — {subject[:100]}")
        notify_dept_users(to_dept, "ADD", new_id, subject=subject)

        # Task 7: registration_number is now numeric only (no ZAS- prefix)
        return jsonify({
            "success": True,
            "id": new_id,
            "registration_number": str(new_id),
            "hijri_date": g_date,
            "attachment_saved": attachment_count > 0,
            "attachment_count": attachment_count
        })
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Advanced search (Task 2) ────────────────────────────────────────
def _fuzzy_search_suggestions(cursor, term: str, where_access: list, params_access: list, limit: int = 5) -> list:
    """
    'Did you mean...?' fallback for zero-result searches.
    Uses SQL Server's built-in SOUNDEX/DIFFERENCE (no new tables, no external
    fuzzy-matching library) to find Subject/Keywords values that sound close
    to the term the user typed. DIFFERENCE() returns 0-4 (4 = identical
    SOUNDEX code); we only surface reasonably close matches (>=2) and never
    suggest the exact term back to the user.
    """
    term = (term or "").strip()
    if not term or len(term) < 3:
        return []
    try:
        where_sql = " AND ".join(["(t.IsDeleted = 0 OR t.IsDeleted IS NULL)"] + where_access)
        cursor.execute(
            f"""
            SELECT DISTINCT TOP {int(limit)} candidate, best_diff FROM (
                SELECT t.Subject AS candidate, DIFFERENCE(t.Subject, ?) AS best_diff
                FROM dbo.Adco_Transactions t
                WHERE {where_sql} AND t.Subject IS NOT NULL AND t.Subject <> ''
                UNION ALL
                SELECT t.Keywords AS candidate, DIFFERENCE(t.Keywords, ?) AS best_diff
                FROM dbo.Adco_Transactions t
                WHERE {where_sql} AND t.Keywords IS NOT NULL AND t.Keywords <> ''
            ) AS candidates
            WHERE best_diff >= 2 AND LOWER(candidate) <> LOWER(?)
            ORDER BY best_diff DESC
            """,
            term, *params_access, term, *params_access, term
        )
        return [row[0] for row in cursor.fetchall() if row[0]]
    except Exception:
        # Fuzzy suggestions are a nice-to-have; never let this break the search itself.
        return []


@app.route("/api/documents/advanced-search")
@login_required
def api_advanced_search():
    """
    Supports filtering by:
      reg_number, doc_number, reg_date_from, reg_date_to,
      topic, keywords, notes, statement
    All filters are combined (AND logic). Fully parameterised.
    """
    conn = None
    try:
        reg_number = (request.args.get("reg_number") or "").strip()
        reg_number_exact = (request.args.get("reg_number_exact") or "").strip() == "1"
        doc_number = (request.args.get("doc_number") or "").strip()
        # Normalise date separators: HTML date inputs send YYYY-MM-DD but H_Date
        # is stored as YYYY/MM/DD.  Replace dashes so string comparisons work.
        reg_date_from = _normalize_filter_date(request.args.get("reg_date_from") or "")
        reg_date_to = _normalize_filter_date(request.args.get("reg_date_to") or "")
        topic = (request.args.get("topic") or "").strip()
        keywords = (request.args.get("keywords") or "").strip()
        notes_filter = (request.args.get("notes") or "").strip()
        statement = (request.args.get("statement") or "").strip()
        folder_id = (request.args.get("folder_id") or "").strip()
        dept_id   = (request.args.get("dept_id")   or "").strip()
        # Custom Fe1–Fe7 field filters
        fe_filters = {}
        for i in range(1, 8):
            v = (request.args.get(f"fe{i}") or "").strip()
            if v:
                fe_filters[i] = v
        page = max(1, int(request.args.get("page", 1)))
        # Inquiries may request many rows (current + previous year); cap for safety.
        page_size = min(5000, max(1, int(request.args.get("page_size", 500))))
        offset = (page - 1) * page_size
        # Keyset pagination: when the caller passes after_id, skip the
        # expensive OFFSET/FETCH (which gets slower every page, since SQL
        # Server has to scan+sort past every already-returned row) and seek
        # directly to "rows with ID < after_id" instead. This keeps every
        # page roughly the same cost no matter how deep we are.
        # Also skip re-running COUNT(*) on these follow-up pages — the total
        # doesn't change page to page, the caller already has it from page 1.
        after_id_raw = (request.args.get("after_id") or "").strip()
        after_id = int(after_id_raw) if after_id_raw.isdigit() else None

        conn = get_db_connection()
        cursor = conn.cursor()

        where = ["(t.IsDeleted = 0 OR t.IsDeleted IS NULL)"]
        params = []

        # Access control: restrict to folders within the user's allowed departments
        allowed = get_allowed_dep_ids()
        if allowed is not None:
            if not allowed:
                return jsonify({"total": 0, "page": 1, "pages": 0, "results": []})
            dep_placeholders = ",".join("?" * len(allowed))
            dcol = adco_folder_dept_col(cursor)
            dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
            where.append(
                f"t.Foldes_ID IN ("
                f"  SELECT ID FROM dbo.Adco_Folder"
                f"  WHERE {dept_bracket} IN ({dep_placeholders}) AND IsDeleted = 0"
                f")"
            )
            params.extend(allowed)

        # Same access restriction, kept separate for the fuzzy 'did you mean'
        # fallback below, since that query only wants the access clause (not
        # the text/date/folder filters that produced zero rows).
        fuzzy_access_where = list(where[1:])
        fuzzy_access_params = list(params)

        reg_number_norm = _normalize_reg_number(reg_number) if reg_number else ""
        search_all_years = request.args.get("skip_default_dates", "").strip() == "1"

        # Browse (no text filters): previous + current year on CreatedOn.
        # Active filters: no date limit — entire archive.
        if search_all_years:
            reg_date_from = ""
            reg_date_to = ""
        else:
            if not reg_date_from or not reg_date_to:
                today = datetime.now()
                y, py = today.year, today.year - 1
                reg_date_from = reg_date_from or f"{py}/01/01"
                reg_date_to = reg_date_to or today.strftime("%Y/%m/%d")

        if reg_number_norm:
            if reg_number_exact:
                if reg_number_norm.isdigit():
                    rid = int(reg_number_norm)
                    where.append(
                        "(t.ID = ? OR CAST(t.ID AS VARCHAR) = ? OR CAST(t.Code AS VARCHAR) = ?)"
                    )
                    params.extend([rid, reg_number_norm, reg_number_norm])
                else:
                    where.append("CAST(t.ID AS VARCHAR) = ?")
                    params.append(reg_number_norm)
            else:
                # Prefix match only — "92" matches 92162, not 95518 or doc# 6092
                where.append("CAST(t.ID AS VARCHAR(20)) LIKE ?")
                params.append(f"{reg_number_norm}%")

        if doc_number:
            where.append("CAST(ISNULL(t.Form_No, '') AS NVARCHAR(100)) LIKE ?")
            params.append(f"%{doc_number}%")

        if not search_all_years and reg_date_from and reg_date_to:
            _append_browse_date_window(where, params, "t", reg_date_from, reg_date_to)

        if topic:
            where.append(
                "(t.Subject LIKE ? OR t.Keywords LIKE ? OR t.Detailes LIKE ?)"
            )
            like = f"%{topic}%"
            params.extend([like, like, like])

        if keywords:
            where.append(
                "(t.Keywords LIKE ? OR t.Subject LIKE ?)"
            )
            like = f"%{keywords}%"
            params.extend([like, like])

        if notes_filter:
            # Statement / Notes — matches the metadata field entered when
            # archiving the document (t.Detailes), NOT the OCR document text.
            where.append("t.Detailes LIKE ?")
            params.append(f"%{notes_filter}%")

        if statement:
            # Document Content searches ONLY the OCR cache — never metadata fields.
            # If no OCR match exists for the query, return no results for this filter.
            ocr_match_att_ids = ocr_cache_search(statement)
            ocr_match_tx_ids = set()
            if ocr_match_att_ids:
                id_placeholders = ",".join("?" * len(ocr_match_att_ids))
                cursor.execute(
                    f"""SELECT DISTINCT Transaction_ID FROM dbo.Adco_Transactions_Attachments
                        WHERE ID IN ({id_placeholders})
                          AND {_attachments_active_sql()}""",
                    *ocr_match_att_ids
                )
                ocr_match_tx_ids = {row[0] for row in cursor.fetchall()}

            if ocr_match_tx_ids:
                tx_placeholders = ",".join("?" * len(ocr_match_tx_ids))
                where.append(f"t.ID IN ({tx_placeholders})")
                params.extend(ocr_match_tx_ids)
            else:
                # No OCR matches — guarantee zero results for this filter
                where.append("1=0")

        if folder_id:
            where.append("t.Foldes_ID = ?")
            params.append(int(folder_id))
        elif dept_id:
            dcol = adco_folder_dept_col(cursor)
            dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
            where.append(
                f"t.Foldes_ID IN (SELECT ID FROM dbo.Adco_Folder WHERE {dept_bracket} = ? AND IsDeleted = 0)"
            )
            params.append(int(dept_id))

        # Fe1–Fe7 custom field filters (Fe1–Fe3 exact/dropdown, Fe4–Fe7 text LIKE)
        fe_col_map = {1:"Fe1",2:"Fe2",3:"Fe3",4:"Fe4",5:"Fe5",6:"Fe6",7:"Fe7"}
        for fi, fv in fe_filters.items():
            col = fe_col_map.get(fi)
            if not col:
                continue
            if fi <= 3:
                where.append(f"ISNULL(t.{col}, '') = ?")
                params.append(fv)
            else:
                where.append(f"ISNULL(t.{col}, '') LIKE ?")
                params.append(f"%{fv}%")

        where_sql = " AND ".join(where)

        if after_id is not None:
            # Keyset page: no COUNT(*) (caller already has the total from
            # page 1), seek past after_id instead of OFFSET-ing.
            total = None
            keyset_where_sql = " AND ".join(where + ["t.ID < ?"])
            cursor.execute(f"""
                SELECT TOP (?) t.ID, t.Subject, t.H_Date, t.G_Date, t.Keywords, t.Detailes,
                       t.Importance_Degree_ID, t.Secret_Degree_ID, t.Status_ID,
                       t.Foldes_ID, t.Form_No,
                       t.Fe1, t.Fe2, t.Fe3, t.Fe4, t.Fe5, t.Fe6, t.Fe7,
                       t.Form_Date
                FROM dbo.Adco_Transactions t
                WHERE {keyset_where_sql}
                ORDER BY t.ID DESC
            """, page_size, *params, after_id)
        else:
            cursor.execute(
                f"SELECT COUNT(*) FROM dbo.Adco_Transactions t WHERE {where_sql}",
                *params
            )
            total = cursor.fetchone()[0]

            cursor.execute(f"""
                SELECT t.ID, t.Subject, t.H_Date, t.G_Date, t.Keywords, t.Detailes,
                       t.Importance_Degree_ID, t.Secret_Degree_ID, t.Status_ID,
                       t.Foldes_ID, t.Form_No,
                       t.Fe1, t.Fe2, t.Fe3, t.Fe4, t.Fe5, t.Fe6, t.Fe7,
                       t.Form_Date
                FROM dbo.Adco_Transactions t
                WHERE {where_sql}
                ORDER BY t.ID DESC
                OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
            """, *params, offset, page_size)

        rows = cursor.fetchall()
        tx_ids = [r[0] for r in rows]
        folder_ids = [r[9] for r in rows]
        attachments_by_tx = load_attachments_for_transactions(cursor, tx_ids)
        folder_names = load_folder_names(cursor, folder_ids)

        ocr_snippets = {}
        if statement and tx_ids:
            tx_id_set = set(tx_ids)
            matched_att_ids = ocr_cache_search(statement)
            if matched_att_ids:
                id_placeholders = ",".join("?" * len(matched_att_ids))
                cursor.execute(
                    f"""SELECT ID, Transaction_ID FROM dbo.Adco_Transactions_Attachments
                        WHERE ID IN ({id_placeholders})
                          AND {_attachments_active_sql()}""",
                    *matched_att_ids
                )
                for att_id, tid in cursor.fetchall():
                    if tid in tx_id_set and tid not in ocr_snippets:
                        text = ocr_cache_read(att_id)
                        if text:
                            ocr_snippets[tid] = _extract_snippet(text, statement)

        results = []
        for r in rows:
            tx_id = r[0]
            atts = attachments_by_tx.get(tx_id, [])
            first = atts[0] if atts else None
            fid = r[9]
            results.append({
                "id": tx_id,
                "registration_number": str(tx_id),
                "subject": r[1] or "",
                "date": str(r[2]) if r[2] else "",
                "hijri_date": r[3] or "",
                "keywords": r[4] or "",
                "notes": r[5] or "",
                "importance_id": r[6],
                "secret_id": r[7],
                "status_id": r[8],
                "folder_id": fid,
                "folder_name": folder_names.get(fid, "") if fid else "",
                "doc_number": r[10] or "",
                "attachments": atts,
                "file_name": first["file_name"] if first else "",
                "file_url": first["download_url"] if first else "",
                "ocr_snippet": ocr_snippets.get(tx_id, ""),
                "Fe1": r[11] or "", "Fe2": r[12] or "", "Fe3": r[13] or "",
                "Fe4": r[14] or "", "Fe5": r[15] or "", "Fe6": r[16] or "",
                "Fe7": r[17] or "",
                "form_date": str(r[18])[:10] if r[18] else "",
            })

        return jsonify({
            "total": total,
            "page": page,
            "pages": ((total + page_size - 1) // page_size) if total is not None else None,
            "next_after_id": (rows[-1][0] if (rows and len(rows) == page_size) else None),
            "results": results,
            "suggestions": (
                _fuzzy_search_suggestions(cursor, topic or keywords, fuzzy_access_where, fuzzy_access_params)
                if total == 0 and (topic or keywords)
                else []
            ),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Search documents ─────────────────────────────────────────────────
def run_document_search(cursor, q=None, folder_id=None, dept_id=None,
                         date_from=None, date_to=None, page=1, page_size=20):
    """Shared search logic used by /api/documents/search and the chatbot."""
    offset = (page - 1) * page_size

    where = ["t.IsDeleted = 0"]
    params = []

    access_where, access_params = _current_user_doc_access_clause(cursor, "t")
    where.append(access_where)
    params.extend(access_params)

    # Content match: also search the extracted/OCR text cache so results
    # include documents whose content matches even when the subject/keywords
    # don't. tx_to_content_att maps a transaction to the first attachment
    # whose text matched, so we can show a snippet in the result.
    tx_to_content_att = {}
    if q:
        ocr_match_att_ids = ocr_cache_search(q)
        if ocr_match_att_ids:
            id_placeholders = ",".join("?" * len(ocr_match_att_ids))
            cursor.execute(
                f"""SELECT ID, Transaction_ID FROM dbo.Adco_Transactions_Attachments
                    WHERE ID IN ({id_placeholders})
                      AND {_attachments_active_sql()}""",
                *ocr_match_att_ids
            )
            for att_id, tid in cursor.fetchall():
                if tid not in tx_to_content_att:
                    tx_to_content_att[tid] = att_id

        like = f"%{q}%"
        if tx_to_content_att:
            tx_placeholders = ",".join("?" * len(tx_to_content_att))
            where.append(
                f"(t.Subject LIKE ? OR t.Keywords LIKE ? OR CAST(t.ID AS VARCHAR) LIKE ? OR t.ID IN ({tx_placeholders}))"
            )
            params += [like, like, like] + list(tx_to_content_att.keys())
        else:
            where.append("(t.Subject LIKE ? OR t.Keywords LIKE ? OR CAST(t.ID AS VARCHAR) LIKE ?)")
            params += [like, like, like]

    if folder_id:
        where.append("t.Foldes_ID = ?")
        params.append(int(folder_id))

    if dept_id:
        dcol = adco_folder_dept_col(cursor)
        dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
        where.append(
            f"t.Foldes_ID IN (SELECT ID FROM dbo.Adco_Folder WHERE {dept_bracket}=? "
            f"AND (IsDeleted=0 OR IsDeleted IS NULL))"
        )
        params.append(int(dept_id))

    date_from = _normalize_filter_date(date_from or "")
    date_to = _normalize_filter_date(date_to or "")
    if date_from:
        _append_reg_date_from(where, params, "t", date_from)

    if date_to:
        _append_reg_date_to(where, params, "t", date_to)

    where_sql = " AND ".join(where)

    cursor.execute(f"SELECT COUNT(*) FROM dbo.Adco_Transactions t WHERE {where_sql}", *params)
    total = cursor.fetchone()[0]

    # Transactions only — attachments loaded separately via Transaction_ID (1:N).
    # Folder names loaded in a second query so a wrong name column cannot hide files.
    cursor.execute(f"""
        SELECT t.ID, t.Subject, t.H_Date, t.Keywords,
               t.Importance_Degree_ID, t.Secret_Degree_ID, t.Status_ID,
               t.Foldes_ID
        FROM dbo.Adco_Transactions t
        WHERE {where_sql}
        ORDER BY t.ID DESC
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
    """, *params, offset, page_size)

    rows = cursor.fetchall()
    tx_ids = [r[0] for r in rows]
    folder_ids = [r[7] for r in rows]
    attachments_by_tx = load_attachments_for_transactions(cursor, tx_ids)
    folder_names = load_folder_names(cursor, folder_ids)

    results = []
    for r in rows:
        tx_id = r[0]
        attachments = attachments_by_tx.get(tx_id, [])
        first = attachments[0] if attachments else None
        f_id = r[7]
        content_snippet = ""
        if q and tx_id in tx_to_content_att:
            text = ocr_cache_read(tx_to_content_att[tx_id])
            if text:
                content_snippet = _extract_snippet(text, q)
        results.append({
            "id": tx_id,
            "registration_number": str(tx_id),
            "subject": r[1] or "",
            "date": str(r[2]) if r[2] else "",
            "keywords": r[3] or "",
            "importance_id": r[4],
            "secret_id": r[5],
            "status_id": r[6],
            "folder_id": f_id,
            "folder_name": folder_names.get(f_id, "") if f_id else "",
            "attachments": attachments,
            "file_name": first["file_name"] if first else "",
            "file_url": first["download_url"] if first else "",
            "content_snippet": content_snippet,
        })

    return total, results


@app.route("/api/documents/search")
@login_required
def api_search_documents():
    conn = None
    try:
        q = (request.args.get("q") or "").strip()
        folder_id = request.args.get("folder_id")
        dept_id = request.args.get("dept_id")
        # Normalise date separators: HTML date inputs send YYYY-MM-DD but H_Date
        # is stored as YYYY/MM/DD.  Replace dashes so string comparisons work.
        date_from = (request.args.get("date_from") or "").strip().replace("-", "/") or None
        date_to = (request.args.get("date_to") or "").strip().replace("-", "/") or None
        page = max(1, int(request.args.get("page", 1)))
        page_size = min(50, max(1, int(request.args.get("page_size", 20))))

        conn = get_db_connection()
        cursor = conn.cursor()

        total, results = run_document_search(
            cursor, q=q, folder_id=folder_id, dept_id=dept_id,
            date_from=date_from, date_to=date_to, page=page, page_size=page_size
        )

        audit_log("SEARCH", page_id=1, notes=f"Search: q='{q}' total={total}")
        return jsonify({
            "total": total,
            "page": page,
            "pages": (total + page_size - 1) // page_size,
            "results": results
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── Basic rule-based chatbot ──────────────────────────────────────────────
# No external AI API, no cost. Understands a handful of simple intents:
# greetings, help, and "find/search <keyword>" document lookups. Anything
# it doesn't recognize gets a helpful fallback message instead of guessing.
_CHATBOT_GREETINGS = ("hi", "hello", "hey", "مرحبا", "السلام عليكم", "اهلا", "أهلا")
_CHATBOT_HELP_WORDS = ("help", "how", "guide", "مساعدة", "كيف")
_CHATBOT_SEARCH_TRIGGERS = ("find", "search", "look up", "لوكيت", "ابحث", "بحث", "دور على")
_CHATBOT_SHOW_MORE_TRIGGERS = (
    "show me more", "show more", "more results", "more",
    "المزيد", "أظهر المزيد", "اظهر المزيد", "زيادة",
)
_CHATBOT_SUMMARIZE_TRIGGERS = (
    "summarize document", "summarize doc", "summarize the document", "summarize",
    "لخص المستند", "لخص", "تلخيص المستند", "تلخيص",
)

# ── Workflow intents (inbox / sent / status / approve / reject / overdue / expiring) ──
_CHATBOT_WF_INBOX_TRIGGERS = (
    "pending my approval", "pending approval", "my inbox", "workflow inbox",
    "waiting on me", "waiting for me", "what's pending", "whats pending", "inbox",
    "بانتظار موافقتي", "صندوق الوارد", "بانتظاري", "قيد الموافقة",
)
_CHATBOT_WF_SENT_TRIGGERS = (
    "my sent", "sent items", "what did i send", "my submissions", "sent",
    "المرسلة", "ما أرسلته", "طلباتي المرسلة",
)
_CHATBOT_WF_STATUS_TRIGGERS = (
    "status of", "status for", "track document", "track workflow", "where is document",
    "حالة المستند", "تتبع المستند", "أين المستند",
)
_CHATBOT_WF_APPROVE_TRIGGERS = ("approve document", "approve workflow", "approve", "وافق على", "موافقة على")
_CHATBOT_WF_REJECT_TRIGGERS = ("reject document", "reject workflow", "reject", "ارفض", "رفض")
_CHATBOT_WF_OVERDUE_TRIGGERS = (
    "overdue", "stuck", "bottleneck", "late approvals", "who hasn't responded", "who hasnt responded",
    "متأخر", "متعثر", "لم يرد",
)
_CHATBOT_WF_EXPIRING_TRIGGERS = ("expiring", "expire", "expiry", "تنتهي", "منتهية الصلاحية", "الصلاحية")


def _wf_chatbot_extract_id(message: str):
    m = re.search(r"#?\s*(\d+)", message)
    return int(m.group(1)) if m else None


def _wf_chatbot_inbox(cursor, user_id, is_ar):
    cursor.execute(
        """
        SELECT wi.InstanceID, wis.StepName, u.USER_NAME, COALESCE(t.Subject, wi.Subject)
        FROM dbo.WF_Instance_Assignments wa
        JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
        JOIN dbo.WF_Instances wi ON wi.InstanceID = wis.InstanceID
            AND wi.SubmissionNumber = wis.SubmissionNumber AND wi.CurrentStepOrder = wis.StepOrder
        JOIN dbo.Sys_User u ON u.USER_ID = wi.SubmittedBy
        LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
        WHERE wa.AssignedTo = ? AND wa.Status = 'Waiting'
          AND wi.Status IN ('Pending', 'Pending Approval', 'Viewed', 'In Progress')
        ORDER BY wi.SubmittedOn ASC
        """,
        user_id,
    )
    rows = cursor.fetchall()
    if not rows:
        return {"reply": "لا توجد مستندات بانتظار موافقتك حاليًا. 🎉" if is_ar
                          else "You have nothing pending your approval right now. 🎉"}
    header = (f"لديك {len(rows)} عنصر بانتظار موافقتك:" if is_ar
              else f"You have {len(rows)} item(s) waiting on you:")
    lines = [header]
    for r in rows[:10]:
        lines.append(f"#{r[0]} — \"{r[3] or ('(بدون موضوع)' if is_ar else '(no subject)')}\" ({r[1]}, {r[2]})")
    lines.append("قل 'وافق على #<رقم>' أو 'ارفض #<رقم>' لاتخاذ إجراء." if is_ar
                 else "Say \"approve #<id>\" or \"reject #<id>\" to act on one.")
    return {"reply": "\n".join(lines)}


def _wf_chatbot_sent(cursor, user_id, is_ar):
    cursor.execute(
        """
        SELECT wi.InstanceID, COALESCE(t.Subject, wi.Subject), wi.Status
        FROM dbo.WF_Instances wi
        LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
        WHERE wi.SubmittedBy = ? AND wi.IsDeleted = 0
          AND (wi.HiddenFromSent = 0 OR wi.HiddenFromSent IS NULL)
        ORDER BY wi.SubmittedOn DESC
        """,
        user_id,
    )
    rows = cursor.fetchall()[:10]
    if not rows:
        return {"reply": "لم ترسل أي شيء عبر سير العمل بعد." if is_ar else "You haven't sent anything into a workflow yet."}
    lines = ["مرسلاتك الأخيرة:" if is_ar else "Your recent submissions:"]
    for r in rows:
        lines.append(f"#{r[0]} — \"{r[1] or ('(بدون موضوع)' if is_ar else '(no subject)')}\" — {r[2]}")
    return {"reply": "\n".join(lines)}


def _wf_chatbot_status(cursor, instance_id, is_ar):
    if instance_id is None:
        return {"reply": "أي مستند؟ مثال: 'حالة المستند #123'." if is_ar else "Which one? Try: \"status of #123\"."}
    cursor.execute(
        "SELECT wi.Status, COALESCE(t.Subject, wi.Subject) FROM dbo.WF_Instances wi "
        "LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID "
        "WHERE wi.InstanceID = ? AND wi.IsDeleted = 0",
        instance_id,
    )
    row = cursor.fetchone()
    if not row:
        return {"reply": f"لم أجد المستند #{instance_id}." if is_ar else f"I couldn't find instance #{instance_id}."}
    status, subject = row
    cursor.execute(
        """SELECT wh.ActionType, u.USER_NAME, wh.ActionOn FROM dbo.WF_History wh
           JOIN dbo.Sys_User u ON u.USER_ID = wh.ActionBy WHERE wh.InstanceID = ? ORDER BY wh.ActionOn ASC""",
        instance_id,
    )
    hist = cursor.fetchall()
    lines = [f"#{instance_id} — \"{subject or ''}\" — " + ("الحالة: " if is_ar else "status: ") + str(status)]
    for h in hist[-6:]:
        ts = h[2].strftime("%Y-%m-%d %H:%M") if hasattr(h[2], "strftime") else h[2]
        lines.append(f"  • {h[0]} — {h[1]} — {ts}")
    return {"reply": "\n".join(lines)}


def _wf_chatbot_overdue(cursor, user_id, is_admin, is_ar):
    cutoff = datetime.now() - timedelta(days=3)
    scope_sql = "" if is_admin else "AND wi.SubmittedBy = ?"
    params = [cutoff] if is_admin else [cutoff, user_id]
    cursor.execute(
        f"""
        SELECT wi.InstanceID, COALESCE(t.Subject, wi.Subject), u2.USER_NAME, wa.AssignedOn
        FROM dbo.WF_Instance_Assignments wa
        JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
        JOIN dbo.WF_Instances wi ON wi.InstanceID = wis.InstanceID AND wi.CurrentStepOrder = wis.StepOrder
        LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
        JOIN dbo.Sys_User u2 ON u2.USER_ID = wa.AssignedTo
        WHERE wa.Status = 'Waiting' AND wa.AssignedOn < ? {scope_sql}
        ORDER BY wa.AssignedOn ASC
        """,
        *params,
    )
    rows = cursor.fetchall()[:10]
    if not rows:
        return {"reply": "لا يوجد شيء متعثر — لا توجد عناصر تنتظر أكثر من 3 أيام." if is_ar
                          else "Nothing looks stuck — no items pending more than 3 days."}
    lines = ["عناصر تنتظر أكثر من 3 أيام:" if is_ar else "Items waiting more than 3 days:"]
    for r in rows:
        lines.append(f"#{r[0]} \"{r[1] or ''}\" — {r[2]} — {r[3]:%Y-%m-%d}")
    return {"reply": "\n".join(lines)}


def _wf_chatbot_expiring(cursor, user_id, is_admin, is_ar):
    cursor.execute(
        """
        SELECT InstanceID, COALESCE(Subject, ''), ExpiryDate FROM dbo.WF_Instances
        WHERE ExpiryDate IS NOT NULL AND ExpiryDate <= DATEADD(day, 30, GETDATE())
          AND ExpiryDate >= GETDATE() AND IsDeleted = 0 AND (SubmittedBy = ? OR ? = 1)
        ORDER BY ExpiryDate ASC
        """,
        user_id, 1 if is_admin else 0,
    )
    rows = cursor.fetchall()[:10]
    if not rows:
        return {"reply": "لا توجد مستندات تنتهي صلاحيتها خلال 30 يومًا." if is_ar
                          else "No documents expiring in the next 30 days."}
    lines = ["مستندات تنتهي صلاحيتها قريبًا:" if is_ar else "Documents expiring soon:"]
    for r in rows:
        lines.append(f"#{r[0]} \"{r[1] or ''}\" — {r[2]}")
    return {"reply": "\n".join(lines)}

# "open <subpage>" navigation intent — lets the chatbot switch the dashboard
# to a given section (same sections as the sidebar / showSection() in JS).
_CHATBOT_OPEN_TRIGGERS = ("open ", "go to ", "navigate to ", "افتح ", "اذهب الى ", "اذهب إلى ", "انتقل الى ", "انتقل إلى ")

_CHATBOT_SECTIONS = [
    {"section": "inquiries", "keywords_en": ("inquiries", "inquiry"), "keywords_ar": ("الاستعلامات", "استعلامات")},
    {"section": "archive", "keywords_en": ("archive",), "keywords_ar": ("الأرشفة", "الارشفة", "ارشفة")},
    {"section": "folders", "keywords_en": ("folders", "folder browser", "folder"), "keywords_ar": ("المجلدات", "متصفح المجلدات", "مجلدات")},
    {"section": "workflow", "keywords_en": ("workflow", "workflows", "approvals"), "keywords_ar": ("سير العمل", "الموافقات")},
    {"section": "messages", "keywords_en": ("messages", "chat", "messaging"), "keywords_ar": ("الرسائل", "المحادثات")},
    {"section": "reports", "keywords_en": ("reports", "report"), "keywords_ar": ("التقارير", "تقارير")},
    {"section": "control", "keywords_en": ("control panel", "control"), "keywords_ar": ("لوحة التحكم", "التحكم")},
    {"section": "settings", "keywords_en": ("settings",), "keywords_ar": ("الإعدادات", "الاعدادات")},
    {"section": "guide", "keywords_en": ("user guide", "guide"), "keywords_ar": ("دليل المستخدم", "الدليل")},
]

def _chatbot_match_section(text: str):
    """Return the section dict whose keywords appear in text, or None."""
    lowered = text.lower()
    for sec in _CHATBOT_SECTIONS:
        for kw in sec["keywords_en"] + sec["keywords_ar"]:
            if kw in lowered:
                return sec
    return None

def _chatbot_parse_date_phrase(text: str):
    """
    Detect a natural-language date phrase ('last week', 'yesterday',
    'الأسبوع الماضي', etc.) inside a chatbot search message. Returns
    (date_from, date_to, remaining_text) in the same YYYY/MM/DD format the
    advanced-search date filters already use, or (None, None, text) if no
    recognizable phrase is found. Matching is deliberately simple (calendar
    week/month, not fiscal) since this only narrows a chat search.
    """
    lowered = text.lower()
    today = datetime.now()

    def fmt(d):
        return d.strftime("%Y/%m/%d")

    phrases = [
        (("today", "اليوم"), lambda: (today, today)),
        (("yesterday", "أمس", "امس"), lambda: (today - timedelta(days=1), today - timedelta(days=1))),
        (("this week", "هذا الأسبوع", "هذا الاسبوع"),
         lambda: (today - timedelta(days=today.weekday()), today)),
        (("last week", "الأسبوع الماضي", "الاسبوع الماضي"),
         lambda: (today - timedelta(days=today.weekday() + 7),
                  today - timedelta(days=today.weekday() + 1))),
        (("this month", "هذا الشهر"), lambda: (today.replace(day=1), today)),
        (("last month", "الشهر الماضي"),
         lambda: ((today.replace(day=1) - timedelta(days=1)).replace(day=1),
                  today.replace(day=1) - timedelta(days=1))),
        (("this year", "هذا العام", "هذه السنة"), lambda: (today.replace(month=1, day=1), today)),
        (("last year", "العام الماضي", "السنة الماضية"),
         lambda: (today.replace(year=today.year - 1, month=1, day=1),
                  today.replace(year=today.year - 1, month=12, day=31))),
        (("last 7 days", "past 7 days", "آخر 7 أيام", "اخر 7 ايام"),
         lambda: (today - timedelta(days=7), today)),
        (("last 30 days", "past 30 days", "آخر 30 يوم", "اخر 30 يوم"),
         lambda: (today - timedelta(days=30), today)),
    ]

    for keywords, range_fn in phrases:
        for kw in keywords:
            if kw in lowered:
                d_from, d_to = range_fn()
                remaining = re.sub(re.escape(kw), "", text, flags=re.I).strip()
                remaining = re.sub(r"\s{2,}", " ", remaining).strip(" ,-")
                return fmt(d_from), fmt(d_to), remaining

    return None, None, text



def _chatbot_extract_search_term(message: str) -> str:
    """Strip a leading trigger word like 'find'/'search for' off the message."""
    text = message.strip()
    lowered = text.lower()
    for trigger in _CHATBOT_SEARCH_TRIGGERS:
        if lowered.startswith(trigger):
            text = text[len(trigger):].strip()
            break
    # Drop a few common filler words some people type after "find"/"search"
    for filler in ("for ", "عن "):
        if text.lower().startswith(filler):
            text = text[len(filler):].strip()
    return text


# ── Bulk actions: "email all documents in <folder>", "download all
# attachments in <folder>" / "...for document <id>" ─────────────────────
# Checked before the single-document search/FAQ handling below, since these
# phrases contain words ("documents", "attachments") that could otherwise be
# swallowed by other intents.
BULK_EMAIL_MAX_DOCS = 15   # keep a chat-triggered bulk send fast & bounded

_CHATBOT_BULK_EMAIL_TRIGGERS = (
    "email all documents", "send all documents", "email all",
    "ارسل كل المستندات", "أرسل كل المستندات", "ارسل جميع المستندات", "أرسل جميع المستندات",
)
_CHATBOT_BULK_ZIP_TRIGGERS = (
    "download all attachments", "download all documents", "zip all attachments", "zip all documents",
    "نزل كل المرفقات", "نزّل كل المرفقات", "تحميل كل المرفقات", "تنزيل كل المرفقات",
)
# "download all attachments for document 123" / "زيب مرفقات المستند 123"
_CHATBOT_SINGLE_DOC_ZIP_TRIGGERS = (
    "download all attachments for document", "download all attachments for", "zip document",
    "نزل كل مرفقات المستند", "نزّل كل مرفقات المستند", "مرفقات المستند رقم",
)

# "download document <reg#>" — a plain single-document download by
# registration number (distinct from the "download all attachments for
# document ..." trigger above, which is really about the same document but
# phrased as an explicit attachments request; both end up in the same place).
_CHATBOT_DOWNLOAD_DOC_TRIGGERS = (
    "download document", "download doc", "download reg", "download registration",
    "نزل مستند", "نزّل مستند", "تنزيل مستند", "تحميل مستند", "نزل المستند", "نزّل المستند", "تحميل المستند",
)

_FOLDER_LOC_WORDS_EN = (" in ", " from ", " inside ")
_FOLDER_LOC_WORDS_AR = (" في ", " من ")
_RECIPIENT_SPLIT_EN = (" to ",)
_RECIPIENT_SPLIT_AR = (" الى ", " إلى ",)

# "email document <reg#> [to <email>] [as attachment|as link]"
_CHATBOT_EMAIL_DOC_TRIGGERS = (
    "email document", "email doc", "email reg", "email registration", "send document",
    "ارسل مستند", "أرسل مستند", "ارسل المستند", "أرسل المستند", "ايميل مستند",
)

_CHATBOT_LIST_RECIPIENTS_TRIGGERS = (
    "who can i send to", "who can i email", "list users", "list recipients", "show users",
    "من يمكنني الإرسال له", "من يمكنني ارسال", "قائمة المستخدمين", "قائمة المستلمين",
)

# "delete document <reg#>" — always confirmed before anything is actually
# deleted (see chatbot_pending_delete handling below).
_CHATBOT_DELETE_DOC_TRIGGERS = (
    "delete document", "delete doc", "delete reg", "delete registration", "remove document",
    "حذف مستند", "احذف مستند", "امسح مستند", "حذف المستند", "احذف المستند", "امسح المستند",
)
_CHATBOT_CONFIRM_YES = ("yes", "y", "confirm", "نعم", "أكد", "اكد", "تأكيد")
_CHATBOT_CONFIRM_NO = ("no", "n", "cancel", "لا", "إلغاء", "الغاء")
_CHATBOT_CANCEL_WORDS = ("cancel", "never mind", "nevermind", "stop", "إلغاء", "الغاء", "انسَ الأمر", "انسى الامر")
_CHATBOT_REPORT_TRIGGERS = (
    "how many documents", "how many docs", "how many files", "documents archived", "docs archived",
    "كم عدد المستندات", "كم مستند", "عدد المستندات", "كم عدد الملفات",
)

# Mode keywords — checked as a substring anywhere in the message, since
# people put "as a link please" / "كرابط" wherever feels natural to them, or
# just reply with the bare word ("attachment" / "link") when we ask.
_CHATBOT_MODE_ATTACH_WORDS = ("as attachment", "as an attachment", "attach the file", "attached",
                              "attachment", "attach",
                              "كمرفق", "كملف مرفق", "مرفق")
_CHATBOT_MODE_LINK_WORDS = ("as a link", "as link", "share link", "just a link", "link only",
                            "link", "لينك",
                            "كرابط", "رابط فقط", "كرابط فقط", "رابط")


def _chatbot_detect_send_mode(text: str):
    """Return 'attach', 'link', or None if the message doesn't say which."""
    lowered = text.lower()
    if any(w in lowered for w in _CHATBOT_MODE_LINK_WORDS):
        return "link"
    if any(w in lowered for w in _CHATBOT_MODE_ATTACH_WORDS):
        return "attach"
    return None


def _chatbot_parse_email_doc(message: str, is_ar: bool):
    """
    Parse "email document <reg#> [to <email>] [as attachment|as link]".
    Returns (doc_id_or_None, recipient_or_None, mode_or_None).
    """
    text = _strip_leading_trigger(message, _CHATBOT_EMAIL_DOC_TRIGGERS)
    mode = _chatbot_detect_send_mode(message)

    # Strip the mode phrase out before looking for a recipient/reg number so
    # "as attachment" isn't mistaken for anything else.
    for w in _CHATBOT_MODE_ATTACH_WORDS + _CHATBOT_MODE_LINK_WORDS:
        idx = text.lower().find(w)
        if idx != -1:
            text = (text[:idx] + text[idx + len(w):]).strip()

    recipient = None
    split_words = _RECIPIENT_SPLIT_AR if is_ar else _RECIPIENT_SPLIT_EN
    lowered = text.lower()
    for sw in split_words:
        idx = lowered.rfind(sw)
        if idx != -1:
            recipient = text[idx + len(sw):].strip().strip(".").strip()
            text = text[:idx].strip()
            break

    m = re.search(r"(\d+)", text)
    doc_id = int(m.group(1)) if m else None

    return doc_id, recipient, mode


def _chatbot_resolve_recipient(cursor, text: str):
    """
    Resolve a chat-typed recipient into an email address. Accepts a literal
    email address as-is, or a person's name/username looked up against
    dbo.Sys_User — so "send to Ahmed" works, not just "send to a@b.com".

    Returns (email_or_None, candidates):
      - Exactly one match (or a literal email) -> (email, [])
      - Ambiguous (2+ name matches) or no match -> (None, candidates), where
        candidates is a list of {"full_name", "email"} dicts (possibly empty)
        so the caller can show the person their options.
    """
    text = (text or "").strip()
    if not text:
        return None, []
    if "@" in text:
        return text, []

    like = f"%{text}%"
    cursor.execute(
        """
        SELECT USER_FULLNAME, USER_NAME, USER_EMAIL
        FROM dbo.Sys_User
        WHERE IsDeleted = 0 AND USER_EMAIL IS NOT NULL AND USER_EMAIL != ''
          AND (USER_FULLNAME LIKE ? OR USER_NAME LIKE ?)
        ORDER BY USER_FULLNAME
        """,
        like, like,
    )
    candidates = [{"full_name": r[0] or r[1] or "", "email": r[2]} for r in cursor.fetchall()]
    if len(candidates) == 1:
        return candidates[0]["email"], []
    return None, candidates[:8]


def _chatbot_delete_document(cursor, conn, doc_id: int, is_ar: bool):
    """
    Perform the same soft-delete as DELETE /api/documents/<id>, but for a
    chatbot-confirmed request. Returns a dict ready to jsonify.
    """
    cursor.execute(
        "SELECT Subject, To_Dep_ID FROM dbo.Adco_Transactions WHERE ID = ? AND IsDeleted = 0", doc_id
    )
    row = cursor.fetchone()
    if not row:
        return {"reply": f"لم أجد المستند #{doc_id}، أو ربما تم حذفه بالفعل." if is_ar
                          else f"I couldn't find document #{doc_id} — it may already be deleted."}
    subject, dept_id = row[0], row[1]

    cursor.execute("DELETE FROM dbo.Adco_Transactions_Attachments WHERE Transaction_ID = ?", doc_id)
    cursor.execute("UPDATE dbo.Adco_Transactions SET IsDeleted = 1 WHERE ID = ?", doc_id)
    if cursor.rowcount == 0:
        conn.rollback()
        return {"reply": "تعذّر حذف المستند. حاول مرة أخرى." if is_ar
                          else "Couldn't delete the document. Please try again."}

    conn.commit()
    audit_log("DELETE", page_id=1, notes=f"Deleted document ID {doc_id} (via chatbot)")
    notify_dept_users(dept_id, "DELETE", doc_id, subject=subject)

    return {
        "reply": f"تم. تم حذف المستند #{doc_id}." if is_ar else f"Done. Document #{doc_id} has been deleted.",
        "action": "document_deleted",
        "id": doc_id,
    }


def _chatbot_list_recipients(cursor, limit=25):
    """Active users with a saved email, for the 'who can I send to' intent."""
    cursor.execute(
        """
        SELECT TOP (?) USER_FULLNAME, USER_NAME, USER_EMAIL
        FROM dbo.Sys_User
        WHERE IsDeleted = 0 AND USER_EMAIL IS NOT NULL AND USER_EMAIL != ''
        ORDER BY USER_FULLNAME
        """,
        limit,
    )
    return [{"full_name": r[0] or r[1] or "", "email": r[2]} for r in cursor.fetchall()]


def _chatbot_send_single_document_email(cursor, doc_id, recipient, mode, is_ar):
    """Send one document by chatbot request, either attached or as a link."""
    atts = load_attachments_for_transactions(cursor, [doc_id]).get(doc_id, [])
    att_ids = [a["id"] for a in atts]
    attach_file = (mode == "attach")
    result, status = _send_document_email_core(
        doc_id, [recipient], attachment_ids=att_ids, attach_file=attach_file,
    )
    if status == 200 and result.get("success"):
        return {"reply": f"تم إرسال المستند #{doc_id} إلى {recipient} {'كمرفق' if attach_file else 'كرابط'}."
                          if is_ar else
                          f"Sent document #{doc_id} to {recipient} {'as an attachment' if attach_file else 'as a link'}."}
    err = result.get("error", "unknown error")
    return {"reply": f"فشل إرسال المستند #{doc_id}: {err}" if is_ar
                      else f"Failed to send document #{doc_id}: {err}"}


def _strip_leading_trigger(text: str, triggers) -> str:
    lowered = text.lower()
    for t in sorted(triggers, key=len, reverse=True):
        if lowered.startswith(t):
            return text[len(t):].strip()
    return text.strip()


def _chatbot_parse_bulk_email(message: str, is_ar: bool):
    """
    Parse "email all documents in <folder> [to <email>]" (or Arabic
    equivalent). Returns (folder_text, recipient_or_None).
    """
    text = _strip_leading_trigger(message, _CHATBOT_BULK_EMAIL_TRIGGERS)

    recipient = None
    split_words = _RECIPIENT_SPLIT_AR if is_ar else _RECIPIENT_SPLIT_EN
    lowered = text.lower()
    for sw in split_words:
        idx = lowered.rfind(sw)
        if idx != -1:
            recipient = text[idx + len(sw):].strip().strip(".").strip()
            text = text[:idx].strip()
            break

    loc_words = _FOLDER_LOC_WORDS_AR if is_ar else _FOLDER_LOC_WORDS_EN
    lowered = text.lower()
    for lw in loc_words:
        if lowered.startswith(lw.strip() + " ") or lowered.startswith(lw):
            text = text[len(lw):].strip()
            break

    return text.strip(), recipient


def _chatbot_parse_bulk_zip_folder(message: str, is_ar: bool, triggers) -> str:
    """Parse "download all attachments in <folder>" -> folder name text."""
    text = _strip_leading_trigger(message, triggers)
    loc_words = _FOLDER_LOC_WORDS_AR if is_ar else _FOLDER_LOC_WORDS_EN
    lowered = text.lower()
    for lw in loc_words:
        if lowered.startswith(lw.strip() + " ") or lowered.startswith(lw):
            text = text[len(lw):].strip()
            break
    return text.strip()


_CHATBOT_FAQ_TOPICS = [
    {
        "keywords_en": ("scan",),
        "keywords_ar": ("مسح", "سكان", "ماسح"),
        "en": "To scan a document: click the camera/scan icon next to a folder, then in the Scan window "
              "pick a tab — Upload a file, use your Camera, or connect a Network Scanner. Add your pages, "
              "fill in the subject/keywords, then click 'Save to archive'.",
        "ar": "لمسح مستند: اضغط على أيقونة الكاميرا/المسح بجانب المجلد، ثم اختر أحد التبويبات في نافذة المسح — "
              "رفع ملف، استخدام الكاميرا، أو الاتصال بماسح شبكي. أضف الصفحات، واملأ الموضوع والكلمات المفتاحية، "
              "ثم اضغط 'حفظ في الأرشيف'.",
    },
    {
        "keywords_en": ("email", "send document"),
        "keywords_ar": ("بريد", "ايميل", "إيميل", "ارسال"),
        "en": "To email a document: open it, click 'Email', pick recipients (search existing users or type "
              "any address), choose which attachments to include, then send.",
        "ar": "لإرسال مستند بالبريد: افتحه، اضغط 'بريد'، اختر المستلمين (ابحث عن مستخدم موجود أو اكتب أي بريد "
              "إلكتروني)، حدد المرفقات المطلوبة، ثم أرسل.",
    },
    {
        "keywords_en": ("qr",),
        "keywords_ar": ("كيو ار", "رمز"),
        "en": "To generate a QR code: open the document, then click 'QR Code'. You can copy the link or "
              "download the QR image — scanning it opens the document directly.",
        "ar": "لإنشاء رمز QR: افتح المستند، ثم اضغط 'رمز QR'. يمكنك نسخ الرابط أو تنزيل صورة الرمز — "
              "مسحه يفتح المستند مباشرة.",
    },
    {
        "keywords_en": ("create document", "add document", "new document"),
        "keywords_ar": ("إنشاء مستند", "انشاء مستند", "إضافة مستند", "اضافة مستند", "مستند جديد"),
        "en": "To create a document: open a folder and use the 'Add Document' form — fill in the subject, "
              "keywords, notes, attach any files, then save.",
        "ar": "لإنشاء مستند: افتح مجلدًا واستخدم نموذج 'إضافة مستند' — املأ الموضوع والكلمات المفتاحية والملاحظات، "
              "وأرفق الملفات، ثم احفظ.",
    },
    {
        "keywords_en": ("edit document", "edit a document"),
        "keywords_ar": ("تعديل مستند",),
        "en": "To edit a document: open it and click 'Edit' — this loads it back into the form so you can "
              "change any field and save your changes.",
        "ar": "لتعديل مستند: افتحه واضغط 'تعديل' — سيتم تحميله في النموذج لتتمكن من تغيير أي حقل وحفظ التعديلات.",
    },
    {
        "keywords_en": ("delete document", "delete a document", "remove document"),
        "keywords_ar": ("حذف مستند", "حذف المستند"),
        "en": "To delete a document: open it (or select it in the list), choose Delete, then confirm in the "
              "dialog that appears — this can't be undone, so double-check first.",
        "ar": "لحذف مستند: افتحه (أو حدده في القائمة)، اختر 'حذف'، ثم أكّد في نافذة التأكيد — لا يمكن التراجع "
              "عن هذا الإجراء، لذا تأكد أولًا.",
    },
    {
        "keywords_en": ("print",),
        "keywords_ar": ("طباعة",),
        "en": "To print: open the document and click 'Print' — it prints the document's details and metadata.",
        "ar": "للطباعة: افتح المستند واضغط 'طباعة' — ستتم طباعة تفاصيل المستند وبياناته.",
    },
    {
        "keywords_en": ("share document", "share link", "share a document"),
        "keywords_ar": ("مشاركة مستند", "مشاركة رابط", "مشاركة الرابط"),
        "en": "To share a document: generate its QR code (which also gives you a copyable link), or use the "
              "share option on an attachment to get a direct link others can open.",
        "ar": "لمشاركة مستند: أنشئ رمز QR الخاص به (وهو يمنحك أيضًا رابطًا قابلًا للنسخ)، أو استخدم خيار "
              "المشاركة على أحد المرفقات للحصول على رابط مباشر يمكن للآخرين فتحه.",
    },
    {
        "keywords_en": ("download attachment", "download file", "download a file"),
        "keywords_ar": ("تنزيل مرفق", "تحميل ملف", "تنزيل ملف"),
        "en": "To download a file: open the document, find it in the Attachments list, and click the download "
              "icon next to it.",
        "ar": "لتنزيل ملف: افتح المستند، اعثر عليه في قائمة المرفقات، واضغط أيقونة التنزيل بجانبه.",
    },
    {
        "keywords_en": ("preview attachment", "preview a file", "view attachment"),
        "keywords_ar": ("معاينة مرفق", "معاينة ملف"),
        "en": "To preview a file: open the document and click on the attachment name — it opens right there "
              "in the viewer without needing to download it.",
        "ar": "لمعاينة ملف: افتح المستند واضغط على اسم المرفق — سيُفتح مباشرة في العارض دون الحاجة لتنزيله.",
    },
    {
        "keywords_en": ("create folder", "add folder", "new folder"),
        "keywords_ar": ("إنشاء مجلد", "انشاء مجلد", "مجلد جديد", "إضافة مجلد"),
        "en": "To create a folder: go to the folder tree, choose to add a new folder (top-level or as a "
              "sub-folder of an existing one), give it a name, and save.",
        "ar": "لإنشاء مجلد: اذهب إلى شجرة المجلدات، اختر إضافة مجلد جديد (رئيسي أو كمجلد فرعي داخل مجلد آخر)، "
              "أعطه اسمًا، واحفظ.",
    },
    {
        "keywords_en": ("delete folder", "remove folder"),
        "keywords_ar": ("حذف مجلد", "حذف المجلد"),
        "en": "To delete a folder: find it in the folder tree and choose Delete — note this may affect "
              "documents stored inside it, so make sure it's really no longer needed.",
        "ar": "لحذف مجلد: اعثر عليه في شجرة المجلدات واختر 'حذف' — لاحظ أن هذا قد يؤثر على المستندات "
              "المخزنة بداخله، لذا تأكد أنه لم يعد مطلوبًا.",
    },
    {
        "keywords_en": ("favorite", "favourite", "pin folder", "pin a folder"),
        "keywords_ar": ("مفضلة", "تثبيت مجلد", "تثبيت المجلد"),
        "en": "To pin a folder as a favourite: click the pin/star icon next to a folder in the tree — "
              "favourited folders show up for quick access.",
        "ar": "لتثبيت مجلد كمفضل: اضغط أيقونة التثبيت/النجمة بجانب المجلد في الشجرة — المجلدات المفضّلة "
              "تظهر للوصول السريع.",
    },
    {
        "keywords_en": ("advanced search", "filter search", "search filters"),
        "keywords_ar": ("بحث متقدم", "تصفية البحث", "فلاتر"),
        "en": "For advanced search: open the Advanced Search panel to filter by folder, department, and "
              "date range in addition to the usual keyword search.",
        "ar": "للبحث المتقدم: افتح لوحة البحث المتقدم لتصفية النتائج حسب المجلد، القسم، ونطاق التاريخ، "
              "بالإضافة إلى البحث المعتاد بالكلمات المفتاحية.",
    },
    {
        "keywords_en": ("notification",),
        "keywords_ar": ("إشعار", "الإشعارات", "اشعار"),
        "en": "Notifications appear in the bell icon at the top — click it to see recent activity, mark "
              "items as read, or clear them all.",
        "ar": "تظهر الإشعارات في أيقونة الجرس أعلى الصفحة — اضغط عليها لرؤية النشاط الأخير، وتعليم العناصر "
              "كمقروءة، أو مسحها جميعًا.",
    },
    {
        "keywords_en": ("change password", "my profile", "my account", "update profile"),
        "keywords_ar": ("كلمة المرور", "الملف الشخصي", "حسابي", "تحديث الملف"),
        "en": "To update your profile or password: open your profile settings from the user menu, edit the "
              "fields you need (including password), and save.",
        "ar": "لتحديث ملفك الشخصي أو كلمة المرور: افتح إعدادات الملف الشخصي من قائمة المستخدم، وعدّل الحقول "
              "المطلوبة (بما في ذلك كلمة المرور)، ثم احفظ.",
    },
    {
        "keywords_en": ("report", "statistics", "stats"),
        "keywords_ar": ("تقرير", "تقارير", "إحصائيات", "احصائيات"),
        "en": "For reports and statistics: open the Reports section from the sidebar to see document counts "
              "and activity summaries.",
        "ar": "للتقارير والإحصائيات: افتح قسم 'التقارير' من الشريط الجانبي لعرض أعداد المستندات وملخصات النشاط.",
    },
    {
        "keywords_en": ("log out", "logout", "sign out"),
        "keywords_ar": ("تسجيل خروج", "تسجيل الخروج"),
        "en": "To log out: use the logout option in the user menu at the top of the page.",
        "ar": "لتسجيل الخروج: استخدم خيار تسجيل الخروج من قائمة المستخدم أعلى الصفحة.",
    },
]


@app.route("/api/chatbot", methods=["POST"])
@login_required
def api_chatbot():
    conn = None
    try:
        data = request.get_json(force=True, silent=True) or {}
        message = (data.get("message") or "").strip()
        lang = (data.get("lang") or "").strip().lower()
        if lang not in ("en", "ar"):
            # No lang given — guess from the message itself (any Arabic letters present).
            lang = "ar" if re.search(r"[\u0600-\u06FF]", message) else "en"
        is_ar = (lang == "ar")

        if not message:
            return jsonify({"reply": "اكتب رسالة وسأحاول المساعدة — جرّب 'ابحث عن <كلمة>' للبحث في المستندات."
                                      if is_ar else
                                      "Type a message and I'll try to help — try 'find <keyword>' to search documents."})

        lowered = message.lower()

        # Universal "cancel" / "never mind" — clears ANY pending multi-turn
        # state (attachment-or-link choice, delete confirmation) with an
        # explicit acknowledgement, checked before those states try to
        # interpret the message themselves. Without this, a pending state
        # only cleared silently once the user said something unrelated.
        if session.get("chatbot_pending_email") or session.get("chatbot_pending_delete"):
            if any(lowered == w for w in _CHATBOT_CANCEL_WORDS):
                session.pop("chatbot_pending_email", None)
                session.pop("chatbot_pending_delete", None)
                return jsonify({"reply": "تم الإلغاء." if is_ar else "Cancelled."})

        # If we're waiting on an "attachment or link?" answer for a previous
        # 'email document <id>' request, try to resolve it before anything
        # else. If this message doesn't look like an answer, drop the pending
        # state and fall through to normal processing rather than getting stuck.
        pending = session.get("chatbot_pending_email")
        if pending:
            mode = _chatbot_detect_send_mode(message)
            if mode:
                session.pop("chatbot_pending_email", None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = get_db_connection()
                cursor = conn.cursor()
                return jsonify(_chatbot_send_single_document_email(
                    cursor, pending["doc_id"], pending["recipient"], mode, is_ar))
            session.pop("chatbot_pending_email", None)

        # If we're waiting on a yes/no answer to "delete document <id>?",
        # resolve it before anything else. Anything that isn't a recognized
        # yes/no drops the pending state and falls through to normal
        # processing, so the person isn't stuck if they change the subject.
        pending_del = session.get("chatbot_pending_delete")
        if pending_del:
            if any(lowered == w for w in _CHATBOT_CONFIRM_YES):
                session.pop("chatbot_pending_delete", None)
                if not _check_accr(1, "Can_Del"):
                    return jsonify({"reply": "لا تملك صلاحية حذف المستندات." if is_ar
                                              else "You don't have permission to delete documents."})
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = get_db_connection()
                conn.autocommit = False
                cursor = conn.cursor()
                return jsonify(_chatbot_delete_document(cursor, conn, pending_del["doc_id"], is_ar))
            if any(lowered == w for w in _CHATBOT_CONFIRM_NO):
                session.pop("chatbot_pending_delete", None)
                return jsonify({"reply": "تم الإلغاء — لم يتم حذف أي شيء." if is_ar
                                          else "Cancelled — nothing was deleted."})
            session.pop("chatbot_pending_delete", None)

        # Greeting
        if any(lowered == g or lowered.startswith(g + " ") for g in _CHATBOT_GREETINGS):
            return jsonify({"reply": "مرحبًا! يمكنني البحث في المستندات (جرّب 'ابحث عن STC البنك')، "
                                      "أو الإجابة عن أسئلة أساسية حول استخدام النظام. كيف أساعدك؟"
                                      if is_ar else
                                      "Hello! I can search documents for you (try 'find STC bank'), "
                                      "or answer basic questions about using this system. What do you need?"})

        # "open <subpage>" navigation — checked before search/FAQ so it always
        # wins for phrases like "open reports" / "افتح التقارير".
        if any(lowered.startswith(t) for t in _CHATBOT_OPEN_TRIGGERS):
            sec = _chatbot_match_section(message)
            if sec:
                name_en = sec["keywords_en"][0]
                name_ar = sec["keywords_ar"][0]
                return jsonify({
                    "reply": f"تم فتح {name_ar}." if is_ar else f"Opening {name_en}.",
                    "action": "open_section",
                    "section": sec["section"]
                })
            return jsonify({"reply": "أي صفحة تريد فتحها؟ مثال: 'افتح التقارير'." if is_ar
                                      else "Which page would you like to open? e.g. 'open reports'."})

        # Bulk: "download all attachments for document <id>" — single document,
        # multiple attachments zipped together. Checked before the folder-based
        # bulk zip trigger below since both share the word "attachments".
        if any(lowered.startswith(t) for t in _CHATBOT_SINGLE_DOC_ZIP_TRIGGERS):
            m = re.search(r"(\d+)", message)
            if not m:
                return jsonify({"reply": "أي رقم مستند؟ مثال: 'نزّل كل مرفقات المستند 88070'" if is_ar
                                          else "Which document number? e.g. 'download all attachments for document 88070'"})
            doc_id = int(m.group(1))
            zip_url = url_for("api_documents_bulk_zip", doc_ids=str(doc_id))
            return jsonify({
                "reply": f"جارٍ تجهيز ملف مضغوط لمرفقات المستند #{doc_id}..." if is_ar
                         else f"Preparing a zip of document #{doc_id}'s attachments...",
                "action": "download_zip",
                "url": zip_url,
            })

        # Plain "download document <reg#>" — access-checked, then a direct
        # file link if there's exactly one attachment, or a zip if there are
        # several (so a one-file document doesn't get wrapped in a zip
        # needlessly).
        if any(lowered.startswith(t) for t in _CHATBOT_DOWNLOAD_DOC_TRIGGERS):
            m = re.search(r"(\d+)", message)
            if not m:
                return jsonify({"reply": "أي رقم مستند؟ مثال: 'نزّل المستند 88070'" if is_ar
                                          else "Which document number? e.g. 'download document 88070'"})
            doc_id = int(m.group(1))

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            access_where, access_params = _current_user_doc_access_clause(cursor, "t")
            cursor.execute(
                f"SELECT t.ID FROM dbo.Adco_Transactions t "
                f"WHERE t.IsDeleted = 0 AND t.ID = ? AND {access_where}",
                doc_id, *access_params,
            )
            if not cursor.fetchone():
                return jsonify({"reply": f"لم أجد المستند #{doc_id}، أو لا تملك صلاحية الوصول إليه." if is_ar
                                          else f"I couldn't find document #{doc_id}, or you don't have access to it."})

            atts = load_attachments_for_transactions(cursor, [doc_id]).get(doc_id, [])
            if not atts:
                return jsonify({"reply": f"لا توجد مرفقات في المستند #{doc_id}." if is_ar
                                          else f"Document #{doc_id} has no attachments to download."})

            if len(atts) == 1:
                return jsonify({
                    "reply": f"جارٍ تنزيل المستند #{doc_id}..." if is_ar
                             else f"Downloading document #{doc_id}...",
                    "action": "download_zip",
                    "url": atts[0]["download_url"],
                })

            zip_url = url_for("api_documents_bulk_zip", doc_ids=str(doc_id))
            return jsonify({
                "reply": f"جارٍ تجهيز ملف مضغوط للمستند #{doc_id} ({len(atts)} ملفات)..." if is_ar
                         else f"Preparing a zip of document #{doc_id} ({len(atts)} files)...",
                "action": "download_zip",
                "url": zip_url,
            })

        # "who can I send to?" — lists active users with a saved email, so
        # people can see valid names before typing "email document ... to <name>".
        if any(lowered.startswith(t) for t in _CHATBOT_LIST_RECIPIENTS_TRIGGERS):
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            people = _chatbot_list_recipients(cursor)
            if not people:
                return jsonify({"reply": "لا يوجد مستخدمون لديهم بريد إلكتروني محفوظ حاليًا." if is_ar
                                          else "There are no users with a saved email right now."})
            lines = [f"{p['full_name']} — {p['email']}" for p in people]
            header = "يمكنك الإرسال إلى:" if is_ar else "You can send to:"
            return jsonify({"reply": header + "\n" + "\n".join(lines)})

        # Delete a document by registration number: "delete document <reg#>".
        # Never deletes immediately — checks permission + access first, then
        # asks for a yes/no confirmation and remembers the pending request.
        if any(lowered.startswith(t) for t in _CHATBOT_DELETE_DOC_TRIGGERS):
            m = re.search(r"(\d+)", message)
            if not m:
                return jsonify({"reply": "أي رقم مستند تريد حذفه؟ مثال: 'حذف مستند 88070'" if is_ar
                                          else "Which document number should I delete? e.g. 'delete document 88070'"})
            doc_id = int(m.group(1))

            if not _check_accr(1, "Can_Del"):
                return jsonify({"reply": "لا تملك صلاحية حذف المستندات." if is_ar
                                          else "You don't have permission to delete documents."})

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            access_where, access_params = _current_user_doc_access_clause(cursor, "t")
            cursor.execute(
                f"SELECT t.ID, t.Subject FROM dbo.Adco_Transactions t "
                f"WHERE t.IsDeleted = 0 AND t.ID = ? AND {access_where}",
                doc_id, *access_params,
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"reply": f"لم أجد المستند #{doc_id}، أو لا تملك صلاحية الوصول إليه." if is_ar
                                          else f"I couldn't find document #{doc_id}, or you don't have access to it."})
            subject = row[1] or ("(بدون موضوع)" if is_ar else "(no subject)")

            session["chatbot_pending_delete"] = {"doc_id": doc_id}
            return jsonify({
                "reply": f"هل أنت متأكد من حذف المستند #{doc_id} — {subject}؟" if is_ar
                         else f"Are you sure you want to delete document #{doc_id} — {subject}?",
                "quick_replies": [
                    {"label": "نعم، احذف" if is_ar else "Yes, delete", "value": "نعم" if is_ar else "yes"},
                    {"label": "لا، إلغاء" if is_ar else "No, cancel", "value": "لا" if is_ar else "no"},
                ],
            })

        # Multi-document bulk send by explicit ID list: "email documents
        # 123, 456, 789 to Ahmed" — distinct from both the single-document
        # send below (exactly one ID) and the whole-folder bulk send above.
        # Only handled here when 2+ document numbers are present; a single
        # ID falls through to the normal single-document flow untouched.
        if any(lowered.startswith(t) for t in _CHATBOT_EMAIL_DOC_TRIGGERS):
            doc_ids = [int(n) for n in re.findall(r"\d+", message)]
            if len(doc_ids) >= 2:
                text = message
                recipient = None
                split_words = _RECIPIENT_SPLIT_AR if is_ar else _RECIPIENT_SPLIT_EN
                lowered_text = text.lower()
                for sw in split_words:
                    idx = lowered_text.rfind(sw)
                    if idx != -1:
                        recipient = text[idx + len(sw):].strip().strip(".").strip()
                        break

                if not recipient:
                    return jsonify({"reply": "إلى من؟ مثال: 'أرسل المستندات 123, 456 إلى بريدي'" if is_ar
                                              else "To whom? e.g. 'email documents 123, 456 to me'"})

                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = get_db_connection()
                cursor = conn.cursor()

                if recipient.strip().lower() in ("me", "myself", "لي", "نفسي"):
                    cursor.execute("SELECT USER_EMAIL FROM dbo.Sys_User WHERE USER_ID = ?", session["user_id"])
                    row = cursor.fetchone()
                    resolved_email = row[0] if row and row[0] else None
                else:
                    resolved_email, candidates = _chatbot_resolve_recipient(cursor, recipient)
                    if not resolved_email and candidates:
                        names = ", ".join(c["full_name"] for c in candidates)
                        return jsonify({"reply": f"وجدت أكثر من مستخدم يطابق \"{recipient}\": {names}. حدد بالبريد الإلكتروني."
                                                  if is_ar else
                                                  f"I found more than one user matching \"{recipient}\": {names}. "
                                                  f"Please specify by email instead."})

                if not resolved_email:
                    return jsonify({"reply": f"لم أجد مستخدمًا أو بريدًا باسم \"{recipient}\"." if is_ar
                                              else f"I couldn't find a user or email matching \"{recipient}\"."})

                doc_ids = doc_ids[:BULK_EMAIL_MAX_DOCS]
                access_where, access_params = _current_user_doc_access_clause(cursor, "t")
                id_placeholders = ",".join("?" * len(doc_ids))
                cursor.execute(
                    f"SELECT t.ID FROM dbo.Adco_Transactions t "
                    f"WHERE t.ID IN ({id_placeholders}) AND {access_where}",
                    *doc_ids, *access_params,
                )
                accessible_ids = {row[0] for row in cursor.fetchall()}
                skipped_ids = [d for d in doc_ids if d not in accessible_ids]

                sent, failed = 0, 0
                for doc_id in doc_ids:
                    if doc_id not in accessible_ids:
                        continue
                    result, status = _send_document_email_core(
                        doc_id, [resolved_email], attach_file=True,
                    )
                    if status == 200 and result.get("success"):
                        sent += 1
                    else:
                        failed += 1

                if is_ar:
                    reply = f"تم إرسال {sent} من {len(doc_ids)} مستند إلى {resolved_email}."
                    if failed:
                        reply += f" فشل إرسال {failed}."
                    if skipped_ids:
                        reply += f" (تم تخطي {', '.join(str(i) for i in skipped_ids)} — غير موجود أو بدون صلاحية)."
                else:
                    reply = f"Sent {sent} of {len(doc_ids)} document(s) to {resolved_email}."
                    if failed:
                        reply += f" {failed} failed to send."
                    if skipped_ids:
                        reply += f" (Skipped #{', #'.join(str(i) for i in skipped_ids)} — not found or no access.)"
                return jsonify({"reply": reply})

        # Email a single document by registration number: "email document
        # <reg#> [to <email>] [as attachment|as link]". If the send mode
        # isn't given inline, ask and remember the pending request so the
        # next message (just "attachment" or "link") can complete it.
        if any(lowered.startswith(t) for t in _CHATBOT_EMAIL_DOC_TRIGGERS):
            doc_id, recipient, mode = _chatbot_parse_email_doc(message, is_ar)
            if not doc_id:
                return jsonify({"reply": "أي رقم مستند؟ مثال: 'أرسل المستند 88070 إلى بريدي'" if is_ar
                                          else "Which document number? e.g. 'email document 88070 to me'"})

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()

            if recipient:
                resolved_email, candidates = _chatbot_resolve_recipient(cursor, recipient)
                if resolved_email:
                    recipient = resolved_email
                elif candidates:
                    names = ", ".join(c["full_name"] for c in candidates)
                    return jsonify({"reply": f"وجدت أكثر من مستخدم يطابق \"{recipient}\": {names}. حدد بالبريد الإلكتروني."
                                              if is_ar else
                                              f"I found more than one user matching \"{recipient}\": {names}. "
                                              f"Please specify by email instead."})
                else:
                    return jsonify({"reply": f"لم أجد مستخدمًا أو بريدًا باسم \"{recipient}\". جرّب 'من يمكنني الإرسال له؟' لعرض القائمة."
                                              if is_ar else
                                              f"I couldn't find a user or email matching \"{recipient}\". "
                                              f"Try 'who can I send to?' to see the list."})
            else:
                cursor.execute("SELECT USER_EMAIL FROM dbo.Sys_User WHERE USER_ID = ?", session["user_id"])
                row = cursor.fetchone()
                recipient = row[0] if row and row[0] else None
            if not recipient:
                return jsonify({"reply": "لا يوجد بريد إلكتروني محفوظ لحسابك — حدد مستلمًا، مثال: '... إلى name@example.com'"
                                          if is_ar else
                                          "Your account has no email on file — specify a recipient, "
                                          "e.g. '... to name@example.com'"})

            access_where, access_params = _current_user_doc_access_clause(cursor, "t")
            cursor.execute(
                f"SELECT t.ID, t.Subject FROM dbo.Adco_Transactions t "
                f"WHERE t.IsDeleted = 0 AND t.ID = ? AND {access_where}",
                doc_id, *access_params,
            )
            row = cursor.fetchone()
            if not row:
                return jsonify({"reply": f"لم أجد المستند #{doc_id}، أو لا تملك صلاحية الوصول إليه." if is_ar
                                          else f"I couldn't find document #{doc_id}, or you don't have access to it."})

            if not mode:
                session["chatbot_pending_email"] = {"doc_id": doc_id, "recipient": recipient}
                return jsonify({
                    "reply": f"هل أرسل المستند #{doc_id} إلى {recipient} كمرفق أم كرابط؟" if is_ar
                             else f"Should I send document #{doc_id} to {recipient} as an attachment or as a link?",
                    "quick_replies": [
                        {"label": "كمرفق" if is_ar else "As attachment", "value": "attachment"},
                        {"label": "كرابط" if is_ar else "As link", "value": "link"},
                    ],
                })

            return jsonify(_chatbot_send_single_document_email(cursor, doc_id, recipient, mode, is_ar))

        # Bulk: "email all documents in <folder> [to <email>]"
        if any(lowered.startswith(t) for t in _CHATBOT_BULK_EMAIL_TRIGGERS):
            folder_text, recipient = _chatbot_parse_bulk_email(message, is_ar)
            if not folder_text:
                return jsonify({"reply": "أي مجلد؟ مثال: 'أرسل كل المستندات في المالية إلى بريدي'" if is_ar
                                          else "Which folder? e.g. 'email all documents in Finance to me'"})

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            folder_id, folder_name = find_folder_by_name(cursor, folder_text)
            if not folder_id:
                return jsonify({"reply": f"لم أجد مجلدًا باسم \"{folder_text}\"." if is_ar
                                          else f"I couldn't find a folder named \"{folder_text}\"."})

            if recipient:
                resolved_email, candidates = _chatbot_resolve_recipient(cursor, recipient)
                if resolved_email:
                    recipient = resolved_email
                elif candidates:
                    names = ", ".join(c["full_name"] for c in candidates)
                    return jsonify({"reply": f"وجدت أكثر من مستخدم يطابق \"{recipient}\": {names}. حدد بالبريد الإلكتروني."
                                              if is_ar else
                                              f"I found more than one user matching \"{recipient}\": {names}. "
                                              f"Please specify by email instead."})
                else:
                    return jsonify({"reply": f"لم أجد مستخدمًا أو بريدًا باسم \"{recipient}\". جرّب 'من يمكنني الإرسال له؟' لعرض القائمة."
                                              if is_ar else
                                              f"I couldn't find a user or email matching \"{recipient}\". "
                                              f"Try 'who can I send to?' to see the list."})
            else:
                cursor.execute("SELECT USER_EMAIL FROM dbo.Sys_User WHERE USER_ID = ?", session["user_id"])
                row = cursor.fetchone()
                recipient = row[0] if row and row[0] else None
            if not recipient:
                return jsonify({"reply": "لا يوجد بريد إلكتروني محفوظ لحسابك — حدد مستلمًا، مثال: '... إلى name@example.com'"
                                          if is_ar else
                                          "Your account has no email on file — specify a recipient, "
                                          "e.g. '... to name@example.com'"})

            total, results = run_document_search(cursor, folder_id=folder_id, page=1, page_size=BULK_EMAIL_MAX_DOCS)

            if not total:
                return jsonify({"reply": f"لا توجد مستندات في \"{folder_name}\"." if is_ar
                                          else f"There are no documents in \"{folder_name}\"."})

            sent, failed = 0, 0
            for r in results:
                att_ids = [a["id"] for a in r.get("attachments", [])]
                result, status = _send_document_email_core(
                    r["id"], [recipient], attachment_ids=att_ids, attach_file=True,
                )
                if status == 200 and result.get("success"):
                    sent += 1
                else:
                    failed += 1

            skipped = total - len(results)
            if is_ar:
                reply = f"تم إرسال {sent} مستند من \"{folder_name}\" إلى {recipient}."
                if failed:
                    reply += f" فشل إرسال {failed}."
                if skipped:
                    reply += f" (تم تخطي {skipped} — الحد الأقصى {BULK_EMAIL_MAX_DOCS} لكل أمر)."
            else:
                reply = f"Sent {sent} document(s) from \"{folder_name}\" to {recipient}."
                if failed:
                    reply += f" {failed} failed to send."
                if skipped:
                    reply += f" ({skipped} skipped — {BULK_EMAIL_MAX_DOCS} max per command.)"
            return jsonify({"reply": reply})

        # Bulk: "download all attachments in <folder>" — zips every attachment
        # across every document in the folder.
        if any(lowered.startswith(t) for t in _CHATBOT_BULK_ZIP_TRIGGERS):
            folder_text = _chatbot_parse_bulk_zip_folder(message, is_ar, _CHATBOT_BULK_ZIP_TRIGGERS)
            if not folder_text:
                return jsonify({"reply": "أي مجلد؟ مثال: 'نزّل كل المرفقات في المالية'" if is_ar
                                          else "Which folder? e.g. 'download all attachments in Finance'"})

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            folder_id, folder_name = find_folder_by_name(cursor, folder_text)
            if not folder_id:
                return jsonify({"reply": f"لم أجد مجلدًا باسم \"{folder_text}\"." if is_ar
                                          else f"I couldn't find a folder named \"{folder_text}\"."})

            zip_url = url_for("api_documents_bulk_zip", folder_id=folder_id)
            return jsonify({
                "reply": f"جارٍ تجهيز ملف مضغوط لكل مرفقات \"{folder_name}\"..." if is_ar
                         else f"Preparing a zip of every attachment in \"{folder_name}\"...",
                "action": "download_zip",
                "url": zip_url,
            })

        # Document search — checked before the FAQ keyword matching below,
        # so an explicit 'find/search ...' command always wins even if the
        # search term happens to contain a FAQ keyword as a substring
        # (e.g. "find printer manual" contains "print" but should still search).
        if any(lowered.startswith(t) for t in _CHATBOT_SEARCH_TRIGGERS):
            term = _chatbot_extract_search_term(message)
            if not term:
                return jsonify({"reply": "عن ماذا تريد البحث؟ مثال: 'ابحث عن فاتورة STC'"
                                          if is_ar else
                                          "What should I search for? e.g. 'find invoice STC'"})

            # Natural-language date phrase ("last week", "الأسبوع الماضي", ...)
            # narrows the search and is stripped out of the free-text term.
            date_from, date_to, term = _chatbot_parse_date_phrase(term)
            if not term and (date_from or date_to):
                # Message was only a date phrase with no keyword, e.g.
                # "find documents from last week" reduces to just a date range.
                term = ""

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            total, results = run_document_search(
                cursor, q=term or None, date_from=date_from, date_to=date_to,
                page=1, page_size=5
            )

            if not total:
                suggestions = []
                if term:
                    access_where, access_params = _current_user_doc_access_clause(cursor, "t")
                    suggestions = _fuzzy_search_suggestions(cursor, term, [access_where] if access_where else [], access_params)
                if suggestions:
                    options = suggestions[:3]
                    reply = ("لم أجد أي مستندات مطابقة لـ \"{}\". هل تقصد أحد هذه؟".format(term) if is_ar
                             else f"I couldn't find any documents matching \"{term}\". Did you mean one of these?")
                    return jsonify({
                        "reply": reply,
                        "quick_replies": [
                            {"label": s, "value": (f"ابحث عن {s}" if is_ar else f"find {s}")}
                            for s in options
                        ],
                    })
                label = term or (date_from and date_to and f"{date_from}–{date_to}") or ""
                return jsonify({"reply": f"لم أجد أي مستندات مطابقة لـ \"{label}\"." if is_ar
                                          else f"I couldn't find any documents matching \"{label}\"."})

            # Remember this search so a follow-up "show me more" / "المزيد"
            # doesn't require retyping the term (or date range), and so a
            # bare "summarize" can refer to the top result without an ID.
            session["chatbot_last_search"] = {
                "term": term, "date_from": date_from, "date_to": date_to, "shown": 5,
                "last_result_ids": [r["id"] for r in results[:5]],
            }

            preview = results[:5]
            no_folder = "بدون مجلد" if is_ar else "no folder"
            no_subject = "(بدون موضوع)" if is_ar else "(no subject)"
            lines = []
            for r in preview:
                line = f"#{r['id']} — {r['subject'] or no_subject} ({r['folder_name'] or no_folder})"
                if r.get("content_snippet"):
                    line += f"\n  … {r['content_snippet']}"
                lines.append(line)
            display_label = term or f"{date_from} – {date_to}"
            if is_ar:
                header = f"تم العثور على {total} مستند مطابق لـ \"{display_label}\":"
                more = f"\n…و{total - len(preview)} أخرى. قل 'المزيد' لعرض المزيد." if total > len(preview) else ""
            else:
                header = f"Found {total} document(s) matching \"{display_label}\":"
                more = f"\n…and {total - len(preview)} more. Say 'show me more' to see more." if total > len(preview) else ""
            return jsonify({
                "reply": header + "\n" + "\n".join(lines) + more,
                "results": preview
            })

        # "Show me more" — continues the most recent chatbot search (term +
        # date range, if any) from where it left off, using session state so
        # the person doesn't have to retype anything.
        if any(lowered.strip() == p for p in _CHATBOT_SHOW_MORE_TRIGGERS):
            last = session.get("chatbot_last_search")
            if not last:
                return jsonify({"reply": "لا يوجد بحث سابق لعرض المزيد منه. جرّب 'ابحث عن ...' أولاً."
                                          if is_ar else
                                          "There's no previous search to continue. Try 'find ...' first."})
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            shown = last.get("shown", 0)
            next_page = (shown // 5) + 1
            total, results = run_document_search(
                cursor, q=last.get("term") or None,
                date_from=last.get("date_from"), date_to=last.get("date_to"),
                page=next_page, page_size=5
            )
            if not results:
                return jsonify({"reply": "لا مزيد من النتائج." if is_ar else "No more results."})

            session["chatbot_last_search"]["shown"] = shown + len(results)
            no_folder = "بدون مجلد" if is_ar else "no folder"
            no_subject = "(بدون موضوع)" if is_ar else "(no subject)"
            lines = []
            for r in results:
                line = f"#{r['id']} — {r['subject'] or no_subject} ({r['folder_name'] or no_folder})"
                if r.get("content_snippet"):
                    line += f"\n  … {r['content_snippet']}"
                lines.append(line)
            remaining = total - (shown + len(results))
            more = (f"\n…و{remaining} أخرى. قل 'المزيد' لعرض المزيد." if remaining > 0 and is_ar else
                    f"\n…and {remaining} more. Say 'show me more' to see more." if remaining > 0 else "")
            return jsonify({"reply": "\n".join(lines) + more, "results": results})

        # "Summarize document <id>" / "لخص المستند <رقم>" — extractive summary
        # of the document's cached OCR text, so the user doesn't have to open
        # the file just to see what it says. With no ID given, falls back to
        # the top result of the most recent chatbot search, if any.
        if any(lowered.startswith(t) or lowered == t.strip() for t in _CHATBOT_SUMMARIZE_TRIGGERS):
            doc_id = None
            m = re.search(r"\d+", message)
            if m:
                doc_id = int(m.group())
            elif session.get("chatbot_last_search", {}).get("last_result_ids"):
                doc_id = session["chatbot_last_search"]["last_result_ids"][0]

            if not doc_id:
                return jsonify({"reply": "أي مستند تريد تلخيصه؟ مثال: 'لخص المستند 12345'" if is_ar
                                          else "Which document should I summarize? e.g. 'summarize document 12345'"})

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            access_where, access_params = _current_user_doc_access_clause(cursor, "t")
            cursor.execute(
                f"SELECT ID FROM dbo.Adco_Transactions t WHERE t.ID = ? AND {access_where}",
                doc_id, *access_params
            )
            if not cursor.fetchone():
                return jsonify({"reply": f"لم أجد المستند رقم {doc_id} أو ليس لديك صلاحية الوصول إليه." if is_ar
                                          else f"I couldn't find document #{doc_id}, or you don't have access to it."})

            cursor.execute(
                f"""SELECT ID FROM dbo.Adco_Transactions_Attachments
                    WHERE Transaction_ID = ? AND {_attachments_active_sql()}""",
                doc_id
            )
            att_ids = [row[0] for row in cursor.fetchall()]
            ocr_text = ""
            for att_id in att_ids:
                ocr_text = ocr_cache_read(att_id)
                if ocr_text:
                    break

            if not ocr_text:
                return jsonify({"reply": f"لا يوجد نص مستخرج (OCR) لهذا المستند بعد، لذلك لا يمكنني تلخيصه." if is_ar
                                          else "There's no extracted (OCR) text for this document yet, so I can't summarize it."})

            if not _text_quality_ok(ocr_text):
                return jsonify({"reply": (
                    f"النص المستخرج من المستند #{doc_id} يبدو رقميًا أو جدوليًا (كنموذج أو جدول)، "
                    "وليس هناك محتوى نصي كافٍ لتلخيصه بشكل مفيد."
                ) if is_ar else (
                    f"The extracted text for document #{doc_id} looks numeric or tabular (like a form or table), "
                    "so there isn't enough real content to produce a useful summary."
                )})

            summary = _summarize_text(ocr_text, max_sentences=3)
            if not summary:
                return jsonify({"reply": "تعذر إنشاء ملخص لهذا المستند." if is_ar else "Couldn't generate a summary for this document."})

            reply = (f"ملخص المستند #{doc_id}:\n{summary}" if is_ar
                     else f"Summary of document #{doc_id}:\n{summary}")
            return jsonify({"reply": reply})

        # "How many documents were archived this month/last week/...?" —
        # a lightweight count pulled straight from Adco_Transactions, scoped
        # to the user's access, reusing the same natural-date phrase parser
        # as chat search. Defaults to "this month" if no period is given,
        # since that's the most common way people ask this.
        if any(t in lowered for t in _CHATBOT_REPORT_TRIGGERS):
            date_from, date_to, _rest = _chatbot_parse_date_phrase(message)
            period_label_ar = "هذا الشهر"
            period_label_en = "this month"
            if not date_from:
                today = datetime.now()
                date_from = today.replace(day=1).strftime("%Y/%m/%d")
                date_to = today.strftime("%Y/%m/%d")
            else:
                period_label_ar = period_label_en = f"{date_from} – {date_to}"

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = get_db_connection()
            cursor = conn.cursor()
            access_where, access_params = _current_user_doc_access_clause(cursor, "t")
            cursor.execute(
                f"""SELECT COUNT(*) FROM dbo.Adco_Transactions t
                    WHERE {access_where}
                      AND CAST(t.CreatedOn AS date) >= CONVERT(date, ?, 111)
                      AND CAST(t.CreatedOn AS date) <= CONVERT(date, ?, 111)""",
                *access_params, date_from, date_to
            )
            count = cursor.fetchone()[0]
            reply = (f"تم أرشفة {count} مستند خلال {period_label_ar}." if is_ar
                     else f"{count} document(s) were archived during {period_label_en}.")
            return jsonify({"reply": reply})



        # ── Workflow: inbox / sent / status / approve / reject / overdue / expiring ──
        # Checked before FAQ/help so these read as actions, not documentation.
        if any(t in lowered for t in _CHATBOT_WF_INBOX_TRIGGERS):
            if conn:
                try: conn.close()
                except Exception: pass
            conn = get_db_connection()
            cursor = conn.cursor()
            return jsonify(_wf_chatbot_inbox(cursor, session["user_id"], is_ar))

        if any(t in lowered for t in _CHATBOT_WF_SENT_TRIGGERS):
            if conn:
                try: conn.close()
                except Exception: pass
            conn = get_db_connection()
            cursor = conn.cursor()
            return jsonify(_wf_chatbot_sent(cursor, session["user_id"], is_ar))

        if any(t in lowered for t in _CHATBOT_WF_STATUS_TRIGGERS):
            if conn:
                try: conn.close()
                except Exception: pass
            conn = get_db_connection()
            cursor = conn.cursor()
            return jsonify(_wf_chatbot_status(cursor, _wf_chatbot_extract_id(message), is_ar))

        if any(t in lowered for t in _CHATBOT_WF_OVERDUE_TRIGGERS):
            if conn:
                try: conn.close()
                except Exception: pass
            conn = get_db_connection()
            cursor = conn.cursor()
            return jsonify(_wf_chatbot_overdue(cursor, session["user_id"], get_current_role() == "admin", is_ar))

        if any(t in lowered for t in _CHATBOT_WF_EXPIRING_TRIGGERS):
            if conn:
                try: conn.close()
                except Exception: pass
            conn = get_db_connection()
            cursor = conn.cursor()
            return jsonify(_wf_chatbot_expiring(cursor, session["user_id"], get_current_role() == "admin", is_ar))

        # Approve/reject reuse the REAL /api/workflow/instances/<id>/approve|reject
        # view functions directly (same permission checks, step-completion,
        # min-approvals, archiving, and notification logic) instead of
        # duplicating that multi-step business logic here. Calling them as
        # plain Python functions is safe: they only read session/request.get_json,
        # both of which are still valid in this same request context, and
        # request.get_json(silent=True) just returns {} since the chatbot body
        # has no "next_user_ids" field, which matches an unassisted approve/reject.
        if any(t in lowered for t in _CHATBOT_WF_APPROVE_TRIGGERS) or any(t in lowered for t in _CHATBOT_WF_REJECT_TRIGGERS):
            is_approve = any(t in lowered for t in _CHATBOT_WF_APPROVE_TRIGGERS)
            instance_id = _wf_chatbot_extract_id(message)
            if instance_id is None:
                verb_ar, verb_en = ("وافق", "approve") if is_approve else ("ارفض", "reject")
                return jsonify({"reply": f"أي مستند تريد {verb_ar}؟ مثال: '{verb_ar} #123'." if is_ar
                                          else f"Which one would you like to {verb_en}? Try: \"{verb_en} #123\"."})
            if conn:
                try: conn.close()
                except Exception: pass
            conn = None  # the underlying route opens/closes its own connection
            resp = api_workflow_approve(instance_id) if is_approve else api_workflow_reject(instance_id)
            # Both views return a Flask Response from jsonify(); unwrap it so we
            # can fold the result into the chatbot's own reply format instead of
            # returning their raw {"success": true, ...} shape to the widget.
            payload = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
            status_code = resp[1] if isinstance(resp, tuple) else 200
            if status_code >= 400 or payload.get("error"):
                return jsonify({"reply": payload.get("error", "Something went wrong.")}), (status_code if status_code >= 400 else 200)
            verb_ar, verb_en = ("تمت الموافقة على", "approved") if is_approve else ("تم رفض", "rejected")
            return jsonify({"reply": f"{verb_ar} المستند #{instance_id}." if is_ar
                                      else f"Document #{instance_id} has been {verb_en}."})

        # Topic-specific FAQ (checked before the generic help fallback, so
        # "how to scan" answers scanning, not a generic feature list)
        for topic in _CHATBOT_FAQ_TOPICS:
            kws = topic["keywords_ar"] if is_ar else topic["keywords_en"]
            if any(kw in lowered for kw in kws):
                return jsonify({"reply": topic["ar"] if is_ar else topic["en"]})

        # Help — built dynamically so we don't advertise actions the user
        # doesn't actually have permission for (e.g. delete, if Can_Del is denied).
        if any(w in lowered for w in _CHATBOT_HELP_WORDS):
            _log_chatbot_unmatched(message, lang, "help")
            can_delete = _check_accr(1, "Can_Del")

            if is_ar:
                lines = [
                    "هذا ما يمكنني فعله:",
                    "",
                    "🔍 البحث والتصفح",
                    "• 'ابحث عن <كلمة>' — بحث حسب الموضوع أو الكلمات المفتاحية أو الرقم",
                    "• 'المزيد' — عرض نتائج إضافية من آخر بحث",
                    "• 'لخص المستند <رقم>' — ملخص سريع لمحتوى المستند (إن توفر نص مستخرج)",
                    "• 'كم عدد المستندات هذا الشهر/الأسبوع الماضي...' — إحصائية سريعة",
                    "• 'افتح <صفحة>' — الانتقال مباشرة إلى صفحة مثل الاستعلامات، الأرشفة، "
                    "المجلدات، التقارير، الإعدادات، أو دليل المستخدم",
                    "",
                    "📄 إدارة المستندات",
                    "• افتح مستندًا من النتائج لعرضه أو تعديله أو إرساله بالبريد أو إنشاء رمز QR له",
                    "• 'أرسل المستند <رقم> إلى <اسم أو بريد>' — إرسال مستند واحد (سأسألك مرفق أم رابط)",
                    "• 'نزّل المستند <رقم>' — تنزيل مباشر (ملف واحد) أو مضغوط (عدة ملفات)",
                ]
                if can_delete:
                    lines.append("• 'حذف مستند <رقم>' — حذف مستند (سأطلب تأكيدك أولًا بـ 'نعم' أو 'لا')")
                lines += [
                    "",
                    "🔄 سير العمل",
                    "• 'بانتظار موافقتي' — عرض العناصر بانتظارك",
                    "• 'المرسلة' — عرض ما أرسلته وحالته",
                    "• 'حالة المستند <رقم>' — سجل الموافقات الكامل",
                    "• 'وافق على #<رقم>' / 'ارفض #<رقم>' — اتخاذ إجراء",
                    "• 'متأخر' — العناصر العالقة أكثر من 3 أيام",
                    "• 'تنتهي قريبًا' — المستندات التي تنتهي صلاحيتها خلال 30 يومًا",
                    "",
                    "📦 إجراءات جماعية",
                    "• 'من يمكنني الإرسال له؟' — عرض قائمة المستخدمين وبريدهم الإلكتروني",
                    "• 'أرسل كل المستندات في <مجلد> إلى <اسم أو بريد>' — إرسال كل مستندات مجلد بالبريد",
                    "• 'نزّل كل المرفقات في <مجلد>' أو 'نزّل كل مرفقات المستند <رقم>' — تنزيل مضغوط",
                    "",
                    "❓ إرشادات الاستخدام",
                    "اسألني 'كيف أمسح مستندًا'، 'كيف أرسل بالبريد'، 'كيف أنشئ رمز QR'، "
                    "'كيف أحذف مستندًا'، 'كيف أطبع'، 'كيف أشارك مستندًا'، 'كيف أنشئ مجلدًا/أحذفه'، "
                    "'البحث المتقدم'، 'الإشعارات'، 'كلمة المرور'، 'التقارير'، 'تسجيل الخروج' وغيرها",
                ]
            else:
                lines = [
                    "Here's what I can do:",
                    "",
                    "🔍 Search & browse",
                    "• 'find <keyword>' — search documents by subject/keywords/ID",
                    "• 'show me more' — see more results from your last search",
                    "• 'summarize document <id>' — quick summary of a document's content (if OCR text exists)",
                    "• 'how many documents this month/last week/...' — quick archive counts",
                    "• 'open <page>' — jump straight to a page like Inquiries, Archive, Folders, "
                    "Reports, Settings, or the User Guide",
                    "",
                    "📄 Document actions",
                    "• Open a document from the results to view, edit, email, or QR it",
                    "• 'email document <reg#> to <name or email>' — send one document (I'll ask attachment or link)",
                    "• 'download document <id>' — direct download (single file) or zip (multiple)",
                ]
                if can_delete:
                    lines.append("• 'delete document <reg#>' — delete a document (I'll ask you to confirm with yes/no first)")
                lines += [
                    "",
                    "🔄 Workflow",
                    "• 'what's pending my approval' — list items waiting on you",
                    "• 'sent' — list what you've submitted and their status",
                    "• 'status of #123' — full approval history for a document",
                    "• 'approve #123' / 'reject #123' — act on an item",
                    "• 'overdue' — items stuck more than 3 days",
                    "• 'expiring' — documents expiring in the next 30 days",
                    "",
                    "📦 Bulk actions",
                    "• 'who can I send to?' — list users with a saved email",
                    "• 'email all documents in <folder> to <name or email>' — bulk-email a whole folder",
                    "• 'download all attachments in <folder>' or 'download all attachments for document <id>' — get a zip file",
                    "",
                    "❓ How-to guides",
                    "Ask me things like 'how to scan', 'how to email', 'how to create a QR code', "
                    "'how to delete a document', 'how to print', 'how to share a document', "
                    "'how to create/delete a folder', 'advanced search', 'notifications', "
                    "'change password', 'reports', 'log out', and more",
                ]

            return jsonify({"reply": "\n".join(lines)})

        # Fallback — don't guess, just point them at what works
        _log_chatbot_unmatched(message, lang, "fallback")
        return jsonify({"reply": "أنا مساعد بسيط حاليًا — يمكنني البحث في المستندات "
                                  "(جرّب 'ابحث عن <كلمة>') أو عرض المساعدة (اكتب 'مساعدة')."
                                  if is_ar else
                                  "I'm a simple assistant for now — I can search documents "
                                  "(try 'find <keyword>') or show help (type 'help')."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Single document detail ───────────────────────────────────────────
@app.route("/api/documents/<int:doc_id>")
@login_required
def api_get_document(doc_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        _ensure_fe_columns_adco_transactions()
        cursor.execute("""
                       SELECT t.ID,
                              t.Subject,
                              t.H_Date,
                              t.Keywords,
                              t.Detailes,
                              t.Importance_Degree_ID,
                              t.Secret_Degree_ID,
                              t.Status_ID,
                              t.Foldes_ID,
                              t.Fe1, t.Fe2, t.Fe3, t.Fe4, t.Fe5, t.Fe6, t.Fe7,
                              t.Form_Date
                       FROM dbo.Adco_Transactions t
                       WHERE t.ID = ?
                         AND t.IsDeleted = 0
                       """, doc_id)
        r = cursor.fetchone()
        if not r:
            return jsonify({"error": "Not found"}), 404

        folder_id = r[8]
        folder_names = load_folder_names(cursor, [folder_id] if folder_id else [])
        attachments = load_attachments_for_transactions(cursor, [doc_id]).get(doc_id, [])

        # Resolve the folder's department id so edit mode can restore the
        # exact same folder/subfolder (and To_Dept) on save.
        folder_dept_id = None
        if folder_id:
            try:
                dcol = adco_folder_dept_col(cursor)
                dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
                cursor.execute(
                    f"SELECT {dept_bracket} FROM dbo.Adco_Folder WHERE ID = ? AND IsDeleted = 0",
                    int(folder_id),
                )
                _drow = cursor.fetchone()
                if _drow and _drow[0]:
                    folder_dept_id = int(_drow[0])
            except Exception:
                pass

        audit_log("VIEW", page_id=1, notes=f"Viewed document ID {doc_id}")
        _wf_mark_in_progress_on_open(cursor, conn, doc_id, session["user_id"])
        return jsonify({
            "id": r[0],
            "registration_number": str(r[0]),
            "subject": r[1] or "",
            "date": str(r[2]) if r[2] else "",
            "keywords": r[3] or "",
            "notes": r[4] or "",
            "importance_id": r[5],
            "secret_id": r[6],
            "status_id": r[7],
            "folder_name": folder_names.get(folder_id, "") if folder_id else "",
            "folder_id": folder_id,
            "folder_dept_id": folder_dept_id,
            "attachments": attachments,
            "Fe1": r[9], "Fe2": r[10], "Fe3": r[11], "Fe4": r[12],
            "Fe5": r[13], "Fe6": r[14], "Fe7": r[15],
            "form_date": str(r[16])[:10] if r[16] else "",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


def _serve_attachment_response(file_url: str, file_name: str, *, as_attachment: bool):
    """Inline preview (as_attachment=False) or forced download (True)."""
    download_name = os.path.basename(file_name) or "download"
    mimetype, _ = mimetypes.guess_type(download_name)

    disk_path = resolve_attachment_disk_path(file_url)
    if disk_path:
        if not mimetype:
            mimetype, _ = mimetypes.guess_type(disk_path)
        return send_file(
            disk_path,
            mimetype=mimetype or "application/octet-stream",
            as_attachment=as_attachment,
            download_name=download_name or os.path.basename(disk_path),
            conditional=True,
        )

    if file_url.startswith(("http://", "https://")):
        return redirect(file_url)

    if file_url.startswith("/uploads/"):
        rel = file_url[len("/uploads/"):].lstrip("/")
        if as_attachment:
            return send_from_directory(
                UPLOAD_DIR,
                rel,
                as_attachment=True,
                download_name=download_name,
            )
        return send_from_directory(UPLOAD_DIR, rel, mimetype=mimetype)

    return None


@app.route("/api/users/list-emails")
@login_required
def api_users_list_emails():
    """All active users with name + email, for the email modal's recipient picker."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT USER_ID, USER_FULLNAME, USER_NAME, USER_EMAIL
                       FROM dbo.Sys_User
                       WHERE IsDeleted = 0
                         AND USER_EMAIL IS NOT NULL AND USER_EMAIL != ''
                       ORDER BY USER_FULLNAME
                       """)
        users = [{
            "user_id": r[0],
            "full_name": r[1] or r[2] or "",
            "username": r[2] or "",
            "email": r[3] or "",
        } for r in cursor.fetchall()]
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/users/list-all")
@login_required
def api_users_list_all():
    """All active users with name + email, for pickers where an email
    address isn't a requirement to be selected (e.g. the Workflow "Select
    User" / approver picker) — unlike list-emails above, this does NOT
    filter out users with no USER_EMAIL on file, since an approver should
    still be assignable even if they can't receive an email notification."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT USER_ID, USER_FULLNAME, USER_NAME, USER_EMAIL
                       FROM dbo.Sys_User
                       WHERE IsDeleted = 0
                       ORDER BY USER_FULLNAME
                       """)
        users = [{
            "user_id": r[0],
            "full_name": r[1] or r[2] or "",
            "username": r[2] or "",
            "email": r[3] or "",
        } for r in cursor.fetchall()]
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/documents/<int:doc_id>/qr", methods=["POST"])
@login_required
def api_generate_document_qr(doc_id):
    """
    Generates (or re-generates) a QR share link for a document. Gated by the
    Can_QR access right on the Inquiries page — admin decides per-user via
    the Control Panel. Returns a public URL; the frontend renders it as a
    QR code client-side (no image is generated server-side).
    """
    if not _check_accr(1, "Can_QR"):
        return jsonify({"error": "Access denied: you do not have permission to generate QR codes."}), 403
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT ID, Subject FROM dbo.Adco_Transactions WHERE ID = ? AND IsDeleted = 0", doc_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Document not found"}), 404

        token = _create_doc_qr_token(doc_id)
        qr_url = f"{request.host_url.rstrip('/')}/api/documents/qr/{token}"
        audit_log("QR_GENERATED", page_id=1, notes=f"Generated QR link for document ID {doc_id}")
        return jsonify({"qr_url": qr_url, "expires_days": QR_TOKEN_TTL_DAYS, "doc_id": doc_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/documents/qr/<token>")
def api_document_qr_view(token):
    """
    Public, mobile-friendly document view opened by scanning a QR code.
    Deliberately NOT behind @login_required — that's the point of a QR code
    you scan on your phone. The 256-bit token is the access control and
    expires after QR_TOKEN_TTL_DAYS.
    """
    tokens = _prune_expired_share_tokens(_load_share_tokens())
    info = tokens.get(token)
    if not info or "doc_id" not in info:
        return "<h3 style='font-family:sans-serif;padding:2rem'>This QR link is invalid or has expired.</h3>", 404
    _save_share_tokens(tokens)  # persist pruning

    doc_id = info["doc_id"]
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT t.ID, t.Subject, t.H_Date, t.Keywords, t.Detailes, t.Foldes_ID
                       FROM dbo.Adco_Transactions t
                       WHERE t.ID = ? AND t.IsDeleted = 0
                       """, doc_id)
        r = cursor.fetchone()
        if not r:
            return "<h3 style='font-family:sans-serif;padding:2rem'>Document not found.</h3>", 404

        folder_names = load_folder_names(cursor, [r[5]] if r[5] else [])
        folder_name = folder_names.get(r[5], "") if r[5] else ""
        attachments = load_attachments_for_transactions(cursor, [doc_id]).get(doc_id, [])

        # Give each attachment its own short-lived, public download link
        att_links = []
        for att in attachments:
            att_token = _create_share_token(att.get("id"), att.get("file_url") or "")
            att_url = f"{request.host_url.rstrip('/')}/api/documents/share/{att_token}"
            att_links.append((att.get("file_name") or "File", att_url))

        audit_log("QR_SCAN", page_id=1, notes=f"QR link opened for document ID {doc_id}", ip=_get_client_ip())

        att_html = "".join(
            f'<a class="att" href="{esc(url)}" target="_blank" rel="noopener">'
            f'<span>📄 {esc(name)}</span><span>Open ›</span></a>'
            for name, url in att_links
        ) or '<p class="muted">No attachments.</p>'

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(r[1] or 'Document')}</title>
<style>
  body {{ font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:#f1f5f9; margin:0; padding:0; color:#1e293b; }}
  .card {{ max-width:520px; margin:0 auto; background:#fff; min-height:100vh; }}
  .hdr {{ background:#0f172a; color:#fff; padding:20px 18px; }}
  .hdr h1 {{ font-size:18px; margin:0 0 4px; }}
  .hdr .reg {{ font-size:12px; opacity:.7; }}
  .body {{ padding:18px; }}
  .field {{ margin-bottom:14px; }}
  .field .lbl {{ font-size:11px; text-transform:uppercase; color:#64748b; letter-spacing:.03em; margin-bottom:3px; }}
  .field .val {{ font-size:14.5px; line-height:1.5; }}
  .atts {{ margin-top:6px; }}
  .att {{ display:flex; justify-content:space-between; align-items:center; padding:11px 12px; border:1px solid #e2e8f0; border-radius:8px; margin-bottom:8px; text-decoration:none; color:#1e293b; font-size:13.5px; }}
  .att:active {{ background:#f1f5f9; }}
  .muted {{ color:#94a3b8; font-size:13px; }}
  .foot {{ text-align:center; padding:16px; font-size:11px; color:#94a3b8; }}
</style></head>
<body>
  <div class="card">
    <div class="hdr">
      <h1>{esc(r[1] or 'Untitled document')}</h1>
      <div class="reg">Document #{doc_id}</div>
    </div>
    <div class="body">
      <div class="field"><div class="lbl">Date</div><div class="val">{esc(str(r[2] or '—'))}</div></div>
      <div class="field"><div class="lbl">Folder</div><div class="val">{esc(folder_name or '—')}</div></div>
      <div class="field"><div class="lbl">Keywords</div><div class="val">{esc(r[3] or '—')}</div></div>
      <div class="field"><div class="lbl">Notes</div><div class="val">{esc(r[4] or '—')}</div></div>
      <div class="field">
        <div class="lbl">Attachments</div>
        <div class="atts">{att_html}</div>
      </div>
    </div>
    <div class="foot">DocPortal Archiving System · Link expires after {QR_TOKEN_TTL_DAYS} days</div>
  </div>
</body></html>"""
        return html
    except Exception as e:
        return f"<h3 style='font-family:sans-serif;padding:2rem'>Error: {esc(str(e))}</h3>", 500
    finally:
        if conn:
            conn.close()


@app.route("/api/documents/share/<token>")
def api_document_share(token):
    """
    Public file link sent inside share emails. Deliberately NOT behind
    @login_required — external recipients need to open this without a
    system account. The 256-bit token is the access control; it expires
    after SHARE_TOKEN_TTL_DAYS (7 days by default).
    """
    tokens = _prune_expired_share_tokens(_load_share_tokens())
    info = tokens.get(token)
    if not info:
        return jsonify({"error": "This link is invalid or has expired."}), 404

    # Persist the pruned set so expired tokens don't accumulate forever.
    _save_share_tokens(tokens)

    disk_path = resolve_attachment_disk_path(info.get("file_url") or "")
    if not disk_path:
        return jsonify({"error": "File not found."}), 404

    mimetype, _ = mimetypes.guess_type(disk_path)
    audit_log("SHARE_DOWNLOAD", page_id=2,
              notes=f"Share link opened for attachment {info.get('att_id')}", ip=_get_client_ip())
    return send_file(
        disk_path,
        mimetype=mimetype or "application/octet-stream",
        as_attachment=False,
        download_name=os.path.basename(disk_path),
        conditional=True,
    )


@app.route("/api/email/send", methods=["POST"])
@login_required
def api_email_send():
    """
    Body JSON: {
      doc_id, recipients: [email, ...], subject?, body?,
      attachment_ids?: [id, ...], attach_file: bool
    }
    attachment_ids selects which attachments (from the picker checkboxes) to
    include — pass all of a transaction's attachment IDs to share every file.
    Omit / empty list to send a plain notification email with just the doc
    info and no files. (Legacy singular `attachment_id` is still accepted.)
    """
    data = request.get_json(silent=True) or {}
    doc_id = data.get("doc_id")
    recipients = [e.strip() for e in (data.get("recipients") or []) if e and str(e).strip()]
    subject_in = (data.get("subject") or "").strip()
    body_in = (data.get("body") or "").strip()
    attach_file = bool(data.get("attach_file"))

    attachment_ids = data.get("attachment_ids")
    if not attachment_ids and data.get("attachment_id"):
        attachment_ids = [data.get("attachment_id")]
    attachment_ids = [int(a) for a in (attachment_ids or []) if str(a).strip()]

    if not doc_id:
        return jsonify({"error": "doc_id is required"}), 400
    if not recipients:
        return jsonify({"error": "At least one recipient is required"}), 400

    result, status = _send_document_email_core(
        int(doc_id), recipients, subject_in=subject_in, body_in=body_in,
        attachment_ids=attachment_ids, attach_file=attach_file,
    )
    return jsonify(result), status


def _send_document_email_core(doc_id, recipients, subject_in="", body_in="",
                               attachment_ids=None, attach_file=False):
    """
    Core "email a document" logic, usable both from the /api/email/send route
    and from bulk-send flows (e.g. the chatbot's "email all documents in
    <folder>" intent). Resolves the current session user's sender identity
    (personal SMTP -> Graph -> shared mailbox, same priority as always),
    then builds and sends one email for the given document.

    Returns (dict, http_status) — same JSON shape api_email_send used to
    return directly, so callers can either jsonify() it for an HTTP response
    or just inspect result["success"] when calling in a loop.
    """
    attachment_ids = list(attachment_ids or [])

    # Prefer the sender's own mailbox (Settings -> Email) so mail goes out
    # under their real identity; fall back to the shared account otherwise.
    #
    # IMPORTANT: this decision is made PER USER, not globally. GRAPH_ENABLED
    # only means Graph is *available* as a fallback — it does NOT force
    # every user through Graph. A user who has personally saved their own
    # SMTP email+app password (e.g. a Gmail user, or anyone outside the
    # Graph tenant) always sends through their own SMTP config, regardless
    # of whether Graph is turned on for everyone else. Only users who have
    # NOT set up personal SMTP fall through to Graph (if enabled) or the
    # shared mailbox.
    sender_reply_to = None
    own_email, own_password, own_server, own_port, own_ssl = _get_user_email_config(session["user_id"])
    use_smtp_for_own = bool(own_email and own_password)
    use_graph = GRAPH_ENABLED and not use_smtp_for_own

    if use_graph:
        # Graph sends genuinely AS from_email using the app's own credentials —
        # no per-user password needed, so no Reply-To workaround either.
        graph_email = _get_user_send_email(session["user_id"])
        sender_email = graph_email or get_shared_from_email()
        own_password = None
        if not sender_email:
            return {
                "error": "Email is not configured. Set a Sender Email in Control Panel "
                         "\u2192 Mail Settings, or set your own email in Settings \u2192 Email."
            }, 500
    else:
        sender_email = own_email

        if not own_email:
            # No personal config -> shared account (Control Panel -> Mail
            # Settings, falling back to .env if nothing's been saved).
            _, _, shared_sender, shared_password, _ = get_shared_smtp_config()
            if not shared_sender or not shared_password:
                return {
                    "error": "Email is not configured. Set it up in Control Panel "
                             "\u2192 Mail Settings, or set your own email in Settings \u2192 Email."
                }, 500
            sender_email = shared_sender

            # Reply-To is set to the sender's own USER_EMAIL (only needed when
            # falling back to the shared account, since real per-user sending
            # already has From = their own address).
            conn_u = None
            try:
                conn_u = get_db_connection()
                cur_u = conn_u.cursor()
                cur_u.execute("SELECT USER_EMAIL FROM dbo.Sys_User WHERE USER_ID = ?", session["user_id"])
                row_u = cur_u.fetchone()
                sender_reply_to = row_u[0] if row_u and row_u[0] else None
            finally:
                if conn_u: conn_u.close()

    conn = None
    doc_subject = ""
    att_infos = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Subject FROM dbo.Adco_Transactions WHERE ID = ? AND IsDeleted = 0",
            int(doc_id),
        )
        row = cursor.fetchone()
        if not row:
            return {"error": "Document not found"}, 404
        doc_subject = row[0] or ""

        if attachment_ids:
            placeholders = ",".join("?" for _ in attachment_ids)
            cursor.execute(
                f"""
                SELECT ID, File_Name, File_URL
                FROM dbo.Adco_Transactions_Attachments
                WHERE ID IN ({placeholders}) AND Transaction_ID = ? AND {_attachments_active_sql()}
                """,
                *attachment_ids, int(doc_id),
            )
            att_infos = [{"id": r[0], "file_name": r[1], "file_url": r[2]} for r in cursor.fetchall()]
    except Exception as e:
        return {"error": str(e)}, 500
    finally:
        if conn: conn.close()

    subject = subject_in or f"{doc_subject} — Reg #{doc_id}"

    # Decide up front which attachments will be physically attached to the
    # email vs which need a share link — a file that's being attached for
    # real doesn't also need a link cluttering the body. A link is only
    # generated as a fallback: attach_file is off, or the file couldn't be
    # attached (missing on disk / pushes the message over the size cap).
    attach_ok_ids = set()
    skipped_too_large = []
    if attach_file and att_infos:
        total_bytes = 0
        for att in att_infos:
            disk_path = resolve_attachment_disk_path(att["file_url"])
            if not disk_path or not os.path.isfile(disk_path):
                continue
            size = os.path.getsize(disk_path)
            # Keep the whole message under a sane SMTP size (most servers,
            # incl. O365, reject well before 25MB once base64-encoded).
            if total_bytes + size > MAX_EMAIL_ATTACH_TOTAL_BYTES:
                skipped_too_large.append(att["file_name"] or os.path.basename(disk_path))
                continue
            total_bytes += size
            attach_ok_ids.add(att["id"])

    link_atts = att_infos if not attach_file else [a for a in att_infos if a["id"] not in attach_ok_ids]

    # request.host_url gives us the server's own address as seen by the
    # browser making this request — no need to hardcode a server IP.
    share_links = []  # [(file_name, url), ...]
    for att in link_atts:
        token = _create_share_token(att["id"], att["file_url"])
        url = f"{request.host_url.rstrip('/')}/api/documents/share/{token}"
        share_links.append((att["file_name"] or "File", url))

    body_lines = []
    if body_in:
        body_lines.append(body_in)
        body_lines.append("")
    body_lines.append(f"Document: {doc_subject}")
    body_lines.append(f"Registration number: {doc_id}")
    if attach_ok_ids:
        body_lines.append(f"({len(attach_ok_ids)} file(s) attached to this email)")
    if share_links:
        body_lines.append("")
        label = "File link" if len(share_links) == 1 else "File links"
        body_lines.append(f"{label} (valid {SHARE_TOKEN_TTL_DAYS} days):")
        for file_name, url in share_links:
            body_lines.append(f"- {file_name}: {url}")
    email_body = "\n".join(body_lines)

    # HTML alternative so the link renders as a real, clickable <a> tag
    # instead of relying on the mail client to auto-detect a plain-text URL.
    def _esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    html_parts = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">']
    if body_in:
        html_parts.append(f'<p style="white-space:pre-wrap;margin:0 0 12px">{_esc(body_in)}</p>')
    html_parts.append(f'<p style="margin:0 0 4px"><b>Document:</b> {_esc(doc_subject)}</p>')
    html_parts.append(f'<p style="margin:0 0 12px"><b>Registration number:</b> {_esc(str(doc_id))}</p>')
    if attach_ok_ids:
        html_parts.append(f'<p style="margin:0 0 12px">({len(attach_ok_ids)} file(s) attached to this email)</p>')
    if share_links:
        label = "File link" if len(share_links) == 1 else "File links"
        html_parts.append(f'<p style="margin:0 0 6px"><b>{label}</b> (valid {SHARE_TOKEN_TTL_DAYS} days):</p>')
        html_parts.append('<ul style="margin:0 0 12px;padding-inline-start:20px">')
        for file_name, url in share_links:
            html_parts.append(
                f'<li style="margin-bottom:4px">{_esc(file_name)}: '
                f'<a href="{_esc(url)}" style="color:#2563eb">{_esc(url)}</a></li>'
            )
        html_parts.append('</ul>')
    html_parts.append('</div>')
    email_html = "".join(html_parts)

    smtp_attachments = []
    if attach_ok_ids:
        for att in att_infos:
            if att["id"] not in attach_ok_ids:
                continue
            disk_path = resolve_attachment_disk_path(att["file_url"])
            ctype, _ = mimetypes.guess_type(disk_path)
            with open(disk_path, "rb") as f:
                smtp_attachments.append({
                    "filename": att["file_name"] or os.path.basename(disk_path),
                    "content_type": ctype or "application/octet-stream",
                    "content_bytes": f.read(),
                })

    try:
        if use_graph:
            _send_graph_mail(
                subject=subject,
                html_body=email_html,
                recipients=recipients,
                from_email=sender_email,
                reply_to=sender_reply_to,
                attachments=smtp_attachments,
            )
        else:
            _send_smtp_mail(
                subject=subject,
                text_body=email_body,
                html_body=email_html,
                recipients=recipients,
                reply_to=sender_reply_to,
                attachments=smtp_attachments,
                from_email=own_email,
                from_password=own_password,
                # Each user's own SMTP host — NOT the admin's shared server.
                # Without this, a personal Gmail login would get tried
                # against smtp.office365.com (or vice versa) and always fail.
                smtp_server=own_server,
                smtp_port=own_port,
                use_ssl=own_ssl,
            )
    except smtplib.SMTPAuthenticationError:
        if own_email:
            return {
                "error": "Your personal email login failed — the password saved in "
                         "Settings \u2192 Email may be wrong or expired. Please update it."
            }, 502
        return {
            "error": "SMTP login failed — the mailbox password may have changed or expired. "
                     "Check SMTP_USERNAME/SMTP_PASSWORD in .env."
        }, 502
    except PermissionError as exc:
        return {"error": str(exc)}, 502
    except (smtplib.SMTPException, OSError) as exc:
        return {"error": f"Failed to send email: {exc}"}, 502
    except Exception as exc:
        return {"error": f"Failed to send email: {exc}"}, 502

    audit_log("EMAIL_SENT", page_id=2,
              notes=f"Emailed document {doc_id} to {', '.join(recipients)} "
                    f"{'as ' + (sender_email or '') if sender_email else '(shared account)'} "
                    f"({len(att_infos)} attachment(s), attach_file={attach_file})")
    return {
        "success": True,
        "doc_id": doc_id,
        "doc_subject": doc_subject,
        "share_links": [{"file_name": n, "url": u} for n, u in share_links],
        "skipped_too_large": skipped_too_large,
    }, 200


@app.route("/api/attachments/<int:attachment_id>/rename", methods=["POST"])
@login_required
def api_rename_attachment(attachment_id):
    """Rename an already-saved attachment (edit mode). Only changes the
    display/download name (File_Name) — the file on disk and its extension
    are left untouched, same as renaming a not-yet-uploaded file."""
    if not _check_accr(1, "Can_Edit"):
        return jsonify({"error": "Access denied: you do not have permission to edit transactions."}), 403
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        new_name = (data.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "Name is required"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT File_Name FROM dbo.Adco_Transactions_Attachments
                WHERE ID = ? AND {_attachments_active_sql()}""",
            attachment_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404

        old_name = row[0] or ""
        # Keep the original extension so the saved file stays openable, even
        # if the caller's new name omits or changes it.
        old_ext = os.path.splitext(old_name)[1]
        new_stem, new_ext = os.path.splitext(new_name)
        final_name = f"{new_stem}{new_ext or old_ext}"

        cursor.execute(
            """UPDATE dbo.Adco_Transactions_Attachments
               SET File_Name = ? WHERE ID = ?""",
            final_name, attachment_id,
        )
        conn.commit()
        audit_log("EDIT", page_id=1, notes=f"Renamed attachment ID {attachment_id}: \"{old_name}\" -> \"{final_name}\"")
        return jsonify({"success": True, "name": final_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/attachments/<int:attachment_id>/preview")
@login_required
def api_attachment_preview(attachment_id):
    """Serve attachment inline for iframe/image preview (no download prompt)."""
    audit_log("PREVIEW", page_id=1, notes=f"Previewed attachment ID {attachment_id}")
    return _api_attachment_serve(attachment_id, as_attachment=False)


@app.route("/api/attachments/<int:attachment_id>/download")
@login_required
def api_attachment_download(attachment_id):
    """Serve attachment as a download (Content-Disposition: attachment)."""
    audit_log("DOWNLOAD", page_id=1, notes=f"Downloaded attachment ID {attachment_id}")
    return _api_attachment_serve(attachment_id, as_attachment=True)


@app.route("/api/attachments/<int:attachment_id>/share-link", methods=["POST"])
@login_required
def api_attachment_share_link(attachment_id):
    """
    Generate a 7-day share link for a single attachment, for the "Copy Link"
    button in the document viewer. Same token store as the email feature —
    just skips sending an email.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT ID, File_Name, File_URL
            FROM dbo.Adco_Transactions_Attachments
            WHERE ID = ? AND {_attachments_active_sql()}
            """,
            attachment_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404
        file_url = row[2]
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

    token = _create_share_token(attachment_id, file_url)
    share_link = f"{request.host_url.rstrip('/')}/api/documents/share/{token}"
    audit_log("SHARE_LINK_CREATED", page_id=1,
              notes=f"Share link created for attachment {attachment_id}")
    return jsonify({"share_link": share_link, "expires_days": SHARE_TOKEN_TTL_DAYS})


def _api_attachment_serve(attachment_id: int, *, as_attachment: bool):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT File_URL, File_Name
            FROM dbo.Adco_Transactions_Attachments
            WHERE ID = ? AND {_attachments_active_sql()}
            """,
            attachment_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404

        file_url, file_name = (row[0] or "").strip(), row[1] or "download"
        response = _serve_attachment_response(file_url, file_name, as_attachment=as_attachment)
        if response is not None:
            return response

        return jsonify({
            "error": "File not found on server",
            "hint": "Set ATTACHMENT_FILES_ROOT in .env if files live on a network share",
            "file_url": file_url,
        }), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ── Document signing (draw/type a signature, stamp it onto a PDF) ──────────
# No new table: the stamped PDF replaces the attachment's file on disk in
# place, and the event is recorded through the existing audit_log() /
# Sys_AuditLog, same as PREVIEW/DOWNLOAD/EDIT.
_SIGNATURE_WIDTH_PT = 160
_SIGNATURE_HEIGHT_PT = 60
_SIGNATURE_MARGIN_PT = 36
_SIGNATURE_VALID_POSITIONS = {"bottom-right", "bottom-left", "top-right", "top-left"}


def _signature_position_to_xy(position: str, page_w: float, page_h: float):
    if position == "bottom-left":
        return _SIGNATURE_MARGIN_PT, _SIGNATURE_MARGIN_PT
    if position == "top-right":
        return (page_w - _SIGNATURE_WIDTH_PT - _SIGNATURE_MARGIN_PT,
                page_h - _SIGNATURE_HEIGHT_PT - _SIGNATURE_MARGIN_PT)
    if position == "top-left":
        return _SIGNATURE_MARGIN_PT, page_h - _SIGNATURE_HEIGHT_PT - _SIGNATURE_MARGIN_PT
    return (page_w - _SIGNATURE_WIDTH_PT - _SIGNATURE_MARGIN_PT, _SIGNATURE_MARGIN_PT)


def _signature_pct_to_xy(x_pct: float, y_pct: float, page_w: float, page_h: float):
    """x_pct/y_pct are the signature box's top-left corner as a fraction
    (0..1) of the page, with y measured from the TOP (matching how it's
    captured from an on-screen drag over a page preview image). PDF
    coordinates put the origin at the bottom-left, so y is flipped here."""
    x_pct = min(max(x_pct, 0.0), 1.0)
    y_pct = min(max(y_pct, 0.0), 1.0)
    x = x_pct * page_w
    y = page_h - (y_pct * page_h) - _SIGNATURE_HEIGHT_PT
    x = min(max(x, 0.0), max(page_w - _SIGNATURE_WIDTH_PT, 0.0))
    y = min(max(y, 0.0), max(page_h - _SIGNATURE_HEIGHT_PT, 0.0))
    return x, y


def _stamp_signature_on_pdf(source_pdf_path, signature_png_bytes, output_pdf_path,
                             *, page_number=-1, position="bottom-right",
                             x_pct=None, y_pct=None):
    """Overlay a signature PNG onto one page of a PDF, writing the result to
    output_pdf_path (may be the same path as source_pdf_path). If x_pct/
    y_pct are given (manual drag-to-place), they take priority over the
    `position` preset."""
    if PdfReader is None or PdfWriter is None or _sig_rl_canvas is None:
        raise RuntimeError("pypdf and reportlab are required for signing (pip install pypdf reportlab)")
    if x_pct is None and position not in _SIGNATURE_VALID_POSITIONS:
        raise ValueError(f"Invalid position: {position}")

    reader = PdfReader(source_pdf_path)
    num_pages = len(reader.pages)
    if num_pages == 0:
        raise ValueError("PDF has no pages")

    target_index = num_pages - 1 if page_number in (-1, None) else page_number - 1
    if not (0 <= target_index < num_pages):
        raise ValueError(f"page_number out of range (PDF has {num_pages} pages)")

    target_page = reader.pages[target_index]
    page_w = float(target_page.mediabox.width)
    page_h = float(target_page.mediabox.height)
    if x_pct is not None and y_pct is not None:
        x, y = _signature_pct_to_xy(float(x_pct), float(y_pct), page_w, page_h)
    else:
        x, y = _signature_position_to_xy(position, page_w, page_h)

    overlay_buf = io.BytesIO()
    c = _sig_rl_canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))
    img = _sig_ImageReader(io.BytesIO(signature_png_bytes))
    c.drawImage(img, x, y, width=_SIGNATURE_WIDTH_PT, height=_SIGNATURE_HEIGHT_PT,
                preserveAspectRatio=True, mask="auto")
    c.save()
    overlay_buf.seek(0)
    overlay_page = PdfReader(overlay_buf).pages[0]

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == target_index:
            page.merge_page(overlay_page)
        writer.add_page(page)

    # Write to a temp file first, then swap in — avoids corrupting the
    # original if writing fails partway through.
    tmp_path = output_pdf_path + ".tmp"
    with open(tmp_path, "wb") as f:
        writer.write(f)
    os.replace(tmp_path, output_pdf_path)


def _decode_signature_png(data_url_or_b64: str) -> bytes:
    if "," in data_url_or_b64 and data_url_or_b64.strip().lower().startswith("data:"):
        data_url_or_b64 = data_url_or_b64.split(",", 1)[1]
    return base64.b64decode(data_url_or_b64)


@app.route("/api/attachments/<int:attachment_id>/sign", methods=["POST"])
@login_required
def api_sign_attachment(attachment_id):
    """Stamp a drawn/typed signature onto a PDF attachment. Overwrites the
    attachment's file in place (same File_URL row, same file on disk) —
    no new table, no new file to track."""
    if not _check_accr(1, "Can_Edit"):
        return jsonify({"error": "Access denied: you do not have permission to edit transactions."}), 403

    data = request.get_json(silent=True) or {}
    signature_data_url = data.get("signature")
    if not signature_data_url:
        return jsonify({"error": "Missing signature image"}), 400

    position = (data.get("position") or "bottom-right").strip()

    x_pct, y_pct = data.get("x_pct"), data.get("y_pct")
    if x_pct is not None or y_pct is not None:
        try:
            x_pct = float(x_pct)
            y_pct = float(y_pct)
        except (TypeError, ValueError):
            return jsonify({"error": "x_pct/y_pct must be numbers"}), 400
        if not (0.0 <= x_pct <= 1.0 and 0.0 <= y_pct <= 1.0):
            return jsonify({"error": "x_pct/y_pct must be between 0 and 1"}), 400
    elif position not in _SIGNATURE_VALID_POSITIONS:
        return jsonify({"error": f"Invalid position. Use one of: {sorted(_SIGNATURE_VALID_POSITIONS)}"}), 400

    try:
        page_number = int(data.get("page_number", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "page_number must be an integer"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT File_URL, File_Name
                FROM dbo.Adco_Transactions_Attachments
                WHERE ID = ? AND {_attachments_active_sql()}""",
            attachment_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404

        file_url, file_name = row[0], row[1] or "document.pdf"
        if not file_name.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF attachments can be signed"}), 400

        source_path = resolve_attachment_disk_path(file_url)
        if not source_path or not os.path.isfile(source_path):
            return jsonify({"error": "Original file not found on server"}), 404

        try:
            signature_png = _decode_signature_png(signature_data_url)
        except Exception:
            return jsonify({"error": "Could not decode signature image"}), 400

        # Keep exactly one pre-signature backup per file, so "remove signature"
        # can restore it later. Only made the *first* time this file is
        # signed — re-signing an already-signed file must not overwrite the
        # backup with an already-stamped copy.
        backup_path = source_path + ".unsigned.bak"
        if not os.path.isfile(backup_path):
            import shutil
            shutil.copy2(source_path, backup_path)

        try:
            _stamp_signature_on_pdf(
                source_path, signature_png, source_path,
                page_number=page_number, position=position,
                x_pct=x_pct, y_pct=y_pct,
            )
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except RuntimeError as re_:
            return jsonify({"error": str(re_)}), 500

        placement_desc = f"custom {x_pct:.2f},{y_pct:.2f}" if x_pct is not None else position
        audit_log(
            "SIGN", page_id=1,
            notes=f"Signed attachment ID {attachment_id} \"{file_name}\" ({placement_desc}, page {page_number})",
        )
        return jsonify({"success": True, "attachment_id": attachment_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/attachments/<int:attachment_id>/replace-pdf", methods=["POST"])
@login_required
def api_replace_attachment_pdf(attachment_id):
    """
    Overwrite a PDF attachment's file in place with a new PDF built client-side
    (Manage Pages: add/remove pages). Mirrors /sign — same File_URL row, same
    file on disk, no new table. Unlike /sign there's no undo backup here: page
    additions/removals are a deliberate content change, not a reversible stamp.
    """
    if not _check_accr(1, "Can_Edit"):
        return jsonify({"error": "Access denied: you do not have permission to edit transactions."}), 403

    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"error": "Missing file"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT File_URL, File_Name
                FROM dbo.Adco_Transactions_Attachments
                WHERE ID = ? AND {_attachments_active_sql()}""",
            attachment_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404

        file_url, file_name = row[0], row[1] or "document.pdf"
        if not file_name.lower().endswith(".pdf"):
            return jsonify({"error": "Only PDF attachments support page editing"}), 400

        target_path = resolve_attachment_disk_path(file_url)
        if not target_path or not os.path.isfile(target_path):
            return jsonify({"error": "Original file not found on server"}), 404

        tmp_path = target_path + ".tmp"
        upload.save(tmp_path)

        # Validate it's actually a readable, non-empty PDF before committing —
        # never let a corrupt upload clobber the archived original.
        if PdfReader is not None:
            try:
                reader = PdfReader(tmp_path)
                page_count = len(reader.pages)
                if page_count == 0:
                    raise ValueError("PDF has no pages")
            except Exception:
                os.remove(tmp_path)
                return jsonify({"error": "The rebuilt PDF is invalid or empty"}), 400
        else:
            page_count = None

        os.replace(tmp_path, target_path)
        new_size = os.path.getsize(target_path)

        cursor.execute(
            "UPDATE dbo.Adco_Transactions_Attachments SET File_Size = ? WHERE ID = ?",
            new_size, attachment_id,
        )
        conn.commit()

        audit_log(
            "EDIT_PAGES", page_id=1,
            notes=f"Edited pages of attachment ID {attachment_id} \"{file_name}\""
                  + (f" ({page_count} pages)" if page_count is not None else ""),
        )
        return jsonify({"success": True, "attachment_id": attachment_id,
                         "file_size": new_size, "page_count": page_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/attachments/<int:attachment_id>/unsign", methods=["POST"])
@login_required
def api_unsign_attachment(attachment_id):
    """Remove a previously-stamped signature by restoring the pre-signature
    backup saved the first time this attachment was signed. No DB table —
    the backup is just a sibling ".unsigned.bak" file next to the original."""
    if not _check_accr(1, "Can_Edit"):
        return jsonify({"error": "Access denied: you do not have permission to edit transactions."}), 403

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT File_URL, File_Name
                FROM dbo.Adco_Transactions_Attachments
                WHERE ID = ? AND {_attachments_active_sql()}""",
            attachment_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404

        file_url, file_name = row[0], row[1] or "document.pdf"
        source_path = resolve_attachment_disk_path(file_url)
        if not source_path or not os.path.isfile(source_path):
            return jsonify({"error": "Original file not found on server"}), 404

        backup_path = source_path + ".unsigned.bak"
        if not os.path.isfile(backup_path):
            return jsonify({"error": "This document has no signature to remove"}), 400

        tmp_path = source_path + ".tmp"
        import shutil
        shutil.copy2(backup_path, tmp_path)
        os.replace(tmp_path, source_path)
        os.remove(backup_path)

        audit_log(
            "UNSIGN", page_id=1,
            notes=f"Removed signature from attachment ID {attachment_id} \"{file_name}\"",
        )
        return jsonify({"success": True, "attachment_id": attachment_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# Hard cap on how many documents a single bulk zip can pull attachments from —
# protects the server from someone asking for "all documents ever" in one go.
BULK_ZIP_MAX_DOCS = 200


@app.route("/api/documents/bulk-zip")
@login_required
# NOTE (audit, verified): has no direct fetch()/link in dashboard.js — it's
# reached indirectly via url_for("api_documents_bulk_zip", ...) built inside
# the chatbot handler (see _CHATBOT_SINGLE_DOC_ZIP_TRIGGERS /
# _chatbot_parse_bulk_zip_folder), which returns {"action": "download_zip",
# "url": ...} that dashboard.js's chatbot response handler follows
# (search "download_zip" in dashboard.js). Confirmed live, not dead code.
def api_documents_bulk_zip():
    """
    Streams a single .zip of every attachment across a set of documents.
    Query params (one of the two selection modes is required):
      - folder_id=<id>        : every document in that folder (access-checked,
                                 same rules as normal search)
      - doc_ids=1,2,3         : an explicit comma-separated list of document IDs
    Files inside the zip are named "<doc_id>/<original filename>" so
    attachments from different documents never collide.
    """
    folder_id = request.args.get("folder_id")
    doc_ids_raw = (request.args.get("doc_ids") or "").strip()

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if doc_ids_raw:
            try:
                doc_ids = [int(x) for x in doc_ids_raw.split(",") if x.strip()]
            except ValueError:
                return jsonify({"error": "doc_ids must be a comma-separated list of integers"}), 400
            # Access control: re-run search scoped to exactly these IDs so the
            # same row-level access rules apply as anywhere else in the app.
            access_where, access_params = _current_user_doc_access_clause(cursor, "t")
            placeholders = ",".join("?" for _ in doc_ids)
            cursor.execute(
                f"""SELECT t.ID FROM dbo.Adco_Transactions t
                    WHERE t.IsDeleted = 0 AND t.ID IN ({placeholders}) AND {access_where}""",
                *doc_ids, *access_params,
            )
            allowed_ids = [r[0] for r in cursor.fetchall()]
        elif folder_id:
            try:
                folder_id = int(folder_id)
            except ValueError:
                return jsonify({"error": "folder_id must be an integer"}), 400
            total, results = run_document_search(cursor, folder_id=folder_id, page=1, page_size=BULK_ZIP_MAX_DOCS)
            allowed_ids = [r["id"] for r in results]
        else:
            return jsonify({"error": "Provide folder_id or doc_ids"}), 400

        if not allowed_ids:
            return jsonify({"error": "No accessible documents found for that selection"}), 404
        if len(allowed_ids) > BULK_ZIP_MAX_DOCS:
            allowed_ids = allowed_ids[:BULK_ZIP_MAX_DOCS]

        attachments_by_tx = load_attachments_for_transactions(cursor, allowed_ids)

        buf = io.BytesIO()
        file_count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for doc_id, atts in attachments_by_tx.items():
                for att in atts:
                    disk_path = resolve_attachment_disk_path(att["file_url"])
                    if not disk_path or not os.path.isfile(disk_path):
                        continue
                    arcname = f"{doc_id}/{att['file_name'] or os.path.basename(disk_path)}"
                    zf.write(disk_path, arcname)
                    file_count += 1

        if file_count == 0:
            return jsonify({"error": "No files could be found on disk for the selected documents"}), 404

        buf.seek(0)
        audit_log("BULK_DOWNLOAD", page_id=1,
                  notes=f"Bulk zip: {len(allowed_ids)} document(s), {file_count} file(s)"
                        + (f", folder_id={folder_id}" if folder_id else ""))
        zip_name = f"documents_{folder_id}.zip" if folder_id else "documents.zip"
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=zip_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/uploads/<path:filename>")
@login_required
def serve_uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# ── API: Scanner / folder-level upload (Task 8) ───────────────────────────
@app.route("/api/folders/<int:folder_id>/scan", methods=["POST"])
@login_required
def api_folder_scan_upload(folder_id):
    """
    Upload one or more files directly into a folder (scanner workflow).
    Creates a transaction row to hold the files so they appear in search.
    Architecture is ready for physical scanner integration.
    """
    conn = None
    try:
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return jsonify({"error": "No files provided"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        ncol = adco_folder_name_col(cursor)
        dcol = adco_folder_dept_col(cursor)
        dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
        cursor.execute(
            f"SELECT ID, [{ncol}], {dept_bracket} FROM dbo.Adco_Folder WHERE ID = ? AND IsDeleted = 0",
            folder_id,
        )
        folder_row = cursor.fetchone()
        if not folder_row:
            return jsonify({"error": "Folder not found"}), 404

        folder_name = folder_row[1] or f"Folder {folder_id}"
        # Task 6A: To_Dep_ID — use folder's own Dept_ID (already in the row).
        # If the Adco_Folder row has no Dept_ID, fall back to user's dep_id.
        dept_id = folder_row[2] or session.get("dep_id")
        today_str = datetime.now().strftime("%Y/%m/%d")
        g_date = parse_hijri_date_string(today_str)
        subject_in = (request.form.get("subject") or "").strip()
        keywords_in = (request.form.get("keywords") or "").strip()
        notes_in = (request.form.get("notes") or request.form.get("statement") or "").strip()
        subject = subject_in or f"Scanned document — {folder_name} — {today_str}"
        merge_pdf = request.form.get("merge_to_pdf", "0") in ("1", "true", "yes")

        # Task 6C: Is_Need_Reply is always 0
        cursor.execute("""
                       INSERT INTO dbo.Adco_Transactions
                       (Type_ID, Cat_ID, H_Date, G_Date,
                        Importance_Degree_ID, Secret_Degree_ID,
                        Subject, Foldes_ID, From_Dep_ID, To_Dep_ID,
                        CreatedBy, CreatedOn, IsDeleted, Status_ID, Is_Need_Reply)
                       VALUES (1, 1, ?, ?, 1, 1, ?, ?, ?, ?, ?, GETDATE(), 0, 1, 0)
                       """, today_str, g_date, subject,
                       folder_id, session.get("dep_id"), dept_id, session["user_id"])

        cursor.execute("""
                       SELECT MAX(ID)
                       FROM dbo.Adco_Transactions
                       WHERE CreatedBy = ?
                         AND IsDeleted = 0
                         AND CreatedOn >= DATEADD(SECOND, -5, GETDATE())
                       """, session["user_id"])
        row = cursor.fetchone()
        tx_id = row[0] if row and row[0] else None
        if not tx_id:
            raise RuntimeError("Could not create transaction for scanned files")

        if keywords_in or notes_in:
            cursor.execute(
                "UPDATE dbo.Adco_Transactions SET Keywords=?, Detailes=? WHERE ID=?",
                keywords_in, notes_in, tx_id,
            )

        # Task 5: enforce and validate Delivery_Method_ID == 1
        _ensure_delivery_method_sync(cursor, tx_id)

        files_to_save = _prepare_scan_upload_files(files, merge_pdf=merge_pdf)
        saved = []
        for idx, uf in enumerate(files_to_save):
            original_name = uf.filename or "scan"
            base_name, ext = os.path.splitext(original_name)
            ext = ext or ".bin"
            file_ext = ext.lstrip(".").lower()
            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
            temp_name = f"_tmp_scan_{tx_id}_{ts}_{idx}{ext}"
            temp_path = os.path.join(FILE_SAVE_DIR, temp_name)
            uf.save(temp_path)
            file_size = os.path.getsize(temp_path)

            cursor.execute("""
                           INSERT INTO dbo.Adco_Transactions_Attachments
                           (Transaction_ID, File_Name, File_Description, File_URL,
                            File_Size, File_Type_ID, CreatedBy, CreatedOn, IsDeleted)
                           VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 0)
                           """, tx_id, uf.filename, "Scanned document",
                           temp_path, file_size, file_ext, session["user_id"])

            cursor.execute("""
                           SELECT MAX(ID) FROM dbo.Adco_Transactions_Attachments
                           WHERE Transaction_ID = ? AND IsDeleted = 0
                             AND CreatedOn >= DATEADD(SECOND, -5, GETDATE())
                           """, tx_id)
            att_row = cursor.fetchone()
            att_id = att_row[0] if att_row and att_row[0] else ts

            # base_name comes from the client's original filename — sanitize
            # before using it to build a disk path (path traversal guard).
            # The real/original name is preserved separately in File_Name.
            safe_base_name = _safe_filename_stem(base_name)
            final_name = f"{safe_base_name}.{att_id}{ext}"
            final_path = os.path.join(FILE_SAVE_DIR, final_name)
            os.rename(temp_path, final_path)

            cursor.execute("""
                           UPDATE dbo.Adco_Transactions_Attachments
                           SET File_URL = ? WHERE ID = ?
                           """, final_name, att_id)
            saved.append({"file_name": uf.filename, "file_url": final_name, "file_size": file_size})

        conn.commit()
        return jsonify({"success": True, "transaction_id": tx_id,
                        "folder_id": folder_id, "files_saved": len(saved), "files": saved})
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


def _is_image_upload(uf) -> bool:
    name = (uf.filename or "").lower()
    ctype = (getattr(uf, "content_type", None) or "").lower()
    return (
            ctype.startswith("image/")
            or name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"))
    )


def _prepare_scan_upload_files(files, *, merge_pdf: bool):
    """Optionally merge multiple image uploads into one PDF (typical scan batch)."""
    if not merge_pdf or len(files) < 2 or not all(_is_image_upload(f) for f in files):
        return files
    try:
        from PIL import Image
    except ImportError:
        return files

    images = []
    for uf in files:
        uf.stream.seek(0)
        img = Image.open(uf.stream)
        if img.mode != "RGB":
            img = img.convert("RGB")
        images.append(img)
    if not images:
        return files

    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    buf.seek(0)
    merged_name = f"scan_{int(datetime.now(timezone.utc).timestamp() * 1000)}.pdf"
    return [
        FileStorage(stream=buf, filename=merged_name, content_type="application/pdf")
    ]


def _probe_escl_scanner(ip: str):
    """Return (scheme, port, make_model) for an eSCL device, or None."""
    if not _requests or not ip:
        return None
    for scheme, port in (("http", 80), ("http", 8080), ("https", 443), ("https", 8443)):
        try:
            url = f"{scheme}://{ip}:{port}/eSCL/ScannerCapabilities"
            r = _requests.get(url, timeout=4, verify=False)
            if r.status_code != 200:
                continue
            name = ip
            try:
                root = ET.fromstring(r.text)
                ns = {"pwg": "http://www.pwg.org/schemas/2010/12/sm"}
                mm = root.find(".//pwg:MakeAndModel", ns)
                if mm is not None and mm.text:
                    name = mm.text.strip()
            except Exception:
                pass
            return scheme, port, name
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════
# eSCL NETWORK SCANNER  (RFC-compliant, works with Canon/Ricoh/HP/Xerox etc.)
# Set SCANNER_IP in your .env or environment, e.g. SCANNER_IP=192.168.1.50
# eSCL default path: http://<ip>/eSCL/   (some Ricoh: /eSCL/  same)
# ═══════════════════════════════════════════════════════════════════════════

SCANNER_IP = os.environ.get("SCANNER_IP", "")  # e.g. 192.168.1.50
SCANNER_PORT = int(os.environ.get("SCANNER_PORT", "80"))  # 443 for HTTPS
SCANNER_HTTPS = os.environ.get("SCANNER_HTTPS", "0") == "1"
SCANNER_BASE = (
    f"{'https' if SCANNER_HTTPS else 'http'}://{SCANNER_IP}:{SCANNER_PORT}/eSCL"
    if SCANNER_IP else ""
)


@app.route("/api/scanner/test", methods=["GET"])
@login_required
def api_scanner_test():
    """Test connectivity to a scanner IP sent as ?ip= query param."""
    if not _requests:
        return jsonify({"ok": False, "error": "requests library not installed — run: pip install requests"}), 500
    ip = request.args.get("ip", "").strip() or SCANNER_IP
    if not ip:
        return jsonify({"ok": False, "error": "No IP provided"}), 400
    found = _probe_escl_scanner(ip)
    if found:
        scheme, port, name = found
        return jsonify({"ok": True, "name": name, "scheme": scheme, "port": port})
    return jsonify({"ok": False, "error": f"No eSCL scanner found at {ip} (tried ports 80, 8080, 443, 8443)"}), 200


@app.route("/api/scanner/discover", methods=["GET"])
@login_required
def api_scanner_discover():
    """
    Probe the local /24 subnet for eSCL-capable scanners.
    Returns a list of discovered scanners: { ip, name, scheme, port }.
    Uses multi-threaded scanning with a short timeout for speed.
    """
    import socket
    import threading

    if not _requests:
        return jsonify({"scanners": [], "error": "requests library not installed"}), 500

    # Determine the server's own IP to derive the subnet
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "192.168.1.1"

    # Collect all subnets to scan: server's own + scanner's subnet from env
    prefixes = set()
    prefixes.add(".".join(local_ip.split(".")[:3]))  # server's own subnet
    if SCANNER_IP:
        prefixes.add(".".join(SCANNER_IP.split(".")[:3]))  # scanner's subnet e.g. 192.168.8

    found = []
    lock = threading.Lock()

    # ALL of these must be present — a switch/router cannot fake all of them
    ESCL_REQUIRED = (
        "ScannerCapabilities",
        "MakeAndModel",
        "ColorModes",
    )
    # At least one of these namespaces must also be present
    ESCL_NAMESPACES = (
        "schemas.hp.com/imaging/escl",
        "www.pwg.org/schemas",
    )

    def _is_escl_response(text):
        """Return True only if the HTTP body looks like a genuine eSCL capabilities document."""
        if not text or "<" not in text:
            return False
        # Must contain ALL required tags
        if not all(sig in text for sig in ESCL_REQUIRED):
            return False
        # Must contain at least one known eSCL namespace
        if not any(ns in text for ns in ESCL_NAMESPACES):
            return False
        return True

    def probe(host):
        for scheme, port in (("http", 80), ("http", 8080), ("https", 443), ("https", 8443)):
            try:
                url = f"{scheme}://{host}:{port}/eSCL/ScannerCapabilities"
                r = _requests.get(url, timeout=0.8, verify=False)
                if r.status_code == 200 and _is_escl_response(r.text):
                    name = host
                    try:
                        root = ET.fromstring(r.text)
                        ns = {"pwg": "http://www.pwg.org/schemas/2010/12/sm"}
                        mm = root.find(".//pwg:MakeAndModel", ns)
                        if mm is not None and mm.text:
                            name = mm.text.strip()
                    except Exception:
                        pass
                    with lock:
                        found.append({"ip": host, "name": name, "scheme": scheme, "port": port})
                    return
            except Exception:
                continue

    # Scan all subnets + add any known scanner IPs from env explicitly
    hosts = list({f"{p}.{i}" for p in prefixes for i in range(1, 255)})
    # Always include the configured scanner IPs so they're never missed
    for known_ip in [SCANNER_IP]:
        if known_ip and known_ip not in hosts:
            hosts.append(known_ip)

    threads = [threading.Thread(target=probe, args=(host,), daemon=True) for host in hosts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)  # wait max 6s — 2 subnets need more time

    return jsonify({"scanners": found, "local_ip": local_ip})


@app.route("/api/scanner/status", methods=["GET"])
@login_required
def api_scanner_status():
    """Check whether the configured scanner is reachable and return its capabilities."""
    if not SCANNER_IP:
        return jsonify({"configured": False,
                        "error": "SCANNER_IP not configured on the server"}), 200
    if not _requests:
        return jsonify({"configured": True, "error": "requests library not installed on server"}), 500
    try:
        r = _requests.get(f"{SCANNER_BASE}/ScannerCapabilities",
                          timeout=4, verify=False)
        r.raise_for_status()
        # Parse a minimal subset of the eSCL XML capabilities
        caps = _parse_escl_capabilities(r.text)
        return jsonify({"configured": True, "reachable": True,
                        "scanner_ip": SCANNER_IP, "capabilities": caps})
    except Exception as e:
        return jsonify({"configured": True, "reachable": False,
                        "scanner_ip": SCANNER_IP, "error": str(e)}), 200


# ── eSCL scanner capabilities cache ──────────────────────────────────────
# ScannerCapabilities almost never changes between scans of the same
# device, but /api/scanner/scan was re-fetching it over the network on
# every single call — including every page of a multi-page scan session
# (the "Add Page" flow in the UI). That's one extra HTTP round-trip to the
# printer plus a fixed settle delay, repeated per page. Cache it briefly.
_ESCL_CAPS_CACHE: dict[tuple, tuple] = {}
_ESCL_CAPS_TTL_SECONDS = 300  # 5 minutes — plenty for a scan session


def _get_escl_capabilities(scheme: str, ip: str, port: int):
    """
    Return (capabilities_dict, was_freshly_fetched) for a scanner, using a
    short-lived in-memory cache keyed by (scheme, ip, port). Avoids hitting
    the physical device again for every page of a multi-page scan.
    """
    import time as _time_mod
    key = (scheme, ip, port)
    cached = _ESCL_CAPS_CACHE.get(key)
    now = _time_mod.time()
    if cached and (now - cached[1]) < _ESCL_CAPS_TTL_SECONDS:
        return cached[0], False

    caps = {}
    try:
        caps_r = _requests.get(
            f"{scheme}://{ip}:{port}/eSCL/ScannerCapabilities",
            timeout=6, verify=False
        )
        if caps_r.status_code == 200:
            caps = _parse_escl_capabilities(caps_r.text)
    except Exception:
        pass  # proceed with user-supplied values if caps fetch fails

    if caps:
        _ESCL_CAPS_CACHE[key] = (caps, now)
    return caps, True


@app.route("/api/scanner/scan", methods=["POST"])
@login_required
def api_scanner_scan():
    """
    Trigger an eSCL scan job and return the scanned document bytes (PDF/JPEG/PNG).
    Body JSON: { ip, scheme?, port?, color, dpi, format, source }

    Strategy:
      1. Probe scanner to get scheme/port.
      2. Fetch ScannerCapabilities and validate/clamp all requested settings
         against what the device actually declares.
      3. Build a minimal ScanSettings XML (no ScanRegion — avoids 409 on
         Kyocera TASKalfa and most other vendors).
      4. POST to /eSCL/ScanJobs, then GET /NextDocument.
    """
    if not _requests:
        return jsonify({"error": "requests library not installed — run: pip install requests"}), 500

    data = request.get_json(silent=True) or {}
    scan_ip = (data.get("ip") or "").strip() or SCANNER_IP
    if not scan_ip:
        return jsonify({"error": "Scanner IP not provided. Enter it in the scanner modal."}), 400

    scan_scheme = data.get("scheme")
    scan_port = data.get("port")
    if scan_scheme and scan_port:
        scan_scheme = str(scan_scheme)
        scan_port = int(scan_port)
    else:
        found = _probe_escl_scanner(scan_ip)
        if not found:
            return jsonify({"error": f"Cannot reach eSCL scanner at {scan_ip}"}), 502
        scan_scheme, scan_port, _ = found

    scanner_base_dynamic = f"{scan_scheme}://{scan_ip}:{scan_port}/eSCL"

    # Requested settings (user-supplied or defaults)
    color_mode = data.get("color", data.get("color_mode", "RGB24"))
    resolution = int(data.get("dpi", data.get("resolution", 200)))
    fmt = data.get("format", "application/pdf")
    source = data.get("source", "Platen")

    # ── Step 1: Fetch capabilities and validate settings ──────────────────
    # Capabilities are cached per device (see _get_escl_capabilities) so
    # multi-page scan sessions don't pay a network round-trip to the
    # printer on every single page.
    caps, caps_freshly_fetched = _get_escl_capabilities(scan_scheme, scan_ip, scan_port)

    # Clamp resolution to a value the scanner supports
    supported_dpis = caps.get("resolutions", [])
    if supported_dpis and resolution not in supported_dpis:
        # Pick the closest supported DPI
        resolution = min(supported_dpis, key=lambda x: abs(x - resolution))

    # Clamp color mode
    supported_colors = caps.get("color_modes", [])
    if supported_colors and color_mode not in supported_colors:
        # Prefer RGB24 → Grayscale8 → first available
        for fallback in ("RGB24", "Grayscale8", "BlackAndWhite1"):
            if fallback in supported_colors:
                color_mode = fallback
                break
        else:
            color_mode = supported_colors[0]

    # Clamp format — prefer requested, fall back to image/jpeg (universally supported)
    supported_fmts = caps.get("formats", [])
    if supported_fmts and fmt not in supported_fmts:
        for fallback in ("application/pdf", "image/jpeg", "image/png"):
            if fallback in supported_fmts:
                fmt = fallback
                break
        else:
            fmt = supported_fmts[0]

    # ── Step 2: Build ScanSettings XML ──────────────────────────────────────
    # Confirmed working on HP Color LaserJet Pro MFP M479fnw (raw-test 201):
    #   pwg:Version 2.63 is REQUIRED — omitting it causes 409 every time.
    #   scan:DocumentFormatExt (not pwg:DocumentFormat)
    import time as _time

    scan_settings_xml = "\r\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<scan:ScanSettings',
        '    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"',
        '    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">',
        '  <pwg:Version>2.63</pwg:Version>',
        '  <scan:Intent>Document</scan:Intent>',
        '  <scan:DocumentFormatExt>' + fmt + '</scan:DocumentFormatExt>',
        '  <scan:XResolution>' + str(resolution) + '</scan:XResolution>',
        '  <scan:YResolution>' + str(resolution) + '</scan:YResolution>',
        '  <scan:ColorMode>' + color_mode + '</scan:ColorMode>',
        '  <scan:InputSource>' + source + '</scan:InputSource>',
        '</scan:ScanSettings>',
    ])

    def _post_job():
        return _requests.post(
            f"{scanner_base_dynamic}/ScanJobs",
            data=scan_settings_xml.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
            timeout=15,
            verify=False,
        )

    def _drain_and_delete_job(uri: str) -> None:
        try:
            dr = _requests.get(f"{uri}/NextDocument", timeout=10, verify=False, stream=True)
            for _ in dr.iter_content(65536): pass
        except Exception:
            pass
        try:
            _requests.delete(uri, timeout=5, verify=False)
        except Exception:
            pass

    def _recover_from_409(base_url: str) -> None:
        base_host = base_url.split("/eSCL")[0]
        try:
            st = _requests.get(f"{base_url}/ScannerStatus", timeout=5, verify=False)
            if st.status_code == 200:
                root = ET.fromstring(st.text)
                for el in root.iter():
                    txt = (el.text or "").strip()
                    if "ScanJobs" in txt and "/" in txt:
                        uri = txt if txt.startswith("http") else f"{base_host}{txt}"
                        _drain_and_delete_job(uri)
        except Exception:
            pass
        _time.sleep(2.0)

    try:
        # ── Step 3: POST to /eSCL/ScanJobs ───────────────────────────────
        # The settle delay is only needed right after the device was just
        # hit for capabilities. On a cache hit (typical for page 2+ of a
        # multi-page scan) there's nothing to settle from — skip it.
        if caps_freshly_fetched:
            _time.sleep(0.5)
        r = _post_job()

        if r.status_code == 409:
            _recover_from_409(scanner_base_dynamic)
            _time.sleep(3.0)  # HP needs time to fully settle before accepting new job
            r = _post_job()

        if r.status_code not in (200, 201):
            return jsonify({
                "error": f"Scanner rejected job: HTTP {r.status_code} — {r.text[:500]}",
                "sent_xml": scan_settings_xml,
                "capabilities": caps,
            }), 502

        # Location header contains the job URI — some printers omit it or
        # put it in the response body / a different header
        job_uri = (
                r.headers.get("Location")
                or r.headers.get("location")
                or r.headers.get("Content-Location")
        )

        # Some Canon/Ricoh/Xerox firmware puts the job URI in the response body
        if not job_uri and r.text:
            try:
                body_root = ET.fromstring(r.text)
                for el in body_root.iter():
                    txt = (el.text or "").strip()
                    if "ScanJobs" in txt and "/" in txt:
                        job_uri = txt
                        break
            except Exception:
                pass

        # Some printers return 200 (not 201) and expect NextDocument directly
        # without a job URI — try the standard path as a fallback
        if not job_uri:
            # Try ScannerStatus to find any new job
            try:
                st = _requests.get(f"{scanner_base_dynamic}/ScannerStatus",
                                   timeout=5, verify=False)
                if st.status_code == 200:
                    st_root = ET.fromstring(st.text)
                    for el in st_root.iter():
                        txt = (el.text or "").strip()
                        if "ScanJobs" in txt and "/" in txt:
                            job_uri = txt
                            break
            except Exception:
                pass

        # Last resort: try well-known job URI paths (job ID 1 is almost always correct
        # for a single-user office scanner with no concurrent jobs)
        if not job_uri:
            for candidate in (
                    f"{scanner_base_dynamic}/ScanJobs/1",
                    f"{scanner_base_dynamic}/ScanJobs/0",
            ):
                try:
                    probe = _requests.get(f"{candidate}/NextDocument",
                                          timeout=8, verify=False, stream=True)
                    if probe.status_code == 200:
                        job_uri = candidate
                        # consume the probe response — we'll re-fetch below
                        chunks_probe = []
                        for chunk in probe.iter_content(65536):
                            if chunk: chunks_probe.append(chunk)
                        content_probe = b"".join(chunks_probe)
                        if content_probe:
                            try:
                                _requests.delete(candidate, timeout=5, verify=False)
                            except Exception:
                                pass
                            ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                            ext = ".pdf" if "pdf" in fmt else (".png" if "png" in fmt else ".jpg")
                            mimetype = "application/pdf" if "pdf" in fmt else (
                                "image/png" if "png" in fmt else "image/jpeg")
                            return send_file(io.BytesIO(content_probe), mimetype=mimetype,
                                             as_attachment=False, download_name=f"scan_{ts}{ext}")
                    probe.close()
                except Exception:
                    pass

        if not job_uri:
            return jsonify({
                               "error": "Scanner did not return a job location — printer firmware may not be fully eSCL compliant"}), 502

        # Ensure absolute URL
        if job_uri.startswith("/"):
            job_uri = f"{scan_scheme}://{scan_ip}:{scan_port}{job_uri}"

        # ── Step 4: GET /NextDocument — stream ALL bytes THEN delete ─────────
        # CRITICAL: HP firmware keeps job locked until all bytes consumed.
        doc_url = f"{job_uri}/NextDocument"
        doc_r = _requests.get(doc_url, timeout=120, verify=False, stream=True)

        if doc_r.status_code != 200:
            try:
                for _ in doc_r.iter_content(chunk_size=65536): pass
            except Exception:
                pass
            try:
                _requests.delete(job_uri, timeout=5, verify=False)
            except Exception:
                pass
            return jsonify({"error": f"Could not retrieve scanned document: HTTP {doc_r.status_code}"}), 502

        chunks = []
        try:
            for chunk in doc_r.iter_content(chunk_size=65536):
                if chunk: chunks.append(chunk)
        except Exception as stream_err:
            try:
                _requests.delete(job_uri, timeout=5, verify=False)
            except Exception:
                pass
            return jsonify({"error": f"Stream interrupted: {stream_err}"}), 502

        content = b"".join(chunks)

        # ── Step 5: Delete AFTER full read ───────────────────────────────────
        try:
            _requests.delete(job_uri, timeout=5, verify=False)
        except Exception:
            pass
        if not content:
            return jsonify({"error": "Scanner returned an empty document"}), 502

        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        if "pdf" in fmt:
            ext, mimetype = ".pdf", "application/pdf"
        elif "png" in fmt:
            ext, mimetype = ".png", "image/png"
        else:
            ext, mimetype = ".jpg", "image/jpeg"
        file_name = f"scan_{ts}{ext}"

        return send_file(
            io.BytesIO(content),
            mimetype=mimetype,
            as_attachment=False,
            download_name=file_name,
        )

    except _requests.exceptions.ConnectTimeout:
        return jsonify({"error": f"Connection timed out — is the scanner at {scan_ip} reachable?"}), 504
    except _requests.exceptions.ConnectionError as e:
        return jsonify({"error": f"Cannot reach scanner at {scan_ip} — {e}"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _cancel_stuck_escl_jobs(scanner_base: str) -> None:
    """
    Cancel only genuinely stuck/failed eSCL scan jobs — never a healthy one.

    eSCL job states (pwg:JobState):
      Processing   — actively scanning right now  → LEAVE ALONE
      Pending      — queued, not yet started       → LEAVE ALONE
      Completed    — finished cleanly              → safe to DELETE (already done)
      Canceled     — already canceled              → safe to DELETE (already done)
      Aborted      — scanner-side error/timeout    → DELETE  (stuck, will cause 409)

    Additionally, if a job has no state in the XML at all (malformed/legacy
    firmware), we treat it as potentially stuck and delete it — same as before,
    but only when no state element is present.

    This is best-effort: all errors are silently swallowed so a hiccup here
    never blocks the real scan attempt.
    """
    if not _requests or not scanner_base:
        return

    # States that are safe to cancel (won't interrupt another user's live scan).
    # NOTE: "Completed" is intentionally included — HP LEDM firmware keeps
    # completed jobs in the queue and they still cause 409 on the next POST.
    CANCELLABLE_STATES = {"Aborted", "Completed", "Canceled"}
    # Only "Processing" is truly live — leave it alone.
    # "Pending" on HP can also cause 409, so we cancel it too.
    ACTIVE_STATES = {"Processing"}

    from urllib.parse import urlparse

    def _make_absolute(uri: str) -> str:
        if uri.startswith("/"):
            p = urlparse(scanner_base)
            return f"{p.scheme}://{p.netloc}{uri}"
        return uri

    try:
        status_r = _requests.get(
            f"{scanner_base}/ScannerStatus",
            timeout=5, verify=False,
        )
        if status_r.status_code != 200:
            return

        ns = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
              "pwg": "http://www.pwg.org/schemas/2010/12/sm"}
        try:
            root = ET.fromstring(status_r.text)
        except Exception:
            return

        # Walk every <scan:JobInfo> block and check its state before deleting
        job_infos = root.findall(".//scan:JobInfo", ns)
        if job_infos:
            for job in job_infos:
                uri_el = job.find("scan:JobUri", ns)
                state_el = job.find("pwg:JobState", ns)
                if uri_el is None or not (uri_el.text or "").strip():
                    continue
                uri = _make_absolute(uri_el.text.strip())
                state = (state_el.text or "").strip() if state_el is not None else None

                if state in ACTIVE_STATES:
                    # Another user's live scan — do not touch it
                    continue
                if state in CANCELLABLE_STATES or state is None:
                    # Stuck/failed/unknown — safe to clear
                    try:
                        _requests.delete(uri, timeout=4, verify=False)
                    except Exception:
                        pass
            return

        # Fallback: firmware doesn't emit <JobInfo> blocks — fall back to plain
        # <scan:JobUri> list with no state info.  Only cancel if the top-level
        # scanner state itself is not "Processing" (i.e. no active scan).
        scanner_state_el = root.find(".//pwg:State", ns)
        scanner_state = (scanner_state_el.text or "").strip() if scanner_state_el is not None else ""
        if scanner_state == "Processing":
            # Scanner is actively scanning for someone — leave everything alone
            return

        job_uris = [
            _make_absolute(el.text.strip())
            for el in root.findall(".//scan:JobUri", ns)
            if el.text and el.text.strip()
        ]
        for uri in job_uris:
            try:
                _requests.delete(uri, timeout=4, verify=False)
            except Exception:
                pass

    except Exception:
        pass  # never block the caller


def _cancel_ledm_jobs(scheme: str, ip: str, port: int) -> list:
    """
    Aggressively clear ALL jobs from HP LEDM and eSCL layers.
    Returns a list of log strings for the reset endpoint to surface to the UI.
    Silently swallows all errors — never blocks the caller.
    """
    if not _requests:
        return []

    import time as _t
    base = f"{scheme}://{ip}:{port}"
    log = []

    # ── 1. Brute-force DELETE jobs 1-20 on every known HP LEDM path ──────────
    # HP LEDM job IDs are small sequential integers.  We don't bother parsing
    # the job list XML (which varies by firmware) — we just DELETE every plausible
    # ID.  404 responses are fine and expected for IDs that don't exist.
    LEDM_DELETE_BASES = [
        "/Jobs/JobList",
        "/Scan/Jobs",
        "/Scan/ScanJobs",
        "/eSCL/ScanJobs",  # some HP models use this path at LEDM level too
    ]
    for jid in range(1, 21):  # covers any realistically stuck job
        for base_path in LEDM_DELETE_BASES:
            url = f"{base}{base_path}/{jid}"
            try:
                r = _requests.delete(url, timeout=3, verify=False)
                if r.status_code in (200, 204):
                    log.append(f"DELETED {url} → {r.status_code}")
            except Exception:
                pass

    # ── 2. Parse job lists and delete by ID (belt-and-suspenders) ────────────
    LIST_PATHS = ["/Jobs/JobList", "/Scan/Jobs"]
    for list_path in LIST_PATHS:
        try:
            resp = _requests.get(f"{base}{list_path}", timeout=4, verify=False)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            # Harvest every element whose text looks like a job ID or URI fragment
            job_ids = set()
            for el in root.iter():
                txt = (el.text or "").strip()
                # Bare integer IDs
                if txt.isdigit():
                    job_ids.add(txt)
                # URI-style: /Jobs/JobList/3  or  /Scan/Jobs/7
                if "/" in txt:
                    part = txt.rstrip("/").split("/")[-1]
                    if part.isdigit():
                        job_ids.add(part)
            for jid in job_ids:
                for del_path in LEDM_DELETE_BASES:
                    url = f"{base}{del_path}/{jid}"
                    try:
                        r = _requests.delete(url, timeout=3, verify=False)
                        if r.status_code in (200, 204):
                            log.append(f"DELETED {url} → {r.status_code}")
                    except Exception:
                        pass
        except Exception:
            pass

    # ── 3. HP-specific: POST a CancelJob action (some firmware generations) ──
    CANCEL_PATHS = [
        "/Scan/Jobs/Cancel",
        "/Jobs/JobList/Cancel",
    ]
    cancel_body = b'<?xml version="1.0"?><CancelJob/>'
    for path in CANCEL_PATHS:
        try:
            _requests.post(f"{base}{path}", data=cancel_body,
                           headers={"Content-Type": "text/xml"}, timeout=3, verify=False)
        except Exception:
            pass

    _t.sleep(0.5)  # brief pause after all DELETEs
    return log


@app.route("/api/scanner/capabilities", methods=["GET"])
@login_required
def api_scanner_capabilities():
    """Return raw ScannerCapabilities XML for debugging."""
    ip = request.args.get("ip", "").strip() or SCANNER_IP
    if not ip:
        return jsonify({"error": "No IP"}), 400
    found = _probe_escl_scanner(ip)
    if not found:
        return jsonify({"error": f"Cannot reach {ip}"}), 502
    scheme, port, _ = found
    try:
        r = _requests.get(f"{scheme}://{ip}:{port}/eSCL/ScannerCapabilities",
                          timeout=6, verify=False)
        return jsonify({"status": r.status_code, "body": r.text, "caps": _parse_escl_capabilities(r.text)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scanner/reset", methods=["POST"])
@login_required
def api_scanner_reset():
    """
    Hard-reset the HP scanner job queue.
    Called by the UI "Reset Scanner" button when a 409 persists.
    Returns a JSON report of what was deleted.
    """
    if not _requests:
        return jsonify({"error": "requests not installed"}), 500

    data = request.get_json(silent=True) or {}
    scan_ip = (data.get("ip") or "").strip() or SCANNER_IP
    if not scan_ip:
        return jsonify({"error": "No scanner IP"}), 400

    found = _probe_escl_scanner(scan_ip)
    if not found:
        return jsonify({"error": f"Cannot reach scanner at {scan_ip}"}), 502
    scheme, port, _ = found
    base = f"{scheme}://{scan_ip}:{port}"

    import time as _t

    escl_base = f"{base}/eSCL"
    log = []

    # ── Drain + DELETE every job found in ScannerStatus ──────────────────────
    # HP IPG-LEDM keeps jobs alive until their document is consumed.
    # We must GET NextDocument (drain the buffer) before DELETE works.
    try:
        st = _requests.get(f"{escl_base}/ScannerStatus", timeout=5, verify=False)
        log.append(f"ScannerStatus → {st.status_code}")
        if st.status_code == 200:
            log.append(f"Body: {st.text[:600]}")
            root = ET.fromstring(st.text)
            for el in root.iter():
                txt = (el.text or "").strip()
                if "ScanJobs" in txt and "/" in txt:
                    uri = txt if txt.startswith("http") else f"{base}{txt}"
                    # Drain first
                    try:
                        dr = _requests.get(f"{uri}/NextDocument", timeout=20,
                                           verify=False, stream=True)
                        for _ in dr.iter_content(65536): pass
                        log.append(f"Drained {uri}/NextDocument → {dr.status_code}")
                    except Exception as de:
                        log.append(f"Drain {uri}: {de}")
                    # Then delete
                    try:
                        dl = _requests.delete(uri, timeout=5, verify=False)
                        log.append(f"DELETE {uri} → {dl.status_code}")
                    except Exception as de:
                        log.append(f"DELETE {uri}: {de}")
    except Exception as e:
        log.append(f"ScannerStatus error: {e}")

    # ── Brute-force drain+delete eSCL job IDs 1-10 ───────────────────────────
    for jid in range(1, 11):
        uri = f"{escl_base}/ScanJobs/{jid}"
        try:
            dr = _requests.get(f"{uri}/NextDocument", timeout=10,
                               verify=False, stream=True)
            if dr.status_code == 200:
                for _ in dr.iter_content(65536): pass
                log.append(f"Drained job {jid}")
        except Exception:
            pass
        try:
            dl = _requests.delete(uri, timeout=3, verify=False)
            if dl.status_code in (200, 204):
                log.append(f"DELETE eSCL job {jid} → {dl.status_code}")
        except Exception:
            pass

    # ── LEDM brute-force DELETE ───────────────────────────────────────────────
    for path in ("/Jobs/JobList", "/Scan/Jobs"):
        for jid in range(1, 11):
            try:
                dl = _requests.delete(f"{base}{path}/{jid}", timeout=3, verify=False)
                if dl.status_code in (200, 204):
                    log.append(f"DELETE LEDM {path}/{jid} → {dl.status_code}")
            except Exception:
                pass

    _t.sleep(2.0)

    # ── Verify ────────────────────────────────────────────────────────────────
    status_ok = False
    scanner_state = "Unknown"
    try:
        st2 = _requests.get(f"{escl_base}/ScannerStatus", timeout=5, verify=False)
        if st2.status_code == 200:
            root2 = ET.fromstring(st2.text)
            ns = {"pwg": "http://www.pwg.org/schemas/2010/12/sm"}
            state_el = root2.find(".//pwg:State", ns)
            scanner_state = (state_el.text or "Unknown").strip() if state_el is not None else "Unknown"
            status_ok = scanner_state in ("Idle", "idle", "")
            log.append(f"Final state: {scanner_state}")
    except Exception as e:
        log.append(f"Final status check: {e}")

    return jsonify({
        "ok": True,
        "ready": status_ok,
        "scanner_state": scanner_state,
        "log": log,
    })


def _parse_escl_capabilities(xml_text):
    """Extract useful fields from eSCL ScannerCapabilities XML."""
    caps = {}
    try:
        ns = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
              "pwg": "http://www.pwg.org/schemas/2010/12/sm"}
        root = ET.fromstring(xml_text)
        make_model = root.find(".//pwg:MakeAndModel", ns)
        if make_model is not None:
            caps["make_model"] = make_model.text
        resolutions = [el.text for el in root.findall(".//scan:XResolution", ns) if el.text]
        if resolutions:
            caps["resolutions"] = sorted(set(int(r) for r in resolutions if r.isdigit()))
        color_modes = [el.text for el in root.findall(".//scan:ColorMode", ns) if el.text]
        if color_modes:
            caps["color_modes"] = list(set(color_modes))
        formats = [el.text for el in root.findall(".//scan:DocumentFormatExt", ns) if el.text]
        if formats:
            caps["formats"] = formats
    except Exception:
        pass
    return caps


# ══════════════════════════════════════════════════════════════════════════════
# USB / WIRED SCANNER  (SANE on Linux/Mac, WIA via PowerShell on Windows)
# ══════════════════════════════════════════════════════════════════════════════
import platform as _platform
import subprocess as _subprocess
import shutil as _shutil
import tempfile as _tempfile


def _usb_backend() -> str:
    """Return 'sane', 'wia', or 'none'."""
    if _platform.system() == "Windows":
        # Check PowerShell is available (it always is on Windows)
        if _shutil.which("powershell") or _shutil.which("pwsh"):
            return "wia"
        return "none"
    # Linux / macOS — check for scanimage (SANE)
    if _shutil.which("scanimage"):
        return "sane"
    return "none"


def _sane_list_devices() -> list:
    """
    Run `scanimage -L` and return a list of {id, name} dicts.
    Example output line:
      device `epson2:libusb:001:005' is a Epson GT-S650 flatbed scanner
    """
    try:
        result = _subprocess.run(
            ["scanimage", "-L"],
            capture_output=True, text=True, timeout=15,
        )
        devices = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("device"):
                continue
            # Extract device id between backtick and quote
            import re as _re
            m = _re.search(r"`([^']+)'", line)
            dev_id = m.group(1).strip() if m else line
            # Extract friendly name after "is a "
            name_match = _re.search(r" is a (.+)$", line, _re.IGNORECASE)
            name = name_match.group(1).strip() if name_match else dev_id
            devices.append({"id": dev_id, "name": name})
        return devices
    except Exception as e:
        return []


def _wia_list_devices() -> list:
    """
    Use PowerShell + WIA COM object to list USB/local scanners on Windows.
    WIA DeviceInfo.Type values: 1=Camera, 2=Scanner, 3=Video.
    We return ALL types (not just Type==2) as a safety net, since some
    all-in-one printers register as Type==0 or Type==1 depending on driver.
    Returns [{id, name, type}]
    """
    # This script:
    #  - Creates the WIA DeviceManager COM object
    #  - Iterates every DeviceInfo entry (no type filter — catch everything)
    #  - Reads the Name property via the Properties collection (index-safe)
    #  - Outputs JSON to stdout
    ps_script = r"""
try {
    $dm = New-Object -ComObject WIA.DeviceManager
    $list = @()
    for ($i = 1; $i -le $dm.DeviceInfos.Count; $i++) {
        $di = $dm.DeviceInfos.Item($i)
        $devId = $di.DeviceID
        $devType = $di.Type
        $devName = $devId
        try {
            for ($j = 1; $j -le $di.Properties.Count; $j++) {
                $p = $di.Properties.Item($j)
                if ($p.Name -eq 'Name') { $devName = $p.Value; break }
            }
        } catch {}
        $list += [PSCustomObject]@{ id = $devId; name = $devName; type = $devType }
    }
    if ($list.Count -eq 0) {
        Write-Output '[]'
    } else {
        $list | ConvertTo-Json -Depth 2
    }
} catch {
    Write-Output ('ERROR: ' + $_.Exception.Message)
}
"""
    try:
        r = _subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=25,
        )
        out = r.stdout.strip()
        if out.startswith("ERROR:"):
            raise RuntimeError(out)
        if not out or out == "[]":
            return []
        import json as _json
        data = _json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return [
            {"id": str(d.get("id", "")), "name": str(d.get("name", d.get("id", ""))), "type": d.get("type", 0)}
            for d in data if d.get("id")
        ]
    except Exception as exc:
        raise RuntimeError(f"WIA list error: {exc}") from exc


def _sane_scan(device_id: str, color: str, dpi: int, fmt: str, source: str) -> bytes:
    """
    Invoke scanimage with the given settings and return the raw bytes.
    fmt: 'application/pdf' → tiff then convert; else 'image/jpeg' or 'image/png'
    Returns raw bytes of the scanned file.
    """
    import re as _re

    # Map color mode to SANE mode string
    mode_map = {"RGB24": "Color", "Grayscale8": "Gray", "BlackAndWhite1": "Lineart"}
    sane_mode = mode_map.get(color, "Color")

    # Map source
    source_map = {"Platen": "Flatbed", "Feeder": "ADF"}
    sane_source = source_map.get(source, "Flatbed")

    # Choose output format for scanimage
    # scanimage can write pnm/tiff; we convert to jpeg/png/pdf via pillow if needed
    with _tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "scan.pnm")
        cmd = [
            "scanimage",
            f"--device-name={device_id}",
            f"--mode={sane_mode}",
            f"--resolution={dpi}",
            "--format=pnm",
            f"--output-file={out_path}",
        ]
        # Add source if not default
        try:
            # Try to pass source option (may not be supported on all devices)
            cmd.append(f"--source={sane_source}")
        except Exception:
            pass

        result = _subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            # Retry without --source (some drivers reject it)
            cmd2 = [c for c in cmd if not c.startswith("--source=")]
            result2 = _subprocess.run(cmd2, capture_output=True, text=True, timeout=120)
            if result2.returncode != 0:
                err = result2.stderr.strip() or result.stderr.strip() or "scanimage failed"
                raise RuntimeError(f"SANE scan error: {err}")

        if not os.path.isfile(out_path):
            raise RuntimeError("scanimage produced no output file")

        raw = open(out_path, "rb").read()
        if not raw:
            raise RuntimeError("Scanner returned an empty image")

        # Convert to requested format using Pillow (optional but nice)
        try:
            from PIL import Image as _Img
            import io as _io
            img = _Img.open(_io.BytesIO(raw))
            buf = _io.BytesIO()
            if "pdf" in fmt:
                img.save(buf, format="PDF", resolution=dpi)
            elif "png" in fmt:
                img.save(buf, format="PNG")
            else:
                img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
        except ImportError:
            # Pillow not installed — return raw PNM bytes (browser won't preview but save still works)
            return raw


def _wia_scan(device_id: str, color: str, dpi: int, fmt: str, source: str) -> bytes:
    """
    Use PowerShell + WIA COM to acquire a scan from a USB/local device on Windows.
    Strategy:
      1. Connect to the device by DeviceID.
      2. Set WIA item properties: horizontal/vertical resolution, color intent, data type.
      3. Transfer the image to a temp file (JPEG or BMP depending on driver support).
      4. Read and return the bytes.
    """
    # WIA CurrentIntent values: 1=Color, 2=Grayscale, 4=BlackAndWhite
    intent_map = {"RGB24": 1, "Grayscale8": 2, "BlackAndWhite1": 4}
    intent = intent_map.get(color, 1)
    # WIA DataType: 3=Color(RGB), 2=Grayscale, 0=BlackAndWhite
    datatype_map = {"RGB24": 3, "Grayscale8": 2, "BlackAndWhite1": 0}
    datatype = datatype_map.get(color, 3)

    with _tempfile.TemporaryDirectory() as tmpdir:
        # Use BMP as transfer format — universally supported by all WIA drivers.
        # We convert to the user's requested format afterwards with Pillow.
        out_path = os.path.join(tmpdir, "scan.bmp")
        out_path_ps = out_path.replace("\\", "\\\\")

        ps_script = f"""
try {{
    $dm = New-Object -ComObject WIA.DeviceManager
    $dev = $null
    for ($i = 1; $i -le $dm.DeviceInfos.Count; $i++) {{
        if ($dm.DeviceInfos.Item($i).DeviceID -eq '{device_id}') {{
            $dev = $dm.DeviceInfos.Item($i).Connect()
            break
        }}
    }}
    if ($dev -eq $null) {{ Write-Error 'Device not found: {device_id}'; exit 1 }}

    $item = $dev.Items.Item(1)

    # Helper: safely set a WIA property by ID if it exists
    function Set-WiaProp($props, $propId, $val) {{
        try {{
            for ($k = 1; $k -le $props.Count; $k++) {{
                if ($props.Item($k).PropertyID -eq $propId) {{
                    $props.Item($k).Value = $val
                    return
                }}
            }}
        }} catch {{ }}
    }}

    # Property IDs (standard WIA):
    # 6146 = CurrentIntent, 6147 = HorizontalResolution, 6148 = VerticalResolution
    # 6149 = HorizontalStartPoint, 6150 = VerticalStartPoint
    # 6151 = HorizontalExtent, 6152 = VerticalExtent, 6154 = DataType
    Set-WiaProp $item.Properties 6146 {intent}
    Set-WiaProp $item.Properties 6147 {dpi}
    Set-WiaProp $item.Properties 6148 {dpi}
    Set-WiaProp $item.Properties 6154 {datatype}

    # Transfer — pass BMP format GUID for maximum driver compatibility
    # BMP GUID: {{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}}
    $img = $item.Transfer("{{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}}")
    $img.SaveFile('{out_path_ps}')
    Write-Output 'OK'
}} catch {{
    Write-Error $_.Exception.Message
    exit 1
}}
"""
        r = _subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0 or "OK" not in r.stdout:
            err = r.stderr.strip() or r.stdout.strip() or "WIA scan failed (no output)"
            raise RuntimeError(f"WIA scan error: {err}")
        if not os.path.isfile(out_path):
            raise RuntimeError("WIA scan completed but no output file was created")

        raw = open(out_path, "rb").read()
        if not raw:
            raise RuntimeError("WIA produced an empty file")

        # Convert BMP → requested format using Pillow if available
        try:
            from PIL import Image as _Img
            import io as _io
            img = _Img.open(_io.BytesIO(raw))
            buf = _io.BytesIO()
            if "pdf" in fmt:
                # Convert to RGB if needed (PDF requires RGB or L)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img.save(buf, format="PDF", resolution=dpi)
            elif "png" in fmt:
                img.save(buf, format="PNG")
            else:
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
        except ImportError:
            # Pillow not installed — return raw BMP bytes; browser can't display
            # but the file will still save/archive correctly
            return raw


@app.route("/api/scanner/usb/list", methods=["GET"])
@login_required
def api_usb_scanner_list():
    """List locally attached USB/wired scanners."""
    backend = _usb_backend()
    if backend == "none":
        return jsonify({
            "backend": "none",
            "scanners": [],
            "error": (
                "No local scanner backend found on this server. "
                "On Linux/Mac: install SANE with 'sudo apt install sane-utils'. "
                "On Windows: PowerShell with WIA COM is required (built-in on Windows 7+)."
            ),
        }), 200
    try:
        if backend == "sane":
            devices = _sane_list_devices()
        else:
            devices = _wia_list_devices()
        return jsonify({"backend": backend, "scanners": devices})
    except Exception as e:
        # Surface the real error message so the UI can show it
        return jsonify({"backend": backend, "scanners": [], "error": str(e)}), 200


@app.route("/api/scanner/usb/debug", methods=["GET"])
@login_required
def api_usb_scanner_debug():
    """
    Debug endpoint — returns raw PowerShell/SANE output so you can diagnose
    why the scanner isn't being detected. Visit /api/scanner/usb/debug in your browser.
    """
    import sys as _sys
    info = {
        "platform": _platform.system(),
        "python": _sys.version,
        "backend_detected": _usb_backend(),
        "powershell_path": _shutil.which("powershell") or _shutil.which("pwsh"),
        "scanimage_path": _shutil.which("scanimage"),
    }

    if _platform.system() == "Windows":
        ps_script = r"""
try {
    $dm = New-Object -ComObject WIA.DeviceManager
    $out = @{ count = $dm.DeviceInfos.Count; devices = @() }
    for ($i = 1; $i -le $dm.DeviceInfos.Count; $i++) {
        $di = $dm.DeviceInfos.Item($i)
        $props = @{}
        try {
            for ($j = 1; $j -le $di.Properties.Count; $j++) {
                $p = $di.Properties.Item($j)
                $props[$p.Name] = "$($p.Value)"
            }
        } catch {}
        $out.devices += [PSCustomObject]@{
            DeviceID = $di.DeviceID
            Type     = $di.Type
            Props    = $props
        }
    }
    $out | ConvertTo-Json -Depth 4
} catch {
    Write-Output ('EXCEPTION: ' + $_.Exception.Message + ' | ' + $_.Exception.GetType().FullName)
}
"""
        r = _subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=25,
        )
        info["ps_stdout"] = r.stdout
        info["ps_stderr"] = r.stderr
        info["ps_returncode"] = r.returncode
    else:
        r = _subprocess.run(["scanimage", "-L"], capture_output=True, text=True, timeout=20)
        info["scanimage_stdout"] = r.stdout
        info["scanimage_stderr"] = r.stderr
        info["scanimage_returncode"] = r.returncode

    return jsonify(info)


@app.route("/api/scanner/usb/scan", methods=["POST"])
@login_required
def api_usb_scanner_scan():
    """
    Trigger a scan from a locally connected USB/wired scanner.
    Body JSON: { device_id, color, dpi, format, source }
    Returns the scanned file bytes.
    """
    backend = _usb_backend()
    if backend == "none":
        return jsonify({"error": "No local scanner backend available on this server."}), 400

    data = request.get_json(silent=True) or {}
    device_id = (data.get("device_id") or "").strip()
    color = data.get("color", "RGB24")
    dpi = int(data.get("dpi", 200))
    fmt = data.get("format", "application/pdf")
    source = data.get("source", "Platen")

    if not device_id:
        return jsonify({"error": "No device_id provided"}), 400

    try:
        if backend == "sane":
            content = _sane_scan(device_id, color, dpi, fmt, source)
        else:
            content = _wia_scan(device_id, color, dpi, fmt, source)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    if not content:
        return jsonify({"error": "Scanner returned an empty document"}), 502

    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    if "pdf" in fmt:
        ext, mimetype = ".pdf", "application/pdf"
    elif "png" in fmt:
        ext, mimetype = ".png", "image/png"
    else:
        ext, mimetype = ".jpg", "image/jpeg"

    return send_file(
        io.BytesIO(content),
        mimetype=mimetype,
        as_attachment=False,
        download_name=f"scan_usb_{ts}{ext}",
    )


# ── API: Admin — validate & repair Delivery_Method_ID sync (Task 5) ──────
# NOTE (audit, verified): no UI caller anywhere in dashboard.js/html by
# design — same pattern as run-fe-migration above: a one-off admin data-
# repair utility meant to be triggered manually (by URL/Postman/curl) when
# needed, not from a button. Confirmed intentional, not dead code.
@app.route("/api/admin/fix-delivery-method", methods=["POST"])
@login_required
def api_fix_delivery_method():
    """
    Task 5 — Admin utility: scan Adco_Transactions for rows where
    Delivery_Method_ID != 1 (or IS NULL) and repair them in a single
    UPDATE.  Returns a count of rows fixed.

    Only admin users may call this endpoint.
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: admin only"}), 403

    conn = None
    try:
        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        # Count mismatches first (for the response)
        cursor.execute("""
                       SELECT COUNT(*)
                       FROM dbo.Adco_Transactions
                       WHERE IsDeleted = 0
                         AND (Delivery_Method_ID IS NULL OR Delivery_Method_ID != 1)
                       """)
        mismatch_count = cursor.fetchone()[0]

        if mismatch_count > 0:
            cursor.execute("""
                           UPDATE dbo.Adco_Transactions
                           SET Delivery_Method_ID = 1
                           WHERE IsDeleted = 0
                             AND (Delivery_Method_ID IS NULL OR Delivery_Method_ID != 1)
                           """)

        conn.commit()
        return jsonify({
            "success": True,
            "rows_fixed": mismatch_count,
            "message": (
                f"Fixed {mismatch_count} row(s) where Delivery_Method_ID != 1"
                if mismatch_count else
                "No mismatches found — all rows already in sync"
            ),
        })
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# ── API: Delete transaction (Task 9) ──────────────────────────────────────
@app.route("/api/documents/<int:doc_id>", methods=["POST"])
@login_required
def api_update_document(doc_id):
    """Update an existing transaction (edit mode). Accepts multipart/form-data with _method=PATCH."""
    method_override = request.form.get("_method", "").upper()
    if method_override != "PATCH":
        return jsonify({"error": "Method not allowed"}), 405
    if not _check_accr(1, "Can_Edit"):
        return jsonify({"error": "Access denied: you do not have permission to edit transactions."}), 403
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Verify exists and not deleted
        cursor.execute("SELECT ID FROM dbo.Adco_Transactions WHERE ID=? AND IsDeleted=0", doc_id)
        if not cursor.fetchone():
            return jsonify({"error": "Document not found"}), 404

        subject = request.form.get("subject", "")
        keywords = request.form.get("keywords", "")
        details = request.form.get("file_description", "")
        form_date = request.form.get("form_date", "")
        doc_number = request.form.get("doc_number", "")
        fe_vals = [request.form.get(f"Fe{i}") or None for i in range(1, 8)]
        _ensure_fe_columns_adco_transactions()

        # Snapshot old values for audit diff (best-effort — never blocks the save)
        _old = {}
        try:
            cursor.execute(
                "SELECT Subject, Keywords, Detailes, Form_Date, Form_No, Foldes_ID,"
                " Fe1, Fe2, Fe3, Fe4, Fe5, Fe6, Fe7"
                " FROM dbo.Adco_Transactions WHERE ID = ? AND IsDeleted = 0",
                doc_id,
            )
            _r = cursor.fetchone()
            if _r:
                _old = {
                    "Subject":   (_r[0] or "").strip(),
                    "Keywords":  (_r[1] or "").strip(),
                    "Details":   (_r[2] or "").strip(),
                    "Form_Date": str(_r[3] or "").strip(),
                    "Form_No":   str(_r[4] or "").strip(),
                    "Folder":    str(_r[5] or "").strip(),
                }
                for _i in range(7):
                    _old[f"Fe{_i+1}"] = str(_r[6 + _i] or "").strip()
        except Exception:
            pass

        # Task: if the edit form carries a folder_id, keep the document filed
        # under the same (or newly chosen) folder/subfolder and update To_Dept
        # accordingly. If not provided, the existing folder is left untouched.
        folder_id = request.form.get("folder_id")
        folder_dept_id = request.form.get("folder_dept_id")

        if folder_id:
            to_dept = None
            if folder_dept_id and folder_dept_id.strip() and folder_dept_id.strip() != "0":
                try:
                    to_dept = int(folder_dept_id)
                except ValueError:
                    to_dept = None
            cursor.execute("""
                           UPDATE dbo.Adco_Transactions
                           SET Subject=?,
                               Keywords=?,
                               Detailes=?,
                               Form_Date=?,
                               Form_No=?,
                               Foldes_ID=?,
                               To_Dep_ID=COALESCE(?, To_Dep_ID),
                               Fe1=?, Fe2=?, Fe3=?, Fe4=?, Fe5=?, Fe6=?, Fe7=?,
                               ModifiedBy=?,
                               ModifiedOn=GETDATE()
                           WHERE ID = ?
                             AND IsDeleted = 0
                           """, subject, keywords, details, form_date or None, doc_number or None,
                           int(folder_id), to_dept,
                           *fe_vals,
                           session["user_id"], doc_id)
        else:
            cursor.execute("""
                           UPDATE dbo.Adco_Transactions
                           SET Subject=?,
                               Keywords=?,
                               Detailes=?,
                               Form_Date=?,
                               Form_No=?,
                               Fe1=?, Fe2=?, Fe3=?, Fe4=?, Fe5=?, Fe6=?, Fe7=?,
                               ModifiedBy=?,
                               ModifiedOn=GETDATE()
                           WHERE ID = ?
                             AND IsDeleted = 0
                           """, subject, keywords, details, form_date or None, doc_number or None,
                           *fe_vals,
                           session["user_id"], doc_id)

        # ── Remove attachments the user deleted while editing ──────────────
        # soft delete alone would still show up everywhere.
        removed_ids_raw = (request.form.get("remove_attachment_ids") or "").strip()
        removed_count = 0
        if removed_ids_raw:
            try:
                removed_ids = [int(i) for i in json.loads(removed_ids_raw) if i is not None]
            except Exception:
                removed_ids = []
            removed_ids = [i for i in removed_ids if i]
            if removed_ids:
                placeholders = ",".join("?" * len(removed_ids))
                # Only ever remove attachments that actually belong to this
                # document — an id for another transaction is ignored.
                cursor.execute(
                    f"""SELECT ID, File_URL FROM dbo.Adco_Transactions_Attachments
                        WHERE ID IN ({placeholders}) AND Transaction_ID = ?""",
                    *removed_ids, doc_id,
                )
                rows_to_remove = cursor.fetchall()
                if rows_to_remove:
                    ids_found = [r[0] for r in rows_to_remove]
                    found_placeholders = ",".join("?" * len(ids_found))
                    cursor.execute(
                        f"""DELETE FROM dbo.Adco_Transactions_Attachments
                            WHERE ID IN ({found_placeholders})""",
                        *ids_found,
                    )
                    removed_count = cursor.rowcount
                    for _att_id, _file_url in rows_to_remove:
                        disk_path = resolve_attachment_disk_path(_file_url or "")
                        if disk_path:
                            try:
                                os.remove(disk_path)
                            except Exception as exc:
                                print(f"Attachment file cleanup warning ({disk_path}): {exc}")

        # OCR is opt-in here too — only run/store it for files the user
        # actually clicked "Extract Text" on (sent as original filenames).
        ocr_requested_files = _parse_ocr_requested_files(request.form)
        ocr_extracted_text = _parse_ocr_extracted_text(request.form)
        ocr_jobs: list[tuple[int, str, str]] = []

        # Handle new file uploads if any
        files_saved = 0
        if request.files:
            # Resolve which folder this document belongs to so files land in the right subfolder
            _folder_id_for_save = None
            if folder_id:
                try:
                    _folder_id_for_save = int(folder_id)
                except ValueError:
                    _folder_id_for_save = None
            if _folder_id_for_save is None:
                try:
                    cursor.execute(
                        "SELECT Foldes_ID FROM dbo.Adco_Transactions WHERE ID = ? AND IsDeleted = 0",
                        doc_id,
                    )
                    _frow = cursor.fetchone()
                    if _frow:
                        _folder_id_for_save = _frow[0]
                except Exception:
                    pass
            save_dir = get_save_dir(_folder_id_for_save, cursor)
            for f in request.files.getlist("files"):
                if not f or not f.filename:
                    continue
                original_name = f.filename or "file"
                base_name, ext = os.path.splitext(original_name)
                ext = ext or ".bin"
                file_ext = ext.lstrip(".").lower()
                ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                temp_name = f"_tmp_edit_{doc_id}_{ts}{ext}"
                temp_path = os.path.join(FILE_SAVE_DIR, temp_name)
                f.save(temp_path)
                file_size = os.path.getsize(temp_path)

                cursor.execute("""
                               INSERT INTO dbo.Adco_Transactions_Attachments
                               (Transaction_ID, File_Name, File_Description,
                                File_URL, File_Size, File_Type_ID,
                                CreatedBy, CreatedOn, IsDeleted)
                               VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 0)
                               """, doc_id, f.filename, subject, temp_path,
                               file_size, file_ext, session["user_id"])

                cursor.execute("""
                               SELECT MAX(ID) FROM dbo.Adco_Transactions_Attachments
                               WHERE Transaction_ID = ? AND IsDeleted = 0
                                 AND CreatedOn >= DATEADD(SECOND, -5, GETDATE())
                               """, doc_id)
                att_row = cursor.fetchone()
                att_id = att_row[0] if att_row and att_row[0] else ts

                # base_name comes from the client's original filename — sanitize
                # before using it to build a disk path (path traversal guard).
                # The real/original name is preserved separately in File_Name.
                safe_base_name = _safe_filename_stem(base_name)
                final_name = f"{safe_base_name}.{att_id}{ext}"
                final_path = os.path.join(FILE_SAVE_DIR, final_name)
                os.rename(temp_path, final_path)

                cursor.execute("""
                               UPDATE dbo.Adco_Transactions_Attachments
                               SET File_URL = ? WHERE ID = ?
                               """, final_name, att_id)

                # Text extraction runs automatically for every image/PDF
                # attachment in the background AFTER commit (see
                # _run_ocr_jobs_async), so it never blocks this request —
                # direct embedded-text extraction is tried first (fast, no
                # OCR engine needed) and only falls back to real OCR for
                # scanned/image-only files. If the user already ran
                # "Extract Text" and previewed the result, reuse that
                # instead of re-running extraction a second time.
                if OCR_ENABLED and (file_ext in _OCR_IMAGE_EXTS or file_ext in _OCR_PDF_EXTS):
                    preextracted = ocr_extracted_text.get(original_name) if original_name in ocr_requested_files else None
                    if preextracted:
                        try:
                            # Cached locally on disk, never written to SQL Server.
                            ocr_cache_write(att_id, final_name, preextracted)
                        except Exception as exc:
                            print(f"OCR save warning: {exc}")
                    else:
                        ocr_jobs.append((att_id, final_path, file_ext))

                files_saved += 1

        conn.commit()
        _run_ocr_jobs_async(ocr_jobs)
        # Build a human-readable diff for the audit note
        _new = {
            "Subject":   subject.strip(),
            "Keywords":  keywords.strip(),
            "Details":   details.strip(),
            "Form_Date": (form_date or "").strip(),
            "Form_No":   (doc_number or "").strip(),
            "Folder":    str(folder_id or "").strip(),
        }
        for _i in range(7):
            _new[f"Fe{_i+1}"] = str(fe_vals[_i] or "").strip()

        _changes = []
        for _field, _new_val in _new.items():
            _old_val = _old.get(_field, "")
            if _old_val != _new_val:
                # Truncate long values so the note stays under 500 chars
                _ov = (_old_val[:40] + "…") if len(_old_val) > 40 else _old_val
                _nv = (_new_val[:40] + "…") if len(_new_val) > 40 else _new_val
                _changes.append(f"{_field}: '{_ov}' → '{_nv}'")
        if removed_count:
            _changes.append(f"removed {removed_count} attachment(s)")
        if files_saved:
            _changes.append(f"added {files_saved} attachment(s)")

        _change_str = "; ".join(_changes) if _changes else "no field changes"
        # Keep total note within the NVARCHAR(500) column limit
        _note = f"Edited document ID {doc_id} — {_change_str}"
        if len(_note) > 497:
            _note = _note[:497] + "…"
        audit_log("EDIT", page_id=1, notes=_note)
        try:
            cursor.execute("SELECT To_Dep_ID FROM dbo.Adco_Transactions WHERE ID = ?", doc_id)
            _dept_row = cursor.fetchone()
            _notify_dept = _dept_row[0] if _dept_row else None
        except Exception:
            _notify_dept = None
        notify_dept_users(_notify_dept, "EDIT", doc_id, subject=subject)
        return jsonify({"success": True, "id": doc_id,
                        "registration_number": str(doc_id),
                        "files_saved": files_saved,
                        "removed_attachments": removed_count})
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/documents/<int:doc_id>", methods=["DELETE"])
@login_required
def api_delete_document(doc_id):
    """
    Deletes a transaction and all attachment records in a single transaction.
    1. Verify the transaction exists.
    2. DELETE from Adco_Transactions_Attachments.
    3. Soft-delete (IsDeleted=1) Adco_Transactions row.
    4. Commit or rollback.
    """
    if not _check_accr(1, "Can_Del"):
        return jsonify({"error": "Access denied: you do not have permission to delete transactions."}), 403
    conn = None
    try:
        if not doc_id or doc_id <= 0:
            return jsonify({"error": "Invalid transaction ID"}), 400

        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        cursor.execute(
            "SELECT Subject, To_Dep_ID FROM dbo.Adco_Transactions WHERE ID = ? AND IsDeleted = 0", doc_id
        )
        _row = cursor.fetchone()
        if not _row:
            return jsonify({"error": "Transaction not found or already deleted"}), 404
        _deleted_subject, _deleted_dept = _row[0], _row[1]

        cursor.execute(
            "DELETE FROM dbo.Adco_Transactions_Attachments WHERE Transaction_ID = ?", doc_id
        )
        deleted_attachments = cursor.rowcount

        cursor.execute(
            "UPDATE dbo.Adco_Transactions SET IsDeleted = 1 WHERE ID = ?", doc_id
        )
        if cursor.rowcount == 0:
            raise RuntimeError("Failed to mark transaction as deleted")

        conn.commit()
        audit_log("DELETE", page_id=1, notes=f"Deleted document ID {doc_id} ({deleted_attachments} attachments)")
        notify_dept_users(_deleted_dept, "DELETE", doc_id, subject=_deleted_subject)
        return jsonify({"success": True, "id": doc_id, "deleted_attachments": deleted_attachments})
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/stats")
@login_required
def api_stats():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM dbo.Adco_Transactions WHERE IsDeleted = 0")
        total_docs = cursor.fetchone()[0] or 0
        cursor.execute("""
                       SELECT COUNT(*)
                       FROM dbo.Adco_Transactions
                       WHERE IsDeleted = 0
                           AND MONTH (
                           CreatedOn) = MONTH (GETDATE())
                         AND YEAR (CreatedOn) = YEAR (GETDATE())
                       """)
        this_month = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM dbo.Adco_Folder WHERE IsDeleted = 0")
        total_folders = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM dbo.Adco_Transactions_Attachments WHERE IsDeleted = 0")
        total_attachments = cursor.fetchone()[0] or 0
        return jsonify({"total_docs": total_docs, "this_month": this_month,
                        "total_folders": total_folders, "total_attachments": total_attachments})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


# NOTE (audit, verified): no caller found anywhere — not a direct fetch(),
# not a chatbot-generated link, nothing in dashboard.html either. The New
# Document form's registration-number field just shows a static "Assigned
# on save" placeholder (see setRegistrationPlaceholder() in dashboard.js)
# rather than calling this. Likely leftover from an abandoned "preview the
# next number before saving" feature. Read-only (SELECT MAX only) so it's
# harmless as-is, but looks safe to remove if confirmed no external tool
# calls it either.
@app.route("/api/last-registration-number")
@login_required
def api_last_registration_number():
    """Stats only — registration numbers are assigned on save (INSERT ID)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(ID) FROM dbo.Adco_Transactions WHERE IsDeleted = 0")
        row = cursor.fetchone()
        return jsonify({
            "last_number": row[0] if row and row[0] else 0,
            "note": "Registration number is assigned when you save a document.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/reports")
@login_required
def api_reports():
    """
    Returns all data needed by the Reports dashboard section.
    Queries: overview KPIs, document activity, department/folder usage,
    classification splits, SLA/performance metrics, and admin activity data.
    Document-facing data is always filtered to the current user's allowed
    departments unless the user is an admin.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        allowed = get_allowed_dep_ids()
        is_admin = allowed is None
        doc_where, doc_params = _current_user_doc_access_clause(cursor, "t")
        dept_col = dept_folder_name_col(cursor)

        # ── Performance: materialize allowed folder IDs once for non-admins ──
        # The access clause contains a correlated subquery
        # (Foldes_ID IN (SELECT ID FROM Adco_Folder WHERE dept IN (...)))
        # which SQL Server re-executes for every COUNT/JOIN in this endpoint.
        # We resolve it once and rewrite doc_where to use a flat IN list instead.
        if not is_admin and allowed:
            dcol = adco_folder_dept_col(cursor)
            dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
            placeholders = ",".join("?" * len(allowed))
            cursor.execute(
                f"SELECT ID FROM dbo.Adco_Folder WHERE {dept_bracket} IN ({placeholders}) AND IsDeleted = 0",
                *allowed,
            )
            folder_ids = [r[0] for r in cursor.fetchall()]
            if folder_ids:
                fid_placeholders = ",".join("?" * len(folder_ids))
                doc_where  = f"(t.IsDeleted = 0 OR t.IsDeleted IS NULL) AND t.Foldes_ID IN ({fid_placeholders})"
                doc_params = folder_ids
            else:
                doc_where  = "1=0"
                doc_params = []
        report_date_from = _normalize_filter_date(request.args.get("date_from") or "")
        report_date_to = _normalize_filter_date(request.args.get("date_to") or "")
        report_dept_id = (request.args.get("dept_id") or "").strip()
        try:
            vol_months = max(1, min(60, int(request.args.get("vol_months") or 6)))
        except (ValueError, TypeError):
            vol_months = 6
        try:
            act_days = max(1, min(365, int(request.args.get("act_days") or 30)))
        except (ValueError, TypeError):
            act_days = 30
        try:
            login_days = max(1, min(365, int(request.args.get("login_days") or 30)))
        except (ValueError, TypeError):
            login_days = 30
        audit_from = _normalize_filter_date(request.args.get("audit_from") or "")
        audit_to   = _normalize_filter_date(request.args.get("audit_to")   or "")
        report_doc_where_parts = [doc_where]
        report_doc_params = list(doc_params)
        if report_date_from:
            report_doc_where_parts.append("CAST(t.CreatedOn AS date) >= CONVERT(date, ?, 111)")
            report_doc_params.append(report_date_from)
        if report_date_to:
            report_doc_where_parts.append("CAST(t.CreatedOn AS date) <= CONVERT(date, ?, 111)")
            report_doc_params.append(report_date_to)
        if report_dept_id:
            report_doc_where_parts.append("""
                EXISTS (
                    SELECT 1
                    FROM dbo.Adco_Folder rf
                    WHERE rf.ID = t.Foldes_ID
                      AND rf.IsDeleted = 0
                      AND rf.Dept_ID = ?
                )
            """.strip())
            report_doc_params.append(report_dept_id)
        report_doc_where = " AND ".join(f"({part})" for part in report_doc_where_parts)

        def _fetch_one(sql, params=None):
            cursor.execute(sql, *(params or []))
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else 0

        def _human_size(value):
            try:
                n = float(value or 0)
            except Exception:
                n = 0.0
            units = ["B", "KB", "MB", "GB", "TB"]
            idx = 0
            while n >= 1024 and idx < len(units) - 1:
                n /= 1024.0
                idx += 1
            return f"{n:.1f} {units[idx]}" if idx else f"{int(n)} {units[idx]}"

        # ── KPI: totals ──────────────────────────────────────────────────────
        total_docs = _fetch_one(f"SELECT COUNT(*) FROM dbo.Adco_Transactions t WHERE {report_doc_where}", report_doc_params)

        # When a date range filter is active, "This Month" reflects docs in
        # that window (same as total_docs already scoped by the filter).
        # Without a filter it falls back to the current calendar month.
        if report_date_from or report_date_to:
            this_month = total_docs
        else:
            this_month = _fetch_one(f"""
                SELECT COUNT(*)
                FROM dbo.Adco_Transactions t
                WHERE {report_doc_where}
                  AND MONTH(t.CreatedOn) = MONTH(GETDATE())
                  AND YEAR(t.CreatedOn) = YEAR(GETDATE())
            """, report_doc_params)

        # Pending: docs with no attachment (awaiting file)
        pending = _fetch_one(f"""
            SELECT COUNT(*)
            FROM dbo.Adco_Transactions t
            WHERE {report_doc_where}
              AND NOT EXISTS (
                  SELECT 1
                  FROM dbo.Adco_Transactions_Attachments a
                  WHERE a.Transaction_ID = t.ID
                    AND a.File_URL IS NOT NULL AND a.File_URL != ''
              )
        """, report_doc_params)

        # ── Monthly activity: last N calendar months (driven by vol_months) ──
        cursor.execute(f"""
            SELECT YEAR(t.CreatedOn) AS yr, MONTH(t.CreatedOn) AS mo, COUNT(*) AS cnt
            FROM dbo.Adco_Transactions t
            WHERE {report_doc_where}
              AND t.CreatedOn >= DATEADD(month, -{vol_months - 1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
            GROUP BY YEAR(t.CreatedOn), MONTH(t.CreatedOn)
            ORDER BY yr, mo
        """, report_doc_params)
        monthly = [{"year": r[0], "month": r[1], "count": r[2]} for r in cursor.fetchall()]

        # ── Docs by department and folder utilization ───────────────────────
        if is_admin:
            dept_where = "1=1"
            dept_params = []
        elif not allowed:
            dept_where = "1=0"
            dept_params = []
        else:
            dept_placeholders = ",".join("?" * len(allowed))
            dept_where = f"d.ID IN ({dept_placeholders})"
            dept_params = allowed

        if is_admin:
            cursor.execute(f"""
                SELECT
                    d.ID AS dept_id,
                    ISNULL(d.[{dept_col}], 'Unknown') AS dept_name,
                    COUNT(DISTINCT f.ID) AS folder_count,
                    COUNT(DISTINCT t.ID) AS doc_count,
                    ISNULL(SUM(CAST(ISNULL(a.File_Size, 0) AS BIGINT)), 0) AS total_size,
                    COUNT(DISTINCT CASE WHEN t.ID IS NULL THEN f.ID END) AS empty_folders,
                    COUNT(DISTINCT CASE WHEN t.ID IS NOT NULL THEN f.ID END) AS active_folders
                FROM dbo.Sys_Department d
                LEFT JOIN dbo.Adco_Folder f
                    ON f.Dept_ID = d.ID AND f.IsDeleted = 0
                LEFT JOIN dbo.Adco_Transactions t
                    ON t.Foldes_ID = f.ID
                   AND (t.IsDeleted = 0 OR t.IsDeleted IS NULL)
                LEFT JOIN dbo.Adco_Transactions_Attachments a
                    ON a.Transaction_ID = t.ID AND a.File_URL IS NOT NULL AND a.File_URL != ''
                WHERE d.IsDeleted = 0 AND {dept_where}{" AND " + report_doc_where if report_dept_id or report_date_from or report_date_to else ""}
                GROUP BY d.ID, d.[{dept_col}]
                ORDER BY doc_count DESC, dept_name
            """, dept_params + report_doc_params if (report_dept_id or report_date_from or report_date_to) else dept_params)
        else:
            cursor.execute(f"""
                SELECT
                    d.ID AS dept_id,
                    ISNULL(d.[{dept_col}], 'Unknown') AS dept_name,
                    COUNT(DISTINCT f.ID) AS folder_count,
                    COUNT(DISTINCT t.ID) AS doc_count,
                    0 AS total_size,
                    COUNT(DISTINCT CASE WHEN t.ID IS NULL THEN f.ID END) AS empty_folders,
                    COUNT(DISTINCT CASE WHEN t.ID IS NOT NULL THEN f.ID END) AS active_folders
                FROM dbo.Sys_Department d
                LEFT JOIN dbo.Adco_Folder f
                    ON f.Dept_ID = d.ID AND f.IsDeleted = 0
                LEFT JOIN dbo.Adco_Transactions t
                    ON t.Foldes_ID = f.ID
                   AND (t.IsDeleted = 0 OR t.IsDeleted IS NULL)
                WHERE d.IsDeleted = 0 AND {dept_where}{" AND " + report_doc_where if report_dept_id or report_date_from or report_date_to else ""}
                GROUP BY d.ID, d.[{dept_col}]
                ORDER BY doc_count DESC, dept_name
            """, dept_params + report_doc_params if (report_dept_id or report_date_from or report_date_to) else dept_params)
        dept_rows = cursor.fetchall()
        dept_usage = []
        for r in dept_rows:
            dept_usage.append({
                "dept_id": r[0],
                "name": r[1],
                "folders": r[2] or 0,
                "docs": r[3] or 0,
                "storage_bytes": int(r[4] or 0),
                "empty_folders": r[5] or 0,
                "active_folders": r[6] or 0,
            })

        dept_breakdown = [{"name": row["name"], "count": row["docs"]} for row in dept_usage if row["docs"] > 0][:8]
        if total_docs and sum(r["docs"] for r in dept_usage) < total_docs:
            dept_breakdown.append({"name": "Other", "count": max(0, total_docs - sum(r["docs"] for r in dept_usage))})

        if not is_admin:
            cursor.execute(f"""
                SELECT TOP 5
                    t.ID,
                    ISNULL(t.Subject, '—')       AS subject,
                    ISNULL(d.[{dept_col}], '—')  AS dept,
                    CONVERT(varchar(10), t.CreatedOn, 120) AS created
                FROM dbo.Adco_Transactions t
                LEFT JOIN dbo.Adco_Folder f   ON f.ID = t.Foldes_ID AND f.IsDeleted = 0
                LEFT JOIN dbo.Sys_Department d ON d.ID = f.Dept_ID
                WHERE {report_doc_where}
                ORDER BY t.CreatedOn DESC
            """, report_doc_params)
            recent = [{
                "id": r[0],
                "subject": r[1],
                "dept": r[2],
                "date": r[3],
                "priority": "Normal",
                "status": "Visible",
            } for r in cursor.fetchall()]
            cursor.execute(f"""
                SELECT TOP 50
                    t.ID,
                    ISNULL(t.Subject, '—') AS subject,
                    ISNULL(d.[{dept_col}], '—') AS dept,
                    CONVERT(varchar(10), t.CreatedOn, 120) AS created
                FROM dbo.Adco_Transactions t
                LEFT JOIN dbo.Adco_Folder f ON f.ID = t.Foldes_ID AND f.IsDeleted = 0
                LEFT JOIN dbo.Sys_Department d ON d.ID = f.Dept_ID
                WHERE {report_doc_where}
                ORDER BY t.CreatedOn DESC
            """, report_doc_params)
            documents = [{
                "id": r[0],
                "subject": r[1],
                "dept": r[2],
                "folder": "",
                "created": r[3],
                "priority": "Normal",
                "status": "Visible",
                "attachments": 0,
            } for r in cursor.fetchall()]
            return jsonify({
                "kpi": {
                    "total_docs": total_docs,
                    "this_month": this_month,
                    "pending": pending,
                },
                "monthly": monthly,
                "dept_breakdown": dept_breakdown,
                "dept_usage": dept_usage,
                "sla_trend": [],
                "priority": {},
                "confidentiality": {},
                "fe_breakdown": [],
                "recent": recent,
                "documents": documents,
                "storage": [],
                "tta_histogram": {},
                "activity": [],
                "audit": [],
                "logins": [],
                "workflow": {},
            })

        sla_trend = []
        priority = {}
        confidentiality = {}
        fe_breakdown = []
        imp_label = {1: "Normal", 2: "Important", 3: "Urgent"}
        recent = []
        documents = []
        storage = []
        tta_histogram = {}
        activity = []
        logins = []
        workflow = {}
        if is_admin:
            # Admin recent: top 5 latest documents across all accessible docs
            cursor.execute(f"""
                SELECT TOP 5
                    t.ID,
                    ISNULL(t.Subject, '—')       AS subject,
                    ISNULL(d.[{dept_col}], '—')  AS dept,
                    CONVERT(varchar(10), t.CreatedOn, 120) AS created
                FROM dbo.Adco_Transactions t
                LEFT JOIN dbo.Adco_Folder f   ON f.ID = t.Foldes_ID AND f.IsDeleted = 0
                LEFT JOIN dbo.Sys_Department d ON d.ID = f.Dept_ID
                WHERE {report_doc_where}
                ORDER BY t.CreatedOn DESC
            """, report_doc_params)
            recent = [{
                "id": r[0],
                "subject": r[1],
                "dept": r[2],
                "date": r[3],
                "priority": "Normal",
                "status": "Visible",
            } for r in cursor.fetchall()]

            # Admin-only extras: heavier summaries that regular users do not need.
            cursor.execute(f"""
                SELECT YEAR(t.CreatedOn) AS yr, MONTH(t.CreatedOn) AS mo,
                       COUNT(DISTINCT t.ID) AS total,
                       COUNT(DISTINCT CASE WHEN DATEDIFF(day, t.CreatedOn, a.CreatedOn) <= 3 THEN t.ID END) AS on_time
                FROM dbo.Adco_Transactions t
                LEFT JOIN dbo.Adco_Transactions_Attachments a
                       ON a.Transaction_ID = t.ID
                      AND a.File_URL IS NOT NULL AND a.File_URL != ''
                WHERE {report_doc_where}
                  AND t.CreatedOn >= DATEADD(month, -{vol_months - 1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
                GROUP BY YEAR(t.CreatedOn), MONTH(t.CreatedOn)
                ORDER BY yr, mo
            """, report_doc_params)
            sla_trend = [{"year": r[0], "month": r[1], "total": r[2], "sla_pct": round((r[3] / r[2] * 100) if r[2] else 0)} for r in cursor.fetchall()]

            cursor.execute(f"""
                SELECT ISNULL(Importance_Degree_ID, 1) AS imp, COUNT(*) AS cnt
                FROM dbo.Adco_Transactions t
                WHERE {report_doc_where}
                GROUP BY Importance_Degree_ID
            """, report_doc_params)
            priority = {imp_label.get(r[0], "Normal"): r[1] for r in cursor.fetchall()}

            cursor.execute(f"""
                SELECT ISNULL(Secret_Degree_ID, 1) AS conf, COUNT(*) AS cnt
                FROM dbo.Adco_Transactions t
                WHERE {report_doc_where}
                GROUP BY Secret_Degree_ID
            """, report_doc_params)
            conf_map = {1: "Normal", 2: "Confidential", 3: "High Confidential"}
            confidentiality = {conf_map.get(r[0], "Normal"): r[1] for r in cursor.fetchall()}

            for i in range(1, 8):
                col = f"Fe{i}"
                cursor.execute(f"""
                    SELECT TOP 8 ISNULL(CAST(t.[{col}] AS NVARCHAR(255)), '—') AS label, COUNT(*) AS cnt
                    FROM dbo.Adco_Transactions t
                    WHERE {report_doc_where}
                      AND NULLIF(LTRIM(RTRIM(CAST(t.[{col}] AS NVARCHAR(255)))), '') IS NOT NULL
                    GROUP BY CAST(t.[{col}] AS NVARCHAR(255))
                    ORDER BY cnt DESC, label
                """, report_doc_params)
                rows = cursor.fetchall()
                fe_breakdown.append({
                    "field": col,
                    "label": col,
                    "items": [{"name": r[0], "count": r[1]} for r in rows],
                })

            cursor.execute(f"""
                SELECT TOP 10 ISNULL(u.USER_FULLNAME, u.USER_NAME) AS user_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN al.Action_Type = 'ADD' THEN 1 ELSE 0 END) AS added,
                       SUM(CASE WHEN al.Action_Type = 'EDIT' THEN 1 ELSE 0 END) AS edited,
                       SUM(CASE WHEN al.Action_Type = 'DELETE' THEN 1 ELSE 0 END) AS deleted,
                       SUM(CASE WHEN al.Action_Type = 'DOWNLOAD' THEN 1 ELSE 0 END) AS downloaded,
                       MAX(al.Action_At) AS last_active
                FROM dbo.Sys_AuditLog al
                LEFT JOIN dbo.Sys_User u ON u.USER_ID = al.USER_ID
                WHERE al.Action_At >= DATEADD(day, -{act_days}, GETDATE())
                GROUP BY ISNULL(u.USER_FULLNAME, u.USER_NAME)
                ORDER BY total DESC
            """)
            activity = [{
                "user": r[0],
                "total": r[1] or 0,
                "added": r[2] or 0,
                "edited": r[3] or 0,
                "deleted": r[4] or 0,
                "downloaded": r[5] or 0,
                "last_active": _fmt_audit_time(r[6]),
            } for r in cursor.fetchall()]

            _audit_where = "1=1"
            _audit_params: list = []
            if audit_from:
                _audit_where += " AND CAST(al.Action_At AS date) >= CONVERT(date, ?, 111)"
                _audit_params.append(audit_from)
            else:
                # default: last 30 days when no explicit range supplied
                _audit_where += " AND al.Action_At >= DATEADD(day, -30, GETDATE())"
            if audit_to:
                _audit_where += " AND CAST(al.Action_At AS date) <= CONVERT(date, ?, 111)"
                _audit_params.append(audit_to)
            cursor.execute(f"""
                SELECT TOP 1000 al.Action_At, ISNULL(u.USER_FULLNAME, u.USER_NAME) AS user_name,
                       al.Action_Type, ISNULL(al.Notes, ''), ISNULL(al.IP_Address, '')
                FROM dbo.Sys_AuditLog al
                LEFT JOIN dbo.Sys_User u ON u.USER_ID = al.USER_ID
                WHERE {_audit_where}
                ORDER BY al.Action_At DESC
            """, _audit_params)
            audit = [{
                "time": _fmt_audit_time(r[0]),
                "user": r[1],
                "action": r[2],
                "notes": r[3],
                "ip": r[4],
            } for r in cursor.fetchall()]

            cursor.execute(f"""
                SELECT TOP 30 ISNULL(u.USER_FULLNAME, u.USER_NAME) AS user_name,
                       SUM(CASE WHEN al.Action_Type = 'LOGIN' THEN 1 ELSE 0 END) AS logins,
                       SUM(CASE WHEN al.Action_Type = 'LOGIN_FAILED' THEN 1 ELSE 0 END) AS failed,
                       MAX(al.Action_At) AS last_active,
                       MAX(ISNULL(al.IP_Address, '')) AS ip
                FROM dbo.Sys_AuditLog al
                LEFT JOIN dbo.Sys_User u ON u.USER_ID = al.USER_ID
                WHERE al.Action_At >= DATEADD(day, -{login_days}, GETDATE())
                GROUP BY ISNULL(u.USER_FULLNAME, u.USER_NAME)
                ORDER BY last_active DESC
            """)
            logins = [{
                "user": r[0],
                "logins": r[1] or 0,
                "failed": r[2] or 0,
                "last_active": _fmt_audit_time(r[3]),
                "ip": r[4] or "",
            } for r in cursor.fetchall()]

            # ── Workflow analytics (admin-only, mirrors Activity/Audit/Logins) ──
            workflow = _build_workflow_report(cursor, vol_months, act_days)
        return jsonify({
            "kpi": {
                "total_docs": total_docs,
                "this_month": this_month,
                "pending": pending,
            },
            "monthly": monthly,
            "dept_breakdown": dept_breakdown,
            "dept_usage": dept_usage,
            "sla_trend": sla_trend,
            "priority": priority,
            "confidentiality": confidentiality,
            "fe_breakdown": fe_breakdown,
            "recent": recent,
            "documents": documents,
            "storage": storage,
            "tta_histogram": tta_histogram,
            "activity": activity,
            "audit": audit,
            "logins": logins,
            "workflow": workflow,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


def _build_workflow_report(cursor, vol_months, act_days):
    """
    Builds the Workflow analytics tab data for the Reports section (admin-only).
    Reuses the cursor/connection already open in api_reports. Every query is
    wrapped defensively — a brand-new install that has never touched the
    Workflow module yet still has WF_* tables (created by
    _ensure_workflow_tables on startup), but we don't want a report page to
    ever 500 just because a table happens to be empty or a column is
    momentarily unexpected, so we fail soft to empty defaults instead.
    """
    result = {
        "kpi": {"pending": 0, "approved": 0, "rejected": 0, "overdue": 0, "avg_turnaround_days": None},
        "status_breakdown": {},
        "monthly": [],
        "top_approvers": [],
        "pending_list": [],
    }
    try:
        # ── KPIs ──────────────────────────────────────────────────────────
        cursor.execute("""
            SELECT COUNT(*) FROM dbo.WF_Instances
            WHERE IsDeleted = 0 AND Status NOT IN ('Approved', 'Rejected', 'Draft')
        """)
        result["kpi"]["pending"] = cursor.fetchone()[0] or 0

        cursor.execute(f"""
            SELECT COUNT(*) FROM dbo.WF_Instances
            WHERE IsDeleted = 0 AND Status = 'Approved'
              AND CompletedOn >= DATEADD(month, -{vol_months - 1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
        """)
        result["kpi"]["approved"] = cursor.fetchone()[0] or 0

        cursor.execute(f"""
            SELECT COUNT(*) FROM dbo.WF_Instances
            WHERE IsDeleted = 0 AND Status = 'Rejected'
              AND CompletedOn >= DATEADD(month, -{vol_months - 1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
        """)
        result["kpi"]["rejected"] = cursor.fetchone()[0] or 0

        cursor.execute("""
            SELECT COUNT(*) FROM dbo.WF_Instances
            WHERE IsDeleted = 0 AND Status NOT IN ('Approved', 'Rejected', 'Draft')
              AND ExpiryDate IS NOT NULL AND ExpiryDate < CAST(GETDATE() AS DATE)
        """)
        result["kpi"]["overdue"] = cursor.fetchone()[0] or 0

        cursor.execute("""
            SELECT AVG(CAST(DATEDIFF(day, SubmittedOn, CompletedOn) AS FLOAT))
            FROM dbo.WF_Instances
            WHERE IsDeleted = 0 AND Status IN ('Approved', 'Rejected') AND CompletedOn IS NOT NULL
        """)
        row = cursor.fetchone()
        result["kpi"]["avg_turnaround_days"] = round(row[0], 1) if row and row[0] is not None else None

        # ── Status breakdown (current snapshot, all non-deleted instances) ──
        cursor.execute("""
            SELECT Status, COUNT(*) FROM dbo.WF_Instances
            WHERE IsDeleted = 0
            GROUP BY Status
        """)
        result["status_breakdown"] = {r[0] or "Unknown": r[1] for r in cursor.fetchall()}

        # ── Monthly submitted vs approved vs rejected ────────────────────────
        cursor.execute(f"""
            SELECT YEAR(SubmittedOn) AS yr, MONTH(SubmittedOn) AS mo, COUNT(*) AS cnt
            FROM dbo.WF_Instances
            WHERE IsDeleted = 0
              AND SubmittedOn >= DATEADD(month, -{vol_months - 1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
            GROUP BY YEAR(SubmittedOn), MONTH(SubmittedOn)
            ORDER BY yr, mo
        """)
        submitted_by_month = {(r[0], r[1]): r[2] for r in cursor.fetchall()}

        cursor.execute(f"""
            SELECT YEAR(CompletedOn) AS yr, MONTH(CompletedOn) AS mo,
                   SUM(CASE WHEN Status = 'Approved' THEN 1 ELSE 0 END) AS approved,
                   SUM(CASE WHEN Status = 'Rejected' THEN 1 ELSE 0 END) AS rejected
            FROM dbo.WF_Instances
            WHERE IsDeleted = 0 AND CompletedOn IS NOT NULL
              AND CompletedOn >= DATEADD(month, -{vol_months - 1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))
            GROUP BY YEAR(CompletedOn), MONTH(CompletedOn)
            ORDER BY yr, mo
        """)
        completed_by_month = {(r[0], r[1]): {"approved": r[2] or 0, "rejected": r[3] or 0} for r in cursor.fetchall()}

        months_key = sorted(set(submitted_by_month) | set(completed_by_month))
        result["monthly"] = [{
            "year": yr, "month": mo,
            "submitted": submitted_by_month.get((yr, mo), 0),
            "approved": completed_by_month.get((yr, mo), {}).get("approved", 0),
            "rejected": completed_by_month.get((yr, mo), {}).get("rejected", 0),
        } for (yr, mo) in months_key]

        # ── Top approvers over the selected activity window ──────────────────
        cursor.execute(f"""
            SELECT TOP 10 ISNULL(u.USER_FULLNAME, u.USER_NAME) AS user_name, COUNT(*) AS cnt
            FROM dbo.WF_History wh
            LEFT JOIN dbo.Sys_User u ON u.USER_ID = wh.ActionBy
            WHERE wh.ActionType = 'APPROVED'
              AND wh.ActionOn >= DATEADD(day, -{act_days}, GETDATE())
            GROUP BY ISNULL(u.USER_FULLNAME, u.USER_NAME)
            ORDER BY cnt DESC
        """)
        result["top_approvers"] = [{"user": r[0], "count": r[1] or 0} for r in cursor.fetchall()]

        # ── Currently-pending instances, oldest first ─────────────────────────
        cursor.execute("""
            SELECT TOP 15
                wi.InstanceID,
                ISNULL(wi.Subject, '—') AS subject,
                ISNULL(u.USER_FULLNAME, u.USER_NAME) AS submitted_by,
                wi.SubmittedOn,
                wi.ExpiryDate,
                wi.Status,
                DATEDIFF(day, wi.SubmittedOn, GETDATE()) AS days_waiting
            FROM dbo.WF_Instances wi
            LEFT JOIN dbo.Sys_User u ON u.USER_ID = wi.SubmittedBy
            WHERE wi.IsDeleted = 0 AND wi.Status NOT IN ('Approved', 'Rejected', 'Draft')
            ORDER BY wi.SubmittedOn ASC
        """)
        result["pending_list"] = [{
            "id": r[0],
            "subject": r[1],
            "submitted_by": r[2],
            "submitted_on": r[3].isoformat(sep=" ") if r[3] else None,
            "expiry_date": r[4].isoformat() if r[4] else None,
            "status": r[5],
            "days_waiting": r[6] or 0,
        } for r in cursor.fetchall()]
    except Exception as exc:
        print(f"[_build_workflow_report] failed, returning partial/empty data: {exc}")
    return result


@app.route("/api/profile", methods=["GET"])
@login_required
def api_profile_get():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT USER_FULLNAME, USER_NAME, USER_EMAIL
                       FROM dbo.Sys_User
                       WHERE USER_ID = ?
                       """, session["user_id"])
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404
        return jsonify({"full_name": row[0], "username": row[1], "email": row[2]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/profile", methods=["POST"])
@login_required
def api_profile_update():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    current_password = (data.get("current_password") or "").strip()
    new_password = (data.get("password") or "").strip()

    if not email:
        return jsonify({"error": "Email is required"}), 400
    if not current_password:
        return jsonify({"error": "Current password is required"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Verify current password
        cursor.execute("""
                       SELECT USER_ID
                       FROM dbo.Sys_User
                       WHERE USER_ID = ? AND USER_PASSWORD = ?
                       """, session["user_id"], current_password)
        if not cursor.fetchone():
            return jsonify({"error": "Current password is incorrect"}), 403

        # Update email and optionally password
        if new_password:
            cursor.execute("""
                           UPDATE dbo.Sys_User
                           SET USER_EMAIL = ?, USER_PASSWORD = ?, ModifiedOn = GETDATE()
                           WHERE USER_ID = ?
                           """, email, new_password, session["user_id"])
        else:
            cursor.execute("""
                           UPDATE dbo.Sys_User
                           SET USER_EMAIL = ?, ModifiedOn = GETDATE()
                           WHERE USER_ID = ?
                           """, email, session["user_id"])

        conn.commit()
        changed = "email+password" if new_password else "email"
        audit_log("PROFILE_CHANGE", page_id=None, notes=f"User updated profile ({changed})")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/settings/email", methods=["GET"])
@login_required
def api_settings_email_get():
    """
    Returns the current user's own send-as email config (never the password).
    { smtp_email, provider, smtp_server, smtp_port, use_ssl, configured,
      graph_enabled, providers }
    graph_enabled tells the frontend whether a password/server section is
    even relevant — Microsoft Graph sends as the user without one.
    Each user's smtp_server/port/use_ssl are THEIR OWN, independent of the
    admin's shared Mail Settings — a Gmail user and an Office365 user can
    both have working personal send-as at the same time.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SmtpEmail, SmtpPasswordEnc, SmtpServer, SmtpPort, SmtpUseSSL "
            "FROM dbo.Sys_User WHERE USER_ID = ?",
            session["user_id"],
        )
        row = cursor.fetchone()
        configured = bool(row and row[0]) if GRAPH_ENABLED else bool(row and row[0] and row[1])
        smtp_server = (row[2] if row else None) or None
        return jsonify({
            "smtp_email": row[0] if row else None,
            "provider": _infer_mail_provider(smtp_server) if smtp_server else "office365",
            "smtp_server": smtp_server or "",
            "smtp_port": (row[3] if row else None) or "",
            "use_ssl": bool(row[4]) if (row and row[4] is not None) else True,
            "configured": configured,
            "graph_enabled": GRAPH_ENABLED,
            "providers": MAIL_PROVIDER_PRESETS,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/settings/email", methods=["POST"])
@login_required
def api_settings_email_update():
    """
    Body: { smtp_email, smtp_password?, provider?,
            smtp_server?, smtp_port?, use_ssl? }
    No portal login password is required here — the request is already
    authenticated via the session (@login_required), and for this
    particular setting the email/app password being entered is itself
    the sensitive credential being saved, so it stands on its own.

    provider: "gmail" | "office365" | "custom" (or omitted). When a known
    preset is given, smtp_server/smtp_port/use_ssl are taken from
    MAIL_PROVIDER_PRESETS regardless of what was passed in, so the UI
    can't drift from the backend's definition of "Gmail"/"Office 365".
    For "custom" (or no provider), smtp_server/smtp_port/use_ssl from the
    request are used, falling back to a guess from the email's domain,
    then to the server's own SMTP_SERVER default.

    Each user's server/port/ssl are stored on THEIR OWN row — completely
    independent of the admin's shared Mail Settings — so a Gmail user and
    an Office 365 user can both send successfully as themselves.

    When Microsoft Graph is configured (GRAPH_ENABLED), smtp_password and
    the server/port/ssl fields are ignored entirely — Graph sends as this
    mailbox using the app's own credentials, so no per-user password or
    SMTP host is stored or needed at all.
    Otherwise (legacy SMTP mode), smtp_password is optional on update —
    omit it to keep the existing saved password and only change other fields.
    """
    data = request.get_json(silent=True) or {}
    smtp_email = (data.get("smtp_email") or "").strip()
    smtp_password = (data.get("smtp_password") or "").strip()
    provider = (data.get("provider") or "").strip().lower()
    smtp_server_in = (data.get("smtp_server") or "").strip()
    smtp_port_in = data.get("smtp_port")
    use_ssl_in = data.get("use_ssl", True)

    if not smtp_email:
        return jsonify({"error": "Email is required"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT USER_ID, SmtpPasswordEnc FROM dbo.Sys_User WHERE USER_ID = ?",
            session["user_id"],
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404

        existing_enc = row[1]

        if not smtp_password and not existing_enc:
            if GRAPH_ENABLED:
                # No app password given, and none saved before — fine, this
                # user is relying on Graph (their mailbox is presumably
                # inside the Graph tenant). Just save the email; sending
                # will route through Graph for them per the per-user check
                # in api_email_send(). If they later add a password here,
                # their own SMTP will be used instead, automatically.
                cursor.execute(
                    "UPDATE dbo.Sys_User SET SmtpEmail = ?, SmtpPasswordEnc = NULL WHERE USER_ID = ?",
                    smtp_email, session["user_id"],
                )
            else:
                return jsonify({"error": "An email password is required for first-time setup"}), 400
        else:
            if not _fernet:
                return jsonify({
                    "error": "Per-user email is not configured on the server. "
                             "Ask an admin to set APP_ENCRYPTION_KEY in .env."
                }), 500
            password_enc = _encrypt_secret(smtp_password) if smtp_password else existing_enc

            # Resolve this user's OWN server/port/ssl — a known preset wins,
            # otherwise use what was submitted, otherwise guess from the
            # email's domain, otherwise fall back to the server default.
            preset = MAIL_PROVIDER_PRESETS.get(provider)
            if preset:
                smtp_server = preset["smtp_server"]
                smtp_port = preset["smtp_port"]
                use_ssl = preset["use_ssl"]
            else:
                smtp_server = smtp_server_in or _infer_smtp_server_for_email(smtp_email) or SMTP_SERVER
                try:
                    smtp_port = int(smtp_port_in) if smtp_port_in not in (None, "") else 587
                except (TypeError, ValueError):
                    return jsonify({"error": "SMTP port must be a number"}), 400
                use_ssl = bool(use_ssl_in)
                if not smtp_server:
                    return jsonify({
                        "error": "Couldn't determine an SMTP server for that email. "
                                 "Pick a provider, or choose Custom and enter one."
                    }), 400

            cursor.execute(
                "UPDATE dbo.Sys_User SET SmtpEmail = ?, SmtpPasswordEnc = ?, "
                "SmtpServer = ?, SmtpPort = ?, SmtpUseSSL = ? WHERE USER_ID = ?",
                smtp_email, password_enc, smtp_server, smtp_port, use_ssl, session["user_id"],
            )
        conn.commit()
        audit_log("EMAIL_SETTINGS_CHANGE", page_id=None, notes="User updated their send-as email config")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/settings/wf-expiry-alert", methods=["GET"])
@login_required
def api_settings_wf_expiry_alert_get():
    """Returns the current user's Workflow expiry-alert preference:
    { alert_days: <int|null> }. null means the user hasn't set one and
    the Archive banner stays off for them."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT WfExpiryAlertDays FROM dbo.Sys_User WHERE USER_ID = ?",
            session["user_id"],
        )
        row = cursor.fetchone()
        return jsonify({"alert_days": row[0] if row and row[0] is not None else None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/settings/wf-expiry-alert", methods=["POST"])
@login_required
def api_settings_wf_expiry_alert_update():
    """Body: { alert_days: <int|null> }. Saves to Sys_User.WfExpiryAlertDays
    for the current user. Passing null/empty turns the banner off for them."""
    data = request.get_json(silent=True) or {}
    alert_days_in = data.get("alert_days", None)
    alert_days = None
    if alert_days_in not in (None, ""):
        try:
            alert_days = int(alert_days_in)
        except (TypeError, ValueError):
            return jsonify({"error": "alert_days must be a whole number"}), 400
        if alert_days < 0 or alert_days > 365:
            return jsonify({"error": "alert_days must be between 0 and 365"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE dbo.Sys_User SET WfExpiryAlertDays = ? WHERE USER_ID = ?",
            alert_days, session["user_id"],
        )
        conn.commit()
        return jsonify({"success": True, "alert_days": alert_days})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/settings/email", methods=["DELETE"])
@login_required
def api_settings_email_delete():
    """Clears the user's personal send-as columns; they fall back to the shared mailbox."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE dbo.Sys_User SET SmtpEmail = NULL, SmtpPasswordEnc = NULL WHERE USER_ID = ?",
            session["user_id"],
        )
        conn.commit()
        audit_log("EMAIL_SETTINGS_CHANGE", page_id=None, notes="User removed their send-as email config")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/settings/email/test", methods=["POST"])
@login_required
def api_settings_email_test():
    """
    Confirms email sending actually works before the user relies on it.

    Graph mode (GRAPH_ENABLED): there's no SMTP login to dry-run, so this
    sends a real, tiny test email to the mailbox being tested — the best
    available proof it works end-to-end.

    Legacy SMTP mode: logs into O365 SMTP without sending anything, same
    as before.

    Body (optional): { smtp_email, smtp_password?, provider?, smtp_server?,
    smtp_port?, use_ssl? } to test unsaved values (smtp_password/server
    fields are ignored/unused in Graph mode). This tests against THIS
    user's own server, never the admin's shared Mail Settings server.
    """
    data = request.get_json(silent=True) or {}
    test_email = (data.get("smtp_email") or "").strip()
    test_password = (data.get("smtp_password") or "").strip()
    provider = (data.get("provider") or "").strip().lower()

    if GRAPH_ENABLED:
        if not test_email:
            test_email = _get_user_send_email(session["user_id"])
        if not test_email:
            return jsonify({"error": "No email configured to test"}), 400
        try:
            _send_graph_mail(
                subject="DocPortal System — test email",
                html_body="<p>This is a test email confirming your send-as address works.</p>",
                recipients=[test_email],
                from_email=test_email,
            )
            return jsonify({"success": True})
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception as exc:
            return jsonify({"error": f"Send failed: {exc}"}), 502

    saved_email, saved_password, saved_server, saved_port, saved_ssl = _get_user_email_config(session["user_id"])
    if not test_email:
        test_email = saved_email
    if not test_password:
        test_password = saved_password
    if not (test_email and test_password):
        return jsonify({"error": "No email configured to test"}), 400

    preset = MAIL_PROVIDER_PRESETS.get(provider)
    if preset:
        test_server, test_port, test_ssl = preset["smtp_server"], preset["smtp_port"], preset["use_ssl"]
    else:
        test_server = (data.get("smtp_server") or "").strip() or saved_server or _infer_smtp_server_for_email(test_email) or SMTP_SERVER
        try:
            test_port = int(data.get("smtp_port")) if data.get("smtp_port") not in (None, "") else (saved_port or 587)
        except (TypeError, ValueError):
            return jsonify({"error": "SMTP port must be a number"}), 400
        test_ssl = bool(data.get("use_ssl", saved_ssl if saved_ssl is not None else True))

    try:
        if test_ssl and test_port == 465:
            with smtplib.SMTP_SSL(test_server, test_port, timeout=15, context=ssl.create_default_context()) as server:
                server.login(test_email, test_password)
        elif test_ssl:
            with smtplib.SMTP(test_server, test_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(test_email, test_password)
        else:
            with smtplib.SMTP(test_server, test_port, timeout=15) as server:
                server.ehlo()
                server.login(test_email, test_password)
        return jsonify({"success": True})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"error": "Login failed — check the email/password, or that SMTP AUTH is enabled for this mailbox."}), 401
    except Exception as e:
        return jsonify({"error": f"Connection failed: {e}"}), 502


# ── ADMIN MAIL SETTINGS (Control Panel — shared/default mailbox) ─────────
# This is the SHARED mailbox every user falls back to when they haven't
# set up their own personal "Send Documents As Yourself" email. Distinct
# from /api/settings/email above, which is per-user.
#
# Storage note: piggybacks on Sys_User.SmtpEmail/SmtpPasswordEnc — the same
# columns personal per-user email uses — via the IsSharedMailAccount flag
# (see _get_shared_mail_row / get_shared_smtp_config above). Saving here
# writes onto the CURRENT ADMIN'S OWN Sys_User row and flags it as shared,
# which overwrites that admin's personal send-as email/password if they'd
# set one. The GET response includes "owner" so the UI can warn about this.

@app.route("/api/admin/mail-settings", methods=["GET"])
@login_required
def api_admin_mail_settings_get():
    """Returns the saved shared mail settings — admin only. Password is never returned."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only administrators can view mail settings."}), 403
    row = _get_shared_mail_row()
    if row:
        _, sender_email, _, smtp_server, smtp_port, use_ssl, owner_name, owner_username = row
    else:
        sender_email = smtp_server = smtp_port = None
        use_ssl = True
        owner_name = owner_username = None
    return jsonify({
        "provider": _infer_mail_provider(smtp_server),
        "smtp_server": smtp_server or "",
        "smtp_port": smtp_port or "",
        "sender_email": sender_email or "",
        "use_ssl": use_ssl if use_ssl is not None else True,
        "configured": bool(sender_email),
        "graph_enabled": GRAPH_ENABLED,
        "owner": owner_name or owner_username,
        "owner_is_me": bool(row and row[0] == session.get("user_id")),
        "providers": MAIL_PROVIDER_PRESETS,
    })


@app.route("/api/admin/mail-settings", methods=["POST"])
@login_required
def api_admin_mail_settings_set():
    """
    Body: { provider, sender_email, smtp_server?, smtp_port?, app_password?, use_ssl? }
    smtp_server/smtp_port/app_password are ignored when Microsoft Graph is
    configured (GRAPH_ENABLED) — Graph sends as sender_email using the
    app's own credentials, so only the address itself matters there.
    app_password is optional on update — omit it to keep the existing
    saved password and only change other fields.

    Writes onto the CALLING ADMIN'S OWN Sys_User row and flags it as the
    shared mailbox (clearing the flag from any other row first, so only
    one row is ever flagged at a time). This overwrites that admin's own
    personal send-as email/password if they had one — same columns are
    reused for both features.
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only administrators can change mail settings."}), 403

    data = request.get_json(silent=True) or {}
    sender_email = (data.get("sender_email") or "").strip()
    smtp_server = (data.get("smtp_server") or "").strip()
    smtp_port_raw = data.get("smtp_port")
    app_password = (data.get("app_password") or "").strip()
    use_ssl = bool(data.get("use_ssl", True))
    my_id = session["user_id"]

    if not sender_email:
        return jsonify({"error": "Sender email is required"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Keep the existing password if the admin left the field blank —
        # whether it came from this row's shared config or their own
        # personal config already saved there.
        cursor.execute("SELECT SmtpPasswordEnc FROM dbo.Sys_User WHERE USER_ID = ?", my_id)
        row = cursor.fetchone()
        existing_enc = row[0] if row else None

        smtp_port = None
        if not GRAPH_ENABLED:
            if not smtp_server:
                return jsonify({"error": "SMTP server is required"}), 400
            try:
                smtp_port = int(smtp_port_raw) if smtp_port_raw not in (None, "") else 587
            except (TypeError, ValueError):
                return jsonify({"error": "SMTP port must be a number"}), 400

            if app_password:
                if not _fernet:
                    return jsonify({
                        "error": "Mail settings encryption is not configured on the server. "
                                 "Ask an admin to set APP_ENCRYPTION_KEY in .env."
                    }), 500
                password_enc = _encrypt_secret(app_password)
            else:
                password_enc = existing_enc
                if not password_enc:
                    return jsonify({"error": "An app password is required for first-time setup"}), 400
        else:
            # Graph mode: no password stored/needed at all.
            password_enc = None
            try:
                smtp_port = int(smtp_port_raw) if smtp_port_raw not in (None, "") else None
            except (TypeError, ValueError):
                smtp_port = None

        # Only one row is ever flagged as the shared mailbox at a time.
        cursor.execute(
            "UPDATE dbo.Sys_User SET IsSharedMailAccount = 0 WHERE IsSharedMailAccount = 1 AND USER_ID <> ?",
            my_id,
        )
        cursor.execute(
            """
            UPDATE dbo.Sys_User
            SET SmtpEmail = ?, SmtpPasswordEnc = ?, SmtpServer = ?, SmtpPort = ?,
                SmtpUseSSL = ?, IsSharedMailAccount = 1
            WHERE USER_ID = ?
            """,
            sender_email, password_enc, smtp_server or None, smtp_port, use_ssl, my_id,
        )
        conn.commit()
        audit_log("MAIL_SETTINGS_CHANGE", page_id=None, notes="Admin updated shared Mail Settings")
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/mail-settings", methods=["DELETE"])
@login_required
def api_admin_mail_settings_delete():
    """
    Reverts to .env fallback by clearing the IsSharedMailAccount flag.
    Deliberately does NOT touch SmtpEmail/SmtpPasswordEnc/SmtpServer/etc on
    that row — the owning admin's personal send-as config (if it's really
    theirs) is left intact; only the "this is the shared mailbox" marker
    is removed.
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only administrators can change mail settings."}), 403
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE dbo.Sys_User SET IsSharedMailAccount = 0 WHERE IsSharedMailAccount = 1")
        conn.commit()
        audit_log("MAIL_SETTINGS_CHANGE", page_id=None, notes="Admin cleared shared Mail Settings (reverted to .env)")
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/mail-settings/test", methods=["POST"])
@login_required
def api_admin_mail_settings_test():
    """
    Sends a real test email to sender_email itself, proving the config
    actually works end-to-end (not just a login dry-run).
    Body (optional): unsaved { sender_email, smtp_server, smtp_port,
    app_password, use_ssl } to test values before saving. Falls back to
    the already-saved (flagged row's) settings for any field left out.
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only administrators can test mail settings."}), 403

    data = request.get_json(silent=True) or {}
    saved = _get_shared_mail_row()  # (id, email, pw_enc, server, port, ssl, name, username) or None

    test_email = (data.get("sender_email") or "").strip() or (saved[1] if saved else "") or ""
    if not test_email:
        return jsonify({"error": "Enter a sender email to test"}), 400

    if GRAPH_ENABLED:
        try:
            _send_graph_mail(
                subject="DocPortal System — Mail Settings test",
                html_body="<p>This is a test email confirming your shared Mail Settings work.</p>",
                recipients=[test_email],
                from_email=test_email,
            )
            return jsonify({"success": True})
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except Exception as exc:
            return jsonify({"error": f"Send failed: {exc}"}), 502

    test_server = (data.get("smtp_server") or "").strip() or (saved[3] if saved else "") or SMTP_SERVER
    test_port_raw = data.get("smtp_port")
    try:
        test_port = int(test_port_raw) if test_port_raw not in (None, "") else int((saved[4] if saved else None) or SMTP_PORT)
    except (TypeError, ValueError):
        return jsonify({"error": "SMTP port must be a number"}), 400
    saved_ssl = bool(saved[5]) if (saved and saved[5] is not None) else True
    test_use_ssl = bool(data.get("use_ssl", saved_ssl))

    test_password = (data.get("app_password") or "").strip()
    if not test_password and saved and saved[2]:
        test_password = _decrypt_secret(saved[2])
    if not test_password:
        return jsonify({"error": "Enter an app password to test (or save one first)"}), 400

    try:
        _send_smtp_mail(
            subject="DocPortal System — Mail Settings test",
            text_body="This is a test email confirming your shared Mail Settings work.",
            html_body="<p>This is a test email confirming your shared Mail Settings work.</p>",
            recipients=[test_email],
            from_email=test_email,
            from_password=test_password,
            smtp_server=test_server,
            smtp_port=test_port,
            use_ssl=test_use_ssl,
        )
        return jsonify({"success": True})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"error": "Login failed — check the sender email/app password, or that SMTP AUTH is enabled."}), 401
    except Exception as exc:
        return jsonify({"error": f"Send failed: {exc}"}), 502


@app.route("/api/admin/workflow-settings", methods=["GET"])
@login_required
def api_admin_workflow_settings_get():
    """Admin only: current minimum-approvals requirement for workflow
    transactions (Control Panel -> Workflow Settings)."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: admin only"}), 403
    try:
        _ensure_workflow_tables()
        return jsonify({"min_approvals": get_wf_min_approvals()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/admin/workflow-settings", methods=["POST"])
@login_required
def api_admin_workflow_settings_set():
    """Admin only: sets the minimum number of distinct approvers a workflow
    transaction needs before it's considered fully approved. Approvers keep
    getting the "you need to send to other user(s) as well" error on
    /approve until this many distinct users have approved the chain."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: admin only"}), 403
    data = request.get_json(silent=True) or {}
    try:
        min_approvals = int(data.get("min_approvals"))
    except (TypeError, ValueError):
        return jsonify({"error": "Minimum approvals must be a whole number."}), 400
    if min_approvals < 1:
        return jsonify({"error": "Minimum approvals must be at least 1."}), 400

    conn = None
    try:
        _ensure_workflow_tables()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM dbo.WF_Config WHERE ConfigKey = 'MIN_APPROVALS'")
        if cursor.fetchone():
            cursor.execute(
                "UPDATE dbo.WF_Config SET ConfigValue = ?, ModifiedBy = ?, ModifiedOn = GETDATE() WHERE ConfigKey = 'MIN_APPROVALS'",
                str(min_approvals), session["user_id"],
            )
        else:
            cursor.execute(
                "INSERT INTO dbo.WF_Config (ConfigKey, ConfigValue, ModifiedBy, ModifiedOn) VALUES ('MIN_APPROVALS', ?, ?, GETDATE())",
                str(min_approvals), session["user_id"],
            )
        conn.commit()
        audit_log("WF_SETTINGS_CHANGE", page_id=WF_PAGE_ID,
                  notes=f"Admin set minimum workflow approvals to {min_approvals}")
        return jsonify({"success": True, "min_approvals": min_approvals})
    except Exception as exc:
        if conn:
            conn.rollback()
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/admin/users")
@login_required
def api_admin_list_users():
    """Admin only: list all non-deleted users for the control panel."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT USER_ID, USER_FULLNAME, USER_NAME, USER_TYPE_ID, Dep_ID, Dep_ID_From
            FROM dbo.Sys_User
            WHERE IsDeleted = 0
            ORDER BY USER_FULLNAME
        """)
        rows = cursor.fetchall()
        return jsonify([{
            "id": r[0],
            "full_name": r[1] or r[2],
            "username": r[2],
            "user_type": r[3],
            "dep_id": r[4],
            "dep_id_from": r[5] or "",
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/users/<int:user_id>/permissions", methods=["GET"])
@login_required
def api_admin_get_user_perms(user_id):
    """Admin only: get the list of department IDs a user currently has access to."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT Dep_ID_From FROM dbo.Sys_User WHERE USER_ID = ? AND IsDeleted = 0",
            user_id
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404
        allowed = sorted(parse_dep_id_from(row[0]))
        return jsonify({"user_id": user_id, "allowed_dep_ids": allowed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/users/<int:user_id>/permissions", methods=["POST"])
@login_required
def api_admin_set_user_perms(user_id):
    """
    Admin only: set the full list of department IDs a user can access.
    Body: { "dep_ids": [46, 53, 60] }
    Writes the comma-separated value back to Sys_User.Dep_ID_From.
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = None
    try:
        data = request.get_json() or {}
        dep_ids = data.get("dep_ids", [])
        # Validate: must be a list of ints
        dep_ids = [int(d) for d in dep_ids if str(d).strip().isdigit() or isinstance(d, int)]
        dep_ids_str = ",".join(str(d) for d in sorted(set(dep_ids)))

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT USER_ID FROM dbo.Sys_User WHERE USER_ID = ? AND IsDeleted = 0",
            user_id
        )
        if not cursor.fetchone():
            return jsonify({"error": "User not found"}), 404

        cursor.execute(
            "UPDATE dbo.Sys_User SET Dep_ID_From = ?, ModifiedBy = ?, ModifiedOn = GETDATE() WHERE USER_ID = ?",
            dep_ids_str, session["user_id"], user_id
        )
        conn.commit()
        return jsonify({
            "success": True,
            "user_id": user_id,
            "dep_ids": dep_ids,
            "dep_id_from": dep_ids_str,
        })
    except Exception as e:
        if conn:
            try: conn.rollback()
            except: pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/users/create", methods=["POST"])
@login_required
def api_admin_create_user():
    """Admin only: create a new user in Sys_User."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email     = (data.get("email")     or "").strip()
    username  = (data.get("username")  or "").strip()
    password  = (data.get("password")  or "").strip()
    dep_id    = int(data.get("dep_id") or 46)

    if not full_name: return jsonify({"error": "Full name is required"}), 400
    if not email:     return jsonify({"error": "Email is required"}), 400
    if not username:  return jsonify({"error": "Username is required"}), 400
    if not password:  return jsonify({"error": "Password is required"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check username not already taken
        cursor.execute("SELECT USER_ID FROM dbo.Sys_User WHERE USER_NAME = ? AND IsDeleted = 0", username)
        if cursor.fetchone():
            return jsonify({"error": f"Username '{username}' is already taken"}), 409

        # Get next USER_TYPE_ID
        cursor.execute("SELECT ISNULL(MAX(USER_TYPE_ID), 0) + 1 FROM dbo.Sys_User")
        next_type_id = cursor.fetchone()[0]

        cursor.execute("""
            INSERT INTO dbo.Sys_User
                (USER_FULLNAME, USER_EMAIL, USER_NAME, USER_PASSWORD,
                 USER_TYPE_ID, Dep_ID, Dep_ID_From, Emp_ID,
                 Enable, Groups_ID, CreatedBy, CreatedOn, IsDeleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, 2, ?, GETDATE(), 0)
        """, full_name, email, username, password,
             next_type_id, dep_id, str(dep_id), session["user_id"])
        conn.commit()
        audit_log("USER_CREATE", page_id=None, notes=f"Created user '{username}' ({full_name})")
        return jsonify({"success": True})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except: pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/users/<int:user_id>/terminate", methods=["POST"])
@login_required
def api_admin_terminate_user(user_id):
    """Admin only: soft-delete a user by setting IsDeleted = 1."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    if user_id == session["user_id"]:
        return jsonify({"error": "You cannot terminate your own account"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT USER_NAME FROM dbo.Sys_User WHERE USER_ID = ? AND IsDeleted = 0", user_id
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404
        if (row[0] or '').lower() == 'admin':
            return jsonify({"error": "Cannot terminate the admin account"}), 403

        # Don't leave any in-flight workflow instance stuck with no one able
        # to act on it: if this user is the (or an) assignee on a step that's
        # still 'Waiting', block termination until an admin reassigns or
        # resolves those first. Without this check, terminating a user whose
        # approval a document is waiting on permanently freezes that document
        # — nothing else can move it out of Pending Approval/In Progress.
        cursor.execute(
            """
            SELECT wi.InstanceID, COALESCE(wi.Subject, t.Subject) AS subject
            FROM dbo.WF_Instance_Assignments wa
            JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
            JOIN dbo.WF_Instances wi ON wi.InstanceID = wis.InstanceID
            LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
            WHERE wa.AssignedTo = ? AND wa.Status = 'Waiting' AND wi.IsDeleted = 0
            ORDER BY wi.InstanceID
            """,
            user_id,
        )
        pending = cursor.fetchall()
        if pending:
            pending_list = [
                {"instance_id": r[0], "subject": r[1] or f"Instance #{r[0]}"}
                for r in pending
            ]
            return jsonify({
                "error": (
                    f"Cannot terminate '{row[0]}': they still have {len(pending_list)} "
                    "pending workflow approval(s) assigned to them. Reassign or "
                    "resolve those first, otherwise the document(s) below can "
                    "never move forward."
                ),
                "pending_approvals": pending_list,
            }), 409

        cursor.execute(
            "UPDATE dbo.Sys_User SET IsDeleted = 1, ModifiedBy = ?, ModifiedOn = GETDATE() WHERE USER_ID = ?",
            session["user_id"], user_id
        )
        conn.commit()
        audit_log("USER_TERMINATE", page_id=None, notes=f"Terminated user ID {user_id} ('{row[0]}')")
        return jsonify({"success": True})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except: pass
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()



# ── USER AUDIT LOG (Sys_AuditLog) ────────────────────────────────────────────
# Action types:  LOGIN, LOGOUT, VIEW, SEARCH, ADD, EDIT, DELETE,
#                PREVIEW, DOWNLOAD, PRINT, ACCR_CHANGE, USER_CREATE, USER_TERMINATE
# PAGE_IDs mirror Sys_AccR: 1=Inquiries, 2=Archive, 3=Folder Browser, NULL=system

# Sys_AuditLog.Action_At is written with SQL Server's GETDATE(), which
# returns the DB server's local clock — this server runs on UTC, but the
# Control Panel's Activity Log is viewed by users in Riyadh (UTC+3), so the
# raw timestamps read a few hours behind wall-clock time. Since DATETIME2
# columns aren't timezone-aware, we can't fix this at the DB layer — instead
# we shift the timestamp forward by this fixed offset only when formatting
# it for display. Adjust this single constant if the DB server's clock is
# ever moved to a different timezone.
AUDIT_LOG_DISPLAY_TZ_OFFSET_HOURS = 3


def _fmt_audit_time(dt):
    """Format a Sys_AuditLog.Action_At value for display, shifted from the
    DB server's clock (UTC) to Riyadh local time (UTC+3)."""
    if not dt:
        return None
    return (dt + timedelta(hours=AUDIT_LOG_DISPLAY_TZ_OFFSET_HOURS)).isoformat(sep=" ")


def _ensure_audit_table():
    """Create Sys_AuditLog if it doesn't exist. Safe to call on every startup."""
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        # Match USER_ID type to Sys_User exactly
        cur.execute("""
            SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_User' AND COLUMN_NAME='USER_ID'
        """)
        row = cur.fetchone()
        if row and row[0].upper() in ('NVARCHAR', 'VARCHAR', 'CHAR', 'NCHAR'):
            length = row[1] if row[1] and row[1] > 0 else 50
            uid_type = f"{row[0].upper()}({length})"
        elif row and row[0].upper() == 'BIGINT':
            uid_type = "BIGINT"
        else:
            uid_type = "INT"

        cur.execute(f"""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AuditLog'
            )
            BEGIN
                CREATE TABLE dbo.Sys_AuditLog (
                    ID          BIGINT IDENTITY(1,1) PRIMARY KEY,
                    USER_ID     {uid_type}    NULL,
                    PAGE_ID     INT           NULL,
                    Action_Type NVARCHAR(50)  NOT NULL,
                    Action_At   DATETIME2     NOT NULL DEFAULT GETDATE(),
                    Notes       NVARCHAR(500) NULL,
                    IP_Address  NVARCHAR(45)  NULL
                )
            END
        """)
        # Migration: add IP_Address to tables created before this column existed
        cur.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AuditLog'
                  AND COLUMN_NAME='IP_Address'
            )
            BEGIN
                ALTER TABLE dbo.Sys_AuditLog
                    ADD IP_Address NVARCHAR(45) NULL
            END
        """)
        print("[AuditLog] Sys_AuditLog ready.")
    except Exception as exc:
        print(f"[AuditLog] ERROR creating table: {exc}")
    finally:
        conn.close()


def _get_client_ip() -> str | None:
    """
    Extract the real client IP from the current request context.
    Respects X-Forwarded-For / X-Real-IP when behind a reverse proxy;
    falls back to request.remote_addr.  Returns None outside a request context.
    """
    try:
        # X-Forwarded-For may be a comma-separated list; first entry is the client
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
        return request.remote_addr
    except Exception:
        return None


def _log_chatbot_unmatched(message: str, lang: str, bucket: str):
    """
    Record a chatbot message that fell through to the generic help or
    fallback reply — i.e. nothing in _CHATBOT_FAQ_TOPICS or any intent
    trigger recognized it. Reuses Sys_AuditLog (no new table needed) so
    these rows show up alongside other activity and can be queried the
    same way (e.g. WHERE Action_Type = 'CHATBOT_UNMATCHED'). bucket is
    'help' (hit the keyword-triggered generic help menu) or 'fallback'
    (matched nothing at all, not even 'help'). Fire-and-forget — audit_log
    already inserts on a background thread, so this never adds latency
    to the chatbot response.
    """
    # Truncate defensively — Notes is NVARCHAR(500) and we prefix a tag.
    trimmed = (message or "")[:400]
    audit_log(
        "CHATBOT_UNMATCHED",
        page_id=None,
        notes=f"[{bucket}|{lang}] {trimmed}",
    )


def audit_log(action_type: str, page_id=None, notes: str = None, user_id=None, ip: str = None):
    """
    Insert one audit row in a background thread so it never blocks a request.
    user_id defaults to the current session user if not supplied.
    ip defaults to the client IP inferred from the current request context;
    pass ip='' to explicitly store NULL (e.g. background/system events).
    """
    # Grab user_id and IP while still in request context
    if user_id is None:
        try:
            user_id = session.get("user_id")
        except Exception:
            user_id = None

    if ip is None:
        ip = _get_client_ip()

    def _insert(uid, pid, atype, note, client_ip):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO dbo.Sys_AuditLog
                    (USER_ID, PAGE_ID, Action_Type, Action_At, Notes, IP_Address)
                VALUES (?, ?, ?, GETDATE(), ?, ?)
                """,
                uid, pid, atype, (note or None), (client_ip or None)
            )
            conn.commit()
        except Exception as exc:
            print(f"[AuditLog] insert failed ({atype}): {exc}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=_insert, args=(user_id, page_id, action_type, notes, ip), daemon=True).start()


# ── NOTIFICATIONS (dbo.Sys_Notifications_Mang — pre-existing table) ─────────
# This table already exists in the database, so no schema changes are made.
# Its columns are repurposed like this for document add/edit/delete events:
#   Name              -> the human-readable notification message
#   User_ID           -> the recipient
#   Cat_ID            -> the related document (Adco_Transactions.ID)
#   Condition_Others  -> the action type: 'ADD' | 'EDIT' | 'DELETE'
#   Ahead_Of          -> unused here, always 0
#   CreatedBy         -> whoever performed the action
#   CreatedOn         -> when it happened
#   ModifiedBy/On     -> set when the recipient marks it read
#   IsDeleted         -> doubles as the "read" flag (0 = unread, 1 = read)

def _get_dept_notify_user_ids(cursor, dept_id, exclude_user_id=None):
    """
    Returns USER_IDs that should be notified about an event in Sys_Department
    ID = dept_id: anyone with dept_id listed in their Dep_ID_From, plus the
    admin account (USER_ID = 1), excluding exclude_user_id (the actor).
    """
    if not dept_id:
        return []
    user_ids = set()
    try:
        target = int(dept_id)
        cursor.execute("SELECT USER_ID, Dep_ID_From FROM dbo.Sys_User WHERE IsDeleted = 0")
        for uid, dep_from in cursor.fetchall():
            if target in parse_dep_id_from(dep_from) or uid == 1:
                user_ids.add(uid)
    except Exception as exc:
        print(f"[Notifications] recipient lookup failed: {exc}")
    if exclude_user_id in user_ids:
        user_ids.discard(exclude_user_id)
    return list(user_ids)


def notify_dept_users(dept_id, action_type, doc_id, subject=None, actor_id=None):
    """
    Fire-and-forget notification fan-out into dbo.Sys_Notifications_Mang.
    Inserts one row per recipient with access to dept_id, except the actor.
    Runs in a background thread (same pattern as audit_log) so it never
    blocks the request.
    """
    if actor_id is None:
        try:
            actor_id = session.get("user_id")
        except Exception:
            actor_id = None

    if not dept_id:
        print(f"[Notifications] SKIPPED — no dept_id for doc {doc_id} ({action_type})")
        return

    # Store the raw subject only (or NULL) — NOT a pre-worded English sentence.
    # The client builds the actual localized sentence from this + the
    # Condition_Others action type, so the same row renders correctly in
    # both English and Arabic depending on the viewer's language setting.
    short_subject = (subject or "").strip()
    if len(short_subject) > 150:
        short_subject = short_subject[:150] + "…"
    name_value = short_subject or None

    def _insert():
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            recipients = _get_dept_notify_user_ids(cursor, dept_id, exclude_user_id=actor_id)
            print(f"[Notifications] dept={dept_id} actor={actor_id} action={action_type} "
                  f"doc={doc_id} -> recipients={recipients}")
            if not recipients:
                print(f"[Notifications] SKIPPED — no recipients after excluding actor {actor_id}. "
                      f"Check that another user (not just USER_ID=1) has {dept_id} in Dep_ID_From.")
            for uid in recipients:
                cursor.execute(
                    """
                    INSERT INTO dbo.Sys_Notifications_Mang
                        (Name, User_ID, Cat_ID, Condition_Others, Ahead_Of,
                         CreatedBy, CreatedOn, IsDeleted)
                    VALUES (?, ?, ?, ?, 0, ?, GETDATE(), 0)
                    """,
                    name_value, uid, doc_id, action_type, actor_id,
                )
                sio_emit("notification", {
                    "action_type": action_type, "doc_id": doc_id, "subject": name_value,
                }, room=_room_for_user(uid))
            conn.commit()
        except Exception as exc:
            import traceback
            print(f"[Notifications] INSERT FAILED ({action_type} doc {doc_id}): {exc}")
            traceback.print_exc()
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=_insert, daemon=True).start()


# ── GROUP MESSAGING ───────────────────────────────────────────────────────
def _ensure_messaging_tables():
    """Create the group-messaging tables if they don't exist yet."""
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_MsgGroup')
            BEGIN
                CREATE TABLE dbo.Sys_MsgGroup (
                    ID          INT IDENTITY(1,1) PRIMARY KEY,
                    NAME        NVARCHAR(150)  NOT NULL,
                    CREATED_BY  INT            NOT NULL,
                    CREATED_AT  DATETIME       NOT NULL DEFAULT GETDATE(),
                    IS_DELETED  BIT            NOT NULL DEFAULT 0
                )
            END
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_MsgGroupMember')
            BEGIN
                CREATE TABLE dbo.Sys_MsgGroupMember (
                    ID        INT IDENTITY(1,1) PRIMARY KEY,
                    GROUP_ID  INT NOT NULL,
                    USER_ID   INT NOT NULL,
                    LAST_READ_MSG_ID INT NOT NULL DEFAULT 0,
                    JOINED_AT DATETIME NOT NULL DEFAULT GETDATE()
                )
            END
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_Msg')
            BEGIN
                CREATE TABLE dbo.Sys_Msg (
                    ID        INT IDENTITY(1,1) PRIMARY KEY,
                    GROUP_ID  INT NOT NULL,
                    USER_ID   INT NOT NULL,
                    BODY      NVARCHAR(MAX) NOT NULL,
                    CREATED_AT DATETIME NOT NULL DEFAULT GETDATE()
                )
            END
        """)
        # Editing / soft-delete / reply-to support
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.Sys_Msg') AND name = 'IS_EDITED')
                ALTER TABLE dbo.Sys_Msg ADD IS_EDITED BIT NOT NULL DEFAULT 0
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.Sys_Msg') AND name = 'EDITED_AT')
                ALTER TABLE dbo.Sys_Msg ADD EDITED_AT DATETIME NULL
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.Sys_Msg') AND name = 'IS_DELETED')
                ALTER TABLE dbo.Sys_Msg ADD IS_DELETED BIT NOT NULL DEFAULT 0
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('dbo.Sys_Msg') AND name = 'REPLY_TO_MSG_ID')
                ALTER TABLE dbo.Sys_Msg ADD REPLY_TO_MSG_ID INT NULL
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_MsgAttachment')
            BEGIN
                CREATE TABLE dbo.Sys_MsgAttachment (
                    ID          INT IDENTITY(1,1) PRIMARY KEY,
                    MSG_ID      INT           NOT NULL,
                    ATT_TYPE    NVARCHAR(10)  NOT NULL,   -- 'upload' or 'archive'
                    FILE_NAME   NVARCHAR(300) NOT NULL,
                    FILE_PATH   NVARCHAR(500) NULL,       -- relative path under UPLOAD_DIR, for 'upload'
                    DOC_ID      INT           NULL,        -- Adco_Transactions.ID, for 'archive'
                    UPLOADED_BY INT           NOT NULL,
                    CREATED_AT  DATETIME      NOT NULL DEFAULT GETDATE()
                )
            END
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_MsgReaction')
            BEGIN
                CREATE TABLE dbo.Sys_MsgReaction (
                    ID         INT IDENTITY(1,1) PRIMARY KEY,
                    MSG_ID     INT           NOT NULL,
                    USER_ID    INT           NOT NULL,
                    EMOJI      NVARCHAR(10)  NOT NULL,
                    CREATED_AT DATETIME      NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT UQ_MsgReaction UNIQUE (MSG_ID, USER_ID, EMOJI)
                )
            END
        """)
        cur.execute("""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_MsgPin')
            BEGIN
                CREATE TABLE dbo.Sys_MsgPin (
                    ID         INT IDENTITY(1,1) PRIMARY KEY,
                    GROUP_ID   INT      NOT NULL,
                    MSG_ID     INT      NOT NULL,
                    PINNED_BY  INT      NOT NULL,
                    PINNED_AT  DATETIME NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT UQ_MsgPin UNIQUE (GROUP_ID, MSG_ID)
                )
            END
        """)
        conn.commit()
    finally:
        conn.close()


# In-memory "user is typing" presence: { group_id: { user_id: last_ping_epoch_seconds } }
# Entries older than TYPING_TTL_SECONDS are treated as stale and ignored/pruned.
_typing_state = {}
_typing_lock = threading.Lock()
TYPING_TTL_SECONDS = 6


def _set_typing(group_id, user_id):
    with _typing_lock:
        _typing_state.setdefault(group_id, {})[user_id] = time.time()


def _get_typing_users(group_id, exclude_user_id):
    now = time.time()
    with _typing_lock:
        entries = _typing_state.get(group_id, {})
        active = [uid for uid, ts in list(entries.items())
                  if uid != exclude_user_id and now - ts <= TYPING_TTL_SECONDS]
        # opportunistically prune stale entries
        for uid, ts in list(entries.items()):
            if now - ts > TYPING_TTL_SECONDS:
                entries.pop(uid, None)
    return active


def _user_in_group(cursor, group_id, user_id):
    cursor.execute(
        "SELECT 1 FROM dbo.Sys_MsgGroupMember WHERE GROUP_ID=? AND USER_ID=?",
        group_id, user_id,
    )
    return cursor.fetchone() is not None


def _get_reactions_for_msgs(cursor, msg_ids, uid):
    """Returns {msg_id: [{emoji, count, mine}, ...]} for the given message ids."""
    if not msg_ids:
        return {}
    placeholders = ",".join("?" * len(msg_ids))
    cursor.execute(f"""
        SELECT MSG_ID, EMOJI, COUNT(*) AS cnt,
               SUM(CASE WHEN USER_ID = ? THEN 1 ELSE 0 END) AS mine
        FROM dbo.Sys_MsgReaction
        WHERE MSG_ID IN ({placeholders})
        GROUP BY MSG_ID, EMOJI
    """, uid, *msg_ids)
    out = {}
    for msg_id, emoji, cnt, mine in cursor.fetchall():
        out.setdefault(msg_id, []).append({"emoji": emoji, "count": cnt, "mine": bool(mine)})
    return out


@app.route("/api/messages/groups", methods=["GET"])
@login_required
def api_msg_list_groups():
    """Groups the current user belongs to, with last message preview and unread count."""
    conn = None
    try:
        _ensure_messaging_tables()
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        cursor.execute("""
            SELECT g.ID, g.NAME, m.LAST_READ_MSG_ID, g.CREATED_BY
            FROM dbo.Sys_MsgGroup g
            JOIN dbo.Sys_MsgGroupMember m ON m.GROUP_ID = g.ID
            WHERE m.USER_ID = ? AND g.IS_DELETED = 0
        """, uid)
        groups = cursor.fetchall()
        result = []
        for gid, name, last_read, created_by in groups:
            cursor.execute("""
                SELECT TOP 1 sm.BODY, sm.CREATED_AT, sm.USER_ID, u.USER_FULLNAME, u.USER_NAME
                FROM dbo.Sys_Msg sm
                LEFT JOIN dbo.Sys_User u ON u.USER_ID = sm.USER_ID
                WHERE sm.GROUP_ID = ?
                ORDER BY sm.ID DESC
            """, gid)
            last = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) FROM dbo.Sys_Msg WHERE GROUP_ID=? AND ID > ?",
                gid, last_read,
            )
            unread = cursor.fetchone()[0]
            cursor.execute(
                "SELECT USER_ID FROM dbo.Sys_MsgGroupMember WHERE GROUP_ID=?", gid
            )
            member_ids = [r[0] for r in cursor.fetchall()]
            result.append({
                "id": gid,
                "name": name,
                "member_ids": member_ids,
                "created_by": created_by,
                "is_creator": created_by == uid,
                "unread_count": unread,
                "last_message": {
                    "body": last[0], "created_at": last[1].isoformat() if last and last[1] else None,
                    "user_id": last[2], "sender": (last[3] or last[4] or "") if last else "",
                } if last else None,
            })
        result.sort(key=lambda g: g["last_message"]["created_at"] if g["last_message"] else "", reverse=True)
        return jsonify({"groups": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups", methods=["POST"])
@login_required
def api_msg_create_group():
    """Create a new group chat. Body: {name, member_ids: [...]}"""
    conn = None
    try:
        _ensure_messaging_tables()
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        member_ids = data.get("member_ids") or []
        uid = session["user_id"]
        member_ids = sorted(set(int(m) for m in member_ids if str(m).strip()) | {uid})
        if len(member_ids) < 2:
            return jsonify({"error": "Select at least one other member."}), 400
        if not name:
            name = "Group Chat"

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO dbo.Sys_MsgGroup (NAME, CREATED_BY) OUTPUT INSERTED.ID VALUES (?, ?)",
            name, uid,
        )
        group_id = cursor.fetchone()[0]
        for mid in member_ids:
            cursor.execute(
                "INSERT INTO dbo.Sys_MsgGroupMember (GROUP_ID, USER_ID) VALUES (?, ?)",
                group_id, mid,
            )
        conn.commit()
        audit_log("MSG_GROUP_CREATE", notes=f"Created group '{name}' (ID {group_id}) with members {member_ids}")
        return jsonify({"success": True, "group_id": group_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>", methods=["DELETE"])
@login_required
def api_msg_delete_group(group_id):
    """Delete (soft-delete) a group chat. Only the user who created the group may do this."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        cursor.execute("SELECT CREATED_BY, NAME FROM dbo.Sys_MsgGroup WHERE ID=? AND IS_DELETED=0", group_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Group not found"}), 404
        created_by, group_name = row
        if created_by != uid:
            return jsonify({"error": "Only the group creator can delete this group."}), 403
        cursor.execute("UPDATE dbo.Sys_MsgGroup SET IS_DELETED=1 WHERE ID=?", group_id)
        conn.commit()
        audit_log("MSG_GROUP_DELETE", notes=f"Deleted group '{group_name}' (ID {group_id})")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/members", methods=["POST"])
@login_required
def api_msg_add_members(group_id):
    """Add members to an existing group. Only the user who created the group
    may add new members. Body: {member_ids: [...]}"""
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        new_ids = [int(m) for m in (data.get("member_ids") or []) if str(m).strip()]
        if not new_ids:
            return jsonify({"error": "No members selected"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]

        cursor.execute("SELECT CREATED_BY, NAME FROM dbo.Sys_MsgGroup WHERE ID=? AND IS_DELETED=0", group_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Group not found"}), 404
        created_by, group_name = row
        if created_by != uid:
            return jsonify({"error": "Only the group creator can add members."}), 403

        added = []
        for mid in new_ids:
            if not _user_in_group(cursor, group_id, mid):
                cursor.execute(
                    "INSERT INTO dbo.Sys_MsgGroupMember (GROUP_ID, USER_ID) VALUES (?, ?)",
                    group_id, mid,
                )
                added.append(mid)
        conn.commit()
        if added:
            audit_log("MSG_GROUP_ADD_MEMBER", notes=f"Added users {added} to group '{group_name}' (ID {group_id})")
        return jsonify({"success": True, "added": added})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/members/<int:user_id>", methods=["DELETE"])
@login_required
def api_msg_remove_member(group_id, user_id):
    """Remove a member from an existing group. Only the user who created the
    group may remove members, and the creator cannot remove themselves
    (they can delete the group instead)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]

        cursor.execute("SELECT CREATED_BY, NAME FROM dbo.Sys_MsgGroup WHERE ID=? AND IS_DELETED=0", group_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Group not found"}), 404
        created_by, group_name = row
        if created_by != uid:
            return jsonify({"error": "Only the group creator can remove members."}), 403
        if user_id == created_by:
            return jsonify({"error": "The group creator cannot be removed."}), 400
        if not _user_in_group(cursor, group_id, user_id):
            return jsonify({"error": "That user is not in this group."}), 404

        cursor.execute(
            "DELETE FROM dbo.Sys_MsgGroupMember WHERE GROUP_ID=? AND USER_ID=?",
            group_id, user_id,
        )
        conn.commit()
        audit_log("MSG_GROUP_REMOVE_MEMBER", notes=f"Removed user {user_id} from group '{group_name}' (ID {group_id})")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/messages", methods=["GET"])
@login_required
def api_msg_list_messages(group_id):
    """Messages in a group, optionally only those after ?since_id=, and marks them read.
    ?since_ts= (ISO timestamp) additionally returns any messages edited/deleted
    since that time, even if their ID is older than since_id, so the client can
    patch already-rendered bubbles."""
    conn = None
    try:
        _ensure_messaging_tables()
        since_id = int(request.args.get("since_id", 0))
        since_ts_raw = request.args.get("since_ts")
        limit = int(request.args.get("limit", 100))
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403

        cursor.execute(f"""
            SELECT TOP {limit} sm.ID, sm.USER_ID, u.USER_FULLNAME, u.USER_NAME, sm.BODY, sm.CREATED_AT,
                   sm.IS_EDITED, sm.EDITED_AT, sm.IS_DELETED, sm.REPLY_TO_MSG_ID
            FROM dbo.Sys_Msg sm
            LEFT JOIN dbo.Sys_User u ON u.USER_ID = sm.USER_ID
            WHERE sm.GROUP_ID = ? AND sm.ID > ?
            ORDER BY sm.ID ASC
        """, group_id, since_id)
        rows = cursor.fetchall()
        items = [{
            "id": r[0], "user_id": r[1], "sender": r[2] or r[3] or "",
            "body": "" if r[8] else r[4], "created_at": r[5].isoformat() if r[5] else None,
            "is_mine": r[1] == uid, "attachments": [],
            "is_edited": bool(r[6]), "edited_at": r[7].isoformat() if r[7] else None,
            "is_deleted": bool(r[8]), "reply_to_msg_id": r[9], "reply_to": None,
        } for r in rows]

        reply_ids = {it["reply_to_msg_id"] for it in items if it["reply_to_msg_id"]}
        if reply_ids:
            placeholders = ",".join("?" * len(reply_ids))
            cursor.execute(f"""
                SELECT sm.ID, u.USER_FULLNAME, u.USER_NAME, sm.BODY, sm.IS_DELETED
                FROM dbo.Sys_Msg sm
                LEFT JOIN dbo.Sys_User u ON u.USER_ID = sm.USER_ID
                WHERE sm.ID IN ({placeholders})
            """, *reply_ids)
            by_id = {r[0]: r for r in cursor.fetchall()}
            for it in items:
                rid = it["reply_to_msg_id"]
                if rid and rid in by_id:
                    r = by_id[rid]
                    snippet = "Message deleted" if r[4] else (r[3] or "")[:80]
                    it["reply_to"] = {"id": rid, "sender": r[1] or r[2] or "", "snippet": snippet}

        if items:
            msg_ids = [it["id"] for it in items if not it["is_deleted"]]
            if msg_ids:
                placeholders = ",".join("?" * len(msg_ids))
                cursor.execute(f"""
                    SELECT a.ID, a.MSG_ID, a.ATT_TYPE, a.FILE_NAME, a.DOC_ID, t.Subject
                    FROM dbo.Sys_MsgAttachment a
                    LEFT JOIN dbo.Adco_Transactions t ON t.ID = a.DOC_ID
                    WHERE a.MSG_ID IN ({placeholders})
                    ORDER BY a.ID ASC
                """, *msg_ids)
                by_msg = {}
                for a in cursor.fetchall():
                    by_msg.setdefault(a[1], []).append({
                        "id": a[0], "type": a[2], "file_name": a[3],
                        "doc_id": a[4],
                        "doc_reg_number": str(a[4]) if a[4] else None,
                        "doc_subject": a[5],
                    })
                for it in items:
                    it["attachments"] = by_msg.get(it["id"], [])

            cursor.execute(
                "UPDATE dbo.Sys_MsgGroupMember SET LAST_READ_MSG_ID=? WHERE GROUP_ID=? AND USER_ID=?",
                items[-1]["id"], group_id, uid,
            )
            conn.commit()

        # Messages edited/deleted since since_ts, so bubbles already on screen
        # (older than since_id) can be patched in place.
        updated_messages = []
        if since_ts_raw:
            try:
                since_ts = datetime.fromisoformat(since_ts_raw.replace("Z", "+00:00"))
                cursor.execute("""
                    SELECT ID, BODY, IS_EDITED, EDITED_AT, IS_DELETED
                    FROM dbo.Sys_Msg
                    WHERE GROUP_ID = ? AND ID <= ? AND EDITED_AT IS NOT NULL AND EDITED_AT > ?
                """, group_id, since_id, since_ts)
                updated_messages = [{
                    "id": r[0], "body": "" if r[4] else r[1],
                    "is_edited": bool(r[2]), "edited_at": r[3].isoformat() if r[3] else None,
                    "is_deleted": bool(r[4]),
                } for r in cursor.fetchall()]
            except ValueError:
                pass

        # Read receipts: every other member's furthest-read message, so the UI
        # can show "Seen by ..." under the right message bubble.
        cursor.execute("""
            SELECT m.USER_ID, u.USER_FULLNAME, u.USER_NAME, m.LAST_READ_MSG_ID
            FROM dbo.Sys_MsgGroupMember m
            LEFT JOIN dbo.Sys_User u ON u.USER_ID = m.USER_ID
            WHERE m.GROUP_ID = ? AND m.USER_ID <> ?
        """, group_id, uid)
        receipts = [{
            "user_id": r[0], "name": r[1] or r[2] or "",
            "last_read_msg_id": r[3] or 0,
        } for r in cursor.fetchall()]

        # Reactions: for newly-fetched items, patched items, and any message
        # that received a new reaction since since_ts (covers reactions added
        # to older, already-rendered messages; removals sync on next full poll).
        touched_ids = {it["id"] for it in items} | {u["id"] for u in updated_messages}
        if since_ts_raw:
            try:
                since_ts = datetime.fromisoformat(since_ts_raw.replace("Z", "+00:00"))
                cursor.execute("""
                    SELECT DISTINCT r.MSG_ID
                    FROM dbo.Sys_MsgReaction r
                    JOIN dbo.Sys_Msg sm ON sm.ID = r.MSG_ID
                    WHERE sm.GROUP_ID = ? AND r.CREATED_AT > ?
                """, group_id, since_ts)
                touched_ids |= {r[0] for r in cursor.fetchall()}
            except ValueError:
                pass
        reactions = _get_reactions_for_msgs(cursor, list(touched_ids), uid)

        # Pinned messages (small, bounded list; safe to send on every poll).
        cursor.execute("""
            SELECT p.MSG_ID, sm.BODY, sm.IS_DELETED, u.USER_FULLNAME, u.USER_NAME, p.PINNED_AT
            FROM dbo.Sys_MsgPin p
            JOIN dbo.Sys_Msg sm ON sm.ID = p.MSG_ID
            LEFT JOIN dbo.Sys_User u ON u.USER_ID = sm.USER_ID
            WHERE p.GROUP_ID = ?
            ORDER BY p.PINNED_AT DESC
        """, group_id)
        pins = [{
            "msg_id": r[0], "snippet": ("Message deleted" if r[2] else (r[1] or "")[:120]),
            "sender": r[3] or r[4] or "", "pinned_at": r[5].isoformat() if r[5] else None,
        } for r in cursor.fetchall()]

        typing_ids = _get_typing_users(group_id, uid)

        return jsonify({
            "messages": items, "receipts": receipts, "updated_messages": updated_messages,
            "reactions": reactions, "pins": pins, "typing_user_ids": typing_ids,
            "server_time": datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/messages", methods=["POST"])
@login_required
def api_msg_send_message(group_id):
    """Send a message into a group. Body: {body, reply_to_msg_id?}"""
    conn = None
    try:
        _ensure_messaging_tables()
        data = request.get_json(silent=True) or {}
        body = (data.get("body") or "").strip()
        reply_to = data.get("reply_to_msg_id")
        reply_to = int(reply_to) if reply_to else None
        if not body:
            return jsonify({"error": "Message cannot be empty."}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        if reply_to:
            cursor.execute("SELECT 1 FROM dbo.Sys_Msg WHERE ID=? AND GROUP_ID=?", reply_to, group_id)
            if not cursor.fetchone():
                reply_to = None
        cursor.execute(
            "INSERT INTO dbo.Sys_Msg (GROUP_ID, USER_ID, BODY, REPLY_TO_MSG_ID) OUTPUT INSERTED.ID VALUES (?, ?, ?, ?)",
            group_id, uid, body, reply_to,
        )
        msg_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE dbo.Sys_MsgGroupMember SET LAST_READ_MSG_ID=? WHERE GROUP_ID=? AND USER_ID=?",
            msg_id, group_id, uid,
        )
        conn.commit()
        sio_emit("new_message", {
            "group_id": group_id, "message_id": msg_id, "user_id": uid,
            "body": body, "reply_to_msg_id": reply_to,
        }, room=_room_for_group(group_id))
        return jsonify({"success": True, "message_id": msg_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/messages/<int:msg_id>", methods=["PUT"])
@login_required
def api_msg_edit_message(group_id, msg_id):
    """Edit the body of a message you sent. Body: {body}"""
    conn = None
    try:
        _ensure_messaging_tables()
        data = request.get_json(silent=True) or {}
        body = (data.get("body") or "").strip()
        if not body:
            return jsonify({"error": "Message cannot be empty."}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        cursor.execute("SELECT USER_ID, IS_DELETED FROM dbo.Sys_Msg WHERE ID=? AND GROUP_ID=?", msg_id, group_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Message not found"}), 404
        owner_id, is_deleted = row
        if owner_id != uid:
            return jsonify({"error": "You can only edit your own messages."}), 403
        if is_deleted:
            return jsonify({"error": "Cannot edit a deleted message."}), 400
        cursor.execute(
            "UPDATE dbo.Sys_Msg SET BODY=?, IS_EDITED=1, EDITED_AT=GETDATE() WHERE ID=?",
            body, msg_id,
        )
        conn.commit()
        sio_emit("message_edited", {
            "group_id": group_id, "message_id": msg_id, "body": body,
        }, room=_room_for_group(group_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/messages/<int:msg_id>", methods=["DELETE"])
@login_required
def api_msg_delete_message(group_id, msg_id):
    """Soft-delete a message. Allowed for the message's own sender, or the
    group creator (moderation)."""
    conn = None
    try:
        _ensure_messaging_tables()
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        cursor.execute("SELECT USER_ID FROM dbo.Sys_Msg WHERE ID=? AND GROUP_ID=?", msg_id, group_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Message not found"}), 404
        owner_id = row[0]
        cursor.execute("SELECT CREATED_BY FROM dbo.Sys_MsgGroup WHERE ID=?", group_id)
        creator_row = cursor.fetchone()
        is_creator = bool(creator_row) and creator_row[0] == uid
        if owner_id != uid and not is_creator:
            return jsonify({"error": "You can only delete your own messages."}), 403
        cursor.execute(
            "UPDATE dbo.Sys_Msg SET IS_DELETED=1, EDITED_AT=GETDATE() WHERE ID=?",
            msg_id,
        )
        cursor.execute("DELETE FROM dbo.Sys_MsgPin WHERE MSG_ID=?", msg_id)
        conn.commit()
        if owner_id != uid:
            audit_log("MSG_DELETE_OTHER", notes=f"Group creator deleted message {msg_id} in group {group_id}")
        sio_emit("message_deleted", {
            "group_id": group_id, "message_id": msg_id,
        }, room=_room_for_group(group_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/messages/<int:msg_id>/react", methods=["POST"])
@login_required
def api_msg_toggle_reaction(group_id, msg_id):
    """Toggle an emoji reaction on a message for the current user. Body: {emoji}"""
    conn = None
    try:
        _ensure_messaging_tables()
        data = request.get_json(silent=True) or {}
        emoji = (data.get("emoji") or "").strip()
        if not emoji:
            return jsonify({"error": "Missing emoji"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        cursor.execute("SELECT 1 FROM dbo.Sys_Msg WHERE ID=? AND GROUP_ID=? AND IS_DELETED=0", msg_id, group_id)
        if not cursor.fetchone():
            return jsonify({"error": "Message not found"}), 404
        cursor.execute(
            "SELECT 1 FROM dbo.Sys_MsgReaction WHERE MSG_ID=? AND USER_ID=? AND EMOJI=?",
            msg_id, uid, emoji,
        )
        if cursor.fetchone():
            cursor.execute(
                "DELETE FROM dbo.Sys_MsgReaction WHERE MSG_ID=? AND USER_ID=? AND EMOJI=?",
                msg_id, uid, emoji,
            )
        else:
            cursor.execute(
                "INSERT INTO dbo.Sys_MsgReaction (MSG_ID, USER_ID, EMOJI) VALUES (?, ?, ?)",
                msg_id, uid, emoji,
            )
        conn.commit()
        reactions = _get_reactions_for_msgs(cursor, [msg_id], uid)
        sio_emit("reaction_updated", {
            "group_id": group_id, "message_id": msg_id,
            "reactions": reactions.get(msg_id, []),
        }, room=_room_for_group(group_id))
        return jsonify({"success": True, "reactions": reactions.get(msg_id, [])})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/typing", methods=["POST"])
@login_required
def api_msg_typing_ping(group_id):
    """Ping that the current user is typing in this group. Kept as an HTTP
    fallback for clients that haven't opened a socket connection yet (e.g.
    first paint before socket.io finishes connecting) — the live path is now
    the 'typing' socket event handled above, which skips this DB write
    entirely."""
    uid = session["user_id"]
    _set_typing(group_id, uid)
    sio_emit("typing", {"group_id": group_id, "user_id": uid}, room=_room_for_group(group_id))
    return jsonify({"success": True})


@app.route("/api/messages/groups/<int:group_id>/search", methods=["GET"])
@login_required
def api_msg_search(group_id):
    """Search message bodies within a group. ?q=..."""
    conn = None
    try:
        _ensure_messaging_tables()
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"results": []})
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        like = f"%{q}%"
        cursor.execute("""
            SELECT TOP 50 sm.ID, u.USER_FULLNAME, u.USER_NAME, sm.BODY, sm.CREATED_AT
            FROM dbo.Sys_Msg sm
            LEFT JOIN dbo.Sys_User u ON u.USER_ID = sm.USER_ID
            WHERE sm.GROUP_ID = ? AND sm.IS_DELETED = 0 AND sm.BODY LIKE ?
            ORDER BY sm.ID DESC
        """, group_id, like)
        results = [{
            "id": r[0], "sender": r[1] or r[2] or "", "body": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
        } for r in cursor.fetchall()]
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/pin", methods=["POST"])
@login_required
def api_msg_pin_message(group_id):
    """Pin a message in a group. Body: {msg_id}. Any group member may pin."""
    conn = None
    try:
        _ensure_messaging_tables()
        data = request.get_json(silent=True) or {}
        msg_id = data.get("msg_id")
        if not msg_id:
            return jsonify({"error": "Missing msg_id"}), 400
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        cursor.execute("SELECT 1 FROM dbo.Sys_Msg WHERE ID=? AND GROUP_ID=? AND IS_DELETED=0", msg_id, group_id)
        if not cursor.fetchone():
            return jsonify({"error": "Message not found"}), 404
        cursor.execute("SELECT 1 FROM dbo.Sys_MsgPin WHERE GROUP_ID=? AND MSG_ID=?", group_id, msg_id)
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO dbo.Sys_MsgPin (GROUP_ID, MSG_ID, PINNED_BY) VALUES (?, ?, ?)",
                group_id, msg_id, uid,
            )
            conn.commit()
        sio_emit("pin_updated", {"group_id": group_id, "msg_id": msg_id, "pinned": True},
                 room=_room_for_group(group_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/pin/<int:msg_id>", methods=["DELETE"])
@login_required
def api_msg_unpin_message(group_id, msg_id):
    """Unpin a message. Any group member may unpin."""
    conn = None
    try:
        _ensure_messaging_tables()
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        cursor.execute("DELETE FROM dbo.Sys_MsgPin WHERE GROUP_ID=? AND MSG_ID=?", group_id, msg_id)
        conn.commit()
        sio_emit("pin_updated", {"group_id": group_id, "msg_id": msg_id, "pinned": False},
                 room=_room_for_group(group_id))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


MSG_ATTACHMENT_DIR = os.path.join(UPLOAD_DIR, "msg_attachments")


@app.route("/api/messages/groups/<int:group_id>/messages/upload", methods=["POST"])
@login_required
def api_msg_upload_attachment(group_id):
    """Attach one or more files from the user's device to a new chat message.
    multipart/form-data: files[] (one or more), body (optional caption)."""
    conn = None
    try:
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            return jsonify({"error": "No files provided"}), 400
        body = (request.form.get("body") or "").strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403

        cursor.execute(
            "INSERT INTO dbo.Sys_Msg (GROUP_ID, USER_ID, BODY) OUTPUT INSERTED.ID VALUES (?, ?, ?)",
            group_id, uid, body,
        )
        msg_id = cursor.fetchone()[0]

        group_dir = os.path.join(MSG_ATTACHMENT_DIR, str(group_id))
        os.makedirs(group_dir, exist_ok=True)

        for f in files:
            stem = _safe_filename_stem(f.filename, fallback="file")
            ext = os.path.splitext(secure_filename(f.filename))[1]
            on_disk_name = f"{stem}.{msg_id}.{secrets.token_hex(4)}{ext}"
            f.save(os.path.join(group_dir, on_disk_name))
            rel_path = os.path.join("msg_attachments", str(group_id), on_disk_name)
            cursor.execute(
                """INSERT INTO dbo.Sys_MsgAttachment
                   (MSG_ID, ATT_TYPE, FILE_NAME, FILE_PATH, UPLOADED_BY)
                   VALUES (?, 'upload', ?, ?, ?)""",
                msg_id, f.filename, rel_path, uid,
            )

        cursor.execute(
            "UPDATE dbo.Sys_MsgGroupMember SET LAST_READ_MSG_ID=? WHERE GROUP_ID=? AND USER_ID=?",
            msg_id, group_id, uid,
        )
        conn.commit()
        return jsonify({"success": True, "message_id": msg_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/groups/<int:group_id>/messages/attach-archive", methods=["POST"])
@login_required
def api_msg_attach_archive(group_id):
    """Share one or more already-archived documents into a group chat.
    Body: {doc_ids: [...], body: optional caption}"""
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        doc_ids = [int(d) for d in (data.get("doc_ids") or []) if str(d).strip()]
        body = (data.get("body") or "").strip()
        if not doc_ids:
            return jsonify({"error": "No documents selected"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403

        cursor.execute(
            "INSERT INTO dbo.Sys_Msg (GROUP_ID, USER_ID, BODY) OUTPUT INSERTED.ID VALUES (?, ?, ?)",
            group_id, uid, body,
        )
        msg_id = cursor.fetchone()[0]

        for doc_id in doc_ids:
            cursor.execute("SELECT Subject FROM dbo.Adco_Transactions WHERE ID=? AND IsDeleted=0", doc_id)
            row = cursor.fetchone()
            if not row:
                continue
            cursor.execute(
                """INSERT INTO dbo.Sys_MsgAttachment
                   (MSG_ID, ATT_TYPE, FILE_NAME, DOC_ID, UPLOADED_BY)
                   VALUES (?, 'archive', ?, ?, ?)""",
                msg_id, row[0] or f"Document {doc_id}", doc_id, uid,
            )

        cursor.execute(
            "UPDATE dbo.Sys_MsgGroupMember SET LAST_READ_MSG_ID=? WHERE GROUP_ID=? AND USER_ID=?",
            msg_id, group_id, uid,
        )
        conn.commit()
        return jsonify({"success": True, "message_id": msg_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/attachments/<int:att_id>/download")
@login_required
def api_msg_download_attachment(att_id):
    """Serve an uploaded chat attachment — only to members of the message's group."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        cursor.execute("""
            SELECT a.FILE_PATH, a.FILE_NAME, sm.GROUP_ID
            FROM dbo.Sys_MsgAttachment a
            JOIN dbo.Sys_Msg sm ON sm.ID = a.MSG_ID
            WHERE a.ID = ? AND a.ATT_TYPE = 'upload'
        """, att_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        file_path, file_name, group_id = row
        if not _user_in_group(cursor, group_id, uid):
            return jsonify({"error": "Forbidden"}), 403
        full_path = os.path.join(UPLOAD_DIR, file_path)
        directory = os.path.dirname(full_path)
        filename = os.path.basename(full_path)
        return send_from_directory(directory, filename, as_attachment=True, download_name=file_name)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/messages/unread-count")
@login_required
def api_msg_unread_count():
    """Total unread message count across all of the current user's groups, for the sidebar badge."""
    conn = None
    try:
        _ensure_messaging_tables()
        conn = get_db_connection()
        cursor = conn.cursor()
        uid = session["user_id"]
        cursor.execute("""
            SELECT ISNULL(SUM(cnt), 0) FROM (
                SELECT (SELECT COUNT(*) FROM dbo.Sys_Msg sm
                        WHERE sm.GROUP_ID = m.GROUP_ID AND sm.ID > m.LAST_READ_MSG_ID) AS cnt
                FROM dbo.Sys_MsgGroupMember m
                JOIN dbo.Sys_MsgGroup g ON g.ID = m.GROUP_ID
                WHERE m.USER_ID = ? AND g.IS_DELETED = 0
            ) t
        """, uid)
        total = cursor.fetchone()[0]
        return jsonify({"unread_count": int(total or 0)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/notifications")
@login_required
def api_list_notifications():
    """Latest notifications for the current user, plus unread count."""
    conn = None
    try:
        limit = int(request.args.get("limit", 30))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP {limit} ID, Name, Cat_ID, Condition_Others, CreatedOn, IsDeleted
            FROM dbo.Sys_Notifications_Mang
            WHERE User_ID = ?
            ORDER BY CreatedOn DESC
            """,
            session["user_id"],
        )
        rows = cursor.fetchall()
        items = [{
            "id": r[0], "subject": r[1], "doc_id": r[2], "action_type": r[3],
            "created_on": r[4].isoformat() if r[4] else None,
            "is_read": bool(r[5]),
        } for r in rows]
        cursor.execute(
            "SELECT COUNT(*) FROM dbo.Sys_Notifications_Mang WHERE User_ID = ? AND IsDeleted = 0",
            session["user_id"],
        )
        unread = cursor.fetchone()[0]
        return jsonify({"items": items, "unread_count": unread})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/notifications/<int:notif_id>/read", methods=["POST"])
@login_required
def api_mark_notification_read(notif_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE dbo.Sys_Notifications_Mang
               SET IsDeleted = 1, ModifiedBy = ?, ModifiedOn = GETDATE()
               WHERE ID = ? AND User_ID = ?""",
            session["user_id"], notif_id, session["user_id"],
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/notifications/read-all", methods=["POST"])
@login_required
def api_mark_all_notifications_read():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE dbo.Sys_Notifications_Mang
               SET IsDeleted = 1, ModifiedBy = ?, ModifiedOn = GETDATE()
               WHERE User_ID = ? AND IsDeleted = 0""",
            session["user_id"], session["user_id"],
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/notifications/<int:notif_id>", methods=["DELETE"])
@login_required
def api_delete_notification(notif_id):
    """Permanently remove a single notification (the ✕ on one item)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM dbo.Sys_Notifications_Mang WHERE ID = ? AND User_ID = ?",
            notif_id, session["user_id"],
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/notifications", methods=["DELETE"])
@login_required
def api_clear_all_notifications():
    """Permanently remove every notification for the current user ("Clear all")."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM dbo.Sys_Notifications_Mang WHERE User_ID = ?",
            session["user_id"],
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ── WORKFLOW MODULE (WF_*) ───────────────────────────────────────────────────
# PAGE_ID 4 = Workflow (new page, alongside 1=Inquiries, 2=Archive, 3=Folder Browser)

WF_PAGE_ID = 4


def _ensure_workflow_tables():
    """Creates every WF_* table if it doesn't already exist. Safe on every
    startup, mirroring _ensure_accr_table/_ensure_audit_table. Does NOT
    touch Adco_Transactions/Adco_Folder/Sys_User.
    Uses a dedicated autocommit connection (like every other _ensure_*
    migration helper) so these checks never sit inside a long-lived,
    uncommitted transaction -- this function runs on every single
    Send-for-Approval / Save-Draft request, so a leaked open transaction
    here would hold locks on WF_Instances for as long as the app process
    is alive, blocking anything else (including SSMS) trying to touch it."""
    conn = _get_ddl_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Templates')
            CREATE TABLE dbo.WF_Templates (
                TemplateID INT IDENTITY(1,1) PRIMARY KEY,
                Name NVARCHAR(150) NOT NULL, Description NVARCHAR(500) NULL,
                Dept_ID INT NULL, IsActive BIT NOT NULL DEFAULT 1,
                CreatedBy INT NOT NULL, CreatedOn DATETIME NOT NULL DEFAULT GETDATE(),
                ModifiedBy INT NULL, ModifiedOn DATETIME NULL
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Template_Steps')
            CREATE TABLE dbo.WF_Template_Steps (
                StepID INT IDENTITY(1,1) PRIMARY KEY, TemplateID INT NOT NULL,
                StepOrder INT NOT NULL, StepName NVARCHAR(150) NOT NULL,
                ApprovalMode CHAR(1) NOT NULL DEFAULT 'A', AssigneeType CHAR(1) NOT NULL DEFAULT 'U',
                IsActive BIT NOT NULL DEFAULT 1,
                CONSTRAINT FK_WFTemplateSteps_Template FOREIGN KEY (TemplateID) REFERENCES dbo.WF_Templates(TemplateID),
                CONSTRAINT UQ_WFTemplateSteps_Order UNIQUE (TemplateID, StepOrder)
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Template_Step_Assignees')
            CREATE TABLE dbo.WF_Template_Step_Assignees (
                StepAssigneeID INT IDENTITY(1,1) PRIMARY KEY, StepID INT NOT NULL,
                User_ID INT NULL, Dep_ID INT NULL,
                CONSTRAINT FK_WFStepAssignees_Step FOREIGN KEY (StepID) REFERENCES dbo.WF_Template_Steps(StepID)
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Instances')
            CREATE TABLE dbo.WF_Instances (
                InstanceID INT IDENTITY(1,1) PRIMARY KEY, Transaction_ID INT NOT NULL,
                TemplateID INT NOT NULL, CurrentStepOrder INT NULL,
                Status VARCHAR(20) NOT NULL DEFAULT 'Pending', SubmissionNumber INT NOT NULL DEFAULT 1,
                SubmittedBy INT NOT NULL, SubmittedOn DATETIME NOT NULL DEFAULT GETDATE(),
                CompletedOn DATETIME NULL, IsDeleted BIT NOT NULL DEFAULT 0,
                CONSTRAINT FK_WFInstances_Template FOREIGN KEY (TemplateID) REFERENCES dbo.WF_Templates(TemplateID)
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Instance_Steps')
            CREATE TABLE dbo.WF_Instance_Steps (
                InstanceStepID INT IDENTITY(1,1) PRIMARY KEY, InstanceID INT NOT NULL,
                SubmissionNumber INT NOT NULL, StepOrder INT NOT NULL, StepName NVARCHAR(150) NOT NULL,
                ApprovalMode CHAR(1) NOT NULL, Status VARCHAR(20) NOT NULL DEFAULT 'Waiting',
                StartedOn DATETIME NULL, CompletedOn DATETIME NULL,
                CONSTRAINT FK_WFInstanceSteps_Instance FOREIGN KEY (InstanceID) REFERENCES dbo.WF_Instances(InstanceID),
                CONSTRAINT UQ_WFInstanceSteps_Order UNIQUE (InstanceID, SubmissionNumber, StepOrder)
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Instance_Assignments')
            CREATE TABLE dbo.WF_Instance_Assignments (
                AssignmentID INT IDENTITY(1,1) PRIMARY KEY, InstanceStepID INT NOT NULL,
                AssignedTo INT NOT NULL, Status VARCHAR(20) NOT NULL DEFAULT 'Waiting',
                AssignedOn DATETIME NOT NULL DEFAULT GETDATE(), ActedOn DATETIME NULL, DelegatedFrom INT NULL,
                CONSTRAINT FK_WFAssignments_InstanceStep FOREIGN KEY (InstanceStepID) REFERENCES dbo.WF_Instance_Steps(InstanceStepID)
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_History')
            CREATE TABLE dbo.WF_History (
                HistoryID INT IDENTITY(1,1) PRIMARY KEY, InstanceID INT NOT NULL,
                SubmissionNumber INT NOT NULL, InstanceStepID INT NULL, ActionBy INT NOT NULL,
                ActionType VARCHAR(30) NOT NULL, ActionOn DATETIME NOT NULL DEFAULT GETDATE(),
                Notes NVARCHAR(1000) NULL,
                CONSTRAINT FK_WFHistory_Instance FOREIGN KEY (InstanceID) REFERENCES dbo.WF_Instances(InstanceID)
            )
        """)
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Comments')
            CREATE TABLE dbo.WF_Comments (
                CommentID INT IDENTITY(1,1) PRIMARY KEY, InstanceID INT NOT NULL,
                InstanceStepID INT NULL, CommentBy INT NOT NULL, CommentText NVARCHAR(MAX) NOT NULL,
                CreatedOn DATETIME NOT NULL DEFAULT GETDATE(), IsDeleted BIT NOT NULL DEFAULT 0,
                CONSTRAINT FK_WFComments_Instance FOREIGN KEY (InstanceID) REFERENCES dbo.WF_Instances(InstanceID)
            )
        """)
        # Document Attachment (Workflow document lifecycle) — holds files
        # uploaded/scanned in the wizard BEFORE the instance is approved. Mirrors
        # Adco_Transactions_Attachments column-for-column on purpose so the
        # approval-time copy into that table is a straight field mapping and no
        # new file-storage mechanism is introduced. See
        # _archive_wf_instance_to_inquiries, which copies these rows into
        # Adco_Transactions_Attachments once the instance is fully approved.
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Instance_Attachments')
            CREATE TABLE dbo.WF_Instance_Attachments (
                ID INT IDENTITY(1,1) PRIMARY KEY,
                WF_Instance_ID INT NOT NULL,
                File_Name NVARCHAR(255) NOT NULL,
                File_Description NVARCHAR(500) NULL,
                File_URL NVARCHAR(500) NOT NULL,
                File_Size BIGINT NULL,
                File_Type_ID VARCHAR(20) NULL,
                CreatedBy INT NOT NULL, CreatedOn DATETIME NOT NULL DEFAULT GETDATE(),
                IsDeleted BIT NOT NULL DEFAULT 0,
                CONSTRAINT FK_WFInstanceAttachments_Instance FOREIGN KEY (WF_Instance_ID) REFERENCES dbo.WF_Instances(InstanceID)
            )
        """)

        # Global Workflow settings (Control Panel -> admin only). Currently just
        # holds the minimum number of distinct approvers required before an
        # ad-hoc workflow submission can be considered fully approved — see
        # get_wf_min_approvals() / api_admin_workflow_settings_*.
        cursor.execute("""
            IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'WF_Config')
            CREATE TABLE dbo.WF_Config (
                ConfigKey NVARCHAR(50) NOT NULL PRIMARY KEY,
                ConfigValue NVARCHAR(255) NOT NULL,
                ModifiedBy INT NULL, ModifiedOn DATETIME NULL
            )
        """)

    finally:
        conn.close()
    print("[Workflow migration] WF_* tables ready.")


def get_wf_min_approvals(cursor=None):
    """Reads the admin-configured minimum number of distinct approvers a
    workflow submission needs before it can be fully approved/archived.
    Defaults to 1 (today's behavior) if never configured. Accepts an
    optional existing cursor so callers mid-transaction don't need a
    second DB connection."""
    owns_conn = cursor is None
    conn = None
    try:
        if owns_conn:
            conn = get_db_connection()
            cursor = conn.cursor()
        cursor.execute("SELECT ConfigValue FROM dbo.WF_Config WHERE ConfigKey = 'MIN_APPROVALS'")
        row = cursor.fetchone()
        if row and row[0]:
            try:
                return max(1, int(row[0]))
            except (TypeError, ValueError):
                return 1
        return 1
    except Exception:
        # WF_Config may not exist yet on a very first run before
        # _ensure_workflow_tables() has been called — default gracefully.
        return 1
    finally:
        if owns_conn and conn:
            conn.close()


def _ensure_workflow_adhoc_columns():
    """Relaxes WF_Instances so a 'Send for Approval' submission can be
    created without a pre-existing Adco_Transactions row or a WF_Templates
    template (the New Workflow tab currently has no document-creation or
    template-selection step wired up). Also adds the small set of metadata
    columns that ad-hoc submissions carry directly on the instance.
    Uses a dedicated autocommit connection so ALTER TABLE always succeeds."""
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()

        # Loosen NOT NULL columns that ad-hoc (template-less, document-less)
        # submissions can't populate.
        cur.execute("""
            SELECT COLUMN_NAME, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'WF_Instances'
              AND COLUMN_NAME IN ('Transaction_ID', 'TemplateID')
        """)
        nullability = {r[0]: r[1] for r in cur.fetchall()}
        if nullability.get("Transaction_ID") == "NO":
            cur.execute("ALTER TABLE dbo.WF_Instances ALTER COLUMN Transaction_ID INT NULL")
            print("[Workflow migration] WF_Instances.Transaction_ID relaxed to NULL.")
        if nullability.get("TemplateID") == "NO":
            cur.execute("ALTER TABLE dbo.WF_Instances ALTER COLUMN TemplateID INT NULL")
            print("[Workflow migration] WF_Instances.TemplateID relaxed to NULL.")

        # Add columns to hold ad-hoc submission metadata directly on the instance.
        cur.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'WF_Instances'
              AND COLUMN_NAME IN ('Subject','Keywords','Notes','DocDate','ImportanceID',
                                   'ViewedOn','DraftAssignedTo','EntityDeptID','FolderID',
                                   'ExpiryDate','HiddenFromSent')
        """)
        existing = {r[0] for r in cur.fetchall()}
        adhoc_columns = {
            "Subject": "NVARCHAR(255) NULL",
            "Keywords": "NVARCHAR(500) NULL",
            "Notes": "NVARCHAR(1000) NULL",
            "DocDate": "DATE NULL",
            "ImportanceID": "INT NULL",
            # Draft / Viewed status granularity (Task 12):
            # ViewedOn records the first time the current assignee opened the
            # document — kept forever even after Status moves on to
            # 'In Progress', so Sent/History can still show "Viewed on ...".
            # DraftAssignedTo stores the recipient chosen while a submission
            # is still a Draft (no WF_Instance_Steps/Assignments row exists
            # yet, since those are only created once a draft is actually sent).
            "ViewedOn": "DATETIME NULL",
            "DraftAssignedTo": "INT NULL",
            # Entity/Department + Volume (Folder) picked on the New Request
            # form's "Send To" panel, mirroring the Archive page's filing
            # fields. Both NULL-able since older/ad-hoc rows won't have them
            # and a folder is optional context, not a hard requirement.
            "EntityDeptID": "INT NULL",
            "FolderID": "INT NULL",
            # Document expiry date — used by per-user alert preferences so the
            # Archive page can warn about upcoming expirations.
            "ExpiryDate": "DATE NULL",
            # Removing an item from the Sent tab must NOT delete/hide it from
            # History (or from the assignee's Inbox) — IsDeleted is a global
            # flag every list filters on, so reusing it there was hiding the
            # instance everywhere, not just the sender's own Sent view. This
            # is a separate, sender-only "hide from my Sent list" flag.
            "HiddenFromSent": "BIT NOT NULL DEFAULT 0",
        }
        for col, col_def in adhoc_columns.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE dbo.WF_Instances ADD [{col}] {col_def}")
                print(f"[Workflow migration] Added dbo.WF_Instances.[{col}]")
    finally:
        conn.close()


def _send_system_email(subject, text_body, html_body, recipients):
    """System-generated emails (rejection notices, etc.) — distinct from
    _send_document_email_core, which is the user-initiated 'email this
    document' flow. Uses the same Graph -> shared-SMTP fallback as the
    admin 'Send Test Email' flow."""
    if GRAPH_ENABLED:
        _send_graph_mail(subject=subject, html_body=html_body, recipients=recipients,
                          from_email=get_shared_from_email())
    else:
        _send_smtp_mail(subject=subject, text_body=text_body, html_body=html_body, recipients=recipients)


def _wf_document_link(instance_id):
    """Deep link back into the Workflow module for a given instance —
    used in every workflow notification email's call-to-action button."""
    return f"{request.host_url.rstrip('/')}/dashboard?wf_open={instance_id}"


def _wf_send_notification_email(recipient_email, recipient_name, subject, heading,
                                 body_lines, instance_id, button_label="Open Document"):
    """
    Shared template for all workflow notification emails (assignment,
    forward, approval, rejection) so they stay visually and structurally
    consistent. body_lines is a list of (label, value) pairs rendered as
    a simple key/value block, e.g. [("Document", "Invoice #4521"),
    ("From", "Ahmed Al-Otaibi"), ("Date", "2026-07-12")].
    Returns None on success, or an error string on failure (never raises —
    callers treat email as best-effort and surface the string as a warning).
    """
    if not recipient_email:
        return None
    link = _wf_document_link(instance_id)
    rows_text = "\n".join(f"{label}: {value}" for label, value in body_lines if value)
    rows_html = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#64748b;font-size:13px">{label}</td>'
        f'<td style="padding:4px 0;font-size:13px;font-weight:600;color:#1e293b">{value}</td></tr>'
        for label, value in body_lines if value
    )
    text_body = (
        f"Hi {recipient_name or ''},\n\n{heading}\n\n{rows_text}\n\n"
        f"Open the document: {link}\n"
    )
    html_body = f"""
    <div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px;margin:0 auto">
        <p style="font-size:14px;color:#1e293b">Hi {recipient_name or ''},</p>
        <p style="font-size:14px;color:#1e293b">{heading}</p>
        <table style="border-collapse:collapse;margin:14px 0">{rows_html}</table>
        <p style="margin-top:20px">
            <a href="{link}" style="background:#1e6fc4;color:#fff;text-decoration:none;
               padding:10px 20px;border-radius:6px;font-size:13px;font-weight:600;display:inline-block">
               {button_label}
            </a>
        </p>
        <p style="font-size:11.5px;color:#94a3b8;margin-top:24px">
            This is an automated message from the DocPortal Archiving System.
        </p>
    </div>
    """
    def _send():
        try:
            _send_system_email(subject=subject, text_body=text_body, html_body=html_body,
                                recipients=[recipient_email])
        except Exception as exc:
            print(f"[Workflow] notification email to {recipient_email} failed: {exc}")

    # The actual SMTP/Graph network call is the slow part of every workflow
    # action (submit, approve, reject, forward, resubmit) — each one can add
    # a second or more per recipient if sent inline. Since this email is
    # already best-effort (failures were only ever surfaced as a console
    # warning, never blocking the action itself), fire it in the background
    # so the API response — and the button spinner the user is staring at —
    # returns immediately instead of waiting on the mail server.
    threading.Thread(target=_send, daemon=True).start()
    return None


def _get_user_email(cursor, user_id):
    cursor.execute("SELECT USER_EMAIL, USER_NAME FROM dbo.Sys_User WHERE USER_ID = ?", user_id)
    row = cursor.fetchone()
    return (row[0], row[1]) if row else (None, None)


def notify_dept_users_single(target_user_id, action_type, doc_id, subject=None, actor_id=None):
    """Same dbo.Sys_Notifications_Mang insert as notify_dept_users, but
    targeted at exactly one user instead of a whole department."""
    if actor_id is None:
        actor_id = session.get("user_id")
    short_subject = (subject or "").strip()
    if len(short_subject) > 150:
        short_subject = short_subject[:150] + "…"
    name_value = short_subject or None

    def _insert():
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO dbo.Sys_Notifications_Mang
                       (Name, User_ID, Cat_ID, Condition_Others, Ahead_Of, CreatedBy, CreatedOn, IsDeleted)
                   VALUES (?, ?, ?, ?, 0, ?, GETDATE(), 0)""",
                name_value, target_user_id, doc_id, action_type, actor_id,
            )
            conn.commit()
            sio_emit("notification", {
                "action_type": action_type, "doc_id": doc_id, "subject": name_value,
            }, room=_room_for_user(target_user_id))
        except Exception as exc:
            print(f"[Notifications] single-target insert failed ({action_type} doc {doc_id}): {exc}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=_insert, daemon=True).start()


def _resolve_step_assignees(cursor, step_id):
    """Resolves a WF_Template_Steps row into a concrete set of USER_IDs —
    specific users directly, or department members via parse_dep_id_from,
    the SAME helper used everywhere else for Sys_User.Dep_ID_From."""
    cursor.execute("SELECT User_ID, Dep_ID FROM dbo.WF_Template_Step_Assignees WHERE StepID = ?", step_id)
    assignee_rows = cursor.fetchall()
    resolved_user_ids = set()
    dep_ids_needed = {dep_id for _uid, dep_id in assignee_rows if dep_id}
    if dep_ids_needed:
        cursor.execute("SELECT USER_ID, Dep_ID_From FROM dbo.Sys_User WHERE IsDeleted = 0")
        for uid, dep_from in cursor.fetchall():
            if parse_dep_id_from(dep_from) & dep_ids_needed:
                resolved_user_ids.add(uid)
    for uid, dep_id in assignee_rows:
        if uid:
            resolved_user_ids.add(uid)
    return resolved_user_ids


def _archive_wf_instance_to_inquiries(cursor, instance_id):
    """
    Uses the SAME archive logic as regular document creation (Adco_Transactions
    INSERT — see api_save_document / api_folder_scan_upload) to turn a fully
    approved, ad-hoc WF_Instances row into a real Inquiries (Adco_Transactions)
    record. Only ever called once a workflow instance has no more pending
    steps, so the registration number (Adco_Transactions.ID) is generated at
    approval time, never before.

    Idempotent: if this instance already has a Transaction_ID, that existing
    ID is returned unchanged instead of archiving a second time.

    Returns the Adco_Transactions.ID (the registration number) for the newly
    (or previously) archived document.
    """
    cursor.execute(
        """SELECT Transaction_ID, Subject, Keywords, Notes, DocDate, ImportanceID, SubmittedBy
           FROM dbo.WF_Instances WHERE InstanceID = ?""",
        instance_id,
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"Workflow instance {instance_id} not found for archiving")

    existing_tx_id, subject, keywords, notes, doc_date, importance_id, submitted_by = row
    if existing_tx_id:
        return existing_tx_id  # already archived — don't create a duplicate

    cursor.execute("SELECT Dep_ID FROM dbo.Sys_User WHERE USER_ID = ?", submitted_by)
    dep_row = cursor.fetchone()
    dept_id = dep_row[0] if dep_row and dep_row[0] else None

    # Foldes_ID on Adco_Transactions must reference a real Adco_Folder.ID —
    # NOT a department id. WF_Instances doesn't currently capture which
    # folder the document should file into, so resolve the best folder we
    # can under the submitter's own department instead of writing the raw
    # dept_id straight into Foldes_ID (which used to point at a folder that
    # doesn't exist, making the archived document invisible to anyone whose
    # Inquiries view is folder/department-scoped).
    folder_id = None
    if dept_id:
        dcol = adco_folder_dept_col(cursor)
        dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
        cursor.execute(
            f"""SELECT TOP 1 ID FROM dbo.Adco_Folder
                WHERE {dept_bracket} = ? AND IsDeleted = 0
                ORDER BY CASE WHEN Parent_ID = 0 THEN 0 ELSE 1 END, ID""",
            dept_id,
        )
        folder_row = cursor.fetchone()
        folder_id = folder_row[0] if folder_row else None
    if not folder_id:
        raise RuntimeError(
            f"Could not resolve a folder to archive workflow instance {instance_id} into "
            f"(submitter's department {dept_id!r} has no Adco_Folder row). "
            f"Create at least one folder under that department first."
        )

    today_str = datetime.now().strftime("%Y/%m/%d")
    h_date = doc_date.strftime("%Y/%m/%d") if hasattr(doc_date, "strftime") else (doc_date or today_str)
    g_date = parse_hijri_date_string(h_date)

    cursor.execute(
        """
        INSERT INTO dbo.Adco_Transactions
            (Type_ID, Cat_ID, H_Date, G_Date,
             Importance_Degree_ID, Secret_Degree_ID,
             Subject, Keywords, Detailes,
             Foldes_ID, From_Dep_ID, To_Dep_ID,
             CreatedBy, CreatedOn, IsDeleted, Status_ID, Is_Need_Reply)
        VALUES (1, 1, ?, ?,
                ?, 1,
                ?, ?, ?,
                ?, ?, ?,
                ?, GETDATE(), 0, 1, 0)
        """,
        h_date, g_date,
        importance_id or 1,
        subject, keywords, notes,
        folder_id, dept_id, dept_id,
        submitted_by,
    )

    cursor.execute(
        """SELECT MAX(ID) FROM dbo.Adco_Transactions WITH (UPDLOCK, HOLDLOCK)
           WHERE CreatedBy = ? AND IsDeleted = 0
             AND CreatedOn >= DATEADD(SECOND, -5, GETDATE())""",
        submitted_by,
    )
    new_row = cursor.fetchone()
    new_tx_id = new_row[0] if new_row and new_row[0] else None
    if not new_tx_id:
        raise RuntimeError("Could not retrieve new transaction ID while archiving approved workflow instance")

    cursor.execute(
        "UPDATE dbo.WF_Instances SET Transaction_ID = ? WHERE InstanceID = ?",
        new_tx_id, instance_id,
    )

    # Document Attachment: copy every file staged in WF_Instance_Attachments
    # during the wizard into the real archive table, against the now-final
    # registration number. The File_URL is copied as-is — same physical file
    # on disk, no re-upload, no duplicate storage. WF_Instance_Attachments
    # rows are left in place afterward purely as a pre-approval history
    # record; Adco_Transactions_Attachments is the system of record from
    # here on, exactly like any document created directly via Archive.
    cursor.execute(
        """SELECT File_Name, File_Description, File_URL, File_Size, File_Type_ID, CreatedBy
           FROM dbo.WF_Instance_Attachments
           WHERE WF_Instance_ID = ? AND IsDeleted = 0""",
        instance_id,
    )
    for fname, fdesc, furl, fsize, ftype, createdby in cursor.fetchall():
        cursor.execute(
            """
            INSERT INTO dbo.Adco_Transactions_Attachments
                (Transaction_ID, File_Name, File_Description, File_URL,
                 File_Size, File_Type_ID, CreatedBy, CreatedOn, IsDeleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 0)
            """,
            new_tx_id, fname, fdesc, furl, fsize, ftype, createdby or submitted_by,
        )

    return new_tx_id


def _wf_mark_in_progress_on_open(cursor, conn, doc_id, user_id):
    """
    Workflow status transition: Pending Approval -> Viewed -> In Progress.

    Called when a user opens/views a document. If that user is the one
    currently assigned to act on the document's workflow instance and the
    instance is still 'Pending Approval', this:
      1) Records the read receipt — ViewedOn is stamped the first time
         (and only the first time) the assignee opens the document. This
         timestamp is kept forever, even once Status moves past 'Viewed',
         so Sent/History can still show "Viewed on ..." later on.
      2) Auto-advances Status: 'Pending Approval' -> 'Viewed' -> 'In
         Progress' in the same call, since opening the document is itself
         the start of the assignee's review.

    This only touches WF_Instances.Status/ViewedOn. No approvals,
    forwarding, or notifications are handled here.
    """
    try:
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.ViewedOn
            FROM dbo.WF_Instances wi
            JOIN dbo.WF_Instance_Steps wis
                ON wis.InstanceID = wi.InstanceID
               AND wis.SubmissionNumber = wi.SubmissionNumber
               AND wis.StepOrder = wi.CurrentStepOrder
            JOIN dbo.WF_Instance_Assignments wa
                ON wa.InstanceStepID = wis.InstanceStepID
               AND wa.AssignedTo = ?
               AND wa.Status = 'Waiting'
            WHERE wi.Transaction_ID = ?
              AND wi.Status = 'Pending Approval'
              AND wi.IsDeleted = 0
            """,
            user_id, doc_id,
        )
        row = cursor.fetchone()
        if row:
            instance_id, viewed_on = row
            if viewed_on is None:
                cursor.execute(
                    "UPDATE dbo.WF_Instances SET Status = 'Viewed', ViewedOn = GETDATE() WHERE InstanceID = ?",
                    instance_id,
                )
                cursor.execute(
                    """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
                       VALUES (?, 1, NULL, ?, 'VIEWED', GETDATE(), NULL)""",
                    instance_id, user_id,
                )
            cursor.execute(
                "UPDATE dbo.WF_Instances SET Status = 'In Progress' WHERE InstanceID = ?",
                instance_id,
            )
            conn.commit()
    except Exception:
        # Never let this side-effect break document viewing.
        try:
            conn.rollback()
        except Exception:
            pass


def _wf_parse_submission_payload(data):
    """Shared parsing/validation for the ad-hoc submission fields used by
    both Save Draft and Send for Approval. Returns (topic, keywords,
    statement, doc_date, importance_id, dept_id, folder_id, expiry_date) —
    error_response is a (jsonify(...), status_code) tuple when validation
    fails, else None. Unlike the send path, a draft only requires a topic —
    everything else, including the recipient, may still be blank while the
    user is drafting. dept_id/folder_id come from the "Send To" panel's
    Entity/Department and Volume (Folder) pickers; both are optional context,
    not a hard requirement. expiry_date drives per-user expiry alert banners
    on the Archive page (see WfExpiryAlertDays in Sys_User).
    """
    topic = (data.get("topic") or "").strip()
    keywords = (data.get("keywords") or "").strip() or None
    statement = (data.get("statement") or "").strip() or None
    doc_date = (data.get("doc_date") or "").strip() or None
    importance = data.get("importance")
    try:
        importance_id = int(importance) if importance not in (None, "") else None
    except (TypeError, ValueError):
        importance_id = None
    dept_id = data.get("dept_id")
    try:
        dept_id = int(dept_id) if dept_id not in (None, "") else None
    except (TypeError, ValueError):
        dept_id = None
    folder_id = data.get("folder_id")
    try:
        folder_id = int(folder_id) if folder_id not in (None, "") else None
    except (TypeError, ValueError):
        folder_id = None
    expiry_date = (data.get("expiry_date") or "").strip() or None
    return topic, keywords, statement, doc_date, importance_id, dept_id, folder_id, expiry_date


def _wf_create_step_and_assign(cursor, instance_id, assigned_user_ids):
    """Creates the initial ad-hoc approval step (StepOrder 1) + one
    assignment per selected user, and returns instance_step_id. ApprovalMode
    'A' (ALL) means every selected user must approve before the step — and
    therefore this leg of the chain — is considered done. Shared by Send for
    Approval (new submissions) and Send Draft (promoting a saved draft).
    Accepts either a single user id or a list/tuple of user ids (multi-select
    "Send To")."""
    return _wf_create_adhoc_step(cursor, instance_id, submission_number=1, step_order=1,
                                  assigned_user_ids=assigned_user_ids)


def _wf_create_adhoc_step(cursor, instance_id, submission_number, step_order, assigned_user_ids):
    """Generic version of _wf_create_step_and_assign that can create any
    ad-hoc step (not just the first one) — used both for the initial send
    and for the "Approve and Send" continuation once an approver forwards
    the chain to more users to satisfy the admin's minimum-approvals
    setting. Always ApprovalMode 'A' (ALL selected users must approve)."""
    if not isinstance(assigned_user_ids, (list, tuple, set)):
        assigned_user_ids = [assigned_user_ids]
    assigned_user_ids = [int(u) for u in assigned_user_ids]

    cursor.execute(
        """
        INSERT INTO dbo.WF_Instance_Steps
            (InstanceID, SubmissionNumber, StepOrder, StepName, ApprovalMode, Status, StartedOn)
        VALUES (?, ?, ?, 'Approval', 'A', 'Waiting', GETDATE())
        """,
        instance_id, submission_number, step_order,
    )
    cursor.execute(
        """
        SELECT MAX(InstanceStepID) FROM dbo.WF_Instance_Steps WITH (UPDLOCK, HOLDLOCK)
        WHERE InstanceID = ? AND SubmissionNumber = ? AND StepOrder = ?
        """,
        instance_id, submission_number, step_order,
    )
    step_row = cursor.fetchone()
    instance_step_id = step_row[0] if step_row and step_row[0] else None
    if not instance_step_id:
        raise RuntimeError("Could not retrieve new workflow step ID after INSERT")

    for uid in assigned_user_ids:
        cursor.execute(
            """
            INSERT INTO dbo.WF_Instance_Assignments
                (InstanceStepID, AssignedTo, Status, AssignedOn)
            VALUES (?, ?, 'Waiting', GETDATE())
            """,
            instance_step_id, uid,
        )
    return instance_step_id


def _wf_collect_uploaded_files(request_obj, is_multipart):
    """Same collection convention as api_save_document: multi-file key
    'files' with a legacy single-file 'file' fallback."""
    if not is_multipart:
        return []
    uploaded_files = [f for f in request_obj.files.getlist("files") if f and f.filename]
    if not uploaded_files:
        single = request_obj.files.get("file")
        if single and single.filename:
            uploaded_files = [single]
    return uploaded_files


def _wf_save_uploaded_attachments(cursor, instance_id, uploaded_files, file_desc, user_id):
    """
    Document Attachment (Workflow wizard). Saves each uploaded/scanned file
    to disk using the SAME storage location and naming convention as
    api_save_document (FILE_SAVE_DIR, temp-write then rename), then records
    it in WF_Instance_Attachments against this instance.

    Deliberately reuses the existing physical file-storage mechanism instead
    of inventing a second one — only the metadata table differs (staging
    table pre-approval vs. Adco_Transactions_Attachments post-approval).
    Unlimited files: each call just appends more rows, there's no fixed slot
    count. Returns the number of attachments saved.
    """
    saved = 0
    for idx, uf in enumerate(uploaded_files):
        original_name = uf.filename or "file"
        base_name, ext = os.path.splitext(original_name)
        ext = ext or ".bin"
        file_ext = ext.lstrip(".").lower()
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        temp_name = f"_tmp_wf{instance_id}_{ts}_{idx}{ext}"
        temp_path = os.path.join(FILE_SAVE_DIR, temp_name)
        uf.save(temp_path)
        file_size = os.path.getsize(temp_path)

        # base_name comes from the client's original filename — sanitize
        # before using it to build a disk path (path traversal guard).
        # The real/original name is preserved separately in File_Name.
        safe_base_name = _safe_filename_stem(base_name)
        final_name = f"{safe_base_name}.wf{instance_id}.{ts}{ext}"
        final_path = os.path.join(FILE_SAVE_DIR, final_name)
        os.rename(temp_path, final_path)

        cursor.execute(
            """
            INSERT INTO dbo.WF_Instance_Attachments
                (WF_Instance_ID, File_Name, File_Description, File_URL,
                 File_Size, File_Type_ID, CreatedBy, CreatedOn, IsDeleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 0)
            """,
            instance_id, original_name, file_desc, final_name,
            file_size, file_ext, user_id,
        )
        saved += 1
    return saved


def _wf_parse_linked_attachment_ids(data):
    """Parses the 'linked_attachment_ids' field sent by the Export/Import ->
    Link picker in the New Request wizard (JSON array or comma-separated
    string of Adco_Transactions_Attachments IDs the user chose to link
    instead of uploading). Returns a list of ints, silently ignoring
    anything malformed."""
    raw = data.get("linked_attachment_ids")
    if not raw:
        return []
    try:
        if isinstance(raw, str):
            raw = raw.strip()
            ids = json.loads(raw) if raw.startswith("[") else raw.split(",")
        else:
            ids = raw
        return [int(i) for i in ids if str(i).strip()]
    except (TypeError, ValueError):
        return []


def _wf_save_linked_attachments(cursor, instance_id, attachment_ids, user_id):
    """Links already-approved/archived attachments (from
    Adco_Transactions_Attachments) onto this workflow instance WITHOUT
    duplicating the physical file — reuses the same File_URL. This backs
    the "Export / Import" -> "Link" field in the New Request wizard, which
    lets a user attach a document that has already been approved/archived
    elsewhere instead of re-uploading a copy of it. Returns the number linked."""
    if not attachment_ids:
        return 0
    placeholders = ",".join("?" for _ in attachment_ids)
    cursor.execute(
        f"""SELECT a.ID, a.Transaction_ID, a.File_Name, a.File_URL, a.File_Size, a.File_Type_ID
            FROM dbo.Adco_Transactions_Attachments a
            WHERE a.ID IN ({placeholders}) AND {_attachments_active_sql('a')}""",
        *attachment_ids,
    )
    rows = cursor.fetchall()
    linked = 0
    for att_id, tx_id, file_name, file_url, file_size, file_type_id in rows:
        desc = f"[Linked from Registration #{tx_id}]"
        cursor.execute(
            """
            INSERT INTO dbo.WF_Instance_Attachments
                (WF_Instance_ID, File_Name, File_Description, File_URL,
                 File_Size, File_Type_ID, CreatedBy, CreatedOn, IsDeleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE(), 0)
            """,
            instance_id, file_name, desc, file_url, file_size, file_type_id, user_id,
        )
        linked += 1
    return linked


def _wf_list_instance_attachments(cursor, instance_id):
    """Attachments for an instance regardless of lifecycle stage: while
    pending, they live in WF_Instance_Attachments; once approved, the real
    copies live in Adco_Transactions_Attachments (see
    _archive_wf_instance_to_inquiries). Returns a list of dicts with a
    'source' field so the frontend/preview route knows which table to hit.
    """
    cursor.execute("SELECT Transaction_ID FROM dbo.WF_Instances WHERE InstanceID = ?", instance_id)
    row = cursor.fetchone()
    transaction_id = row[0] if row else None

    if transaction_id:
        cursor.execute(
            f"""SELECT ID, File_Name, File_Size, File_Type_ID, File_Description, File_URL
                FROM dbo.Adco_Transactions_Attachments
                WHERE Transaction_ID = ? AND {_attachments_active_sql()}""",
            transaction_id,
        )
        return [
            {"id": r[0], "file_name": r[1], "file_size": r[2], "file_type": r[3],
             "source": "archived", "is_linked": bool(r[4] and r[4].startswith("[Linked from")),
             "is_signed": _attachment_is_signed(r[5]), "can_sign": True}
            for r in cursor.fetchall()
        ]

    cursor.execute(
        """SELECT ID, File_Name, File_Size, File_Type_ID, File_Description
           FROM dbo.WF_Instance_Attachments
           WHERE WF_Instance_ID = ? AND IsDeleted = 0""",
        instance_id,
    )
    # Not-yet-archived instance attachments live in a different table than
    # /api/attachments/<id>/sign operates on, so signing isn't available
    # here yet — can_sign=False hides the Sign/Remove buttons instead of
    # letting them hit a 404.
    return [
        {"id": r[0], "file_name": r[1], "file_size": r[2], "file_type": r[3],
         "source": "pending", "is_linked": bool(r[4] and r[4].startswith("[Linked from")),
         "is_signed": False, "can_sign": False}
        for r in cursor.fetchall()
    ]


def _wf_parse_assigned_user_ids(data):
    """Extracts the list of user ids picked in the (now multi-select) "Send
    To" / "Approve and Send" picker. Accepts a JSON list, a comma-separated
    string (multipart form fallback), or the legacy singular
    'assigned_user_id' field for backward compatibility. Returns a
    de-duplicated list of ints, or [] if nothing usable was supplied."""
    raw = data.get("assigned_user_ids")
    if raw is None:
        raw = data.get("assigned_user_id")
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        values = raw
    elif isinstance(raw, str):
        values = [v.strip() for v in raw.split(",")]
    else:
        values = [raw]
    out = []
    for v in values:
        if v in (None, ""):
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv not in out:
            out.append(iv)
    return out


def _wf_notify_assignment(cursor, instance_id, assigned_user_id, topic, actor_id):
    """Shared notification + email fan-out once a document is actually sent
    (from a fresh submission or from sending a saved draft)."""
    notify_dept_users_single(target_user_id=assigned_user_id, action_type="WF_SUBMITTED",
                              doc_id=instance_id, subject=topic, actor_id=actor_id)

    assignee_email, assignee_name = _get_user_email(cursor, assigned_user_id)
    _, sender_name = _get_user_email(cursor, actor_id)
    return _wf_send_notification_email(
        recipient_email=assignee_email, recipient_name=assignee_name,
        subject=f"Document Assigned to You for Review — {topic}",
        heading="You have received a document requiring your review.",
        body_lines=[("Document", topic), ("Sender", sender_name or ""),
                    ("Date", datetime.now().strftime("%Y-%m-%d"))],
        instance_id=instance_id, button_label="Review Document",
    )


# ── API: Send for Approval (creates a workflow record, assigns a single
#         user, sets its status to Pending Approval) ───────────────────────
@app.route("/api/workflow/submit", methods=["POST"])
@login_required
def api_workflow_submit():
    conn = None
    try:
        is_multipart = request.content_type and "multipart/form-data" in request.content_type
        data = request.form if is_multipart else (request.get_json(silent=True) or {})
        uploaded_files = _wf_collect_uploaded_files(request, is_multipart)
        topic, keywords, statement, doc_date, importance_id, dept_id, folder_id, expiry_date = _wf_parse_submission_payload(data)
        if not topic:
            return jsonify({"error": "Topic / Subject is required."}), 400
        if not dept_id:
            return jsonify({"error": "Please select an Entity / Department."}), 400

        assigned_user_ids = _wf_parse_assigned_user_ids(data)
        if not assigned_user_ids:
            return jsonify({"error": "Please select a user to send for approval."}), 400

        _ensure_workflow_tables()
        _ensure_workflow_adhoc_columns()

        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        # Every selected approver must be a real, active user.
        placeholders = ",".join("?" for _ in assigned_user_ids)
        cursor.execute(
            f"SELECT USER_ID FROM dbo.Sys_User WHERE USER_ID IN ({placeholders}) AND IsDeleted = 0",
            *assigned_user_ids,
        )
        found_ids = {r[0] for r in cursor.fetchall()}
        missing = [u for u in assigned_user_ids if u not in found_ids]
        if missing:
            conn.rollback()
            return jsonify({"error": "One or more selected users were not found."}), 404

        # The admin can require a minimum number of distinct approvers
        # before a submission is considered fully approved (Control Panel ->
        # Workflow Settings). The sender doesn't have to pick that many up
        # front — if fewer approve here, the last approver in this step gets
        # prompted to "Approve and Send" to more users until the minimum is
        # met (see api_workflow_approve).
        assigned_user_id = assigned_user_ids[0]  # kept for legacy single-user code paths below

        # 1) Create the workflow record — ad-hoc (no template/document link
        #    yet), owned by the submitting user, status Pending Approval.
        cursor.execute(
            """
            INSERT INTO dbo.WF_Instances
                (TemplateID, CurrentStepOrder, Status, SubmissionNumber,
                 SubmittedBy, SubmittedOn, IsDeleted,
                 Subject, Keywords, Notes, DocDate, ImportanceID,
                 EntityDeptID, FolderID, ExpiryDate)
            VALUES
                (NULL, 1, 'Pending Approval', 1,
                 ?, GETDATE(), 0,
                 ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            session["user_id"], topic, keywords, statement, doc_date, importance_id,
            dept_id, folder_id, expiry_date,
        )

        cursor.execute(
            """
            SELECT MAX(InstanceID) FROM dbo.WF_Instances WITH (UPDLOCK, HOLDLOCK)
            WHERE SubmittedBy = ? AND IsDeleted = 0
              AND SubmittedOn >= DATEADD(SECOND, -5, GETDATE())
            """,
            session["user_id"],
        )
        row = cursor.fetchone()
        instance_id = row[0] if row and row[0] else None
        if not instance_id:
            raise RuntimeError("Could not retrieve new workflow instance ID after INSERT")

        # 2) + 3) Single approval step, with all selected users assigned to
        # it (ApprovalMode 'A' — every one of them must approve).
        _wf_create_step_and_assign(cursor, instance_id, assigned_user_ids)

        # Document Attachment: save any files uploaded/scanned in the wizard
        # against this instance. Staged in WF_Instance_Attachments until
        # approval — see _archive_wf_instance_to_inquiries.
        if uploaded_files:
            _wf_save_uploaded_attachments(cursor, instance_id, uploaded_files, statement, session["user_id"])

        linked_attachment_ids = _wf_parse_linked_attachment_ids(data)
        if linked_attachment_ids:
            _wf_save_linked_attachments(cursor, instance_id, linked_attachment_ids, session["user_id"])

        conn.commit()

        email_warning = None
        for uid in assigned_user_ids:
            w = _wf_notify_assignment(cursor, instance_id, uid, topic, session["user_id"])
            email_warning = email_warning or w

        resp = {
            "success": True,
            "instance_id": instance_id,
            "status": "Pending Approval",
        }
        if email_warning:
            resp["warning"] = email_warning
        return jsonify(resp)
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Save Draft (persists the in-progress New Request form without
#         creating any workflow step/assignment or notifying anyone —
#         nothing happens for the recipient until the draft is sent) ───────
@app.route("/api/workflow/save-draft", methods=["POST"])
@login_required
def api_workflow_save_draft():
    conn = None
    try:
        is_multipart = request.content_type and "multipart/form-data" in request.content_type
        data = request.form if is_multipart else (request.get_json(silent=True) or {})
        uploaded_files = _wf_collect_uploaded_files(request, is_multipart)
        topic, keywords, statement, doc_date, importance_id, dept_id, folder_id, expiry_date = _wf_parse_submission_payload(data)
        if not topic:
            return jsonify({"error": "Topic / Subject is required to save a draft."}), 400

        _draft_ids = _wf_parse_assigned_user_ids(data)
        assigned_user_id = _draft_ids[0] if _draft_ids else None

        _ensure_workflow_tables()
        _ensure_workflow_adhoc_columns()

        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO dbo.WF_Instances
                (TemplateID, CurrentStepOrder, Status, SubmissionNumber,
                 SubmittedBy, SubmittedOn, IsDeleted,
                 Subject, Keywords, Notes, DocDate, ImportanceID, DraftAssignedTo,
                 EntityDeptID, FolderID, ExpiryDate)
            VALUES
                (NULL, NULL, 'Draft', 1,
                 ?, GETDATE(), 0,
                 ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            session["user_id"], topic, keywords, statement, doc_date, importance_id, assigned_user_id,
            dept_id, folder_id, expiry_date,
        )
        cursor.execute(
            """
            SELECT MAX(InstanceID) FROM dbo.WF_Instances WITH (UPDLOCK, HOLDLOCK)
            WHERE SubmittedBy = ? AND IsDeleted = 0 AND Status = 'Draft'
              AND SubmittedOn >= DATEADD(SECOND, -5, GETDATE())
            """,
            session["user_id"],
        )
        row = cursor.fetchone()
        instance_id = row[0] if row and row[0] else None
        if not instance_id:
            raise RuntimeError("Could not retrieve new draft ID after INSERT")

        if uploaded_files:
            _wf_save_uploaded_attachments(cursor, instance_id, uploaded_files, statement, session["user_id"])


        linked_attachment_ids = _wf_parse_linked_attachment_ids(data)
        if linked_attachment_ids:
            _wf_save_linked_attachments(cursor, instance_id, linked_attachment_ids, session["user_id"])
        conn.commit()
        return jsonify({"success": True, "instance_id": instance_id, "status": "Draft"})
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Update Draft (re-save an existing draft's fields; still 'Draft'
#         status, still no step/assignment/notification) ──────────────────
@app.route("/api/workflow/drafts/<int:instance_id>/update", methods=["POST"])
@login_required
def api_workflow_update_draft(instance_id):
    conn = None
    try:
        is_multipart = request.content_type and "multipart/form-data" in request.content_type
        data = request.form if is_multipart else (request.get_json(silent=True) or {})
        uploaded_files = _wf_collect_uploaded_files(request, is_multipart)
        topic, keywords, statement, doc_date, importance_id, dept_id, folder_id, expiry_date = _wf_parse_submission_payload(data)
        if not topic:
            return jsonify({"error": "Topic / Subject is required to save a draft."}), 400

        _draft_ids = _wf_parse_assigned_user_ids(data)
        assigned_user_id = _draft_ids[0] if _draft_ids else None

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SubmittedBy, Status FROM dbo.WF_Instances WHERE InstanceID = ? AND IsDeleted = 0",
            instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Draft not found."}), 404
        if row[0] != session["user_id"]:
            return jsonify({"error": "You do not own this draft."}), 403
        if row[1] != "Draft":
            return jsonify({"error": "This document has already been sent and can no longer be edited as a draft."}), 400

        cursor.execute(
            """
            UPDATE dbo.WF_Instances
            SET Subject = ?, Keywords = ?, Notes = ?, DocDate = ?, ImportanceID = ?,
                DraftAssignedTo = ?, EntityDeptID = ?, FolderID = ?, ExpiryDate = ?
            WHERE InstanceID = ?
            """,
            topic, keywords, statement, doc_date, importance_id, assigned_user_id,
            dept_id, folder_id, expiry_date, instance_id,
        )

        if uploaded_files:
            _wf_save_uploaded_attachments(cursor, instance_id, uploaded_files, statement, session["user_id"])

        linked_attachment_ids = _wf_parse_linked_attachment_ids(data)
        if linked_attachment_ids:
            _wf_save_linked_attachments(cursor, instance_id, linked_attachment_ids, session["user_id"])

        conn.commit()
        return jsonify({"success": True, "instance_id": instance_id, "status": "Draft"})
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: My Drafts (Save Draft items I haven't sent yet) ───────────────────
@app.route("/api/workflow/drafts")
@login_required
def api_workflow_drafts():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.Subject, wi.Keywords, wi.Notes, wi.DocDate,
                   wi.ImportanceID, wi.DraftAssignedTo, wi.SubmittedOn, su.USER_FULLNAME, su.USER_NAME,
                   wi.EntityDeptID, wi.FolderID, wi.ExpiryDate
            FROM dbo.WF_Instances wi
            LEFT JOIN dbo.Sys_User su ON su.USER_ID = wi.DraftAssignedTo
            WHERE wi.SubmittedBy = ? AND wi.IsDeleted = 0 AND wi.Status = 'Draft'
            ORDER BY wi.SubmittedOn DESC
            """,
            session["user_id"],
        )
        items = [{
            "instance_id": r[0], "subject": r[1] or "", "keywords": r[2] or "",
            "statement": r[3] or "", "doc_date": str(r[4]) if r[4] else "",
            "importance_id": r[5], "assigned_user_id": r[6],
            "assigned_user_name": (r[8] or r[9] or "") if r[6] else "",
            "saved_on": r[7].isoformat() if r[7] else None,
            "dept_id": r[10], "folder_id": r[11],
            "expiry_date": str(r[12]) if r[12] else "",
        } for r in cursor.fetchall()]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Delete Draft (only while still a Draft, only by its owner) ────────
@app.route("/api/workflow/drafts/<int:instance_id>", methods=["DELETE"])
@login_required
def api_workflow_delete_draft(instance_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SubmittedBy, Status FROM dbo.WF_Instances WHERE InstanceID = ? AND IsDeleted = 0",
            instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Draft not found."}), 404
        if row[0] != session["user_id"]:
            return jsonify({"error": "You do not own this draft."}), 403
        if row[1] != "Draft":
            return jsonify({"error": "Only drafts that haven't been sent yet can be deleted."}), 400

        cursor.execute("UPDATE dbo.WF_Instances SET IsDeleted = 1 WHERE InstanceID = ?", instance_id)
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Send Draft (promotes a saved Draft into a real submission —
#         creates the step/assignment, flips Status to Pending Approval,
#         and fires the same notifications as a brand-new submission) ─────
@app.route("/api/workflow/drafts/<int:instance_id>/send", methods=["POST"])
@login_required
def api_workflow_send_draft(instance_id):
    conn = None
    try:
        data = request.get_json(silent=True) or request.form or {}
        assigned_user_ids = _wf_parse_assigned_user_ids(data)
        dept_id = data.get("dept_id")
        try:
            dept_id = int(dept_id) if dept_id not in (None, "") else None
        except (TypeError, ValueError):
            dept_id = None

        conn = get_db_connection()
        conn.autocommit = False
        cursor = conn.cursor()

        cursor.execute(
            """SELECT SubmittedBy, Status, Subject, DraftAssignedTo, EntityDeptID
               FROM dbo.WF_Instances WHERE InstanceID = ? AND IsDeleted = 0""",
            instance_id,
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"error": "Draft not found."}), 404
        submitted_by, status, topic, draft_assigned_to, draft_dept_id = row
        if submitted_by != session["user_id"]:
            conn.rollback()
            return jsonify({"error": "You do not own this draft."}), 403
        if status != "Draft":
            conn.rollback()
            return jsonify({"error": "This document has already been sent."}), 400

        # The department can be confirmed/changed at send time, or fall back
        # to whichever entity was picked while the draft was being edited.
        if not dept_id:
            dept_id = draft_dept_id
        if not dept_id:
            conn.rollback()
            return jsonify({"error": "Please select an Entity / Department."}), 400

        # The recipient(s) can be confirmed/changed at send time, or fall
        # back to whichever single user was picked while the draft was
        # being edited (drafts only ever stage one recipient).
        if not assigned_user_ids and draft_assigned_to:
            assigned_user_ids = [draft_assigned_to]
        if not assigned_user_ids:
            conn.rollback()
            return jsonify({"error": "Please select a user to send for approval."}), 400

        placeholders = ",".join("?" for _ in assigned_user_ids)
        cursor.execute(
            f"SELECT USER_ID FROM dbo.Sys_User WHERE USER_ID IN ({placeholders}) AND IsDeleted = 0",
            *assigned_user_ids,
        )
        found_ids = {r[0] for r in cursor.fetchall()}
        missing = [u for u in assigned_user_ids if u not in found_ids]
        if missing:
            conn.rollback()
            return jsonify({"error": "One or more selected users were not found."}), 404

        cursor.execute(
            """
            UPDATE dbo.WF_Instances
            SET Status = 'Pending Approval', CurrentStepOrder = 1,
                SubmittedOn = GETDATE(), DraftAssignedTo = ?, EntityDeptID = ?
            WHERE InstanceID = ?
            """,
            assigned_user_ids[0], dept_id, instance_id,
        )
        _wf_create_step_and_assign(cursor, instance_id, assigned_user_ids)
        cursor.execute(
            """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
               VALUES (?, 1, NULL, ?, 'DRAFT_SENT', GETDATE(), NULL)""",
            instance_id, session["user_id"],
        )
        conn.commit()

        email_warning = None
        for uid in assigned_user_ids:
            w = _wf_notify_assignment(cursor, instance_id, uid, topic, session["user_id"])
            email_warning = email_warning or w

        resp = {"success": True, "instance_id": instance_id, "status": "Pending Approval"}
        if email_warning:
            resp["warning"] = email_warning
        return jsonify(resp)
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Document Attachment — list/preview/download files attached to a
#         workflow instance, from Inbox, Sent, History, or the approver's
#         review screen. Works across the whole lifecycle: while pending,
#         files live in WF_Instance_Attachments; once approved, the real
#         copies live in Adco_Transactions_Attachments (see
#         _archive_wf_instance_to_inquiries) and this transparently switches
#         over — same instance_id, same UI, no client-side branching needed.
@app.route("/api/workflow/instances/<int:instance_id>/attachments")
@login_required
def api_wf_instance_attachments(instance_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        attachments = _wf_list_instance_attachments(cursor, instance_id)
        return jsonify({"attachments": attachments})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


def _api_wf_attachment_serve(instance_id: int, attachment_id: int, *, as_attachment: bool):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT Transaction_ID FROM dbo.WF_Instances WHERE InstanceID = ?", instance_id)
        row = cursor.fetchone()
        transaction_id = row[0] if row else None

        if transaction_id:
            # Already approved — the real copy lives in the standard archive
            # table; reuse the exact same serving path Archive/Inquiries use.
            cursor.execute(
                f"""SELECT File_URL, File_Name FROM dbo.Adco_Transactions_Attachments
                    WHERE ID = ? AND Transaction_ID = ? AND {_attachments_active_sql()}""",
                attachment_id, transaction_id,
            )
        else:
            cursor.execute(
                """SELECT File_URL, File_Name FROM dbo.WF_Instance_Attachments
                   WHERE ID = ? AND WF_Instance_ID = ? AND IsDeleted = 0""",
                attachment_id, instance_id,
            )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Attachment not found"}), 404
        file_url, file_name = (row[0] or "").strip(), row[1] or "download"
    finally:
        if conn:
            conn.close()

    response = _serve_attachment_response(file_url, file_name, as_attachment=as_attachment)
    if response is not None:
        return response
    return jsonify({
        "error": "File not found on server",
        "hint": "Set ATTACHMENT_FILES_ROOT in .env if files live on a network share",
        "file_url": file_url,
    }), 404


@app.route("/api/workflow/instances/<int:instance_id>/attachments/<int:attachment_id>/preview")
@login_required
def api_wf_attachment_preview(instance_id, attachment_id):
    audit_log("PREVIEW", page_id=4, notes=f"Previewed workflow attachment ID {attachment_id} (instance {instance_id})")
    return _api_wf_attachment_serve(instance_id, attachment_id, as_attachment=False)


@app.route("/api/workflow/instances/<int:instance_id>/attachments/<int:attachment_id>/download")
@login_required
def api_wf_attachment_download(instance_id, attachment_id):
    audit_log("DOWNLOAD", page_id=4, notes=f"Downloaded workflow attachment ID {attachment_id} (instance {instance_id})")
    return _api_wf_attachment_serve(instance_id, attachment_id, as_attachment=True)


# ── API: My Workflow Inbox (items currently waiting on me) ─────────────────
@app.route("/api/workflow/inbox")
@login_required
def api_workflow_inbox():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.SubmissionNumber, wis.StepName,
                   u.USER_NAME, COALESCE(t.Subject, wi.Subject)
            FROM dbo.WF_Instance_Assignments wa
            JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
            JOIN dbo.WF_Instances wi ON wi.InstanceID = wis.InstanceID
                AND wi.SubmissionNumber = wis.SubmissionNumber
                AND wi.CurrentStepOrder = wis.StepOrder
            JOIN dbo.Sys_User u ON u.USER_ID = wi.SubmittedBy
            LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
            WHERE wa.AssignedTo = ? AND wa.Status = 'Waiting'
              AND wi.Status IN ('Pending', 'Pending Approval', 'Viewed', 'In Progress')
            ORDER BY wi.SubmittedOn ASC
            """,
            session["user_id"],
        )
        items = [{
            "instance_id": r[0], "submission_number": r[1], "step_name": r[2],
            "submitted_by_name": r[3], "subject": r[4],
        } for r in cursor.fetchall()]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: My Sent items (documents I submitted into a workflow) ─────────────
@app.route("/api/workflow/sent/<int:instance_id>", methods=["DELETE"])
@login_required
def api_workflow_delete_sent(instance_id):
    """Remove an item from the current user's Sent list only.
    Unlike deleting a Draft (which requires Status == 'Draft'), this works
    for any status — Pending, Approved, Rejected, etc. Sets HiddenFromSent
    (sender-only, Sent-tab-only flag) rather than IsDeleted — IsDeleted is
    the flag every workflow list (Inbox, History, expiry alerts, ...)
    filters on, so setting it here would have made the instance vanish
    everywhere, not just from the sender's own Sent view. It never touches
    the underlying archived document/registration, which lives
    independently once approved.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT SubmittedBy FROM dbo.WF_Instances WHERE InstanceID = ? AND IsDeleted = 0",
            instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Item not found."}), 404
        if row[0] != session["user_id"]:
            return jsonify({"error": "You do not own this item."}), 403

        cursor.execute("UPDATE dbo.WF_Instances SET HiddenFromSent = 1 WHERE InstanceID = ?", instance_id)
        conn.commit()
        return jsonify({"success": True})
    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/workflow/sent")
@login_required
def api_workflow_sent():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.Status, wi.SubmissionNumber,
                   COALESCE(t.Subject, wi.Subject), wi.ViewedOn, wi.ExpiryDate
            FROM dbo.WF_Instances wi
            LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
            WHERE wi.SubmittedBy = ? AND wi.IsDeleted = 0
              AND (wi.HiddenFromSent = 0 OR wi.HiddenFromSent IS NULL)
            ORDER BY wi.SubmittedOn DESC
            """,
            session["user_id"],
        )
        items = [{
            "instance_id": r[0], "status": r[1], "submission_number": r[2], "subject": r[3],
            "viewed_on": r[4].isoformat() if r[4] else None,
            "expiry_date": r[5].isoformat() if r[5] else None,
        } for r in cursor.fetchall()]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Workflow documents (submitted by OR assigned to the current user)
#    whose ExpiryDate falls within the user's configured alert window ──────
@app.route("/api/workflow/expiry-alerts", methods=["GET"])
@login_required
def api_workflow_expiry_alerts():
    """Returns { alerts: [{ instance_id, subject, expiry_date, days_remaining }] }
    for documents expiring within today … today + WfExpiryAlertDays days.
    Returns [] if the user hasn't set an alert-days preference (NULL)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        user_id = session["user_id"]
        cursor.execute(
            "SELECT WfExpiryAlertDays FROM dbo.Sys_User WHERE USER_ID = ?",
            user_id,
        )
        row = cursor.fetchone()
        alert_days = row[0] if row and row[0] is not None else None
        if alert_days is None:
            return jsonify({"alerts": []})

        cursor.execute(
            """
            SELECT DISTINCT wi.InstanceID, COALESCE(t.Subject, wi.Subject) AS Subject,
                   wi.ExpiryDate
            FROM dbo.WF_Instances wi
            LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
            LEFT JOIN dbo.WF_Instance_Steps wis ON wis.InstanceID = wi.InstanceID
            LEFT JOIN dbo.WF_Instance_Assignments wa ON wa.InstanceStepID = wis.InstanceStepID
            WHERE wi.IsDeleted = 0
              AND wi.ExpiryDate IS NOT NULL
              AND wi.ExpiryDate >= CAST(GETDATE() AS DATE)
              AND wi.ExpiryDate <= DATEADD(DAY, ?, CAST(GETDATE() AS DATE))
              AND wi.Status NOT IN ('Approved', 'Rejected')
              AND (
                    (wa.AssignedTo = ? AND wa.Status = 'Waiting')
                 OR (wi.SubmittedBy = ?)
                  )
            ORDER BY wi.ExpiryDate ASC
            """,
            alert_days, user_id, user_id,
        )
        today = datetime.now().date()
        alerts = []
        for r in cursor.fetchall():
            expiry = r[2]
            days_remaining = (expiry - today).days if expiry else None
            alerts.append({
                "instance_id": r[0], "subject": r[1] or f"Document #{r[0]}",
                "expiry_date": expiry.isoformat() if expiry else None,
                "days_remaining": days_remaining,
            })
        return jsonify({"alerts": alerts})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Approve the current step of a workflow instance ───────────────────
@app.route("/api/workflow/instances/<int:instance_id>/approve", methods=["POST"])
@login_required
def api_workflow_approve(instance_id):
    if not _check_accr(WF_PAGE_ID, "Can_Approve"):
        return jsonify({"error": "Access denied: you do not have approval permission."}), 403

    user_id = session["user_id"]
    data = request.get_json(silent=True) or {}
    next_user_ids = _wf_parse_assigned_user_ids(data) if data else []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.Transaction_ID, wi.SubmissionNumber, wi.TemplateID,
                   wis.InstanceStepID, wis.StepOrder, wis.ApprovalMode, wa.AssignmentID, wi.Subject
            FROM dbo.WF_Instances wi
            JOIN dbo.WF_Instance_Steps wis
                ON wis.InstanceID = wi.InstanceID AND wis.SubmissionNumber = wi.SubmissionNumber
               AND wis.StepOrder = wi.CurrentStepOrder
            JOIN dbo.WF_Instance_Assignments wa
                ON wa.InstanceStepID = wis.InstanceStepID AND wa.AssignedTo = ? AND wa.Status = 'Waiting'
            WHERE wi.InstanceID = ? AND wi.Status IN ('Pending', 'Pending Approval', 'Viewed', 'In Progress')
            """,
            user_id, instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "No pending approval assigned to you for this document."}), 404

        (_, transaction_id, submission_no, template_id,
         instance_step_id, step_order, approval_mode, assignment_id, wf_subject) = row
        wf_subject = wf_subject or f"Document #{instance_id}"
        _, approver_name = _get_user_email(cursor, user_id)

        cursor.execute(
            "UPDATE dbo.WF_Instance_Assignments SET Status = 'Approved', ActedOn = GETDATE() WHERE AssignmentID = ?",
            assignment_id,
        )

        # ApprovalMode 'A' (ALL) needs every assignment on this step Approved
        # before the step itself completes; 'O' (ANY) completes on the first.
        cursor.execute(
            "SELECT COUNT(*) FROM dbo.WF_Instance_Assignments WHERE InstanceStepID = ? AND Status = 'Waiting'",
            instance_step_id,
        )
        still_waiting = cursor.fetchone()[0]
        step_done = (approval_mode == 'O') or (still_waiting == 0)

        cursor.execute(
            """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
               VALUES (?, ?, ?, ?, 'APPROVED', GETDATE(), NULL)""",
            instance_id, submission_no, instance_step_id, user_id,
        )

        if not step_done:
            conn.commit()
            audit_log("WF_STEP_APPROVED", page_id=WF_PAGE_ID,
                      notes=f"Instance {instance_id}: one of several approvers on step {step_order} approved")
            return jsonify({"success": True, "instance_id": instance_id, "status": "Pending"})

        cursor.execute(
            "UPDATE dbo.WF_Instance_Steps SET Status = 'Approved', CompletedOn = GETDATE() WHERE InstanceStepID = ?",
            instance_step_id,
        )

        # ── Ad-hoc (template-less) instances: admin minimum-approvals check ──
        # These submissions have no WF_Templates chain, so normally they'd
        # archive immediately once their one step is done. If the admin has
        # set a minimum number of distinct approvers (Control Panel ->
        # Workflow Settings) and that minimum hasn't been reached yet, the
        # approver must use "Approve and Send" to forward to more user(s)
        # instead of completing here.
        if template_id is None:
            cursor.execute(
                """
                SELECT COUNT(DISTINCT wa.AssignedTo)
                FROM dbo.WF_Instance_Assignments wa
                JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
                WHERE wis.InstanceID = ? AND wis.SubmissionNumber = ? AND wa.Status = 'Approved'
                """,
                instance_id, submission_no,
            )
            approved_so_far = cursor.fetchone()[0]
            min_approvals = get_wf_min_approvals(cursor)

            if approved_so_far < min_approvals:
                if not next_user_ids:
                    conn.rollback()
                    remaining = min_approvals - approved_so_far
                    return jsonify({
                        "error": f"You need to send to {remaining} more user(s) for approval before this can be completed.",
                        "needs_more_approvers": True,
                        "min_approvals": min_approvals,
                        "current_approvals": approved_so_far,
                    }), 400

                # "Approve and Send" — validate the chosen users, then chain
                # a brand-new ad-hoc step onto them.
                placeholders = ",".join("?" for _ in next_user_ids)
                cursor.execute(
                    f"SELECT USER_ID FROM dbo.Sys_User WHERE USER_ID IN ({placeholders}) AND IsDeleted = 0",
                    *next_user_ids,
                )
                found_ids = {r[0] for r in cursor.fetchall()}
                missing = [u for u in next_user_ids if u not in found_ids]
                if missing:
                    conn.rollback()
                    return jsonify({"error": "One or more selected users were not found."}), 404

                next_order = step_order + 1
                new_step_id = _wf_create_adhoc_step(cursor, instance_id, submission_no, next_order, next_user_ids)
                cursor.execute(
                    "UPDATE dbo.WF_Instances SET CurrentStepOrder = ? WHERE InstanceID = ?",
                    next_order, instance_id,
                )
                conn.commit()

                email_warning = None
                for uid in next_user_ids:
                    notify_dept_users_single(target_user_id=uid, action_type="WF_SUBMITTED",
                                              doc_id=instance_id, subject=wf_subject, actor_id=user_id)
                    w = _wf_notify_assignment(cursor, instance_id, uid, wf_subject, user_id)
                    email_warning = email_warning or w

                audit_log("WF_STEP_APPROVED", page_id=WF_PAGE_ID,
                          notes=f"Instance {instance_id}: step {step_order} approved by {user_id} and "
                                f"forwarded to {len(next_user_ids)} more user(s) to satisfy minimum approvals")
                resp = {"success": True, "instance_id": instance_id, "status": "Pending"}
                if email_warning:
                    resp["warning"] = email_warning
                return jsonify(resp)
            # else: minimum met — fall through to the normal archive path
            # below (no template next-step will be found since template_id
            # is None, so it archives immediately, same as before).

        # Is there a next step in the template?
        cursor.execute(
            """SELECT StepID, StepOrder, StepName, ApprovalMode FROM dbo.WF_Template_Steps
               WHERE TemplateID = ? AND StepOrder = ? AND IsActive = 1""",
            template_id, step_order + 1,
        )
        next_step = cursor.fetchone()

        if next_step:
            next_step_id, next_order, next_name, next_mode = next_step
            cursor.execute(
                """UPDATE dbo.WF_Instances SET CurrentStepOrder = ? WHERE InstanceID = ?""",
                next_order, instance_id,
            )
            cursor.execute(
                """INSERT INTO dbo.WF_Instance_Steps
                       (InstanceID, SubmissionNumber, StepOrder, StepName, ApprovalMode, Status, StartedOn)
                   OUTPUT INSERTED.InstanceStepID
                   VALUES (?, ?, ?, ?, ?, 'InProgress', GETDATE())""",
                instance_id, submission_no, next_order, next_name, next_mode,
            )
            new_step_id = cursor.fetchone()[0]
            next_assignees = _resolve_step_assignees(cursor, next_step_id)
            for uid in next_assignees:
                cursor.execute(
                    """INSERT INTO dbo.WF_Instance_Assignments (InstanceStepID, AssignedTo, Status, AssignedOn)
                       VALUES (?, ?, 'Waiting', GETDATE())""",
                    new_step_id, uid,
                )
            conn.commit()

            email_warning = None
            for uid in next_assignees:
                notify_dept_users_single(target_user_id=uid, action_type="WF_SUBMITTED",
                                          doc_id=instance_id, subject=f"Step {next_order}", actor_id=user_id)
                assignee_email, assignee_name = _get_user_email(cursor, uid)
                w = _wf_send_notification_email(
                    recipient_email=assignee_email, recipient_name=assignee_name,
                    subject=f"Document Assigned to You for Review — {wf_subject}",
                    heading="You have received a document requiring your review.",
                    body_lines=[("Document", wf_subject), ("Step", next_name),
                                ("Date", datetime.now().strftime("%Y-%m-%d"))],
                    instance_id=instance_id, button_label="Review Document",
                )
                email_warning = email_warning or w

            audit_log("WF_STEP_APPROVED", page_id=WF_PAGE_ID,
                      notes=f"Instance {instance_id}: step {step_order} approved, advanced to step {next_order}")
            resp = {"success": True, "instance_id": instance_id, "status": "Pending"}
            if email_warning:
                resp["warning"] = email_warning
            return jsonify(resp)

        # No further steps — fully approved. Only NOW do we archive the
        # document into Inquiries (Adco_Transactions) using the existing
        # archive logic, which is also where the registration number
        # (Adco_Transactions.ID) gets generated for the very first time.
        cursor.execute(
            """UPDATE dbo.WF_Instances SET Status = 'Approved', CurrentStepOrder = NULL, CompletedOn = GETDATE()
               WHERE InstanceID = ?""",
            instance_id,
        )
        registration_number = _archive_wf_instance_to_inquiries(cursor, instance_id)
        cursor.execute(
            """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
               VALUES (?, ?, ?, ?, 'ARCHIVED', GETDATE(), ?)""",
            instance_id, submission_no, instance_step_id, user_id, f"Registration number {registration_number}",
        )
        conn.commit()

        cursor.execute("SELECT SubmittedBy FROM dbo.WF_Instances WHERE InstanceID = ?", instance_id)
        submitter_id = cursor.fetchone()[0]
        notify_dept_users_single(target_user_id=submitter_id, action_type="WF_APPROVED",
                                  doc_id=instance_id, subject="Fully approved", actor_id=user_id)

        submitter_email, submitter_name = _get_user_email(cursor, submitter_id)
        email_warning = _wf_send_notification_email(
            recipient_email=submitter_email, recipient_name=submitter_name,
            subject=f"Document Approved — {wf_subject}",
            heading=f"Your document was approved by {approver_name or 'the approver'} and has been archived.",
            body_lines=[("Document", wf_subject), ("Approved By", approver_name or ""),
                        ("Registration Number", str(registration_number)),
                        ("Date", datetime.now().strftime("%Y-%m-%d"))],
            instance_id=instance_id, button_label="View Document",
        )

        audit_log("WF_APPROVED", page_id=WF_PAGE_ID,
                  notes=f"Instance {instance_id} fully approved and archived as registration number {registration_number}")
        resp = {
            "success": True,
            "instance_id": instance_id,
            "status": "Approved",
            "registration_number": str(registration_number),
        }
        if email_warning:
            resp["warning"] = email_warning
        return jsonify(resp)

    except Exception as exc:
        if conn:
            conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Approval failed: {exc}"}), 500
    finally:
        if conn:
            conn.close()


# ── API: Forward a workflow instance to another user ────────────────────────
@app.route("/api/workflow/instances/<int:instance_id>/forward", methods=["POST"])
@login_required
def api_workflow_forward(instance_id):
    if not _check_accr(WF_PAGE_ID, "Can_Approve"):
        return jsonify({"error": "Access denied: you do not have approval permission."}), 403

    data = request.get_json(silent=True) or {}
    target_user_id = data.get("target_user_id")
    if not target_user_id:
        return jsonify({"error": "Please select a user to forward this document to."}), 400
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid user selected."}), 400

    note = (data.get("note") or "").strip() or None
    user_id = session["user_id"]

    if target_user_id == user_id:
        return jsonify({"error": "You cannot forward a document to yourself."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # The instance must currently be assigned (Waiting) to the logged-in
        # user on its current step — same lookup pattern as Approve/Reject.
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.SubmissionNumber, wis.InstanceStepID, wa.AssignmentID, wi.Subject
            FROM dbo.WF_Instances wi
            JOIN dbo.WF_Instance_Steps wis
                ON wis.InstanceID = wi.InstanceID AND wis.SubmissionNumber = wi.SubmissionNumber
               AND wis.StepOrder = wi.CurrentStepOrder
            JOIN dbo.WF_Instance_Assignments wa
                ON wa.InstanceStepID = wis.InstanceStepID AND wa.AssignedTo = ? AND wa.Status = 'Waiting'
            WHERE wi.InstanceID = ? AND wi.Status IN ('Pending', 'Pending Approval', 'Viewed', 'In Progress')
            """,
            user_id, instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "No pending assignment found for you on this document."}), 404

        _, submission_no, instance_step_id, assignment_id, wf_subject = row
        wf_subject = wf_subject or f"Document #{instance_id}"

        # Target must be a real, active user.
        cursor.execute(
            "SELECT USER_ID FROM dbo.Sys_User WHERE USER_ID = ? AND IsDeleted = 0",
            target_user_id,
        )
        if not cursor.fetchone():
            return jsonify({"error": "Selected user was not found."}), 404

        # Close out the current assignment as Forwarded (not Approved/Rejected)
        # so it's clearly distinguishable in the history, and hand the same
        # step off to the new assignee — the InstanceID and step never change,
        # only who currently holds it.
        cursor.execute(
            "UPDATE dbo.WF_Instance_Assignments SET Status = 'Forwarded', ActedOn = GETDATE() WHERE AssignmentID = ?",
            assignment_id,
        )
        cursor.execute(
            """INSERT INTO dbo.WF_Instance_Assignments (InstanceStepID, AssignedTo, Status, AssignedOn, DelegatedFrom)
               VALUES (?, ?, 'Waiting', GETDATE(), ?)""",
            instance_step_id, target_user_id, user_id,
        )

        # NOTE: we deliberately do NOT change wi.Status here. Every other
        # query (Inbox, Approve, Reject, Forward) only considers instances
        # whose Status is IN ('Pending', 'Pending Approval', 'Viewed',
        # 'In Progress'). Setting Status to 'Forwarded' used to knock the
        # document out of that set entirely — it would vanish from
        # everyone's Inbox and the new assignee could never act on it.
        # The instance's overall status hasn't actually changed: it's still
        # waiting on the same step, just held by a different assignee.

        cursor.execute(
            """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
               VALUES (?, ?, ?, ?, 'FORWARDED', GETDATE(), ?)""",
            instance_id, submission_no, instance_step_id, user_id,
            f"Forwarded to user ID {target_user_id}" + (f": {note}" if note else ""),
        )
        conn.commit()

        forwarder_email, forwarder_name = _get_user_email(cursor, user_id)
        target_email, target_name = _get_user_email(cursor, target_user_id)

    except Exception as exc:
        if conn:
            conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Forward failed: {exc}"}), 500
    finally:
        if conn:
            conn.close()

    notify_dept_users_single(target_user_id=target_user_id, action_type="WF_FORWARDED",
                              doc_id=instance_id, subject=note or "Document forwarded to you",
                              actor_id=user_id)

    email_warning = _wf_send_notification_email(
        recipient_email=target_email, recipient_name=target_name,
        subject=f"Document Forwarded to You for Review — {wf_subject}",
        heading=f"{forwarder_name or 'A colleague'} forwarded a document to you for review.",
        body_lines=[("Document", wf_subject), ("From", forwarder_name or ""),
                    ("Note", note or ""), ("Date", datetime.now().strftime("%Y-%m-%d"))],
        instance_id=instance_id, button_label="Review Document",
    )

    audit_log("WF_FORWARDED", page_id=WF_PAGE_ID,
              notes=f"Instance {instance_id} (submission {submission_no}) forwarded by user {user_id} to user {target_user_id}")

    resp = {"success": True, "instance_id": instance_id, "status": "Forwarded"}
    if email_warning:
        resp["warning"] = email_warning
    return jsonify(resp)


# ── API: Reject a workflow instance ─────────────────────────────────────────
@app.route("/api/workflow/instances/<int:instance_id>/reject", methods=["POST"])
@login_required
def api_workflow_reject(instance_id):
    if not _check_accr(WF_PAGE_ID, "Can_Approve"):
        return jsonify({"error": "Access denied: you do not have approval permission."}), 403

    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "A rejection reason is required."}), 400

    user_id = session["user_id"]
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wi.InstanceID, wi.Transaction_ID, wi.SubmissionNumber,
                   wi.SubmittedBy, wi.Status, wis.InstanceStepID, wa.AssignmentID, wa.Status, wi.Subject
            FROM dbo.WF_Instances wi
            JOIN dbo.WF_Instance_Steps wis
                ON wis.InstanceID = wi.InstanceID AND wis.SubmissionNumber = wi.SubmissionNumber
               AND wis.StepOrder = wi.CurrentStepOrder
            JOIN dbo.WF_Instance_Assignments wa
                ON wa.InstanceStepID = wis.InstanceStepID AND wa.AssignedTo = ? AND wa.Status = 'Waiting'
            WHERE wi.InstanceID = ? AND wi.Status IN ('Pending', 'Pending Approval', 'Viewed', 'In Progress')
            """,
            user_id, instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "No pending approval assigned to you for this document."}), 404

        (_, transaction_id, submission_no, submitted_by, _status,
         instance_step_id, assignment_id, _a_status, wf_subject) = row
        wf_subject = wf_subject or (f"Transaction #{transaction_id}" if transaction_id else f"Document #{instance_id}")

        cursor.execute(
            "UPDATE dbo.WF_Instance_Assignments SET Status = 'Rejected', ActedOn = GETDATE() WHERE AssignmentID = ?",
            assignment_id,
        )
        cursor.execute(
            "UPDATE dbo.WF_Instance_Steps SET Status = 'Rejected', CompletedOn = GETDATE() WHERE InstanceStepID = ?",
            instance_step_id,
        )
        cursor.execute(
            """UPDATE dbo.WF_Instances SET Status = 'Rejected', CurrentStepOrder = NULL, CompletedOn = GETDATE()
               WHERE InstanceID = ?""",
            instance_id,
        )
        cursor.execute(
            """INSERT INTO dbo.WF_Comments (InstanceID, InstanceStepID, CommentBy, CommentText, CreatedOn, IsDeleted)
               VALUES (?, ?, ?, ?, GETDATE(), 0)""",
            instance_id, instance_step_id, user_id, reason,
        )
        cursor.execute(
            """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
               VALUES (?, ?, ?, ?, 'REJECTED', GETDATE(), ?)""",
            instance_id, submission_no, instance_step_id, user_id, reason,
        )
        conn.commit()

        rejector_email, rejector_name = _get_user_email(cursor, user_id)
        sender_email, sender_name = _get_user_email(cursor, submitted_by)

    except Exception as exc:
        if conn:
            conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Rejection failed: {exc}"}), 500
    finally:
        if conn:
            conn.close()

    notify_dept_users_single(target_user_id=submitted_by, action_type="WF_REJECTED",
                              doc_id=instance_id, subject=reason, actor_id=user_id)

    email_warning = _wf_send_notification_email(
        recipient_email=sender_email, recipient_name=sender_name,
        subject=f"Document Rejected — Action Required — {wf_subject}",
        heading=f"Your document was rejected by {rejector_name or 'the approver'}.",
        body_lines=[("Document", wf_subject), ("Rejected By", rejector_name or ""),
                    ("Reason", reason), ("Date", datetime.now().strftime("%Y-%m-%d"))],
        instance_id=instance_id, button_label="Edit & Resubmit",
    )

    audit_log("WF_REJECTED", page_id=WF_PAGE_ID,
              notes=f"Instance {instance_id} (submission {submission_no}) rejected: {reason}")

    resp = {"success": True, "instance_id": instance_id, "status": "Rejected"}
    if email_warning:
        resp["warning"] = email_warning
    return jsonify(resp)


# ── API: Prior rejection comments, shown before resubmitting ───────────────
@app.route("/api/workflow/instances/<int:instance_id>/rejection-context", methods=["GET"])
@login_required
def api_workflow_rejection_context(instance_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT SubmittedBy FROM dbo.WF_Instances WHERE InstanceID = ?", instance_id)
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Workflow instance not found."}), 404
        if row[0] != session["user_id"]:
            return jsonify({"error": "Only the original sender can view this."}), 403

        cursor.execute(
            """
            SELECT wc.CommentText, wc.CreatedOn, u.USER_NAME, wh.SubmissionNumber
            FROM dbo.WF_Comments wc
            JOIN dbo.Sys_User u ON u.USER_ID = wc.CommentBy
            LEFT JOIN dbo.WF_History wh ON wh.InstanceStepID = wc.InstanceStepID AND wh.ActionType = 'REJECTED'
            WHERE wc.InstanceID = ? AND wc.IsDeleted = 0
            ORDER BY wc.CreatedOn ASC
            """,
            instance_id,
        )
        comments = [{"text": r[0], "on": r[1].isoformat(), "by": r[2], "submission": r[3]}
                    for r in cursor.fetchall()]
        return jsonify({"success": True, "comments": comments})
    finally:
        if conn:
            conn.close()


# ── API: Resubmit a rejected instance (same InstanceID, new SubmissionNumber) ─
@app.route("/api/workflow/instances/<int:instance_id>/resubmit", methods=["POST"])
@login_required
def api_workflow_resubmit(instance_id):
    data = request.get_json(silent=True) or {}
    resubmission_comment = (data.get("comment") or "").strip()

    user_id = session["user_id"]
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT InstanceID, TemplateID, SubmittedBy, Status, SubmissionNumber, Subject FROM dbo.WF_Instances WHERE InstanceID = ?",
            instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Workflow instance not found."}), 404

        _, template_id, submitted_by, status, submission_no, wf_subject = row
        wf_subject = wf_subject or f"Document #{instance_id}"
        if submitted_by != user_id:
            return jsonify({"error": "Only the original sender can resubmit this document."}), 403
        if status != "Rejected":
            return jsonify({"error": f"Document is not in a rejected state (current: {status})."}), 400

        new_submission_no = submission_no + 1

        cursor.execute(
            """SELECT StepID, StepOrder, StepName, ApprovalMode FROM dbo.WF_Template_Steps
               WHERE TemplateID = ? AND StepOrder = 1 AND IsActive = 1""",
            template_id,
        )
        step1 = cursor.fetchone()
        if not step1:
            return jsonify({"error": "Workflow template has no active Step 1 — cannot resubmit."}), 500
        step_id, step_order, step_name, approval_mode = step1

        cursor.execute(
            """UPDATE dbo.WF_Instances SET Status = 'Pending Approval', CurrentStepOrder = 1,
                   SubmissionNumber = ?, CompletedOn = NULL WHERE InstanceID = ?""",
            new_submission_no, instance_id,
        )
        cursor.execute(
            """INSERT INTO dbo.WF_Instance_Steps
                   (InstanceID, SubmissionNumber, StepOrder, StepName, ApprovalMode, Status, StartedOn)
               OUTPUT INSERTED.InstanceStepID
               VALUES (?, ?, ?, ?, ?, 'InProgress', GETDATE())""",
            instance_id, new_submission_no, step_order, step_name, approval_mode,
        )
        new_instance_step_id = cursor.fetchone()[0]

        resolved_user_ids = _resolve_step_assignees(cursor, step_id)
        for uid in resolved_user_ids:
            cursor.execute(
                """INSERT INTO dbo.WF_Instance_Assignments (InstanceStepID, AssignedTo, Status, AssignedOn)
                   VALUES (?, ?, 'Waiting', GETDATE())""",
                new_instance_step_id, uid,
            )

        cursor.execute(
            """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
               VALUES (?, ?, ?, ?, 'RESUBMITTED', GETDATE(), NULL)""",
            instance_id, new_submission_no, new_instance_step_id, user_id,
        )

        if resubmission_comment:
            cursor.execute(
                """INSERT INTO dbo.WF_Comments (InstanceID, InstanceStepID, CommentBy, CommentText, CreatedOn, IsDeleted)
                   VALUES (?, ?, ?, ?, GETDATE(), 0)""",
                instance_id, new_instance_step_id, user_id, resubmission_comment,
            )
            cursor.execute(
                """INSERT INTO dbo.WF_History (InstanceID, SubmissionNumber, InstanceStepID, ActionBy, ActionType, ActionOn, Notes)
                   VALUES (?, ?, ?, ?, 'COMMENT', GETDATE(), ?)""",
                instance_id, new_submission_no, new_instance_step_id, user_id, resubmission_comment,
            )

        conn.commit()

        _, resubmitter_name = _get_user_email(cursor, user_id)
        assignee_contacts = [(_get_user_email(cursor, uid)) for uid in resolved_user_ids]

    except Exception as exc:
        if conn:
            conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Resubmission failed: {exc}"}), 500
    finally:
        if conn:
            conn.close()

    email_warning = None
    for uid, (assignee_email, assignee_name) in zip(resolved_user_ids, assignee_contacts):
        notify_dept_users_single(target_user_id=uid, action_type="WF_SUBMITTED",
                                  doc_id=instance_id, subject=f"Resubmission {new_submission_no}", actor_id=user_id)
        w = _wf_send_notification_email(
            recipient_email=assignee_email, recipient_name=assignee_name,
            subject=f"Document Resubmitted for Your Review — {wf_subject}",
            heading=f"{resubmitter_name or 'The sender'} resubmitted a document that requires your review.",
            body_lines=[("Document", wf_subject), ("Sender", resubmitter_name or ""),
                        ("Date", datetime.now().strftime("%Y-%m-%d"))],
            instance_id=instance_id, button_label="Review Document",
        )
        email_warning = email_warning or w

    audit_log("WF_RESUBMITTED", page_id=WF_PAGE_ID,
              notes=f"Instance {instance_id} resubmitted as submission {new_submission_no}")

    resp = {"success": True, "instance_id": instance_id, "submission_number": new_submission_no}
    if email_warning:
        resp["warning"] = email_warning
    return jsonify(resp)


# ── API: Workflow History filter options — senders/assignees/approvers,
#         scoped to exactly the same "my history" rule as api_workflow_history
#         (submitted by me, ever assigned to me, or ever acted on by me;
#         admins see everyone) so the dropdowns only ever list people who
#         actually appear in what this user can see — never the whole
#         directory ───────────────────────────────────────────────────────
@app.route("/api/workflow/history/filter-options", methods=["GET"])
@login_required
def api_workflow_history_filter_options():
    if not _check_accr(WF_PAGE_ID, "Can_Approve"):
        return jsonify({"error": "Access denied: you do not have workflow permission."}), 403

    user_id = session["user_id"]
    is_admin = get_current_role() == "admin"

    scope_sql = "" if is_admin else """ AND (
        wi.SubmittedBy = ?
        OR EXISTS (
            SELECT 1 FROM dbo.WF_Instance_Assignments wa3
            JOIN dbo.WF_Instance_Steps wis3 ON wis3.InstanceStepID = wa3.InstanceStepID
            WHERE wis3.InstanceID = wi.InstanceID AND wa3.AssignedTo = ?
        )
        OR EXISTS (
            SELECT 1 FROM dbo.WF_History wh3
            WHERE wh3.InstanceID = wi.InstanceID AND wh3.ActionBy = ?
        )
    )"""
    scope_params = [] if is_admin else [user_id, user_id, user_id]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Senders: whoever submitted a document within this user's scope.
        cursor.execute(
            f"""
            SELECT DISTINCT su.USER_ID, su.USER_FULLNAME, su.USER_NAME
            FROM dbo.WF_Instances wi
            JOIN dbo.Sys_User su ON su.USER_ID = wi.SubmittedBy
            WHERE wi.IsDeleted = 0 AND wi.Status <> 'Draft'{scope_sql}
            ORDER BY su.USER_FULLNAME
            """,
            *scope_params,
        )
        senders = [{"user_id": r[0], "full_name": r[1] or r[2] or ""} for r in cursor.fetchall()]

        # Assignees: anyone ever assigned a step on an in-scope document.
        cursor.execute(
            f"""
            SELECT DISTINCT su.USER_ID, su.USER_FULLNAME, su.USER_NAME
            FROM dbo.WF_Instances wi
            JOIN dbo.WF_Instance_Steps wis ON wis.InstanceID = wi.InstanceID
            JOIN dbo.WF_Instance_Assignments wa ON wa.InstanceStepID = wis.InstanceStepID
            JOIN dbo.Sys_User su ON su.USER_ID = wa.AssignedTo
            WHERE wi.IsDeleted = 0 AND wi.Status <> 'Draft'{scope_sql}
            ORDER BY su.USER_FULLNAME
            """,
            *scope_params,
        )
        assignees = [{"user_id": r[0], "full_name": r[1] or r[2] or ""} for r in cursor.fetchall()]

        # Approvers: anyone who has ever taken an APPROVED action on an
        # in-scope document.
        cursor.execute(
            f"""
            SELECT DISTINCT su.USER_ID, su.USER_FULLNAME, su.USER_NAME
            FROM dbo.WF_Instances wi
            JOIN dbo.WF_History wh ON wh.InstanceID = wi.InstanceID AND wh.ActionType = 'APPROVED'
            JOIN dbo.Sys_User su ON su.USER_ID = wh.ActionBy
            WHERE wi.IsDeleted = 0 AND wi.Status <> 'Draft'{scope_sql}
            ORDER BY su.USER_FULLNAME
            """,
            *scope_params,
        )
        approvers = [{"user_id": r[0], "full_name": r[1] or r[2] or ""} for r in cursor.fetchall()]

        return jsonify({"senders": senders, "assignees": assignees, "approvers": approvers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Workflow History — every instance the user is allowed to see,
#         with the current status/sender/assignee, filterable by status,
#         sender, assignee, and a submission-date range ────────────────────
@app.route("/api/workflow/history", methods=["GET"])
@login_required
def api_workflow_history():
    if not _check_accr(WF_PAGE_ID, "Can_Approve"):
        return jsonify({"error": "Access denied: you do not have workflow permission."}), 403

    status = (request.args.get("status") or "").strip() or None
    sender_id = (request.args.get("sender_id") or "").strip() or None
    assignee_id = (request.args.get("assignee_id") or "").strip() or None
    dept_id = (request.args.get("dept_id") or "").strip() or None
    approver_id = (request.args.get("approver_id") or "").strip() or None
    date_from = (request.args.get("date_from") or "").strip() or None
    date_to = (request.args.get("date_to") or "").strip() or None
    search = (request.args.get("search") or "").strip() or None

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        query = """
            SELECT wi.InstanceID, wi.Subject, wi.Status, wi.SubmissionNumber,
                   wi.SubmittedBy, su.USER_FULLNAME, su.USER_NAME, wi.SubmittedOn,
                   wi.CurrentStepOrder, wi.ViewedOn
            FROM dbo.WF_Instances wi
            LEFT JOIN dbo.Sys_User su ON su.USER_ID = wi.SubmittedBy
            LEFT JOIN dbo.Adco_Transactions t ON t.ID = wi.Transaction_ID
            WHERE wi.IsDeleted = 0 AND wi.Status <> 'Draft'
        """
        params = []

        # Scope to "my" history: submitted by me, ever assigned to me, or
        # ever acted on by me — never someone else's unrelated documents.
        # Admins are the only exception and can still see every instance
        # (same admin-sees-all pattern used elsewhere in the app).
        if get_current_role() != "admin":
            query += """ AND (
                wi.SubmittedBy = ?
                OR EXISTS (
                    SELECT 1 FROM dbo.WF_Instance_Assignments wa3
                    JOIN dbo.WF_Instance_Steps wis3 ON wis3.InstanceStepID = wa3.InstanceStepID
                    WHERE wis3.InstanceID = wi.InstanceID AND wa3.AssignedTo = ?
                )
                OR EXISTS (
                    SELECT 1 FROM dbo.WF_History wh3
                    WHERE wh3.InstanceID = wi.InstanceID AND wh3.ActionBy = ?
                )
            )"""
            params.extend([session["user_id"], session["user_id"], session["user_id"]])

        if status:
            if status == "Pending":
                # "Pending" in the History filter means "sent for approval
                # and still awaiting action" — it covers every status a
                # document carries between submission and a final
                # Approved/Rejected/Forwarded outcome, not a single exact
                # status string (mirrors the IN-list used by the
                # approve/reject/forward endpoints elsewhere).
                query += " AND wi.Status IN ('Pending', 'Pending Approval', 'Viewed', 'In Progress')"
            else:
                query += " AND wi.Status = ?"
                params.append(status)
        if sender_id:
            query += " AND wi.SubmittedBy = ?"
            params.append(int(sender_id))
        if date_from:
            query += " AND wi.SubmittedOn >= ?"
            params.append(date_from)
        if date_to:
            query += " AND wi.SubmittedOn < DATEADD(DAY, 1, ?)"
            params.append(date_to)
        if assignee_id:
            # "Assignee" filter matches anyone who has ever been assigned
            # this document (current or past step) — not just the one
            # currently holding it — so a history search for a name still
            # finds documents that have since moved on from them.
            query += """ AND EXISTS (
                SELECT 1 FROM dbo.WF_Instance_Assignments wa2
                JOIN dbo.WF_Instance_Steps wis2 ON wis2.InstanceStepID = wa2.InstanceStepID
                WHERE wis2.InstanceID = wi.InstanceID AND wa2.AssignedTo = ?
            )"""
            params.append(int(assignee_id))
        if approver_id:
            # "Approver" filter matches anyone who has ever taken an
            # APPROVED action on this document, at any step/submission.
            query += """ AND EXISTS (
                SELECT 1 FROM dbo.WF_History wh2
                WHERE wh2.InstanceID = wi.InstanceID AND wh2.ActionBy = ?
                  AND wh2.ActionType = 'APPROVED'
            )"""
            params.append(int(approver_id))
        if dept_id:
            dcol = adco_folder_dept_col(cursor)
            dept_bracket = f"[{dcol}]" if dcol != "ID" else "ID"
            query += f""" AND t.Foldes_ID IN (
                SELECT ID FROM dbo.Adco_Folder WHERE {dept_bracket} = ? AND IsDeleted = 0
            )"""
            params.append(int(dept_id))
        if search:
            query += """ AND (
                wi.Subject LIKE ? OR su.USER_FULLNAME LIKE ? OR su.USER_NAME LIKE ?
            )"""
            like = f"%{search}%"
            params.extend([like, like, like])

        query += " ORDER BY wi.SubmittedOn DESC"
        cursor.execute(query, params)
        rows = cursor.fetchall()

        instances = []
        for r in rows:
            (instance_id, subject, status_val, submission_no,
             sender_id_val, sender_fullname, sender_username, submitted_on, current_step_order, viewed_on) = r

            current_assignees = []
            if current_step_order is not None:
                cursor.execute(
                    """
                    SELECT su2.USER_FULLNAME, su2.USER_NAME
                    FROM dbo.WF_Instance_Assignments wa
                    JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
                    JOIN dbo.Sys_User su2 ON su2.USER_ID = wa.AssignedTo
                    WHERE wis.InstanceID = ? AND wis.SubmissionNumber = ?
                      AND wis.StepOrder = ?
                    """,
                    instance_id, submission_no, current_step_order,
                )
                current_assignees = [(row[0] or row[1] or "") for row in cursor.fetchall()]

            instances.append({
                "instance_id": instance_id,
                "subject": subject,
                "status": status_val,
                "submission_number": submission_no,
                "sender_id": sender_id_val,
                "sender_name": sender_fullname or sender_username or "",
                "submitted_on": submitted_on.isoformat() if submitted_on else None,
                "viewed_on": viewed_on.isoformat() if viewed_on else None,
                "current_assignees": current_assignees,
            })

        return jsonify({"success": True, "items": instances})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── API: Full continuous timeline for one instance (all submissions) ──────
@app.route("/api/workflow/instances/<int:instance_id>/timeline", methods=["GET"])
@login_required
def api_workflow_timeline(instance_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # The original submission never gets a WF_History row of its own
        # (only actions taken on it do), so it's synthesized here as the
        # first event to make this a genuinely complete timeline.
        cursor.execute(
            """
            SELECT wi.SubmittedBy, su.USER_FULLNAME, su.USER_NAME, wi.SubmittedOn, wi.Subject
            FROM dbo.WF_Instances wi
            LEFT JOIN dbo.Sys_User su ON su.USER_ID = wi.SubmittedBy
            WHERE wi.InstanceID = ?
            """,
            instance_id,
        )
        inst_row = cursor.fetchone()
        events = []
        if inst_row:
            _, sub_fullname, sub_username, submitted_on, subject = inst_row
            events.append({
                "submission": 1,
                "action": "SUBMITTED",
                "by": sub_fullname or sub_username or "",
                "on": submitted_on.isoformat() if submitted_on else None,
                "notes": subject,
            })

        cursor.execute(
            """
            SELECT wh.SubmissionNumber, wh.ActionType, u.USER_FULLNAME, u.USER_NAME, wh.ActionOn, wh.Notes
            FROM dbo.WF_History wh
            JOIN dbo.Sys_User u ON u.USER_ID = wh.ActionBy
            WHERE wh.InstanceID = ?
            ORDER BY wh.ActionOn ASC
            """,
            instance_id,
        )
        events += [{"submission": r[0], "action": r[1], "by": (r[2] or r[3] or ""),
                    "on": r[4].isoformat() if r[4] else None, "notes": r[5]}
                   for r in cursor.fetchall()]

        return jsonify({"success": True, "events": events})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


# ── WORKFLOW COMMENTS & @MENTIONS (Task 14) ─────────────────────────────────
# Reuses the pre-existing dbo.WF_Comments table (already scaffolded for
# rejection/resubmission notes) as a general-purpose comment thread per
# instance. No new table/columns: an @-mention is encoded inline inside
# CommentText as a small token — @[Full Name](user_id) — written by the
# composer when someone is picked from the autocomplete list. The token is
# parsed back out both to notify the mentioned user(s) and to render a
# highlighted "chip" in the UI; it's never shown to the user in raw form.
_WF_MENTION_RE = re.compile(r"@\[([^\[\]]{1,100})\]\((\d+)\)")


def _wf_parse_mentions(text):
    """Returns the sorted, de-duplicated list of user_ids @-mentioned in text."""
    return sorted({int(uid) for _name, uid in _WF_MENTION_RE.findall(text or "")})


def _wf_comment_plain_text(text, limit=150):
    """Strips mention tokens down to plain '@Name' for notification subjects."""
    plain = _WF_MENTION_RE.sub(lambda m: f"@{m.group(1)}", text or "")
    plain = plain.strip()
    if len(plain) > limit:
        plain = plain[:limit] + "…"
    return plain


@app.route("/api/workflow/instances/<int:instance_id>/comments", methods=["GET"])
@login_required
def api_workflow_list_comments(instance_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT wc.CommentID, wc.CommentText, wc.CreatedOn, wc.CommentBy,
                   u.USER_FULLNAME, u.USER_NAME
            FROM dbo.WF_Comments wc
            JOIN dbo.Sys_User u ON u.USER_ID = wc.CommentBy
            WHERE wc.InstanceID = ? AND wc.IsDeleted = 0
            ORDER BY wc.CreatedOn ASC
            """,
            instance_id,
        )
        comments = [{
            "id": r[0],
            "text": r[1],
            "mentions": _wf_parse_mentions(r[1]),
            "on": r[2].isoformat() if r[2] else None,
            "by_id": r[3],
            "by_name": r[4] or r[5] or "",
        } for r in cursor.fetchall()]
        return jsonify({"success": True, "comments": comments})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


@app.route("/api/workflow/instances/<int:instance_id>/comments", methods=["POST"])
@login_required
def api_workflow_add_comment(instance_id):
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Comment text is required."}), 400
    if len(text) > 4000:
        return jsonify({"error": "Comment is too long (4000 characters max)."}), 400

    user_id = session["user_id"]
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT InstanceID, Subject, SubmittedBy FROM dbo.WF_Instances WHERE InstanceID = ? AND IsDeleted = 0",
            instance_id,
        )
        row = cursor.fetchone()
        if not row:
            return jsonify({"error": "Workflow instance not found."}), 404
        _, wf_subject, submitted_by = row
        wf_subject = wf_subject or f"Document #{instance_id}"

        # Mentioned users must actually be real, active accounts — silently
        # drop anything else rather than erroring the whole comment out.
        mention_ids = _wf_parse_mentions(text)
        valid_mention_ids = []
        if mention_ids:
            placeholders = ",".join("?" * len(mention_ids))
            cursor.execute(
                f"SELECT USER_ID FROM dbo.Sys_User WHERE USER_ID IN ({placeholders}) AND IsDeleted = 0",
                *mention_ids,
            )
            valid_mention_ids = [r[0] for r in cursor.fetchall()]

        cursor.execute(
            """INSERT INTO dbo.WF_Comments (InstanceID, InstanceStepID, CommentBy, CommentText, CreatedOn, IsDeleted)
               OUTPUT INSERTED.CommentID, INSERTED.CreatedOn
               VALUES (?, NULL, ?, ?, GETDATE(), 0)""",
            instance_id, user_id, text,
        )
        new_id, created_on = cursor.fetchone()

        # Everyone else who has a stake in this document — the sender, every
        # user ever assigned a step, and everyone who has commented before —
        # gets a lightweight "comment added" ping. Anyone explicitly
        # @-mentioned gets the stronger "you were mentioned" notification
        # instead, not both.
        cursor.execute(
            """
            SELECT DISTINCT wa.AssignedTo
            FROM dbo.WF_Instance_Assignments wa
            JOIN dbo.WF_Instance_Steps wis ON wis.InstanceStepID = wa.InstanceStepID
            WHERE wis.InstanceID = ?
            """,
            instance_id,
        )
        stakeholder_ids = {r[0] for r in cursor.fetchall()}
        cursor.execute(
            "SELECT DISTINCT CommentBy FROM dbo.WF_Comments WHERE InstanceID = ? AND IsDeleted = 0",
            instance_id,
        )
        stakeholder_ids |= {r[0] for r in cursor.fetchall()}
        if submitted_by:
            stakeholder_ids.add(submitted_by)
        stakeholder_ids.discard(user_id)
        stakeholder_ids -= set(valid_mention_ids)

        conn.commit()

        _, actor_name = _get_user_email(cursor, user_id)
        plain_text = _wf_comment_plain_text(text)
        stakeholder_contacts = [(uid, _get_user_email(cursor, uid)) for uid in stakeholder_ids]
        mention_contacts = [(uid, _get_user_email(cursor, uid)) for uid in valid_mention_ids]

    except Exception as exc:
        if conn:
            conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Failed to post comment: {exc}"}), 500
    finally:
        if conn:
            conn.close()

    for uid, (email, name) in mention_contacts:
        notify_dept_users_single(target_user_id=uid, action_type="WF_MENTIONED",
                                  doc_id=instance_id, subject=plain_text,
                                  actor_id=user_id)
        _wf_send_notification_email(
            recipient_email=email, recipient_name=name,
            subject=f"You were mentioned — {wf_subject}",
            heading=f"{actor_name or 'Someone'} mentioned you in a comment on \"{wf_subject}\".",
            body_lines=[("Comment", plain_text)],
            instance_id=instance_id, button_label="View Comment",
        )

    for uid, (email, name) in stakeholder_contacts:
        notify_dept_users_single(target_user_id=uid, action_type="WF_COMMENT_ADDED",
                                  doc_id=instance_id, subject=plain_text,
                                  actor_id=user_id)
        _wf_send_notification_email(
            recipient_email=email, recipient_name=name,
            subject=f"New Comment — {wf_subject}",
            heading=f"{actor_name or 'Someone'} commented on \"{wf_subject}\".",
            body_lines=[("Comment", plain_text)],
            instance_id=instance_id, button_label="View Comment",
        )

    audit_log("WF_COMMENT_ADDED", page_id=WF_PAGE_ID,
              notes=f"Instance {instance_id}: comment added ({len(valid_mention_ids)} mention(s))")

    return jsonify({
        "success": True,
        "comment": {
            "id": new_id,
            "text": text,
            "mentions": valid_mention_ids,
            "on": created_on.isoformat() if created_on else None,
            "by_id": user_id,
            "by_name": actor_name or "",
        },
    })


# ── PAGE ACCESS RIGHTS (Sys_AccR) ────────────────────────────────────────────
# PAGE_IDs: 1=Inquiries, 2=Archive, 3=Folder Browser, 4=Workflow, 5=Messages
# Columns: Can_Open, Can_Edit, Can_Del, Can_Print, Can_Add, Can_Prew, Can_QR, Can_Approve
# Convention: 0 = allowed, 1 = denied

def _ensure_accr_table():
    """
    Create Sys_AccR (if missing), add constraints, and seed a row for every
    active user x every page (1=Inquiries, 2=Archive, 3=Folder Browser,
    4=Workflow, 5=Messages).
    Uses _get_ddl_connection() — autocommit=True at connect time — which is
    the only reliable way to run DDL through pyodbc on SQL Server.
    """
    conn = _get_ddl_connection()
    try:
        cur = conn.cursor()

        # 1. Detect the real data type of Sys_User.USER_ID so our FK matches exactly
        cur.execute("""
            SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_User' AND COLUMN_NAME='USER_ID'
        """)
        row = cur.fetchone()
        if row:
            data_type = row[0].upper()
            char_len  = row[1]
            if data_type in ('NVARCHAR', 'VARCHAR', 'CHAR', 'NCHAR'):
                length = char_len if char_len and char_len > 0 else 50
                uid_col_def = f"{data_type}({length})"
            elif data_type == 'BIGINT':
                uid_col_def = "BIGINT"
            elif data_type in ('SMALLINT', 'TINYINT'):
                uid_col_def = data_type
            else:
                uid_col_def = "INT"
        else:
            uid_col_def = "INT"

        # 2. Create table using the matched USER_ID type
        cur.execute(f"""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AccR'
            )
            BEGIN
                CREATE TABLE dbo.Sys_AccR (
                    ID            INT IDENTITY(1,1) PRIMARY KEY,
                    USER_ID       {uid_col_def}    NOT NULL,
                    USER_FULLNAME NVARCHAR(150) NOT NULL DEFAULT '',
                    USER_NAME     NVARCHAR(150) NOT NULL DEFAULT '',
                    PAGE_ID       INT           NOT NULL,
                    Can_Open      TINYINT       NOT NULL DEFAULT 0,
                    Can_Edit      TINYINT       NULL,
                    Can_Del       TINYINT       NULL,
                    Can_Print     TINYINT       NULL,
                    Can_Add       TINYINT       NULL,
                    Can_Prew      TINYINT       NULL
                )
            END
        """)

        # 3. Unique constraint
        cur.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM sys.key_constraints
                WHERE name='UQ_Sys_AccR_User_Page'
                  AND parent_object_id=OBJECT_ID('dbo.Sys_AccR')
            )
            BEGIN
                ALTER TABLE dbo.Sys_AccR
                    ADD CONSTRAINT UQ_Sys_AccR_User_Page UNIQUE (USER_ID, PAGE_ID)
            END
        """)

        # 4. FK skipped — USER_ID is logically linked to Sys_User.USER_ID

        # 5. Add Can_Prew column if missing, then alter existing columns to allow NULL
        cur.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AccR' AND COLUMN_NAME='Can_Prew'
            )
            BEGIN
                ALTER TABLE dbo.Sys_AccR ADD Can_Prew TINYINT NULL
            END
        """)
        # 5b. Add Can_QR column if missing (QR-code generation right, Inquiries only)
        cur.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AccR' AND COLUMN_NAME='Can_QR'
            )
            BEGIN
                ALTER TABLE dbo.Sys_AccR ADD Can_QR TINYINT NULL
            END
        """)
        # 5c. Add Can_Approve column if missing (Workflow page, PAGE_ID=4)
        cur.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AccR' AND COLUMN_NAME='Can_Approve'
            )
            BEGIN
                ALTER TABLE dbo.Sys_AccR ADD Can_Approve TINYINT NULL
            END
        """)
        for col in ('Can_Edit', 'Can_Del', 'Can_Print', 'Can_Add', 'Can_Prew', 'Can_QR', 'Can_Approve'):
            cur.execute(f"""
                IF EXISTS (
                    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='Sys_AccR'
                      AND COLUMN_NAME='{col}' AND IS_NULLABLE='NO'
                )
                BEGIN
                    ALTER TABLE dbo.Sys_AccR ALTER COLUMN [{col}] TINYINT NULL
                END
            """)

        # 6. Set non-applicable fields to NULL for existing rows
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Add=NULL
            WHERE PAGE_ID IN (1,3,5) AND Can_Add IS NOT NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Add=0
            WHERE PAGE_ID IN (2,4) AND Can_Add IS NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Edit=NULL, Can_Del=NULL, Can_Print=NULL
            WHERE PAGE_ID IN (2,3,4,5) AND (Can_Edit IS NOT NULL OR Can_Del IS NOT NULL OR Can_Print IS NOT NULL)
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Prew=NULL
            WHERE PAGE_ID IN (3,4,5) AND Can_Prew IS NOT NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Prew=0
            WHERE PAGE_ID IN (1,2) AND Can_Prew IS NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_QR=NULL
            WHERE PAGE_ID IN (2,3,4,5) AND Can_QR IS NOT NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_QR=1
            WHERE PAGE_ID = 1 AND Can_QR IS NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Approve=NULL
            WHERE PAGE_ID IN (1,2,3,5) AND Can_Approve IS NOT NULL
        """)
        cur.execute("""
            UPDATE dbo.Sys_AccR SET Can_Approve=0
            WHERE PAGE_ID = 4 AND Can_Approve IS NULL
        """)

        # 5. Seed missing rows: every active user x pages 1,2,3,4,5
        #    Only applicable fields get 0 — non-applicable stay NULL
        #    Page 1 (Inquiries):      Can_Open, Can_Edit, Can_Del, Can_Print, Can_Prew, Can_QR
        #    Page 2 (Archive):        Can_Open, Can_Add, Can_Prew
        #    Page 3 (Folder Browser): Can_Open
        #    Page 4 (Workflow):       Can_Open, Can_Add, Can_Approve
        #    Page 5 (Messages):       Can_Open
        cur.execute("""
            INSERT INTO dbo.Sys_AccR
                (USER_ID, USER_FULLNAME, USER_NAME, PAGE_ID,
                 Can_Open, Can_Edit, Can_Del, Can_Print, Can_Add, Can_Prew, Can_QR, Can_Approve)
            SELECT u.USER_ID,
                   ISNULL(u.USER_FULLNAME, ''),
                   ISNULL(u.USER_NAME, ''),
                   p.PAGE_ID,
                   0,
                   CASE p.PAGE_ID WHEN 1 THEN 0 ELSE NULL END,
                   CASE p.PAGE_ID WHEN 1 THEN 0 ELSE NULL END,
                   CASE p.PAGE_ID WHEN 1 THEN 0 ELSE NULL END,
                   CASE p.PAGE_ID WHEN 2 THEN 0 WHEN 4 THEN 0 ELSE NULL END,
                   CASE p.PAGE_ID WHEN 1 THEN 0 WHEN 2 THEN 0 ELSE NULL END,
                   CASE p.PAGE_ID WHEN 1 THEN 1 ELSE NULL END,
                   CASE p.PAGE_ID WHEN 4 THEN 0 ELSE NULL END
            FROM dbo.Sys_User u
            CROSS JOIN (SELECT 1 AS PAGE_ID
                        UNION ALL SELECT 2
                        UNION ALL SELECT 3
                        UNION ALL SELECT 4
                        UNION ALL SELECT 5) p
            WHERE u.IsDeleted = 0
              AND NOT EXISTS (
                  SELECT 1 FROM dbo.Sys_AccR a
                  WHERE a.USER_ID = u.USER_ID AND a.PAGE_ID = p.PAGE_ID
              )
        """)
        print("[AccR migration] Sys_AccR ready.")
    except Exception as exc:
        print(f"[AccR migration] ERROR: {exc}")
    finally:
        conn.close()


@app.route("/api/admin/users/<int:user_id>/accr", methods=["GET"])
@login_required
def api_admin_get_accr(user_id):
    """Return page-access rows for a user. Creates table if missing."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403
    conn = None
    try:
        _ensure_accr_table()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT PAGE_ID, Can_Open, Can_Edit, Can_Del, Can_Print, Can_Add, Can_Prew, Can_QR, Can_Approve "
            "FROM dbo.Sys_AccR WHERE USER_ID = ?", user_id
        )
        rows = cursor.fetchall()
        return jsonify([{
            "page_id":     r[0],
            "can_open":    r[1],
            "can_edit":    r[2],
            "can_del":     r[3],
            "can_print":   r[4],
            "can_add":     r[5],
            "can_prew":    r[6],
            "can_qr":      r[7],
            "can_approve": r[8],
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route("/api/admin/users/<int:user_id>/accr", methods=["POST"])
@login_required
def api_admin_set_accr(user_id):
    """
    Upsert one page-access row.
    Body: { "page_id": 1, "field": "can_open", "value": 0 }
    value 0 = allowed, 1 = denied.
    """
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden"}), 403

    ALLOWED_FIELDS = {"can_open", "can_edit", "can_del", "can_print", "can_add", "can_prew", "can_qr", "can_approve"}
    COL_MAP = {
        "can_open":    "Can_Open",
        "can_edit":    "Can_Edit",
        "can_del":     "Can_Del",
        "can_print":   "Can_Print",
        "can_add":     "Can_Add",
        "can_prew":    "Can_Prew",
        "can_qr":      "Can_QR",
        "can_approve": "Can_Approve",
    }
    # Which fields are applicable per page (others are NULL and must not be updated)
    PAGE_FIELDS = {
        1: {"can_open", "can_edit", "can_del", "can_print", "can_prew", "can_qr"},
        2: {"can_open", "can_add", "can_prew"},
        3: {"can_open"},
        4: {"can_open", "can_add", "can_approve"},
        5: {"can_open"},
    }

    data = request.get_json(silent=True) or {}
    page_id = data.get("page_id")
    field   = (data.get("field") or "").lower()
    value   = data.get("value")

    if page_id not in (1, 2, 3, 4, 5):
        return jsonify({"error": "Invalid page_id"}), 400
    if field not in ALLOWED_FIELDS:
        return jsonify({"error": "Invalid field"}), 400
    if field not in PAGE_FIELDS.get(page_id, set()):
        return jsonify({"error": f"Field '{field}' not applicable for page {page_id}"}), 400
    if value not in (0, 1):
        return jsonify({"error": "Value must be 0 or 1"}), 400

    col = COL_MAP[field]
    conn = None
    try:
        _ensure_accr_table()
        conn = get_db_connection()
        cursor = conn.cursor()
        # UPDATE the specific field — row is guaranteed after _ensure_accr_table seeds it
        cursor.execute(
            f"UPDATE dbo.Sys_AccR SET [{col}] = ? WHERE USER_ID = ? AND PAGE_ID = ?",
            value, user_id, page_id
        )
        conn.commit()
        action_label = "allowed" if value == 0 else "denied"
        audit_log("ACCR_CHANGE", page_id=page_id,
                  notes=f"Set {field}={action_label} for user ID {user_id} on page {page_id}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


def _check_accr(page_id, field):
    """
    Returns True if the current user is allowed (value=0 or row missing).
    Returns False if denied (value=1), OR if the permission check itself
    fails for any reason (DB error, etc.) — fails closed/safe, never open.
    """
    user_id = session.get("user_id")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT [{field}] FROM dbo.Sys_AccR WHERE USER_ID=? AND PAGE_ID=?",
            user_id, page_id
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return True   # no row or NULL = allowed
        return row[0] == 0  # 0 = allowed, 1 = denied
    except Exception as exc:

        print(f"[_check_accr] permission check failed (page_id={page_id}, field={field}, "
              f"user_id={session.get('user_id')}): {exc}")
        return False  # fail closed
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Current user's own access rights ─────────────────────────────────────────
@app.route("/api/my/accr")
@login_required
def api_my_accr():
    """Return the logged-in user's Sys_AccR rows (all pages)."""
    user_id = session.get("user_id")
    conn = None
    try:
        _ensure_accr_table()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT PAGE_ID, Can_Open, Can_Edit, Can_Del, Can_Print, Can_Add, Can_Prew, Can_QR, Can_Approve "
            "FROM dbo.Sys_AccR WHERE USER_ID = ?", user_id
        )
        rows = cursor.fetchall()
        return jsonify([{
            "page_id":     r[0],
            "can_open":    r[1],
            "can_edit":    r[2],
            "can_del":     r[3],
            "can_print":   r[4],
            "can_add":     r[5],
            "can_prew":    r[6],
            "can_qr":      r[7],
            "can_approve": r[8],
        } for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()

# ── Current user's own department access (always fresh from DB) ──────────────
@app.route("/api/my/deps")
@login_required
def api_my_deps():
    """
    Return the current user's allowed Sys_Department IDs, read fresh from DB.
    The JS calls this after any folder-tree interaction to ensure the
    in-page permission set is current (no page reload required after admin
    grants or revokes folder access).
    Returns { "admin": true } for admins (unrestricted), or
    { "admin": false, "dep_ids": [46, 53, ...] } for regular users.
    """
    allowed = get_allowed_dep_ids()
    if allowed is None:
        return jsonify({"admin": True, "dep_ids": []})
    # Also keep session in sync
    session["allowed_dep_ids"] = allowed
    return jsonify({"admin": False, "dep_ids": allowed})


# ── GLOBAL FIELD LABELS (admin-only write, all users read) ──────────────────
_LABELS_PATH = os.path.join(app.root_path, "data", "field_labels.json")

def _load_labels_file() -> dict:
    """Return saved labels dict, or {} if file doesn't exist yet."""
    try:
        if os.path.isfile(_LABELS_PATH):
            with open(_LABELS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_labels_file(labels: dict) -> None:
    """Persist labels to disk, creating the data/ directory if needed."""
    os.makedirs(os.path.dirname(_LABELS_PATH), exist_ok=True)
    with open(_LABELS_PATH, "w", encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False, indent=2)

@app.route("/api/labels", methods=["GET"])
@login_required
def api_labels_get():
    """Return the current global field labels (readable by all users)."""
    return jsonify(_load_labels_file())

@app.route("/api/labels", methods=["POST"])
@login_required
def api_labels_set():
    """Save global field labels — admin only."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only administrators can change field labels."}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid payload — expected a JSON object."}), 400
    _save_labels_file(data)
    return jsonify({"success": True})


# ── SUPPORT CONTACTS (User Guide info pane — admin-only write, all users read) ──
_CONTACTS_PATH = os.path.join(app.root_path, "data", "support_contacts.json")

_DEFAULT_CONTACTS = {
    "primary":   {"name": "", "role": "", "email": "", "number": ""},
    "secondary": {"name": "", "role": "", "email": "", "number": ""},
}

def _load_contacts_file() -> dict:
    """Return saved support contacts dict, falling back to blank defaults."""
    result = json.loads(json.dumps(_DEFAULT_CONTACTS))
    try:
        if os.path.isfile(_CONTACTS_PATH):
            with open(_CONTACTS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in ("primary", "secondary"):
                    if isinstance(data.get(key), dict):
                        for field in ("name", "role", "email", "number"):
                            if field in data[key]:
                                result[key][field] = data[key][field]
    except Exception:
        pass
    return result

def _save_contacts_file(contacts: dict) -> None:
    """Persist support contacts to disk, creating the data/ directory if needed."""
    cleaned = json.loads(json.dumps(_DEFAULT_CONTACTS))
    for key in ("primary", "secondary"):
        src = contacts.get(key) if isinstance(contacts, dict) else None
        if isinstance(src, dict):
            for field in ("name", "role", "email", "number"):
                cleaned[key][field] = str(src.get(field, "") or "").strip()
    os.makedirs(os.path.dirname(_CONTACTS_PATH), exist_ok=True)
    with open(_CONTACTS_PATH, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

@app.route("/api/guide-contacts", methods=["GET"])
@login_required
def api_guide_contacts_get():
    """Return the User Guide support contact info (readable by all logged-in users)."""
    return jsonify(_load_contacts_file())

@app.route("/api/guide-contacts", methods=["POST"])
@login_required
def api_guide_contacts_set():
    """Save the User Guide support contact info — admin only."""
    if get_current_role() != "admin":
        return jsonify({"error": "Forbidden: only administrators can edit support contacts."}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid payload — expected a JSON object."}), 400
    _save_contacts_file(data)
    return jsonify({"success": True, "contacts": _load_contacts_file()})


@app.route("/logout")
def logout():
    audit_log("LOGOUT", page_id=None, notes="User logged out")
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill-ocr":
        parser = argparse.ArgumentParser(prog="python app.py backfill-ocr")
        parser.add_argument("--limit", type=int, default=None, help="Max attachments to process this run")
        parser.add_argument("--workers", type=int, default=1, help="Parallel extraction workers (default 1)")
        args = parser.parse_args(sys.argv[2:])
        run_ocr_backfill(limit=args.limit, workers=args.workers)
        sys.exit(0)

    if OCR_ENABLED and USE_EASYOCR:
        print("Preloading EasyOCR model (this may take a while on first run)...")
        _get_easyocr_reader()

    # Ensure Fe1–Fe7 columns exist in both tables before taking traffic
    _run_fe_startup_migration()

    # Ensure audit log table exists
    _ensure_audit_table()

    # Ensure Workflow (WF_*) tables exist
    try:
        _ensure_workflow_tables()
        _ensure_workflow_adhoc_columns()
    except Exception as exc:
        print(f"[Workflow migration] ERROR during startup: {exc}")

    print("Server running on http://localhost:5002")

    # Serve through Waitress either way — a real production WSGI server,
    # no dev-server warning. Flask-SocketIO (async_mode="threading") attaches
    # itself as WSGI middleware on `app`, so Waitress transparently serves
    # /socket.io/* too. The only thing this gives up vs. eventlet/gevent is
    # the WebSocket upgrade itself: Socket.IO falls back to (long-)polling
    # transport automatically, which is plain HTTP and works fine under any
    # WSGI server. Messages/notifications still arrive near-instantly —
    # they just ride on polling instead of a persistent WS connection.
    from waitress import serve
    if socketio is None:
        print("[socketio] flask-socketio not installed — real-time push "
              "disabled, falling back to the old HTTP polling intervals. "
              "Run: pip install flask-socketio")
    serve(app, host="0.0.0.0", port=5002, threads=16)