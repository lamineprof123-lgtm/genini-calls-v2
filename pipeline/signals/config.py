from __future__ import annotations
import os, tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load ROOT/.env into os.environ without overriding existing vars. Never logs values."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:  # manual fallback
        p = ROOT / ".env"
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.toml"
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key in ("logs_dir", "state_dir"):
        p = Path(cfg["general"][key])
        cfg["general"][key] = str(p if p.is_absolute() else ROOT / p)
    p = Path(cfg["equity"]["paper_file"])
    cfg["equity"]["paper_file"] = str(p if p.is_absolute() else ROOT / p)
    site = Path(cfg.setdefault("site", {}).get("dir", "site"))
    cfg["site"]["dir"] = str(site if site.is_absolute() else ROOT / site)
    # GitHub Actions / external data dir: state, logs and the site JSON live in a separate checkout
    # (the pipeline-data branch). SIGNALS_DATA_DIR=/path -> state/, logs/, and site JSON at its root.
    data = os.environ.get("SIGNALS_DATA_DIR")
    if data:
        d = Path(data).resolve()
        cfg["general"]["state_dir"] = str(d / "state")
        cfg["general"]["logs_dir"] = str(d / "logs")
        cfg["equity"]["paper_file"] = str(d / "state" / "paper_equity.json")
        cfg["site"]["dir"] = str(d)
        cfg.setdefault("publish", {})["enabled"] = False   # the GHA loop commits + deploys itself
        cfg["general"]["log_mode"] = os.environ.get("SIGNALS_LOG_MODE", "compact")
    # Entry and exit models are independent. GEMINI_ENTRY_MODEL wins over legacy GEMINI_MODEL
    # (entry only). A rejected ID is not rewritten here — callers must not silently fall back.
    if os.environ.get("GEMINI_ENTRY_MODEL"):
        cfg["gemini"]["model"] = os.environ["GEMINI_ENTRY_MODEL"].strip()
    elif os.environ.get("GEMINI_MODEL"):
        cfg["gemini"]["model"] = os.environ["GEMINI_MODEL"].strip()
    if os.environ.get("GEMINI_EXIT_MODEL"):
        cfg["gemini"]["exit_model"] = os.environ["GEMINI_EXIT_MODEL"].strip()
    if os.environ.get("SIGNALS_INTERVAL_MINUTES"):
        cfg["scheduler"]["interval_minutes"] = int(os.environ["SIGNALS_INTERVAL_MINUTES"])
    vis = int(cfg["chart"]["visible_candles"])
    cfg["chart"]["visible_candles"] = max(80, min(120, vis))
    return cfg


def entry_model(cfg: dict, override: str | None = None) -> str:
    """Batched entry-scan model (config [gemini].model / GEMINI_ENTRY_MODEL / --model)."""
    return (override or (cfg.get("gemini") or {}).get("model") or "").strip()


def exit_model(cfg: dict, override: str | None = None) -> str:
    """18s exit-manager model (config [gemini].exit_model / GEMINI_EXIT_MODEL). Does not use the entry model unless exit_model is unset."""
    g = cfg.get("gemini") or {}
    return (override or g.get("exit_model") or g.get("model") or "").strip()
