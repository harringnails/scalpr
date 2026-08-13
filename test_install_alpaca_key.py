"""Network-free secret-safety tests for the Alpaca paper key installer."""

from install_alpaca_key import normalize_key, validate_key_pair


class Response:
    def __init__(self, status):
        self.status_code = status


class Session:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params, timeout, dict(headers or {})))
        return Response(self.status)


def test_normalize_accepts_raw_and_assignment_forms():
    assert normalize_key("  secret  ") == "secret"
    assert normalize_key("apiKey=secret") == "secret"
    assert normalize_key("API_KEY=secret") == "secret"
    assert normalize_key("Authorization: Bearer secret") == "secret"


def test_success_proves_authentication_and_uses_headers_only():
    session = Session(200)
    accepted, message = validate_key_pair("key", "secret", session=session)
    assert accepted is True and "accepted" in message
    assert session.calls[0][3]["APCA-API-KEY-ID"] == "key"
    assert session.calls[0][3]["APCA-API-SECRET-KEY"] == "secret"


def test_failures_are_sanitized_and_never_echo_secret():
    for status in (401, 403, 429, 500):
        accepted, message = validate_key_pair("key", "secret-value", session=Session(status))
        assert accepted is False
        assert "secret-value" not in message
        assert str(status) in message


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
