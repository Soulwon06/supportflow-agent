"""Small local-account authentication layer for the internal demo deployment."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from .database import connection, initialize_database


SESSION_COOKIE = "supportflow_session"
SESSION_HOURS = 12

PERMISSIONS = {
    "query_order",
    "query_logistics",
    "query_inventory",
    "query_production",
    "create_sales_order",
    "refund_order",  # legacy compatibility; not part of the new workbench flow
    "submit_order",
    "submit_production_report",
    "submit_quality_report",
    "request_outbound",
    "admin_users",
    "admin_data",
}

DEFAULT_ROLE_PERMISSIONS = {
    "employee": {
        "query_order",
        "query_logistics",
        "query_inventory",
        "query_production",
        "submit_order",
        "submit_production_report",
        "submit_quality_report",
        "request_outbound",
    },
    "manager": {
        "query_order",
        "query_logistics",
        "query_inventory",
        "query_production",
        "create_sales_order",
        "refund_order",
    },
    "admin": PERMISSIONS,
}

ROLE_PERMISSION_PROFILES = {
    "employee": DEFAULT_ROLE_PERMISSIONS["employee"],
    "sales_employee": {"query_order", "query_logistics", "submit_order"},
    "production_employee": {"query_order", "query_production", "submit_production_report"},
    "quality_employee": {"query_order", "query_production", "submit_quality_report"},
    "warehouse_employee": {"query_order", "query_inventory", "query_logistics", "request_outbound"},
    "admin": PERMISSIONS,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.isoformat()


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("密码至少需要 8 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return "pbkdf2_sha256$210000$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, int(rounds)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _admin_credentials() -> tuple[str, str]:
    username = os.getenv("SUPPORTFLOW_ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.getenv("SUPPORTFLOW_ADMIN_PASSWORD", "").strip()
    return username, password


def ensure_bootstrap_admin() -> None:
    """Create/update the development admin from local env configuration."""

    initialize_database()
    username, password = _admin_credentials()
    if not password:
        migrate_legacy_employee_permissions()
        return
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                """INSERT INTO users(
                    username, display_name, password_hash, role, department
                ) VALUES (?, '系统管理员', ?, 'admin', '管理')""",
                (username, hash_password(password)),
            )
            user_id = cursor.lastrowid
        else:
            user_id = row["id"]
            conn.execute(
                """UPDATE users SET password_hash = ?, role = 'admin', status = 'ACTIVE'
                WHERE id = ?""",
                (hash_password(password), user_id),
            )
        for permission in sorted(PERMISSIONS):
            conn.execute(
                """INSERT OR IGNORE INTO user_permissions(user_id, permission)
                VALUES (?, ?)""",
                (user_id, permission),
            )
    migrate_legacy_employee_permissions()


def migrate_legacy_employee_permissions() -> int:
    """Safely add new defaults only to untouched legacy employee accounts."""
    old = {"query_order", "query_logistics", "query_inventory", "query_production"}
    additions = DEFAULT_ROLE_PERMISSIONS["employee"] - old
    changed = 0
    with connection() as conn:
        rows = conn.execute("SELECT id FROM users WHERE role = 'employee'").fetchall()
        for row in rows:
            current = {item[0] for item in conn.execute("SELECT permission FROM user_permissions WHERE user_id = ?", (row["id"],)).fetchall()}
            if current == old:
                for permission in additions:
                    conn.execute("INSERT OR IGNORE INTO user_permissions(user_id, permission) VALUES (?, ?)", (row["id"], permission))
                changed += 1
    return changed


def create_user(username: str, password: str, display_name: str, department: str) -> dict:
    initialize_database()
    username = username.strip().lower()
    display_name = display_name.strip()
    department = department.strip()
    if not username or not display_name:
        raise ValueError("用户名和姓名不能为空")
    with connection() as conn:
        try:
            cursor = conn.execute(
                """INSERT INTO users(username, display_name, password_hash, department)
                VALUES (?, ?, ?, ?)""",
                (username, display_name, hash_password(password), department),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError("USERNAME_EXISTS") from exc
            raise
        for permission in sorted(DEFAULT_ROLE_PERMISSIONS["employee"]):
            conn.execute(
                "INSERT INTO user_permissions(user_id, permission) VALUES (?, ?)",
                (cursor.lastrowid, permission),
            )
        return {
            "id": cursor.lastrowid,
            "username": username,
            "display_name": display_name,
            "department": department,
            "role": "employee",
            "status": "ACTIVE",
            "permissions": sorted(DEFAULT_ROLE_PERMISSIONS["employee"]),
        }


def authenticate(username: str, password: str) -> dict | None:
    initialize_database()
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND status = 'ACTIVE'",
            (username.strip().lower(),),
        ).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            return None
        now = _format_time(_now())
        conn.execute("UPDATE users SET last_login_at = ?, last_seen_at = ? WHERE id = ?", (now, now, row["id"]))
        return _user_from_row(conn, row)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = _now()
    expires = now + timedelta(hours=SESSION_HOURS)
    with connection() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, _format_time(expires)),
        )
    return token


def get_user_by_session(token: str | None) -> dict | None:
    if not token:
        return None
    initialize_database()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = _now()
    with connection() as conn:
        row = conn.execute(
            """SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND u.status = 'ACTIVE'""",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            return None
        if expires <= now:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None
        now_text = _format_time(now)
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now_text, token_hash))
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now_text, row["id"]))
        return _user_from_row(conn, row)


def destroy_session(token: str | None) -> None:
    if not token:
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def _user_from_row(conn, row) -> dict:
    permissions = [
        item["permission"]
        for item in conn.execute(
            "SELECT permission FROM user_permissions WHERE user_id = ?",
            (row["id"],),
        ).fetchall()
    ]
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "department": row["department"],
        "role": row["role"],
        "status": row["status"],
        "last_login_at": row["last_login_at"],
        "last_seen_at": row["last_seen_at"],
        "permissions": sorted(permissions),
    }


def list_users() -> list[dict]:
    initialize_database()
    with connection() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [_user_from_row(conn, row) for row in rows]


def set_user_permissions(user_id: int, permissions: list[str], actor_id: int) -> dict | None:
    initialize_database()
    invalid = set(permissions) - PERMISSIONS
    if invalid:
        raise ValueError("INVALID_PERMISSION")
    with connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            return None
        conn.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        for permission in sorted(set(permissions)):
            conn.execute(
                "INSERT INTO user_permissions(user_id, permission, granted_by) VALUES (?, ?, ?)",
                (user_id, permission, actor_id),
            )
        return _user_from_row(conn, user)


def set_user_access(user_id: int, role: str, permissions: list[str] | None, actor_id: int) -> dict | None:
    if role not in ROLE_PERMISSION_PROFILES:
        raise ValueError("INVALID_ROLE")
    selected = set(permissions) if permissions is not None else set(ROLE_PERMISSION_PROFILES[role])
    invalid = selected - PERMISSIONS
    if invalid:
        raise ValueError("INVALID_PERMISSION")
    with connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            return None
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        conn.execute("DELETE FROM user_permissions WHERE user_id = ?", (user_id,))
        for permission in sorted(selected):
            conn.execute("INSERT INTO user_permissions(user_id, permission, granted_by) VALUES (?, ?, ?)", (user_id, permission, actor_id))
        return _user_from_row(conn, conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
