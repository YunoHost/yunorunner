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

#### installing yunorunner

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

If you need this server to be front (without nginx in front of it) you can start it like that:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --ssl

It will try to find the "key.pem" and "crt.pem" at /etc/yunohost/certs/ci-apps.yunohost.org/key.pem and /etc/yunohost/certs/ci-apps.yunohost.org/crt.pem (this is "ci-apps" container configuration on bearnaise LXC).

This can be changed this way:

    ve3/bin/python ./run.py /path/to/analyseCI.sh --ssl -k /path/to/key.pem -c /path/to/cert.pem

### Cli tool

For now it's very shitty and will change once I get the energy ™

The file is "add_job.py" and the usage is the following one:

    $ ve3/bin/python add_job.py
    usage: add_job.py [-h] [-t TEST_TYPE] [-y YUNOHOST_VERSION]
                      [--debian-version DEBIAN_VERSION] [-r REVISION]
                      [--domain DOMAIN]
                      name url-or-path
    add_job.py: error: the following arguments are required: name, url-or-path

For example:

    python add_job.py mumbleserver https://github.com/YunoHost-Apps/mumbleserver_ynh

On the SERVER side logs you will see:

    [2018-08-24 17:48:43 +0200] [12522] [BACKGROUND] [run_job] Starting job 'mumbleserver'...
    [2018-08-24 17:48:43 +0200] [12522] [BACKGROUND] [run_job] Launch command: /bin/bash ./stupidScript.sh https://github.com/YunoHost-Apps/mumbleserver_ynh "mumbleserver"

# Deployment

You need to put this program behind a nginx mod proxy AND add the magical lines
to allow websocket (it's not allowed for whatever reason) and that all the way
through all the proxies (if you deploy on bearnaise's lxc or something
similar).

# Licence

Agplv3+

Copyright YunoHost 2018 (you can find the authors in the commits)
