#!/usr/bin/env bash
#
# Watch a GitHub Actions run to completion, then fetch every job's log,
# triage each one through fabric in parallel, and produce one final
# cross-job summary. Everything is written under a mktemp -d directory
# whose path is printed so the full logs and per-job/final summaries can be
# read afterwards.

set -euo pipefail

if (($# != 1)); then
    echo "usage: $0 <run-id>" >&2
    exit 2
fi

run_id=$1
watch_timeout=${WATCH_GH_RUN_TIMEOUT:-1800}

work_dir=$(mktemp -d -t "gh-run-${run_id}-XXXXXX")
echo "workdir: $work_dir" >&2

# 1. Watch until the run completes (or we give up waiting -- a wedged
# runner/infra job could otherwise block forever even after nanopynix's own
# pytest-level hang fixes).
if timeout "$watch_timeout" gh run watch "$run_id" --exit-status; then
    echo "run $run_id finished" >&2
else
    echo "run $run_id did not cleanly finish within ${watch_timeout}s (or a job failed); continuing to collect whatever logs exist" >&2
fi

# 2. Fetch every non-skipped job's log and triage it through fabric, all in
# parallel.
mapfile -t jobs < <(
    gh run view "$run_id" --json jobs \
        --jq '.jobs[] | select(.conclusion != "skipped") | "\(.databaseId)\t\(.name)"'
)

if ((${#jobs[@]} == 0)); then
    echo "no non-skipped jobs found for run $run_id" >&2
    exit 1
fi

declare -a summary_files=()
declare -a pids=()

for job in "${jobs[@]}"; do
    job_id=${job%%$'\t'*}
    job_name=${job#*$'\t'}
    safe_name=$(tr -c 'A-Za-z0-9_.-' '_' <<<"$job_name")

    log_file="$work_dir/job-${job_id}-${safe_name}.log"
    summary_file="$work_dir/job-${job_id}-${safe_name}.summary.md"
    summary_files+=("$summary_file")

    (
        gh run view --job "$job_id" --log >"$log_file" 2>&1 || true
        {
            echo "### Job: $job_name (id $job_id)"
            echo
            fabric --pattern analyze_logs <"$log_file" || echo "(fabric analysis failed for this job)"
        } >"$summary_file"
    ) &
    pids+=("$!")
done

for pid in "${pids[@]}"; do
    wait "$pid"
done

echo "per-job logs and summaries written under: $work_dir" >&2

# 3. Concatenate every per-job summary and produce one final cross-job
# summary.
final_summary="$work_dir/final-summary.md"
cat "${summary_files[@]}" | fabric --pattern combine_ci_job_summaries >"$final_summary"

echo "final summary: $final_summary" >&2
cat "$final_summary"

#if [[ -n ${TMUX:-} ]]; then
#    tmux send-keys "gh run $run_id fully processed: $final_summary" || true
#    sleep 1
#    tmux send-keys enter || true
#fi
