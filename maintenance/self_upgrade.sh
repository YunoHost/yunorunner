#!/bin/bash

# This script is designed to be used in a cron file

#=================================================
# Grab the script directory
#=================================================

if [ "${0:0:1}" == "/" ]; then script_dir="$(dirname "$0")"; else script_dir="$(echo $PWD/$(dirname "$0" | cut -d '.' -f2) | sed 's@/$@@')"; fi

cd $script_dir/..

# We only self-upgrade if we're in a git repo on master branch
# (which should correspond to production contexts)
[[ -d ".git" ]] || exit

[[ $(git rev-parse --abbrev-ref HEAD) == "master" ]] || exit

git fetch origin --quiet

# If already up to date, don't do anything else
[[ $(git rev-parse HEAD) == $(git rev-parse origin/master) ]] && exit

git reset --hard origin/master --quiet
