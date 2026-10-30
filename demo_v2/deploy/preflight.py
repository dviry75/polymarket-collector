"""Fail before production startup without creating a DB or printing config values."""
import os
from argon2 import PasswordHasher
from ..config import DemoConfig


def validate(config):
    if not config.login_username or len(config.login_username)>120:
        raise ValueError("Provision DEMO_AUTH_USERNAME")
    if len(config.session_secret)<32:
        raise ValueError("Provision an independent DEMO_SESSION_SECRET of at least 32 characters")
    if not config.login_password_hash.startswith("$argon2id$"):
        raise ValueError("Provision an Argon2id DEMO_AUTH_PASSWORD_HASH")
    try:
        PasswordHasher().check_needs_rehash(config.login_password_hash)
    except Exception:
        raise ValueError("Invalid DEMO Argon2id hash") from None
    if not config.session_cookie_secure:
        raise ValueError("Production requires DEMO_COOKIE_SECURE=true")
    if not config.public_data_enabled:
        raise ValueError("Production requires DEMO_PUBLIC_DATA_ENABLED=true")


def main():
    try:
        validate(DemoConfig.from_env())
        if os.geteuid()==0:
            raise ValueError("Run the DEMO service as its dedicated non-root user")
    except (ValueError,TypeError):
        # Deliberately do not echo exception/config values, even malformed secrets.
        raise SystemExit("DEMO preflight failed: check the private DEMO configuration and service user") from None
    print("DEMO production configuration accepted; no database opened")


if __name__=="__main__":
    main()
