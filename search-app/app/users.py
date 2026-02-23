from __future__ import annotations

import logging
from typing import Optional, List

from passlib.context import CryptContext

from .db import get_conn

logger = logging.getLogger(__name__)

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_ctx.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_ctx.verify(password, password_hash)
    except Exception:
        return False


def get_user_by_email(email: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, password_hash, created_at, last_login_at FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": int(row[0]),
                "email": row[1],
                "password_hash": row[2],
                "created_at": (row[3].isoformat() if row[3] else None),
                "last_login_at": (row[4].isoformat() if row[4] else None),
            }


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email, password_hash, created_at, last_login_at FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": int(row[0]),
                "email": row[1],
                "password_hash": row[2],
                "created_at": (row[3].isoformat() if row[3] else None),
                "last_login_at": (row[4].isoformat() if row[4] else None),
            }


def create_user(email: str, password: str) -> dict:
    ph = hash_password(password)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (email, ph),
            )
            uid = int(cur.fetchone()[0])
    ensure_default_space(uid)
    return {"id": uid, "email": email}


def authenticate_user(email: str, password: str) -> Optional[dict]:
    u = get_user_by_email(email)
    if not u:
        return None
    if not verify_password(password, u.get("password_hash") or ""):
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (u["id"],))
    return {"id": u["id"], "email": u["email"]}


def ensure_default_space(user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM spaces WHERE user_id = %s AND is_default = TRUE", (user_id,))
            row = cur.fetchone()
            if row:
                return int(row[0])
            cur.execute(
                "INSERT INTO spaces (user_id, name, is_default) VALUES (%s, %s, TRUE) RETURNING id",
                (user_id, "My Space"),
            )
            return int(cur.fetchone()[0])


def create_space(user_id: int, name: str, is_default: bool = False) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO spaces (user_id, name, is_default) VALUES (%s, %s, %s) RETURNING id",
                (user_id, name, is_default),
            )
            sid = int(cur.fetchone()[0])
            if is_default:
                cur.execute("UPDATE spaces SET is_default = FALSE WHERE user_id = %s AND id <> %s", (user_id, sid))
            return sid


def list_spaces(user_id: int) -> List[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, is_default, created_at FROM spaces WHERE user_id = %s ORDER BY is_default DESC, name ASC",
                (user_id,),
            )
            rows = cur.fetchall()
            return [
                {"id": int(r[0]), "name": r[1], "is_default": bool(r[2]), "created_at": (r[3].isoformat() if r[3] else None)} for r in rows
            ]


def get_default_space_id(user_id: int) -> Optional[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM spaces WHERE user_id = %s AND is_default = TRUE", (user_id,))
            row = cur.fetchone()
            return int(row[0]) if row else None


def set_default_space(user_id: int, space_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE spaces SET is_default = FALSE WHERE user_id = %s", (user_id,))
            cur.execute("UPDATE spaces SET is_default = TRUE WHERE user_id = %s AND id = %s", (user_id, space_id))