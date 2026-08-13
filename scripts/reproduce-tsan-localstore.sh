#!/usr/bin/env bash
#
# Reproduce the released-Nix LocalStore ThreadSanitizer workload in the same
# process order as CI: five concurrency-soak processes, one per seed, followed
# by the mixed build/evaluation/query workload.  Each soak deals every eligible
# test into overlapping lanes, so a process is not sequential inside itself;
# the five processes are.  They share one writable LocalStore root so its
# SQLite and store-path state is retained between processes.
#
# A seed fixes which tests overlap, so a run that finds a race can be run
# again.  See nanopynix_testing.soak.

set -euo pipefail

runner=${NANOPYNIX_TSAN_RUNNER:-./result/bin/nanopynix-tests}
timeout_seconds=${NANOPYNIX_TSAN_TIMEOUT:-240}
work_dir=$(mktemp -d -t nanopynix-tsan-localstore-XXXXXX)
store_root="$work_dir/store-root"
log_file="$work_dir/pytest.log"

if [[ ! -x $runner ]]; then
    echo "TSAN test runner is not executable: $runner" >&2
    echo 'Build one first, for example: nix build .#nanopynix-tests-nix_2_34-tsan --out-link result' >&2
    exit 2
fi

echo "workdir: $work_dir"
echo "store root: $store_root"

failure_status=0

run_selection() {
    local selection=$1
    shift
    local runner_status
    local -a pipeline_status

    echo "=== pytest selection: $selection $* ===" | tee -a "$log_file"

    # Fabric's analysis exit status is intentionally separate from pytest's:
    # it is diagnostic output, while the runner status determines success.
    # Keep the pipeline in an `if` so `errexit` cannot end the reproducer when
    # Fabric returns nonzero after producing its analysis.
    if direnv exec . timeout "$timeout_seconds" \
        unshare --user --map-root-user --mount --pid --fork --mount-proc \
        env "NIX_REMOTE=local?root=$store_root" \
            NANOPYNIX_CORE_DEBUG=1 \
            NANOPYNIX_RPC_TIMEOUT=30 \
            PYTHONDONTWRITEBYTECODE=1 \
            "$runner" --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds \
            -m "$selection" "$@" \
        2>&1 | tee -a "$log_file" | fabric --pattern analyze_logs; then
        pipeline_status=("${PIPESTATUS[@]}")
    else
        pipeline_status=("${PIPESTATUS[@]}")
    fi

    runner_status=${pipeline_status[0]}
    echo "=== pytest selection: $selection; runner status: $runner_status ===" | tee -a "$log_file"
    if ((runner_status != 0 && failure_status == 0)); then
        failure_status=$runner_status
    fi
}

for seed in 1 2 3 4 5; do
    run_selection soak --soak-seed="$seed" --soak-report="$work_dir/soak-run$seed.json"
done
run_selection known_nix_tsan_localstore_bug


echo "preserved pytest output: $log_file"
exit "$failure_status"
