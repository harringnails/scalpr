#!/usr/bin/env python3
"""Apply explicit, checksum-verified Scalpr V2 Cloud SQL migrations."""

import hashlib
import sys
from pathlib import Path

from sqlalchemy import text
from cloudsql_database import CloudSqlDatabase

MIGRATIONS = Path(__file__).with_name("cloudsql_migrations")


def apply_migrations(database: CloudSqlDatabase) -> list[str]:
    applied = []
    with database.engine().begin() as connection:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version, sql = path.stem, path.read_text()
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            connection.exec_driver_sql(sql)
            result = connection.execute(text(
                "INSERT INTO scalpr_v2.schema_migrations(version, checksum) "
                "VALUES (:version, :checksum) ON CONFLICT (version) DO NOTHING"),
                {"version": version, "checksum": checksum})
            if result.rowcount:
                applied.append(version)
            existing = connection.execute(text(
                "SELECT checksum FROM scalpr_v2.schema_migrations "
                "WHERE version = :version"), {"version": version}).scalar_one()
            if existing != checksum:
                raise RuntimeError(f"Migration {version} checksum differs from the applied version.")
    return applied


def main() -> int:
    try:
        with CloudSqlDatabase() as database:
            print("Cloud SQL connection:", database.health())
            applied = apply_migrations(database)
    except Exception as exc:
        print(f"Cloud SQL migration failed ({type(exc).__name__}).")
        return 1
    print("Applied migrations:", ", ".join(applied) if applied else "none (current)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
