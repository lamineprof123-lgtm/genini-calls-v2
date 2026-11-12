"""Exit-prompt version tags written onto every closed trade (all books).

Used so scoring can split pre-fix vs post-fix Claude exits, and so Gemini
closes are labelled with the prompt generation that managed them. Changing
a version string does not change live exit behaviour.
"""

# Claude sims (C-OPUS / C-SONNET): Haiku exit prompt v2. After the 12 Nov
# grace-period fix, server ignores claude_close until the first M15 bar close
# after entry. Trades closed before that fix are backfilled as PRE.
CLAUDE_EXIT_PROMPT_VERSION = "v2-grace"
CLAUDE_EXIT_PROMPT_VERSION_PRE = "v2-pre-grace"

# Gemini main + 1-strike / nostop / bracket shadow books. The live Gemini
# exit prompt is templates/exit_prompt.j2 (profit-lock / reset6). Tag only;
# Gemini exit behaviour is unchanged.
GEMINI_EXIT_PROMPT_VERSION = "gemini-profit-lock-reset6"
