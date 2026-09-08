from __future__ import annotations

from getpass import getpass
from futu_quant.admin_auth import hash_password
from futu_quant.config import save_runtime_settings


def main() -> None:
    password = getpass("New administrator password: ")
    confirmation = getpass("Confirm password: ")
    if len(password) < 10:
        raise ValueError("Administrator password must contain at least 10 characters")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    save_runtime_settings({"ADMIN_PASSWORD_HASH": hash_password(password)})
    print("Administrator password hash saved to .env")


if __name__ == "__main__":
    main()
