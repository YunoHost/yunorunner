import peewee

db = peewee.SqliteDatabase('db.sqlite')


class Repo(peewee.Model):
    name = peewee.CharField()
    url = peewee.CharField()
    revision = peewee.CharField(null=True)
    app_list = peewee.CharField(null=True)

    state = peewee.CharField(choices=(
        ('working', 'Working'),
        ('other_than_working', 'Other than working'),
    ), default="other_than_working")

    random_job_day = peewee.IntegerFieldIntegerField(null=True)

    class Meta:
        database = db


class Job(peewee.Model):
    name = peewee.CharField()
    url_or_path = peewee.CharField()

    state = peewee.CharField(choices=(
        ('scheduled', 'Scheduled'),
        ('runnning', 'Running'),
        ('done', 'Done'),
        ('failure', 'Failure'),
        ('canceled', 'Canceled'),
    ), default="scheduled")

    log = peewee.TextField(default="")

    created_time = peewee.DateTimeField(constraints=[peewee.SQL("DEFAULT (datetime('now'))")])
    started_time = peewee.DateTimeField(null=True)
    end_time = peewee.DateTimeField(null=True)

    class Meta:
        database = db


class Worker(peewee.Model):
    state = peewee.CharField(choices=(
        ('available', 'Available'),
        ('busy', 'Busy'),
    ))

    class Meta:
        database = db


# peewee is a bit stupid and will crash if the table already exists
for i in [Repo, Job, Worker]:
    try:
        i.create_table()
    except:
        pass
