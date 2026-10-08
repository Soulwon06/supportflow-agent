from fastapi.testclient import TestClient

from app.api import api
from app.auth import ensure_bootstrap_admin
from app.database import initialize_database


def test_employee_registration_admin_login_and_permission_grant(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPPORTFLOW_TEST_MODE", raising=False)
    monkeypatch.setenv("SUPPORTFLOW_AUTH_REQUIRED", "true")
    monkeypatch.setenv("SUPPORTFLOW_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SUPPORTFLOW_ADMIN_PASSWORD", "LocalAdmin-2026!")
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "auth.sqlite"))
    initialize_database()
    ensure_bootstrap_admin()

    client = TestClient(api)
    registered = client.post(
        "/auth/register",
        json={
            "username": "employee01",
            "password": "Employee-2026!",
            "display_name": "测试员工",
            "department": "生产部",
        },
    )
    assert registered.status_code == 200
    employee_id = registered.json()["user"]["id"]
    assert registered.json()["user"]["status"] == "ACTIVE"
    assert registered.json()["user"]["permissions"] == [
        "query_inventory",
        "query_logistics",
        "query_order",
        "query_production",
        "request_outbound",
        "submit_order",
        "submit_production_report",
        "submit_quality_report",
    ]

    # Registration is not an approval gate: the employee can enter the
    # workbench immediately with the safe default query permissions.
    employee_login_before_admin_grant = client.post(
        "/auth/login",
        json={"username": "employee01", "password": "Employee-2026!"},
    )
    assert employee_login_before_admin_grant.status_code == 200
    assert employee_login_before_admin_grant.json()["user"]["permissions"] == [
        "query_inventory",
        "query_logistics",
        "query_order",
        "query_production",
        "request_outbound",
        "submit_order",
        "submit_production_report",
        "submit_quality_report",
    ]
    client.post("/auth/logout")

    admin_login = client.post(
        "/auth/login",
        json={"username": "admin", "password": "LocalAdmin-2026!"},
    )
    assert admin_login.status_code == 200
    users = client.get("/admin/users")
    assert users.status_code == 200
    assert any(item["username"] == "employee01" for item in users.json()["users"])

    updated = client.put(
        f"/admin/users/{employee_id}/permissions",
        json={"permissions": ["query_inventory"]},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["permissions"] == ["query_inventory"]

    client.post("/auth/logout")
    employee_login = client.post(
        "/auth/login",
        json={"username": "employee01", "password": "Employee-2026!"},
    )
    assert employee_login.status_code == 200
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "employee01"

    forbidden = client.get("/admin/users")
    assert forbidden.status_code == 403
