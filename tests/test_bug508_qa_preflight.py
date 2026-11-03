"""Bug #508: run-qa.sh must only wake Claude for real QA work.

`bug` is a permanent type label, so an open bug anywhere in the pipeline used
to start /work-qa every 5 minutes. These tests source only `qa_has_work` and
put a stub `gh` (canned JSON per --label) and a tripwire `claude` on PATH, so
no real issues are read and Claude never starts.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'run-qa.sh'

pytestmark = pytest.mark.skipif(
    shutil.which('bash') is None or shutil.which('jq') is None,
    reason='needs bash and jq',
)

STUB_GH = r'''#!/bin/bash
# Stub gh: prints $STUB_DIR/<label>.out for `gh issue list --label <label>`.
[ -n "$STUB_GH_FAIL" ] && { [ "$STUB_GH_FAIL" = garbage ] && echo '<html>502</html>'; exit 1; }
label=""
while [ $# -gt 0 ]; do
  [ "$1" = "--label" ] && { label="$2"; shift; }
  shift
done
f="$STUB_DIR/$label.out"
if [ -f "$f" ]; then cat "$f"; else echo '[]'; fi
'''


def _issue(*labels):
    return {'number': 1, 'labels': [{'name': n} for n in labels]}


def run_check(tmp_path, queues=None, fail=None):
    """Run qa_has_work with canned issues per label. Returns CompletedProcess."""
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    gh = bindir / 'gh'
    gh.write_text(STUB_GH)
    gh.chmod(0o755)
    claude = bindir / 'claude'
    claude.write_text('#!/bin/bash\necho CLAUDE_STARTED >&2\nexit 99\n')
    claude.chmod(0o755)

    stub_dir = tmp_path / 'gh-data'
    stub_dir.mkdir(exist_ok=True)
    for label, issues in (queues or {}).items():
        (stub_dir / f'{label}.out').write_text(json.dumps(issues))

    env = {
        'PATH': f'{bindir}:/usr/bin:/bin:/usr/local/bin',
        'STUB_DIR': str(stub_dir),
    }
    if fail:
        env['STUB_GH_FAIL'] = fail
    return subprocess.run(
        ['bash', '-c', f'source "{SCRIPT}"; qa_has_work'],
        env=env, capture_output=True, text=True, timeout=30,
    )


def assert_skip(r):
    assert r.returncode == 1, r.stdout + r.stderr
    assert '=== Skipping: no items in any QA queue ===' in r.stdout
    assert r.stderr == ''


def assert_hit(r, text):
    assert r.returncode == 0, r.stdout + r.stderr
    assert text in r.stdout
    assert r.stderr == ''


def test_script_syntax():
    r = subprocess.run(['bash', '-n', str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_sourcing_does_not_start_the_loop(tmp_path):
    r = run_check(tmp_path)
    assert 'QA Agent:' not in r.stdout
    assert 'CLAUDE_STARTED' not in r.stderr


def test_no_work_skips(tmp_path):
    assert_skip(run_check(tmp_path))


def test_bug_ready_for_pr_skips(tmp_path):
    """The original defect: a bug waiting on a human merge woke QA."""
    assert_skip(run_check(tmp_path, {'bug': [_issue('bug', 'ready-for-pr')]}))


@pytest.mark.parametrize('state', [
    'needs-test-plan', 'ready-for-implementation', 'needs-design-review',
    'needs-design-changes', 'needs-qa-testing', 'needs-fixes', 'ready-for-pr',
    'blocked', 'needs-clarification', 'needs-human-decision', 'deferred',
    'triaged',
])
def test_bug_with_state_label_is_not_untriaged(tmp_path, state):
    # Only the bug query sees the issue, so this isolates the bug check.
    assert_skip(run_check(tmp_path, {'bug': [_issue('bug', state)]}))


def test_bare_bug_wakes_qa(tmp_path):
    assert_hit(run_check(tmp_path, {'bug': [_issue('bug')]}),
               'Queue hit: 1 untriaged bug(s)')


def test_bug_with_non_workflow_labels_wakes_qa(tmp_path):
    assert_hit(run_check(tmp_path, {'bug': [_issue('bug', 'P1', 'enhancement')]}),
               'untriaged bug')


def test_label_names_match_exactly(tmp_path):
    assert_hit(run_check(tmp_path, {'bug': [_issue('bug', 'needs-test-plan-old')]}),
               'untriaged bug')


def test_mixed_bugs_count_only_untriaged(tmp_path):
    r = run_check(tmp_path, {'bug': [_issue('bug'), _issue('bug', 'ready-for-pr')]})
    assert_hit(r, 'Queue hit: 1 untriaged bug(s)')


def test_needs_test_plan_queue_wakes_qa(tmp_path):
    r = run_check(tmp_path, {'needs-test-plan': [_issue('user-story', 'needs-test-plan')]})
    assert_hit(r, "labeled 'needs-test-plan'")


def test_needs_qa_testing_queue_wakes_qa(tmp_path):
    r = run_check(tmp_path, {'needs-qa-testing': [_issue('user-story', 'needs-qa-testing')]})
    assert_hit(r, "labeled 'needs-qa-testing'")


@pytest.mark.parametrize('mode', ['empty', 'garbage'])
def test_gh_failure_skips_quietly(tmp_path, mode):
    assert_skip(run_check(tmp_path, fail=mode))


def test_gh_failure_only_for_bug_query_skips_quietly(tmp_path):
    (tmp_path / 'gh-data').mkdir()
    (tmp_path / 'gh-data' / 'bug.out').write_text('not json')
    assert_skip(run_check(tmp_path))
