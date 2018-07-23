# encoding: utf-8

import os
import sys
import argh
import random
import logging
import asyncio

from datetime import datetime
from collections import defaultdict

import ujson
import aiohttp
import aiofiles

from websockets.exceptions import ConnectionClosed

from sanic import Sanic, response
from sanic.exceptions import NotFound
from sanic.log import LOGGING_CONFIG_DEFAULTS

from playhouse.shortcuts import model_to_dict, dict_to_model

from models import Repo, Job, db, Worker

LOGGING_CONFIG_DEFAULTS["loggers"]["task"] = {
    "level": "INFO",
    "handlers": ["task_console"],
}

LOGGING_CONFIG_DEFAULTS["handlers"]["task_console"] = {
    "class": "logging.StreamHandler",
    "formatter": "background",
    "stream": sys.stdout,
}

LOGGING_CONFIG_DEFAULTS["formatters"]["background"] = {
    "format": "%(asctime)s [%(process)d] [BACKGROUND] [%(funcName)s] %(message)s",
    "datefmt": "[%Y-%m-%d %H:%M:%S %z]",
    "class": "logging.Formatter",
}

task_logger = logging.getLogger("task")

app = Sanic()

APPS_LISTS = {
    "Official": "https://app.yunohost.org/official.json",
    "Community": "https://app.yunohost.org/community.json",
}

subscriptions = defaultdict(list)

# this will have the form:
# jobs_in_memory_state = {
#     some_job_id: {"worker": some_worker_id, "task": some_aio_task},
# }
jobs_in_memory_state = {}


def reset_pending_jobs():
    Job.update(state="scheduled").where(Job.state == "running").execute()


def reset_busy_workers():
    # XXX when we'll have distant workers that might break those
    Worker.update(state="available").execute()


async def monitor_apps_lists():
    "parse apps lists every hour or so to detect new apps"

    # only support github for now :(
    async def get_master_commit_sha(url):
        print(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>> git ls-remote {url} master")
        command = await asyncio.create_subprocess_shell(f"git ls-remote {url} master", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data = await command.stdout.read()
        print(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>> {data.decode()}")
        return data.decode().strip().split(" ")[0]

    for app_list_name, url in APPS_LISTS.items():
        async with aiohttp.ClientSession() as session:
            app_list = "official"
            task_logger.info(f"Downloading {app_list_name}.json...")
            async with session.get(url) as resp:
                data = await resp.json()

        repos = {x.name: x for x in Repo.select().where(Repo.app_list == app_list_name)}

        for app_id, app_data in data.items():
            commit_sha = await get_master_commit_sha(app_data["git"]["url"])

            # already know, look to see if there is new commits
            if app_id in repos:
                repo = repos[app_id]
                if repo.revision != commit_sha:
                    task_logger.info(f"Application {app_id} has new commits on github"
                                     f"({repo.revision} → {commit_sha}), schedule new job")
                    repo.revision = commit_sha
                    repo.save()

                    job = Job.create(
                        name=f"{app_id} ({app_list_name})",
                        url_or_path=repo.url,
                        state="scheduled",
                    )

                    await broadcast({
                        "action": "new_job",
                        "data": model_to_dict(job),
                    }, "jobs")

            # new app
            else:
                task_logger.info(f"New application detected: {app_id} in {app_list_name}, scheduling a new job")
                repo = Repo.create(
                    name=app_id,
                    url=app_data["git"]["url"],
                    revision=commit_sha,
                    app_list=app_list_name,
                )

                job = Job.create(
                    name=f"{app_id} ({app_list_name})",
                    url_or_path=repo.url,
                    state="scheduled",
                )

                await broadcast({
                    "action": "new_job",
                    "data": model_to_dict(job),
                }, "jobs")

            await asyncio.sleep(3)

    await asyncio.sleep(5 * 60)
    asyncio.ensure_future(monitor_apps_lists())


async def jobs_dispatcher():
    if Worker.select().count() == 0:
        for i in range(1):
            Worker.create(state="available")

    while True:
        workers = Worker.select().where(Worker.state == "available")

        # no available workers, wait
        if workers.count() == 0:
            await asyncio.sleep(3)
            continue

        with db.atomic('IMMEDIATE'):
            jobs = Job.select().where(Job.state == "scheduled")

            # no jobs to process, wait
            if jobs.count() == 0:
                await asyncio.sleep(3)
                continue

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
    }, ["jobs", f"job-{job.id}"])

    # fake stupid command, whould run CI instead
    task_logger.info(f"Starting job '{job.name}'...")

    cwd = os.path.split(path_to_analyseCI)[0]
    arguments = f' {job.url_or_path} "{job.name}"'
    task_logger.info(f"Launch command: /bin/bash " + path_to_analyseCI + arguments)
    command = await asyncio.create_subprocess_shell("/bin/bash " + path_to_analyseCI + arguments,
                                                    cwd=cwd,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)

    while not command.stdout.at_eof():
        data = await command.stdout.readline()
        line = data.decode().rstrip()

        job.log += data.decode()
        # XXX seems to be okay performance wise but that's probably going to be
        # a bottleneck at some point :/
        # theoritically jobs are going to have slow output
        job.save()

        await broadcast({
            "action": "update_job",
            "id": job.id,
            "data": model_to_dict(job),
        }, ["jobs", f"job-{job.id}"])

    task_logger.info(f"Finished job '{job.name}'")

    await command.wait()
    job.end_time = datetime.now()
    job.state = "done"
    job.save()

    # remove ourself from the state
    del jobs_in_memory_state[job.id]

    worker.state = "available"
    worker.save()

    await broadcast({
        "action": "update_job",
        "id": job.id,
        "data": model_to_dict(job),
    }, ["jobs", f"job-{job.id}"])


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


@app.websocket('/index-ws')
async def index_ws(request, websocket):
    subscribe(websocket, "jobs")

    await websocket.send(ujson.dumps({
        "action": "init_jobs",
        "data": map(model_to_dict, Job.select().order_by(-Job.id)),
    }))

    while True:
        # do nothing with input but wait
        await websocket.recv()


@app.websocket('/job-<job_id>-ws')
async def job_ws(request, websocket, job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count == 0:
        raise NotFound()

    job = job[0]

    subscribe(websocket, f"job-{job.id}")

    await websocket.send(ujson.dumps({
        "action": "init_job",
        "data": model_to_dict(job),
    }))

    while True:
        # do nothing with input but wait
        await websocket.recv()


@app.route("/api/job", methods=['POST'])
async def api_new_job(request):
    # TODO auth or some kind

    # test type ?
    # architecture (ARM)
    # debian version
    # yunohost version (in test type?
    # day for the montly test?
    # ? analyseCI path ?

    job = Job.create(
        name=request.json["name"],
        url_or_path=request.json["url_or_path"],
        type=request.json.get("test_type", "stable"),
        debian_version=request.json.get("debian_version", "stretch"),
    )

    task_logger.info(f"Request to add new job '{job.name}' [{job.id}]")

    await broadcast({
        "action": "new_job",
        "data": model_to_dict(job),
    }, "jobs")

    return response.text("ok")


@app.route("/api/job/<job_id>/stop", methods=['POST'])
async def api_stop_job(request, job_id):
    # TODO auth or some kind

    job = Job.select().where(Job.id == job_id)

    if job.count == 0:
        raise NotFound()

    job = job[0]

    if job.state == "scheduled":
        task_logger.info(f"Cancel scheduled job '{job.name}' [job.id] on request")
        job.state = "canceled"
        job.save()

        await broadcast({
            "action": "update_job",
            "data": model_to_dict(job),
        }, ["jobs", f"job-{job.id}"])

        return response.text("ok")

    if job.state == "running":
        task_logger.info(f"Cancel running job '{job.name}' [job.id] on request")
        job.state = "canceled"
        job.save()

        jobs_in_memory_state[job.id]["task"].cancel()

        worker = Worker.select().where(Worker.id == jobs_in_memory_state[job.id]["worker"])[0]
        worker.state = "available"
        worker.save()

        await broadcast({
            "action": "update_job",
            "data": model_to_dict(job),
        }, ["jobs", f"job-{job.id}"])

        return response.text("ok")

    if job.state in ("done", "canceled", "failure"):
        task_logger.info(f"Request to cancel job '{job.name}' [job.id] but job is already in '{job.state}' state, do nothing")
        # nothing to do, task is already done
        return response.text("ok")

    raise Exception(f"Tryed to cancel a job with an unknown state: {job.state}")


@app.route('/job/<job_id>')
async def job(request, job_id):
    job = Job.select().where(Job.id == job_id)

    if job.count == 0:
        raise NotFound()

    job = job[0]

    async with aiofiles.open("./templates/job.html", mode="r") as index_template:
        return response.html(await index_template.read() % job.id)


@app.route('/')
async def index(request):
    async with aiofiles.open("./templates/index.html", mode="r") as index_template:
        return response.html(await index_template.read())


def main(path_to_analyseCI):
    reset_pending_jobs()
    reset_busy_workers()

    app.config.path_to_analyseCI = path_to_analyseCI
    app.add_task(monitor_apps_lists())
    app.add_task(jobs_dispatcher())
    app.run('localhost', port=4242, debug=True)


if __name__ == "__main__":
    argh.dispatch_command(main)
