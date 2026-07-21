from __future__ import annotations

import secrets
import string

from argon2 import PasswordHasher


USERNAME = "Admin@system.com"
ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"


def generate_password(length: int = 32) -> str:
    while True:
        password = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if (
            any(ch.islower() for ch in password)
            and any(ch.isupper() for ch in password)
            and any(ch.isdigit() for ch in password)
            and any(ch in "!@#$%^&*()-_=+" for ch in password)
        ):
            return password


def main() -> None:
    password = generate_password()
    password_hash = PasswordHasher().hash(password)
    print("Create this single LIVE admin user on the server.")
    print("Store the password immediately in a password manager. It is shown once only.")
    print()
    print(f"LIVE_LOGIN_USERNAME={USERNAME}")
    print(f"LIVE_LOGIN_PASSWORD={password}")
    print()
    print("Store this hash in Google Secret Manager as LIVE_LOGIN_PASSWORD_HASH:")
    print(password_hash)


if __name__ == "__main__":
    main()
