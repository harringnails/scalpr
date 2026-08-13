"""Optional Cloud SQL PostgreSQL repository for Scalpr V2 evidence.

Importing this module never connects, migrates, or changes legacy storage.
Google ADC authenticates the connector; the PostgreSQL password stays in the
macOS Keychain and is loaded only while opening a database connection.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


INSTANCE_CONNECTION_NAME = "scalpr-dev:us-central1:scalpr-dev"
DATABASE_NAME = "scalpr"
KEYCHAIN_SERVICE = "scalpr.cloudsql.database"
PROFILE_PATH = Path("v2_data/cloudsql_profile.json")


class CloudSqlConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudSqlProfile:
    db_user: str
    instance_connection_name: str = INSTANCE_CONNECTION_NAME
    database: str = DATABASE_NAME
    ip_type: str = "PUBLIC"
    enable_iam_auth: bool = False

    @classmethod
    def load(cls, path: Path = PROFILE_PATH) -> "CloudSqlProfile":
        try:
            raw = json.loads(Path(path).read_text())
        except FileNotFoundError as exc:
            raise CloudSqlConfigurationError(
                "Cloud SQL is not configured; run setup_cloudsql.py first."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CloudSqlConfigurationError(
                "Cloud SQL profile is unreadable; run setup_cloudsql.py again."
            ) from exc
        db_user = str(raw.get("db_user") or "").strip()
        if not db_user:
            raise CloudSqlConfigurationError("Cloud SQL profile has no database user.")
        profile = cls(
            db_user=db_user,
            instance_connection_name=str(
                raw.get("instance_connection_name") or INSTANCE_CONNECTION_NAME
            ),
            database=str(raw.get("database") or DATABASE_NAME),
            ip_type=str(raw.get("ip_type") or "PUBLIC").upper(),
            enable_iam_auth=bool(raw.get("enable_iam_auth", False)),
        )
        if profile.instance_connection_name != INSTANCE_CONNECTION_NAME:
            raise CloudSqlConfigurationError("Cloud SQL profile targets an unexpected instance.")
        if profile.database != DATABASE_NAME:
            raise CloudSqlConfigurationError("Cloud SQL profile targets an unexpected database.")
        if profile.ip_type != "PUBLIC":
            raise CloudSqlConfigurationError("Cloud SQL profile must use the public connector path.")
        if profile.enable_iam_auth:
            raise CloudSqlConfigurationError("IAM database authentication is not enabled for this instance.")
        return profile


def read_keychain_password(db_user: str) -> str:
    """Read the password in-process without putting it in argv or stdout."""
    framework = ctypes.util.find_library("Security")
    if not framework:
        raise CloudSqlConfigurationError("macOS Security.framework is unavailable.")
    security = ctypes.CDLL(framework)
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    account, service = db_user.encode("utf-8"), KEYCHAIN_SERVICE.encode("utf-8")
    length, data = ctypes.c_uint32(), ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(service), service, len(account), account,
        ctypes.byref(length), ctypes.byref(data), None,
    )
    if status != 0 or not data.value or not length.value:
        raise CloudSqlConfigurationError(
            "Cloud SQL password is unavailable in macOS Keychain."
        )
    try:
        return ctypes.string_at(data, length.value).decode("utf-8")
    finally:
        security.SecKeychainItemFreeContent(None, data)


def _configure_tls_trust() -> str:
    """Use native macOS trust evaluation, with a verified PEM fallback."""
    try:
        import truststore
    except ImportError:
        import certifi
        system_ca = Path("/etc/ssl/cert.pem")
        ca_bundle = system_ca if system_ca.is_file() else Path(certifi.where())
        os.environ["SSL_CERT_FILE"] = str(ca_bundle)
        return str(ca_bundle)
    truststore.inject_into_ssl()
    return "macOS Keychain"


def _restore_stdlib_tls() -> None:
    """Restore OpenSSL after aiohttp has cached the native HTTPS context.

    Cloud SQL uses an ephemeral, instance-specific CA for its database socket.
    That private CA must be loaded into the standard SSLContext by the connector
    rather than evaluated against the macOS system trust store.
    """
    try:
        import truststore
    except ImportError:
        return
    truststore.extract_from_ssl()


class CloudSqlDatabase:
    """Lazy SQLAlchemy pool backed by the Cloud SQL Python Connector."""

    def __init__(self, profile: CloudSqlProfile | None = None, *,
                 password_loader: Callable[[str], str] = read_keychain_password,
                 connector_factory=None, engine_factory=None):
        self.profile = profile or CloudSqlProfile.load()
        self._password_loader = password_loader
        self._connector_factory = connector_factory
        self._engine_factory = engine_factory
        self._connector = None
        self._engine = None

    def _dbapi_connection(self):
        if self._connector is None:
            if self._connector_factory is None:
                # Use Security.framework-backed macOS trust evaluation so
                # locally trusted issuers are honored. This must happen before
                # importing Connector because aiohttp builds and caches its
                # verified SSL context at import time. Restore stdlib SSL after
                # that import so Cloud SQL can load its private ephemeral CA.
                _configure_tls_trust()
                try:
                    from google.cloud.sql.connector import Connector
                finally:
                    _restore_stdlib_tls()
                self._connector = Connector(
                    refresh_strategy="LAZY", quota_project="scalpr-dev")
            else:
                self._connector = self._connector_factory()
        password = self._password_loader(self.profile.db_user)
        return self._connector.connect(
            self.profile.instance_connection_name, "pg8000",
            user=self.profile.db_user, password=password, db=self.profile.database,
            ip_type=self.profile.ip_type,
            enable_iam_auth=self.profile.enable_iam_auth,
        )

    def engine(self):
        if self._engine is None:
            if self._engine_factory is None:
                from sqlalchemy import create_engine
                self._engine_factory = create_engine
            self._engine = self._engine_factory(
                "postgresql+pg8000://", creator=self._dbapi_connection,
                pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=1800,
            )
        return self._engine

    def health(self) -> dict:
        from sqlalchemy import text
        with self.engine().connect() as connection:
            row = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
        return {
            "available": True, "database": row[0], "db_user": row[1],
            "instance": self.profile.instance_connection_name,
            "ip_type": self.profile.ip_type,
            "iam_database_auth": self.profile.enable_iam_auth,
        }

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        if self._connector is not None:
            self._connector.close()
            self._connector = None

    def __enter__(self) -> "CloudSqlDatabase":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def configured(path: Path = PROFILE_PATH) -> bool:
    try:
        CloudSqlProfile.load(path)
    except CloudSqlConfigurationError:
        return False
    return True
