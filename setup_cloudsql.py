#!/usr/bin/env python3
"""Validate Cloud SQL access, then store its PostgreSQL password in Keychain."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from cloudsql_database import (DATABASE_NAME, INSTANCE_CONNECTION_NAME,
    KEYCHAIN_SERVICE, PROFILE_PATH, CloudSqlDatabase, CloudSqlProfile)
from install_ivolatility_key import KeychainError, write_keychain_secret


def _write_profile(profile: CloudSqlProfile, path: Path = PROFILE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "cloudsql-profile-v1",
        "instance_connection_name": profile.instance_connection_name,
        "database": profile.database,
        "db_user": profile.db_user,
        "ip_type": profile.ip_type,
        "enable_iam_auth": profile.enable_iam_auth,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def validate(profile: CloudSqlProfile, password: str) -> dict:
    database = CloudSqlDatabase(profile, password_loader=lambda _user: password)
    try:
        return database.health()
    finally:
        database.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and store Scalpr's Cloud SQL credential.")
    parser.add_argument("--db-user", help="PostgreSQL user (prompted when omitted)")
    args = parser.parse_args(argv)
    print("Scalpr Cloud SQL setup")
    print(f"Instance: {INSTANCE_CONNECTION_NAME}")
    print(f"Database: {DATABASE_NAME}")
    print("This hidden prompt expects the PostgreSQL password, not your Google password.")
    db_user = args.db_user or input("Database user [postgres]: ").strip() or "postgres"
    password = getpass.getpass("Database password (hidden): ")
    if not password:
        print("No password entered. Nothing was stored.")
        return 1
    profile = CloudSqlProfile(db_user=db_user)
    try:
        health = validate(profile, password)
    except Exception as exc:
        print(f"Cloud SQL validation failed ({type(exc).__name__}). Nothing was stored.")
        print("Confirm Application Default Credentials, Cloud SQL Client access, and the database login.")
        return 1
    try:
        write_keychain_secret(db_user, KEYCHAIN_SERVICE, password)
        _write_profile(profile)
    except (OSError, KeychainError):
        print("Validation succeeded, but local configuration was incomplete. Run setup again.")
        return 1
    print(f"Cloud SQL validated and configured: {health['database']} as {health['db_user']}.")
    print("The password is in macOS Keychain and was not written to disk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
