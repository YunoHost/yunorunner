#!/bin/bash

if [ "${0:0:1}" == "/" ]; then script_dir="$(dirname "$0")"; else script_dir="$(echo $PWD/$(dirname "$0" | cut -d '.' -f2) | sed 's@/$@@')"; fi

cd $script_dir

if [ $# -ne 4 ]
then
    echo "This script need to take in argument the package which be tested, the name of the test, and the ID of the worker."
    exit 1
fi

mkdir -p /var/run/yunorunner/locks/

worker_id="$4"
lock_yunorunner="/var/run/yunorunner/locks/${worker_id}.lock"
lock_package_check="./package_check/pcheck-${worker_id}.lock"

# 10800 sec / 60 = 180 min = 3 hours
TIMEOUT="10800"

BASE_URL="$(cat "./config.py" | tr -d ' "' | grep "^BASE_URL=" | cut --delimiter="=" --fields=2)"
ynh_branch="$(cat "./config.py" | tr -d ' "' | grep "^YNH_BRANCH=" | cut --delimiter="=" --fields=2)"
arch="$(dpkg --print-architecture)"
dist="$(cat "./config.py" | tr -d ' "' | grep "^DIST=" | cut --delimiter="=" --fields=2)"

# Enable chat notifications only on main CI
if [[ "$ynh_branch" == "stable" ]] && [[ "$arch" == "amd64" ]] && [[ -e "./maintenance/chat_notify.sh" ]]
then
    chat_notify="./maintenance/chat_notify.sh"
else
    chat_notify="true" # 'true' is a dummy program that won't do anything
fi

#=================================================
# Delay the beginning of this script, to prevent concurrent executions
#=================================================

# Get 3 ramdom digit. To build a value between 001 and 999
milli_sleep=$(head --lines=20 /dev/urandom | tr --complement --delete '0-9' | head --bytes=3)
# And wait for this value in millisecond
sleep "5.$milli_sleep"

#============================
# Check / take the lock
#=============================

if [ -e $lock_yunorunner ]
then
    lock_yunorunner_PID="$(cat $lock_yunorunner)"
    if [ -n "$lock_yunorunner_PID" ]
    then
	# We check that the corresponding PID is still running AND that the PPid is not 1 ..
	# If the PPid is 1, it tends to indicate that a previous analyseCI is still running and was not killed, and therefore got adopted by init.
	# This typically happens when the job is cancelled / restarted .. though we should have a better way of handling cancellation from yunorunner directly :/
        if ps --pid $lock_yunorunner_PID | grep --quiet $lock_yunorunner_PID && [[ $(grep PPid /proc/${lock_yunorunner_PID}/status | awk '{print $2}') != "1" ]]
        then
            echo -e "\e[91m\e[1m!!! Another analyseCI process is currently using the lock $lock_yunorunner !!!\e[0m"
            "$chat_notify" "CI miserably crashed because another process is using the lock"
            sleep 10
            exit 1
        fi
    fi
    [[ $(grep PPid /proc/$(lock_yunorunner_PID)/status | awk '{print $2}') != "1" ]] && { echo "Killing stale analyseCI process ..."; kill -s SIGTERM $lock_yunorunner_PID; sleep 30; }
    echo "Removing stale lock"
    rm -f $lock_yunorunner
fi

echo "$$" > $lock_yunorunner

#============================
# Cleanup after exit/kill
#=============================

function cleanup()
{
    rm $lock_yunorunner

    if [ -n "$package_check_pid" ]
    then
        kill -s SIGTERM $package_check_pid
        WORKER_ID="$worker_id" ARCH="$arch" DIST="$dist" YNH_BRANCH="$ynh_branch" "./package_check/package_check.sh" --force-stop
    fi
}

trap cleanup EXIT
trap 'exit 2' TERM

#============================
# Test parameters
#=============================

repo="$1"
test_name="$2"
job_id="$3"

# Keep only the repositery
repo=$(echo $repo | cut --delimiter=';' --fields=1)
app="$(echo $test_name | awk '{print $1}')"

test_full_log=${app}_${arch}_${ynh_branch}_complete.log
test_json_results="./results/logs/${app}_${arch}_${ynh_branch}_results.json"
test_url="$BASE_URL/job/$job_id"

# Make sure /usr/local/bin is in the path, because that's where the lxc/lxd bin lives
export PATH=$PATH:/usr/local/bin

#=================================================
# Timeout handling utils
#=================================================

function watchdog() {
    local package_check_pid=$1
    # Start a loop while package check is working
    while ps --pid $package_check_pid | grep --quiet $package_check_pid
    do
        sleep 10

        if [ -e $lock_package_check ]
        then
            lock_timestamp="$(stat -c %Y $lock_package_check)"
            current_timestamp="$(date +%s)"
            if [[ "$(($current_timestamp - $lock_timestamp))" -gt "$TIMEOUT" ]]
            then
                kill -s SIGTERM $package_check_pid
                rm -f $lock_package_check
                force_stop "Package check aborted, timeout reached ($(( $TIMEOUT / 60 )) min)."
                return 1
            fi
        fi
    done

    if [ ! -e "./package_check/results-$worker_id.json" ]
    then
        force_stop "It looks like package_check did not finish properly ... on $test_url"
        return 1
    fi
}

function force_stop() {
    local message="$1"

    echo -e "\e[91m\e[1m!!! $message !!!\e[0m"

    "$chat_notify" "While testing $app: $message"

    WORKER_ID="$worker_id" ARCH="$arch" DIST="$dist" YNH_BRANCH="$ynh_branch" "./package_check/package_check.sh" --force-stop
}

#=================================================
# The actual testing ...
#=================================================

# Exec package check according to the architecture
echo "$(date) - Starting a test for $app on architecture $arch distribution $dist with yunohost $ynh_branch"

rm -f "./package_check/Complete-$worker_id.log"
rm -f "./package_check/results-$worker_id.json"

# Here we use a weird trick with 'script -qefc'
# The reason is that :
# if running the command in background (with &) the corresponding command *won't be in a tty* (not sure exactly)
# therefore later, the command lxc exec -t *won't be in a tty* (despite the -t) and command outputs will appear empty...
# Instead, with the magic of script -qefc we can pretend to be in a tty somehow...
# Adapted from https://stackoverflow.com/questions/32910661/pretend-to-be-a-tty-in-bash-for-any-command
cmd="WORKER_ID=$worker_id ARCH=$arch DIST=$dist YNH_BRANCH=$ynh_branch nice --adjustment=10 './package_check/package_check.sh' '$repo' 2>&1"
script -qefc "$cmd" &

watchdog $! || exit 1

# Copy the complete log
cp "./package_check/Complete-$worker_id.log" "./results/logs/$test_full_log"
cp "./package_check/results-$worker_id.json" "$test_json_results"
rm -f "./package_check/Complete-$worker_id.log"
rm -f "./package_check/results-$worker_id.json"
mkdir -p "./summary/"
[ ! -e "./package_check/summary.png" ] || cp "./package_check/summary.png" "./summary/${job_id}.png"

if [ -n "$BASE_URL" ]
then
    full_log_path="$BASE_URL/logs/$test_full_log"
else
    full_log_path="$(pwd)/logs/$test_full_log"
fi

echo "The complete log for this application was duplicated and is accessible at $full_log_path"

echo ""
echo "-------------------------------------------"
echo ""

#=================================================
# Check / update level of the app
#=================================================

public_result_list="./results/logs/list_level_${ynh_branch}_$arch.json"
[ -s "$public_result_list" ] || echo "{}" > "$public_result_list"

# Check that we have a valid json...
jq -e '' "$test_json_results" >/dev/null 2>/dev/null && bad_json="false" || bad_json="true"

# Get new level and previous level
app_level="$(jq -r ".level" "$test_json_results")"
previous_level="$(jq -r ".$app" "$public_result_list")"

# We post message on chat if we're running for tests on stable/amd64
if [ "$bad_json" == "true" ] || [ "$app_level" -eq 0 ]; then
    message="Application $app completely failed the continuous integration tests"
elif [ -z "$previous_level" ]; then
    message="Application $app rises from level (unknown) to level $app_level"
elif [ $app_level -gt $previous_level ]; then
    message="Application $app rises from level $previous_level to level $app_level"
elif [ $app_level -lt $previous_level ]; then
    message="Application $app goes down from level $previous_level to level $app_level"
elif [ $app_level -ge 6 ]; then
    # Dont notify anything, reduce CI flood on app chatroom
    message=""
else
    message="Application $app stays at level $app_level"
fi

# Send chat notification
if [[ -n "$message" ]]
then
    message+=" on $test_url"
    echo $message
    "$chat_notify" "$message"
fi

# Update/add the results from package_check in the public result list
if [ "$bad_json" == "false" ]
then
    jq --argfile results "$test_json_results" ".\"$app\"=\$results" $public_result_list > $public_result_list.new
    mv $public_result_list.new $public_result_list
fi

# Annnd we're done !
echo "$(date) - Test completed"

[ "$app_level" -gt 5 ] && exit 0 || exit 1
