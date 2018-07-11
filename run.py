# encoding: utf-8

import os
import sys
import ujson
import asyncio

import random

from datetime import datetime

import aiohttp
import aiofiles

from websockets.exceptions import ConnectionClosed

from sanic import Sanic, response

from playhouse.shortcuts import model_to_dict, dict_to_model

from models import Repo, BuildTask, db

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
                BuildTask.create(
                    repo=repo,
                    target_revision=app_data["git"]["revision"],
                    yunohost_version="stretch-stable",
                    state="scheduled",
                )

    asyncio.ensure_future(run_jobs())


async def run_jobs():
    print("Run jobs...")
    while True:
        with db.atomic():
            build_task = BuildTask.select().where(BuildTask.state == "scheduled").limit(1)

            if not build_task.count():
                await asyncio.sleep(3)
                continue

            build_task = build_task[0]

            build_task.state = "running"
            build_task.started_time = datetime.now()
            build_task.save()

        # fake stupid command, whould run CI instead
        print(f"Starting job for {build_task.repo.name}...")
        command = await asyncio.create_subprocess_shell("/usr/bin/tail /var/log/auth.log",
                                                       stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)

        while not command.stdout.at_eof():
            data = await command.stdout.readline()
            line = data.decode().rstrip()
            print(f">> {line}")

        # XXX stupid crap to stimulate long jobs
        # await asyncio.sleep(random.randint(30, 120))
        # await asyncio.sleep(5)
        print(f"Finished task for {build_task.repo.name}")

        await command.wait()
        build_task.end_time = datetime.now()
        build_task.save()

        await broadcast_to_ws(all_index_ws, {
                "target": "build_task",
                "id": build_task.id,
                "data": model_to_dict(build_task),
            })

        await asyncio.sleep(3)


async def broadcast_to_ws(ws_list, message):
    dead_ws = []

    for ws in ws_list:
        try:
            await ws.send(ujson.dumps(message))
        except ConnectionClosed:
            dead_ws.append(ws)

    for to_remove in dead_ws:
        ws_list.remove(to_remove)


@app.websocket('/index-ws')
async def index_ws(request, websocket):
    all_index_ws.append(websocket)
    while True:
        data = await websocket.recv()
        print(f"websocket: {data}")
        await websocket.send(f"echo {data}")


@app.route("/api/tasks")
async def api_tasks(request):
    return response.json(map(model_to_dict, BuildTask.select()))


@app.route('/')
async def index(request):
    return response.html(open("./templates/index.html", "r").read())
    # return await render_template("index.html", build_tasks=BuildTask.select().order_by("id"))


if __name__ == "__main__":
    all_index_ws = []
    app.add_task(initialize_app_list())
    app.run('localhost', port=5000, debug=True)
