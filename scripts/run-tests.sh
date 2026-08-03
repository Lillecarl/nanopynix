#!/usr/bin/env bash
#
# Run a test command in a transient systemd user unit, so that it survives the
# shell that started it.
#
# A run of this suite takes minutes, and the shell that starts it does not
# always live that long: an agent harness kills its background tasks, a
# terminal closes, an editor restarts. The work dies with the shell, and the
# evidence dies with it. A transient unit has its own lifetime and its own
# cgroup, so the run continues, the whole process tree is accounted for, and
# one `systemctl --user stop` ends all of it.
#
# The unit also gets `CPUWeight`, because the usual reason a run dies is
# another build on the same machine rather than a defect in the run.
#
# The output goes to a file rather than to the journal. journald rate-limits a
# noisy service and drops the rest, and a dropped line is the line that
# explains the failure.
#
# Examples:
#
#   scripts/run-tests.sh tests/gates
#   scripts/run-tests.sh --wait tests/nanopynix/bindings -x
#   scripts/run-tests.sh --label nogc --memory-max 4G --raw ./result/bin/nanopynix-tests tests
#
# `--wait` blocks and exits with the status of the run. The unit still owns the
# work, so a kill of this script leaves the run alive; reattach with --status.

set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
log_dir=${NANOPYNIX_RUN_LOG_DIR:-$repo_root/.test-runs}

label=""
wait_for_it=0
raw=0
memory_max=""
cpu_weight=${NANOPYNIX_RUN_CPU_WEIGHT:-50}

usage() {
    cat >&2 <<'EOF'
usage: run-tests.sh [options] [pytest args...]
       run-tests.sh [options] --raw <command> [args...]
       run-tests.sh --status [label]
       run-tests.sh --stop [label]

options:
  --label NAME   name the unit and the log (default: derived from the arguments)
  --wait         block until the run finishes, and exit with its status
  --raw          run the command as given, rather than pytest in the dev shell
  --memory-max N cap the run at N (for example 2G), and forbid swap
  --status       show the state of a run, its peak memory, and the tail of its log
  --stop         stop a run, and everything it started
EOF
}

# systemd reports the peak of a unit when the unit stops, and it reports the
# peak to the journal rather than to the output of the run. A transient unit
# that succeeded is already unloaded by then, so `systemctl show -p MemoryPeak`
# answers `[not set]` and the number is gone. The journal keeps it.
show_peak() {
    journalctl --user --no-pager --since "-1day" 2>/dev/null |
        grep "$1.service: Consumed" | tail -n 1 >&2 || true
}

# `--status` and `--stop` take the label of an earlier run, so they are handled
# before the options that describe a new one.
case "${1:-}" in
--status | --stop)
    action=$1
    shift
    unit="nanopynix-${1:-run}"
    if [[ $action == --stop ]]; then
        systemctl --user stop "$unit.service"
        echo "stopped $unit" >&2
        exit 0
    fi
    systemctl --user show "$unit.service" \
        -p ActiveState -p SubState -p Result -p ExecMainStatus -p ExecMainStartTimestamp >&2
    show_peak "$unit"
    echo "--- tail of $log_dir/$unit.log ---" >&2
    tail -n "${NANOPYNIX_RUN_TAIL:-40}" "$log_dir/$unit.log" >&2 || true
    exit 0
    ;;
-h | --help)
    usage
    exit 0
    ;;
esac

while (($#)); do
    case $1 in
    --label)
        label=$2
        shift 2
        ;;
    --wait)
        wait_for_it=1
        shift
        ;;
    --raw)
        raw=1
        shift
        break
        ;;
    --memory-max)
        memory_max=$2
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *) break ;;
    esac
done

if (($# == 0)); then
    usage
    exit 2
fi

# A label with no directory separators, so that it names both a unit and a file.
if [[ -z $label ]]; then
    label=$(printf '%s' "$1" | tr -c 'a-zA-Z0-9' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')
    label=${label:-run}
fi
unit="nanopynix-$label"
log="$log_dir/$unit.log"

mkdir -p "$log_dir"
rm -f "$log"

if ((raw)); then
    command=("$@")
else
    # `nix develop --file .` rather than the flake: `packages.shell` cannot
    # evaluate in a pure flake evaluation, which is why CLAUDE.md builds every
    # gate with `--file .` as well.
    command=(nix develop --print-build-logs --file . shell --command pytest "$@")
fi

# A unit of the same name from an earlier run blocks this one, and the earlier
# run is finished by the time anybody starts another.
systemctl --user reset-failed "$unit.service" 2>/dev/null || true

# CLAUDECODE reaches pytest-agent, which writes the per-test detail this
# repository's workflow depends on. systemd gives a unit a clean environment,
# so without this the run loses `pytest-agent digest`, `rerun` and `history`.
# No `--collect`. That sets CollectMode=inactive-or-failed, which unloads the
# unit as soon as it stops -- including a unit that failed, whose exit status is
# the thing worth reading afterwards. systemd already unloads a transient unit
# that succeeded, so leaving this out costs nothing and keeps each failure
# queryable with `--status`. The `reset-failed` above clears the last one.
systemd_args=(
    --user
    --same-dir
    --unit="$unit"
    --property=CPUWeight="$cpu_weight"
    --property=StandardOutput=file:"$log"
    --property=StandardError=inherit
)
if [[ -n $memory_max ]]; then
    # `MemorySwapMax=0` with it, because a cap without one is not a cap. A run
    # capped at 2G with swap available stayed under the cap and pushed 8.1G to
    # swap instead, which reported success and hid a demand of ten gigabytes.
    systemd_args+=(
        --property=MemoryMax="$memory_max"
        --property=MemorySwapMax=0
    )
fi
if [[ -n ${CLAUDECODE:-} ]]; then
    systemd_args+=(--setenv=CLAUDECODE="$CLAUDECODE")
fi
if ((wait_for_it)); then
    systemd_args+=(--wait)
fi

echo "unit: $unit.service" >&2
echo "log:  $log" >&2

status=0
systemd-run "${systemd_args[@]}" -- "${command[@]}" || status=$?

if ((wait_for_it)); then
    tail -n "${NANOPYNIX_RUN_TAIL:-40}" "$log" >&2 || true
    show_peak "$unit"
    exit "$status"
fi

echo "follow with: tail -f $log" >&2
echo "state with:  $0 --status $label" >&2
