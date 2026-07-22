import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# LLM: headless Claude Code CLI (`claude -p`), reusing whatever auth the
# local `claude` binary already has (subscription or API key) instead of
# per-token Bedrock billing. Model alias: "sonnet", "opus", "haiku", etc.
CLAUDE_CODE_MODEL = os.environ.get("CLAUDE_CODE_MODEL", "sonnet")

# Embeddings: local sentence-transformers model, 1024-d to match the
# lessons.embedding VECTOR(1024) column without a schema migration.
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "BAAI/bge-large-en-v1.5")
EMBED_DIMENSIONS = 1024

# Offline mode: deterministic fake embeddings + heuristic lesson classifier,
# so the memory layer (and its conflict tests) run without a local model
# download or a `claude` login.
OFFLINE = os.environ.get("TRIBUTARY_OFFLINE", "") not in ("", "0", "false")
