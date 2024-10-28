#!/usr/bin/env bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

LOGS_DIR=$(dirname "$SCRIPT_DIR")/results/logs

pushd "$LOGS_DIR" || exit 1

# Compress older-than-one-week files
find . -mtime +7 -name '*.log' -print0 \
| xargs -0 -P 4 gzip
