#!/bin/bash

# Récupère le dossier du script
if [ "${0:0:1}" == "/" ]; then script_dir="$(dirname "$0")"; else script_dir="$(echo $PWD/$(dirname "$0" | cut -d '.' -f2) | sed 's@/$@@')"; fi

# Only run this on the true main, official CI, prevent instances from packagers to push weird stuff
grep -q "BASE_URL.*=.*ci-apps.yunohost.org" "$script_dir/../config" || exit 0

# Supprime le précédent clone de YunoHost/apps
rm -r "$script_dir/../../apps"	

# Se place dans le dossier du dépot git pour le script python
tmp_dir=$(mktemp -d)
git clone -q git@github.com:YunoHost/apps.git "$tmp_dir"
cd $tmp_dir

# Créer une nouvelle branche pour commiter les changements
git checkout -b modify_level

public_result_list="$script_dir/results/logs/list_level_stable_amd64.json"
majorregressions=""
minorregressions=""
improvements=""

# For each app in the result file
for APP in $(jq -r 'keys[]' "$public_result_list")
do
    # Check if the app is still in the list
    if ! jq -r 'keys[]' "apps.json" | grep -qw $APP; then
        continue
    fi

    current_level="$(jq -r ".\"$APP\".level" apps.json)"
    # Get the level from the stable+amd64 tests
    new_level="$(jq -r ".\"$APP\".level" "$public_result_list")"

    if [[ "$current_level" != "null" ]] && [[ "$new_level" -lt "$current_level" ]]
    then
        if [[ "$new_level" -le 4 ]] && [[ "$current_level" -gt 4 ]]
        then
            majorregressions+="  - $APP $current_level -> $new_level | https://ci-apps.yunohost.org/ci/apps/$APP/latestjob\n"
        else
            minorregressions+="  - $APP $current_level -> $new_level | https://ci-apps.yunohost.org/ci/apps/$APP/latestjob\n"
        fi
    elif [[ "$new_level" != "$current_level" ]]
    then
        improvements+="  - $APP $current_level -> $new_level | https://ci-apps.yunohost.org/ci/apps/$APP/latestjob\n"
    fi

    # Inject the new level value to apps.json
    jq --sort-keys --indent 4 --arg APP $APP --argjson new_level $new_level '.[$APP].level=$new_level' apps.json > apps.json.new
    mv apps.json.new apps.json
done

# Affiche les changements (2 lignes de contexte suffisent à voir l'app)
# git diff -U2 --raw
# Ajoute les modifications des listes au prochain commit
git add --all *.json
git commit -q -m "Update app levels according to CI results$(echo -e "\n\n### Major Regressions\n$majorregressions\n\n### Minor Regressions\n$minorregressions\n\n### Improvements\n$improvements")"

# Git doit être configuré sur la machine.
# git config --global user.email "MAIL..."
# git config --global user.name "yunohost-bot"
# ssh-keygen -t rsa -f $HOME/.ssh/github -P ''		Pour créer une clé ssh sans passphrase
# Host github.com
# IdentityFile ~/.ssh/github
# Dans le config ssh
# Et la clé doit être enregistrée dans le compte github de yunohost-bot
git push -q -u origin modify_level
