
# Installation

It's recommended to use : https://github.com/YunoHost-Apps/yunorunner_ynh

But you can also install stuff manually .... : 

```bash
cd /var/www/
git clone https://github.com/YunoHost/yunorunner
cd yunorunner
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

In either cases, you probably want to run the `finish_install.sh` script in `maintenance/`

The configuration happens in `config.py` and typically looks like : 

```
BASE_URL = "https://ci-apps-foobar.yunohost.org/ci"
PORT = 34567
PATH_TO_ANALYZER = "/var/www/yunorunner/analyze_yunohost_app.sh"
MONITOR_APPS_LIST = True
MONITOR_GIT = True
MONITOR_ONLY_GOOD_QUALITY_APPS = True
MONTHLY_JOBS = True
WORKER_COUNT = 1
YNH_BRANCH = "unstable"
DIST = "bullseye" 
```

### Cli tool

The cli tool is called "ciclic" because my (Bram ;)) humour is legendary.

Basic tool signature:

```
$ ve3/bin/python ciclic
usage: ciclic [-h] {add,list,delete,stop,restart} ...
```

This tool works by doing a http request to the CI instance of you choice, by
default the domain is "http://localhost:4242", you cand modify this for EVERY
command by add "-d https://some.other.domain.com/path/".

##### Usage

Adding a new job:

```
$ ve3/bin/python ciclic add "some_app (Official)" "https://github.com/yunohost-apps/some_app_ynh" -d "https://ci-stable.yunohost.org/ci/"
```

Listing jobs:

```
$ ve3/bin/python ciclic list
  31 - mumbleserver_ynh 15 [done]
  30 - mumbleserver_ynh 14 [done]
  29 - mumbleserver_ynh 13 [done]
  28 - mumbleserver_ynh 12 [done]
  27 - mumbleserver_ynh 11 [done]
  26 - mumbleserver_ynh 10 [done]
  25 - mumbleserver_ynh 9 [done]
  24 - mumbleserver_ynh 8 [done]
  ...
```

**IMPORTANT**: the first digit is the ID of the job, it will be used in ALL the other commands.

Deleting a job:

```
$ # $job_id is an id from "ciclic list" or the one in the URL of a job
$ ve3/bin/python ciclic delete $job_id
```

Stop a job:

```
$ # $job_id is an id from "ciclic list" or the one in the URL of a job
$ ve3/bin/python ciclic stop $job_id
```

Restart a job:

```
$ # $job_id is an id from "ciclic list" or the one in the URL of a job
$ ve3/bin/python ciclic restart $job_id
```

Note that delete/restart will stop the job first to free the worker.

You will see a resulting log on the server for each action.

List apps:

```
$ ve3/bin/python ciclic app-list
```

# Deployment

You need to put this program behind a nginx mod proxy AND add the magical lines
to allow websocket (it's not allowed for whatever reason) and that all the way
through all the proxies (if you deploy on bearnaise's lxc or something
similar).

# Licence

AGPL v3+

Copyright YunoHost 2018 (you can find the authors in the commits)
