import paper_account_flat_check_v0 as proof


class Response:
    def __init__(self, value, status=200): self.value, self.status = value, status
    def json(self): return self.value
    def raise_for_status(self):
        if self.status >= 400: raise RuntimeError(self.status)


def requester(values):
    def get(url, **_kwargs):
        if url.endswith("/account"): return Response(values[0])
        if url.endswith("/positions"): return Response(values[1])
        return Response(values[2])
    return get


def test_active_empty_paper_account_is_flat():
    value = proof.prove_flat(api_key="key", api_secret="secret",
                             requester=requester(({"status": "ACTIVE"}, [], [])))
    assert value["flat"] is True
    assert value["source"] == "alpaca_trading_api_direct_uncached"
    assert value["positions_count"] == value["open_orders_count"] == 0


def test_position_order_or_inactive_account_fails_closed():
    cases = (({"status": "ACTIVE"}, [{"symbol": "SPY"}], []),
             ({"status": "ACTIVE"}, [], [{"id": "order"}]),
             ({"status": "SUSPENDED"}, [], []))
    for values in cases:
        assert proof.prove_flat(api_key="key", api_secret="secret",
                                requester=requester(values))["flat"] is False


def test_restart_cold_start_and_sleep_survival_are_enforced():
    source = open("restart.sh", encoding="utf-8").read()
    assert "paper_account_flat_check_v0.py" in source
    assert "/usr/bin/caffeinate -is" in source
    assert "REFUSING startup" in source
    assert 'wait "$SERVER_PID"' in source
    assert "/usr/bin/nohup" not in source
