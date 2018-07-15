import argh
import requests


def main(name, url_or_path, test_type="stable", yunohost_version="unstable", debian_version="stretch", revision="master", domain="localhost:4242"):

    https = False
    if domain.split(":")[0] not in ("localhost", "127.0.0.1", "0.0.0.0"):
        https = True

    response = requests.post("http%s://%s/api/job" % ("s" if https else "", domain), json={
        "name": name,
        "url_or_path": url_or_path,
        "yunohost_version": yunohost_version,
        "test_type": test_type,
        "debian_version": debian_version,
        "revision": revision,
    })

    assert response.content == b"ok", response.content


argh.dispatch_command(main)
