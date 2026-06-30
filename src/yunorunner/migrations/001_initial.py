"""Initial migration - DB creation"""

import peewee
from peewee_migrate import Migrator


def migrate(
    migrator: Migrator, database: peewee.Database, *, fake: bool = False
) -> None:
    @migrator.create_model
    class Repo(peewee.Model):
        name = peewee.CharField()
        url = peewee.CharField()
        revision = peewee.CharField(null=True)
        state = peewee.CharField(
            choices=(
                ("working", "Working"),
                ("other_than_working", "Other than working"),
            ),
            default="other_than_working",
        )

        random_job_day = peewee.IntegerField(null=True)

    @migrator.create_model
    class Job(peewee.Model):
        name = peewee.CharField()
        url_or_path = peewee.CharField()

        state = peewee.CharField(
            choices=(
                ("scheduled", "Scheduled"),
                ("runnning", "Running"),
                ("done", "Done"),
                ("failure", "Failure"),
                ("error", "Error"),
                ("canceled", "Canceled"),
            ),
            default="scheduled",
        )

        log = peewee.TextField(default="")

        created_time = peewee.DateTimeField(
            constraints=[peewee.SQL("DEFAULT (datetime('now'))")]
        )
        started_time = peewee.DateTimeField(null=True)
        end_time = peewee.DateTimeField(null=True)

    @migrator.create_model
    class Worker(peewee.Model):
        state = peewee.CharField(
            choices=(
                ("available", "Available"),
                ("busy", "Busy"),
            )
        )


def rollback(
    migrator: Migrator, database: peewee.Database, *, fake: bool = False
) -> None:
    """Write your rollback migrations here."""
    migrator.remove_model("repo")
    migrator.remove_model("job")
    migrator.remove_model("worker")
