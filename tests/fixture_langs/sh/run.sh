#!/bin/bash
set -e
source ./lib.sh

export TMP_FILE=/tmp/x

main() {
  helper "$1"
  cleanup
}

main "$@"
