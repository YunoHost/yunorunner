#!/bin/bash

#=================================================
# Grab the script directory
#=================================================

if [ "${0:0:1}" == "/" ]; then script_dir="$(dirname "$0")"; else script_dir="$(echo $PWD/$(dirname "$0" | cut -d '.' -f2) | sed 's@/$@@')"; fi

#=================================================
# Get the list and check for any modifications
#=================================================

# Get the apps list from app.yunohost.org
wget -nv https://app.yunohost.org/default/v3/apps.json -O "$script_dir/apps.json"

do_update=1
if [ -e "$script_dir/apps.json.md5" ]
then
    if md5sum --check --status "$script_dir/apps.json.md5"
    then
        echo "No changes into the app list since the last execution."
        do_update=0
    fi
fi

if [ $do_update -eq 1 ]
then
    md5sum "$script_dir/apps.json" > "$script_dir/apps.json.md5"

    #=================================================
    # Update badges for all apps
    #=================================================

    # Parse each app into the list
    while read app
    do
        # Get the status for this app
        state=$(jq --raw-output ".apps[\"$app\"] | .state" "$script_dir/apps.json")
        level=$(jq --raw-output ".apps[\"$app\"] | .level" "$script_dir/apps.json")
        if [[ "$state" == "working" ]]
        then
            if [[ "$level" == "null" ]] || [[ "$level" == "?" ]]
            then
                state="just-got-added-to-catalog"
            elif [[ "$level" == "0" ]] || [[ "$level" == "-1" ]]
            then
                state="broken"
            fi
        fi

        # Get the maintained status for this app
        maintained=$(jq --raw-output ".apps[\"$app\"] | .antifeatures | .[]" "$script_dir/apps.json" | grep -q 'package-not-maintained' && echo unmaintained || echo maintained)

        cp "$script_dir/../badges/$state.svg" "$script_dir/../badges/${app}.status.svg"
        cp "$script_dir/../badges/$maintained.svg" "$script_dir/../badges/${app}.maintain.svg"

    # List all apps from the list, by getting manifest ID.
    done <<< "$(jq --raw-output ".apps[] | .manifest.id" "$script_dir/apps.json")"
fi
