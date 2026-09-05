import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HTML = ROOT / "dashboard.html"
FROZEN_SUBMIT_HASH = "1fcc7b3c8d861a62de440c1a7afb15f4b75727bd1f385d4d605ebdcd5e25fc6b"
FROZEN_SERVER_HASH = "ba20c7cd084825a57a2326dbafe98b89c28fb5a76e23ec8659d8d94e362838e7"


def function_source(html, name):
    match = re.search(rf"(?:async )?function {name}\([^)]*\)\s*\{{.*?\n\}}", html, re.S)
    assert match, f"missing {name}"
    return match.group()


def test_trade_serializer_and_server_are_byte_unchanged():
    html = HTML.read_text()
    assert hashlib.sha256(function_source(html, "submitTrade").encode()).hexdigest() == FROZEN_SUBMIT_HASH
    assert hashlib.sha256((ROOT / "scalp_server.py").read_bytes()).hexdigest() == FROZEN_SERVER_HASH


def test_chart_has_required_controls_overlay_and_local_echarts():
    html = HTML.read_text()
    panel = re.search(r'<section id="marketStructurePanel".*?</section>', html, re.S).group()
    render = function_source(html, "renderMarketStructure")
    assert all(f'data-structure-symbol="{symbol}"' in panel for symbol in ("SPY", "QQQ", "IWM", "DIA"))
    assert all(f'data-structure-timeframe="{frame}"' in panel for frame in ("1m", "5m"))
    assert 'http://127.0.0.1:8423/static/echarts.min.js' in html
    assert "type:'candlestick'" in render
    assert "markLine" in render and "markArea" in render and "type:'scatter'" in render
    assert "gamma_flip" in render and "call_wall" in render and "put_wall" in render
    assert "pin pocket" in render and "Accrued episode t0" in render


def test_stale_and_not_started_states_disable_interpretation():
    html = HTML.read_text()
    render = function_source(html, "renderMarketStructure")
    refresh = function_source(html, "refreshMarketStructure")
    state = function_source(html, "renderStructureState")
    assert "NOT_STARTED" in render
    assert "STALE — DO NOT INTERPRET" in render
    assert "body.classList.toggle('stale', state !== 'LIVE')" in state
    assert "MARKET STRUCTURE CAPTURE NOT RUNNING" in refresh
    assert "if (structureChart) structureChart.clear()" in refresh


def test_chart_is_display_only_and_absent_from_protected_paths():
    html = HTML.read_text()
    protected = function_source(html, "submitTrade") + function_source(html, "runPrecheck")
    assert "structure" not in protected.lower()
    assert "8423" not in protected
    source = (ROOT / "scalp_server.py").read_text()
    assert "marketStructure" not in source and "8423" not in source
    panel = re.search(r'<section id="marketStructurePanel".*?</section>', html, re.S).group()
    assert "no execution/admission/Guard authority" in panel
    assert "episode markers are anchors, not signals" in panel
    assert "study basis is quote-mid" in panel


def test_echarts_is_vendored_and_no_live_cdn_is_used():
    html = HTML.read_text()
    asset = ROOT / "vendor" / "echarts.min.js"
    assert asset.exists() and asset.stat().st_size > 1_000_000
    assert "cdn.jsdelivr" not in html and "unpkg.com" not in html
    assert (ROOT / "vendor" / "ECHARTS_LICENSE.txt").exists()

