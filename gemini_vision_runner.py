"""Send the latest chart screenshot to Gemini and write latest_signal.json + history.json.

Expects the screenshot at latest_chart.png (override with CHART_PATH).
"""
import json
import os
from datetime import datetime, timezone

from google import genai
from google.genai import types

MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"   # gemini-1.5-pro is shut down
CHART_PATH = os.environ.get("CHART_PATH", "latest_chart.png")
HISTORY_MAX = 500

PROMPT = """
You are an expert quantitative trading vision model. Analyze the provided order flow footprint chart.

1. Check for specific textual context notes (e.g. "Aggressive Buying", "RTH Open above ETH VAH").
2. Identify if there is a clear visual pattern indicating "Buyers Absorb Sellers" (BUY) or
   "Sellers Absorb Buyers" (SELL) at key support/resistance nodes.
3. Return ONLY JSON matching this schema:

{
  "no_trade": [],
  "analysis_failed": false,
  "trades": [
    {
      "symbol": "NQ-202612",
      "action": "BUY",
      "entry": 0.0,
      "stop_loss": 0.0,
      "take_profit": 0.0,
      "rr": 0.0,
      "digits": 2,
      "context_notes": []
    }
  ]
}

If there is no valid setup, return "trades": [] and put the reasons in "no_trade".
If the chart is unreadable, set "analysis_failed": true.
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def analyze(path):
    if not os.path.exists(path):
        return {"analysis_failed": True, "error": f"Chart image not found ({path})", "trades": []}
    if not os.environ.get("GEMINI_API_KEY"):
        return {"analysis_failed": True, "error": "GEMINI_API_KEY not set", "trades": []}
    try:
        client = genai.Client()  # reads GEMINI_API_KEY
        with open(path, "rb") as f:
            img = types.Part.from_bytes(data=f.read(), mime_type="image/png")
        resp = client.models.generate_content(
            model=MODEL,
            contents=[img, PROMPT],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        text = resp.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        return {"analysis_failed": True, "error": str(e)[:300], "trades": []}


if __name__ == "__main__":
    signal_time = now_iso()
    result = analyze(CHART_PATH)
    result.setdefault("trades", [])
    result.setdefault("no_trade", [])
    # The panel needs these to time and de-duplicate signals.
    result["signal_at"] = signal_time
    result["written_at"] = now_iso()
    result["tz"] = "Asia/Dubai"
    result["model"] = MODEL

    with open("latest_signal.json", "w") as f:
        json.dump(result, f, indent=2)

    history = []
    if os.path.exists("history.json"):
        try:
            with open("history.json") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(result)
    with open("history.json", "w") as f:
        json.dump(history[-HISTORY_MAX:], f, indent=2)

    print(f"Signal logged. analysis_failed={result.get('analysis_failed')} error={result.get('error')}")
