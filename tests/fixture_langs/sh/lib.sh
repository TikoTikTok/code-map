#!/bin/bash
# shared helpers

VERSION=1

# Print the argument with a prefix.
helper() {
  echo "lib: $1"
}

function cleanup {
  rm -f "$TMP_FILE"
}
