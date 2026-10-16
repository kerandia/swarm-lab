#!/bin/sh
# Fetch the Anatomy skill (MIT, https://github.com/wheresryan22/anatomy) at the commit
# that was reviewed for this project, into skills/anatomy/ for the Docker build.
set -eu
COMMIT=e948829
cd "$(dirname "$0")"
rm -rf skills/anatomy
mkdir -p skills
curl -fsSL "https://codeload.github.com/wheresryan22/anatomy/tar.gz/$COMMIT" -o anatomy.tgz
mkdir -p skills/anatomy
tar -xzf anatomy.tgz -C skills/anatomy --strip-components=1
rm anatomy.tgz
echo "Anatomy skill $COMMIT in $(pwd)/skills/anatomy"
