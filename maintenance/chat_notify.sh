#!/bin/bash

#
# To install before using:
#
# ... Aleks copied the matrix-commander-rs bin and credentials.json from another machine ...
# (and tweaked the device id and room id)
# ... because the latest bin from upstream wants a more recent glibc :|
#

# groupadd matrixcommander
# usermod -a -G  matrixcommander yunorunner
# chgrp -R matrixcommander /etc/matrix-commander-rs/
# chmod -R 770 /etc/matrix-commander-rs/
#

timeout 10 /usr/bin/matrix-commander-rs \
    -c /etc/matrix-commander-rs/credentials.json \
    --store /etc/matrix-commander-rs/store \
    --sync off \
    --markdown \
    -m "$*"
