"""Create the Tributary schema on the cluster from DATABASE_URL.

    python scripts/init_db.py
"""

from tributary.db import init_schema


def main():
    init_schema()
    print("Schema created: agents, lessons (+ vector index), memory_audit")


if __name__ == "__main__":
    main()
