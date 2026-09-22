"""
Tests for US-16.1.1: Move Anthropic model IDs into environment configuration.

This is a pure refactor — the bar is that nothing observable changes. These
tests pin the defaults, prove the env vars reach the API call sites, and check
that no second source of truth for a model ID was introduced.
"""

import ast
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

REPO_ROOT = Path(__file__).parent.parent
SIGNALTRACKERS_DIR = REPO_ROOT / 'signaltrackers'

sys.path.insert(0, str(SIGNALTRACKERS_DIR))

OPUS = 'claude-opus-4-6'
SONNET = 'claude-sonnet-4-6'


def load_module(name, relative_path, env=None):
    """Load a module straight from its file path.

    Other test modules in this suite leave a MagicMock in sys.modules['services'],
    which breaks a plain `from services.ai_service import ...` depending on test
    order. Loading by path sidesteps that, and re-executes module-level
    os.environ reads so env overrides take effect.
    """
    with patch.dict(os.environ, env or {}, clear=False):
        spec = importlib.util.spec_from_file_location(
            name, SIGNALTRACKERS_DIR / relative_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def load_config(env=None):
    """Import config.py fresh so module-level os.environ reads re-evaluate."""
    with patch.dict(os.environ, env or {}, clear=False):
        spec = importlib.util.spec_from_file_location(
            'config_under_test', SIGNALTRACKERS_DIR / 'config.py'
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Defaults — behaviour must be byte-identical without the new vars
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_briefing_model_default_unchanged(self):
        cfg = load_config()
        assert cfg.Config.ANTHROPIC_MODEL == OPUS

    def test_chatbot_model_default_unchanged(self):
        cfg = load_config()
        assert cfg.Config.ANTHROPIC_CHATBOT_MODEL == SONNET

    def test_module_level_aliases_match_config_class(self):
        """Non-Flask modules import these; they must not drift from Config."""
        cfg = load_config()
        assert cfg.ANTHROPIC_MODEL == cfg.Config.ANTHROPIC_MODEL
        assert cfg.ANTHROPIC_CHATBOT_MODEL == cfg.Config.ANTHROPIC_CHATBOT_MODEL

    def test_env_overrides_briefing_model(self):
        cfg = load_config({'ANTHROPIC_MODEL': 'sentinel-briefing'})
        assert cfg.Config.ANTHROPIC_MODEL == 'sentinel-briefing'
        assert cfg.ANTHROPIC_MODEL == 'sentinel-briefing'

    def test_env_overrides_chatbot_model(self):
        cfg = load_config({'ANTHROPIC_CHATBOT_MODEL': 'sentinel-chatbot'})
        assert cfg.Config.ANTHROPIC_CHATBOT_MODEL == 'sentinel-chatbot'

    def test_overrides_are_independent(self):
        """Setting the briefing model must not leak into the chatbot model."""
        cfg = load_config({'ANTHROPIC_MODEL': 'sentinel-briefing'})
        assert cfg.Config.ANTHROPIC_CHATBOT_MODEL == SONNET


# ---------------------------------------------------------------------------
# ai_service getters read from current_app.config
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    application = Flask(__name__)
    application.config['TESTING'] = True
    application.config['ANTHROPIC_MODEL'] = OPUS
    application.config['ANTHROPIC_CHATBOT_MODEL'] = SONNET
    return application


@pytest.fixture
def ai_service():
    return load_module('ai_service_under_test', 'services/ai_service.py')


class TestAIServiceGetters:
    def test_briefing_model_from_config(self, app, ai_service):
        app.config['SYSTEM_AI_PROVIDER'] = 'anthropic'
        with app.app_context():
            assert ai_service.get_system_ai_model() == OPUS

    def test_chatbot_model_from_config(self, app, ai_service):
        app.config['SYSTEM_AI_PROVIDER'] = 'anthropic'
        with app.app_context():
            assert ai_service.get_system_chatbot_model() == SONNET

    def test_getters_follow_config_override(self, app, ai_service):
        app.config['SYSTEM_AI_PROVIDER'] = 'anthropic'
        app.config['ANTHROPIC_MODEL'] = 'sentinel-briefing'
        app.config['ANTHROPIC_CHATBOT_MODEL'] = 'sentinel-chatbot'
        with app.app_context():
            assert ai_service.get_system_ai_model() == 'sentinel-briefing'
            assert ai_service.get_system_chatbot_model() == 'sentinel-chatbot'

    def test_openai_branch_untouched(self, app, ai_service):
        """The OpenAI path must not be affected by this refactor."""
        app.config['SYSTEM_AI_PROVIDER'] = 'openai'
        with app.app_context():
            assert ai_service.get_system_ai_model() == 'gpt-5.2'
            assert ai_service.get_system_chatbot_model() == 'gpt-5.2'

    def test_module_constants_removed(self, ai_service):
        """The hardcoded constants must be gone, not merely unused."""
        assert not hasattr(ai_service, 'ANTHROPIC_MODEL')
        assert not hasattr(ai_service, 'ANTHROPIC_CHATBOT_MODEL')
        assert ai_service.OPENAI_MODEL == 'gpt-5.2'


# ---------------------------------------------------------------------------
# news_pipeline — runs outside any Flask app context
# ---------------------------------------------------------------------------

class TestNewsPipeline:
    def test_imports_outside_app_context(self):
        """No RuntimeError: Working outside of application context."""
        mod = load_module('news_pipeline_under_test', 'news_pipeline.py')
        assert mod.ANTHROPIC_MODEL == OPUS

    def test_configured_model_reaches_messages_create(self):
        news_pipeline = load_module(
            'news_pipeline_under_test', 'news_pipeline.py'
        )

        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(
            content=[MagicMock(text='summary')]
        )
        fake_lib = MagicMock()
        fake_lib.Anthropic.return_value = fake_client

        with patch.dict(sys.modules, {'anthropic': fake_lib}), \
                patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'}), \
                patch.object(news_pipeline, 'ANTHROPIC_MODEL', 'sentinel-briefing'):
            news_pipeline._summarize_with_anthropic('system', 'user')

        kwargs = fake_client.messages.create.call_args.kwargs
        assert kwargs['model'] == 'sentinel-briefing'

    def test_no_model_literal_in_source(self):
        source = (SIGNALTRACKERS_DIR / 'news_pipeline.py').read_text()
        assert 'claude-' not in source


# ---------------------------------------------------------------------------
# ai_summary — module-level constant now sourced from config.py
# ---------------------------------------------------------------------------

class TestAISummary:
    def test_no_local_model_literal(self):
        """ai_summary must not define its own default."""
        source = (SIGNALTRACKERS_DIR / 'ai_summary.py').read_text()
        assert 'claude-' not in source

    def test_imports_model_from_config(self):
        tree = ast.parse((SIGNALTRACKERS_DIR / 'ai_summary.py').read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == 'config'
            for alias in node.names
        ]
        assert 'ANTHROPIC_MODEL' in imported

    def test_model_resolves_to_default(self):
        import ai_summary
        assert ai_summary.ANTHROPIC_MODEL == OPUS


# ---------------------------------------------------------------------------
# Single source of truth
# ---------------------------------------------------------------------------

class TestNoStraySourcesOfTruth:
    # usage_metering.MODEL_PRICING is keyed *by* model name — those literals are
    # pricing data looked up against whatever model ran, not model selectors.
    ALLOWED = {'config.py', 'services/usage_metering.py'}

    def test_no_model_literal_outside_allowed_files(self):
        offenders = []
        for path in SIGNALTRACKERS_DIR.rglob('*.py'):
            rel = path.relative_to(SIGNALTRACKERS_DIR).as_posix()
            if rel in self.ALLOWED:
                continue
            if 'claude-' in path.read_text():
                offenders.append(rel)
        assert offenders == [], f"model ID literals outside config.py: {offenders}"

    def test_pricing_table_still_resolves_both_models(self):
        """Deleting the pricing keys would silently drop metering to defaults.

        Checked statically: importing usage_metering pulls in the db session.
        """
        source = (SIGNALTRACKERS_DIR / 'services/usage_metering.py').read_text()
        tree = ast.parse(source)
        pricing_keys = [
            key.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == 'MODEL_PRICING'
                for t in node.targets
            )
            for key in node.value.keys
            if isinstance(key, ast.Constant)
        ]
        assert OPUS in pricing_keys
        assert SONNET in pricing_keys


# ---------------------------------------------------------------------------
# Container plumbing
# ---------------------------------------------------------------------------

class TestPlumbing:
    @pytest.mark.parametrize('compose_file', [
        'docker-compose.yml', 'docker-compose.prod.yml'
    ])
    def test_compose_passes_both_vars(self, compose_file):
        content = (REPO_ROOT / compose_file).read_text()
        assert f'ANTHROPIC_MODEL=${{ANTHROPIC_MODEL:-{OPUS}}}' in content
        assert (
            f'ANTHROPIC_CHATBOT_MODEL=${{ANTHROPIC_CHATBOT_MODEL:-{SONNET}}}'
            in content
        )

    def test_env_example_documents_both_vars(self):
        content = (REPO_ROOT / '.env.example').read_text()
        assert 'ANTHROPIC_MODEL=' in content
        assert 'ANTHROPIC_CHATBOT_MODEL=' in content
