import peewee as pw
from peewee_migrate import Migrator
from playhouse.reflection import generate_models, print_model

from yunorunner.run import job_logfile


def migrate(migrator: Migrator, database: pw.Database, *, fake: bool = False) -> None:
    """Write your migrations here."""
    models = generate_models(database)
    Job = models["job"]  # noqa: N806

    for job in Job.select():
        job_logfile(job).write_text(job.log)

    migrator.remove_fields("job", "log")


def rollback(migrator: Migrator, database: pw.Database, *, fake: bool = False) -> None:
    """Write your rollback migrations here."""
