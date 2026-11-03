#!/bin/bash
# QA Agent — runs every 5 minutes
# Start this from within the financial-qa directory

# Labels that mean a bug has already been triaged and is somewhere in (or
# parked outside) the pipeline. `bug` is a permanent type label, so an open
# bug only needs QA when it carries none of these (bug #508).
QA_TRIAGED_BUG_LABELS=(
  needs-test-plan ready-for-implementation needs-design-review
  needs-design-changes needs-qa-testing needs-fixes ready-for-pr
  blocked needs-clarification needs-human-decision deferred triaged
)

# Pre-flight check: returns 0 if Claude should run, 1 if there's nothing to do.
qa_has_work() {
  local label count
  for label in needs-qa-testing needs-test-plan; do
    count=$(gh issue list --label "$label" --state open --json number 2>/dev/null \
      | jq 'length' 2>/dev/null)
    if [ "${count:-0}" -gt 0 ] 2>/dev/null; then
      echo "=== Queue hit: $count issue(s) labeled '$label' ==="
      return 0
    fi
  done

  # Exact label-name match: `needs-test-plan-old` does not count as triaged.
  count=$(gh issue list --label bug --state open --json labels 2>/dev/null \
    | jq '[.[] | select([.labels[].name] | any(IN($ARGS.positional[])) | not)] | length' \
        --args "${QA_TRIAGED_BUG_LABELS[@]}" 2>/dev/null)
  if [ "${count:-0}" -gt 0 ] 2>/dev/null; then
    echo "=== Queue hit: $count untriaged bug(s) ==="
    return 0
  fi

  echo "=== Skipping: no items in any QA queue ==="
  return 1
}

# Allow `source scripts/run-qa.sh` to load qa_has_work without starting the loop.
[ "${BASH_SOURCE[0]}" != "$0" ] && return 0

while true; do
  echo ""
  echo "=== QA Agent: $(date) ==="

  if qa_has_work; then
    claude --dangerously-skip-permissions -p "/work-qa"
    echo "=== QA done. Next run in 5 minutes ==="
  fi

  sleep 300
done
