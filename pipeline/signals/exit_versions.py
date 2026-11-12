"""Exit-prompt version tags written onto every closed trade (all books).

Used so scoring can split pre-fix vs post-fix Claude exits, and so Gemini
closes are labelled with the prompt generation that managed them. Changing
a version string does not change live exit behaviour.
"""

# Claude sims (C-OPUS / C-SONNET): Haiku exit prompt v3. Phase A is server-only
# (model CLOSE ignored); B/C keep the first-M15-close grace. New closes are
# tagged v3. Earlier closes stay v2-grace / v2-pre-grace and are not rewritten.
CLAUDE_EXIT_PROMPT_VERSION = "v3"
CLAUDE_EXIT_PROMPT_VERSION_PRE = "v2-pre-grace"

# Gemini main + 1-strike / nostop / bracket shadow books. The live Gemini
# exit prompt is templates/exit_prompt.j2 (profit-lock / reset6). Tag only;
# Gemini exit behaviour is unchanged.
GEMINI_EXIT_PROMPT_VERSION = "gemini-profit-lock-reset6"
# When Gemini exit/close API calls are paused, new closes on main / 1-strike / nostop
# (and CB-RUNNER, which drops its model exit checks) are tagged so entry stats can be split.
GEMINI_MECH_EXIT_VERSION = "mech-exit-v1"


def gemini_exit_calls_enabled(cfg: dict | None) -> bool:
    """Reversible switch for every Gemini CLOSE / exit-model API call (exit manager + Carbon runner).

    Prefers [exit_manager].gemini_exit_calls_enabled, then [gemini].exit_calls_enabled.
    Default True preserves the previous behaviour when the key is absent.
    """
    cfg = cfg or {}
    em = cfg.get("exit_manager") or {}
    if "gemini_exit_calls_enabled" in em:
        return bool(em["gemini_exit_calls_enabled"])
    g = cfg.get("gemini") or {}
    if "exit_calls_enabled" in g:
        return bool(g["exit_calls_enabled"])
    return True


def gemini_close_tag(cfg: dict | None = None) -> str:
    try:
        from .uniform_exit import uniform_exits_enabled, UNIFORM_EXIT_VERSION as _U
        if uniform_exits_enabled(cfg):
            return _U
    except Exception:
        pass
    return GEMINI_EXIT_PROMPT_VERSION if gemini_exit_calls_enabled(cfg) else GEMINI_MECH_EXIT_VERSION


# t247u: every book uses the same 1 ATR / 1 ATR / 90-min mechanical bracket.
# close_tag() lives in uniform_exit.py and returns this when uniform_1atr_exits is on.
UNIFORM_EXIT_VERSION = "uniform-1atr-v1"
