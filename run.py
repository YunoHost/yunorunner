# encoding: utf-8

import os
import sys
import ujson
import asyncio

import random

from datetime import datetime
from collections import defaultdict

import aiohttp
import aiofiles

from websockets.exceptions import ConnectionClosed

from sanic import Sanic, response

from playhouse.shortcuts import model_to_dict, dict_to_model

from models import Repo, Job, db, Worker

app = Sanic()

OFFICAL_APPS_LIST = "https://app.yunohost.org/official.json"
# TODO handle community list
COMMUNITY_APPS_LIST = "https://app.yunohost.org/community.json"

APPS_LIST = [OFFICAL_APPS_LIST, COMMUNITY_APPS_LIST]


async def initialize_app_list():
    if not os.path.exists("lists"):
        os.makedirs("lists")

    async with aiohttp.ClientSession() as session:
        app_list = "official"
        sys.stdout.write(f"Downloading {OFFICAL_APPS_LIST}...")
        sys.stdout.flush()
        async with session.get(OFFICAL_APPS_LIST) as resp:
            data = await resp.json()
            sys.stdout.write("done\n")

        repos = {x.name: x for x in Repo.select()}

        for app_id, app_data in data.items():
            if app_id in repos:
                # print(f"déjà là: {app_id}")
                pass
            else:
                print(f"New application detected: {app_id} in {app_list}")
                repo = Repo.create(
                    name=app_id,
                    url=app_data["git"]["url"],
                    revision=app_data["git"]["revision"],
                    app_list=app_list,
                )

                print(f"Schedule a new build for {app_id}")
                Job.create(
                    name=f"{app_id} stable",
                    url_or_path=repo.url,
                    target_revision=app_data["git"]["revision"],
                    yunohost_version="stretch-stable",
                    state="scheduled",
                )


async def jobs_dispatcher():
    if Worker.select().count() == 0:
        for i in range(5):
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
                print(job)
                job.save()

                worker.state = "busy"
                worker.save()

                asyncio.ensure_future(run_job(worker, job))


async def run_job(worker, job):
    await broadcast({
            "target": "job",
            "id": job.id,
            "data": model_to_dict(job),
        }, "jobs")

    # fake stupid command, whould run CI instead
    print(f"Starting job {job.name}...")
    command = await asyncio.create_subprocess_shell("/usr/bin/tail /var/log/auth.log",
                                                   stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)

    while not command.stdout.at_eof():
        data = await command.stdout.readline()
        line = data.decode().rstrip()
        print(f">> {line}")

    # XXX stupid crap to stimulate long jobs
    await asyncio.sleep(random.randint(1, 15))
    # await asyncio.sleep(5)
    print(f"Finished job {job.name}")

    await command.wait()
    job.end_time = datetime.now()
    job.state = "done"
    job.save()

    worker.state = "available"
    worker.save()

    await broadcast({
            "target": "job",
            "id": job.id,
            "data": model_to_dict(job),
        }, "jobs")


async def broadcast(message, channel):
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

    while True:
        data = await websocket.recv()
        print(f"websocket: {data}")
        await websocket.send(f"echo {data}")


@app.route("/api/jobs")
async def api_jobs(request):
    return response.json(map(model_to_dict, Job.select()))


@app.route("/api/jobs", methods=['POST'])
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
        target_revision=request.json["revision"],
        type=request.json.get("test_type", "stable"),
        yunohost_version=request.json.get("yunohost_version", "unstable"),
        debian_version=request.json.get("debian_version", "stretch"),
    )

    print(f"Request to add new job '{job.name}' {job}")

    return response.text("ok")


@app.route('/')
async def index(request):
    return response.html(open("./templates/index.html", "r").read())
    # return await render_template("index.html", jobs=Job.select().order_by("id"))


if __name__ == "__main__":
    subscriptions = defaultdict(list)

    app.add_task(initialize_app_list())
    app.add_task(jobs_dispatcher())
    app.run('localhost', port=5000, debug=True)
