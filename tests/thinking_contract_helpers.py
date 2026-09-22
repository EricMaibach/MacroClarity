"""Shared helpers for the thinking-parameter contract tests (Bugs #55, #131).

Not a test module — pytest does not collect it.

Two ideas here worth keeping:

1. **Grep the code, not the prose.** `budget_tokens` and `"type": "enabled"`
   legitimately appear in explanatory comments and docstrings that describe why
   they were removed. A naive substring search over raw source would fail on
   the very documentation that records the fix, so `executable_code()` strips
   comments and docstrings before matching.

   It deliberately keeps ordinary string literals: `budget_tokens` and
   `thinking` appear in the request as *dict keys*, so stripping every string
   would remove exactly what needs guarding and leave a test that can never
   fail.

2. **Check the whole package, not one file.** The request shape used to live in
   `ai_summary.py`; it now lives in `anthropic_request.py`. A guard pinned to a
   single path stops guarding the moment the code moves, without ever going
   red. These helpers sweep every module under `signaltrackers/`.
"""

import ast
import importlib.util
import io
import os
import sys
import tokenize
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'

# The only accepted on-mode on the current models. '{"type": "enabled"}' with a
# budget_tokens field, and '{"type": "disabled"}', both return 400.
EXPECTED_THINKING = {'type': 'adaptive'}
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')


def _docstring_spans(source):
    """Start positions of every module/class/function docstring in `source`."""
    spans = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - all package modules parse
        return spans
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, 'body', None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.add((first.value.lineno, first.value.col_offset))
    return spans


def executable_code(path):
    """Return `path`'s source with comments and docstrings removed.

    Ordinary string literals are preserved: the constructs under guard
    (`budget_tokens`, `'thinking'`, `'adaptive'`) appear as dict keys and
    values, so dropping all strings would make every guard vacuous.
    """
    source = Path(path).read_text(encoding='utf-8', errors='replace')
    docstrings = _docstring_spans(source)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):  # pragma: no cover
        return source
    kept = []
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            continue
        kept.append(tok.string)
    return '\n'.join(kept)


def package_code():
    """Map of relative path -> executable code for every signaltrackers module."""
    sources = {}
    for path in sorted(SIGNALTRACKERS_DIR.rglob('*.py')):
        rel = path.relative_to(REPO_ROOT).as_posix()
        sources[rel] = executable_code(path)
    return sources


def files_containing(needle):
    """Relative paths of package modules whose executable code contains `needle`."""
    return sorted(rel for rel, code in package_code().items() if needle in code)


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(
        name, SIGNALTRACKERS_DIR / relative_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def request_module(env=None):
    """Load `anthropic_request` over a freshly-evaluated `config`.

    Both modules bind ANTHROPIC_EFFORT at import time, so an env override only
    takes effect if both are reloaded — the same chain a container start uses.
    Other modules in this suite leave MagicMocks in sys.modules for 'config'
    and 'services', hence the snapshot/restore.
    """
    if str(SIGNALTRACKERS_DIR) not in sys.path:
        sys.path.insert(0, str(SIGNALTRACKERS_DIR))
    with patch.dict(os.environ, env or {}, clear=False), patch.dict(sys.modules):
        for cached in ('config', 'anthropic_request', 'services'):
            sys.modules.pop(cached, None)
        _load('config', 'config.py')
        yield _load('anthropic_request', 'anthropic_request.py')


def build_sample_request(env=None, **overrides):
    """Build a representative request payload through the real code path."""
    params = dict(
        model='claude-fable-5-1',
        max_tokens=16000,
        system='system prompt',
        messages=[{'role': 'user', 'content': 'hello'}],
    )
    params.update(overrides)
    with request_module(env) as mod:
        return mod.build_request_params(**params)
