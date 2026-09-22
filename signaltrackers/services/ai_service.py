"""
AI Service Layer

Handles AI client management using the system API key for all features.
All AI calls (chatbot, portfolio analysis, scheduled briefings) route through
the system client — there is no per-user API key path.
"""

import os
from typing import Tuple, Optional, Any

from flask import current_app


class AIServiceError(Exception):
    """Custom exception for AI service errors."""
    pass


# AI Provider constants
OPENAI_MODEL = "gpt-5.2"

# Anthropic model IDs are not constants here — they come from app config
# (config.py), so they can be overridden per-environment without a rebuild.


def get_system_ai_client() -> Tuple[Optional[Any], Optional[str]]:
    """
    Get AI client using system API key from environment/config.

    Returns:
        tuple: (client, provider_name) or (None, error_message)
    """
    provider = current_app.config.get('SYSTEM_AI_PROVIDER', 'openai').lower()

    if provider == 'anthropic':
        key = current_app.config.get('SYSTEM_ANTHROPIC_KEY')
        if not key:
            return None, "System Anthropic key not configured"

        try:
            import anthropic
            return anthropic.Anthropic(api_key=key), 'anthropic'
        except ImportError:
            return None, "Anthropic package not installed"

    else:  # OpenAI (default)
        key = current_app.config.get('SYSTEM_OPENAI_KEY')
        if not key:
            return None, "System OpenAI key not configured"

        try:
            from openai import OpenAI
            return OpenAI(api_key=key), 'openai'
        except ImportError:
            return None, "OpenAI package not installed"


def get_system_ai_model() -> str:
    """
    Get the appropriate model name for the system's configured provider.

    Returns:
        str: Model name
    """
    provider = current_app.config.get('SYSTEM_AI_PROVIDER', 'openai').lower()
    if provider == 'anthropic':
        return current_app.config['ANTHROPIC_MODEL']
    return OPENAI_MODEL


def get_system_chatbot_model() -> str:
    """
    Get the chatbot-specific model for the system's configured provider.

    The chatbot uses a cheaper model than briefings for cost efficiency with
    large context, while briefings use the top tier for analytical depth.
    Both are set by ANTHROPIC_CHATBOT_MODEL / ANTHROPIC_MODEL.

    Returns:
        str: Model name
    """
    provider = current_app.config.get('SYSTEM_AI_PROVIDER', 'openai').lower()
    if provider == 'anthropic':
        return current_app.config['ANTHROPIC_CHATBOT_MODEL']
    return OPENAI_MODEL
