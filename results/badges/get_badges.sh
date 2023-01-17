#!/bin/bash

# Get the path of this script
script_dir="$(dirname "$(realpath "$0")")"

wget -q https://upload.wikimedia.org/wikipedia/commons/1/1d/No_image.svg -O "$script_dir/maintained.svg"
wget -q https://img.shields.io/badge/Status-Package%20not%20maintained-red.svg -O "$script_dir/unmaintained.svg"

wget -q https://img.shields.io/badge/Status-working-brightgreen.svg -O "$script_dir/working.svg"
wget -q https://img.shields.io/badge/Status-Just%20got%20added%20to%20catalog-yellowgreen.svg -O "$script_dir/just-got-added-to-catalog.svg"
wget -q https://img.shields.io/badge/Status-In%20progress-orange.svg -O "$script_dir/inprogress.svg"
wget -q https://img.shields.io/badge/Status-Not%20working-red.svg -O "$script_dir/notworking.svg"
wget -q https://img.shields.io/badge/Status-Broken-red.svg -O "$script_dir/broken.svg"
