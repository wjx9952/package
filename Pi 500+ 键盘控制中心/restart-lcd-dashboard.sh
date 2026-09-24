#!/bin/sh
set -eu

if systemctl --user restart codex-lcd-hat.service 2>/dev/null; then
    systemctl --user is-active codex-lcd-hat.service
    exit 0
fi

systemctl --machine="$(id -un)@.host" --user restart codex-lcd-hat.service
systemctl --machine="$(id -un)@.host" --user is-active codex-lcd-hat.service
