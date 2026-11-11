import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from google.genai import errors
from signals.gemini_client import _is_retryable


def err(code, status, msg):
    return errors.APIError(code, {"error": {"code": code, "status": status, "message": msg}})


def test_retry_classification():
    assert _is_retryable(err(503, "UNAVAILABLE", "high demand"))
    assert _is_retryable(err(429, "RESOURCE_EXHAUSTED", "rate limit"))
    assert _is_retryable(err(500, "INTERNAL", "x"))
    assert not _is_retryable(err(402, "RESOURCE_EXHAUSTED", "Your prepayment credits are depleted"))
    assert not _is_retryable(err(404, "NOT_FOUND", "no longer available to new users"))
