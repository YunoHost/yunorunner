#!/bin/bash

cd "$(dirname $(realpath $0))"

if (( $# < 1 ))
then
    cat << EOF
Usage: ./finish_install.sh [auto|manual] [cluster]

1st argument is the CI type (scheduling strategy):
  - auto means job will automatically be scheduled by yunorunner from apps.json etc.
  - manual means job will be scheduled manually (e.g. via webhooks or yunorunner ciclic)

2nd argument is to build the first node of an lxc cluster
 - lxd cluster will be created with the current server
 - some.domain.tld will be the cluster hostname and SecretAdminPasswurzd! the trust password to join the cluster

EOF
    exit 1
fi

YUNORUNNER_HOME="/var/www/yunorunner"
if [ $(pwd) != "$YUNORUNNER_HOME" ]
then
    echo "This script should be ran from $YUNORUNNER_HOME"
    exit 1
fi

ci_type=$3
lxd_cluster=$4

# User which execute the CI software.
ci_user=yunorunner

echo_bold () {
	echo -e "\e[1m$1\e[0m"
}

# -----------------------------------------------------------------

   
function tweak_yunohost() {
	
    #echo_bold "> Setting up Yunohost..."
    #local DIST="bullseye"
    #local INSTALL_SCRIPT="https://install.yunohost.org/$DIST"
    #curl $INSTALL_SCRIPT | bash -s -- -a
	
    #echo_bold "> Running yunohost postinstall"
	#yunohost tools postinstall --domain $domain --password $yuno_pwd
    
    # What is it used for :| ...
    #echo_bold "> Create Yunohost CI user"
    #local ynh_ci_user=ynhci
    #yunohost user create --firstname "$ynh_ci_user" --domain "$domain" --lastname "$ynh_ci_user" "$ynh_ci_user" --password $yuno_pwd

    # Idk why this is needed but wokay I guess >_>
    echo -e "\n127.0.0.1 $domain	#CI_APP" >> /etc/hosts

    echo_bold "> Disabling unecessary services to save up RAM"
    for SERVICE in mysql php7.4-fpm metronome rspamd dovecot postfix redis-server postsrsd yunohost-api avahi-daemon
    do
        systemctl stop $SERVICE
        systemctl disable $SERVICE --quiet
    done
}

function tweak_yunorunner() {
    echo_bold "> Tweaking YunoRunner..."
    
    
    #if ! yunohost app list --output-as json --quiet | jq -e '.apps[] | select(.id == "yunorunner")' >/dev/null
    #then
    #    yunohost app install --force https://github.com/YunoHost-Apps/yunorunner_ynh -a "domain=$domain&path=/$ci_path"
    #fi
    domain=$(yunohost app setting yunorunner domain)
    ci_path=$(yunohost app setting yunorunner path)
    port=$(yunohost app setting yunorunner port)

    # Stop YunoRunner
    # to be started manually by the admin after the CI_package_check install
    # finishes
    systemctl stop $ci_user

    # Remove the original database, in order to rebuilt it with the new config.
    rm -f $YUNORUNNER_HOME/db.sqlite

    # For automatic / "main" CI we want to auto schedule jobs using the app list
    if [ $ci_type == "auto" ]
    then
        cat >/var/www/yunorunner/config.py <<EOF
BASE_URL = "https://$domain/$ci_path"
PORT = $port
PATH_TO_ANALYZER = "$YUNORUNNER_HOME/analyze_yunohost_app.sh"
MONITOR_APPS_LIST = True
MONITOR_GIT = True
MONITOR_ONLY_GOOD_QUALITY_APPS = False
MONTHLY_JOBS = True
WORKER_COUNT = 1
YNH_BRANCH = stable
DIST = $DIST 
EOF
    # For Dev CI, we want to control the job scheduling entirely
    # (c.f. the github webhooks)
    else
        cat >/var/www/yunorunner/config.py <<EOF
BASE_URL = "https://$domain/$ci_path"
PORT = $port
PATH_TO_ANALYZER = "$YUNORUNNER_HOME/analyze_yunohost_app.sh"
MONITOR_APPS_LIST = False
MONITOR_GIT = False
MONITOR_ONLY_GOOD_QUALITY_APPS = False
MONTHLY_JOBS = False
WORKER_COUNT = 1
YNH_BRANCH = stable
DIST = $DIST 
EOF
    fi

    # Add permission to the user for the entire yunorunner home because it'll be the one running the tests (as a non-root user)
    chown -R $ci_user $YUNORUNNER_HOME

    # Put YunoRunner as the default app on the root of the domain
    yunohost app makedefault -d "$domain" yunorunner
}

function setup_lxd() {
    if ! yunohost app list --output-as json --quiet | jq -e '.apps[] | select(.id == "lxd")' >/dev/null
    then
        yunohost app install --force https://github.com/YunoHost-Apps/lxd_ynh
    fi

    echo_bold "> Configuring lxd..."

    if [ "$lxd_cluster" == "cluster" ]
    then
        local free_space=$(df --output=avail / | sed 1d)
        local btrfs_size=$(( $free_space * 90 / 100 / 1024 / 1024 ))
        local lxc_network=$((1 + $RANDOM % 254))

        yunohost firewall allow TCP 8443
        cat >./preseed.conf <<EOF
config:
  cluster.https_address: $domain:8443
  core.https_address: ${domain}:8443
  core.trust_password: ${yuno_pwd}
networks:
- config:
    ipv4.address: 192.168.${lxc_network}.1/24
    ipv4.nat: "true"
    ipv6.address: none
  description: ""
  name: lxdbr0
  type: bridge
  project: default
storage_pools:
- config:
    size: ${btrfs_size}GB
    source: /var/lib/lxd/disks/local.img
  description: ""
  name: local
  driver: btrfs
profiles:
- config: {}
  description: Default LXD profile
  devices:
    lxdbr0:
      nictype: bridged
      parent: lxdbr0
      type: nic
    root:
      path: /
      pool: local
      type: disk
  name: default
projects:
- config:
    features.images: "true"
    features.networks: "true"
    features.profiles: "true"
    features.storage.volumes: "true"
  description: Default LXD project
  name: default
cluster:
  server_name: ${domain}
  enabled: true
EOF
        cat ./preseed.conf | lxd init --preseed
        rm ./preseed.conf
        lxc config set core.https_address [::]
    else
        lxd init --auto --storage-backend=dir
    fi

    # ci_user will be the one launching job, gives it permission to run lxd commands
    usermod -a -G lxd $ci_user

    # We need a home for the "su" command later ?
    mkdir -p /home/$ci_user
    chown -R $ci_user /home/$ci_user

    su $ci_user -s /bin/bash -c "lxc remote add yunohost https://devbaseimgs.yunohost.org --public --accept-certificate"
}

function add_cron_jobs() {
    echo_bold "> Configuring the CI..."
  
    # Cron tasks
    cat >>  "/etc/cron.d/yunorunner" << EOF
# self-upgrade every night
0 3 * * * root "$YUNORUNNER_HOME/maintenance/self_upgrade.sh" >> "$YUNORUNNER_HOME/maintenance/self_upgrade.log" 2>&1

# Update app list
0 20 * * 5 root "$YUNORUNNER_HOME/maintenance/update_level_apps.sh" >> "$YUNORUNNER_HOME/maintenance/update_level_apps.log" 2>&1

# Update badges
0 1 * * * root "$YUNORUNNER_HOME/maintenance/update_badges.sh" >> "$YUNORUNNER_HOME/maintenance/update_badges.log" 2>&1
EOF
}

# =========================
#  Main stuff
# =========================

#git clone https://github.com/YunoHost/package_check "./package_check"
#install_dependencies

[ -e /usr/bin/yunohost ] || { echo "YunoHost is not installed"; exit; }
[ -e /etc/yunohost/apps/yunorunner ] || { echo "Yunorunner is not installed on YunoHost"; exit; }

tweak_yunohost
tweak_yunorunner
setup_lxd
add_cron_jobs

echo "Done!"
echo " "
echo "N.B. : If you want to enable Matrix notification, you should look at "
echo "the instructions inside lib/chat_notify.sh to deploy matrix-commander"
echo ""
echo "You may also want to tweak the 'config' file to run test with a different branch / arch"
echo ""
echo "When you're ready to start the CI, run:    systemctl restart $ci_user"
