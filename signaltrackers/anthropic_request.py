"""
Anthropic request shape helpers.

One place for the parts of the Messages API request that are the same at every
Anthropic call site, so a future API change lands in a single file instead of
three. Deliberately depends only on `config` and the standard library — this
module is imported by `news_pipeline.py` and `ai_summary.py`, which run from
scheduled jobs with no Flask app context.

Why these particular parameters:

* **Thinking is always on** on the current models. ``{"type": "adaptive"}`` is
  the only accepted on-mode; ``{"type": "enabled", "budget_tokens": N}`` and
  ``{"type": "disabled"}`` both return 400. ``display`` is left at its default
  (``"omitted"``) because we never surface reasoning to the reader, and
  ``"summarized"`` would add billed output we would just throw away.
* **Depth is set by** ``output_config.effort`` — nested, never a top-level
  request field.
* Because thinking shares ``max_tokens`` with the visible answer, ``max_tokens``
  is now a *combined* budget. Short-output call sites must leave headroom or
  they stop at ``max_tokens`` having produced only reasoning.
* **Server-side refusal fallback** is on by default: a safety-classifier
  decline is re-run on a fallback model inside the same call instead of losing
  the briefing. The beta header and the parameter form are coupled — the
  ``-2026-07-01`` header pairs with the scalar ``fallbacks="default"``; pairing
  it with the older array form is itself a 400. Requires
  ``client.beta.messages``, not ``client.messages``.
"""

import logging

from config import ANTHROPIC_EFFORT

logger = logging.getLogger(__name__)

# Paired with fallbacks="default" (scalar). The array form
# fallbacks=[{"model": ...}] belongs to the older -2026-06-01 header.
SERVER_SIDE_FALLBACK_BETA = 'server-side-fallback-2026-07-01'


def build_request_params(model, max_tokens, system, messages,
                         tools=None, effort=None):
    """Build the kwargs for a `client.beta.messages.create()` call.

    `effort` defaults to the validated ANTHROPIC_EFFORT from config. Pass it
    explicitly on short-output paths, where the configured level would spend
    most of `max_tokens` on reasoning before any visible text.
    """
    params = {
        'model': model,
        'max_tokens': max_tokens,
        'system': system,
        'messages': messages,
        'thinking': {'type': 'adaptive'},
        'output_config': {'effort': effort or ANTHROPIC_EFFORT},
        'betas': [SERVER_SIDE_FALLBACK_BETA],
        'fallbacks': 'default',
    }
    if tools:
        params['tools'] = tools
    return params


def refusal_reason(response):
    """Return a human-readable refusal reason, or None if not a refusal.

    `stop_details` is populated *only* when `stop_reason == "refusal"` and is
    None for every other stop reason, so it must never be read unguarded.
    Callers must check this before reading `response.content` — a refusal is
    not a briefing and must never be rendered as one.
    """
    if getattr(response, 'stop_reason', None) != 'refusal':
        return None
    details = getattr(response, 'stop_details', None)
    category = getattr(details, 'category', None)
    explanation = getattr(details, 'explanation', None)
    return f"category={category!r} explanation={explanation!r}"


def extract_text(response):
    """Join the visible text blocks of a response.

    Never index `content[0]` — with thinking always on, responses lead with a
    `thinking` block, which has no `.text` attribute. Under the default
    `display: "omitted"` its text is empty, so it is invisible in a hand-built
    mock and only fails against the real API.
    """
    text_blocks = [b for b in response.content if getattr(b, 'type', None) == 'text']
    if not text_blocks:
        return None
    joined = ' '.join(b.text for b in text_blocks).strip()
    return joined or None


def warn_if_truncated(response, log_prefix, max_tokens):
    """Log when a response stopped at `max_tokens`.

    Thinking is billed against the same budget as the answer, so a limit sized
    for a no-thinking model can return zero text blocks with no other signal.
    """
    if getattr(response, 'stop_reason', None) == 'max_tokens':
        logger.warning(
            '%s Response hit max_tokens (%s) — reasoning may have consumed the '
            'budget before any visible text. Raise max_tokens or lower effort.',
            log_prefix, max_tokens,
        )
        return True
    return False


def served_model(response, requested_model):
    """The model that actually produced the response.

    A server-side fallback can serve the turn on a different model than the one
    requested; `response.model` reports which. Costing the turn at the
    requested model's rates would attribute spend to a model that never ran.
    """
    return getattr(response, 'model', None) or requested_model
