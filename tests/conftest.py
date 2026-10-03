"""
tests/conftest.py

config.py raises EnvironmentError at import time if Alpaca credentials
aren't set (see config.py's module-level check), and bot/broker.py's
AlpacaBroker talks to the real Alpaca API in its constructor. Neither of
those should ever run in a unit test, so:

  - Dummy credentials are set here, before any other test module imports
    `config` or anything under `bot/`, so the import-time check passes.
  - Individual tests monkeypatch `bot.main.AlpacaBroker` with a fake that
    never touches the network (see tests/test_circuit_breaker.py).

This file has no fixtures of its own; it just guarantees the environment
is sane before collection imports anything else.
"""

import os

os.environ.setdefault("APCA_API_KEY_ID", "test-key-id")
os.environ.setdefault("APCA_API_SECRET_KEY", "test-secret-key")
