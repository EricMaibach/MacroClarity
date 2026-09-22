"""
QA integration tests for US-16.1.1: configured model reaches the API call sites.

The engineer's unit tests (test_us1611_anthropic_model_config.py) pin the
config values and cover the news_pipeline call site. These cover the two gaps
QA found during verification:

  1. ai_summary's briefing call — the highest-cost path — was not asserted at
     the messages.create() boundary, only at the module constant.
  2. dashboard.py's create() call sites were not covered at all. They are the
     only remaining places a model ID could be re-hardcoded without any
     existing test noticing.

It also pins the config -> usage-metering coupling this story creates: the
model string is now operator-settable and flows into MODEL_PRICING lookup.

Test isolation note: other modules in this suite leave MagicMocks in
sys.modules for 'config' and 'services', so every test here loads the modules
it asserts on from their file paths inside a restored sys.modules snapshot.
Importing them normally passes or fails depending on test order.
"""

import ast
import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'

sys.path.insert(0, str(SIGNALTRACKERS_DIR))

# Shipped defaults, moved to the current generation by US-16.1.2.
OPUS = 'claude-fable-5-1'
SONNET = 'claude-sonnet-5'


def load_module(name, relative_path):
    """Load a module straight from its file path, re-executing it."""
    spec = importlib.util.spec_from_file_location(
        name, SIGNALTRACKERS_DIR / relative_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _evict(*prefixes):
    """Drop cached modules so the next import re-executes the real file."""
    for name in list(sys.modules):
        if name in prefixes or name.split('.')[0] in prefixes:
            sys.modules.pop(name, None)


@contextmanager
def fresh_process(env=None):
    """Approximate a fresh interpreter for the model-config chain.

    ai_summary/news_pipeline get their model from `from config import
    ANTHROPIC_MODEL`, and config.py binds that alias from os.environ at import
    time. Re-executing ai_summary alone is therefore NOT enough to pick up an
    env override — an already-imported `config` in sys.modules wins and the
    override is silently ignored. Evicting it is what makes the reload behave
    like container startup, which is the only moment the env var is read in
    production.

    'services' is evicted for a different reason: other test modules leave a
    MagicMock there, which breaks ai_summary's inner
    `from services.usage_metering import ...`.

    sys.modules is snapshotted and restored, so nothing leaks to other tests.
    """
    with patch.dict(os.environ, env or {}, clear=False), patch.dict(sys.modules):
        _evict('config', 'services')
        yield


def _fake_anthropic_response(text='summary text', model=None):
    """A response that ends the tool-calling loop on the first iteration.

    `model` mirrors the real response field: a server-side fallback can serve
    the turn on a different model than the one requested, and that is what the
    usage record must be costed against. Defaults to None so callers that do
    not care fall back to the requested model.
    """
    return SimpleNamespace(
        stop_reason='end_turn',
        stop_details=None,
        model=model,
        content=[SimpleNamespace(type='text', text=text)],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


# ---------------------------------------------------------------------------
# ai_summary — the briefing path, at the API boundary
# ---------------------------------------------------------------------------

class TestAISummaryCallSite:
    def _call(self, env=None):
        with fresh_process(env):
            ai_summary = load_module('ai_summary_under_test', 'ai_summary.py')
            client = MagicMock()
            # client.beta.messages — betas/fallbacks are beta-namespace only.
            client.beta.messages.create.return_value = _fake_anthropic_response()
            with patch.object(ai_summary, 'is_tavily_configured', return_value=False):
                result = ai_summary._call_anthropic_with_tools(
                    client, 'system', 'user', 1000, '[TEST]'
                )
        return client, result

    def test_default_model_reaches_messages_create(self):
        """With nothing set, the briefing call must send the shipped default."""
        client, _ = self._call()
        assert client.beta.messages.create.call_args.kwargs['model'] == OPUS

    def test_configured_model_reaches_messages_create(self):
        client, _ = self._call({'ANTHROPIC_MODEL': 'sentinel-briefing'})
        assert client.beta.messages.create.call_args.kwargs['model'] == 'sentinel-briefing'

    def test_reported_model_matches_model_called(self):
        """The result's 'model' feeds usage metering — it must not drift."""
        client, result = self._call({'ANTHROPIC_MODEL': 'sentinel-briefing'})
        assert result['model'] == client.beta.messages.create.call_args.kwargs['model']

    def test_chatbot_var_does_not_leak_into_briefing(self):
        client, _ = self._call({'ANTHROPIC_CHATBOT_MODEL': 'sentinel-chatbot'})
        assert client.beta.messages.create.call_args.kwargs['model'] == OPUS


# ---------------------------------------------------------------------------
# dashboard — no create() call may name a model literally
# ---------------------------------------------------------------------------

def _create_calls(tree):
    """Every client.messages.create / chat.completions.create call in the tree."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'create':
            yield node


def _effective_keywords(call):
    """Keywords the create() call actually sends.

    US-16.1.2 routes the Anthropic call sites through
    `create(**build_request_params(model=..., ...))`, so the model arrives via
    the helper rather than directly. Unwrap that one level; the guarantee is
    unchanged, since build_request_params has no default model.
    """
    keywords = []
    for kw in call.keywords:
        if (
            kw.arg is None
            and isinstance(kw.value, ast.Call)
            and isinstance(kw.value.func, ast.Name)
            and kw.value.func.id == 'build_request_params'
        ):
            keywords.extend(kw.value.keywords)
        else:
            keywords.append(kw)
    return keywords


@pytest.fixture(scope='module')
def tree():
    return ast.parse((SIGNALTRACKERS_DIR / 'dashboard.py').read_text())


class TestDashboardCallSites:
    def test_no_create_call_passes_a_string_model(self, tree):
        """model= must be a name resolved from config, never a literal."""
        literals = [
            kw.value.value
            for call in _create_calls(tree)
            for kw in _effective_keywords(call)
            if kw.arg == 'model' and isinstance(kw.value, ast.Constant)
        ]
        assert literals == [], f"hardcoded model IDs at dashboard call sites: {literals}"

    def test_every_create_call_passes_a_model(self, tree):
        """A create() with no model= would silently take the SDK default."""
        missing = [
            call.lineno
            for call in _create_calls(tree)
            if not any(kw.arg == 'model' for kw in _effective_keywords(call))
        ]
        assert missing == [], f"create() without model= at lines {missing}"

    def test_model_sourced_from_the_getters(self, tree):
        """`model` must be assigned from ai_service, not built locally."""
        sources = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == 'model' for t in node.targets
            ):
                continue
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                sources.add(node.value.func.id)
            else:
                sources.add(ast.dump(node.value))
        assert sources == {'get_system_ai_model', 'get_system_chatbot_model'}, (
            f"unexpected sources for `model` in dashboard.py: {sources}"
        )


# ---------------------------------------------------------------------------
# Config -> usage metering coupling introduced by this story
# ---------------------------------------------------------------------------

class TestPricingCoupling:
    """The model string is now operator-settable and flows into cost lookup.

    Pinned as behaviour, not as prices — the Opus figures are being corrected
    under #484 and these assertions must survive that fix.
    """

    @contextmanager
    def _metering(self):
        with patch.dict(sys.modules):
            _evict('config', 'services')
            yield load_module(
                'usage_metering_under_test', 'services/usage_metering.py'
            )

    def test_both_default_models_have_their_own_pricing(self):
        with self._metering() as um:
            for model in (OPUS, SONNET):
                assert model in um.MODEL_PRICING, (
                    f"{model} is a shipped default; losing its pricing key "
                    f"drops metering to _DEFAULT_PRICING with only a log warning"
                )
            assert um.MODEL_PRICING[OPUS] is not um._DEFAULT_PRICING

    def test_unknown_configured_model_falls_back_silently(self):
        """Documents the hazard: a typo'd ANTHROPIC_MODEL mis-prices, not fails.

        Verified, not desired. If a guard is added later, update this test —
        do not delete the case.
        """
        with self._metering() as um:
            assert um._get_pricing('not-a-real-model') is um._DEFAULT_PRICING

    def test_config_default_resolves_real_pricing(self):
        """End to end: the value config.py ships must price off its own row."""
        with self._metering() as um:
            cfg = load_module('config_for_pricing', 'config.py')
            assert um._get_pricing(cfg.Config.ANTHROPIC_MODEL) is um.MODEL_PRICING[OPUS]
            assert (
                um._get_pricing(cfg.Config.ANTHROPIC_CHATBOT_MODEL)
                is um.MODEL_PRICING[SONNET]
            )


# ---------------------------------------------------------------------------
# The reload gotcha, pinned so #483 does not trip over it
# ---------------------------------------------------------------------------

class TestEnvOverrideRequiresFreshConfig:
    def test_override_ignored_while_config_is_cached(self):
        """Reloading ai_summary alone silently ignores the env override.

        Not a product defect — a fresh container process reads the env var
        correctly. Pinned because a test that reloads only ai_summary will
        pass against the old default and prove nothing.
        """
        with patch.dict(sys.modules):
            _evict('config', 'services')
            sys.modules['config'] = load_module('config', 'config.py')
            with patch.dict(os.environ, {'ANTHROPIC_MODEL': 'sentinel'}, clear=False):
                mod = load_module('ai_summary_stale', 'ai_summary.py')
            assert mod.ANTHROPIC_MODEL == OPUS

    def test_override_applies_once_config_is_reloaded(self):
        with fresh_process({'ANTHROPIC_MODEL': 'sentinel'}):
            mod = load_module('ai_summary_fresh', 'ai_summary.py')
            assert mod.ANTHROPIC_MODEL == 'sentinel'

    def test_news_pipeline_follows_the_same_chain(self):
        with fresh_process({'ANTHROPIC_MODEL': 'sentinel'}):
            mod = load_module('news_pipeline_fresh', 'news_pipeline.py')
            assert mod.ANTHROPIC_MODEL == 'sentinel'
