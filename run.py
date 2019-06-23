# encoding: utf-8


import os
import sys
import argh
import random
import logging
import asyncio
import traceback
import itertools
import tracemalloc

from datetime import datetime, date
from collections import defaultdict
from functools import wraps
from concurrent.futures._base import CancelledError
from asyncio import Task

import ujson
import aiohttp
import aiofiles

from websockets.exceptions import ConnectionClosed
from websockets import WebSocketCommonProtocol

from sanic import Sanic, response
from sanic.exceptions import NotFound
from sanic.log import LOGGING_CONFIG_DEFAULTS
from sanic.response import json

from sanic_jinja2 import SanicJinja2

from peewee import fn
from playhouse.shortcuts import model_to_dict

from models import Repo, Job, db, Worker
from schedule import always_relaunch, once_per_day

LOGGING_CONFIG_DEFAULTS["loggers"] = {
    "task": {
        "level": "INFO",
        "handlers": ["task_console"],
    },
    "api": {
        "level": "INFO",
        "handlers": ["api_console"],
    },
}

LOGGING_CONFIG_DEFAULTS["handlers"] = {
    "api_console": {
        "class": "logging.StreamHandler",
        "formatter": "api",
        "stream": sys.stdout,
    },
    "task_console": {
        "class": "logging.StreamHandler",
        "formatter": "background",
        "stream": sys.stdout,
    }
}

LOGGING_CONFIG_DEFAULTS["formatters"] = {
    "background": {
        "format": "%(asctime)s [%(process)d] [BACKGROUND] [%(funcName)s] %(message)s",
        "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
        "class": "logging.Formatter",
    },
    "api": {
        "format": "%(asctime)s [%(process)d] [API] [%(funcName)s] %(message)s",
        "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
        "class": "logging.Formatter",
    },
}

task_logger = logging.getLogger("task")
api_logger = logging.getLogger("api")

app = Sanic()
app.static('/static', './static/')

jinja = SanicJinja2(app)

# to avoid conflict with vue.js
jinja.env.block_start_string = '<%'
jinja.env.block_end_string = '%>'
jinja.env.variable_start_string = '<{'
jinja.env.variable_end_string = '}>'
jinja.env.comment_start_string = '<#'
jinja.env.comment_end_string = '#>'

APPS_LISTS = {
    "Apps": "https://app.yunohost.org/apps.json",
}

subscriptions = defaultdict(list)

# this will have the form:
# jobs_in_memory_state = {
#     some_job_id: {"worker": some_worker_id, "task": some_aio_task},
# }
jobs_in_memory_state = {}


@asyncio.coroutine
def wait_closed(self):
    """
    Wait until the connection is closed.

    This is identical to :attr:`closed`, except it can be awaited.

    This can make it easier to handle connection termination, regardless
    of its cause, in tasks that interact with the WebSocket connection.

    """
    yield from asyncio.shield(self.connection_lost_waiter)


# this is a backport of websockets 7.0 which sanic doesn't support yet
WebSocketCommonProtocol.wait_closed = wait_closed


def reset_pending_jobs():
    Job.update(state="scheduled", log="").where(Job.state == "running").execute()


def reset_busy_workers():
    # XXX when we'll have distant workers that might break those
    Worker.update(state="available").execute()


def merge_jobs_on_startup():
    task_logger.info(f"looks for jobs to merge on startup")

    query = Job.select().where(Job.state == "scheduled").order_by(Job.name, -Job.id)

    name_to_jobs = defaultdict(list)

    for job in query:
        name_to_jobs[job.name].append(job)

    for jobs in name_to_jobs.values():
        # keep oldest job

        if jobs[:-1]:
            task_logger.info(f"Merging {jobs[0].name} jobs...")

        for to_delete in jobs[:-1]:
            to_delete.delete_instance()

            task_logger.info(f"* delete {to_delete.name} [{to_delete.id}]")


def set_random_day_for_monthy_job():
    for repo in Repo.select().where((Repo.random_job_day == None)):
        repo.random_job_day = random.randint(1, 28)
        task_logger.info(f"set random day for montly job of repo '{repo.name}' at '{repo.random_job_day}'")
        repo.save()


async def create_job(app_id, app_list_name, repo, job_command_last_part):
    if isinstance(job_command_last_part, str):
        job_name = f"{app_id} ({app_list_name})" + job_command_last_part

        # avoid scheduling twice
        if Job.select().where(Job.name == job_name, Job.state == "scheduled").count() > 0:
            task_logger.info(f"a job for '{job_name} is already scheduled, don't add another one")
            return

        job = Job.create(
            name=job_name,
            url_or_path=repo.url,
            state="scheduled",
        )

        await broadcast({
            "action": "new_job",
            "data": model_to_dict(job),
        }, "jobs")

    else:
        for i in job_command_last_part:
            job_name = f"{app_id} ({app_list_name})" + i

            # avoid scheduling twice
            if Job.select().where(Job.name == job_name, Job.state == "scheduled").count() > 0:
                task_logger.info(f"a job for '{job_name} is already scheduled, don't add another one")
                continue

            job = Job.create(
                name=job_name,
                url_or_path=repo.url,
                state="scheduled",
            )

            await broadcast({
                "action": "new_job",
                "data": model_to_dict(job),
            }, "jobs")


@always_relaunch(sleep=60 * 5)
async def monitor_apps_lists(type="stable", dont_monitor_git=False):
    "parse apps lists every hour or so to detect new apps"

    job_command_last_part = ""
    if type == "arm":
        job_command_last_part = " (~ARM~)"
    elif type == "testing-unstable":
        job_command_last_part = [" (testing)", " (unstable)"]

    # only support github for now :(
    async def get_master_commit_sha(url):
        command = await asyncio.create_subprocess_shell(f"git ls-remote {url} master", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data = await command.stdout.read()
        commit_sha = data.decode().strip().replace("\t", " ").split(" ")[0]
        return commit_sha

    for app_list_name, url in APPS_LISTS.items():
        async with aiohttp.ClientSession() as session:
            task_logger.info(f"Downloading {app_list_name}.json...")
            async with session.get(url) as resp:
                data = await resp.json()

        repos = {x.name: x for x in Repo.select().where(Repo.app_list == app_list_name)}

        for app_id, app_data in data.items():
            commit_sha = await get_master_commit_sha(app_data["git"]["url"])

            if app_data["state"] not in ("working", "validated"):
                task_logger.debug(f"skip {app_id} because state is {app_data['state']}")
                continue

            # already know, look to see if there is new commits
            if app_id in repos:
                repo = repos[app_id]

                # but first check if the URL has changed
                if repo.url != app_data["git"]["url"]:
                    task_logger.info(f"Application {app_id} has changed of url from {repo.url} to {app_data['git']['url']}")

                    repo.url = app_data["git"]["url"]
                    repo.save()

                    await broadcast({
                        "action": "update_app",
                        "data": model_to_dict(repo),
                    }, "apps")

                    # change the url of all jobs that used to have this URL I
                    # guess :/
                    # this isn't perfect because that could overwrite added by
                    # hand jobs but well...
                    for job in Job.select().where(Job.url_or_path == repo.url, Job.state == "scheduled"):
                        job.url_or_path = repo.url
                        job.save()

                        task_logger.info(f"Updating job {job.name} #{job.id} for {app_id} to {repo.url} since the app has changed of url")

                        await broadcast({
                            "action": "update_job",
                            "data": model_to_dict(job),
                        }, ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"])

                # we don't want to do anything else
                if dont_monitor_git:
                    continue

                repo_is_updated = False
                if repo.revision != commit_sha:
                    task_logger.info(f"Application {app_id} has new commits on github "
                                     f"({repo.revision} → {commit_sha}), schedule new job")
                    repo.revision = commit_sha
                    repo.save()
                    repo_is_updated = True

                    await create_job(app_id, app_list_name, repo, job_command_last_part)

                repo_state = "working" if app_data["state"] in ("working", "validated") else "other_than_working"

                if repo.state != repo_state:
                    repo.state = repo_state
                    repo.save()
                    repo_is_updated = True

                if repo.random_job_day is None:
                    repo.random_job_day = random.randint(1, 28)
                    repo.save()
                    repo_is_updated = True

                if repo_is_updated:
                    await broadcast({
                        "action": "update_app",
                        "data": model_to_dict(repo),
                    }, "apps")

            # new app
            elif app_id not in repos:
                task_logger.info(f"New application detected: {app_id} in {app_list_name}" + (", scheduling a new job" if not dont_monitor_git else ""))
                repo = Repo.create(
                    name=app_id,
                    url=app_data["git"]["url"],
                    revision=commit_sha,
                    app_list=app_list_name,
                    state="working" if app_data["state"] in ("working", "validated") else "other_than_working",
                    random_job_day=random.randint(1, 28),
                )

                await broadcast({
                    "action": "new_app",
                    "data": model_to_dict(repo),
                }, "apps")

                if not dont_monitor_git:
                    await create_job(app_id, app_list_name, repo, job_command_last_part)

            await asyncio.sleep(3)

        # delete apps removed from the list
        unseen_repos = set(repos.keys()) - set(data.keys())

        for repo_name in unseen_repos:
            repo = repos[repo_name]

            # delete scheduled jobs first
            task_logger.info(f"Application {repo_name} has been removed from the app list, start by removing its scheduled job if there are any...")
            for job in Job.select().where(Job.url_or_path == repo.url, Job.state == "scheduled"):
                await api_stop_job(None, job.id)  # not sure this is going to work
                job_id = job.id

                task_logger.info(f"Delete scheduled job {job.name} #{job.id} for application {repo_name} because the application is being deleted.")

                data = model_to_dict(job)
                job.delete_instance()

                await broadcast({
                    "action": "delete_job",
                    "data": data,
                }, ["jobs", f"job-{job_id}", f"app-jobs-{job.url_or_path}"])

            task_logger.info(f"Delete application {repo_name} because it has been removed from the {app_list_name} apps list.")
            data = model_to_dict(repo)
            repo.delete_instance()

            await broadcast({
                "action": "delete_app",
                "data": data,
            }, "apps")


@once_per_day
async def launch_monthly_job(type):
    # XXX DRY
    job_command_last_part = ""
    if type == "arm":
        job_command_last_part = " (~ARM~)"
    elif type == "testing-unstable":
        job_command_last_part = [" (testing)", " (unstable)"]

    today = date.today().day

    for repo in Repo.select().where(Repo.random_job_day == today):
        task_logger.info(f"Launch montly job for {repo.name} on day {today} of the month ")
        await create_job(repo.name, repo.app_list, repo, job_command_last_part)


@always_relaunch(sleep=3)
async def jobs_dispatcher():
    if Worker.select().count() == 0:
        for i in range(1):
            Worker.create(state="available")

    workers = Worker.select().where(Worker.state == "available")

    # no available workers, wait
    if workers.count() == 0:
        return

    with db.atomic('IMMEDIATE'):
        jobs = Job.select().where(Job.state == "scheduled")

        # no jobs to process, wait
        if jobs.count() == 0:
            await asyncio.sleep(3)
            return

        for i in range(min(workers.count(), jobs.count())):
            job = jobs[i]
            worker = workers[i]

            job.state = "running"
            job.started_time = datetime.now()
            job.save()

            worker.state = "busy"
            worker.save()

            jobs_in_memory_state[job.id] = {
                "worker": worker.id,
                "task": asyncio.ensure_future(run_job(worker, job)),
            }


async def run_job(worker, job):
    path_to_analyseCI = app.config.path_to_analyseCI

    await broadcast({
        "action": "update_job",
        "data": model_to_dict(job),
    }, ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"])

    # fake stupid command, whould run CI instead
    task_logger.info(f"Starting job '{job.name}' #{job.id}...")

    cwd = os.path.split(path_to_analyseCI)[0]
    arguments = f' {job.url_or_path} "{job.name}"'
    task_logger.info(f"Launch command: /bin/bash " + path_to_analyseCI + arguments)
    try:
        command = await asyncio.create_subprocess_shell("/bin/bash " + path_to_analyseCI + arguments,
                                                        cwd=cwd,
                                                        # default limit is not enough in some situations
                                                        limit=(2 ** 16) ** 10,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)

        while not command.stdout.at_eof():
            data = await command.stdout.readline()

            job.log += data.decode()
            # XXX seems to be okay performance wise but that's probably going to be
            # a bottleneck at some point :/
            # theoritically jobs are going to have slow output
            job.save()

            await broadcast({
                "action": "update_job",
                "id": job.id,
                "data": model_to_dict(job),
            }, ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"])

    except CancelledError:
        job.log += "\n"
        job.end_time = datetime.now()
        job.state = "canceled"
        job.save()

        task_logger.info(f"Job '{job.name} #{job.id}' has been canceled")
    except Exception:
        traceback.print_exc()
        task_logger.exception(f"ERROR in job '{job.name} #{job.id}'")

        job.log += "\n"
        job.log += "Job error on:\n"
        job.log += traceback.format_exc()

        job.end_time = datetime.now()
        job.state = "error"
        job.save()

        # XXX add mechanism to reschedule error jobs

    else:
        task_logger.info(f"Finished job '{job.name}'")

        await command.wait()
        job.end_time = datetime.now()
        job.state = "done" if command.returncode == 0 else "failure"
        job.save()

    # remove ourself from the state
    del jobs_in_memory_state[job.id]

    worker.state = "available"
    worker.save()

    await broadcast({
        "action": "update_job",
        "id": job.id,
        "data": model_to_dict(job),
    }, ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"])


async def broadcast(message, channels):
    if not isinstance(channels, (list, tuple)):
        channels = [channels]

    for channel in channels:
        ws_list = subscriptions[channel]
        dead_ws = []

        for ws in ws_list:
            try:
                await ws.send(ujson.dumps(message))
            except ConnectionClosed:
                dead_ws.append(ws)

    for to_remove in dead_ws:
        ws_list.remove(to_remove)


def subscribe(ws, channel):
    subscriptions[channel].append(ws)


def unsubscribe_all(ws):
    for channel in subscriptions:
        if ws in subscriptions[channel]:
            if ws in subscriptions[channel]:
                print(f"\033[1;36mUnsubscribe ws {ws} from {channel}\033[0m")
                subscriptions[channel].remove(ws)


def clean_websocket(function):
    @wraps(function)
    async def _wrap(request, websocket, *args, **kwargs):
        try:
            to_return = await function(request, websocket, *args, **kwargs)
            return to_return
        except Exception:
            print(function.__name__)
            unsubscribe_all(websocket)
            raise

    return _wrap


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    chunk = []
    a = 0

    for i in l:
        if a < n:
            a += 1
            chunk.append(i)
        else:
            yield chunk
            chunk = []
            a = 0


@app.websocket('/index-ws')
@clean_websocket
async def ws_index(request, websocket):
    subscribe(websocket, "jobs")

    # avoid fetch "log" field from the db to reduce memory usage
    selected_fields = (
        Job.id,
        Job.name,
        Job.url_or_path,
        Job.state,
        Job.created_time,
        Job.started_time,
        Job.end_time,
    )

    JobAlias = Job.alias()
    subquery = JobAlias.select(*selected_fields)\
                       .where(JobAlias.state << ("done", "failure", "canceled", "error"))\
                       .group_by(JobAlias.url_or_path)\
                       .select(fn.Max(JobAlias.id).alias("max_id"))

    latest_done_jobs = Job.select(*selected_fields)\
                          .join(subquery, on=(Job.id == subquery.c.max_id))\
                          .order_by(-Job.id)

    subquery = JobAlias.select(*selected_fields)\
                       .where(JobAlias.state == "scheduled")\
                       .group_by(JobAlias.url_or_path)\
                       .select(fn.Min(JobAlias.id).alias("min_id"))

    next_scheduled_jobs = Job.select(*selected_fields)\
                             .join(subquery, on=(Job.id == subquery.c.min_id))\
                             .order_by(-Job.id)

    # chunks initial data by batch of 30 to avoid killing firefox
    data = chunks(itertools.chain(map(model_to_dict, next_scheduled_jobs.iterator()),
                                  map(model_to_dict, Job.select().where(Job.state == "running").iterator()),
                                  map(model_to_dict, latest_done_jobs.iterator())), 30)

    await websocket.send(ujson.dumps({
        "action": "init_jobs",
        "data": next(data),  # send first chunk
    }))

    for chunk in data:
        await websocket.send(ujson.dumps({
            "action": "init_jobs_stream",
            "data": chunk,
        }))

    await websocket.wait_closed()


@app.websocket('/job-<job_id>-ws')
@clean_websocket
async def ws_job(request, websocket, job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count == 0:
        raise NotFound()

    job = job[0]

    subscribe(websocket, f"job-{job.id}")

    await websocket.send(ujson.dumps({
        "action": "init_job",
        "data": model_to_dict(job),
    }))

    await websocket.wait_closed()


@app.websocket('/apps-ws')
@clean_websocket
async def ws_apps(request, websocket):
    subscribe(websocket, "jobs")
    subscribe(websocket, "apps")

    # I need to do this because peewee strangely fuck up on join and remove the
    # subquery fields which breaks everything
    repos = Repo.raw('''
    SELECT
        *
    FROM
        "repo" AS "t1"
    INNER JOIN (
        SELECT
            "t1"."id" as "job_id",
            "t1"."name" as "job_name",
            "t1"."url_or_path",
            "t1"."state" as "job_state",
            "t1"."log",
            "t1"."created_time",
            "t1"."started_time",
            "t1"."end_time"
        FROM
            "job" AS "t1"
        INNER JOIN (
            SELECT
                Max("t2"."id") AS "max_id"
            FROM
                "job" AS "t2"
            GROUP BY
                "t2"."url_or_path"
        )
        AS
            "t3"
        ON
            ("t1"."id" = "t3"."max_id")
    ) AS
        "t5"
    ON
        ("t5"."url_or_path" = "t1"."url")
    ORDER BY
        "name"
    ''')

    repos = [
        {
            "id": x.id,
            "name": x.name,
            "url": x.url,
            "revision": x.revision,
            "app_list": x.app_list,
            "state": x.state,
            "random_job_day": x.random_job_day,
            "job_id": x.job_id,
            "job_name": x.job_name,
            "job_state": x.job_state,
            "log": x.log,
            "created_time": datetime.strptime(x.created_time.split(".")[0], '%Y-%m-%d %H:%M:%S') if x.created_time else None,
            "started_time": datetime.strptime(x.started_time.split(".")[0], '%Y-%m-%d %H:%M:%S') if x.started_time else None,
            "end_time": datetime.strptime(x.end_time.split(".")[0], '%Y-%m-%d %H:%M:%S') if x.end_time else None,
        } for x in repos
    ]

    # add apps without jobs
    selected_repos = {x["name"] for x in repos}
    for repo in Repo.select():
        if repo.id in selected_repos:
            continue

        repos.append({
            "id": repo.id,
            "name": repo.name,
            "url": repo.url,
            "revision": repo.revision,
            "app_list": repo.app_list,
            "state": repo.state,
            "random_job_day": repo.random_job_day,
            "job_id": None,
            "job_name": None,
            "job_state": None,
            "log": None,
            "created_time": None,
            "started_time": None,
            "end_time": None,
        })

    repos = sorted(repos, key=lambda x: x["name"])

    await websocket.send(ujson.dumps({
        "action": "init_apps",
        "data": repos,
    }))

    await websocket.wait_closed()


@app.websocket('/app-<app_name>-ws')
@clean_websocket
async def ws_app(request, websocket, app_name):
    # XXX I don't check if the app exists because this websocket is supposed to
    # be only loaded from the app page which does this job already
    app = Repo.select().where(Repo.name == app_name)[0]

    subscribe(websocket, f"app-jobs-{app.url}")

    await websocket.send(ujson.dumps({
        "action": "init_jobs",
        "data": Job.select().where(Job.url_or_path ==
                                   app.url).order_by(-Job.id),
    }))

    await websocket.wait_closed()


def require_token():
    def decorator(f):
        @wraps(f)
        async def decorated_function(request, *args, **kwargs):
            # run some method that checks the request
            # for the client's authorization status
            if "X-Token" not in request.headers:
                return response.json({'status': 'you need to provide a token '
                                                'to access the API, please '
                                                'refer to the README'}, 403)

            if not os.path.exists("tokens"):
                api_logger.warning("No tokens available and a user is trying "
                                   "to access the API")
                return response.json({'status': 'invalide token'}, 403)

            async with aiofiles.open('tokens', mode='r') as file:
                tokens = await file.read()
                tokens = {x.strip() for x in tokens.split("\n") if x.strip()}

            token = request.headers["X-Token"].strip()

            if token not in tokens:
                api_logger.warning(f"someone tried to access the API using "
                                   "the {token} but it's not a valid token in "
                                   "the 'tokens' file")
                return response.json({'status': 'invalide token'}, 403)

            result = await f(request, *args, **kwargs)
            return result
        return decorated_function
    return decorator


@app.route("/api/job", methods=['POST'])
@require_token()
async def api_new_job(request):
    job = Job.create(
        name=request.json["name"],
        url_or_path=request.json["url_or_path"],
        created_time=datetime.now(),
    )

    api_logger.info(f"Request to add new job '{job.name}' [{job.id}]")

    await broadcast({
        "action": "new_job",
        "data": model_to_dict(job),
    }, ["jobs", f"app-jobs-{job.url_or_path}"])

    return response.text("ok")


@app.route("/api/job", methods=['GET'])
@require_token()
async def api_list_job(request):
    query = Job.select()

    if not all:
        query.where(Job.state in ('scheduled', 'running'))

    return response.json([model_to_dict(x) for x in query.order_by(-Job.id)])


@app.route("/api/app", methods=['GET'])
@require_token()
async def api_list_app(request):
    query = Repo.select()

    return response.json([model_to_dict(x) for x in query.order_by(Repo.name)])


@app.route("/api/job/<job_id:int>", methods=['DELETE'])
@require_token()
async def api_delete_job(request, job_id):
    api_logger.info(f"Request to restart job {job_id}")
    # need to stop a job before deleting it
    await api_stop_job(request, job_id)

    # no need to check if job exist, api_stop_job will do it for us
    job = Job.select().where(Job.id == job_id)[0]

    api_logger.info(f"Request to delete job '{job.name}' [{job.id}]")

    data = model_to_dict(job)
    job.delete_instance()

    await broadcast({
        "action": "delete_job",
        "data": data,
    }, ["jobs", f"job-{job_id}", f"app-jobs-{job.url_or_path}"])

    return response.text("ok")


@app.route("/api/job/<job_id:int>/stop", methods=['POST'])
async def api_stop_job(request, job_id):
    # TODO auth or some kind
    job = Job.select().where(Job.id == job_id)

    if job.count() == 0:
        raise NotFound(f"Error: no job with the id '{job_id}'")

    job = job[0]

    api_logger.info(f"Request to stop job '{job.name}' [{job.id}]")

    if job.state == "scheduled":
        api_logger.info(f"Cancel scheduled job '{job.name}' [job.id] "
                        f"on request")
        job.state = "canceled"
        job.save()

        await broadcast({
            "action": "update_job",
            "data": model_to_dict(job),
        }, ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"])

        return response.text("ok")

    if job.state == "running":
        api_logger.info(f"Cancel running job '{job.name}' [job.id] on request")
        job.state = "canceled"
        job.end_time = datetime.now()
        job.save()

        jobs_in_memory_state[job.id]["task"].cancel()

        worker = Worker.select().where(Worker.id == jobs_in_memory_state[job.id]["worker"])[0]
        worker.state = "available"
        worker.save()

        await broadcast({
            "action": "update_job",
            "data": model_to_dict(job),
        }, ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"])

        return response.text("ok")

    if job.state in ("done", "canceled", "failure", "error"):
        api_logger.info(f"Request to cancel job '{job.name}' "
                        f"[job.id] but job is already in '{job.state}' state, "
                        f"do nothing")
        # nothing to do, task is already done
        return response.text("ok")

    raise Exception(f"Tryed to cancel a job with an unknown state: "
                    f"{job.state}")


@app.route("/api/job/<job_id:int>/restart", methods=['POST'])
async def api_restart_job(request, job_id):
    api_logger.info(f"Request to restart job {job_id}")
    await api_stop_job(request, job_id)

    # no need to check if job existss, api_stop_job will do it for us
    job = Job.select().where(Job.id == job_id)[0]
    job.state = "scheduled"
    job.log = ""
    job.save()

    await broadcast({
        "action": "update_job",
        "data": model_to_dict(job),
    }, ["jobs", f"job-{job_id}", f"app-jobs-{job.url_or_path}"])

    return response.text("ok")


@app.route('/job/<job_id>')
@jinja.template('job.html')
async def html_job(request, job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count == 0:
        raise NotFound()

    job = job[0]

    app = Repo.select().where(Repo.url == job.url_or_path)
    app = app[0] if app else None

    return {
        "job": job,
        'app': app,
        'relative_path_to_root': '../',
        'path': request.path
    }


@app.route('/apps/')
@jinja.template('apps.html')
async def html_apps(request):
    return {'relative_path_to_root': '../', 'path': request.path}


@app.route('/apps/<app_name>/')
@jinja.template('app.html')
async def html_app(request, app_name):
    app = Repo.select().where(Repo.name == app_name)

    if app.count == 0:
        raise NotFound()

    return {"app": app[0], 'relative_path_to_root': '../../', 'path': request.path}


@app.route('/')
@jinja.template('index.html')
async def html_index(request):
    return {'relative_path_to_root': '', 'path': request.path}


@always_relaunch(sleep=2)
async def number_of_tasks():
    print("Number of tasks: %s" % len(Task.all_tasks()))


@app.route('/monitor')
async def monitor(request):
    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics('lineno')

    tasks = Task.all_tasks()

    return json({
        "top_20_trace": [str(x) for x in top_stats[:20]],
        "tasks": {
            "number": len(tasks),
            "array": map(show_coro, tasks),
        }
    })


def show_coro(c):
    data = {
        'txt': str(c),
        'type': str(type(c)),
        'done': c.done(),
        'cancelled': False,
        'stack': None,
        'exception': None,
    }
    if not c.done():
        data['stack'] = [format_frame(x) for x in c.get_stack()]
    else:
        if c.cancelled():
            data['cancelled'] = True
        else:
            data['exception'] = str(c.exception())

    return data


def format_frame(f):
    keys = ['f_code', 'f_lineno']
    return dict([(k, str(getattr(f, k))) for k in keys])


@argh.arg('-t', '--type', choices=['stable', 'arm', 'testing-unstable', 'dev'], default="stable")
def main(path_to_analyseCI, ssl=False, keyfile_path="/etc/yunohost/certs/ci-apps.yunohost.org/key.pem", certfile_path="/etc/yunohost/certs/ci-apps.yunohost.org/crt.pem", type="stable", dont_minotor_apps_list=False, dont_monitor_git=False, no_monthly_jobs=False, port=4242, debug=False):
    if not os.path.exists(path_to_analyseCI):
        print(f"Error: analyseCI script doesn't exist at '{path_to_analyseCI}'")
        sys.exit(1)

    reset_pending_jobs()
    reset_busy_workers()
    merge_jobs_on_startup()

    set_random_day_for_monthy_job()

    app.config.path_to_analyseCI = path_to_analyseCI

    if not dont_minotor_apps_list:
        app.add_task(monitor_apps_lists(type=type,
                                        dont_monitor_git=dont_monitor_git))

    if not no_monthly_jobs:
        app.add_task(launch_monthly_job(type=type))

    app.add_task(jobs_dispatcher())
    app.add_task(number_of_tasks())

    if not ssl:
        app.run('localhost', port=port, debug=debug)
    else:
        import ssl
        context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile_path, keyfile=keyfile_path)

        app.run('0.0.0.0', port=port, ssl=context, debug=debug)


if __name__ == "__main__":
    tracemalloc.start()
    argh.dispatch_command(main)
