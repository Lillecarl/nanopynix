#!/usr/bin/env bash
#
# Move the `last-green` branch to the commit whose full matrix just passed.
#
# **What it is for.** A red `develop` gives no answer to "what was the last
# commit that worked", and the person who needs that answer is the one
# bisecting. This branch is that answer, kept by CI rather than by hand.
#
# **What "green" means is decided by the caller, and not here.** The workflow
# runs this only when every gating job of the matrix succeeded. The macOS job
# is not one of those: it carries `continue-on-error`, it is red on every
# recent run, and issues #143, #210 and #219 hold it. So `last-green` means
# "every gate that this repository trusts today", and not "every job".
#
# **It moves forward, and it will not move back.** Two runs can finish out of
# order -- a re-run of an old commit, or a slow matrix on an older push -- and
# a plain force-push would then point the branch at an *older* commit than it
# already holds. That is worse than useless, because the whole value of the
# branch is that a reader can trust it. So this checks first, and says so when
# it declines.
#
# The push is forced anyway, for the case the check allows: `develop` is never
# force-pushed, so a fast-forward is the normal path, and the force costs
# nothing while covering a branch that was moved by hand.
#
# **The caller must check out the whole history.** The ancestry check below
# walks from one commit to the other, and a shallow clone has nothing to walk:
# the two tips share no visible ancestry, the check answers "not ahead" every
# time, and the branch stops advancing after the first run. That failure is
# silent and looks like nothing happening, which is why it is written here as
# well as in the job that sets `fetch-depth: 0`.
set -euo pipefail

branch="${LAST_GREEN_BRANCH:?LAST_GREEN_BRANCH must name the branch to move}"
commit="${LAST_GREEN_COMMIT:?LAST_GREEN_COMMIT must name the commit that passed}"

# The branch this moves is not a local one: the checkout brings `develop`, and
# this fetches the branch to compare against.
if git fetch --quiet origin "refs/heads/${branch}:refs/remotes/origin/${branch}" 2>/dev/null; then
    current="$(git rev-parse "refs/remotes/origin/${branch}")"
    echo "${branch} is at ${current}"
    if [ "${current}" = "${commit}" ]; then
        echo "${branch} already names ${commit}; nothing to do"
        exit 0
    fi
    # `--is-ancestor` answers "is the branch behind this commit", which is the
    # only direction worth taking. An unrelated commit answers no as well, and
    # declining is right for that too.
    if ! git merge-base --is-ancestor "${current}" "${commit}"; then
        echo "${commit} is not ahead of ${branch}; leaving ${branch} where it is"
        exit 0
    fi
else
    echo "${branch} does not exist yet; creating it"
fi

git push --force origin "${commit}:refs/heads/${branch}"
echo "${branch} now names ${commit}"
