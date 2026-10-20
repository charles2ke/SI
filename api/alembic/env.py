from alembic import context
from sqlalchemy import text

from si.db import Base, engine

if context.is_offline_mode():
    context.configure(url=str(engine.url), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.commit()
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
