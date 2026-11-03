"""[FR-07] Alembic environment (online and offline SQL modes).

Citations: SPEC.md:130-143.
"""
from alembic import context
from sqlalchemy import create_engine, event

config = context.config
url = config.get_main_option("sqlalchemy.url")

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    if url is None:
        raise RuntimeError("sqlalchemy.url is not configured")
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        # pysqlite autocommits before DDL; take over BEGIN so a failed migration rolls back whole. [NFR-03]
        @event.listens_for(engine, "connect")
        def _manual_begin(dbapi_connection, _record):
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _emit_begin(conn):
            conn.exec_driver_sql("BEGIN")

    with engine.connect() as connection:
        context.configure(connection=connection, transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()
