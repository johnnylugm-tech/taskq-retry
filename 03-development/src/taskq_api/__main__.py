"""Command line entry point: `python -m taskq_api key create --scope <scope>` and `serve`.

[FR-03] Citations: SPEC.md:105.
[NFR-02] Citations: SPEC.md:54, SPEC.md:301-302 (`serve` listens on TASKQ_HOST:TASKQ_PORT).
"""
import argparse
import os

import uvicorn

from taskq_api import config
from taskq_api.app import configure_logging
from taskq_api.repository.session import create_db_engine, session_scope
from taskq_api.service import keys


def serve() -> None:
    """Run the ASGI service on TASKQ_HOST:TASKQ_PORT with the configured logging. [NFR-02]"""
    host, port = config.host(), config.port()
    configure_logging(config.log_level(), config.log_format())
    uvicorn.run("taskq_api.app:create_app", factory=True, host=host, port=port, log_config=None)


def main() -> None:
    """Run the CLI."""
    parser = argparse.ArgumentParser(prog="taskq_api")
    sub = parser.add_subparsers(dest="group", required=True)
    sub.add_parser("serve")
    key = sub.add_parser("key").add_subparsers(dest="action", required=True)
    create = key.add_parser("create")
    create.add_argument("--scope", required=True, choices=["read", "write", "admin"])
    args = parser.parse_args()
    if args.group == "serve":
        serve()
        return
    engine = create_db_engine(os.environ["TASKQ_DB_URL"])
    with session_scope(engine) as session:
        print(keys.create_key(session, args.scope))
    engine.dispose()


if __name__ == "__main__":
    main()
