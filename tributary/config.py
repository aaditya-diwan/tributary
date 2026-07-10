import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
BEDROCK_EMBED_MODEL_ID = os.environ.get(
    "BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"
)
EMBED_DIMENSIONS = 1024

# Offline mode: deterministic fake embeddings + heuristic lesson classifier,
# so the memory layer (and its conflict tests) run without AWS credentials.
OFFLINE = os.environ.get("TRIBUTARY_OFFLINE", "") not in ("", "0", "false")
