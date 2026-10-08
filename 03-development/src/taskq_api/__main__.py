"""Command line entry point: `python -m taskq_api key create --scope <scope>`.

[FR-03] Citations: SPEC.md:105.
"""
import argparse
import os

from taskq_api.repository.session import create_db_engine, session_scope
from taskq_api.service import keys


def main() -> None:
    """Run the CLI."""
    parser = argparse.ArgumentParser(prog="taskq_api")
    sub = parser.add_subparsers(dest="group", required=True)
    key = sub.add_parser("key").add_subparsers(dest="action", required=True)
    create = key.add_parser("create")
    create.add_argument("--scope", required=True, choices=["read", "write", "admin"])
    args = parser.parse_args()
    engine = create_db_engine(os.environ["TASKQ_DB_URL"])
    with session_scope(engine) as session:
        print(keys.create_key(session, args.scope))
    engine.dispose()


if __name__ == "__main__":
    main()
