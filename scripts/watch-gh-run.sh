#!/usr/bin/env bash

set -euo pipefail

if (($# != 1)); then
    echo "usage: $0 <run-id>" >&2
    exit 2
fi

run_id=$1
deadline=$((SECONDS + 600))

while ((SECONDS < deadline)); do
    status=$(gh run view "$run_id" --json status --jq .status)
    if [[ $status == completed ]]; then
        conclusion=$(gh run view "$run_id" --json conclusion --jq .conclusion)
        if [[ -n ${TMUX:-} ]]; then
            tmux send-keys "ci run finished with status $conclusion" || true
            sleep 1
            tmux send-keys enter || true
        fi
        gh run view "$run_id" --log | nix run n#fabric-ai -- --pattern cleaning_logs
        [[ $conclusion == success ]]
        exit
    fi

    last_log_line=$(gh run view "$run_id" --log 2>/dev/null | tail -n 1 || true)
    if [[ -n $last_log_line ]]; then
        echo "run $run_id is $status; latest log: $last_log_line" >&2
    else
        echo "run $run_id is $status; no log line available yet" >&2
    fi
    echo "checking again in 10 seconds" >&2
    sleep 10
done

echo "timed out waiting 10 minutes for run $run_id" >&2
exit 124
