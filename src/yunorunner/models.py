from typing import TYPE_CHECKING

import peewee

db = peewee.SqliteDatabase("db.sqlite")


class Repo(peewee.Model):
    if TYPE_CHECKING:
        id: int

    name = peewee.CharField()  # TODO make this uniq/index
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

    class Meta:
        database = db


class Job(peewee.Model):
    if TYPE_CHECKING:
        id: int

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

    class Meta:
        database = db


class Worker(peewee.Model):
    if TYPE_CHECKING:
        id: int

    state = peewee.CharField(
        choices=(
            ("available", "Available"),
            ("busy", "Busy"),
        )
    )

    class Meta:
        database = db
