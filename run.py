#!/usr/bin/env python3


import os
import sys
import argh
import random
import logging
import asyncio
import traceback
import itertools
import tracemalloc
import string
import shutil

import hmac
import hashlib

from datetime import datetime, date
from collections import defaultdict
from functools import wraps
from concurrent.futures._base import CancelledError
from asyncio import Task

import json
import aiohttp
import aiofiles

from websockets.exceptions import ConnectionClosed
from websockets import WebSocketCommonProtocol

from sanic import Sanic, response
from sanic.exceptions import NotFound, WebsocketClosed
from sanic.log import LOGGING_CONFIG_DEFAULTS

from jinja2 import FileSystemLoader
from sanic_jinja2 import SanicJinja2

from peewee import fn
from playhouse.shortcuts import model_to_dict

from models import Repo, Job, db, Worker
from schedule import always_relaunch, once_per_day

# This is used by ciclic
admin_token = "".join(random.choices(string.ascii_lowercase + string.digits, k=32))
open(".admin_token", "w").write(admin_token)

try:
    asyncio_all_tasks = asyncio.all_tasks
except AttributeError as e:
    asyncio_all_tasks = asyncio.Task.all_tasks

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
    },
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


def datetime_to_epoch_json_converter(o):
    if isinstance(o, datetime):
        return o.strftime("%s")


# define a custom json dumps to convert datetime
def my_json_dumps(o):
    return json.dumps(o, default=datetime_to_epoch_json_converter)


task_logger = logging.getLogger("task")
api_logger = logging.getLogger("api")

app = Sanic(__name__, dumps=my_json_dumps)
app.static("/static", "./static/")

yunorunner_dir = os.path.abspath(os.path.dirname(__file__))
loader = FileSystemLoader(yunorunner_dir + "/templates", encoding="utf8")
jinja = SanicJinja2(app, loader=loader)

# to avoid conflict with vue.js
jinja.env.block_start_string = "<%"
jinja.env.block_end_string = "%>"
jinja.env.variable_start_string = "<{"
jinja.env.variable_end_string = "}>"
jinja.env.comment_start_string = "<#"
jinja.env.comment_end_string = "#>"

APPS_LIST = "https://app.yunohost.org/default/v3/apps.json"

subscriptions = defaultdict(list)

# this will have the form:
# jobs_in_memory_state = {
#     some_job_id: {"worker": some_worker_id, "task": some_aio_task},
# }
jobs_in_memory_state = {}


async def wait_closed(self):
    """
    Wait until the connection is closed.

    This is identical to :attr:`closed`, except it can be awaited.

    This can make it easier to handle connection termination, regardless
    of its cause, in tasks that interact with the WebSocket connection.

    """
    await asyncio.shield(self.connection_lost_waiter)


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
        task_logger.info(
            f"set random day for monthly job of repo '{repo.name}' at '{repo.random_job_day}'"
        )
        repo.save()


async def create_job(app_id, repo_url, job_comment=""):
    job_name = app_id
    if job_comment:
        job_name += f" ({job_comment})"

    # avoid scheduling twice
    if Job.select().where(Job.name == job_name, Job.state == "scheduled").count() > 0:
        task_logger.info(
            f"a job for '{job_name} is already scheduled, not adding another one"
        )
        return

    job = Job.create(
        name=job_name,
        url_or_path=repo_url,
        state="scheduled",
    )

    await broadcast(
        {
            "action": "new_job",
            "data": model_to_dict(job),
        },
        "jobs",
    )

    return job


@always_relaunch(sleep=60 * 5)
async def monitor_apps_lists(monitor_git=False, monitor_only_good_quality_apps=False):
    "parse apps lists every hour or so to detect new apps"

    # only support github for now :(
    async def get_main_commit_sha(url):
        command = await asyncio.create_subprocess_shell(
            f"git ls-remote {url} main master",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        data = await command.stdout.read()
        commit_sha = data.decode().strip().replace("\t", " ").split(" ")[0]
        return commit_sha

    async with aiohttp.ClientSession() as session:
        task_logger.info(f"Downloading applist...")
        async with session.get(APPS_LIST) as resp:
            data = await resp.json()
            data = data["apps"]

    repos = {x.name: x for x in Repo.select()}

    for app_id, app_data in data.items():
        commit_sha = await get_main_commit_sha(app_data["git"]["url"])

        if app_data["state"] != "working":
            task_logger.debug(f"skip {app_id} because state is {app_data['state']}")
            continue

        if monitor_only_good_quality_apps:
            if app_data.get("level") in [None, "?"] or app_data["level"] <= 4:
                task_logger.debug(f"skip {app_id} because app is not good quality")
                continue

        # already know, look to see if there is new commits
        if app_id in repos:
            repo = repos[app_id]

            # but first check if the URL has changed
            if repo.url != app_data["git"]["url"]:
                task_logger.info(
                    f"Application {app_id} has changed of url from {repo.url} to {app_data['git']['url']}"
                )

                repo.url = app_data["git"]["url"]
                repo.save()

                await broadcast(
                    {
                        "action": "update_app",
                        "data": model_to_dict(repo),
                    },
                    "apps",
                )

                # change the url of all jobs that used to have this URL I
                # guess :/
                # this isn't perfect because that could overwrite added by
                # hand jobs but well...
                for job in Job.select().where(
                    Job.url_or_path == repo.url, Job.state == "scheduled"
                ):
                    job.url_or_path = repo.url
                    job.save()

                    task_logger.info(
                        f"Updating job {job.name} #{job.id} for {app_id} to {repo.url} since the app has changed of url"
                    )

                    await broadcast(
                        {
                            "action": "update_job",
                            "data": model_to_dict(job),
                        },
                        ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
                    )

            # we don't want to do anything else
            if not monitor_git:
                continue

            repo_is_updated = False
            if repo.revision != commit_sha:
                task_logger.info(
                    f"Application {app_id} has new commits on github "
                    f"({repo.revision} → {commit_sha}), schedule new job"
                )
                repo.revision = commit_sha
                repo.save()
                repo_is_updated = True

                await create_job(app_id, repo.url)

            repo_state = (
                "working" if app_data["state"] == "working" else "other_than_working"
            )

            if repo.state != repo_state:
                repo.state = repo_state
                repo.save()
                repo_is_updated = True

            if repo.random_job_day is None:
                repo.random_job_day = random.randint(1, 28)
                repo.save()
                repo_is_updated = True

            if repo_is_updated:
                await broadcast(
                    {
                        "action": "update_app",
                        "data": model_to_dict(repo),
                    },
                    "apps",
                )

        # new app
        elif app_id not in repos:
            task_logger.info(
                f"New application detected: {app_id} "
                + (", scheduling a new job" if monitor_git else "")
            )
            repo = Repo.create(
                name=app_id,
                url=app_data["git"]["url"],
                revision=commit_sha,
                state=(
                    "working"
                    if app_data["state"] == "working"
                    else "other_than_working"
                ),
                random_job_day=random.randint(1, 28),
            )

            await broadcast(
                {
                    "action": "new_app",
                    "data": model_to_dict(repo),
                },
                "apps",
            )

            if monitor_git:
                await create_job(app_id, repo.url)

        await asyncio.sleep(1)

    # delete apps removed from the list
    unseen_repos = set(repos.keys()) - set(data.keys())

    for repo_name in unseen_repos:
        repo = repos[repo_name]

        # delete scheduled jobs first
        task_logger.info(
            f"Application {repo_name} has been removed from the app list, start by removing its scheduled job if there are any..."
        )
        for job in Job.select().where(
            Job.url_or_path == repo.url, Job.state == "scheduled"
        ):
            await api_stop_job(None, job.id)  # not sure this is going to work
            job_id = job.id

            task_logger.info(
                f"Delete scheduled job {job.name} #{job.id} for application {repo_name} because the application is being deleted."
            )

            data = model_to_dict(job)
            job.delete_instance()

            await broadcast(
                {
                    "action": "delete_job",
                    "data": data,
                },
                ["jobs", f"job-{job_id}", f"app-jobs-{job.url_or_path}"],
            )

        task_logger.info(
            f"Delete application {repo_name} because it has been removed from the apps list."
        )
        data = model_to_dict(repo)
        repo.delete_instance()

        await broadcast(
            {
                "action": "delete_app",
                "data": data,
            },
            "apps",
        )


@once_per_day
async def launch_monthly_job():
    today = date.today().day

    for repo in Repo.select().where(Repo.random_job_day == today):
        task_logger.info(
            f"Launch monthly job for {repo.name} on day {today} of the month "
        )
        await create_job(repo.name, repo.url)


async def ensure_workers_count():
    if Worker.select().count() < app.config.WORKER_COUNT:
        for _ in range(app.config.WORKER_COUNT - Worker.select().count()):
            Worker.create(state="available")
    elif Worker.select().count() > app.config.WORKER_COUNT:
        workers_to_remove = Worker.select().count() - app.config.WORKER_COUNT
        workers = Worker.select().where(Worker.state == "available")
        for worker in workers:
            if workers_to_remove == 0:
                break
            worker.delete_instance()
            workers_to_remove -= 1

        jobs_to_stop = workers_to_remove
        for job_id in jobs_in_memory_state:
            if jobs_to_stop == 0:
                break
            await stop_job(job_id)
            jobs_to_stop -= 1
            job = Job.select().where(Job.id == job_id)[0]
            job.state = "scheduled"
            job.log = ""
            job.save()

        workers = Worker.select().where(Worker.state == "available")
        for worker in workers:
            if workers_to_remove == 0:
                break
            worker.delete_instance()
            workers_to_remove -= 1


@always_relaunch(sleep=3)
async def jobs_dispatcher():
    await ensure_workers_count()

    workers = Worker.select().where(Worker.state == "available")

    # no available workers, wait
    if workers.count() == 0:
        return

    with db.atomic("IMMEDIATE"):
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
            job.end_time = None
            job.save()

            worker.state = "busy"
            worker.save()

            jobs_in_memory_state[job.id] = {
                "worker": worker.id,
                "task": asyncio.ensure_future(run_job(worker, job)),
            }


async def cleanup_old_package_check_if_lock_exists(worker, job, ignore_error=False):

    await asyncio.sleep(1)

    if not os.path.exists(
        app.config.PACKAGE_CHECK_LOCK_PER_WORKER.format(worker_id=worker.id)
    ):
        return

    job.log += f"Lock for worker {worker.id} still exist ... trying to cleanup the old package check still running ...\n"
    job.save()
    await broadcast(
        {
            "action": "update_job",
            "id": job.id,
            "data": model_to_dict(job),
        },
        ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
    )

    task_logger.info(
        f"Lock for worker {worker.id} still exist ... trying to cleanup old check process ..."
    )

    cwd = os.path.split(app.config.PACKAGE_CHECK_PATH)[0]
    env = {
        "IN_YUNORUNNER": "1",
        "WORKER_ID": str(worker.id),
        "ARCH": app.config.ARCH,
        "DIST": app.config.DIST,
        "YNH_BRANCH": app.config.YNH_BRANCH,
        "YNHDEV_BACKEND": os.environ.get("YNHDEV_BACKEND", ""),
        "PATH": os.environ["PATH"]
        + ":/usr/local/bin",  # This is because lxc/lxd is in /usr/local/bin
    }

    cmd = f"script -qefc '{app.config.PACKAGE_CHECK_PATH} --force-stop 2>&1'"
    try:
        command = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        while not command.stdout.at_eof():
            data = await command.stdout.readline()
            await asyncio.sleep(1)
    except Exception:
        traceback.print_exc()
        task_logger.exception(f"ERROR in job '{job.name} #{job.id}'")

        job.log += "\n"
        job.log += "Exception:\n"
        job.log += traceback.format_exc()

        if not ignore_error:
            job.state = "error"

        return False
    except (CancelledError, asyncio.exceptions.CancelledError):
        command.terminate()

        if not ignore_error:
            job.log += "\nFailed to kill old check process?"
            job.state = "canceled"

            task_logger.info(f"Job '{job.name} #{job.id}' has been canceled")

        return False
    else:
        job.log += "Cleaning done\n"
        return True
    finally:
        job.save()
        await broadcast(
            {
                "action": "update_job",
                "id": job.id,
                "data": model_to_dict(job),
            },
            ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
        )

        # Dirty hack to kill ~zombi processes adopted by init doing funky stuff -_-
        os.system(
            "for PID in $(ps -ef --forest | grep 'lxc exec' | grep ' 1 ' | awk '{print $2}'); do kill -9 $PID; done"
        )
        os.system(
            "for PID in $(ps -ef --forest | grep 'incus exec' | grep ' 1 ' | awk '{print $2}'); do kill -9 $PID; done"
        )
        os.system(
            "for PID in $(ps -ef --forest | grep 'script -qefc' | grep ' 1 ' | awk '{print $2}' ); do kill $PID; done"
        )


async def run_job(worker, job):

    async def update_github_commit_status(
        app_url, job_url, commit_sha, state, level=None
    ):

        token = app.config.GITHUB_COMMIT_STATUS_TOKEN
        if token is None:
            return

        if state == "canceled":
            state = "error"
        if state == "done":
            state = "success"

        org = app_url.lower().strip("/").replace("https://", "").split("/")[1]
        repo = app_url.lower().strip("/").replace("https://", "").split("/")[2]
        ci_name = app.config.BASE_URL.lower().replace("https://", "").split(".")[0]
        message = f"{ci_name}: "
        if level:
            message += f"level {level}"
        else:
            message += state

        api_url = f"https://api.github.com/repos/{org}/{repo}/statuses/{commit_sha}"

        async with aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        ) as session:
            async with session.post(
                api_url,
                data=my_json_dumps(
                    {
                        "state": state,
                        "target_url": job_url,
                        "description": f"{ci_name}: level {level}",
                        "context": ci_name,
                    }
                ),
            ) as resp:
                respjson = await resp.json()
                if "url" in respjson:
                    api_logger.info(
                        f"Updated commit status for {org}/{repo}/{commit_sha}"
                    )
                else:
                    api_logger.error(
                        f"Failed to update commit status for {org}/{repo}/{commit_sha}"
                    )
                    api_logger.error(respjson)

    await broadcast(
        {
            "action": "update_job",
            "data": model_to_dict(job),
        },
        ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
    )

    await asyncio.sleep(5)

    cleanup_ret = await cleanup_old_package_check_if_lock_exists(worker, job)
    if cleanup_ret is False:
        return

    job_app = job.name.split()[0]

    task_logger.info(f"Starting job '{job.name}' #{job.id}...")

    cwd = os.path.split(app.config.PACKAGE_CHECK_PATH)[0]
    env = {
        "IN_YUNORUNNER": "1",
        "WORKER_ID": str(worker.id),
        "ARCH": app.config.ARCH,
        "DIST": app.config.DIST,
        "YNH_BRANCH": app.config.YNH_BRANCH,
        "YNHDEV_BACKEND": os.environ.get("YNHDEV_BACKEND", ""),
        "PATH": os.environ["PATH"]
        + ":/usr/local/bin",  # This is because lxc/lxd is in /usr/local/bin
    }

    if hasattr(app.config, "STORAGE_PATH"):
        env["YNH_PACKAGE_CHECK_STORAGE_DIR"] = app.config.STORAGE_PATH

    begin = datetime.now()
    begin_human = begin.strftime("%d/%m/%Y - %H:%M:%S")
    msg = (
        begin_human
        + f" - Starting test for {job.name} on arch {app.config.ARCH}, distrib {app.config.DIST}, with YunoHost {app.config.YNH_BRANCH}"
    )
    job.log += "=" * len(msg) + "\n"
    job.log += msg + "\n"
    job.log += "=" * len(msg) + "\n"
    job.save()
    await broadcast(
        {
            "action": "update_job",
            "id": job.id,
            "data": model_to_dict(job),
        },
        ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
    )

    result_json = app.config.PACKAGE_CHECK_RESULT_JSON_PER_WORKER.format(
        worker_id=worker.id
    )
    full_log = app.config.PACKAGE_CHECK_FULL_LOG_PER_WORKER.format(worker_id=worker.id)
    summary_png = app.config.PACKAGE_CHECK_SUMMARY_PNG_PER_WORKER.format(
        worker_id=worker.id
    )

    if os.path.exists(result_json):
        os.remove(result_json)
    if os.path.exists(full_log):
        os.remove(full_log)
    if os.path.exists(summary_png):
        os.remove(summary_png)

    cmd = f"nice --adjustment=10 script -qefc '/bin/bash {app.config.PACKAGE_CHECK_PATH} {job.url_or_path} 2>&1'"
    task_logger.info(f"Launching command: {cmd}")

    try:
        command = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            env=env,
            # default limit is not enough in some situations
            limit=(2**16) ** 10,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        while not command.stdout.at_eof():

            try:
                data = await asyncio.wait_for(command.stdout.readline(), 60)
            except asyncio.TimeoutError:
                if (datetime.now() - begin).total_seconds() > app.config.TIMEOUT:
                    raise Exception(f"Job timed out ({app.config.TIMEOUT / 60} min.)")
            else:
                try:
                    job.log += data.decode("utf-8", "replace")
                except UnicodeDecodeError as e:
                    job.log += "Uhoh ?! UnicodeDecodeError in yunorunner !?"
                    job.log += str(e)

                job.save()

                await broadcast(
                    {
                        "action": "update_job",
                        "id": job.id,
                        "data": model_to_dict(job),
                    },
                    ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
                )

    except (CancelledError, asyncio.exceptions.CancelledError):
        command.terminate()
        job.log += "\n"
        job.state = "canceled"

        level = None

        task_logger.info(f"Job '{job.name} #{job.id}' has been canceled")
    except Exception:
        traceback.print_exc()
        task_logger.exception(f"ERROR in job '{job.name} #{job.id}'")

        level = None

        job.log += "\n"
        job.log += "Job error on:\n"
        job.log += traceback.format_exc()

        job.state = "error"
    else:
        task_logger.info(f"Finished job '{job.name}'")

        if command.returncode == 124:
            job.log += f"\nJob timed out ({app.config.TIMEOUT / 60} min.)\n"
            job.state = "error"
        else:
            if command.returncode != 0 or not os.path.exists(result_json):
                job.log += f"\nJob failed ? Return code is {command.returncode} / Or maybe the json result doesnt exist...\n"
                job.state = "error"
            else:
                job.log += "\nPackage check completed\n"
                results = json.load(open(result_json))
                level = results["level"]
                job.state = "done" if level > 4 else "failure"

                job.log += f"\nThe full log is available at {app.config.BASE_URL}/logs/{job.id}.log\n"

                shutil.copy(full_log, yunorunner_dir + f"/results/logs/{job.id}.log")
                if "ci-apps-dev.yunohost.org" in app.config.BASE_URL:
                    job_app_branch = job.url_or_path.lower().strip("/").split("/")[-1]
                    if "PR #" in job.name:
                        pr_id = job.name.split("#")[-1].split(",")[0].strip(")")
                        pr_url = job.url_or_path.rsplit("/", 2)[0] + "/pull/" + pr_id
                        results["pr_url"] = pr_url
                    result_json_file = f"{yunorunner_dir}/results/logs/{job_app}___{job_app_branch}.json"
                    with open(result_json_file, "w") as f:
                        json.dump(results, f)
                else:
                    result_json_file = f"{yunorunner_dir}/results/logs/{job_app}_{app.config.ARCH}_{app.config.YNH_BRANCH}_results.json"
                    shutil.copy(result_json, result_json_file)
                shutil.copy(
                    summary_png, yunorunner_dir + f"/results/summary/{job.id}.png"
                )

    finally:
        job.end_time = datetime.now()
        job_url = app.config.BASE_URL + "/job/" + str(job.id)

        now = datetime.now().strftime("%d/%m/%Y - %H:%M:%S")
        msg = now + f" - Finished job for {job.name} ({job.state})"
        job.log += "=" * len(msg) + "\n"
        job.log += msg + "\n"
        job.log += "=" * len(msg) + "\n"

        job.save()
        await broadcast(
            {
                "action": "update_job",
                "id": job.id,
                "data": model_to_dict(job),
            },
            ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
        )

        if "ci-apps.yunohost.org" in app.config.BASE_URL:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(APPS_LIST) as resp:
                        data = await resp.json()
                        data = data["apps"]
                public_level = data.get(job_app, {}).get("level")

                job_id_with_url = f"[#{job.id}]({job_url})"
                if job.state == "error" or level is None:
                    msg = f"Job {job_id_with_url} for {job_app} failed miserably :("
                elif level == 0:
                    msg = f"App {job_app} failed all tests in job {job_id_with_url} :("
                elif public_level is None:
                    msg = f"App {job_app} rises from level (unknown) to {level} in job {job_id_with_url} !"
                elif level > public_level:
                    msg = f"App {job_app} rises from level {public_level} to {level} in job {job_id_with_url} !"
                elif level < public_level:
                    msg = f"App {job_app} goes down from level {public_level} to {level} in job {job_id_with_url}"
                elif level < 6:
                    msg = (
                        f"App {job_app} stays at level {level} in job {job_id_with_url}"
                    )
                else:
                    # Dont notify anything, reduce CI flood on app chatroom if app is already level 6+
                    msg = ""

                if msg:
                    cmd = f"{yunorunner_dir}/maintenance/chat_notify.sh '{msg}'"
                    try:
                        command = await asyncio.create_subprocess_shell(cmd)
                        while not command.stdout.at_eof():
                            await asyncio.sleep(1)
                    except:
                        pass
            except:
                traceback.print_exc()
                task_logger.exception(f"ERROR in job '{job.name} #{job.id}'")

                job.log += "\n"
                job.log += "Exception:\n"
                job.log += traceback.format_exc()

        try:
            if os.path.exists(result_json):
                results = json.load(open(result_json))
                level = results["level"]
                commit = results["commit"]
            await update_github_commit_status(
                job.url_or_path, job_url, commit, job.state, level
            )
        except Exception as e:
            task_logger.error(
                f"Failed to push commit status for '{job.name}' #{job.id}... : {e}"
            )

        # if job.state != "canceled":
        #    await cleanup_old_package_check_if_lock_exists(worker, job, ignore_error=True)

        # remove ourself from the state
        del jobs_in_memory_state[job.id]

        worker.state = "available"
        worker.save()

        await broadcast(
            {
                "action": "update_job",
                "id": job.id,
                "data": model_to_dict(job),
            },
            ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
        )


async def broadcast(message, channels):
    if not isinstance(channels, (list, tuple)):
        channels = [channels]

    for channel in channels:
        ws_list = subscriptions[channel]
        dead_ws = []

        for ws in ws_list:
            try:
                await ws.send(my_json_dumps(message))
            except (ConnectionClosed, WebsocketClosed):
                dead_ws.append(ws)
            except asyncio.exceptions.CancelledError as err:
                api_logger.info(f"broadcast ws.send() received cancellederror {err}")

        for to_remove in dead_ws:
            try:
                ws_list.remove(to_remove)
            except ValueError:
                pass


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

    yield chunk


@app.websocket("/index-ws")
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
    subquery = (
        JobAlias.select(*selected_fields)
        .where(JobAlias.state << ("done", "failure", "canceled", "error"))
        .group_by(JobAlias.url_or_path)
        .select(fn.Max(JobAlias.id).alias("max_id"))
    )

    latest_done_jobs = (
        Job.select(*selected_fields)
        .join(subquery, on=(Job.id == subquery.c.max_id))
        .order_by(-Job.id)
        .limit(500)
    )

    subquery = (
        JobAlias.select(*selected_fields)
        .where(JobAlias.state == "scheduled")
        .group_by(JobAlias.url_or_path)
        .select(fn.Min(JobAlias.id).alias("min_id"))
    )

    next_scheduled_jobs = (
        Job.select(*selected_fields)
        .join(subquery, on=(Job.id == subquery.c.min_id))
        .order_by(-Job.id)
    )

    # chunks initial data by batch of 30 to avoid killing firefox
    data = chunks(
        itertools.chain(
            map(model_to_dict, next_scheduled_jobs.iterator()),
            map(model_to_dict, Job.select().where(Job.state == "running").iterator()),
            map(model_to_dict, latest_done_jobs.iterator()),
        ),
        30,
    )

    first_chunck = next(data)

    await websocket.send(
        my_json_dumps(
            {
                "action": "init_jobs",
                "data": first_chunck,  # send first chunk
            }
        )
    )

    for chunk in data:
        await websocket.send(
            my_json_dumps(
                {
                    "action": "init_jobs_stream",
                    "data": chunk,
                }
            )
        )

    await websocket.wait_for_connection_lost()


@app.websocket("/job-ws/<job_id:int>")
@clean_websocket
async def ws_job(request, websocket, job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count() == 0:
        raise NotFound()

    job = job[0]

    subscribe(websocket, f"job-{job.id}")

    await websocket.send(
        my_json_dumps(
            {
                "action": "init_job",
                "data": model_to_dict(job),
            }
        )
    )

    await websocket.wait_for_connection_lost()


@app.websocket("/apps-ws")
@clean_websocket
async def ws_apps(request, websocket):
    subscribe(websocket, "jobs")
    subscribe(websocket, "apps")

    # I need to do this because peewee strangely fuck up on join and remove the
    # subquery fields which breaks everything
    repos = Repo.raw(
        """
    SELECT
        "id",
        "name",
        "url",
        "revision",
        "state",
        "random_job_day",
        "job_id",
        "job_name",
        "job_state",
        "created_time",
        "started_time",
        "end_time"
    FROM
        "repo" AS "t1"
    INNER JOIN (
        SELECT
            "t1"."id" as "job_id",
            "t1"."name" as "job_name",
            "t1"."url_or_path",
            "t1"."state" as "job_state",
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
    """
    )

    repos = [
        {
            "id": x.id,
            "name": x.name,
            "url": x.url,
            "revision": x.revision,
            "state": x.state,
            "random_job_day": x.random_job_day,
            "job_id": x.job_id,
            "job_name": x.job_name,
            "job_state": x.job_state,
            "created_time": (
                datetime.strptime(x.created_time.split(".")[0], "%Y-%m-%d %H:%M:%S")
                if x.created_time
                else None
            ),
            "started_time": (
                datetime.strptime(x.started_time.split(".")[0], "%Y-%m-%d %H:%M:%S")
                if x.started_time
                else None
            ),
            "end_time": (
                datetime.strptime(x.end_time.split(".")[0], "%Y-%m-%d %H:%M:%S")
                if x.end_time
                else None
            ),
        }
        for x in repos
    ]

    # add apps without jobs
    selected_repos = {x["id"] for x in repos}
    for repo in Repo.select().where(Repo.id.not_in(selected_repos)):
        repos.append(
            {
                "id": repo.id,
                "name": repo.name,
                "url": repo.url,
                "revision": repo.revision,
                "state": repo.state,
                "random_job_day": repo.random_job_day,
                "job_id": None,
                "job_name": None,
                "job_state": None,
                "created_time": None,
                "started_time": None,
                "end_time": None,
            }
        )

    repos = sorted(repos, key=lambda x: x["name"])

    await websocket.send(
        my_json_dumps(
            {
                "action": "init_apps",
                "data": repos,
            }
        )
    )

    await websocket.wait_for_connection_lost()


@app.websocket("/app-ws/<app_name>")
@clean_websocket
async def ws_app(request, websocket, app_name):
    # XXX I don't check if the app exists because this websocket is supposed to
    # be only loaded from the app page which does this job already
    _app = Repo.select().where(Repo.name == app_name)[0]

    subscribe(websocket, f"app-jobs-{_app.url}")

    job = list(
        Job.select()
        .where(Job.url_or_path == _app.url)
        .order_by(-Job.id)
        .limit(10)
        .dicts()
    )
    await websocket.send(
        my_json_dumps(
            {
                "action": "init_jobs",
                "data": job,
            }
        )
    )

    await websocket.wait_for_connection_lost()


def require_token():
    def decorator(f):
        @wraps(f)
        async def decorated_function(request, *args, **kwargs):
            # run some method that checks the request
            # for the client's authorization status
            if "X-Token" not in request.headers:
                return response.json(
                    {
                        "status": "you need to provide a token "
                        "to access the API, please "
                        "refer to the README"
                    },
                    403,
                )

            token = request.headers["X-Token"].strip()

            if not hmac.compare_digest(token, admin_token):
                api_logger.warning(
                    "someone tried to access the API using an invalid admin token"
                )
                return response.json({"status": "invalid token"}, 403)

            result = await f(request, *args, **kwargs)
            return result

        return decorated_function

    return decorator


@app.route("/api/job", methods=["POST"])
@require_token()
async def api_new_job(request):
    job = Job.create(
        name=request.json["name"],
        url_or_path=request.json["url_or_path"],
        created_time=datetime.now(),
    )

    api_logger.info(f"Request to add new job '{job.name}' [{job.id}]")

    await broadcast(
        {
            "action": "new_job",
            "data": model_to_dict(job),
        },
        ["jobs", f"app-jobs-{job.url_or_path}"],
    )

    return response.text("ok")


@app.route("/api/job", methods=["GET"])
@require_token()
async def api_list_job(request):
    query = Job.select()

    if not all:
        query.where(Job.state in ("scheduled", "running"))

    return response.json([model_to_dict(x) for x in query.order_by(-Job.id)])


@app.route("/api/app", methods=["GET"])
@require_token()
async def api_list_app(request):
    query = Repo.select()

    return response.json([model_to_dict(x) for x in query.order_by(Repo.name)])


@app.route("/api/job/<job_id:int>", methods=["DELETE"])
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

    await broadcast(
        {
            "action": "delete_job",
            "data": data,
        },
        ["jobs", f"job-{job_id}", f"app-jobs-{job.url_or_path}"],
    )

    return response.text("ok")


async def stop_job(job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count() == 0:
        raise NotFound(f"Error: no job with the id '{job_id}'")

    job = job[0]

    api_logger.info(f"Request to stop job '{job.name}' [{job.id}]")

    if job.state == "scheduled":
        api_logger.info(f"Cancel scheduled job '{job.name}' [job.id] " f"on request")
        job.state = "canceled"
        job.save()

        await broadcast(
            {
                "action": "update_job",
                "data": model_to_dict(job),
            },
            ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
        )

        return response.text("ok")

    if job.state == "running":
        api_logger.info(f"Cancel running job '{job.name}' [job.id] on request")

        job.state = "canceled"
        job.end_time = datetime.now()
        job.save()

        jobs_in_memory_state[job.id]["task"].cancel()

        worker = Worker.select().where(
            Worker.id == jobs_in_memory_state[job.id]["worker"]
        )[0]

        worker.state = "available"
        worker.save()

        await broadcast(
            {
                "action": "update_job",
                "data": model_to_dict(job),
            },
            ["jobs", f"job-{job.id}", f"app-jobs-{job.url_or_path}"],
        )

        return response.text("ok")

    if job.state in ("done", "canceled", "failure", "error"):
        api_logger.info(
            f"Request to cancel job '{job.name}' "
            f"[job.id] but job is already in '{job.state}' state, "
            f"do nothing"
        )
        # nothing to do, task is already done
        return response.text("ok")

    raise Exception(f"Tryed to cancel a job with an unknown state: " f"{job.state}")


@app.route("/api/job/<job_id:int>/stop", methods=["POST"])
async def api_stop_job(request, job_id):
    # TODO auth or some kind
    return await stop_job(job_id)


@app.route("/api/job/<job_id:int>/restart", methods=["POST"])
async def api_restart_job(request, job_id):
    api_logger.info(f"Request to restart job {job_id}")
    # Calling a route (eg api_stop_job) doesn't work anymore
    await stop_job(job_id)

    # no need to check if job existss, api_stop_job will do it for us
    job = Job.select().where(Job.id == job_id)[0]
    job.state = "scheduled"
    job.log = ""
    job.save()

    await broadcast(
        {
            "action": "update_job",
            "data": model_to_dict(job),
        },
        ["jobs", f"job-{job_id}", f"app-jobs-{job.url_or_path}"],
    )

    return response.text("ok")


@app.route("/api/results", methods=["GET"])
async def api_results(request):

    import re

    repos = Repo.select().order_by(Repo.name)

    all_results = {}

    for repo in repos:

        latest_result_path = (
            yunorunner_dir
            + f"/results/logs/{repo.name}_{app.config.ARCH}_{app.config.YNH_BRANCH}_results.json"
        )
        if not os.path.exists(latest_result_path):
            continue
        all_results[repo.name] = json.load(open(latest_result_path))

    return response.json(all_results)


@app.route("/api/results-dev", methods=["GET"])
async def api_results_dev(request):

    #
    # That's your face when discovering this horrendous code --,
    #                                                          v
    import glob

    result_files = glob.glob(yunorunner_dir + "/results/logs/*___*.json")
    out = {}
    for result_file in result_files:

        app, branch = result_file.split("/")[-1].replace(".json", "").split("___")

        if app not in out:
            out[app] = {}

        infos = json.load(open(result_file))
        if "commit" not in infos:
            continue

        out[app][branch] = {
            "commit": infos["commit"],
            "commit_timestamp": infos["commit_timestamp"],
            "level": infos["level"],
            "timestamp": infos["timestamp"],
            "yunohost_version": infos["yunohost_version"],
            "pr_url": infos.get("pr_url"),
            "app_version": infos["app_version"],
        }

    return response.json(out)


# Meant to interface with https://shields.io/endpoint
@app.route("/api/job/<job_id:int>/badge", methods=["GET"])
async def api_badge_job(request, job_id):

    job = Job.select().where(Job.id == job_id)

    if job.count() == 0:
        raise NotFound(f"Error: no job with the id '{job_id}'")

    job = job[0]

    state_to_color = {
        "scheduled": "lightgrey",
        "running": "blue",
        "done": "brightgreen",
        "failure": "red",
        "error": "red",
        "canceled": "yellow",
    }

    return response.json(
        {
            "schemaVersion": 1,
            "label": "tests",
            "message": job.state,
            "color": state_to_color[job.state],
        }
    )


@app.route("/job/<job_id>")
@jinja.template("job.html")
async def html_job(request, job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count() == 0:
        raise NotFound()

    job = job[0]

    application = Repo.select().where(Repo.url == job.url_or_path)
    application = application[0] if application else None

    job_url = app.config.BASE_URL + app.url_for("html_job", job_id=job.id)
    badge_url = app.config.BASE_URL + app.url_for("api_badge_job", job_id=job.id)
    shield_badge_url = f"https://img.shields.io/endpoint?url={badge_url}"
    summary_url = app.config.BASE_URL + "/summary/" + str(job.id) + ".png"

    return {
        "job": job,
        "app": application,
        "job_url": job_url,
        "badge_url": badge_url,
        "shield_badge_url": shield_badge_url,
        "summary_url": summary_url,
        "relative_path_to_root": "../",
        "path": request.path,
    }


@app.route(
    "/apps/", strict_slashes=True
)  # To avoid reaching the route "/apps/<app_name>/" with <app_name> an empty string
@jinja.template("apps.html")
async def html_apps(request):
    return {"relative_path_to_root": "../", "path": request.path}


@app.route("/apps/<app_name>/")
@jinja.template("app.html")
async def html_app(request, app_name):
    _app = Repo.select().where(Repo.name == app_name)

    if _app.count() == 0:
        raise NotFound()

    return {"app": _app[0], "relative_path_to_root": "../../", "path": request.path}


@app.route("/apps/<app_name>/latestjob")
async def html_app_latestjob(request, app_name):
    _app = Repo.select().where(Repo.name == app_name)

    if _app.count() == 0:
        raise NotFound()

    jobs = Job.select(fn.MAX(Job.id)).where(
        Job.url_or_path == _app[0].url, Job.state != "scheduled"
    )

    if jobs.count() == 0:
        jobs = Job.select(fn.MAX(Job.id)).where(Job.url_or_path == _app[0].url)

    if jobs.count() == 0:
        raise NotFound()

    job_url = app.config.BASE_URL + app.url_for("html_job", job_id=jobs[0].id)

    return response.redirect(job_url)


@app.route("/")
@jinja.template("index.html")
async def html_index(request):
    return {"relative_path_to_root": "", "path": request.path}


# @always_relaunch(sleep=10)
# async def number_of_tasks():
#    print("Number of tasks: %s" % len(asyncio_all_tasks()))
#
#
# @app.route('/monitor')
# async def monitor(request):
#    snapshot = tracemalloc.take_snapshot()
#    top_stats = snapshot.statistics('lineno')
#
#    tasks = asyncio_all_tasks()
#
#    return response.json({
#        "top_20_trace": [str(x) for x in top_stats[:20]],
#        "tasks": {
#            "number": len(tasks),
#            "array": [show_coro(t) for t in tasks],
#        }
#    })


@app.route("/github", methods=["GET"])
async def github_get(request):
    return response.text(
        "You aren't supposed to go on this page using a browser, it's for webhooks push instead."
    )


@app.route("/github", methods=["POST"])
async def github(request):

    # Abort directly if no secret opened
    # (which also allows to only enable this feature if we define the webhook secret)
    if app.config.GITHUB_WEBHOOK_SECRET is None:
        api_logger.info(
            "Received a webhook but no settings GITHUB_WEBHOOK_SECRET or GITHUB_BOT_TOKEN... ignoring"
        )
        return response.json({"error": "GitHub hooks not configured"}, 403)

    # Only SHA1 is supported
    header_signature = request.headers.get("X-Hub-Signature")
    if header_signature is None:
        api_logger.info("Received a webhook but there's no header X-Hub-Signature")
        return response.json({"error": "No X-Hub-Signature"}, 403)

    sha_name, signature = header_signature.split("=")
    if sha_name != "sha1":
        api_logger.info(
            "Received a webhook but signing algo isn't sha1, it's '%s'" % sha_name
        )
        return response.json({"error": "Signing algorightm is not sha1 ?!"}, 501)

    # HMAC requires the key to be bytes, but data is string
    mac = hmac.new(
        app.config.GITHUB_WEBHOOK_SECRET.encode(),
        msg=request.body,
        digestmod=hashlib.sha1,
    )

    if not hmac.compare_digest(str(mac.hexdigest()), str(signature)):
        api_logger.info(
            "Received a webhook but signature authentication failed (is the secret properly configured?)"
        )
        return response.json({"error": "Bad signature ?!"}, 403)

    hook_type = request.headers.get("X-Github-Event")
    hook_infos = request.json

    # We expect issue comments (issue = also PR in github stuff...)
    # - *New* comments
    # - On issue/PRs which are still open
    if hook_type == "issue_comment":
        if (
            hook_infos["action"] != "created"
            or hook_infos["issue"]["state"] != "open"
            or "pull_request" not in hook_infos["issue"]
        ):
            # Nothing to do but success anyway (204 = No content)
            api_logger.debug(
                "Received an issue_comment webhook but doesn't qualify for starting a job."
            )
            return response.empty(status=204)

        # Check the comment contains proper keyword trigger
        body = hook_infos["comment"]["body"].strip()[:100].lower()
        if not any(trigger.lower() in body for trigger in app.config.WEBHOOK_TRIGGERS):
            # Nothing to do but success anyway (204 = No content)
            api_logger.debug(
                "Received an issue_comment webhook but doesn't contain any keyword."
            )
            return response.empty(status=204)

        # We only accept this from people which are member of the org
        # https://docs.github.com/en/rest/reference/orgs#check-organization-membership-for-a-user
        # We need a token an we can't rely on "author_association" because sometimes, users are members in Private,
        # which is not represented in the original webhook
        async def is_user_in_organization(user):
            async with aiohttp.ClientSession(
                headers={
                    "Authorization": f"token {app.config.GITHUB_COMMIT_STATUS_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                }
            ) as session:
                resp = await session.get(
                    f"https://api.github.com/orgs/YunoHost-Apps/members/{user}",
                )
                return resp.status == 204

        github_username = hook_infos["comment"]["user"]["login"]
        if not await is_user_in_organization(github_username):
            # Unauthorized
            api_logger.warning(
                f"User {github_username} is not authorized to run webhooks!"
            )
            return response.json({"error": "Unauthorized"}, 403)
        # Fetch the PR infos (yeah they ain't in the initial infos we get @_@)
        pr_infos_url = hook_infos["issue"]["pull_request"]["url"]

    elif hook_type == "pull_request":
        if hook_infos["action"] != "opened":
            # Nothing to do but success anyway (204 = No content)
            api_logger.debug(
                "Received a pull_request webhook but doesn't qualify for starting a job."
            )
            return response.empty(status=204)

        # We only accept PRs that are created by github-action bot
        if hook_infos["pull_request"]["user"]["login"] not in [
            "github-actions[bot]",
            "yunohost-bot",
        ] or not hook_infos["pull_request"]["head"]["ref"].startswith(
            "ci-auto-update-"
        ):
            # Unauthorized
            api_logger.debug(
                "Received a pull_request webhook but from an unknown github user."
            )
            return response.empty(status=204)
        if not app.config.ANSWER_TO_AUTO_UPDATER:
            # Unauthorized
            api_logger.info(
                "Received a pull_request webhook but configured to ignore the auto-updater."
            )
            return response.empty(status=204)
        # Fetch the PR infos (yeah they ain't in the initial infos we get @_@)
        pr_infos_url = hook_infos["pull_request"]["url"]

    else:
        # Nothing to do but success anyway (204 = No content)
        return response.empty(status=204)

    async with aiohttp.ClientSession() as session:
        async with session.get(pr_infos_url) as resp:
            pr_infos = await resp.json()

    branch_name = pr_infos["head"]["ref"]
    repo = pr_infos["head"]["repo"]["html_url"]
    url_to_test = f"{repo}/tree/{branch_name}"
    app_id = pr_infos["base"]["repo"]["name"].rstrip("")
    if app_id.endswith("_ynh"):
        app_id = app_id[: -len("_ynh")]

    pr_id = str(pr_infos["number"])

    # Create the job for the corresponding app (with the branch url)

    api_logger.info("Scheduling a new job from comment on a PR")
    job = await create_job(
        app_id, url_to_test, job_comment=f"PR #{pr_id}, {branch_name}"
    )

    if not job:
        api_logger.warning("Corresponding job already scheduled!")
        return response.empty(status=204)

    # Answer with comment with link+badge for the job

    async def comment(body):
        if hook_type == "issue_comment":
            comments_url = hook_infos["issue"]["comments_url"]
        else:
            comments_url = hook_infos["pull_request"]["comments_url"]

        async with aiohttp.ClientSession(
            headers={"Authorization": f"token {app.config.GITHUB_COMMIT_STATUS_TOKEN}"}
        ) as session:
            async with session.post(
                comments_url, data=my_json_dumps({"body": body})
            ) as resp:
                respjson = await resp.json()
                api_logger.info("Added comment %s" % respjson["html_url"])

    catchphrase = random.choice(app.config.WEBHOOK_CATCHPHRASES)
    # Dirty hack with BASE_URL passed from cmd argument because we can't use request.url_for because Sanic < 20.x
    job_url = app.config.BASE_URL + app.url_for("html_job", job_id=job.id)
    badge_url = app.config.BASE_URL + app.url_for("api_badge_job", job_id=job.id)
    shield_badge_url = f"https://img.shields.io/endpoint?url={badge_url}"
    summary_url = app.config.BASE_URL + f"/summary/{job.id}.png"

    body = f"{catchphrase}\n[![Test Badge]({shield_badge_url})]({job_url})\n[![]({summary_url})]({job_url})"
    api_logger.info(body)
    await comment(body)

    return response.text("ok")


# def show_coro(c):
#    data = {
#        'txt': str(c),
#        'type': str(type(c)),
#        'done': c.done(),
#        'cancelled': False,
#        'stack': None,
#        'exception': None,
#    }
#    if not c.done():
#        data['stack'] = [format_frame(x) for x in c.get_stack()]
#    else:
#        if c.cancelled():
#            data['cancelled'] = True
#        else:
#            data['exception'] = str(c.exception())
#
#    return data


# def format_frame(f):
#    keys = ['f_code', 'f_lineno']
#    return dict([(k, str(getattr(f, k))) for k in keys])


@app.listener("before_server_start")
async def listener_before_server_start(*args, **kwargs):
    task_logger.info("before_server_start")
    reset_pending_jobs()
    reset_busy_workers()
    merge_jobs_on_startup()

    set_random_day_for_monthy_job()


@app.listener("after_server_start")
async def listener_after_server_start(*args, **kwargs):
    task_logger.info("after_server_start")


@app.listener("before_server_stop")
async def listener_before_server_stop(*args, **kwargs):
    task_logger.info("before_server_stop")


@app.listener("after_server_stop")
async def listener_after_server_stop(*args, **kwargs):
    task_logger.info("after_server_stop")
    for job_id in jobs_in_memory_state:
        await stop_job(job_id)
        job = Job.select().where(Job.id == job_id)[0]
        job.state = "scheduled"
        job.log = ""
        job.save()


def set_config(config="./config.py"):

    default_config = {
        "BASE_URL": "",
        "PORT": 4242,
        "TIMEOUT": 10800,
        "DEBUG": False,
        "MONITOR_APPS_LIST": False,
        "MONITOR_GIT": False,
        "MONITOR_ONLY_GOOD_QUALITY_APPS": False,
        "MONTHLY_JOBS": False,
        "ANSWER_TO_AUTO_UPDATER": True,
        "WORKER_COUNT": 1,
        "ARCH": "amd64",
        "DIST": "bullseye",
        "YNH_BRANCH": "stable",
        "PACKAGE_CHECK_DIR": yunorunner_dir + "/package_check/",
        "WEBHOOK_TRIGGERS": [
            "!testme",
            "!gogogadgetoci",
            "By the power of systemd, I invoke The Great App CI to test this Pull Request!",
        ],
        "WEBHOOK_CATCHPHRASES": [
            "Alrighty!",
            "Fingers crossed!",
            "May the CI gods be with you!",
            ":carousel_horse:",
            ":rocket:",
            ":sunflower:",
            "Meow :cat2:",
            ":v:",
            ":stuck_out_tongue_winking_eye:",
        ],
        "GITHUB_COMMIT_STATUS_TOKEN": None,
        "GITHUB_WEBHOOK_SECRET": None,
    }

    app.config.update_config(default_config)
    app.config.update_config(config)

    app.config.PACKAGE_CHECK_PATH = app.config.PACKAGE_CHECK_DIR + "package_check.sh"
    app.config.PACKAGE_CHECK_LOCK_PER_WORKER = (
        app.config.PACKAGE_CHECK_DIR + "pcheck-{worker_id}.lock"
    )
    app.config.PACKAGE_CHECK_FULL_LOG_PER_WORKER = (
        app.config.PACKAGE_CHECK_DIR + "full_log_{worker_id}.log"
    )
    app.config.PACKAGE_CHECK_RESULT_JSON_PER_WORKER = (
        app.config.PACKAGE_CHECK_DIR + "results_{worker_id}.json"
    )
    app.config.PACKAGE_CHECK_SUMMARY_PNG_PER_WORKER = (
        app.config.PACKAGE_CHECK_DIR + "summary_{worker_id}.png"
    )

    if not os.path.exists(app.config.PACKAGE_CHECK_PATH):
        print(
            f"Error: analyzer script doesn't exist at '{app.config.PACKAGE_CHECK_PATH}'. Please fix the configuration in {config}"
        )
        sys.exit(1)


if __name__ == "__main__":
    set_config()
    app.run("localhost", port=app.config.PORT, debug=app.config.DEBUG)


if __name__ == "__mp_main__":
    # Worker thread
    set_config()

    if app.config.MONITOR_APPS_LIST:
        app.add_task(
            monitor_apps_lists(
                monitor_git=app.config.MONITOR_GIT,
                monitor_only_good_quality_apps=app.config.MONITOR_ONLY_GOOD_QUALITY_APPS,
            )
        )

    if app.config.MONTHLY_JOBS:
        app.add_task(launch_monthly_job())

    # app.add_task(number_of_tasks())

    app.add_task(jobs_dispatcher())
