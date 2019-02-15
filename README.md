WIP CI runner for YunoHost.

# Installation

You need python 3.6 for that. The simpliest way to get it is to uses [pythonz](https://bettercallsaghul.com/pythonz/) if it's not available in your distribution.

#### Getting python 3.6 (skip if you already have it)

Install pythonz:

    curl -kL https://raw.githubusercontent.com/saghul/pythonz/master/pythonz-install | bash

    # add tis to your .bashrc
    [[ -s $HOME/.pythonz/etc/bashrc ]] && source $HOME/.pythonz/etc/bashrc

Now install python 3.6:

    pythonz install 3.6.6

#### Installing yunorunner

You need virtualenv and sqlite:

    sudo apt-get install python-virtualenv sqlite3

Download the source code:

    git clone https://github.com/YunoHost/yunorunner
    cd yunorunner

Install dependancies:

    virtualenv -p $(pythonz locate 3.6.6) ve3
    ve3/bin/pip install -r requirements.txt

And that's it.

# Usage

### Server

Simple usage:

    ve3/bin/python ./run.py /path/to/analyseCI.sh

This will start the server which will listen on http://localhost:4242

If you don't want to CI to monitor the app list you can ask it using the `--dont-monitor-apps-list` option:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --dont-monitor-apps-list

You can also disable git monitoring and the monthly jobs:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --dont-monitor-git --no-montly-jobs

You can also specify the CI type this way:

    ve3/bin/python ./run.py /path/to/analyseCI.sh -t arm
    # or
    ve3/bin/python ./run.py /path/to/analyseCI.sh -t testing-unstable

The default value is `stable`.

Current behavior:

* for stable : launch job as `$app_id ($app_list_name)`
* for arm : launch job as `$app_id ($app_list_name) (~ARM~)`
* for stable : launch TWO jobs as `$app_id ($app_list_name) (testing)` and `$app_id ($app_list_name) (unstable)`

#### Changing the port

If you need to change to port on which yunorunner is listening, you can simply uses the `--port` cli argument like that:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --port 4343

##### SSL

If you need this server to be front (without nginx in front of it) you can start it like that:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --ssl

It will try to find the "key.pem" and "crt.pem" at /etc/yunohost/certs/ci-apps.yunohost.org/key.pem and /etc/yunohost/certs/ci-apps.yunohost.org/crt.pem (this is "ci-apps" container configuration on bearnaise LXC).

This can be changed this way:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --ssl -k /path/to/key.pem -c /path/to/cert.pem

### Cli tool

The cli tool is called "ciclic" because my humour is legendary.

Basic tool signature:

```
$ ve3/bin/python ciclic
usage: ciclic [-h] {add,list,delete,stop,restart} ...
```

This tool works by doing a http request to the CI instance of you choice, by
default the domain is "http://localhost:4242", you cand modify this for EVERY
command by add "-d https://some.other.domain.com/path/".

##### Configuration

Because it does request other HTTP, this tool needs configuration for authentification. To do that, you need to do 2 thinks:

* generate a random token, for example using: `< /dev/urandom tr -dc _A-Za-z0-9 | head -c${1:-80};echo;>`
* on the place where the CLI TOOL is installed, at the same place that "ciclic", put this password in a file named `token`
* on the place where the SERVER (run.py) is running, place this token in a file name `tokens` (with a S at the end), one token per line.

That's it.

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

Agplv3+

Copyright YunoHost 2018 (you can find the authors in the commits)
