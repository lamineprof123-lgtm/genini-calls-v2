import os

# Unit tests run on any day: the client-level weekend backstop (signals/market_hours.guard) is off here.
# The caller-level gates are tested explicitly in tests/test_market_hours.py.
os.environ.setdefault("BB_MARKET_GUARD", "0")
