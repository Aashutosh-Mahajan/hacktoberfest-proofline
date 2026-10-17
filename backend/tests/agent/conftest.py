import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs a running Ollama with gemma4:e4b (opt-in: pytest -m live)")


def pytest_collection_modifyitems(config, items):
    if "live" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="live Gemma test; run with: pytest -m live")
    for it in items:
        if "live" in it.keywords:
            it.add_marker(skip)
