"""
Usage Metering Service

Records AI interaction usage data (tokens, cost) for registered users.
Metering is silent and failure-safe — errors are logged but never break AI features.
"""

import logging
from decimal import Decimal

from extensions import db
from models.ai_usage import AIUsageRecord

logger = logging.getLogger(__name__)

# Token pricing per million tokens (USD)
# Source: provider pricing pages, updated as needed.
#
# Every value here is a *published* per-model figure and must be copied
# literally from the provider's pricing page. In particular, do not derive
# 'cache_read' as a percentage of 'input' -- the ratio is not a rule and
# varies per model, so a derived value silently misprices the row.
MODEL_PRICING = {
    # Anthropic models
    'claude-opus-4-6': {
        'input': Decimal('5.00'),
        'output': Decimal('25.00'),
        'cache_read': Decimal('0.50'),
        'cache_creation': Decimal('6.25'),
    },
    'claude-sonnet-4-6': {
        'input': Decimal('3.00'),
        'output': Decimal('15.00'),
        'cache_read': Decimal('0.30'),
        'cache_creation': Decimal('3.75'),
    },
    # OpenAI models
    'gpt-5.2': {
        'input': Decimal('2.50'),
        'output': Decimal('10.00'),
        'cache_read': Decimal('1.25'),
        'cache_creation': Decimal('0'),
    },
}

# Fallback pricing if model not found
_DEFAULT_PRICING = {
    'input': Decimal('3.00'),
    'output': Decimal('15.00'),
    'cache_read': Decimal('0.30'),
    'cache_creation': Decimal('3.75'),
}

_PER_MILLION = Decimal('1000000')


def _get_pricing(model_name):
    """Get pricing for a model, falling back to default if unknown."""
    # Try exact match first, then prefix match
    if model_name in MODEL_PRICING:
        return MODEL_PRICING[model_name]
    for key in MODEL_PRICING:
        if model_name.startswith(key):
            return MODEL_PRICING[key]
    logger.warning(f"Unknown model for pricing: {model_name}, using defaults")
    return _DEFAULT_PRICING


def calculate_cost(model_name, input_tokens, output_tokens,
                   cache_read_tokens=None, cache_creation_tokens=None):
    """Calculate estimated cost in USD for the given token counts."""
    pricing = _get_pricing(model_name)
    cost = Decimal('0')
    if input_tokens:
        cost += Decimal(str(input_tokens)) * pricing['input'] / _PER_MILLION
    if output_tokens:
        cost += Decimal(str(output_tokens)) * pricing['output'] / _PER_MILLION
    if cache_read_tokens:
        cost += Decimal(str(cache_read_tokens)) * pricing['cache_read'] / _PER_MILLION
    if cache_creation_tokens:
        cost += Decimal(str(cache_creation_tokens)) * pricing['cache_creation'] / _PER_MILLION
    return cost


def extract_usage_anthropic(response):
    """Extract token usage from an Anthropic API response."""
    usage = getattr(response, 'usage', None)
    if usage is None:
        return {}
    return {
        'input_tokens': getattr(usage, 'input_tokens', None),
        'output_tokens': getattr(usage, 'output_tokens', None),
        'cache_read_tokens': getattr(usage, 'cache_read_input_tokens', None) or None,
        'cache_creation_tokens': getattr(usage, 'cache_creation_input_tokens', None) or None,
    }


def extract_usage_openai(response):
    """Extract token usage from an OpenAI API response."""
    usage = getattr(response, 'usage', None)
    if usage is None:
        return {}
    return {
        'input_tokens': getattr(usage, 'prompt_tokens', None),
        'output_tokens': getattr(usage, 'completion_tokens', None),
        'cache_read_tokens': None,
        'cache_creation_tokens': None,
    }


def extract_usage(response, provider):
    """Extract token usage from an AI provider response."""
    if provider == 'anthropic':
        return extract_usage_anthropic(response)
    return extract_usage_openai(response)


def accumulate_usage(total, new_usage):
    """Accumulate token counts across multiple API calls (agentic loops)."""
    for key in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens'):
        new_val = new_usage.get(key)
        if new_val is not None:
            total[key] = (total.get(key) or 0) + new_val
    return total


def record_usage(user_id, interaction_type, model_name,
                 input_tokens=None, output_tokens=None,
                 cache_read_tokens=None, cache_creation_tokens=None):
    """Record an AI usage metering entry. Fails silently — never breaks AI features.

    Args:
        user_id: The authenticated user's ID (required, non-null).
        interaction_type: One of AIUsageRecord.VALID_INTERACTION_TYPES.
        model_name: The AI model used (e.g., 'claude-sonnet-4-6').
        input_tokens: Number of input/prompt tokens (nullable).
        output_tokens: Number of output/completion tokens (nullable).
        cache_read_tokens: Anthropic cache read tokens (nullable).
        cache_creation_tokens: Anthropic cache creation tokens (nullable).
    """
    try:
        estimated_cost = calculate_cost(
            model_name, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens
        )

        record = AIUsageRecord(
            user_id=user_id,
            interaction_type=interaction_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            model=model_name,
            estimated_cost=estimated_cost,
        )
        db.session.add(record)
        db.session.commit()
        logger.info(
            f"Metered {interaction_type} for user {user_id}: "
            f"in={input_tokens} out={output_tokens} cost=${estimated_cost:.8f}"
        )
    except Exception:
        logger.exception(f"Failed to record usage metering for user {user_id}")
        try:
            db.session.rollback()
        except Exception:
            pass
