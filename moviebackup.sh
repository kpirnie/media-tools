#!/usr/bin/env bash

# setup the local folder
LOCAL_FROM=/mnt/media

# setup the "remote" folder
LOCAL_TO=/mnt/backup

# make sure the TO exists
mkdir -p $LOCAL_TO

# we need to make sure the remote path is actually mounted, so fire up a loop
while true; do

    # check the mounts
    if $(mount | grep -q $LOCAL_TO); then 

        # rsync the rest
        rsync -au $LOCAL_FROM/Music $LOCAL_TO/Music &
        rsync -au $LOCAL_FROM/Shows $LOCAL_TO/Shows &
        rsync -au $LOCAL_FROM/Movies $LOCAL_TO/Movies &

        # break out of the primary loop
        break;

    # otherwise
    else

        # make sure it's mounted
        mount /dev/sdc1 $LOCAL_TO;

    fi;

    # sleep for 1 second
    sleep 1;

done;
