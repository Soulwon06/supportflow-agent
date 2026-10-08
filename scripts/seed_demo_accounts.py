"""Create role-based local demo accounts from environment-provided passwords.

Passwords are intentionally never hard-coded or printed. Set one or more of:
SUPPORTFLOW_DEMO_SALES_PASSWORD, SUPPORTFLOW_DEMO_PRODUCTION_PASSWORD,
SUPPORTFLOW_DEMO_QUALITY_PASSWORD, SUPPORTFLOW_DEMO_WAREHOUSE_PASSWORD.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env", override=False)

from app.auth import ROLE_PERMISSION_PROFILES, create_user, ensure_bootstrap_admin, set_user_access
from app.database import connection, initialize_database


ACCOUNTS = {
    "sales_demo": ("业务员演示", "业务部", "SUPPORTFLOW_DEMO_SALES_PASSWORD", "sales_employee"),
    "production_demo": ("生产员工演示", "生产部", "SUPPORTFLOW_DEMO_PRODUCTION_PASSWORD", "production_employee"),
    "quality_demo": ("质检员工演示", "质量部", "SUPPORTFLOW_DEMO_QUALITY_PASSWORD", "quality_employee"),
    "warehouse_demo": ("仓库员工演示", "仓储部", "SUPPORTFLOW_DEMO_WAREHOUSE_PASSWORD", "warehouse_employee"),
}


def main() -> None:
    initialize_database()
    ensure_bootstrap_admin()
    with connection() as conn:
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    if admin is None:
        raise SystemExit("未找到管理员，请先配置管理员环境变量")
    created = []
    for username, (display_name, department, env_name, role) in ACCOUNTS.items():
        password = os.getenv(env_name, "").strip()
        if not password:
            continue
        with connection() as conn:
            row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            user = create_user(username, password, display_name, department)
            user_id = user["id"]
        else:
            user_id = row["id"]
        set_user_access(user_id, role, sorted(ROLE_PERMISSION_PROFILES[role]), admin["id"])
        created.append(username)
    print("DEMO_ACCOUNTS_READY=" + ",".join(created))


if __name__ == "__main__":
    main()
