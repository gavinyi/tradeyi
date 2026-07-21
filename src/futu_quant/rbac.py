from __future__ import annotations


PERMISSIONS = {
    "VIEWER": {"read"},
    "OPERATOR": {"read", "trade", "cancel", "run_validation"},
    "ADMIN": {"read", "trade", "cancel", "run_validation", "publish_config", "backup", "service_control", "manage_secrets"},
}


def allowed(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(str(role).upper(), set())


def role_for_session(admin_authenticated: bool, requested: str = "VIEWER") -> str:
    if admin_authenticated:
        return "ADMIN"
    requested = str(requested).upper()
    return requested if requested == "VIEWER" else "VIEWER"
