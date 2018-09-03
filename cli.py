import os
import sys
import argh
import requests

from argh.decorators import named


DOMAIN = "localhost:4242"

def require_token():
    if os.path.exists("token") and open("token").read().strip():
        return

    print("You need a token to be able to uses this command tool for security reasons, please refer to the README on how to add one https://github.com/YunoHost/yunorunner")
    try:
        token = input("Token: ").strip()
    except KeyboardInterrupt:
        print()
        token = None

    if not token:
        print("Error: you need to provide a valid token")
        sys.exit(1)

    open("token", "w").write(token)


def request_api(path, domain, verb, data={}, check_return_code=True):
    assert verb in ("get", "post", "put", "delete")

    https = False
    if domain.split(":")[0] not in ("localhost", "127.0.0.1", "0.0.0.0"):
        https = True

    response = getattr(requests, verb)(
        "http%s://%s/api/%s" % ("s" if https else "", domain, path),
        headers={"X-Token": open("token", "r").read().strip()},
        json=data,
    )

    if response.status_code == 403:
        print(f"Error: access refused because '{response.json()['status']}'")
        sys.exit(1)

    if check_return_code:
        # TODO: real error message
        assert response.status_code == 200, response.content

    return response


def add(name, url_or_path, test_type="stable", yunohost_version="unstable", debian_version="stretch", revision="master", domain=DOMAIN):
    request_api(
        path="job",
        verb="post",
        domain=domain,
        data={
            "name": name,
            "url_or_path": url_or_path,
            "yunohost_version": yunohost_version,
            "test_type": test_type,
            "debian_version": debian_version,
            "revision": revision,
        },
    )


@named("list")
def list_(all=False, domain=DOMAIN):
    response = request_api(
        path="job",
        verb="get",
        domain=domain,
        data={
            "all": all,
        },
    )

    for i in response.json():
        print(f"{i['id']:4d} - {i['name']}")


def delete(job_id, domain=DOMAIN):
    response = request_api(
        path=f"job/{job_id}",
        verb="delete",
        domain=domain,
        check_return_code=False
    )

    if response.status_code == 404:
        print(f"Error: no job with the id '{job_id}'")
        sys.exit(1)

    assert response.status_code == 200, response.content


def update(job_id, domain=DOMAIN): pass
def stop(job_id, domain=DOMAIN):
    response = request_api(
        path=f"job/{job_id}/stop",
        verb="post",
        domain=domain,
        check_return_code=False
    )

    if response.status_code == 404:
        print(f"Error: no job with the id '{job_id}'")
        sys.exit(1)

    assert response.status_code == 200, response.content


def resume(job_id, domain=DOMAIN): pass


if __name__ == '__main__':
    require_token()
    argh.dispatch_commands([add, list_, delete, update, stop, resume])
