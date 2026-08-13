"""Network-free tests for the optional Cloud SQL boundary."""

import json
import os
import tempfile
from pathlib import Path

from cloudsql_database import (CloudSqlConfigurationError, CloudSqlDatabase,
    CloudSqlProfile, _configure_tls_trust, _restore_stdlib_tls, configured)


def check(name, condition):
    print(f"  {'ok' if condition else 'FAIL'}: {name}")
    assert condition, name


class FakeConnector:
    def __init__(self):
        self.calls, self.closed = [], False
    def connect(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "dbapi-connection"
    def close(self):
        self.closed = True


def test_profile_is_explicit_and_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "profile.json"
        check("missing profile is unconfigured", not configured(path))
        try:
            CloudSqlProfile.load(path)
        except CloudSqlConfigurationError:
            pass
        else:
            raise AssertionError("missing profile did not fail closed")
        path.write_text(json.dumps({"db_user": "scalpr_app"}))
        profile = CloudSqlProfile.load(path)
        check("known instance retained", profile.instance_connection_name.endswith(":scalpr-dev"))
        check("public IP explicit", profile.ip_type == "PUBLIC")
        check("IAM database auth disabled", profile.enable_iam_auth is False)
        path.write_text(json.dumps({"db_user": "scalpr_app", "enable_iam_auth": True}))
        try:
            CloudSqlProfile.load(path)
        except CloudSqlConfigurationError:
            pass
        else:
            raise AssertionError("unexpected IAM database auth did not fail closed")


def test_connector_receives_regular_postgres_auth():
    fake = FakeConnector()
    profile = CloudSqlProfile(db_user="scalpr_app")
    database = CloudSqlDatabase(profile,
        password_loader=lambda user: "hidden-password" if user == "scalpr_app" else "",
        connector_factory=lambda: fake)
    check("fake DBAPI connection returned", database._dbapi_connection() == "dbapi-connection")
    args, kwargs = fake.calls[0]
    check("connection name and driver set", args == (profile.instance_connection_name, "pg8000"))
    check("database password passed only to connector", kwargs["password"] == "hidden-password")
    check("public connector path used", kwargs["ip_type"] == "PUBLIC")
    check("IAM database auth remains off", kwargs["enable_iam_auth"] is False)
    password_args = repr((args, {**kwargs, "password": "<redacted>"}))
    check("test diagnostics can redact password", "hidden-password" not in password_args)
    database.close()
    check("connector closes cleanly", fake.closed)


def test_certifi_bundle_is_valid():
    import certifi
    import ssl
    context = ssl.create_default_context(cafile=certifi.where())
    check("certifi provides trusted roots", len(context.get_ca_certs()) > 100)


def test_macos_bundle_is_valid_when_present():
    import ssl
    bundle = Path("/etc/ssl/cert.pem")
    if bundle.is_file():
        context = ssl.create_default_context(cafile=bundle)
        check("macOS bundle provides trusted roots", len(context.get_ca_certs()) > 100)


def test_native_truststore_is_available():
    import truststore
    check("native truststore adapter installed", bool(truststore))


if __name__ == "__main__":
    test_profile_is_explicit_and_fail_closed()
    test_connector_receives_regular_postgres_auth()
    test_certifi_bundle_is_valid()
    test_macos_bundle_is_valid_when_present()
    test_native_truststore_is_available()
    print("\nALL CLOUD SQL TESTS PASSED")
