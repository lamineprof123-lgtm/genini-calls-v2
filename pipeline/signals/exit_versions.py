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
