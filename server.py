#!/usr/bin/env python3
"""Doomsday API and installable mobile web app server.

Python 3.12+, SQLite (FTS5 + R*Tree), and no runtime framework dependency.
Run behind an HTTPS reverse proxy in production. Secrets are read from the
environment or a local .env file and never sent to the browser.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import io
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("DOOMSDAY_DB_PATH", ROOT / "data" / "doomsday.sqlite3"))
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
MAX_BODY = 18 * 1024 * 1024
MAX_IMAGE = 1024 * 1024
MAX_DOCUMENT = 8 * 1024 * 1024
MAX_IMAGES = 5
APP_NAME = "Doomsday"

STATUSES = {
    "OPEN", "OFFER_RECEIVED", "ASSIGNED", "TASKER_ON_THE_WAY", "IN_PROGRESS",
    "COMPLETED_BY_TASKER", "CONFIRMED_BY_CUSTOMER", "PAYMENT_RELEASED",
    "CANCELLED", "DISPUTED",
}
ACTIVE_TASK_STATUSES = ("OPEN", "OFFER_RECEIVED", "ASSIGNED", "TASKER_ON_THE_WAY", "IN_PROGRESS", "COMPLETED_BY_TASKER", "CONFIRMED_BY_CUSTOMER", "DISPUTED")
PROHIBITED_TERMS = (
    "weapon", "explosive", "drugs", "narcotic", "illegal substance", "escort",
    "stolen", "counterfeit", "hack account", "buy votes", "surveillance device",
)
CATEGORY_SEEDS = [
    ("delivery-pickup", "Delivery & Pickup", "டெலிவரி மற்றும் எடுத்தல்", "📦"),
    ("cleaning", "Cleaning", "சுத்தம் செய்தல்", "🧹"),
    ("moving-lifting", "Moving & Lifting", "பொருட்கள் மாற்றுதல்", "📦"),
    ("home-help", "Home Help", "வீட்டு உதவி", "🏠"),
    ("repairs", "Repairs", "பழுதுபார்ப்பு", "🛠️"),
    ("shopping-errands", "Shopping & Errands", "ஷாப்பிங் மற்றும் வெளியிடங்கள்", "🛍️"),
    ("vehicle-help", "Vehicle Help", "வாகன உதவி", "🛵"),
    ("pet-help", "Pet Help", "செல்லப்பிராணி பராமரிப்பு", "🐾"),
    ("elderly-assistance", "Elderly Assistance", "முதியோர் உதவி", "🤝"),
    ("tutoring", "Tutoring", "பயிற்சி", "📚"),
    ("student-study-support", "Student Study Support", "மாணவர் படிப்பு உதவி", "🎓"),
    ("photography", "Photography", "புகைப்படம்", "📷"),
    ("video-editing", "Video Editing", "வீடியோ எடிட்டிங்", "🎬"),
    ("computer-phone-help", "Computer/Phone Help", "கணினி / மொபைல் உதவி", "💻"),
    ("events", "Events", "நிகழ்வுகள்", "🎉"),
    ("gardening", "Gardening", "தோட்ட வேலை", "🌱"),
    ("other", "Other", "மற்றவை", "✨"),
]


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

# Load path and listener overrides after the local .env file is read.
DB_PATH = Path(os.environ.get("DOOMSDAY_DB_PATH", ROOT / "data" / "doomsday.sqlite3"))
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.create_function("distance_km", 4, lambda lat1, lng1, lat2, lng2: haversine_km(lat1, lng1, lat2, lng2), deterministic=True)
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  phone TEXT NOT NULL UNIQUE,
  email TEXT,
  name TEXT NOT NULL DEFAULT '',
  about TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  area TEXT NOT NULL DEFAULT '',
  languages TEXT NOT NULL DEFAULT '["English"]',
  skills TEXT NOT NULL DEFAULT '[]',
  is_student INTEGER NOT NULL DEFAULT 0 CHECK(is_student IN (0,1)),
  service_radius_km INTEGER NOT NULL DEFAULT 10 CHECK(service_radius_km IN (2,5,10,25,50)),
  lat REAL,
  lng REAL,
  photo BLOB,
  photo_mime TEXT,
  suspended INTEGER NOT NULL DEFAULT 0,
  joined_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS users_location_idx ON users(lat,lng);
CREATE TABLE IF NOT EXISTS admin_users (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_payout_profiles (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  razorpay_linked_account_id TEXT UNIQUE,
  verified_by TEXT REFERENCES users(id),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  provider_ref TEXT,
  status TEXT NOT NULL,
  verified_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  name_ta TEXT NOT NULL,
  icon TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  customer_id TEXT NOT NULL REFERENCES users(id),
  category_id INTEGER NOT NULL REFERENCES categories(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  address TEXT NOT NULL,
  landmark TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  lat REAL NOT NULL,
  lng REAL NOT NULL,
  public_lat REAL NOT NULL,
  public_lng REAL NOT NULL,
  time_mode TEXT NOT NULL CHECK(time_mode IN ('ASAP','TODAY','TOMORROW','SCHEDULED')),
  scheduled_at TEXT,
  budget_mode TEXT NOT NULL CHECK(budget_mode IN ('FIXED','NEGOTIABLE')),
  budget_min_paise INTEGER NOT NULL CHECK(budget_min_paise >= 0),
  budget_max_paise INTEGER NOT NULL CHECK(budget_max_paise >= budget_min_paise),
  delivery_mode TEXT NOT NULL DEFAULT 'LOCAL' CHECK(delivery_mode IN ('LOCAL','DIGITAL')),
  integrity_acknowledged INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'OPEN',
  selected_offer_id INTEGER REFERENCES offers(id),
  assigned_tasker_id TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  cancelled_by TEXT REFERENCES users(id),
  cancel_reason TEXT,
  CHECK(lat BETWEEN -90 AND 90), CHECK(lng BETWEEN -180 AND 180)
);
CREATE INDEX IF NOT EXISTS tasks_status_created_idx ON tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_category_status_idx ON tasks(category_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_customer_idx ON tasks(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tasks_assignee_idx ON tasks(assigned_tasker_id, created_at DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS task_locations USING rtree(id, min_lng, max_lng, min_lat, max_lat);
CREATE TRIGGER IF NOT EXISTS task_locations_insert AFTER INSERT ON tasks BEGIN
 INSERT INTO task_locations VALUES (new.id,new.lng,new.lng,new.lat,new.lat);
END;
CREATE TRIGGER IF NOT EXISTS task_locations_update AFTER UPDATE OF lat,lng ON tasks BEGIN
 UPDATE task_locations SET min_lng=new.lng,max_lng=new.lng,min_lat=new.lat,max_lat=new.lat WHERE id=new.id;
END;
CREATE TRIGGER IF NOT EXISTS task_locations_delete AFTER DELETE ON tasks BEGIN
 DELETE FROM task_locations WHERE id=old.id;
END;
CREATE VIRTUAL TABLE IF NOT EXISTS task_search USING fts5(title, description, category, content='');
CREATE TABLE IF NOT EXISTS task_images (
  id TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL REFERENCES users(id),
  mime TEXT NOT NULL,
  data BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_images_task_idx ON task_images(task_id);
CREATE TABLE IF NOT EXISTS task_submissions (
  id TEXT PRIMARY KEY,
  task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
  tasker_id TEXT NOT NULL REFERENCES users(id),
  original_name TEXT NOT NULL,
  original_mime TEXT NOT NULL,
  original_data BLOB NOT NULL,
  preview_name TEXT NOT NULL,
  preview_mime TEXT NOT NULL CHECK(preview_mime='application/pdf'),
  preview_data BLOB NOT NULL,
  submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_submissions_tasker_idx ON task_submissions(tasker_id,submitted_at DESC);
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  tasker_id TEXT NOT NULL REFERENCES users(id),
  amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
  message TEXT NOT NULL,
  eta_minutes INTEGER NOT NULL CHECK(eta_minutes BETWEEN 1 AND 10080),
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','ACCEPTED','DECLINED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(task_id,tasker_id)
);
CREATE INDEX IF NOT EXISTS offers_task_idx ON offers(task_id, status, created_at);
CREATE TABLE IF NOT EXISTS task_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
  offer_id INTEGER NOT NULL UNIQUE REFERENCES offers(id),
  customer_id TEXT NOT NULL REFERENCES users(id),
  tasker_id TEXT NOT NULL REFERENCES users(id),
  agreed_amount_paise INTEGER NOT NULL,
  assigned_at TEXT NOT NULL,
  completed_at TEXT,
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
  customer_id TEXT NOT NULL REFERENCES users(id),
  tasker_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_id TEXT NOT NULL REFERENCES users(id),
  body TEXT NOT NULL DEFAULT '',
  image_mime TEXT,
  image_data BLOB,
  attachment_name TEXT,
  attachment_mime TEXT,
  attachment_data BLOB,
  created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id,id);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  task_id TEXT,
  read_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user_idx ON notifications(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL UNIQUE,
  subscription_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
  customer_id TEXT NOT NULL REFERENCES users(id),
  tasker_id TEXT NOT NULL REFERENCES users(id),
  amount_paise INTEGER NOT NULL,
  fee_paise INTEGER NOT NULL,
  payout_paise INTEGER NOT NULL,
  promo_code TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'INR',
  provider TEXT NOT NULL DEFAULT 'razorpay',
  provider_order_id TEXT UNIQUE,
  provider_payment_id TEXT UNIQUE,
  provider_transfer_id TEXT UNIQUE,
  provider_refund_id TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'order_pending',
  transfer_status TEXT NOT NULL DEFAULT 'not_started',
  created_at TEXT NOT NULL,
  paid_at TEXT,
  released_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_customer_idx ON payments(customer_id,created_at DESC);
CREATE INDEX IF NOT EXISTS payments_tasker_idx ON payments(tasker_id,created_at DESC);
CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  payment_id INTEGER NOT NULL UNIQUE REFERENCES payments(id),
  tasker_id TEXT NOT NULL REFERENCES users(id),
  amount_paise INTEGER NOT NULL,
  provider_transfer_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  from_user_id TEXT NOT NULL REFERENCES users(id),
  to_user_id TEXT NOT NULL REFERENCES users(id),
  score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
  review TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(task_id,from_user_id)
);
CREATE INDEX IF NOT EXISTS ratings_to_user_idx ON ratings(to_user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reporter_id TEXT NOT NULL REFERENCES users(id),
  subject_user_id TEXT REFERENCES users(id),
  task_id INTEGER REFERENCES tasks(id),
  reason TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','UNDER_REVIEW','RESOLVED','REJECTED')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  opened_by TEXT NOT NULL REFERENCES users(id),
  reason TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','UNDER_REVIEW','RESOLVED','REJECTED')),
  resolution TEXT NOT NULL DEFAULT '',
  resolution_action TEXT NOT NULL DEFAULT '',
  resolved_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS disputes_status_idx ON disputes(status,created_at DESC);
CREATE TABLE IF NOT EXISTS dispute_evidence (
  id TEXT PRIMARY KEY,
  dispute_id INTEGER NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL REFERENCES users(id),
  mime TEXT NOT NULL,
  data BLOB NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_blocks (
  blocker_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  blocked_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY(blocker_id,blocked_id),
  CHECK(blocker_id != blocked_id)
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (
  bucket TEXT PRIMARY KEY,
  hits INTEGER NOT NULL,
  window_start INTEGER NOT NULL
);
"""


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # Additive migrations keep existing local accounts and tasks intact.
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "delivery_mode" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'LOCAL' CHECK(delivery_mode IN ('LOCAL','DIGITAL'))")
        if "integrity_acknowledged" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN integrity_acknowledged INTEGER NOT NULL DEFAULT 0")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "is_student" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_student INTEGER NOT NULL DEFAULT 0")
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
        for column, declaration in (("attachment_name", "TEXT"), ("attachment_mime", "TEXT"), ("attachment_data", "BLOB")):
            if column not in message_columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {declaration}")
        for order, (slug, name, name_ta, icon) in enumerate(CATEGORY_SEEDS):
            conn.execute(
                "INSERT INTO categories(slug,name,name_ta,icon,sort_order) VALUES(?,?,?,?,?) "
                "ON CONFLICT(slug) DO UPDATE SET name=excluded.name,name_ta=excluded.name_ta,icon=excluded.icon,sort_order=excluded.sort_order",
                (slug, name, name_ta, icon, order),
            )
        defaults = {"commission_percent": "10", "commission_min_paise": "0", "commission_max_paise": "100000", "promo_percent": "", "promo_code": "", "platform_paused": "false"}
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)", (key, value, now_iso()))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, uncertain: bool = False):
        self.status, self.code, self.message = status, code, message
        self.uncertain = uncertain


def safe_json(value: Any) -> Any:
    if isinstance(value, sqlite3.Row):
        return {k: safe_json(value[k]) for k in value.keys()}
    if isinstance(value, list):
        return [safe_json(v) for v in value]
    if isinstance(value, dict):
        return {k: safe_json(v) for k, v in value.items()}
    return value


def as_int(value: Any, default: int = 0, low: int = 0, high: int = 10**9) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, number))


def money_paise(value: Any, field: str = "amount") -> int:
    try:
        if len(str(value))>32:
            raise InvalidOperation()
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ApiError(400, "invalid_amount", f"Enter a valid {field}.")
    if not amount.is_finite() or amount < Decimal("1") or amount > Decimal("250000"):
        raise ApiError(400, "invalid_amount", f"{field.capitalize()} must be between ₹1 and ₹2,50,000.")
    return int(amount * 100)


def calculate_payment_split(amount_paise: int, commission_percent: str, minimum_fee: str, maximum_fee: str, promo_percent: str = "", digital: bool = False) -> tuple[int,int,int]:
    fee=int((Decimal(amount_paise)*Decimal(commission_percent)/100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
    fee=max(int(minimum_fee),fee)
    fee=min(int(maximum_fee),fee,amount_paise-100)
    if promo_percent:
        discount=int((Decimal(fee)*Decimal(promo_percent)/100).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        fee=max(0,fee-discount)
    payout=amount_paise if digital else amount_paise-fee
    customer_charge=amount_paise+fee if digital else amount_paise
    return fee,payout,customer_charge


def normalize_phone(raw: Any) -> str:
    phone = re.sub(r"[\s()\-]", "", str(raw or ""))
    if re.fullmatch(r"[6-9]\d{9}", phone):
        phone = "+91" + phone
    elif re.fullmatch(r"91[6-9]\d{9}", phone):
        phone = "+" + phone
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ApiError(400, "invalid_phone", "Enter a valid phone number with country code.")
    return phone


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def profile(conn: sqlite3.Connection, user_id: str, viewer_id: str | None = None) -> dict[str, Any]:
    row = conn.execute(
        "SELECT u.id,u.name,u.email,u.phone,u.about,u.city,u.area,u.languages,u.skills,u.is_student,u.service_radius_km,u.lat,u.lng,u.joined_at,u.suspended, "
        "EXISTS(SELECT 1 FROM user_verifications v WHERE v.user_id=u.id AND v.kind='phone' AND v.status='verified') AS phone_verified, "
        "(SELECT COUNT(*) FROM task_assignments a JOIN tasks t ON t.id=a.task_id WHERE a.tasker_id=u.id AND t.status='PAYMENT_RELEASED') AS completed_tasks, "
        "(SELECT AVG(score) FROM ratings r WHERE r.to_user_id=u.id) AS rating, "
        "(SELECT COUNT(*) FROM ratings r WHERE r.to_user_id=u.id) AS rating_count, "
        "(SELECT razorpay_linked_account_id FROM user_payout_profiles p WHERE p.user_id=u.id) AS payout_connected "
        "FROM users u WHERE u.id=?", (user_id,)
    ).fetchone()
    if not row:
        raise ApiError(404, "user_not_found", "This profile is no longer available.")
    data = safe_json(row)
    data["languages"] = json.loads(data["languages"] or "[]")
    data["skills"] = json.loads(data["skills"] or "[]")
    data["rating"] = round(data["rating"] or 0, 1)
    data["is_admin"] = bool(conn.execute("SELECT 1 FROM admin_users WHERE user_id=?", (user_id,)).fetchone())
    data["identity_verified"] = bool(conn.execute("SELECT 1 FROM user_verifications WHERE user_id=? AND kind='identity' AND status='verified'", (user_id,)).fetchone())
    data["has_photo"] = bool(conn.execute("SELECT 1 FROM users WHERE id=? AND photo IS NOT NULL", (user_id,)).fetchone())
    data["photo_url"] = f"/api/users/{user_id}/photo" if data["has_photo"] else ""
    data["payout_connected"] = bool(data["payout_connected"])
    data["verified"] = bool(data["phone_verified"])
    data["blocked"] = bool(viewer_id and conn.execute("SELECT 1 FROM user_blocks WHERE blocker_id=? AND blocked_id=?", (viewer_id,user_id)).fetchone())
    data.pop("suspended", None)
    return data


# This table is intentionally separate so the account token is never exposed.
with db() as _conn:
    _conn.execute("CREATE TABLE IF NOT EXISTS user_payout_profiles(user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,razorpay_linked_account_id TEXT UNIQUE,verified_by TEXT REFERENCES users(id),updated_at TEXT NOT NULL)")


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM app_settings")}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(min(1.0, a)))


def validate_coordinates(lat: Any, lng: Any) -> tuple[float, float]:
    try:
        latitude, longitude = float(lat), float(lng)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_location", "Choose a valid location or use your current location.")
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ApiError(400, "invalid_location", "Choose a valid location or use your current location.")
    return latitude, longitude


def clean_image(value: Any) -> tuple[str, bytes]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_image", "One of the photos could not be uploaded.")
    raw = str(value.get("data", ""))
    match = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", raw)
    if not match:
        raise ApiError(400, "invalid_image", "Use a JPG, PNG or WebP image.")
    mime = match.group(1)
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except ValueError:
        raise ApiError(400, "invalid_image", "One of the photos could not be uploaded.")
    if not data or len(data) > MAX_IMAGE:
        raise ApiError(413, "image_too_large", "Each photo must be smaller than 1 MB.")
    signatures = {"image/jpeg": data.startswith(b"\xff\xd8\xff"), "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"), "image/webp": len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"}
    if not signatures[mime]:
        raise ApiError(400, "invalid_image", "The selected file is not a valid image.")
    return mime, data


UPLOAD_TYPES = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}
OOXML_PARTS = {
    ".pptx": "ppt/presentation.xml",
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
}


def clean_upload(value: Any, *, pdf_only: bool = False) -> tuple[str, str, bytes]:
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_upload", "Choose a supported document or photo and try again.")
    filename = Path(str(value.get("name", "" )).replace("\\", "/")).name
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")[:120]
    ext = Path(filename).suffix.lower()
    mime_by_ext = {".pdf": "application/pdf"} if pdf_only else UPLOAD_TYPES
    expected_mime = mime_by_ext.get(ext)
    raw = str(value.get("data", ""))
    match = re.fullmatch(r"data:([A-Za-z0-9.+/-]+);base64,([A-Za-z0-9+/=]+)", raw)
    if not filename or not expected_mime or not match or match.group(1).lower() != expected_mime:
        message = "Upload a PDF preview." if pdf_only else "Use a PDF, PowerPoint, Word, Excel, JPG, PNG or WebP file."
        raise ApiError(400, "invalid_upload", message)
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except ValueError:
        raise ApiError(400, "invalid_upload", "This file could not be read. Please choose it again.")
    if not data or len(data) > MAX_DOCUMENT:
        raise ApiError(413, "upload_too_large", "Each document must be smaller than 8 MB.")
    if expected_mime == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise ApiError(400, "invalid_upload", "That file doesn't appear to be a valid PDF.")
    elif expected_mime.startswith("image/"):
        image_mime, checked = clean_image({"data": raw})
        if image_mime != expected_mime:
            raise ApiError(400, "invalid_upload", "The selected file type doesn't match its contents.")
        data = checked
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if len(names) > 500 or sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                    raise ValueError("oversized package")
                if "[Content_Types].xml" not in names or OOXML_PARTS.get(ext) not in names:
                    raise ValueError("not an Office Open XML document")
        except (OSError, zipfile.BadZipFile, ValueError):
            raise ApiError(400, "invalid_upload", "That Office file could not be verified. Save it as a standard PPTX, DOCX or XLSX and try again.")
    return filename or "file", expected_mime, data


def auth_provider_ready() -> bool:
    return all(os.environ.get(key) for key in ("TWILIO_API_KEY", "TWILIO_API_SECRET", "TWILIO_VERIFY_SERVICE_SID"))


def payment_provider_ready() -> bool:
    return all(os.environ.get(key) for key in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"))


def push_ready() -> bool:
    return all(os.environ.get(key) for key in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"))


def twilio_request(phone: str, code: str | None = None) -> dict[str, Any]:
    service = os.environ["TWILIO_VERIFY_SERVICE_SID"]
    endpoint = f"https://verify.twilio.com/v2/Services/{urllib.parse.quote(service)}/"
    if code is None:
        endpoint += "Verifications"
        fields = {"To": phone, "Channel": "sms"}
    else:
        endpoint += "VerificationCheck"
        fields = {"To": phone, "Code": code}
    api_key = os.environ["TWILIO_API_KEY"]
    secret = os.environ["TWILIO_API_SECRET"]
    request = urllib.request.Request(endpoint, data=urllib.parse.urlencode(fields).encode(), method="POST")
    request.add_header("Authorization", "Basic " + base64.b64encode(f"{api_key}:{secret}".encode()).decode())
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        raise ApiError(502, "otp_provider_error", body.get("message", "We couldn't send or verify your code. Please try again."))
    except (urllib.error.URLError, TimeoutError):
        raise ApiError(503, "otp_unavailable", "Phone verification is temporarily unavailable. Please try again.")


def razorpay_request(path: str, payload: dict[str, Any] | list[tuple[str, str]] | None = None, method: str = "GET", raw: bytes | None = None) -> dict[str, Any]:
    if not payment_provider_ready():
        raise ApiError(503, "payments_unavailable", "Payments are not configured yet. Your task and funds have not changed.")
    key_id, secret = os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]
    data: bytes | None = raw
    content_type = "application/json"
    if payload is not None:
        if isinstance(payload, list):
            data = urllib.parse.urlencode(payload).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request("https://api.razorpay.com/v1" + path, data=data, method=method)
    request.add_header("Authorization", "Basic " + base64.b64encode(f"{key_id}:{secret}".encode()).decode())
    request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read())
            return result if isinstance(result, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", {}).get("description", "")
        except Exception:
            detail = ""
        raise ApiError(502, "payment_provider_error", detail or "The payment provider could not complete that request. Please try again.", uncertain=exc.code >= 500)
    except (urllib.error.URLError, TimeoutError):
        raise ApiError(503, "payment_provider_unavailable", "The payment provider is temporarily unavailable. The request may still be processing; contact support before retrying.", uncertain=True)


_push_queue: queue.Queue[tuple[str,dict[str,str]]] = queue.Queue(maxsize=1000)


def notify(conn: sqlite3.Connection, user_id: str, kind: str, title: str, body: str, task_id: str | None = None) -> None:
    created = now_iso()
    conn.execute("INSERT INTO notifications(user_id,kind,title,body,task_id,created_at) VALUES(?,?,?,?,?,?)", (user_id,kind,title,body,task_id,created))
    publish(user_id, {"type": kind, "title": title, "body": body, "task_id": task_id, "created_at": created})
    suffix="chat" if kind=="new_message" else "task"
    if push_ready():
        try: _push_queue.put_nowait((user_id,{"title": title, "body": body, "url": f"/?{suffix}={task_id}" if task_id else "/"}))
        except queue.Full: pass


_clients: dict[str, set[queue.Queue[str]]] = {}
_clients_lock = threading.Lock()


def publish(user_id: str, event: dict[str, Any]) -> None:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with _clients_lock:
        targets = list(_clients.get(user_id, set()))
    for target in targets:
        try:
            target.put_nowait(payload)
        except queue.Full:
            pass


def push_user(user_id: str, event: dict[str, str]) -> None:
    if not push_ready():
        return
    try:
        from pywebpush import WebPushException, webpush
        with db() as conn:
            subscriptions = conn.execute("SELECT endpoint,subscription_json FROM push_subscriptions WHERE user_id=?", (user_id,)).fetchall()
        for sub in subscriptions:
            try:
                webpush(json.loads(sub["subscription_json"]), json.dumps(event), vapid_private_key=os.environ["VAPID_PRIVATE_KEY"], vapid_claims={"sub": os.environ["VAPID_SUBJECT"]})
            except WebPushException as exc:
                if getattr(getattr(exc, "response", None), "status_code", 0) in (404, 410):
                    with db() as conn:
                        conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (sub["endpoint"],))
    except ImportError:
        # Push is an optional deployment extra; in-app SSE notifications remain active.
        return


def push_worker() -> None:
    while True:
        user_id,event=_push_queue.get()
        try:
            push_user(user_id,event)
        except Exception:
            pass
        finally:
            _push_queue.task_done()


if push_ready():
    threading.Thread(target=push_worker,daemon=True,name="doomsday-push").start()


def publish_to_nearby(conn: sqlite3.Connection, lat: float, lng: float, city: str, task_id: str, title: str, digital: bool = False) -> None:
    if digital:
        candidates=conn.execute("SELECT id FROM users WHERE suspended=0").fetchall()
        for candidate in candidates:
            notify(conn,candidate["id"],"nearby_task","New online study task",f"{title} is available as an online task.",task_id)
        return
    lat_delta=50/110.574
    lon_delta=50/max(1,111.320*math.cos(math.radians(lat)))
    candidates=conn.execute("SELECT id,lat,lng,service_radius_km FROM users WHERE suspended=0 AND id IN (SELECT id FROM users WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?)", (lat-lat_delta,lat+lat_delta,lng-lon_delta,lng+lon_delta)).fetchall()
    for candidate in candidates:
        if candidate["lat"] is None or candidate["lng"] is None:
            continue
        distance=haversine_km(lat,lng,candidate["lat"],candidate["lng"])
        if distance <= candidate["service_radius_km"]:
            notify(conn,candidate["id"],"nearby_task","A new task nearby",f"{title} was posted near you.",task_id)


def request_rate(conn: sqlite3.Connection, key: str, limit: int, window: int) -> None:
    stamp = int(time.time())
    row = conn.execute("SELECT hits,window_start FROM rate_limits WHERE bucket=?", (key,)).fetchone()
    if not row or stamp - row["window_start"] >= window:
        conn.execute("INSERT INTO rate_limits(bucket,hits,window_start) VALUES(?,?,?) ON CONFLICT(bucket) DO UPDATE SET hits=excluded.hits,window_start=excluded.window_start", (key,1,stamp))
        conn.commit()
        return
    if row["hits"] >= limit:
        raise ApiError(429, "rate_limited", "Too many attempts. Please wait a few minutes and try again.")
    conn.execute("UPDATE rate_limits SET hits=hits+1 WHERE bucket=?", (key,))
    conn.commit()


def session_user(conn: sqlite3.Connection, handler: "Handler", required: bool = True) -> sqlite3.Row | None:
    token = request_session_token(handler)
    if not token:
        if required: raise ApiError(401, "sign_in_required", "Sign in to continue.")
        return None
    token_hash = hash_token(token)
    row = conn.execute("SELECT u.*,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (token_hash,)).fetchone()
    if not row or row["expires_at"] <= now_iso():
        if required:
            raise ApiError(401, "session_expired", "Your session expired. Please sign in again.")
        return None
    if row["suspended"]:
        raise ApiError(403, "account_suspended", "This account is currently restricted. Contact support for help.")
    return row


def request_session_token(handler: "Handler") -> str:
    header=handler.headers.get("Authorization","")
    if header.startswith("Bearer "):
        return header[7:].strip()
    cookies=SimpleCookie()
    try: cookies.load(handler.headers.get("Cookie",""))
    except Exception: return ""
    cookie=cookies.get("doomsday_session")
    return cookie.value if cookie else ""


def mobile_allowed_origins() -> set[str]:
    configured = os.environ.get("MOBILE_ALLOWED_ORIGINS", "capacitor://localhost,https://localhost")
    return {origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()}


def client_ip(handler: "Handler") -> str:
    direct=handler.client_address[0] if handler.client_address else "unknown"
    if os.environ.get("TRUST_PROXY","false").lower()!="true": return direct
    forwarded=handler.headers.get("X-Forwarded-For","").split(",",1)[0].strip()
    try: return str(ipaddress.ip_address(forwarded))
    except ValueError: return direct


def require_admin(conn: sqlite3.Connection, user: sqlite3.Row) -> None:
    if not conn.execute("SELECT 1 FROM admin_users WHERE user_id=?", (user["id"],)).fetchone():
        raise ApiError(403, "admin_only", "You don't have permission to access this area.")


def require_task_participant(task: sqlite3.Row, user_id: str) -> None:
    if user_id not in (task["customer_id"], task["assigned_tasker_id"]):
        raise ApiError(403, "task_access_denied", "This task is private to its customer and selected tasker.")


def task_select_columns() -> str:
    return """t.public_id,t.customer_id,t.category_id,t.title,t.description,t.public_lat,t.public_lng,t.lat private_lat,t.lng private_lng,t.city,t.time_mode,t.scheduled_at,t.budget_mode,t.budget_min_paise,t.budget_max_paise,t.delivery_mode,t.status,t.assigned_tasker_id,t.created_at,t.updated_at,c.slug category_slug,c.name category_name,c.name_ta category_name_ta,c.icon category_icon,u.name customer_name,COALESCE((SELECT AVG(score) FROM ratings r WHERE r.to_user_id=t.customer_id),0) customer_rating,(SELECT COUNT(*) FROM offers o WHERE o.task_id=t.id AND o.status='PENDING') offer_count,EXISTS(SELECT 1 FROM task_submissions s WHERE s.task_id=t.id) has_submission"""


def task_payload(conn: sqlite3.Connection, row: sqlite3.Row, viewer_id: str | None = None, viewer_lat: float | None = None, viewer_lng: float | None = None) -> dict[str, Any]:
    data = safe_json(row)
    data["budget_min"] = data["budget_min_paise"] / 100
    data["budget_max"] = data["budget_max_paise"] / 100
    data["distance_km"] = round(haversine_km(viewer_lat,viewer_lng,data.get("private_lat",data["public_lat"]),data.get("private_lng",data["public_lng"])),1) if viewer_lat is not None and viewer_lng is not None and data.get("delivery_mode")!="DIGITAL" else None
    data["is_digital"] = data.get("delivery_mode")=="DIGITAL"
    if data["is_digital"]:
        data.pop("public_lat",None); data.pop("public_lng",None)
    data.pop("private_lat",None); data.pop("private_lng",None)
    data["is_mine"] = bool(viewer_id and data["customer_id"] == viewer_id)
    data["is_assigned"] = bool(data.get("assigned_tasker_id"))
    data["assigned_to_me"] = bool(viewer_id and data.get("assigned_tasker_id") == viewer_id)
    data["has_offer"] = bool(viewer_id and conn.execute("SELECT 1 FROM offers o JOIN tasks t ON t.id=o.task_id WHERE t.public_id=? AND o.tasker_id=?", (data["public_id"],viewer_id)).fetchone())
    data["has_reviewed"] = bool(viewer_id and conn.execute("SELECT 1 FROM ratings r JOIN tasks t ON t.id=r.task_id WHERE t.public_id=? AND r.from_user_id=?", (data["public_id"],viewer_id)).fetchone())
    data["image_count"] = conn.execute("SELECT COUNT(*) FROM task_images i JOIN tasks t ON t.id=i.task_id WHERE t.public_id=?", (data["public_id"],)).fetchone()[0]
    data.pop("customer_id", None)
    if not data["is_mine"] and not data["assigned_to_me"]:
        data.pop("assigned_tasker_id", None)
    return data


def visible_blocks(conn: sqlite3.Connection, user_id: str) -> set[str]:
    rows = conn.execute("SELECT blocked_id AS id FROM user_blocks WHERE blocker_id=? UNION SELECT blocker_id AS id FROM user_blocks WHERE blocked_id=?", (user_id,user_id)).fetchall()
    return {row["id"] for row in rows}


class Handler(BaseHTTPRequestHandler):
    server_version = "Doomsday/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("LOG_REQUESTS", "false").lower() == "true":
            super().log_message(fmt, *args)

    def cors_headers(self) -> list[tuple[str,str]]:
        origin = self.headers.get("Origin", "").rstrip("/")
        if not urllib.parse.urlsplit(self.path).path.startswith("/api/") or origin not in mobile_allowed_origins():
            return []
        return [("Access-Control-Allow-Origin", origin), ("Vary", "Origin")]

    def send_json(self, status: int, payload: Any, extra_headers: list[tuple[str,str]] | None = None) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self' https://checkout.razorpay.com; connect-src 'self' https://api.razorpay.com https://verify.twilio.com; frame-src 'self' https://api.razorpay.com https://checkout.razorpay.com; base-uri 'self'; form-action 'self'; object-src 'none'; frame-ancestors 'none'")
        for name,value in self.cors_headers(): self.send_header(name,value)
        for name,value in extra_headers or []: self.send_header(name,value)
        self.end_headers()
        self.wfile.write(encoded)

    def send_blob(self, data: bytes, mime: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        for name,value in self.cors_headers(): self.send_header(name,value)
        self.end_headers()
        self.wfile.write(data)

    def send_private_file(self, data: bytes, mime: str, filename: str, disposition: str) -> None:
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename).name).strip(" .")[:120] or "file"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'{disposition}; filename="{safe_name}"')
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN" if disposition == "inline" else "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        for name,value in self.cors_headers(): self.send_header(name,value)
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY:
            raise ApiError(413, "request_too_large", "This upload is too large. Try fewer or smaller photos.")
        if length <= 0:
            return {}
        try:
            self._raw_body = self.rfile.read(length)
            parsed = json.loads(self._raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "invalid_json", "We couldn't read that request. Please try again.")
        if not isinstance(parsed, dict):
            raise ApiError(400, "invalid_request", "We couldn't read that request. Please try again.")
        return parsed

    def do_OPTIONS(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        origin = self.headers.get("Origin", "").rstrip("/")
        requested_method = self.headers.get("Access-Control-Request-Method", "GET").upper()
        requested_headers = {value.strip().lower() for value in self.headers.get("Access-Control-Request-Headers", "").split(",") if value.strip()}
        if not path.startswith("/api/") or origin not in mobile_allowed_origins() or requested_method not in {"GET", "POST", "PATCH", "DELETE"} or not requested_headers.issubset({"authorization", "content-type"}):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        for name,value in self.cors_headers(): self.send_header(name,value)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/events":
                self.events()
                return
            if path.startswith("/api/"):
                with db() as conn:
                    if path!="/api/health":
                        ip=client_ip(self)
                        request_rate(conn,"read-ip:"+hashlib.sha256(ip.encode()).hexdigest(),1200,60)
                    self.api_get(conn, path, params)
                return
            self.static_file(path)
        except ApiError as exc:
            self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.send_json(500, {"error": {"code": "unexpected_error", "message": "Something went wrong. Please try again."}})

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PATCH(self) -> None:
        self.dispatch("PATCH")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            payload = self.body()
            with db() as conn:
                if path!="/api/webhooks/razorpay":
                    ip=client_ip(self)
                    request_rate(conn,"write-ip:"+hashlib.sha256(ip.encode()).hexdigest(),240,60)
                result, status = self.api_write(conn, method, path, payload)
                extra_headers=[]
                if method=="POST" and path=="/api/auth/verify" and status==200 and result.get("token"):
                    secure="; Secure" if os.environ.get("COOKIE_SECURE","false").lower()=="true" else ""
                    extra_headers.append(("Set-Cookie",f"doomsday_session={result['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS*86400}{secure}"))
                elif method=="POST" and path=="/api/auth/logout":
                    secure="; Secure" if os.environ.get("COOKIE_SECURE","false").lower()=="true" else ""
                    extra_headers.append(("Set-Cookie",f"doomsday_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"))
                self.send_json(status, result, extra_headers)
        except ApiError as exc:
            self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except sqlite3.IntegrityError as exc:
            text = str(exc).lower()
            if "unique" in text:
                self.send_json(409, {"error": {"code": "already_exists", "message": "That action has already been completed."}})
            else:
                self.send_json(400, {"error": {"code": "invalid_request", "message": "We couldn't save that change. Check the details and try again."}})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.send_json(500, {"error": {"code": "unexpected_error", "message": "Something went wrong. Please try again."}})

    def static_file(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        resolved = (STATIC / path.lstrip("/")).resolve()
        if not str(resolved).startswith(str(STATIC.resolve())) or not resolved.is_file():
            self.send_json(404, {"error": {"code": "not_found", "message": "This page couldn't be found."}})
            return
        data = resolved.read_bytes()
        mime = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith(("text/", "application/javascript", "application/manifest+json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if resolved.name in ("index.html", "sw.js", "app.js", "styles.css") else "public,max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self' https://checkout.razorpay.com; connect-src 'self' https://checkout.razorpay.com; frame-src https://api.razorpay.com https://checkout.razorpay.com; base-uri 'self'; form-action 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def events(self) -> None:
        with db() as conn:
            ip=client_ip(self)
            request_rate(conn,"events-ip:"+hashlib.sha256(ip.encode()).hexdigest(),120,60)
            user = session_user(conn, self)
            user_id = user["id"]
        subscriber: queue.Queue[str] = queue.Queue(maxsize=100)
        with _clients_lock:
            _clients.setdefault(user_id, set()).add(subscriber)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        for name,value in self.cors_headers(): self.send_header(name,value)
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        try:
            while True:
                try:
                    event = subscriber.get(timeout=18)
                    self.wfile.write(("data: " + event + "\n\n").encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _clients_lock:
                _clients.get(user_id, set()).discard(subscriber)
                if not _clients.get(user_id):
                    _clients.pop(user_id, None)

    def api_get(self, conn: sqlite3.Connection, path: str, params: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self.send_json(200, {"ok": True, "app": APP_NAME, "version": "1.0.0"})
            return
        if path == "/api/config":
            key = os.environ.get("RAZORPAY_KEY_ID", "")
            settings=get_settings(conn)
            self.send_json(200, {"app_name": APP_NAME,"auth_ready": auth_provider_ready(),"payments_ready": payment_provider_ready(),"razorpay_key_id": key,"push_ready": push_ready(),"vapid_public_key": os.environ.get("VAPID_PUBLIC_KEY", ""),"platform_paused":settings.get("platform_paused")=="true","locale_default":"en"})
            return
        if path == "/api/categories":
            rows = conn.execute("SELECT slug,name,name_ta,icon FROM categories WHERE active=1 ORDER BY sort_order").fetchall()
            self.send_json(200, {"categories": safe_json(rows)})
            return
        user = session_user(conn, self, required=False)
        uid = user["id"] if user else None
        if path == "/api/me":
            if not user:
                raise ApiError(401, "sign_in_required", "Sign in to continue.")
            self.send_json(200, {"user": profile(conn, uid, uid)})
            return
        public_profile_match=re.fullmatch(r"/api/profiles/([0-9a-f-]{36})",path)
        if public_profile_match:
            if not user: raise ApiError(401,"sign_in_required","Sign in to view profiles.")
            target=public_profile_match.group(1)
            if conn.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(uid,target,target,uid)).fetchone(): raise ApiError(404,"profile_not_found","This profile isn't available.")
            row=conn.execute("SELECT u.id,u.name,u.about,u.city,u.area,u.languages,u.skills,u.joined_at,(u.photo IS NOT NULL) has_photo,COALESCE((SELECT AVG(score) FROM ratings r WHERE r.to_user_id=u.id),0) rating,(SELECT COUNT(*) FROM ratings r WHERE r.to_user_id=u.id) rating_count,(SELECT COUNT(*) FROM task_assignments a JOIN tasks t ON t.id=a.task_id WHERE a.tasker_id=u.id AND t.status='PAYMENT_RELEASED') completed_tasks,EXISTS(SELECT 1 FROM user_verifications v WHERE v.user_id=u.id AND v.kind='phone' AND v.status='verified') phone_verified,EXISTS(SELECT 1 FROM user_verifications v WHERE v.user_id=u.id AND v.kind='identity' AND v.status='verified') identity_verified FROM users u WHERE u.id=? AND u.suspended=0",(target,)).fetchone()
            if not row: raise ApiError(404,"profile_not_found","This profile isn't available.")
            result=safe_json(row); result["languages"]=json.loads(result["languages"] or "[]"); result["skills"]=json.loads(result["skills"] or "[]"); result["rating"]=round(result["rating"] or 0,1); result["verified"]=bool(result["phone_verified"]); result["photo_url"]=f"/api/users/{target}/photo" if result["has_photo"] else ""
            result["reviews"]=safe_json(conn.execute("SELECT r.score,r.review,r.created_at,u.name reviewer_name FROM ratings r JOIN users u ON u.id=r.from_user_id WHERE r.to_user_id=? AND r.review!='' ORDER BY r.created_at DESC LIMIT 20",(target,)).fetchall())
            self.send_json(200,{"profile":result})
            return
        if path == "/api/tasks":
            if not user:
                raise ApiError(401, "sign_in_required", "Sign in to see nearby tasks.")
            self.list_tasks(conn, user, params)
            return
        if path == "/api/my/tasks":
            if not user:
                raise ApiError(401, "sign_in_required", "Sign in to see your tasks.")
            rows = conn.execute("SELECT " + task_select_columns() + " FROM tasks t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.customer_id WHERE t.customer_id=? ORDER BY t.created_at DESC LIMIT 100", (uid,)).fetchall()
            tasks = [task_payload(conn,r,uid) for r in rows]
            self.send_json(200, {"tasks":tasks})
            return
        if path == "/api/my/work":
            if not user:
                raise ApiError(401, "sign_in_required", "Sign in to see your work.")
            rows = conn.execute("SELECT " + task_select_columns() + " FROM tasks t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.customer_id WHERE t.assigned_tasker_id=? ORDER BY t.updated_at DESC LIMIT 100", (uid,)).fetchall()
            self.send_json(200, {"tasks":[task_payload(conn,r,uid) for r in rows]})
            return
        if path == "/api/notifications":
            if not user:
                raise ApiError(401, "sign_in_required", "Sign in to see notifications.")
            rows = conn.execute("SELECT id,kind,title,body,task_id,read_at,created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
            unread = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (uid,)).fetchone()[0]
            self.send_json(200, {"notifications":safe_json(rows),"unread":unread})
            return
        if path == "/api/inbox":
            if not user: raise ApiError(401,"sign_in_required","Sign in to see your messages.")
            rows=conn.execute("SELECT c.task_id,t.public_id task_public_id,t.title task_title,t.status,CASE WHEN c.customer_id=? THEN c.tasker_id ELSE c.customer_id END other_id,CASE WHEN c.customer_id=? THEN other.name ELSE customer.name END other_name,CASE WHEN c.customer_id=? THEN (other.photo IS NOT NULL) ELSE (customer.photo IS NOT NULL) END other_has_photo,(SELECT m.body FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) last_message,(SELECT m.created_at FROM messages m WHERE m.conversation_id=c.id ORDER BY m.id DESC LIMIT 1) last_at,(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id AND m.sender_id!=? AND m.read_at IS NULL) unread FROM conversations c JOIN tasks t ON t.id=c.task_id JOIN users other ON other.id=CASE WHEN c.customer_id=? THEN c.tasker_id ELSE c.customer_id END JOIN users customer ON customer.id=c.customer_id WHERE c.customer_id=? OR c.tasker_id=? ORDER BY COALESCE(last_at,c.created_at) DESC LIMIT 100",(uid,uid,uid,uid,uid,uid,uid)).fetchall()
            result=[]
            for r in rows:
                item=safe_json(r); item["other_photo_url"]=f"/api/users/{r['other_id']}/photo" if r["other_has_photo"] else ""; item.pop("other_has_photo",None); result.append(item)
            self.send_json(200,{"conversations":result})
            return
        if path.startswith("/api/tasks/"):
            self.get_task_route(conn,user,path,params)
            return
        evidence_match=re.fullmatch(r"/api/admin/disputes/([0-9a-f-]{36})/evidence/([0-9a-f-]{36})",path)
        if evidence_match:
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            row=conn.execute("SELECT e.mime,e.data FROM dispute_evidence e JOIN disputes d ON d.id=e.dispute_id WHERE d.public_id=? AND e.id=?",(evidence_match.group(1),evidence_match.group(2))).fetchone()
            if not row: raise ApiError(404,"evidence_not_found","This evidence photo could not be found.")
            self.send_blob(row["data"],row["mime"])
            return
        if path == "/api/admin/overview":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            self.admin_overview(conn)
            return
        if path == "/api/admin/users":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            q = (params.get("q",[""])[0])[:80]
            rows = conn.execute("SELECT id,name,phone,city,suspended,joined_at FROM users WHERE name LIKE ? OR phone LIKE ? ORDER BY joined_at DESC LIMIT 100", (f"%{q}%",f"%{q}%")).fetchall()
            result=[]
            for r in rows:
                item=safe_json(r); item["verified"]=bool(conn.execute("SELECT 1 FROM user_verifications WHERE user_id=? AND kind='phone' AND status='verified'",(r["id"],)).fetchone()); result.append(item)
            self.send_json(200,{"users":result})
            return
        if path == "/api/admin/tasks":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            rows=conn.execute("SELECT t.public_id,t.title,t.status,t.created_at,u.name customer_name,u.phone FROM tasks t JOIN users u ON u.id=t.customer_id ORDER BY t.created_at DESC LIMIT 150").fetchall()
            self.send_json(200,{"tasks":safe_json(rows)})
            return
        if path == "/api/admin/payments":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            rows=conn.execute("SELECT p.public_id,p.task_id,p.amount_paise,p.fee_paise,p.payout_paise,p.status,p.transfer_status,p.provider_order_id,p.provider_payment_id,p.provider_transfer_id,p.created_at,t.public_id task_public_id,t.title FROM payments p JOIN tasks t ON t.id=p.task_id ORDER BY p.created_at DESC LIMIT 150").fetchall()
            self.send_json(200,{"payments":safe_json(rows)})
            return
        if path == "/api/admin/disputes":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            rows=conn.execute("SELECT d.public_id,d.reason,d.description,d.status,d.resolution,d.resolution_action,d.created_at,d.updated_at,t.public_id task_public_id,t.title,u.name opener_name FROM disputes d JOIN tasks t ON t.id=d.task_id JOIN users u ON u.id=d.opened_by ORDER BY d.created_at DESC LIMIT 150").fetchall()
            disputes=[]
            for row in rows:
                item=safe_json(row)
                item["evidence"]=[{"id":e["id"],"url":f"/api/admin/disputes/{row['public_id']}/evidence/{e['id']}"} for e in conn.execute("SELECT id FROM dispute_evidence WHERE dispute_id=(SELECT id FROM disputes WHERE public_id=?) ORDER BY created_at",(row["public_id"],))]
                disputes.append(item)
            self.send_json(200,{"disputes":disputes})
            return
        if path == "/api/admin/reports":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            rows=conn.execute("SELECT r.id,r.reason,r.description,r.status,r.created_at,u.name reporter_name,s.name subject_name,t.public_id task_public_id FROM reports r JOIN users u ON u.id=r.reporter_id LEFT JOIN users s ON s.id=r.subject_user_id LEFT JOIN tasks t ON t.id=r.task_id ORDER BY r.created_at DESC LIMIT 150").fetchall()
            self.send_json(200,{"reports":safe_json(rows)})
            return
        if path == "/api/admin/settings":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            self.send_json(200,{"settings":get_settings(conn)})
            return
        if path == "/api/admin/categories":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_admin(conn,user)
            rows=conn.execute("SELECT id,slug,name,name_ta,icon,active,sort_order FROM categories ORDER BY sort_order,name").fetchall()
            self.send_json(200,{"categories":safe_json(rows)})
            return
        match = re.fullmatch(r"/api/users/([0-9a-f-]{36})/photo",path)
        if match:
            row=conn.execute("SELECT photo,photo_mime FROM users WHERE id=?",(match.group(1),)).fetchone()
            if not row or not row["photo"]: raise ApiError(404,"photo_not_found","There is no profile photo here yet.")
            self.send_blob(row["photo"],row["photo_mime"])
            return
        image_match=re.fullmatch(r"/api/messages/(\d+)/image",path)
        if image_match:
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            row=conn.execute("SELECT m.image_data,m.image_mime,c.customer_id,c.tasker_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",(int(image_match.group(1)),)).fetchone()
            if not row or user["id"] not in (row["customer_id"],row["tasker_id"]): raise ApiError(404,"image_not_found","This message photo isn't available.")
            self.send_blob(row["image_data"],row["image_mime"])
            return
        attachment_match=re.fullmatch(r"/api/messages/(\d+)/attachment",path)
        if attachment_match:
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            return self.message_attachment(conn,user,int(attachment_match.group(1)))
        raise ApiError(404,"not_found","This page couldn't be found.")

    def list_tasks(self, conn: sqlite3.Connection, user: sqlite3.Row, params: dict[str, list[str]]) -> None:
        def val(k: str, default: str = "") -> str: return params.get(k,[default])[0][:160]
        lat = lng = None
        if val("lat") and val("lng"):
            lat,lng = validate_coordinates(val("lat"),val("lng"))
        elif user["lat"] is not None and user["lng"] is not None:
            lat,lng=float(user["lat"]),float(user["lng"])
        radius=as_int(val("radius","10"),10,1,50)
        page=as_int(val("page","1"),1,1,10**6)
        limit=as_int(val("limit","20"),20,1,40)
        where=["t.status IN ('OPEN','OFFER_RECEIVED')","t.customer_id != ?"]
        values: list[Any]=[user["id"]]
        blocks=visible_blocks(conn,user["id"])
        if blocks:
            where.append("t.customer_id NOT IN ("+",".join("?" for _ in blocks)+")"); values.extend(blocks)
        category=val("category")
        if category:
            where.append("c.slug=?"); values.append(category)
        budget_min=val("budget_min"); budget_max=val("budget_max")
        if budget_min:
            where.append("t.budget_max_paise>=?"); values.append(money_paise(budget_min,"minimum budget"))
        if budget_max:
            try: max_paise=int(Decimal(budget_max)*100)
            except Exception: raise ApiError(400,"invalid_amount","Enter a valid maximum budget.")
            where.append("t.budget_min_paise<=?"); values.append(max_paise)
        if val("date") in ("ASAP","TODAY","TOMORROW","SCHEDULED"):
            where.append("t.time_mode=?"); values.append(val("date"))
        search=val("q").strip()
        if search:
            words=[w for w in re.findall(r"[\w\u0B80-\u0BFF]+",search) if w][:8]
            if words:
                match=" AND ".join('"'+w.replace('"','')+'"*' for w in words)
                where.append("t.id IN (SELECT rowid FROM task_search WHERE task_search MATCH ?)"); values.append(match)
        if lat is not None:
            lat_delta=radius/110.574
            lon_delta=radius/max(1,111.320*math.cos(math.radians(lat)))
            where.append("(t.delivery_mode='DIGITAL' OR (t.id IN (SELECT id FROM task_locations WHERE min_lat<=? AND max_lat>=? AND min_lng<=? AND max_lng>=?) AND distance_km(?,?,t.lat,t.lng)<=?))")
            values.extend([lat+lat_delta,lat-lat_delta,lng+lon_delta,lng-lon_delta,lat,lng,radius])
        else:
            if user["city"]:
                where.append("(t.delivery_mode='DIGITAL' OR lower(t.city)=lower(?))")
                values.append(user["city"])
            else:
                where.append("t.delivery_mode='DIGITAL'")
        sort=val("sort","distance" if lat is not None else "newest")
        order="t.created_at DESC"
        if sort=="budget": order="t.budget_max_paise ASC,t.created_at DESC"
        elif sort=="newest": order="t.created_at DESC"
        order_values=[]
        if sort=="distance" and lat is not None:
            order="CASE WHEN t.delivery_mode='DIGITAL' THEN 0 ELSE distance_km(?,?,t.lat,t.lng) END,t.created_at DESC"; order_values=[lat,lng]
        query="SELECT "+task_select_columns()+" FROM tasks t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.customer_id WHERE "+" AND ".join(where)+" ORDER BY "+order+" LIMIT ? OFFSET ?"
        rows=conn.execute(query,values+order_values+[limit+1,(page-1)*limit]).fetchall()
        tasks=[task_payload(conn,r,user["id"],lat,lng) for r in rows]
        self.send_json(200,{"tasks":tasks[:limit],"has_more":len(tasks)>limit,"page":page,"limit":limit})

    def get_task_route(self, conn: sqlite3.Connection, user: sqlite3.Row | None, path: str, params: dict[str,list[str]]) -> None:
        image_match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})/images/([0-9a-f-]{36})",path)
        if image_match:
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(image_match.group(1),)).fetchone()
            if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
            if user["id"] not in (task["customer_id"],task["assigned_tasker_id"]):
                if task["status"] not in ("OPEN","OFFER_RECEIVED") or conn.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(user["id"],task["customer_id"],task["customer_id"],user["id"])).fetchone():
                    raise ApiError(403,"task_access_denied","This photo is private to the task participants.")
            row=conn.execute("SELECT mime,data FROM task_images WHERE id=? AND task_id=?",(image_match.group(2),task["id"])).fetchone()
            if not row: raise ApiError(404,"image_not_found","This photo is no longer available.")
            self.send_blob(row["data"],row["mime"]); return
        submission_match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})/submission/(preview|download)",path)
        if submission_match:
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            task=conn.execute("SELECT id,customer_id,status,delivery_mode FROM tasks WHERE public_id=?",(submission_match.group(1),)).fetchone()
            if not task or task["delivery_mode"]!="DIGITAL": raise ApiError(404,"submission_not_found","This study task submission isn't available.")
            if user["id"]!=task["customer_id"]: raise ApiError(403,"submission_access_denied","Only the student who posted this task can view its submission.")
            submission=conn.execute("SELECT original_name,original_mime,original_data,preview_name,preview_mime,preview_data FROM task_submissions WHERE task_id=?",(task["id"],)).fetchone()
            if not submission: raise ApiError(404,"submission_not_found","The tasker hasn't submitted a file yet.")
            if submission_match.group(2)=="preview":
                self.send_private_file(submission["preview_data"],submission["preview_mime"],submission["preview_name"],"inline")
                return
            payment=conn.execute("SELECT status,transfer_status FROM payments WHERE task_id=?",(task["id"],)).fetchone()
            if task["status"]!="PAYMENT_RELEASED" or not payment or payment["status"] not in ("transfer_created","released") or payment["transfer_status"] not in ("settled","released"):
                raise ApiError(402,"download_locked","Pay for the completed study task to unlock the original file.")
            self.send_private_file(submission["original_data"],submission["original_mime"],submission["original_name"],"attachment")
            return
        if not user: raise ApiError(401,"sign_in_required","Sign in to view task details.")
        match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})(?:/(offers|messages|reviews|disputes))?",path)
        if not match: raise ApiError(404,"not_found","This page couldn't be found.")
        public_id,resource=match.group(1),match.group(2)
        task=conn.execute("SELECT "+task_select_columns()+",t.id task_db_id,t.address,t.landmark,t.lat,t.lng,t.selected_offer_id,t.assigned_tasker_id FROM tasks t JOIN categories c ON c.id=t.category_id JOIN users u ON u.id=t.customer_id WHERE t.public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if resource=="offers":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            if task["customer_id"]!=user["id"] and task["assigned_tasker_id"]!=user["id"]:
                own=conn.execute("SELECT 1 FROM offers WHERE task_id=? AND tasker_id=?",(task["task_db_id"],user["id"])).fetchone()
                if not own: raise ApiError(403,"offer_access_denied","Offers are private to this task's participants.")
            offer_sql="SELECT o.public_id,o.amount_paise,o.message,o.eta_minutes,o.status,o.created_at,u.id tasker_id,u.name tasker_name,COALESCE((SELECT AVG(score) FROM ratings r WHERE r.to_user_id=u.id),0) rating,(SELECT COUNT(*) FROM task_assignments a JOIN tasks t ON t.id=a.task_id WHERE a.tasker_id=u.id AND t.status='PAYMENT_RELEASED') completed_tasks,EXISTS(SELECT 1 FROM user_verifications v WHERE v.user_id=u.id AND v.kind='phone' AND v.status='verified') verified FROM offers o JOIN users u ON u.id=o.tasker_id WHERE o.task_id=?"
            if task["customer_id"]==user["id"] or conn.execute("SELECT 1 FROM admin_users WHERE user_id=?",(user["id"],)).fetchone():
                rows=conn.execute(offer_sql+" ORDER BY CASE o.status WHEN 'ACCEPTED' THEN 0 WHEN 'PENDING' THEN 1 ELSE 2 END,o.created_at DESC",(task["task_db_id"],)).fetchall()
            else:
                rows=conn.execute(offer_sql+" AND o.tasker_id=? ORDER BY o.created_at DESC",(task["task_db_id"],user["id"])).fetchall()
            self.send_json(200,{"offers":[dict(safe_json(r),amount=r["amount_paise"]/100,rating=round(r["rating"] or 0,1),is_me=bool(user and r["tasker_id"]==user["id"])) for r in rows]}); return
        if resource=="messages":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            if user["id"] not in (task["customer_id"],task["assigned_tasker_id"]) and not conn.execute("SELECT 1 FROM admin_users WHERE user_id=?",(user["id"],)).fetchone():
                require_task_participant(task,user["id"])
            convo=conn.execute("SELECT id FROM conversations WHERE task_id=?",(task["task_db_id"],)).fetchone()
            if not convo: self.send_json(200,{"messages":[]}); return
            rows=conn.execute("SELECT m.id,m.sender_id,m.body,m.image_mime,m.created_at,m.read_at,u.name sender_name,m.image_data,m.attachment_name,m.attachment_mime,m.attachment_data FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.conversation_id=? ORDER BY m.id DESC LIMIT 100",(convo["id"],)).fetchall()
            data=[]
            payout_settled=False
            if task["delivery_mode"]=="DIGITAL":
                payment=conn.execute("SELECT status,transfer_status FROM payments WHERE task_id=?",(task["task_db_id"],)).fetchone()
                payout_settled=bool(task["status"]=="PAYMENT_RELEASED" and payment and payment["status"] in ("transfer_created","released") and payment["transfer_status"] in ("settled","released"))
            for r in reversed(rows):
                item={k:r[k] for k in r.keys() if k not in ("image_data","attachment_data")}
                item["image_url"]=f"/api/messages/{r['id']}/image" if r["image_data"] else ""
                item["attachment_locked"]=bool(r["attachment_data"] and not r["attachment_mime"].startswith("image/") and task["delivery_mode"]=="DIGITAL" and user["id"]==task["customer_id"] and r["sender_id"]==task["assigned_tasker_id"] and not payout_settled)
                item["attachment_url"]=f"/api/messages/{r['id']}/attachment" if r["attachment_data"] and not item["attachment_locked"] else ""
                item["is_me"]=r["sender_id"]==user["id"]
                data.append(item)
            conn.execute("UPDATE messages SET read_at=? WHERE conversation_id=? AND sender_id!=? AND read_at IS NULL",(now_iso(),convo["id"],user["id"]))
            self.send_json(200,{"messages":data}); return
        if resource=="reviews":
            rows=conn.execute("SELECT r.score,r.review,r.created_at,u.name reviewer_name FROM ratings r JOIN users u ON u.id=r.from_user_id WHERE r.to_user_id=? ORDER BY r.created_at DESC LIMIT 50",(task["customer_id"],)).fetchall()
            self.send_json(200,{"reviews":safe_json(rows)}); return
        if resource=="disputes":
            if not user: raise ApiError(401,"sign_in_required","Sign in to continue.")
            require_task_participant(task,user["id"])
            rows=conn.execute("SELECT public_id,reason,description,status,resolution,created_at,updated_at FROM disputes WHERE task_id=? ORDER BY created_at DESC",(task["task_db_id"],)).fetchall()
            self.send_json(200,{"disputes":safe_json(rows)}); return
        viewer_id=user["id"] if user else None
        if viewer_id:
            require_task_participant(task,viewer_id) if task["customer_id"]==viewer_id or task["assigned_tasker_id"]==viewer_id else None
        is_admin=bool(user and conn.execute("SELECT 1 FROM admin_users WHERE user_id=?",(user["id"],)).fetchone())
        if not is_admin and user["id"] not in (task["customer_id"],task["assigned_tasker_id"]):
            if task["status"] not in ("OPEN","OFFER_RECEIVED") or conn.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(user["id"],task["customer_id"],task["customer_id"],user["id"])).fetchone():
                raise ApiError(404,"task_not_found","This task is no longer available.")
        exact=bool(user and (is_admin or task["customer_id"]==user["id"] or task["assigned_tasker_id"]==user["id"]))
        data=task_payload(conn,task,viewer_id)
        data["address"]=task["address"] if exact else "Shared after a tasker is selected"
        data["landmark"]=task["landmark"] if exact else ""
        data["lat"]=task["lat"] if exact and task["delivery_mode"]!="DIGITAL" else None
        data["lng"]=task["lng"] if exact and task["delivery_mode"]!="DIGITAL" else None
        data["customer_user_id"]=task["customer_id"] if user else None
        if exact:
            paid=conn.execute("SELECT status,transfer_status FROM payments WHERE task_id=?",(task["task_db_id"],)).fetchone()
            data["payment_status"]=paid["status"] if paid else "not_paid"
            data["payment_transfer_status"]=paid["transfer_status"] if paid else "not_started"
            assignment=conn.execute("SELECT agreed_amount_paise FROM task_assignments WHERE task_id=?",(task["task_db_id"],)).fetchone()
            data["agreed_amount_paise"]=assignment["agreed_amount_paise"] if assignment else None
            submission=conn.execute("SELECT original_name,preview_name,submitted_at FROM task_submissions WHERE task_id=?",(task["task_db_id"],)).fetchone()
            data["submission"]={"original_name":submission["original_name"],"preview_name":submission["preview_name"],"submitted_at":submission["submitted_at"],"preview_url":f"/api/tasks/{public_id}/submission/preview"} if submission else None
            data["download_unlocked"]=bool(submission and task["status"]=="PAYMENT_RELEASED" and paid and paid["status"] in ("transfer_created","released") and paid["transfer_status"] in ("settled","released"))
        can_see_photos=exact or bool(user and task["status"] in ("OPEN","OFFER_RECEIVED"))
        data["images"]=[{"id":r["id"],"url":f"/api/tasks/{public_id}/images/{r['id']}"} for r in conn.execute("SELECT id FROM task_images WHERE task_id=? ORDER BY created_at",(task["task_db_id"],))] if can_see_photos else []
        self.send_json(200,{"task":data})

    def api_write(self, conn: sqlite3.Connection, method: str, path: str, payload: dict[str,Any]) -> tuple[Any,int]:
        if method=="POST" and path=="/api/auth/start":
            phone=normalize_phone(payload.get("phone"))
            if not auth_provider_ready(): raise ApiError(503,"auth_not_configured","Phone verification is not configured yet. Add Twilio Verify credentials to the server environment.")
            ip=client_ip(self)
            request_rate(conn,"otp-ip:"+ip,12,600); request_rate(conn,"otp-phone:"+hashlib.sha256(phone.encode()).hexdigest(),4,600)
            twilio_request(phone)
            return {"sent":True,"message":"Code sent. It usually arrives within a minute.","phone":phone},200
        if method=="POST" and path=="/api/auth/verify":
            phone=normalize_phone(payload.get("phone")); code=re.sub(r"\s", "", str(payload.get("code", "")))
            if not re.fullmatch(r"\d{4,10}",code): raise ApiError(400,"invalid_code","Enter the verification code from your SMS.")
            if not auth_provider_ready(): raise ApiError(503,"auth_not_configured","Phone verification is not configured yet. Add Twilio Verify credentials to the server environment.")
            ip=client_ip(self)
            request_rate(conn,"verify-ip:"+ip,20,600); request_rate(conn,"verify-phone:"+hashlib.sha256(phone.encode()).hexdigest(),10,600)
            result=twilio_request(phone,code)
            if result.get("status")!="approved": raise ApiError(401,"invalid_code","That code doesn't match. Check it and try again.")
            user=conn.execute("SELECT * FROM users WHERE phone=?",(phone,)).fetchone()
            created=now_iso()
            if not user:
                user_id=str(uuid.uuid4())
                conn.execute("INSERT INTO users(id,phone,joined_at,updated_at) VALUES(?,?,?,?)",(user_id,phone,created,created))
                conn.execute("INSERT INTO user_verifications(user_id,kind,provider_ref,status,verified_at) VALUES(?,?,?,?,?)",(user_id,"phone",result.get("sid"),"verified",created))
                user=conn.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
            else:
                conn.execute("INSERT INTO user_verifications(user_id,kind,provider_ref,status,verified_at) VALUES(?,?,?,?,?)",(user["id"],"phone",result.get("sid"),"verified",created))
            bootstrap=normalize_phone(os.environ["ADMIN_BOOTSTRAP_PHONE"]) if os.environ.get("ADMIN_BOOTSTRAP_PHONE") else None
            if bootstrap and phone==bootstrap:
                conn.execute("INSERT OR IGNORE INTO admin_users(user_id,created_at) VALUES(?,?)",(user["id"],created))
            token=secrets.token_urlsafe(40)
            from datetime import timedelta
            expires=(datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds").replace("+00:00","Z")
            conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",(hash_token(token),user["id"],created,expires))
            return {"token":token,"expires_at":expires,"user":profile(conn,user["id"],user["id"]),"is_new":not bool(user["name"])},200
        if method=="POST" and path=="/api/webhooks/razorpay":
            self.razorpay_webhook(conn,payload)
            return {"received":True},200
        user=session_user(conn,self)
        uid=user["id"]
        if method=="POST" and path=="/api/auth/logout":
            conn.execute("DELETE FROM sessions WHERE token_hash=?",(hash_token(request_session_token(self)),))
            return {"ok":True},200
        if method=="PATCH" and path=="/api/me":
            name=str(payload.get("name",user["name"])).strip()[:80]
            about=str(payload.get("about",user["about"])).strip()[:500]
            email=str(payload.get("email",user["email"] or "")).strip().lower()[:160]
            city=str(payload.get("city",user["city"])).strip()[:80]
            area=str(payload.get("area",user["area"])).strip()[:100]
            languages=payload.get("languages",json.loads(user["languages"]))
            skills=payload.get("skills",json.loads(user["skills"]))
            is_student=payload.get("is_student",bool(user["is_student"])) in (True,1,"1","true","on")
            if not name: raise ApiError(400,"name_required","Add your name to finish setting up your profile.")
            if not city: raise ApiError(400,"city_required","Add your city so we can show tasks near you.")
            if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): raise ApiError(400,"invalid_email","Enter a valid email address.")
            if not isinstance(languages,list) or len(languages)>10 or any(not isinstance(x,str) or len(x)>40 for x in languages): raise ApiError(400,"invalid_languages","Choose up to 10 languages.")
            if not isinstance(skills,list) or len(skills)>20 or any(not isinstance(x,str) or len(x)>60 for x in skills): raise ApiError(400,"invalid_skills","Choose up to 20 skills.")
            radius=as_int(payload.get("service_radius_km",user["service_radius_km"]),10,2,50)
            if radius not in (2,5,10,25,50): radius=10
            lat,lng=user["lat"],user["lng"]
            if payload.get("lat") is not None and payload.get("lng") is not None: lat,lng=validate_coordinates(payload.get("lat"),payload.get("lng"))
            conn.execute("UPDATE users SET name=?,email=?,about=?,city=?,area=?,languages=?,skills=?,is_student=?,service_radius_km=?,lat=?,lng=?,updated_at=? WHERE id=?",(name,email or None,about,city,area,json.dumps(languages,ensure_ascii=False),json.dumps(skills,ensure_ascii=False),1 if is_student else 0,radius,lat,lng,now_iso(),uid))
            return {"user":profile(conn,uid,uid)},200
        if method=="POST" and path=="/api/me/photo":
            mime,data=clean_image(payload)
            conn.execute("UPDATE users SET photo=?,photo_mime=?,updated_at=? WHERE id=?",(data,mime,now_iso(),uid))
            return {"photo_url":f"/api/users/{uid}/photo"},200
        if method=="POST" and path=="/api/push/subscribe":
            if not push_ready(): raise ApiError(503,"push_not_configured","Push notifications aren't configured for this app yet.")
            sub=payload.get("subscription")
            if not isinstance(sub,dict) or not isinstance(sub.get("endpoint"),str) or not sub.get("keys",{}).get("p256dh") or not sub.get("keys",{}).get("auth"):
                raise ApiError(400,"invalid_subscription","The notification setup could not be completed. Please try again.")
            conn.execute("INSERT INTO push_subscriptions(user_id,endpoint,subscription_json,created_at) VALUES(?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,subscription_json=excluded.subscription_json",(uid,sub["endpoint"],json.dumps(sub),now_iso()))
            return {"enabled":True},200
        if method=="POST" and path=="/api/tasks":
            if get_settings(conn).get("platform_paused")=="true": raise ApiError(503,"platform_paused","New task posts are paused for a short update.")
            return self.create_task(conn,user,payload),201
        match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})/offers",path)
        if method=="POST" and match:
            return self.create_offer(conn,user,match.group(1),payload),201
        match=re.fullmatch(r"/api/offers/([0-9a-f-]{36})/select",path)
        if method=="POST" and match:
            return self.select_offer(conn,user,match.group(1)),200
        match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})/(status|cancel|confirm|rate|dispute|report)",path)
        if method=="POST" and match:
            tid,action=match.group(1),match.group(2)
            if action=="status": return self.change_status(conn,user,tid,payload),200
            if action=="cancel": return self.cancel_task(conn,user,tid,payload),200
            if action=="confirm": return self.confirm_task(conn,user,tid),200
            if action=="rate": return self.rate_task(conn,user,tid,payload),201
            if action=="dispute": return self.open_dispute(conn,user,tid,payload),201
            return self.report_task(conn,user,tid,payload),201
        match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})/submission",path)
        if method=="POST" and match:
            return self.submit_deliverable(conn,user,match.group(1),payload),201
        match=re.fullmatch(r"/api/tasks/([0-9a-f-]{36})/messages",path)
        if method=="POST" and match:
            return self.create_message(conn,user,match.group(1),payload),201
        match=re.fullmatch(r"/api/messages/(\d+)/image",path)
        if method=="GET" and match:
            return self.message_image(conn,user,int(match.group(1)))
        if method=="PATCH" and path=="/api/notifications/read":
            conn.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",(now_iso(),uid))
            return {"ok":True},200
        match=re.fullmatch(r"/api/users/([0-9a-f-]{36})/(block|report)",path)
        if method=="POST" and match:
            other,action=match.group(1),match.group(2)
            if other==uid: raise ApiError(400,"invalid_user","You can't do that to your own account.")
            if not conn.execute("SELECT 1 FROM users WHERE id=?",(other,)).fetchone(): raise ApiError(404,"user_not_found","This profile is no longer available.")
            if action=="block":
                conn.execute("INSERT OR IGNORE INTO user_blocks(blocker_id,blocked_id,created_at) VALUES(?,?,?)",(uid,other,now_iso()))
                return {"blocked":True},200
            reason=str(payload.get("reason","safety")).strip()[:100]; description=str(payload.get("description","")).strip()[:1000]
            conn.execute("INSERT INTO reports(reporter_id,subject_user_id,reason,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",(uid,other,reason or "safety",description,now_iso(),now_iso()))
            return {"reported":True},201
        if method=="POST" and path=="/api/payments/order":
            return self.create_payment_order(conn,user,payload),201
        if method=="POST" and path=="/api/payments/verify":
            return self.verify_payment(conn,user,payload),200
        if method=="POST" and path=="/api/admin/announcements":
            require_admin(conn,user)
            title=str(payload.get("title","")).strip()[:100]; body=str(payload.get("body","")).strip()[:1000]
            if not title or not body: raise ApiError(400,"announcement_required","Add a title and message.")
            conn.execute("INSERT INTO announcements(title,body,created_by,created_at) VALUES(?,?,?,?)",(title,body,uid,now_iso()))
            for u in conn.execute("SELECT id FROM users WHERE suspended=0"):
                notify(conn,u["id"],"announcement",title,body)
            return {"sent":True},201
        if path.startswith("/api/admin/"):
            return self.admin_write(conn,user,method,path,payload)
        raise ApiError(404,"not_found","This page couldn't be found.")

    def create_task(self,conn:sqlite3.Connection,user:sqlite3.Row,payload:dict[str,Any])->dict[str,Any]:
        title=str(payload.get("title","")).strip(); description=str(payload.get("description","")).strip()
        if len(title)<4 or len(title)>100: raise ApiError(400,"invalid_title","Use a task title between 4 and 100 characters.")
        if len(description)<15 or len(description)>2000: raise ApiError(400,"invalid_description","Add a little more detail (15–2,000 characters).")
        lower=(title+" "+description).lower()
        if any(term in lower for term in PROHIBITED_TERMS): raise ApiError(400,"prohibited_task","This task may not be safe or allowed. Please review the prohibited-task policy.")
        category=conn.execute("SELECT id,slug FROM categories WHERE slug=? AND active=1",(str(payload.get("category","")),)).fetchone()
        if not category: raise ApiError(400,"invalid_category","Choose one of the available task categories.")
        is_digital=category["slug"]=="student-study-support"
        if is_digital and not user["is_student"]: raise ApiError(403,"student_access_required","Turn on student access in your profile to post a study-support task.")
        ack=payload.get("integrity_acknowledged") in (True,1,"1","true","on")
        if is_digital and not ack: raise ApiError(400,"study_support_ack_required","Confirm this is for tutoring or study support, not work to submit as your own.")
        address=str(payload.get("address","")).strip()[:400]
        landmark=str(payload.get("landmark","")).strip()[:160]
        city=str(payload.get("city",user["city"])).strip()[:80]
        if is_digital:
            city=city or "Online"
            address="Online task"
            landmark=""
            if payload.get("lat") is not None and payload.get("lng") is not None:
                lat,lng=validate_coordinates(payload.get("lat"),payload.get("lng"))
            elif user["lat"] is not None and user["lng"] is not None:
                lat,lng=float(user["lat"]),float(user["lng"])
            else:
                lat,lng=22.5937,78.9629
        else:
            if not city: raise ApiError(400,"city_required","Add the task city so people nearby can find it.")
            if len(address)<4: raise ApiError(400,"address_required","Add the task address. Only the selected tasker will see it.")
            lat,lng=validate_coordinates(payload.get("lat"),payload.get("lng"))
        time_mode=str(payload.get("time_mode","ASAP"))
        if time_mode not in ("ASAP","TODAY","TOMORROW","SCHEDULED"): raise ApiError(400,"invalid_time","Choose when you'd like the task done.")
        scheduled=str(payload.get("scheduled_at","")).strip()[:40] or None
        if time_mode=="SCHEDULED":
            try:
                dt=datetime.fromisoformat(scheduled.replace("Z","+00:00")) if scheduled else None
                if not dt or dt.timestamp()<time.time()-60: raise ValueError()
            except ValueError: raise ApiError(400,"invalid_schedule","Choose a future date and time.")
        budget_mode=str(payload.get("budget_mode","FIXED"))
        if budget_mode not in ("FIXED","NEGOTIABLE"): raise ApiError(400,"invalid_budget","Choose a fixed or negotiable budget.")
        bmin=money_paise(payload.get("budget_min"),"minimum budget")
        bmax=money_paise(payload.get("budget_max",payload.get("budget_min")),"maximum budget")
        if bmax<bmin: raise ApiError(400,"invalid_budget","Your budget range is not valid.")
        if budget_mode=="FIXED" and bmax!=bmin: raise ApiError(400,"invalid_budget","A fixed-price task must use the same minimum and maximum budget.")
        images=payload.get("images",[])
        if not isinstance(images,list) or len(images)>MAX_IMAGES: raise ApiError(400,"too_many_images",f"Add up to {MAX_IMAGES} photos.")
        checked=[clean_image(img) for img in images]
        if sum(len(image[1]) for image in checked)>3*MAX_IMAGE: raise ApiError(413,"images_too_large","The photos are too large together. Please choose fewer or smaller images.")
        created=now_iso(); public_id=str(uuid.uuid4())
        cur=conn.execute("INSERT INTO tasks(public_id,customer_id,category_id,title,description,address,landmark,city,lat,lng,public_lat,public_lng,time_mode,scheduled_at,budget_mode,budget_min_paise,budget_max_paise,delivery_mode,integrity_acknowledged,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)",(public_id,user["id"],category["id"],title,description,address,landmark,city,lat,lng,round(lat,2),round(lng,2),time_mode,scheduled,budget_mode,bmin,bmax,"DIGITAL" if is_digital else "LOCAL",1 if ack else 0,created,created))
        task_db_id=cur.lastrowid
        cat_name=conn.execute("SELECT name FROM categories WHERE id=?",(category["id"],)).fetchone()[0]
        conn.execute("INSERT INTO task_search(rowid,title,description,category) VALUES(?,?,?,?)",(task_db_id,title,description,cat_name+" "+city))
        for mime,data in checked:
            conn.execute("INSERT INTO task_images(id,task_id,owner_id,mime,data,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),task_db_id,user["id"],mime,data,created))
        publish_to_nearby(conn,lat,lng,city,public_id,title,is_digital)
        return {"task_id":public_id,"status":"OPEN","message":"Your task is live. Nearby taskers can now send offers."}

    def create_offer(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if task["customer_id"]==user["id"]: raise ApiError(403,"own_task","You can't offer on your own task.")
        if task["status"] not in ("OPEN","OFFER_RECEIVED"): raise ApiError(409,"task_not_open","This task is no longer accepting offers.")
        if conn.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(user["id"],task["customer_id"],task["customer_id"],user["id"])).fetchone(): raise ApiError(403,"user_blocked","This task isn't available to your account.")
        existing=conn.execute("SELECT 1 FROM offers WHERE task_id=? AND tasker_id=?",(task["id"],user["id"])).fetchone()
        if existing: raise ApiError(409,"duplicate_offer","You've already sent an offer for this task.")
        amount=money_paise(payload.get("amount"),"offer")
        if amount>task["budget_max_paise"]*2: raise ApiError(400,"offer_out_of_range","That offer is far above the customer's listed budget.")
        message=str(payload.get("message","")).strip()[:500]
        if len(message)<8: raise ApiError(400,"offer_message_required","Add a short note so the customer knows how you can help.")
        eta=as_int(payload.get("eta_minutes"),60,1,10080)
        created=now_iso(); offer_id=str(uuid.uuid4())
        conn.execute("INSERT INTO offers(public_id,task_id,tasker_id,amount_paise,message,eta_minutes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'PENDING',?,?)",(offer_id,task["id"],user["id"],amount,message,eta,created,created))
        if task["status"]=="OPEN": conn.execute("UPDATE tasks SET status='OFFER_RECEIVED',updated_at=? WHERE id=?",(created,task["id"]))
        notify(conn,task["customer_id"],"offer_received","New offer received",f"{user['name'] or 'A nearby tasker'} sent an offer for {task['title']}.",public_id)
        return {"offer_id":offer_id,"message":"Offer sent. You can chat after the customer selects you."}

    def select_offer(self,conn:sqlite3.Connection,user:sqlite3.Row,offer_public_id:str)->dict[str,Any]:
        conn.execute("BEGIN IMMEDIATE")
        offer=conn.execute("SELECT o.*,t.public_id task_public_id,t.customer_id,t.status task_status,t.title FROM offers o JOIN tasks t ON t.id=o.task_id WHERE o.public_id=?",(offer_public_id,)).fetchone()
        if not offer: raise ApiError(404,"offer_not_found","This offer is no longer available.")
        if offer["customer_id"]!=user["id"]: raise ApiError(403,"offer_access_denied","Only the task owner can choose an offer.")
        if offer["task_status"] not in ("OPEN","OFFER_RECEIVED") or offer["status"]!="PENDING": raise ApiError(409,"offer_unavailable","This offer is no longer available.")
        if conn.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(user["id"],offer["tasker_id"],offer["tasker_id"],user["id"])).fetchone(): raise ApiError(403,"user_blocked","This tasker is blocked. Unblock them before selecting the offer.")
        created=now_iso()
        declined=conn.execute("SELECT tasker_id FROM offers WHERE task_id=? AND id!=? AND status='PENDING'",(offer["task_id"],offer["id"])).fetchall()
        conn.execute("UPDATE offers SET status=CASE WHEN id=? THEN 'ACCEPTED' ELSE 'DECLINED' END,updated_at=? WHERE task_id=?",(offer["id"],created,offer["task_id"]))
        changed=conn.execute("UPDATE tasks SET status='ASSIGNED',selected_offer_id=?,assigned_tasker_id=?,updated_at=? WHERE id=? AND status IN ('OPEN','OFFER_RECEIVED')",(offer["id"],offer["tasker_id"],created,offer["task_id"]))
        if changed.rowcount!=1: raise ApiError(409,"task_already_assigned","Another offer was selected first.")
        conn.execute("INSERT INTO task_assignments(task_id,offer_id,customer_id,tasker_id,agreed_amount_paise,assigned_at) VALUES(?,?,?,?,?,?)",(offer["task_id"],offer["id"],user["id"],offer["tasker_id"],offer["amount_paise"],created))
        conn.execute("INSERT INTO conversations(task_id,customer_id,tasker_id,created_at) VALUES(?,?,?,?)",(offer["task_id"],user["id"],offer["tasker_id"],created))
        notify(conn,offer["tasker_id"],"offer_accepted","Your offer was selected",f"You're assigned to {offer['title']}. Message the customer to coordinate.",offer["task_public_id"])
        for other in declined:
            notify(conn,other["tasker_id"],"offer_declined","Offer not selected",f"Another tasker was selected for {offer['title']}.",offer["task_public_id"])
        notify(conn,user["id"],"task_assigned","Tasker selected",f"{offer['title']} is assigned. You can now chat and pay securely.",offer["task_public_id"])
        return {"task_id":offer["task_public_id"],"status":"ASSIGNED","message":"Tasker selected. Chat is now open."}

    def change_status(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        new=str(payload.get("status",""))
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if task["assigned_tasker_id"]!=user["id"]: raise ApiError(403,"status_access_denied","Only the selected tasker can update this task.")
        if task["delivery_mode"]=="DIGITAL": raise ApiError(409,"submission_required","Submit the finished study file in the task instead of using in-person status updates.")
        transitions={"ASSIGNED":"TASKER_ON_THE_WAY","TASKER_ON_THE_WAY":"IN_PROGRESS","IN_PROGRESS":"COMPLETED_BY_TASKER"}
        if transitions.get(task["status"])!=new: raise ApiError(409,"invalid_transition","That task status can't be changed at this step.")
        payment=conn.execute("SELECT status FROM payments WHERE task_id=?",(task["id"],)).fetchone()
        if not payment or payment["status"]!="paid": raise ApiError(409,"payment_required","The customer needs to complete secure checkout before work starts.")
        created=now_iso()
        conn.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?",(new,created,task["id"]))
        if new=="COMPLETED_BY_TASKER": conn.execute("UPDATE task_assignments SET completed_at=? WHERE task_id=?",(created,task["id"]))
        msg={"TASKER_ON_THE_WAY":"Your tasker is on the way.","IN_PROGRESS":"Your task has started.","COMPLETED_BY_TASKER":"Your tasker marked the task complete. Review it to confirm."}[new]
        kind={"TASKER_ON_THE_WAY":"tasker_arriving","IN_PROGRESS":"task_started","COMPLETED_BY_TASKER":"task_completed"}[new]
        notify(conn,task["customer_id"],kind,msg,task["title"],public_id)
        return {"status":new,"message":"Task status updated."}

    def cancel_task(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if user["id"] not in (task["customer_id"],task["assigned_tasker_id"]): raise ApiError(403,"cancel_access_denied","Only the customer or selected tasker can cancel this task.")
        if task["status"] not in ("OPEN","OFFER_RECEIVED","ASSIGNED"): raise ApiError(409,"cancel_unavailable","This task can't be cancelled at this stage. Open a dispute if you need help.")
        payment=conn.execute("SELECT * FROM payments WHERE task_id=?",(task["id"],)).fetchone()
        if payment and payment["status"] in ("creating_order","order_pending"): raise ApiError(409,"checkout_in_progress","Close checkout and wait for its result before cancelling this task.")
        if payment and payment["status"] in ("refunding","refund_pending"): raise ApiError(409,"refund_in_progress","The refund is being processed. Please wait for an update before trying again.")
        if payment and payment["status"] in ("paid","transfer_created","released"):
            if task["status"]!="ASSIGNED" or payment["status"]!="paid": raise ApiError(409,"refund_review_required","A payment has been made after work began. Open a dispute so support can review it.")
            if not payment_provider_ready(): raise ApiError(503,"payments_unavailable","The refund provider isn't available. This task has not been cancelled.")
            reserved=conn.execute("UPDATE payments SET status='refunding',updated_at=? WHERE id=? AND status='paid'",(now_iso(),payment["id"]))
            if reserved.rowcount!=1: raise ApiError(409,"refund_in_progress","A refund is already being reviewed.")
            conn.commit()
            try:
                refund=razorpay_request(f"/payments/{urllib.parse.quote(payment['provider_payment_id'])}/refund",{"amount":payment["amount_paise"],"notes":{"task_id":public_id,"reason":"task_cancelled_before_start"}},"POST")
                refund_id=refund.get("id")
                if not refund_id: raise ApiError(502,"refund_failed","The refund wasn't accepted. The task has not been cancelled.")
                conn.execute("UPDATE payments SET status='refund_pending',provider_refund_id=?,updated_at=? WHERE id=?",(refund_id,now_iso(),payment["id"]))
            except ApiError as exc:
                if not exc.uncertain:
                    conn.execute("UPDATE payments SET status='paid',updated_at=? WHERE id=? AND status='refunding'",(now_iso(),payment["id"]))
                    conn.commit()
                raise
        created=now_iso(); reason=str(payload.get("reason","Cancelled by user")).strip()[:300]
        conn.execute("UPDATE tasks SET status='CANCELLED',cancelled_by=?,cancel_reason=?,updated_at=? WHERE id=?",(user["id"],reason,created,task["id"]))
        recipient=task["assigned_tasker_id"] if user["id"]==task["customer_id"] else task["customer_id"]
        if recipient: notify(conn,recipient,"task_cancelled","Task cancelled",f"{task['title']} was cancelled.",public_id)
        return {"status":"CANCELLED","message":"Task cancelled."}

    def create_payment_order(self,conn:sqlite3.Connection,user:sqlite3.Row,payload:dict[str,Any])->dict[str,Any]:
        public_id=str(payload.get("task_id",""))
        task=conn.execute("SELECT t.*,a.tasker_id,a.agreed_amount_paise FROM tasks t JOIN task_assignments a ON a.task_id=t.id WHERE t.public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This assigned task is no longer available.")
        if task["customer_id"]!=user["id"]: raise ApiError(403,"payment_access_denied","Only the customer can pay for this task.")
        if task["delivery_mode"]=="DIGITAL":
            if task["status"]!="COMPLETED_BY_TASKER" or not conn.execute("SELECT 1 FROM task_submissions WHERE task_id=?",(task["id"],)).fetchone():
                raise ApiError(409,"payment_unavailable","The tasker must submit the study file before checkout opens.")
        elif task["status"]!="ASSIGNED":
            raise ApiError(409,"payment_unavailable","Payment is available once a tasker is assigned.")
        if not payment_provider_ready(): raise ApiError(503,"payments_unavailable","Payments aren't configured yet. Your task has not been charged.")
        route=conn.execute("SELECT razorpay_linked_account_id FROM user_payout_profiles WHERE user_id=?",(task["tasker_id"],)).fetchone()
        if not route or not route["razorpay_linked_account_id"]: raise ApiError(409,"payout_setup_required","This tasker is not yet set up for secure payouts. Please contact support or choose another offer.")
        conn.execute("BEGIN IMMEDIATE")
        existing=conn.execute("SELECT * FROM payments WHERE task_id=?",(task["id"],)).fetchone()
        if existing and existing["provider_order_id"] and existing["status"] in ("order_pending","paid"):
            return {"order_id":existing["provider_order_id"],"amount":existing["amount_paise"],"currency":"INR","key_id":os.environ["RAZORPAY_KEY_ID"],"payment_id":existing["public_id"],"status":existing["status"]}
        if existing and existing["status"]=="creating_order": raise ApiError(409,"checkout_starting","Checkout is being prepared. Please wait a moment and reopen the task.")
        if existing and existing["status"] not in ("failed",): raise ApiError(409,"payment_exists","This task already has a payment record.")
        settings=get_settings(conn)
        promo_code=str(payload.get("promo_code","")).strip().upper()[:40]
        if promo_code:
            if not settings.get("promo_code") or promo_code!=settings.get("promo_code","").strip().upper() or not settings.get("promo_percent"):
                raise ApiError(400,"invalid_promo_code","That promo code isn't active.")
        is_digital=task["delivery_mode"]=="DIGITAL"
        fee,payout,charge_amount=calculate_payment_split(task["agreed_amount_paise"],settings.get("commission_percent","10"),settings.get("commission_min_paise","0"),settings.get("commission_max_paise","100000"),settings.get("promo_percent","") if promo_code else "",is_digital)
        public_payment=str(uuid.uuid4()); receipt="dd_"+uuid.uuid4().hex[:24]; created=now_iso()
        if existing:
            conn.execute("UPDATE payments SET public_id=?,amount_paise=?,fee_paise=?,payout_paise=?,promo_code=?,provider_order_id=NULL,provider_payment_id=NULL,provider_transfer_id=NULL,provider_refund_id=NULL,status='creating_order',transfer_status='not_started',created_at=?,paid_at=NULL,released_at=NULL,updated_at=? WHERE id=?",(public_payment,charge_amount,fee,payout,promo_code,created,created,existing["id"]))
        else:
            conn.execute("INSERT INTO payments(public_id,task_id,customer_id,tasker_id,amount_paise,fee_paise,payout_paise,promo_code,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?, 'creating_order',?,?)",(public_payment,task["id"],user["id"],task["tasker_id"],charge_amount,fee,payout,promo_code,created,created))
        payment_row=conn.execute("SELECT id FROM payments WHERE task_id=?",(task["id"],)).fetchone()
        conn.commit()
        try:
            order=razorpay_request("/orders",{"amount":charge_amount,"currency":"INR","receipt":receipt,"notes":{"task_id":public_id,"payment_id":public_payment,"app":APP_NAME}},"POST")
        except ApiError as exc:
            if not exc.uncertain:
                conn.execute("UPDATE payments SET status='failed',updated_at=? WHERE id=? AND status='creating_order'",(now_iso(),payment_row["id"]))
                conn.commit()
            raise
        if not order.get("id"): raise ApiError(502,"payment_order_failed","The provider response needs review before another checkout attempt.",uncertain=True)
        conn.execute("UPDATE payments SET provider_order_id=?,status='order_pending',updated_at=? WHERE id=? AND status='creating_order'",(order["id"],now_iso(),payment_row["id"]))
        return {"order_id":order["id"],"amount":charge_amount,"currency":"INR","key_id":os.environ["RAZORPAY_KEY_ID"],"payment_id":public_payment,"fee":fee,"payout":payout,"status":"order_pending"}

    def verify_payment(self,conn:sqlite3.Connection,user:sqlite3.Row,payload:dict[str,Any])->dict[str,Any]:
        order_id=str(payload.get("razorpay_order_id",""))[:100]
        payment_id=str(payload.get("razorpay_payment_id",""))[:100]
        signature=str(payload.get("razorpay_signature",""))[:200]
        if not order_id.startswith("order_") or not payment_id.startswith("pay_") or not re.fullmatch(r"[a-f0-9]{64}",signature): raise ApiError(400,"invalid_payment_response","We couldn't verify that payment. Check your payment status before trying again.")
        row=conn.execute("SELECT * FROM payments WHERE provider_order_id=? AND customer_id=?",(order_id,user["id"])).fetchone()
        if not row: raise ApiError(404,"payment_not_found","We couldn't find this payment for your account.")
        if row["status"] in ("paid","transfer_created","released") and row["provider_payment_id"]==payment_id: return {"status":row["status"],"message":"Payment already confirmed."}
        expected=hmac.new(os.environ.get("RAZORPAY_KEY_SECRET","").encode(),f"{order_id}|{payment_id}".encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature): raise ApiError(400,"payment_signature_invalid","The payment couldn't be verified. Your task has not been marked as paid.")
        remote=razorpay_request("/payments/"+urllib.parse.quote(payment_id))
        if remote.get("order_id")!=order_id or int(remote.get("amount",0))!=row["amount_paise"] or remote.get("currency")!="INR": raise ApiError(400,"payment_mismatch","The payment details didn't match this task. Please contact support.")
        if remote.get("status")=="authorized":
            remote=razorpay_request("/payments/"+urllib.parse.quote(payment_id)+"/capture",{"amount":row["amount_paise"],"currency":"INR"},"POST")
        if remote.get("status")!="captured": raise ApiError(402,"payment_not_captured","Payment couldn't be completed. Your task has not been charged.")
        paid=now_iso()
        conn.execute("UPDATE payments SET provider_payment_id=?,status='paid',paid_at=?,updated_at=? WHERE id=? AND status='order_pending'",(payment_id,paid,paid,row["id"]))
        task=conn.execute("SELECT public_id,title,assigned_tasker_id,delivery_mode FROM tasks WHERE id=?",(row["task_id"],)).fetchone()
        if task["delivery_mode"]=="DIGITAL":
            notify(conn,row["tasker_id"],"payment_successful","Student paid",f"Payment for your submitted file for {task['title']} is confirmed. Payout is processing.",task["public_id"])
            notify(conn,user["id"],"payment_successful","Payment successful",f"Payment for {task['title']} is confirmed. The original file unlocks after payout settlement.",task["public_id"])
        else:
            notify(conn,row["tasker_id"],"payment_successful","Customer paid",f"Payment is secured for {task['title']}. Start the task when you're ready.",task["public_id"])
            notify(conn,user["id"],"payment_successful","Payment successful",f"Payment for {task['title']} is confirmed.",task["public_id"])
        if task["delivery_mode"]=="DIGITAL":
            conn.commit()
            try:
                result=self.confirm_task(conn,user,task["public_id"])
                return {"status":result["status"],"message":result["message"]}
            except ApiError:
                # Checkout succeeded. Keep the original locked until Route accepts
                # the payout; the customer can safely retry payout without paying again.
                return {"status":"paid","payout_pending":True,"message":"Payment succeeded. The tasker's payout is still processing, so the original file remains locked for now."}
        return {"status":"paid","message":"Payment confirmed. You can follow the task as it progresses."}

    def confirm_task(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str)->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if task["customer_id"]!=user["id"]: raise ApiError(403,"confirm_access_denied","Only the customer can confirm task completion.")
        if task["status"] not in ("COMPLETED_BY_TASKER","CONFIRMED_BY_CUSTOMER"): raise ApiError(409,"task_not_ready","The tasker must mark the task complete before you confirm it.")
        payment=conn.execute("SELECT * FROM payments WHERE task_id=?",(task["id"],)).fetchone()
        if not payment or payment["status"]!="paid": raise ApiError(409,"payment_required","A completed payment is needed before the payout can be released.")
        if not payment_provider_ready(): raise ApiError(503,"payments_unavailable","Payouts aren't available right now. Your task has not been confirmed; try again later.")
        route=conn.execute("SELECT razorpay_linked_account_id FROM user_payout_profiles WHERE user_id=?",(task["assigned_tasker_id"],)).fetchone()
        if not route or not route["razorpay_linked_account_id"]: raise ApiError(409,"payout_setup_required","The tasker payout account needs review before this payment can be released.")
        if payment["transfer_status"] in ("initiating","created","processed","settled"):
            if task["status"]=="PAYMENT_RELEASED": return {"status":"PAYMENT_RELEASED","message":"Task complete. The payout is being processed."}
            raise ApiError(409,"payout_already_started","The payout is already being processed.")
        if task["status"]=="CONFIRMED_BY_CUSTOMER" and payment["transfer_status"]!="failed":
            raise ApiError(409,"payout_state_uncertain","Support needs to verify the provider status before another payout attempt.")
        # Reserve the release before the network call to prevent a double transfer.
        stamp=now_iso()
        conn.execute("BEGIN IMMEDIATE")
        reserved=conn.execute("UPDATE payments SET transfer_status='initiating',updated_at=? WHERE id=? AND transfer_status IN ('not_started','failed')",(stamp,payment["id"]))
        if reserved.rowcount!=1: raise ApiError(409,"payout_already_started","The payout is already being processed.")
        conn.execute("UPDATE tasks SET status='CONFIRMED_BY_CUSTOMER',updated_at=? WHERE id=?",(stamp,task["id"]))
        conn.execute("UPDATE task_assignments SET confirmed_at=COALESCE(confirmed_at,?) WHERE task_id=?",(stamp,task["id"]))
        conn.commit()
        try:
            transfer=razorpay_request(f"/payments/{urllib.parse.quote(payment['provider_payment_id'])}/transfers",[
                ("transfers[0][account]",route["razorpay_linked_account_id"]),
                ("transfers[0][amount]",str(payment["payout_paise"])),
                ("transfers[0][currency]","INR"),
                ("transfers[0][notes][task_id]",public_id),
            ],"POST")
            item=(transfer.get("items") or [transfer])[0]
            transfer_id=item.get("id")
            if not transfer_id: raise ApiError(502,"payout_failed","The provider response needs review before another payout attempt.",uncertain=True)
            transfer_state="failed" if item.get("status")=="failed" else item.get("settlement_status") or item.get("status","created")
            with db() as update:
                update.execute("BEGIN IMMEDIATE")
                observed=update.execute("SELECT transfer_status FROM payments WHERE id=?",(payment["id"],)).fetchone()
                if observed and observed["transfer_status"] in ("settled","released"): transfer_state=observed["transfer_status"]
                released=task["delivery_mode"]!="DIGITAL" or transfer_state in ("settled","released")
                final_status="PAYMENT_RELEASED" if released else "CONFIRMED_BY_CUSTOMER"
                payment_status="paid" if task["delivery_mode"]=="DIGITAL" and transfer_state=="failed" else "transfer_created"
                stamp=now_iso()
                update.execute("UPDATE payments SET provider_transfer_id=?,transfer_status=?,status=?,released_at=?,updated_at=? WHERE id=?",(transfer_id,transfer_state,payment_status,stamp,stamp,payment["id"]))
                update.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?",(final_status,stamp,task["id"]))
                update.execute("INSERT INTO payouts(payment_id,tasker_id,amount_paise,provider_transfer_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(payment_id) DO UPDATE SET provider_transfer_id=excluded.provider_transfer_id,status=excluded.status,updated_at=excluded.updated_at",(payment["id"],task["assigned_tasker_id"],payment["payout_paise"],transfer_id,transfer_state,stamp,stamp))
                if released:
                    notify(update,task["assigned_tasker_id"],"payment_released","Payout settled",f"Your payout for {task['title']} has settled.",public_id)
                    notify(update,user["id"],"payment_released","Task confirmed",f"The tasker's payout for {task['title']} has settled. The original file is unlocked.",public_id)
                elif transfer_state=="failed":
                    notify(update,task["assigned_tasker_id"],"payment_failed","Payout needs attention",f"The payout for {task['title']} did not go through. Support or retry can help.",public_id)
                    notify(update,user["id"],"payment_failed","Payout delayed",f"Your payment succeeded, but the payout for {task['title']} failed. The original file remains locked.",public_id)
                else:
                    notify(update,task["assigned_tasker_id"],"payment_released","Payout processing",f"Your payout for {task['title']} is processing.",public_id)
                    notify(update,user["id"],"payment_released","Task confirmed",f"The tasker's payout for {task['title']} is processing. The original file unlocks after settlement.",public_id)
            message="Task complete. The original file is unlocked." if released else "Payment confirmed. The tasker's payout is processing; the original file unlocks after settlement."
            if transfer_state=="failed": message="Payment succeeded, but payout failed. The original file remains locked while the payout is retried."
            return {"status":final_status,"message":message}
        except ApiError as exc:
            if not exc.uncertain:
                with db() as update:
                    update.execute("UPDATE payments SET transfer_status='failed',updated_at=? WHERE id=? AND transfer_status='initiating'",(now_iso(),payment["id"]))
            raise
        except Exception:
            # A lost response can follow a successful provider transfer. Leave the
            # reservation locked for provider reconciliation instead of risking a duplicate payout.
            raise

    def submit_deliverable(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if task["delivery_mode"]!="DIGITAL": raise ApiError(409,"submission_not_available","Only student study-support tasks accept a file submission.")
        if task["assigned_tasker_id"]!=user["id"]: raise ApiError(403,"submission_access_denied","Only the tasker selected by the student can submit this file.")
        if task["status"] not in ("ASSIGNED","COMPLETED_BY_TASKER"): raise ApiError(409,"submission_unavailable","Submit the file after the student accepts your offer.")
        payment=conn.execute("SELECT status FROM payments WHERE task_id=?",(task["id"],)).fetchone()
        if payment and payment["status"] not in ("failed",): raise ApiError(409,"submission_locked","A payment has already started for this submission. Contact support if you need to replace it.")
        original_name,original_mime,original_data=clean_upload(payload.get("original"))
        preview_name,preview_mime,preview_data=clean_upload(payload.get("preview"),pdf_only=True)
        if len(original_data)+len(preview_data)>12*1024*1024: raise ApiError(413,"submission_too_large","The original and preview together must be smaller than 12 MB.")
        stamp=now_iso(); submission_id=str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        current=conn.execute("SELECT tasker_id FROM task_submissions WHERE task_id=?",(task["id"],)).fetchone()
        if current and current["tasker_id"]!=user["id"]: raise ApiError(403,"submission_access_denied","Only the selected tasker can replace this submission.")
        conn.execute("INSERT INTO task_submissions(id,task_id,tasker_id,original_name,original_mime,original_data,preview_name,preview_mime,preview_data,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET id=excluded.id,original_name=excluded.original_name,original_mime=excluded.original_mime,original_data=excluded.original_data,preview_name=excluded.preview_name,preview_mime=excluded.preview_mime,preview_data=excluded.preview_data,submitted_at=excluded.submitted_at",(submission_id,task["id"],user["id"],original_name,original_mime,original_data,preview_name,preview_mime,preview_data,stamp))
        conn.execute("UPDATE tasks SET status='COMPLETED_BY_TASKER',updated_at=? WHERE id=?",(stamp,task["id"]))
        conn.execute("UPDATE task_assignments SET completed_at=? WHERE task_id=?",(stamp,task["id"]))
        notify(conn,task["customer_id"],"task_completed","Study file submitted",f"{user['name'] or 'Your tasker'} submitted a file for {task['title']}. Preview it in Doomsday.",public_id)
        return {"status":"COMPLETED_BY_TASKER","message":"Submission sent. The student can preview it in Doomsday."}

    def create_message(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if user["id"] not in (task["customer_id"],task["assigned_tasker_id"]): raise ApiError(403,"chat_access_denied","Chat opens after the customer selects a tasker.")
        convo=conn.execute("SELECT * FROM conversations WHERE task_id=?",(task["id"],)).fetchone()
        if not convo: raise ApiError(409,"chat_not_open","Chat opens after an offer is accepted.")
        other=convo["tasker_id"] if user["id"]==convo["customer_id"] else convo["customer_id"]
        if conn.execute("SELECT 1 FROM user_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(user["id"],other,other,user["id"])).fetchone(): raise ApiError(403,"chat_blocked","Messages are unavailable because one of you blocked the other.")
        body=str(payload.get("body","")).strip()[:2000]
        image=None; mime=None
        if payload.get("image"):
            mime,image=clean_image(payload["image"])
        attachment_name=None; attachment_mime=None; attachment_data=None
        if payload.get("attachment"):
            if image: raise ApiError(400,"too_many_attachments","Send one file at a time.")
            attachment_name,attachment_mime,attachment_data=clean_upload(payload["attachment"])
        if not body and not image and not attachment_data: raise ApiError(400,"empty_message","Type a message or attach a photo or document.")
        created=now_iso()
        cur=conn.execute("INSERT INTO messages(conversation_id,sender_id,body,image_mime,image_data,attachment_name,attachment_mime,attachment_data,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(convo["id"],user["id"],body,mime,image,attachment_name,attachment_mime,attachment_data,created))
        preview=body or (f"Sent {attachment_name}" if attachment_name else "Sent a photo")
        notify(conn,other,"new_message","New message",f"{user['name'] or 'Your task partner'}: {preview[:120]}",public_id)
        return {"id":cur.lastrowid,"created_at":created,"message":"Message sent."}

    def message_image(self,conn:sqlite3.Connection,user:sqlite3.Row,message_id:int)->tuple[dict[str,Any],int]:
        row=conn.execute("SELECT m.image_data,m.image_mime,c.customer_id,c.tasker_id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",(message_id,)).fetchone()
        if not row or user["id"] not in (row["customer_id"],row["tasker_id"]): raise ApiError(404,"image_not_found","This message photo isn't available.")
        self.send_blob(row["image_data"],row["image_mime"])
        return {},200

    def message_attachment(self,conn:sqlite3.Connection,user:sqlite3.Row,message_id:int)->tuple[dict[str,Any],int]:
        row=conn.execute("SELECT m.attachment_data,m.attachment_mime,m.attachment_name,m.sender_id,c.customer_id,c.tasker_id,t.id task_db_id,t.public_id,t.delivery_mode,t.status FROM messages m JOIN conversations c ON c.id=m.conversation_id JOIN tasks t ON t.id=c.task_id WHERE m.id=?",(message_id,)).fetchone()
        if not row or user["id"] not in (row["customer_id"],row["tasker_id"]) or not row["attachment_data"]:
            raise ApiError(404,"attachment_not_found","This chat file isn't available.")
        if row["delivery_mode"]=="DIGITAL" and user["id"]==row["customer_id"] and row["sender_id"]==row["tasker_id"] and not row["attachment_mime"].startswith("image/"):
            payment=conn.execute("SELECT status,transfer_status FROM payments WHERE task_id=?",(row["task_db_id"],)).fetchone()
            if row["status"]!="PAYMENT_RELEASED" or not payment or payment["status"] not in ("transfer_created","released") or payment["transfer_status"] not in ("settled","released"):
                raise ApiError(402,"attachment_locked","The tasker's document unlocks after the payout settles.")
        disposition="inline" if row["attachment_mime"].startswith("image/") else "attachment"
        self.send_private_file(row["attachment_data"],row["attachment_mime"],row["attachment_name"],disposition)
        return {},200

    def rate_task(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        if task["status"]!="PAYMENT_RELEASED": raise ApiError(409,"rating_unavailable","Ratings open after the task and payout are complete.")
        if user["id"] not in (task["customer_id"],task["assigned_tasker_id"]): raise ApiError(403,"rating_access_denied","Only the customer and selected tasker can rate this task.")
        if conn.execute("SELECT 1 FROM ratings WHERE task_id=? AND from_user_id=?",(task["id"],user["id"])).fetchone(): raise ApiError(409,"duplicate_review","You've already reviewed this task.")
        other=task["assigned_tasker_id"] if user["id"]==task["customer_id"] else task["customer_id"]
        try: score=int(payload.get("score",0))
        except (TypeError,ValueError): score=0
        if score<1 or score>5: raise ApiError(400,"invalid_rating","Choose a rating from 1 to 5 stars.")
        review=str(payload.get("review","")).strip()[:1000]
        conn.execute("INSERT INTO ratings(task_id,from_user_id,to_user_id,score,review,created_at) VALUES(?,?,?,?,?,?)",(task["id"],user["id"],other,score,review,now_iso()))
        notify(conn,other,"new_review","New review",f"{user['name'] or 'Your task partner'} left you a review.",public_id)
        return {"saved":True,"message":"Thanks. Your review has been saved."}

    def open_dispute(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        require_task_participant(task,user["id"])
        if task["status"] in ("CANCELLED","PAYMENT_RELEASED"): raise ApiError(409,"dispute_unavailable","A dispute can't be opened for this task now. Contact support if you still need help.")
        reason=str(payload.get("reason","other")).strip()[:100]
        description=str(payload.get("description","")).strip()[:2000]
        if len(description)<10: raise ApiError(400,"dispute_details_required","Describe what happened (at least 10 characters).")
        evidence=payload.get("evidence",[])
        if not isinstance(evidence,list) or len(evidence)>5: raise ApiError(400,"invalid_evidence","Attach up to 5 evidence photos.")
        checked=[clean_image(v) for v in evidence]
        created=now_iso(); public_dispute=str(uuid.uuid4())
        cur=conn.execute("INSERT INTO disputes(public_id,task_id,opened_by,reason,description,status,created_at,updated_at) VALUES(?,?,?,?,?,'OPEN',?,?)",(public_dispute,task["id"],user["id"],reason,description,created,created))
        for mime,data in checked:
            conn.execute("INSERT INTO dispute_evidence(id,dispute_id,owner_id,mime,data,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),cur.lastrowid,user["id"],mime,data,created))
        conn.execute("UPDATE tasks SET status='DISPUTED',updated_at=? WHERE id=?",(created,task["id"]))
        other=task["assigned_tasker_id"] if user["id"]==task["customer_id"] else task["customer_id"]
        if other: notify(conn,other,"dispute_opened","A dispute was opened",f"A dispute has been opened for {task['title']}. Support will review it.",public_id)
        return {"dispute_id":public_dispute,"status":"OPEN","message":"Dispute sent. Support will review the task."}

    def report_task(self,conn:sqlite3.Connection,user:sqlite3.Row,public_id:str,payload:dict[str,Any])->dict[str,Any]:
        task=conn.execute("SELECT id FROM tasks WHERE public_id=?",(public_id,)).fetchone()
        if not task: raise ApiError(404,"task_not_found","This task is no longer available.")
        reason=str(payload.get("reason","safety")).strip()[:100]; description=str(payload.get("description","")).strip()[:1000]
        conn.execute("INSERT INTO reports(reporter_id,task_id,reason,description,created_at,updated_at) VALUES(?,?,?,?,?,?)",(user["id"],task["id"],reason or "safety",description,now_iso(),now_iso()))
        return {"reported":True,"message":"Thanks for letting us know. Our team will review this task."}

    def admin_overview(self,conn:sqlite3.Connection)->None:
        def count(sql:str)->int: return conn.execute(sql).fetchone()[0]
        settings=get_settings(conn)
        total_value=conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE status IN ('paid','transfer_created','released')").fetchone()[0]
        revenue=conn.execute("SELECT COALESCE(SUM(fee_paise),0) FROM payments WHERE status IN ('paid','transfer_created','released')").fetchone()[0]
        daily=conn.execute("SELECT substr(joined_at,1,10) day,COUNT(*) users FROM users WHERE joined_at >= datetime('now','-30 day') GROUP BY day ORDER BY day").fetchall()
        recent=conn.execute("SELECT substr(created_at,1,10) day,COUNT(*) tasks FROM tasks WHERE created_at >= datetime('now','-30 day') GROUP BY day ORDER BY day").fetchall()
        self.send_json(200,{"stats":{"total_users":count("SELECT COUNT(*) FROM users"),"active_users":count("SELECT COUNT(*) FROM users WHERE suspended=0"),"tasks_posted":count("SELECT COUNT(*) FROM tasks"),"tasks_completed":count("SELECT COUNT(*) FROM tasks WHERE status='PAYMENT_RELEASED'"),"tasks_cancelled":count("SELECT COUNT(*) FROM tasks WHERE status='CANCELLED'"),"transaction_value_paise":total_value,"platform_revenue_paise":revenue,"active_disputes":count("SELECT COUNT(*) FROM disputes WHERE status IN ('OPEN','UNDER_REVIEW')"),"new_users_30d":count("SELECT COUNT(*) FROM users WHERE joined_at >= datetime('now','-30 day')")},"users_growth":safe_json(daily),"tasks_growth":safe_json(recent),"settings":settings})

    def admin_write(self,conn:sqlite3.Connection,user:sqlite3.Row,method:str,path:str,payload:dict[str,Any])->tuple[Any,int]:
        require_admin(conn,user)
        match=re.fullmatch(r"/api/admin/users/([0-9a-f-]{36})/(suspend|verify|payout-account)",path)
        if method=="POST" and match:
            target,action=match.group(1),match.group(2)
            row=conn.execute("SELECT id FROM users WHERE id=?",(target,)).fetchone()
            if not row: raise ApiError(404,"user_not_found","This account could not be found.")
            if action=="suspend":
                suspended=bool(payload.get("suspended",True))
                conn.execute("UPDATE users SET suspended=?,updated_at=? WHERE id=?",(1 if suspended else 0,now_iso(),target))
                if suspended: conn.execute("DELETE FROM sessions WHERE user_id=?",(target,))
                return {"suspended":suspended},200
            if action=="verify":
                conn.execute("INSERT INTO user_verifications(user_id,kind,status,verified_at) VALUES(?,?,?,?)",(target,"identity","verified",now_iso()))
                return {"verified":True},200
            account=str(payload.get("linked_account_id","")).strip()
            if not re.fullmatch(r"acc_[A-Za-z0-9]+",account): raise ApiError(400,"invalid_linked_account","Enter the Razorpay Route linked account ID after identity and payout checks are complete.")
            conn.execute("INSERT INTO user_payout_profiles(user_id,razorpay_linked_account_id,verified_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET razorpay_linked_account_id=excluded.razorpay_linked_account_id,verified_by=excluded.verified_by,updated_at=excluded.updated_at",(target,account,user["id"],now_iso()))
            return {"payout_connected":True},200
        match=re.fullmatch(r"/api/admin/tasks/([0-9a-f-]{36})/(remove|restore)",path)
        if method=="POST" and match:
            task=conn.execute("SELECT * FROM tasks WHERE public_id=?",(match.group(1),)).fetchone()
            if not task: raise ApiError(404,"task_not_found","This task could not be found.")
            action=match.group(2)
            if action=="remove":
                if task["status"] not in ("OPEN","OFFER_RECEIVED"): raise ApiError(409,"task_removal_blocked","Assigned or paid tasks must be handled through cancellation or a dispute review.")
                conn.execute("UPDATE tasks SET status='CANCELLED',cancel_reason='Removed by moderation',updated_at=? WHERE id=?",(now_iso(),task["id"]))
                notify(conn,task["customer_id"],"task_removed","Task removed","Support removed this task after review.",task["public_id"])
            elif task["status"]=="CANCELLED" and task["cancel_reason"]=="Removed by moderation" and not task["assigned_tasker_id"] and not conn.execute("SELECT 1 FROM payments WHERE task_id=?",(task["id"],)).fetchone():
                conn.execute("UPDATE tasks SET status='OPEN',cancel_reason=NULL,updated_at=? WHERE id=?",(now_iso(),task["id"]))
            else: raise ApiError(409,"task_restore_blocked","Only an unassigned task removed by moderation can be restored.")
            return {"status":"CANCELLED" if action=="remove" else "OPEN"},200
        match=re.fullmatch(r"/api/admin/disputes/([0-9a-f-]{36})",path)
        if method=="PATCH" and match:
            status=str(payload.get("status","")); resolution=str(payload.get("resolution","")).strip()[:2000]
            if status not in ("UNDER_REVIEW","RESOLVED","REJECTED"): raise ApiError(400,"invalid_dispute_status","Choose a valid dispute status.")
            dispute=conn.execute("SELECT d.*,t.public_id task_public_id,t.title,t.customer_id,t.assigned_tasker_id,t.status task_status FROM disputes d JOIN tasks t ON t.id=d.task_id WHERE d.public_id=?",(match.group(1),)).fetchone()
            if not dispute: raise ApiError(404,"dispute_not_found","This dispute could not be found.")
            if status in ("RESOLVED","REJECTED") and len(resolution)<5: raise ApiError(400,"resolution_required","Add a short note explaining the decision.")
            action=str(payload.get("outcome","")) if status in ("RESOLVED","REJECTED") else ""
            payment=conn.execute("SELECT * FROM payments WHERE task_id=(SELECT task_id FROM disputes WHERE id=?)",(dispute["id"],)).fetchone()
            if status in ("RESOLVED","REJECTED") and action not in ("release","refund","cancel"):
                raise ApiError(400,"resolution_action_required","Choose whether to release the payout, refund the customer, or cancel an unpaid task.")
            if action=="release":
                if not payment or payment["status"]!="paid" or not payment["provider_payment_id"]: raise ApiError(409,"payment_not_releasable","There isn't a paid task amount to release.")
                if not payment_provider_ready(): raise ApiError(503,"payments_unavailable","Route payouts aren't available. The dispute decision was not saved.")
                route=conn.execute("SELECT razorpay_linked_account_id FROM user_payout_profiles WHERE user_id=?",(payment["tasker_id"],)).fetchone()
                if not route or not route["razorpay_linked_account_id"]: raise ApiError(409,"payout_setup_required","The tasker payout account needs review before the payout can be released.")
                reserved=conn.execute("UPDATE payments SET transfer_status='initiating',updated_at=? WHERE id=? AND status='paid' AND transfer_status IN ('not_started','failed')",(now_iso(),payment["id"]))
                if reserved.rowcount!=1: raise ApiError(409,"payout_already_started","This payout has already started.")
                conn.commit()
                try:
                    transfer=razorpay_request(f"/payments/{urllib.parse.quote(payment['provider_payment_id'])}/transfers",[("transfers[0][account]",route["razorpay_linked_account_id"]),("transfers[0][amount]",str(payment["payout_paise"])),("transfers[0][currency]","INR"),("transfers[0][notes][task_id]",dispute["task_public_id"])],"POST")
                except ApiError as exc:
                    if not exc.uncertain:
                        conn.execute("UPDATE payments SET transfer_status='failed',updated_at=? WHERE id=? AND transfer_status='initiating'",(now_iso(),payment["id"]))
                        conn.commit()
                    raise
                item=(transfer.get("items") or [transfer])[0]; transfer_id=item.get("id")
                if not transfer_id:
                    raise ApiError(502,"payout_failed","The provider response needs review before another payout attempt.",uncertain=True)
                stamp=now_iso()
                conn.execute("UPDATE payments SET provider_transfer_id=?,transfer_status=?,status='transfer_created',released_at=?,updated_at=? WHERE id=?",(transfer_id,item.get("status","created"),stamp,stamp,payment["id"]))
                conn.execute("UPDATE tasks SET status='PAYMENT_RELEASED',updated_at=? WHERE id=?",(stamp,dispute["task_id"]))
                conn.execute("UPDATE task_assignments SET confirmed_at=COALESCE(confirmed_at,?) WHERE task_id=?",(stamp,dispute["task_id"]))
                conn.execute("INSERT INTO payouts(payment_id,tasker_id,amount_paise,provider_transfer_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(payment_id) DO UPDATE SET provider_transfer_id=excluded.provider_transfer_id,status=excluded.status,updated_at=excluded.updated_at",(payment["id"],payment["tasker_id"],payment["payout_paise"],transfer_id,item.get("status","created"),stamp,stamp))
            elif action=="refund":
                if not payment or payment["status"]!="paid" or not payment["provider_payment_id"]: raise ApiError(409,"payment_not_refundable","There isn't a paid task amount to refund.")
                if not payment_provider_ready(): raise ApiError(503,"payments_unavailable","Refunds aren't available. The dispute decision was not saved.")
                reserved=conn.execute("UPDATE payments SET status='refunding',updated_at=? WHERE id=? AND status='paid'",(now_iso(),payment["id"]))
                if reserved.rowcount!=1: raise ApiError(409,"refund_in_progress","A refund is already being processed.")
                conn.commit()
                try:
                    refund=razorpay_request(f"/payments/{urllib.parse.quote(payment['provider_payment_id'])}/refund",{"amount":payment["amount_paise"],"notes":{"task_id":dispute["task_public_id"],"reason":"dispute_resolution"}},"POST")
                except ApiError as exc:
                    if not exc.uncertain:
                        conn.execute("UPDATE payments SET status='paid',updated_at=? WHERE id=? AND status='refunding'",(now_iso(),payment["id"]))
                        conn.commit()
                    raise
                if not refund.get("id"):
                    raise ApiError(502,"refund_failed","The provider response needs review before another refund attempt.",uncertain=True)
                conn.execute("UPDATE payments SET status='refund_pending',provider_refund_id=?,updated_at=? WHERE id=?",(refund["id"],now_iso(),payment["id"]))
                conn.execute("UPDATE tasks SET status='CANCELLED',cancel_reason='Cancelled after dispute review',updated_at=? WHERE id=?",(now_iso(),dispute["task_id"]))
            elif action=="cancel":
                if payment and payment["status"] in ("paid","transfer_created","released","refunding","refund_pending"):
                    raise ApiError(409,"payment_action_required","This paid task needs a refund or payout outcome, not a plain cancellation.")
                conn.execute("UPDATE tasks SET status='CANCELLED',cancel_reason='Cancelled after dispute review',updated_at=? WHERE id=?",(now_iso(),dispute["task_id"]))
            stamp=now_iso()
            conn.execute("UPDATE disputes SET status=?,resolution=?,resolution_action=?,resolved_by=?,updated_at=? WHERE id=?",(status,resolution,action,user["id"] if status in ("RESOLVED","REJECTED") else None,stamp,dispute["id"]))
            if status in ("RESOLVED","REJECTED"):
                for target in (dispute["customer_id"],dispute["assigned_tasker_id"]):
                    if target: notify(conn,target,"dispute_update","Dispute updated",resolution,dispute["task_public_id"])
            return {"status":status,"message":"Dispute updated."},200
        if method=="PATCH" and path=="/api/admin/settings":
            editable={"commission_percent","commission_min_paise","commission_max_paise","promo_percent","promo_code","platform_paused"}
            for key,value in payload.items():
                if key not in editable: continue
                if key in ("commission_percent","promo_percent"):
                    if key=="promo_percent" and str(value).strip()=="": value=""
                    else:
                        try: number=Decimal(str(value)); assert Decimal(0)<=number<=Decimal(50)
                        except Exception: raise ApiError(400,"invalid_commission","Commission must be between 0% and 50%.")
                        value=str(number)
                elif key in ("commission_min_paise","commission_max_paise"):
                    try: number=int(value); assert 0<=number<=10000000
                    except Exception: raise ApiError(400,"invalid_commission","Enter a valid commission limit.")
                    value=str(number)
                elif key=="platform_paused": value="true" if value else "false"
                else: value=str(value).strip()[:120]
                conn.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,value,now_iso()))
            return {"settings":get_settings(conn)},200
        if method=="POST" and path=="/api/admin/categories":
            slug=re.sub(r"[^a-z0-9-]+","-",str(payload.get("slug","")).lower()).strip("-")[:60]
            name=str(payload.get("name","")).strip()[:80]; name_ta=str(payload.get("name_ta",name)).strip()[:80]; icon=str(payload.get("icon","✨")).strip()[:8]
            if not slug or not name: raise ApiError(400,"category_required","Add a category name and short ID.")
            conn.execute("INSERT INTO categories(slug,name,name_ta,icon,active,sort_order) VALUES(?,?,?,?,1,99)",(slug,name,name_ta or name,icon or "✨"))
            return {"created":True},201
        match=re.fullmatch(r"/api/admin/categories/(\d+)",path)
        if method=="PATCH" and match:
            category_id=int(match.group(1)); row=conn.execute("SELECT id FROM categories WHERE id=?",(category_id,)).fetchone()
            if not row: raise ApiError(404,"category_not_found","This category could not be found.")
            if "active" in payload: conn.execute("UPDATE categories SET active=? WHERE id=?",(1 if payload["active"] else 0,category_id))
            if "name" in payload: conn.execute("UPDATE categories SET name=? WHERE id=?",(str(payload["name"]).strip()[:80],category_id))
            if "name_ta" in payload: conn.execute("UPDATE categories SET name_ta=? WHERE id=?",(str(payload["name_ta"]).strip()[:80],category_id))
            if "icon" in payload: conn.execute("UPDATE categories SET icon=? WHERE id=?",(str(payload["icon"]).strip()[:8],category_id))
            return {"updated":True},200
        if method=="PATCH" and path.startswith("/api/admin/reports/"):
            try: report_id=int(path.rsplit("/",1)[1])
            except ValueError: raise ApiError(404,"report_not_found","This report could not be found.")
            status=str(payload.get("status",""))
            if status not in ("UNDER_REVIEW","RESOLVED","REJECTED"): raise ApiError(400,"invalid_report_status","Choose a valid report status.")
            conn.execute("UPDATE reports SET status=?,updated_at=? WHERE id=?",(status,now_iso(),report_id))
            return {"status":status},200
        raise ApiError(404,"not_found","This admin action couldn't be found.")

    def razorpay_webhook(self,conn:sqlite3.Connection,payload:dict[str,Any])->None:
        # Read exact raw bytes; the caller re-reads raw JSON in dispatch handler below.
        signature=self.headers.get("X-Razorpay-Signature","")
        event_id=self.headers.get("X-Razorpay-Event-Id","")[:100]
        raw=getattr(self,"_raw_body",b"")
        secret=os.environ.get("RAZORPAY_WEBHOOK_SECRET","")
        expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        if not secret or not signature or not hmac.compare_digest(expected,signature): raise ApiError(401,"webhook_signature_invalid","Webhook signature could not be verified.")
        if not event_id: event_id=hashlib.sha256(raw).hexdigest()
        try: conn.execute("INSERT INTO webhook_events(event_id,event_type,received_at) VALUES(?,?,?)",(event_id,str(payload.get("event","unknown")),now_iso()))
        except sqlite3.IntegrityError: return
        event=payload.get("event"); data=payload.get("payload",{})
        if event=="payment.captured":
            entity=data.get("payment",{}).get("entity") or {}
            payment_id=entity.get("id"); order_id=entity.get("order_id")
            if payment_id and order_id:
                payment=conn.execute("SELECT * FROM payments WHERE provider_order_id=?",(order_id,)).fetchone()
                if payment and int(entity.get("amount",payment["amount_paise"]))==payment["amount_paise"]:
                    task=conn.execute("SELECT status FROM tasks WHERE id=?",(payment["task_id"],)).fetchone()
                    if task and task["status"]=="CANCELLED":
                        conn.execute("UPDATE payments SET provider_payment_id=?,status='refund_required',paid_at=COALESCE(paid_at,?),updated_at=? WHERE id=?",(payment_id,now_iso(),now_iso(),payment["id"]))
                    else:
                        conn.execute("UPDATE payments SET provider_payment_id=?,status='paid',paid_at=COALESCE(paid_at,?),updated_at=? WHERE id=?",(payment_id,now_iso(),now_iso(),payment["id"]))
        elif event in ("transfer.created","transfer.processed","transfer.settled","transfer.failed","transfer.reversed"):
            entity=data.get("transfer",{}).get("entity") or {}
            transfer_id=entity.get("id")
            if transfer_id:
                status=entity.get("status",event.split(".",1)[1])
                settlement="failed" if event=="transfer.failed" or status=="failed" else entity.get("settlement_status") or ("settled" if event=="transfer.settled" else status)
                payment=conn.execute("SELECT * FROM payments WHERE provider_payment_id=? OR provider_transfer_id=?",(entity.get("source"),transfer_id)).fetchone()
                if payment:
                    task=conn.execute("SELECT id,public_id,title,delivery_mode,status,customer_id,assigned_tasker_id FROM tasks WHERE id=?",(payment["task_id"],)).fetchone()
                    payment_status="paid" if settlement=="failed" and task and task["delivery_mode"]=="DIGITAL" else "transfer_created"
                    conn.execute("UPDATE payments SET provider_transfer_id=?,transfer_status=?,status=?,updated_at=? WHERE id=?",(transfer_id,settlement,payment_status,now_iso(),payment["id"]))
                    conn.execute("UPDATE payouts SET status=?,updated_at=? WHERE payment_id=?",(settlement,now_iso(),payment["id"]))
                    if settlement in ("settled","released") and task and task["delivery_mode"]=="DIGITAL" and task["status"]=="CONFIRMED_BY_CUSTOMER":
                        conn.execute("UPDATE tasks SET status='PAYMENT_RELEASED',updated_at=? WHERE id=?",(now_iso(),task["id"]))
                        notify(conn,task["assigned_tasker_id"],"payment_released","Payout settled",f"Your payout for {task['title']} has settled.",task["public_id"])
                        notify(conn,task["customer_id"],"payment_released","Original file unlocked",f"The tasker's payout for {task['title']} has settled. Download the original file in Doomsday.",task["public_id"])
                    elif settlement=="failed" and task and task["delivery_mode"]=="DIGITAL" and task["status"]=="CONFIRMED_BY_CUSTOMER":
                        notify(conn,task["assigned_tasker_id"],"payment_failed","Payout needs attention",f"The payout for {task['title']} did not go through. Retry or contact support.",task["public_id"])
                        notify(conn,task["customer_id"],"payment_failed","Payout delayed",f"The payout for {task['title']} failed. Your original file remains locked while support reviews it.",task["public_id"])
        elif event=="payment.failed":
            entity=data.get("payment",{}).get("entity") or {}
            if entity.get("order_id"):
                conn.execute("UPDATE payments SET status='failed',updated_at=? WHERE provider_order_id=? AND status='order_pending'",(now_iso(),entity["order_id"]))
        elif event in ("refund.processed","refund.failed"):
            entity=data.get("refund",{}).get("entity") or {}
            if entity.get("payment_id"):
                payment=conn.execute("SELECT * FROM payments WHERE provider_payment_id=?",(entity["payment_id"],)).fetchone()
                if payment:
                    status="refunded" if event=="refund.processed" else "refund_failed"
                    conn.execute("UPDATE payments SET provider_refund_id=COALESCE(provider_refund_id,?),status=?,updated_at=? WHERE id=?",(entity.get("id"),status,now_iso(),payment["id"]))


def main() -> None:
    init_db()
    print(f"{APP_NAME} is running at http://{HOST}:{PORT}")
    if not auth_provider_ready(): print("Phone sign-in is disabled until Twilio Verify is configured.")
    if not payment_provider_ready(): print("Payments are disabled until Razorpay credentials and Route are configured.")
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    server.daemon_threads=True
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopping Doomsday.")
    finally: server.server_close()


if __name__=="__main__":
    main()
