#!/bin/bash

#
# To install before using:
#
# MCHOME="/opt/matrix-commander"
# MCARGS="-c $MCHOME/credentials.json --store $MCHOME/store"
# mkdir -p "$MCHOME/venv"
# python3 -m venv "$MCHOME/venv"
# source "$MCHOME/venv/bin/activate"
# pip3 install matrix-commander
# chmod 700 "$MCHOME"
# matrix-commander $MCARGS --login password   # < NB here this is literally 'password' as authentication method, the actual password will be asked by a prompt
# matrix-commander $MCARGS --room-join '#yunohost-apps:matrix.org'
#
# groupadd matrixcommander
# usermod -a -G  matrixcommander yunorunner
# chgrp -R matrixcommander $MCHOME
# chmod -R 770 $MCHOME
#

MCHOME="/opt/matrix-commander/"
MCARGS="-c $MCHOME/credentials.json --store $MCHOME/store"
timeout 10 "$MCHOME/venv/bin/matrix-commander" $MCARGS -m "$@"  --room 'yunohost-apps'
