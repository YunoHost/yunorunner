"""Replace"""

import peewee as pw
from peewee_migrate import Migrator


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False) -> None:
    if fake:
        return

    migrator.sql("UPDATE repo SET name = lower(name)")
    migrator.sql("UPDATE repo SET url = lower(url)")

    migrator.sql("UPDATE job SET name = lower(name)")
    migrator.sql("UPDATE job SET url_or_path = lower(url_or_path)")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False) -> None:
    """No rollback."""
