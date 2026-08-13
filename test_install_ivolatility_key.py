"""Network-free secret-safety tests for the IVolatility key installer."""

from install_ivolatility_key import normalize_key, validate_key


class Response:
    def __init__(self, status):
        self.status_code = status


class Session:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return Response(self.status)


def test_normalize_accepts_raw_and_assignment_forms():
    assert normalize_key("  secret  ") == "secret"
    assert normalize_key("apiKey=secret") == "secret"
    assert normalize_key("API_KEY=secret") == "secret"


def test_success_and_no_data_both_prove_authentication():
    for status in (200, 204):
        session = Session(status)
        accepted, message = validate_key("secret", session=session)
        assert accepted is True and "accepted" in message
        assert session.calls[0][1]["apiKey"] == "secret"
        assert session.calls[0][1]["symbol"] == "SPY"
        assert session.calls[0][1]["dteFrom"] == 0
        assert session.calls[0][1]["dteTo"] == 2


def test_failures_are_sanitized_and_never_echo_key():
    for status in (401, 403, 429, 500):
        accepted, message = validate_key("secret-value", session=Session(status))
        assert accepted is False
        assert "secret-value" not in message
        assert str(status) in message


if __name__ == "__main__":
    for name, test in sorted(globals().copy().items()):
        if name.startswith("test_"):
            test()
            print("PASS", name)
