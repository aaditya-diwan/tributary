import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# LLM backend:
#   "claude" - headless Claude Code CLI (`claude -p`), reusing whatever auth the
#              local `claude` binary already has (subscription or API key).
#   "openai" - any OpenAI-compatible chat API: DeepSeek, OpenRouter, Ollama,
#              vLLM, OpenAI itself. Configured by LLM_BASE_URL / LLM_API_KEY /
#              LLM_MODEL (defaults target DeepSeek).
LLM_BACKEND = os.environ.get("LLM_BACKEND", "claude").lower()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
_OPENAI = LLM_BACKEND == "openai"

# The agent/reasoning model. With the claude backend this is an alias
# ("sonnet", "opus", "haiku"); with the openai backend, a provider model name.
CLAUDE_CODE_MODEL = os.environ.get("CLAUDE_CODE_MODEL", LLM_MODEL if _OPENAI else "sonnet")
if _OPENAI and CLAUDE_CODE_MODEL in ("haiku", "sonnet", "opus"):
    CLAUDE_CODE_MODEL = LLM_MODEL  # a Claude alias left over in .env means nothing here

# Model tiering for the duplicate/contradiction classifier: the cheap model
# handles the easy calls; ambiguous or destructive ones escalate to the
# stronger model. A contradiction always escalates because superseding a
# lesson is destructive, and a low-confidence verdict escalates because that's
# where the cheap model is unreliable.
CLASSIFY_MODEL_CHEAP = os.environ.get("CLASSIFY_MODEL_CHEAP", LLM_MODEL if _OPENAI else "haiku")
CLASSIFY_MODEL_STRONG = os.environ.get("CLASSIFY_MODEL_STRONG", CLAUDE_CODE_MODEL)
# 0.75 was chosen against LLM self-reported confidence. Jev's is derived from
# its probabilities (with 3 options, 0.75 means the top option has p >= 0.83);
# re-tune from the live classification eval once Jev numbers exist.
CLASSIFY_ESCALATE_BELOW = float(os.environ.get("CLASSIFY_ESCALATE_BELOW", "0.75"))

# Jev (TypeSafe's System One classification model) as a classifier tier: set
# CLASSIFY_MODEL_CHEAP=jev. Env names match the TypeSafe SDK's own. Jev bills
# input tokens only; the rate is here so cost logging stays honest if it moves.
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
JEV_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
JEV_USD_PER_MTOK = float(os.environ.get("JEV_USD_PER_MTOK", "0.042"))

# Second layer of the injection screen (the regex layer always runs first):
# "jev" asks Jev one yes/no question per hazard (~0.25 s); anything else is an
# LLM model name (the original screen, ~10 s via `claude -p`). A Jev hazard
# fires at p >= SCREEN_JEV_THRESHOLD (TypeSafe's guardrails cookbook "strict").
SCREEN_MODEL = os.environ.get("SCREEN_MODEL", LLM_MODEL if _OPENAI else "haiku")
SCREEN_JEV_THRESHOLD = float(os.environ.get("SCREEN_JEV_THRESHOLD", "0.70"))

# Embeddings: local sentence-transformers model, 1024-d to match the
# lessons.embedding VECTOR(1024) column without a schema migration.
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "BAAI/bge-large-en-v1.5")
EMBED_DIMENSIONS = 1024

# Offline mode: deterministic fake embeddings + heuristic lesson classifier,
# so the memory layer (and its conflict tests) run without a local model
# download or a `claude` login.
OFFLINE = os.environ.get("TRIBUTARY_OFFLINE", "") not in ("", "0", "false")
