"""[FR-07] Alembic environment (online and offline SQL modes).

Citations: SPEC.md:130-143.
"""
from alembic import context
from sqlalchemy import create_engine

config = context.config
url = config.get_main_option("sqlalchemy.url")

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
