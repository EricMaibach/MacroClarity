#!/usr/bin/env python3
"""
AI Week-in-Review Generator

Generates the weekly market reviews (the general review and five section
reviews) using AI with web search capability. Each review is written in the
week-in-review content contract (see weekly_review.py) and stored as raw
markdown plus its parsed parts. Supports both OpenAI and Anthropic APIs,
configurable via environment variables. Reviews are stored historically and
provide context for continuity.
"""

import os
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import pytz

# AI Provider imports - both optional
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    OpenAI = None

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    anthropic = None

import briefing_week
import weekly_review

from web_search import (
    search_web, search_financial_news, is_tavily_configured,
    SEARCH_FUNCTION_DEFINITION, execute_search_function
)

try:
    from market_conditions import get_market_conditions, get_conditions_history
except ImportError:
    def get_market_conditions():
        return None

    def get_conditions_history():
        return {}

# =============================================================================
# AI Provider Configuration
# =============================================================================
# Set AI_PROVIDER environment variable to "openai" or "anthropic"
# Default: "openai" for backward compatibility
#
# Environment variables:
#   AI_PROVIDER: "openai" or "anthropic"
#   OPENAI_API_KEY: Required if using OpenAI
#   ANTHROPIC_API_KEY: Required if using Anthropic
#
# Models used:
#   OpenAI: gpt-5.2
#   Anthropic: set by ANTHROPIC_MODEL (see config.py for the default)
# =============================================================================

AI_PROVIDER = os.environ.get('AI_PROVIDER', 'openai').lower()
OPENAI_MODEL = "gpt-5.2"

# This module runs outside a Flask app context (scheduled briefing jobs import
# it directly), so it cannot read current_app.config. It imports the resolved
# value from config.py instead of redefining the default, keeping config.py the
# single source of truth for model IDs.
from config import ANTHROPIC_MODEL  # noqa: E402
from anthropic_request import (  # noqa: E402
    build_request_params,
    extract_text,
    refusal_reason,
    served_model,
    warn_if_truncated,
)

# The request shape (thinking / output_config.effort / refusal fallbacks) lives
# in anthropic_request.py, which reads the validated ANTHROPIC_EFFORT from
# config. ANTHROPIC_EFFORT is an effort level (low/medium/high/xhigh/max), not
# a token budget; an invalid value falls back rather than reaching the API,
# which would 400.


def get_ai_provider():
    """Get the configured AI provider."""
    return AI_PROVIDER


def get_ai_client():
    """
    Get an AI client based on the configured provider.

    Returns:
        tuple: (client, provider_name) or (None, error_message)
    """
    provider = get_ai_provider()

    if provider == 'anthropic':
        if not ANTHROPIC_AVAILABLE:
            return None, "Anthropic package not installed. Run: pip install anthropic"
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            return None, "ANTHROPIC_API_KEY environment variable not set"
        return anthropic.Anthropic(api_key=api_key), 'anthropic'

    else:  # Default to OpenAI
        if not OPENAI_AVAILABLE:
            return None, "OpenAI package not installed. Run: pip install openai"
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            return None, "OPENAI_API_KEY environment variable not set"
        return OpenAI(api_key=api_key), 'openai'


def is_ai_configured():
    """Check if AI is properly configured."""
    client, result = get_ai_client()
    return client is not None

# Storage location
DATA_DIR = Path("data")
SUMMARIES_FILE = DATA_DIR / "ai_summaries.json"


def _dump_prompt_to_file(log_prefix, provider, system_prompt, user_prompt, max_tokens):
    """DEBUG: Save the full prompt sent to the AI to a text file for review."""
    dump_dir = Path("data/prompt_dumps")
    dump_dir.mkdir(parents=True, exist_ok=True)
    # Build a filename from the log_prefix, e.g. "[Crypto Summary]" -> "crypto_summary"
    tag = log_prefix.strip("[] ").lower().replace(" ", "_")
    filename = dump_dir / f"{tag}.txt"
    with open(filename, "w") as f:
        f.write(f"=== PROMPT DUMP: {log_prefix} ===\n")
        f.write(f"Timestamp: {datetime.now().isoformat()}\n")
        f.write(f"Provider: {provider}\n")
        f.write(f"Max tokens: {max_tokens}\n")
        f.write(f"\n{'='*60}\n")
        f.write(f"SYSTEM PROMPT\n{'='*60}\n")
        f.write(system_prompt)
        f.write(f"\n\n{'='*60}\n")
        f.write(f"USER PROMPT\n{'='*60}\n")
        f.write(user_prompt)
        f.write("\n")
    print(f"{log_prefix} Prompt dumped to {filename}")


def call_ai_with_tools(client, system_prompt, user_prompt, max_tokens=16000, log_prefix="[AI]", provider=None,
                       effort=None):
    """
    Make an AI API call with web search tool support.
    Supports both OpenAI and Anthropic APIs.

    Args:
        client: AI client instance (OpenAI or Anthropic)
        system_prompt: System prompt for the model
        user_prompt: User prompt with data/context
        max_tokens: Ceiling for the whole completion. Thinking is always on
            and is generated against this same ceiling, so this is a combined
            reasoning + visible-output budget, not an output length. Sizing it
            from the visible answer alone truncates the answer with reasoning
            that has already been generated and billed. Raising the ceiling
            does not raise cost — you pay for tokens produced, not for the cap.
            Spend is controlled by ANTHROPIC_EFFORT.
        log_prefix: Prefix for log messages (e.g., "[Crypto Summary]")
        provider: 'openai' or 'anthropic' (auto-detected if None)
        effort: Anthropic `output_config.effort` override; None uses
            ANTHROPIC_EFFORT. Ignored by OpenAI.

    Returns:
        dict with 'success', 'content', 'error', 'usage', 'model' and
        'truncated' keys. 'truncated' is True when the final response stopped
        at max_tokens, so the content may be cut off mid-part.
    """
    # Auto-detect provider if not specified
    if provider is None:
        provider = get_ai_provider()

    print(f"{log_prefix} Using provider: {provider}")

    # DEBUG: dump full prompt to file for review
    _dump_prompt_to_file(log_prefix, provider, system_prompt, user_prompt, max_tokens)

    if provider == 'anthropic':
        result = _call_anthropic_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix, effort)
    else:
        result = _call_openai_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix)

    # Scheduled weekly-run calls are metered here, once per call (tool-loop
    # turns are already summed into result['usage']). User-triggered calls are
    # metered by their routes; outside the scheduled run this is a no-op.
    briefing_week.meter_ai_call(result.get('usage'), result.get('model'))
    return result


def _call_openai_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix):
    """OpenAI-specific implementation of AI call with tools."""
    from services.usage_metering import extract_usage_openai, accumulate_usage

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    # Set up tools if Tavily is available
    tools = None
    tool_choice = None
    web_search_available = is_tavily_configured()

    if web_search_available:
        tools = [{"type": "function", "function": SEARCH_FUNCTION_DEFINITION}]
        tool_choice = "auto"
        print(f"{log_prefix} Web search tool available")
    else:
        print(f"{log_prefix} Web search tool not available (Tavily not configured)")

    # Tool calling loop
    # Allow up to 6 web search iterations for comprehensive market analysis
    # Complex market days often require 4-5 searches for context (breaking news,
    # economic data, Fed policy, sector developments, geopolitical events)
    max_iterations = 6
    iteration = 0
    total_usage = {}

    while iteration < max_iterations:
        iteration += 1
        print(f"{log_prefix} API call iteration {iteration}/{max_iterations}")

        api_params = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_completion_tokens": max_tokens
        }

        if tools:
            api_params["tools"] = tools
            # Last turn: no more searches, so the loop always ends with an answer
            api_params["tool_choice"] = "none" if iteration == max_iterations else tool_choice

        response = client.chat.completions.create(**api_params)
        total_usage = accumulate_usage(total_usage, extract_usage_openai(response))
        response_message = response.choices[0].message

        print(f"{log_prefix} Response finish_reason: {response.choices[0].finish_reason}")
        print(f"{log_prefix} Response has tool_calls: {bool(response_message.tool_calls)}")

        if response_message.tool_calls:
            print(f"{log_prefix} Tool calls requested: {[tc.function.name for tc in response_message.tool_calls]}")
            messages.append(response_message)

            for tool_call in response_message.tool_calls:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                print(f"{log_prefix} Executing {function_name} with args: {function_args}")

                if function_name == "search_web":
                    result = execute_search_function(function_args)
                else:
                    result = json.dumps({"error": f"Unknown function: {function_name}"})

                print(f"{log_prefix} {function_name} returned {len(result)} chars")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result
                })

            # Continue loop to get model's response after tool results
            continue

        # No tool calls - this is the final response
        content = response_message.content
        print(f"{log_prefix} Response content length: {len(content) if content else 0}")

        if content:
            print(f"{log_prefix} Response content preview: {content[:200]}...")

        if not content or not content.strip():
            print(f"{log_prefix} API returned empty content")
            return {
                'success': False,
                'content': None,
                'error': 'API returned empty response',
                'usage': total_usage,
                'model': OPENAI_MODEL,
            }

        return {
            'success': True,
            'content': content.strip(),
            'error': None,
            'usage': total_usage,
            'model': OPENAI_MODEL,
            'truncated': response.choices[0].finish_reason == 'length',
        }

    # Exceeded max iterations
    print(f"{log_prefix} Exceeded max iterations")
    return {
        'success': False,
        'content': None,
        'error': 'Exceeded maximum tool call iterations',
        'usage': total_usage,
        'model': OPENAI_MODEL,
    }


def _call_anthropic_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix, effort=None):
    """Anthropic-specific implementation of AI call with tools."""
    from services.usage_metering import extract_usage_anthropic, accumulate_usage

    messages = [
        {"role": "user", "content": user_prompt}
    ]

    # Set up tools if Tavily is available
    tools = None
    web_search_available = is_tavily_configured()

    if web_search_available:
        # Anthropic tool format
        tools = [{
            "name": SEARCH_FUNCTION_DEFINITION["name"],
            "description": SEARCH_FUNCTION_DEFINITION["description"],
            "input_schema": SEARCH_FUNCTION_DEFINITION["parameters"]
        }]
        print(f"{log_prefix} Web search tool available")
    else:
        print(f"{log_prefix} Web search tool not available (Tavily not configured)")

    # Tool calling loop
    # Allow up to 6 web search iterations for comprehensive market analysis
    # Complex market days often require 4-5 searches for context (breaking news,
    # economic data, Fed policy, sector developments, geopolitical events)
    max_iterations = 6
    iteration = 0
    total_usage = {}
    # A server-side fallback can serve the turn on a different model than we
    # asked for. Cost is attributed to whatever actually ran, not to the
    # requested model. Fallback routing is sticky for the rest of the turn, so
    # the last response's model is the one that produced the answer.
    cost_model = ANTHROPIC_MODEL

    while iteration < max_iterations:
        iteration += 1
        print(f"{log_prefix} API call iteration {iteration}/{max_iterations}")

        api_params = build_request_params(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=messages,
            tools=tools,
            effort=effort,
            # Last turn: no more searches, so the loop always ends with an answer.
            # The weekly review's calendar lookups made 6 search turns reachable.
            tool_choice={"type": "none"} if iteration == max_iterations else None,
            # Each turn re-sends every earlier search result; cache the prefix.
            # A real weekly run without this cost ~$1 per call, nearly all of
            # it re-sent input.
            cache=True,
        )

        # No try/except here on purpose. This call used to catch every
        # exception, drop `thinking`, and retry — which turned a malformed
        # request into a silently thinking-free briefing with no signal beyond
        # one print. A 400 must fail loudly.
        response = client.beta.messages.create(**api_params)

        cost_model = served_model(response, ANTHROPIC_MODEL)
        total_usage = accumulate_usage(total_usage, extract_usage_anthropic(response))
        print(f"{log_prefix} Response stop_reason: {response.stop_reason}, "
              f"served by: {cost_model}")

        # A refusal must be checked before content is read, and must never be
        # returned to the reader as a briefing. Reaching here means the whole
        # fallback chain declined.
        refusal = refusal_reason(response)
        if refusal:
            print(f"{log_prefix} Request refused: {refusal}")
            return {
                'success': False,
                'content': None,
                'error': f'Model refused the request ({refusal})',
                'usage': total_usage,
                'model': cost_model,
            }

        truncated = warn_if_truncated(response, log_prefix, max_tokens)

        # Check for tool use
        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]

        if tool_use_blocks:
            print(f"{log_prefix} Tool calls requested: {[tb.name for tb in tool_use_blocks]}")

            # Add assistant message with tool use
            messages.append({
                "role": "assistant",
                "content": response.content
            })

            # Process each tool call
            tool_results = []
            for tool_use in tool_use_blocks:
                function_name = tool_use.name
                function_args = tool_use.input if hasattr(tool_use, 'input') else {}
                print(f"{log_prefix} Executing {function_name} with args: {function_args}")

                if function_name == "search_web":
                    result = execute_search_function(function_args)
                else:
                    result = json.dumps({"error": f"Unknown function: {function_name}"})

                print(f"{log_prefix} {function_name} returned {len(result)} chars")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result
                })

            # Add tool results as user message
            messages.append({
                "role": "user",
                "content": tool_results
            })

            # Continue loop to get model's response after tool results
            continue

        # No tool calls - extract text response
        content = extract_text(response)

        print(f"{log_prefix} Response content length: {len(content) if content else 0}")

        if content:
            print(f"{log_prefix} Response content preview: {content[:200]}...")

        if not content:
            print(f"{log_prefix} API returned empty content")
            return {
                'success': False,
                'content': None,
                'error': 'API returned empty response',
                'usage': total_usage,
                'model': cost_model,
            }

        return {
            'success': True,
            'content': content,
            'error': None,
            'usage': total_usage,
            'model': cost_model,
            'truncated': truncated,
        }

    # Exceeded max iterations
    print(f"{log_prefix} Exceeded max iterations")
    return {
        'success': False,
        'content': None,
        'error': 'Exceeded maximum tool call iterations',
        'usage': total_usage,
        'model': cost_model,
    }


# Backward compatibility alias
def call_openai_with_tools(client, system_prompt, user_prompt, max_tokens=600, log_prefix="[AI]"):
    """
    Legacy wrapper - calls the unified AI function with OpenAI provider.
    Deprecated: Use call_ai_with_tools() instead.
    """
    return call_ai_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix, provider='openai')


def load_summaries():
    """Load all stored AI summaries."""
    if SUMMARIES_FILE.exists():
        try:
            with open(SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def _format_fields(structured):
    """Review-format fields stored on a briefing record (US-490.2).

    `structured` holds the parsed week-in-review parts, or None when parsing
    failed (or the text is a rule-based fallback). The raw markdown is always
    stored as `summary` either way, so the briefing can always be displayed.
    """
    return {
        'structured': structured,
        'format': 'structured' if structured else 'markdown',
    }


def _display_parts(record):
    """Structured parts for a display API. Pre-US-490.2 records return None."""
    structured = record.get('structured')
    return {
        'structured': structured if isinstance(structured, dict) else None,
        'format': 'structured' if isinstance(structured, dict) else 'markdown',
    }


def save_summaries(data):
    """Save summaries to JSON file."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_summary():
    """Get the most recent AI summary."""
    data = load_summaries()
    if data["summaries"]:
        # Sort by date descending and return most recent
        sorted_summaries = sorted(data["summaries"], key=lambda x: x["date"], reverse=True)
        return sorted_summaries[0]
    return None


def _get_recent_summaries(data, days):
    """Shared helper: get summaries from the last N calendar days.

    Uses a date-cutoff approach (entries within the last N calendar days)
    rather than a slice of the N most recent entries, ensuring consistent
    behavior across all briefing types.
    """
    if not data.get("summaries"):
        return []

    eastern = pytz.timezone('US/Eastern')
    cutoff = (datetime.now(eastern) - timedelta(days=days)).strftime('%Y-%m-%d')
    recent = [s for s in data["summaries"] if s["date"] >= cutoff]
    return sorted(recent, key=lambda x: x["date"])


def get_recent_summaries(days=3):
    """Get summaries from the last N days for context."""
    return _get_recent_summaries(load_summaries(), days)


def save_summary(date_str, summary_text, web_search_used=False, news_context=None, structured=None):
    """Save a new summary, overwriting if same date exists."""
    data = load_summaries()

    # Remove existing summary for this date if present
    data["summaries"] = [s for s in data["summaries"] if s["date"] != date_str]

    eastern = pytz.timezone('US/Eastern')
    # Add new summary
    data["summaries"].append({
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "web_search_used": web_search_used,
        "news_context": news_context[:500] if news_context else None,  # Store snippet of news used
        **briefing_week.record_metadata(),
        **_format_fields(structured),
    })

    # Keep last 90 days of summaries
    cutoff = (datetime.now(eastern) - timedelta(days=90)).strftime('%Y-%m-%d')
    data["summaries"] = [s for s in data["summaries"] if s["date"] >= cutoff]

    save_summaries(data)


def _get_stored_news_context(topic: str | None = None) -> str | None:
    """
    Read news context from the stored news pipeline data.

    If topic is None, returns the cross-market summary string (for the general review).
    If topic is given (e.g. 'crypto'), returns the pre-built AI topic summary.
    Returns None if no stored data is available or the topic has no content.
    """
    try:
        from news_pipeline import get_stored_news
        stored = get_stored_news()
    except Exception:
        return None

    if stored is None:
        return None

    if topic is None:
        return stored.get('summary')

    # Use pre-built AI topic summary if available
    topic_summaries = stored.get('topic_summaries', {})
    topic_summary = topic_summaries.get(topic)
    if topic_summary:
        topic_label = topic.replace('_', ' ').title()
        return f"## This Week's {topic_label} News\n{topic_summary}"

    # No pre-built summary available for this topic
    return None


def fetch_news_for_summary():
    """Fetch current financial news for summary context."""
    if not is_tavily_configured():
        return None

    # Search for multiple topics to get broad coverage
    news_parts = []

    # Main financial news
    main_news = search_financial_news("major financial market news today", max_results=5)
    if main_news.get('results'):
        news_parts.append("## Today's Major Financial News")
        if main_news.get('answer'):
            news_parts.append(main_news['answer'])
        for r in main_news['results'][:3]:
            news_parts.append(f"- {r['title']}: {r['content'][:200]}...")

    # Fed/monetary policy news
    fed_news = search_web("Federal Reserve monetary policy news today", max_results=3)
    if fed_news.get('results'):
        news_parts.append("\n## Fed/Monetary Policy")
        for r in fed_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    # Market-moving events
    events_news = search_web("stock market moving events economic data today", max_results=3)
    if events_news.get('results'):
        news_parts.append("\n## Market-Moving Events")
        for r in events_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    return "\n".join(news_parts) if news_parts else None


# =============================================================================
# Market Conditions Context for AI Briefing (US-325.1)
# =============================================================================

def _build_conditions_context(conditions):
    """Build a structured conditions context string for the AI prompt.

    Args:
        conditions: dict from get_market_conditions() with quadrant, dimensions, etc.

    Returns:
        Formatted string describing current market conditions across all 4 dimensions.
    """
    if not conditions:
        return ""

    dims = conditions.get('dimensions', {})
    quadrant = conditions.get('quadrant', 'Unknown')

    quad_dims = dims.get('quadrant', {})
    liq = dims.get('liquidity', {})
    risk = dims.get('risk', {})
    policy = dims.get('policy', {})

    parts = ["## CURRENT MARKET CONDITIONS"]
    parts.append(f"Quadrant: {quadrant}")

    # Transition watch (graduated stability filter)
    tw = quad_dims.get('transition_watch')
    if tw is not None:
        direction = tw.get('direction', 'Unknown')
        parts.append(f"  ⚠ Transition Watch — signals shifting toward {direction} (month {tw.get('month', 1)} of 2 for confirmation)")

    # Growth/inflation composites if available
    growth = quad_dims.get('growth_composite')
    inflation = quad_dims.get('inflation_composite')
    if growth is not None and inflation is not None:
        parts.append(f"  Growth composite: {growth:+.2f}, Inflation composite: {inflation:+.2f}")

    # Inflation breadth
    breadth = quad_dims.get('inflation_breadth')
    breadth_total = quad_dims.get('inflation_breadth_total')
    if breadth is not None and breadth_total is not None:
        parts.append(f"  Inflation breadth: {breadth}/{breadth_total} indicators agree on direction")

    # Inflation component breakdown
    components = quad_dims.get('inflation_components')
    if components:
        comp_lines = []
        _INDICATOR_LABELS = {
            'CPIAUCSL': 'CPI',
            'PCEPILFE': 'Core PCE',
            'MEDCPIM158SFRBCLE': 'Median CPI',
            'T10YIE': '10Y Breakeven',
            'T5YIFR': '5Y5Y Forward',
            'MICH': 'Michigan 1Y Expectations',
        }
        for key, comp in components.items():
            label = _INDICATOR_LABELS.get(key, key)
            if comp is None:
                comp_lines.append(f"    {label}: no data")
            else:
                direction = comp.get('direction', '?')
                z = comp.get('z_score')
                raw = comp.get('raw_value')
                line = f"    {label}: {direction}"
                if z is not None:
                    line += f" (z={z:+.2f})"
                if raw is not None:
                    line += f" [raw={raw}]"
                comp_lines.append(line)
        if comp_lines:
            parts.append("  Inflation components:")
            parts.extend(comp_lines)

    # Liquidity
    liq_state = liq.get('state', 'Unknown')
    liq_score = liq.get('score')
    liq_line = f"Liquidity: {liq_state}"
    if liq_score is not None:
        liq_line += f" (score: {liq_score:.2f})"
    parts.append(liq_line)

    # Risk
    risk_state = risk.get('state', 'Unknown')
    risk_score = risk.get('score')
    risk_line = f"Risk: {risk_state}"
    if risk_score is not None:
        risk_line += f" (score: {risk_score:.1f})"
    vix = risk.get('vix_level')
    if vix is not None:
        risk_line += f", VIX: {vix:.1f}"
    parts.append(risk_line)

    # Policy
    stance = policy.get('stance', 'Unknown')
    direction = policy.get('direction', 'Unknown')
    policy_line = f"Policy: {stance}, direction {direction}"
    taylor_gap = policy.get('taylor_gap')
    if taylor_gap is not None:
        policy_line += f" (Taylor gap: {taylor_gap:+.2f})"
    parts.append(policy_line)

    # Asset expectations
    expectations = conditions.get('asset_expectations', [])
    if expectations:
        parts.append("\nAsset expectations:")
        for exp in expectations:
            parts.append(f"  {exp['asset']}: {exp['direction']} ({exp['magnitude']})")

    return "\n".join(parts)


def _build_conditions_history_context(history, days=90):
    """Build a conditions history summary for the AI prompt.

    Args:
        history: dict mapping ISO date strings to condition snapshots.
        days: Number of days of history to include.

    Returns:
        Formatted string summarizing conditions trends over the period.
    """
    if not history:
        return ""

    eastern = pytz.timezone('US/Eastern')
    cutoff = (datetime.now(eastern) - timedelta(days=days)).strftime('%Y-%m-%d')

    # Filter and sort chronologically
    recent = {d: v for d, v in history.items() if d >= cutoff}
    if not recent:
        return ""

    sorted_dates = sorted(recent.keys())

    parts = [f"\n## CONDITIONS HISTORY ({len(sorted_dates)} snapshots, last {days} days)"]

    # Show each snapshot compactly
    for date in sorted_dates:
        entry = recent[date]
        quadrant = entry.get('quadrant', '?')
        dims = entry.get('dimensions', {})
        liq_state = dims.get('liquidity', {}).get('state', '?')
        liq_score = dims.get('liquidity', {}).get('score')
        risk_state = dims.get('risk', {}).get('state', '?')
        policy_stance = dims.get('policy', {}).get('stance', '?')
        policy_dir = dims.get('policy', {}).get('direction', '?')

        # Include growth/inflation scores if available (per PM note on #333)
        growth = entry.get('growth_score') or dims.get('quadrant', {}).get('growth_composite')
        inflation = entry.get('inflation_score') or dims.get('quadrant', {}).get('inflation_composite')

        score_str = ""
        if growth is not None and inflation is not None:
            score_str = f" [growth={growth:+.2f}, inflation={inflation:+.2f}]"

        liq_str = liq_state
        if liq_score is not None:
            liq_str = f"{liq_state}({liq_score:.2f})"

        # Transition watch indicator
        tw = entry.get('transition_watch') or dims.get('quadrant', {}).get('transition_watch')
        tw_str = ""
        if tw is not None:
            tw_str = f" [WATCH→{tw.get('direction', '?')}]"

        parts.append(
            f"  {date}: {quadrant}{score_str}{tw_str} | Liq: {liq_str} | Risk: {risk_state} | Policy: {policy_stance}/{policy_dir}"
        )

    # Compute summary statistics for the AI
    quadrants = [recent[d].get('quadrant') for d in sorted_dates if recent[d].get('quadrant')]
    if quadrants:
        current = quadrants[-1]
        # Count consecutive days in current quadrant from the end
        streak = 0
        for q in reversed(quadrants):
            if q == current:
                streak += 1
            else:
                break
        if streak > 1:
            parts.append(f"\nCurrent quadrant streak: {current} for {streak} consecutive snapshots")

        # Note any transitions
        transitions = []
        for i in range(1, len(quadrants)):
            if quadrants[i] != quadrants[i - 1]:
                transitions.append(f"{sorted_dates[i]}: {quadrants[i-1]} → {quadrants[i]}")
        if transitions:
            parts.append("Transitions: " + "; ".join(transitions))

    return "\n".join(parts)


def _generate_fallback_briefing(conditions):
    """Generate a rule-based briefing when AI is unavailable.

    Args:
        conditions: dict from get_market_conditions().

    Returns:
        String with a coherent briefing built from dimension states.
    """
    if not conditions:
        return "Market conditions data is currently unavailable. This week's review will follow once it is back."

    quadrant = conditions.get('quadrant', 'Unknown')
    dims = conditions.get('dimensions', {})
    liq = dims.get('liquidity', {})
    risk = dims.get('risk', {})
    policy = dims.get('policy', {})

    liq_state = liq.get('state', 'Unknown')
    risk_state = risk.get('state', 'Normal')
    policy_stance = policy.get('stance', 'Neutral')
    policy_dir = policy.get('direction', 'Paused')

    # Quadrant descriptions
    quadrant_desc = {
        'Goldilocks': "Growth is accelerating while inflation is cooling — the most favorable macro environment for risk assets.",
        'Reflation': "Both growth and inflation are accelerating — good for equities and commodities, but bonds face headwinds from rising rate expectations.",
        'Stagflation': "Growth is decelerating while inflation remains sticky — the most challenging environment for portfolios, historically favoring gold and real assets.",
        'Deflation Risk': "Growth is decelerating alongside falling inflation — bonds benefit from flight to safety, while equities face earnings headwinds.",
    }

    # Liquidity context
    liq_desc = {
        'Expanding': "Expanding liquidity provides a tailwind for risk assets.",
        'Strongly Expanding': "Strongly expanding liquidity is a powerful tailwind for risk assets, particularly crypto and equities.",
        'Neutral': "Liquidity conditions are neutral, providing neither tailwind nor headwind.",
        'Contracting': "Contracting liquidity creates headwinds across asset classes.",
        'Strongly Contracting': "Strongly contracting liquidity is a significant headwind — historically associated with drawdowns.",
    }

    # Risk context
    risk_desc = {
        'Calm': "Risk appetite is healthy with low volatility.",
        'Normal': "Volatility is within normal ranges.",
        'Elevated': "Elevated volatility signals caution — markets are pricing in uncertainty.",
        'Stressed': "Risk markets are under stress — defensive positioning is warranted.",
    }

    # Policy context
    policy_desc = {
        ('Accommodative', 'Easing'): "The Fed is actively easing — a tailwind for both stocks and bonds.",
        ('Accommodative', 'Paused'): "Monetary policy is accommodative and on hold — supportive of asset prices.",
        ('Accommodative', 'Tightening'): "Policy is transitioning from accommodative — watch for the shift in market leadership.",
        ('Neutral', 'Easing'): "The Fed is moving toward easier policy — a developing tailwind for markets.",
        ('Neutral', 'Paused'): "Monetary policy is neutral and stable — markets driven by fundamentals rather than policy.",
        ('Neutral', 'Tightening'): "The Fed is tightening from a neutral stance — headwinds building for rate-sensitive sectors.",
        ('Restrictive', 'Easing'): "Policy is restrictive but easing — the most important macro development to watch.",
        ('Restrictive', 'Paused'): "Policy remains restrictive — higher-for-longer weighs on duration and leveraged assets.",
        ('Restrictive', 'Tightening'): "Aggressive tightening continues — historically a precursor to significant market stress.",
    }

    p1 = quadrant_desc.get(quadrant, f"The current macro quadrant is {quadrant}.")
    p2 = liq_desc.get(liq_state, "") + " " + risk_desc.get(risk_state, "")
    p3 = policy_desc.get((policy_stance, policy_dir), f"Monetary policy stance is {policy_stance} with a {policy_dir.lower()} direction.")

    return f"The macro environment is currently in {quadrant}. {p1}\n\n{p2.strip()}\n\n{p3}"


def _previous_reviews(records, week_start, limit=2):
    """The latest review from each of the `limit` weeks before `week_start`.

    Records from before US-490.1 are daily; grouping by trading week keeps one
    per week, so three weeks of daily history cannot flood the prompt.
    """
    by_week = {}
    for record in records:
        try:
            if record.get('week_start'):
                rec_week = date.fromisoformat(record['week_start'])
            else:
                rec_week = briefing_week.trading_week(date.fromisoformat(record['date']))[0]
        except (KeyError, TypeError, ValueError):
            continue
        if rec_week >= week_start:
            continue
        if rec_week not in by_week or record['date'] >= by_week[rec_week]['date']:
            by_week[rec_week] = record
    weeks = sorted(by_week)[-limit:]
    return [(w, by_week[w]) for w in weeks]


def _previous_reviews_block(records, week_start, title):
    previous = _previous_reviews(records, week_start)
    if not previous:
        return ""
    block = f"\n\n## {title} (earlier weeks, for continuity: refer back to earlier calls when relevant, but do not repeat their themes):\n"
    for week, record in previous:
        block += f"\n### Week of {week.isoformat()}:\n{record['summary']}\n"
    return block


def _same_week(record, week_start):
    """True if a stored briefing belongs to the week now being reviewed."""
    if not record:
        return False
    if record.get('week_start'):
        return record['week_start'] == week_start.isoformat()
    try:
        return briefing_week.trading_week(date.fromisoformat(record['date']))[0] == week_start
    except (KeyError, TypeError, ValueError):
        return False


GENERAL_SYSTEM_PROMPT = """You are the host of MacroClarity's Week in Review: one calm, trusted read on what changed in markets this week, why it matters, and what to watch next week. Your reader checks in once a week instead of following markets every session, and has chosen you because:

1. You separate signal from noise. A move that reversed within the week is usually noise; a data surprise, a policy shift or a trend that held all week is signal.
2. You CONNECT THE DOTS across stocks, rates, credit, the dollar and crypto, and tell one coherent story of the week.
3. You point out when something is historically unusual, and you are honest about uncertainty.
4. You make people SMARTER about markets, not just informed.

Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Never cite z-scores or percentiles; translate them into plain-language magnitude (e.g., "near historic highs," "the largest weekly move in months"). Weekly levels and moves are fine in *What changed*; in *Why it matters*, favour plain-language magnitude over raw figures.

RULES:
- Ground the review in the market conditions framework. Use only the four quadrants: Goldilocks, Reflation, Stagflation, Deflation Risk. Say whether this week's data moved us toward or away from the current quadrant, using the conditions history for how long we have been there.
- *What changed* covers only things that happened during the week under review. Never describe an earlier week's event or release as this week's.
- When you cite a weekly move, use the WEEK IN NUMBERS figures exactly. Never compute a weekly change yourself from the latest levels; for a series not in WEEK IN NUMBERS, cite its level only.
- The section reviews provided were written in this same run from the same data. Synthesize them into one picture rather than summarizing each in turn. Any figure that also appears in a section review must carry the identical value.
- Do not repeat themes from earlier weeks' reviews; refer back to an earlier call when it matters ("Last week's review flagged...").
- If news events drove markets this week, weave them in.

You have access to a web search tool. Use it to check the timing of this week's data releases and to build next week's calendar.

""" + weekly_review.copy_rules() + "\n\n" + weekly_review.output_format_instructions(weekly_review.GENERAL)


def generate_weekly_summary(market_data_summary, top_movers):
    """
    Generate the general week-in-review briefing (US-490.2).

    Runs last in the weekly job, after the five section reviews, and feeds
    those reviews into the prompt so the run reads as one picture.

    Args:
        market_data_summary: String output from generate_market_summary()
        top_movers: List of top 5-day movers from calculate_top_movers(period=5)

    Returns:
        dict with 'success', 'summary', 'structured' and 'error' keys
    """
    week_start, week_end, through = briefing_week.current_week()
    eastern = pytz.timezone('US/Eastern')
    today = datetime.now(eastern).strftime('%Y-%m-%d')

    client, error = get_ai_client()
    if client is None:
        # AI unavailable — generate rule-based fallback (US-325.1)
        print("[AI Summary] AI client unavailable, generating rule-based fallback briefing")
        conditions = get_market_conditions()
        fallback = _generate_fallback_briefing(conditions)
        save_summary(date_str=today, summary_text=fallback, web_search_used=False, news_context=None)
        return {'success': True, 'summary': fallback, 'structured': None, 'error': None}

    try:
        # Earlier weeks' reviews, for continuity
        previous_context = _previous_reviews_block(
            get_recent_summaries(days=21), week_start, "YOUR PREVIOUS WEEKLY REVIEWS")

        # Read news context from stored pipeline data
        news_context = _get_stored_news_context()
        news_section = ""
        if news_context:
            news_section = f"\n\n## THIS WEEK'S NEWS (summary of the week's articles):\n{news_context}"

        # The section reviews from this week's run, generated before this one
        briefings_found = []
        for label, record in (
            ("Crypto/Bitcoin", get_latest_crypto_summary()),
            ("Equity Markets", get_latest_equity_summary()),
            ("Rates & Yield Curve", get_latest_rates_summary()),
            ("Dollar & Currency", get_latest_dollar_summary()),
            ("Credit Markets", get_latest_credit_summary()),
        ):
            if _same_week(record, week_start):
                briefings_found.append(f"### {label} review:\n{record['summary']}")

        specific_briefings_context = ""
        if briefings_found:
            specific_briefings_context = (
                "\n\n## THIS WEEK'S SECTION REVIEWS (same run, same data: synthesize these and "
                "keep every shared figure identical):\n" + "\n\n".join(briefings_found))

        movers_text = ""
        if top_movers:
            movers_text = "\n\n## MOST UNUSUAL 5-DAY MOVES (largest relative to each series' own history):\n"
            for m in top_movers[:5]:
                direction = "up" if m['change'] > 0 else "down"
                movers_text += f"- {m['name']}: {m['change']:+.1f}{m['unit']} ({direction})\n"

        # Build market conditions context (US-325.1)
        conditions = get_market_conditions()
        conditions_context = _build_conditions_context(conditions)

        # Build 90-day conditions history context
        history = get_conditions_history()
        conditions_history = _build_conditions_history_context(history, days=90)

        week_numbers = weekly_review.week_in_numbers('general', week_start, through)
        releases = weekly_review.macro_readings(week_start, through)
        next_start, next_end = weekly_review.next_trading_week(week_end)
        calendar = weekly_review.calendar_instructions(next_start, next_end, is_tavily_configured())

        user_prompt = f"""{weekly_review.week_context_line(week_start, through)} Write this week's review.

{conditions_context}
{conditions_history}

{week_numbers}

{releases}

## LATEST LEVELS (as of the most recent close)
{market_data_summary}
{movers_text}
{news_section}
{specific_briefings_context}
{previous_context}

{calendar}

Remember: headline, then What changed, Why it matters and What to watch next week, in the exact format. One coherent picture of the week, no repeated themes from earlier weeks."""

        result = call_ai_with_tools(
            client=client,
            system_prompt=GENERAL_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=16000,
            log_prefix="[AI Summary]",
            effort=weekly_review.REVIEW_EFFORT,
        )

        if not result['success']:
            # Fall back to rule-based briefing when AI is unavailable
            print("[AI Summary] AI call failed, generating rule-based fallback briefing")
            fallback = _generate_fallback_briefing(conditions)
            save_summary(
                date_str=today,
                summary_text=fallback,
                web_search_used=False,
                news_context=None
            )
            return {'success': True, 'summary': fallback, 'structured': None, 'error': None}

        summary = result['content']
        structured, _ = weekly_review.parse_and_log(
            summary, weekly_review.GENERAL, result.get('truncated', False), "[AI Summary]")

        save_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context,
            structured=structured,
        )

        print(f"[AI Summary] Generated weekly review for {week_start} to {through}")
        return {'success': True, 'summary': summary, 'structured': structured, 'error': None}

    except Exception as e:
        print(f"[AI Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'structured': None,
            'error': str(e)
        }


def get_summary_for_display():
    """
    Get the current summary formatted for dashboard display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary'],
            'web_search_used': summary.get('web_search_used', False),
            **briefing_week.display_fields(summary),
            **_display_parts(summary),
        }
    return None


# ============================================================================
# CRYPTO-SPECIFIC AI SUMMARY
# ============================================================================

CRYPTO_SUMMARIES_FILE = DATA_DIR / "crypto_summaries.json"


def load_crypto_summaries():
    """Load all stored Crypto AI summaries."""
    if CRYPTO_SUMMARIES_FILE.exists():
        try:
            with open(CRYPTO_SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def save_crypto_summaries(data):
    """Save crypto summaries to JSON file."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(CRYPTO_SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_crypto_summary():
    """Get the most recent Crypto AI summary."""
    data = load_crypto_summaries()
    if data["summaries"]:
        sorted_summaries = sorted(data["summaries"], key=lambda x: x["date"], reverse=True)
        return sorted_summaries[0]
    return None


def get_recent_crypto_summaries(days=3):
    """Get crypto summaries from the last N days for context."""
    return _get_recent_summaries(load_crypto_summaries(), days)


def save_crypto_summary(date_str, summary_text, web_search_used=False, news_context=None, structured=None):
    """Save a new crypto summary, overwriting if same date exists."""
    data = load_crypto_summaries()

    # Remove existing summary for this date if present
    data["summaries"] = [s for s in data["summaries"] if s["date"] != date_str]

    eastern = pytz.timezone('US/Eastern')
    # Add new summary
    data["summaries"].append({
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "web_search_used": web_search_used,
        "news_context": news_context[:500] if news_context else None,
        **briefing_week.record_metadata(),
        **_format_fields(structured),
    })

    # Keep last 90 days of summaries
    cutoff = (datetime.now(eastern) - timedelta(days=90)).strftime('%Y-%m-%d')
    data["summaries"] = [s for s in data["summaries"] if s["date"] >= cutoff]

    save_crypto_summaries(data)


def fetch_crypto_news():
    """Fetch current crypto/Bitcoin news for summary context."""
    if not is_tavily_configured():
        return None

    news_parts = []

    # Bitcoin/crypto news
    btc_news = search_financial_news("Bitcoin BTC crypto market news today", max_results=5)
    if btc_news.get('results'):
        news_parts.append("## Today's Bitcoin/Crypto News")
        if btc_news.get('answer'):
            news_parts.append(btc_news['answer'])
        for r in btc_news['results'][:3]:
            news_parts.append(f"- {r['title']}: {r['content'][:200]}...")

    # Fed/liquidity news (important for BTC)
    fed_news = search_web("Federal Reserve liquidity QT QE monetary policy today", max_results=3)
    if fed_news.get('results'):
        news_parts.append("\n## Fed/Liquidity News")
        for r in fed_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    return "\n".join(news_parts) if news_parts else None


def generate_crypto_summary(crypto_data_summary):
    """Generate the Crypto/Bitcoin week-in-review. See generate_section_review()."""
    return generate_section_review('crypto', crypto_data_summary)


def get_crypto_summary_for_display():
    """
    Get the current crypto summary formatted for display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_crypto_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary'],
            'web_search_used': summary.get('web_search_used', False),
            **briefing_week.display_fields(summary),
            **_display_parts(summary),
        }
    return None


# ============================================================================
# EQUITY MARKETS AI SUMMARY
# ============================================================================

EQUITY_SUMMARIES_FILE = DATA_DIR / "equity_summaries.json"


def load_equity_summaries():
    """Load all stored Equity AI summaries."""
    if EQUITY_SUMMARIES_FILE.exists():
        try:
            with open(EQUITY_SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def save_equity_summaries(data):
    """Save equity summaries to JSON file."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(EQUITY_SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_equity_summary():
    """Get the most recent Equity AI summary."""
    data = load_equity_summaries()
    if data["summaries"]:
        sorted_summaries = sorted(data["summaries"], key=lambda x: x["date"], reverse=True)
        return sorted_summaries[0]
    return None


def get_recent_equity_summaries(days=3):
    """Get equity summaries from the last N days for context."""
    return _get_recent_summaries(load_equity_summaries(), days)


def save_equity_summary(date_str, summary_text, web_search_used=False, news_context=None, structured=None):
    """Save a new equity summary, overwriting if same date exists."""
    data = load_equity_summaries()

    # Remove existing summary for this date if present
    data["summaries"] = [s for s in data["summaries"] if s["date"] != date_str]

    eastern = pytz.timezone('US/Eastern')
    # Add new summary
    data["summaries"].append({
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "web_search_used": web_search_used,
        "news_context": news_context[:500] if news_context else None,
        **briefing_week.record_metadata(),
        **_format_fields(structured),
    })

    # Keep last 90 days of summaries
    cutoff = (datetime.now(eastern) - timedelta(days=90)).strftime('%Y-%m-%d')
    data["summaries"] = [s for s in data["summaries"] if s["date"] >= cutoff]

    save_equity_summaries(data)


def fetch_equity_news():
    """Fetch current equity/stock market news for summary context."""
    if not is_tavily_configured():
        return None

    news_parts = []

    # Stock market news
    market_news = search_financial_news("stock market S&P 500 equity market news today", max_results=5)
    if market_news.get('results'):
        news_parts.append("## Today's Stock Market News")
        if market_news.get('answer'):
            news_parts.append(market_news['answer'])
        for r in market_news['results'][:3]:
            news_parts.append(f"- {r['title']}: {r['content'][:200]}...")

    # Sector rotation / earnings news
    sector_news = search_web("stock sector rotation earnings reports today", max_results=3)
    if sector_news.get('results'):
        news_parts.append("\n## Sector & Earnings News")
        for r in sector_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    return "\n".join(news_parts) if news_parts else None


def generate_equity_summary(equity_data_summary):
    """Generate the Equity Markets week-in-review. See generate_section_review()."""
    return generate_section_review('equity', equity_data_summary)


def get_equity_summary_for_display():
    """
    Get the current equity summary formatted for display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_equity_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary'],
            'web_search_used': summary.get('web_search_used', False),
            **briefing_week.display_fields(summary),
            **_display_parts(summary),
        }
    return None


# ============================================================================
# RATES SUMMARY FUNCTIONS
# ============================================================================

RATES_SUMMARIES_FILE = DATA_DIR / "rates_summaries.json"


def load_rates_summaries():
    """Load all stored rates AI summaries."""
    if RATES_SUMMARIES_FILE.exists():
        try:
            with open(RATES_SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def save_rates_summary(date_str, summary_text, web_search_used=False, news_context=None, structured=None):
    """Save a rates AI summary to storage."""
    data = load_rates_summaries()

    eastern = pytz.timezone('US/Eastern')
    # Update existing or add new
    existing_idx = None
    for idx, s in enumerate(data["summaries"]):
        if s["date"] == date_str:
            existing_idx = idx
            break

    summary_entry = {
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "web_search_used": web_search_used,
        "news_context": news_context[:500] if news_context else None,
        **briefing_week.record_metadata(),
        **_format_fields(structured),
    }

    if existing_idx is not None:
        data["summaries"][existing_idx] = summary_entry
    else:
        data["summaries"].append(summary_entry)

    # Keep only the last 30 summaries
    data["summaries"] = sorted(data["summaries"], key=lambda x: x["date"])[-30:]

    # Ensure data directory exists
    DATA_DIR.mkdir(exist_ok=True)

    with open(RATES_SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_rates_summary():
    """Get the most recent rates AI summary."""
    data = load_rates_summaries()
    if data["summaries"]:
        return sorted(data["summaries"], key=lambda x: x["date"])[-1]
    return None


def get_recent_rates_summaries(days=3):
    """Get rates summaries from the last N days for context."""
    return _get_recent_summaries(load_rates_summaries(), days)


def fetch_rates_news():
    """Fetch current rates/bond market news using web search."""
    if not is_tavily_configured():
        return None

    news_parts = []

    # Treasury/bond market news
    rates_news = search_financial_news("Treasury yields bond market Fed interest rates news today", max_results=5)
    if rates_news.get('results'):
        news_parts.append("## Today's Rates & Bond Market News")
        if rates_news.get('answer'):
            news_parts.append(rates_news['answer'])
        for r in rates_news['results'][:3]:
            news_parts.append(f"- {r['title']}: {r['content'][:200]}...")

    # Fed/monetary policy news
    fed_news = search_web("Federal Reserve monetary policy interest rate decision", max_results=3)
    if fed_news.get('results'):
        news_parts.append("\n## Fed & Monetary Policy")
        for r in fed_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    return "\n".join(news_parts) if news_parts else None


def generate_rates_summary(rates_data_summary):
    """Generate the Rates & Yield Curve week-in-review. See generate_section_review()."""
    return generate_section_review('rates', rates_data_summary)


def get_rates_summary_for_display():
    """
    Get the current rates summary formatted for display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_rates_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary'],
            'web_search_used': summary.get('web_search_used', False),
            **briefing_week.display_fields(summary),
            **_display_parts(summary),
        }
    return None


# ============================================================================
# DOLLAR SUMMARY FUNCTIONS
# ============================================================================

DOLLAR_SUMMARIES_FILE = DATA_DIR / "dollar_summaries.json"


def load_dollar_summaries():
    """Load all stored dollar AI summaries."""
    if DOLLAR_SUMMARIES_FILE.exists():
        try:
            with open(DOLLAR_SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def save_dollar_summary(date_str, summary_text, web_search_used=False, news_context=None, structured=None):
    """Save a dollar AI summary to storage."""
    data = load_dollar_summaries()

    eastern = pytz.timezone('US/Eastern')
    # Update existing or add new
    existing_idx = None
    for idx, s in enumerate(data["summaries"]):
        if s["date"] == date_str:
            existing_idx = idx
            break

    summary_entry = {
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "web_search_used": web_search_used,
        "news_context": news_context[:500] if news_context else None,
        **briefing_week.record_metadata(),
        **_format_fields(structured),
    }

    if existing_idx is not None:
        data["summaries"][existing_idx] = summary_entry
    else:
        data["summaries"].append(summary_entry)

    # Keep only the last 30 summaries
    data["summaries"] = sorted(data["summaries"], key=lambda x: x["date"])[-30:]

    # Ensure data directory exists
    DATA_DIR.mkdir(exist_ok=True)

    with open(DOLLAR_SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_dollar_summary():
    """Get the most recent dollar AI summary."""
    data = load_dollar_summaries()
    if data["summaries"]:
        return sorted(data["summaries"], key=lambda x: x["date"])[-1]
    return None


def get_recent_dollar_summaries(days=3):
    """Get dollar summaries from the last N days for context."""
    return _get_recent_summaries(load_dollar_summaries(), days)


def fetch_dollar_news():
    """Fetch current dollar/currency market news using web search."""
    if not is_tavily_configured():
        return None

    news_parts = []

    # Dollar/currency news
    dollar_news = search_financial_news("US dollar DXY currency forex Fed policy news today", max_results=5)
    if dollar_news.get('results'):
        news_parts.append("## Today's Dollar & Currency News")
        if dollar_news.get('answer'):
            news_parts.append(dollar_news['answer'])
        for r in dollar_news['results'][:3]:
            news_parts.append(f"- {r['title']}: {r['content'][:200]}...")

    # Central bank/forex news
    cb_news = search_web("central bank policy Bank of Japan ECB interest rate", max_results=3)
    if cb_news.get('results'):
        news_parts.append("\n## Central Bank & Policy")
        for r in cb_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    return "\n".join(news_parts) if news_parts else None


def generate_dollar_summary(dollar_data_summary):
    """Generate the Dollar & Currency week-in-review. See generate_section_review()."""
    return generate_section_review('dollar', dollar_data_summary)


def get_dollar_summary_for_display():
    """
    Get the current dollar summary formatted for display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_dollar_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary'],
            'web_search_used': summary.get('web_search_used', False),
            **briefing_week.display_fields(summary),
            **_display_parts(summary),
        }
    return None


# ============================================================================
# Credit Summary Functions
# ============================================================================

CREDIT_SUMMARIES_FILE = DATA_DIR / "credit_summaries.json"


def load_credit_summaries():
    """Load all stored credit AI summaries."""
    if CREDIT_SUMMARIES_FILE.exists():
        try:
            with open(CREDIT_SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def save_credit_summary(date_str, summary_text, web_search_used=False, news_context=None, structured=None):
    """Save a credit AI summary to storage."""
    data = load_credit_summaries()

    eastern = pytz.timezone('US/Eastern')
    existing_idx = None
    for idx, s in enumerate(data["summaries"]):
        if s["date"] == date_str:
            existing_idx = idx
            break

    summary_entry = {
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "web_search_used": web_search_used,
        "news_context": news_context[:500] if news_context else None,
        **briefing_week.record_metadata(),
        **_format_fields(structured),
    }

    if existing_idx is not None:
        data["summaries"][existing_idx] = summary_entry
    else:
        data["summaries"].append(summary_entry)

    data["summaries"] = sorted(data["summaries"], key=lambda x: x["date"])[-30:]

    DATA_DIR.mkdir(exist_ok=True)
    with open(CREDIT_SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_credit_summary():
    """Get the most recent credit AI summary."""
    data = load_credit_summaries()
    if data["summaries"]:
        return sorted(data["summaries"], key=lambda x: x["date"])[-1]
    return None


def get_recent_credit_summaries(days=3):
    """Get credit summaries from the last N days for context."""
    return _get_recent_summaries(load_credit_summaries(), days)


def fetch_credit_news():
    """Fetch current credit market news using web search."""
    if not is_tavily_configured():
        return None

    news_parts = []

    credit_news = search_financial_news("high yield credit spreads corporate bonds Fed policy news today", max_results=5)
    if credit_news.get('results'):
        news_parts.append("## Today's Credit Market News")
        if credit_news.get('answer'):
            news_parts.append(credit_news['answer'])
        for r in credit_news['results'][:3]:
            news_parts.append(f"- {r['title']}: {r['content'][:200]}...")

    ig_news = search_web("investment grade credit spreads corporate debt issuance", max_results=3)
    if ig_news.get('results'):
        news_parts.append("\n## Investment Grade & Issuance")
        for r in ig_news['results'][:2]:
            news_parts.append(f"- {r['title']}: {r['content'][:150]}...")

    return "\n".join(news_parts) if news_parts else None


def generate_credit_summary(credit_data_summary):
    """Generate the Credit Markets week-in-review. See generate_section_review()."""
    return generate_section_review('credit', credit_data_summary)


def get_credit_summary_for_display():
    """
    Get the current credit summary formatted for display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_credit_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary'],
            'web_search_used': summary.get('web_search_used', False),
            **briefing_week.display_fields(summary),
            **_display_parts(summary),
        }
    return None


# ============================================================================
# Section week-in-review (US-490.2)
# ============================================================================
# The five section reviews share one prompt shape and one code path. Each
# entry carries what differs: the analyst persona, the section's focus, the
# metrics in its WEEK IN NUMBERS block, and its storage functions.

SECTION_REVIEWS = {
    'crypto': {
        'log_prefix': '[Crypto Summary]',
        'title': 'Bitcoin',
        'persona': 'a Bitcoin/crypto market analyst writing the weekly review for the Crypto page',
        'audience': 'Your reader tracks Bitcoin alongside macro liquidity indicators like the Fed balance sheet, financial conditions (NFCI) and the Fear & Greed index.',
        'focus': (
            "Bitcoin's week: price action, sentiment, and how macro liquidity (Fed balance sheet, "
            "financial conditions, M2, the dollar) shaped it. Bitcoin is a liquidity-sensitive "
            "asset, so explain the cause and effect. If Fear & Greed is extreme (below 25 or above "
            "75), give the historical contrarian read. Name the price levels that matter and why."
        ),
        'save': save_crypto_summary,
        'recent': get_recent_crypto_summaries,
    },
    'equity': {
        'log_prefix': '[Equity Summary]',
        'title': 'Equity',
        'persona': 'an equity market analyst writing the weekly review for the Equity Markets page',
        'audience': 'Your reader wants to understand what is happening under the surface of the stock market, not just whether it went up or down.',
        'focus': (
            "The equity market's week: which indices, sectors and styles led or lagged, market "
            "breadth and concentration, growth vs value and small vs large caps, and what they say "
            "about economic expectations and risk appetite. Explain why narrow breadth or extreme "
            "concentration matters in plain terms."
        ),
        'save': save_equity_summary,
        'recent': get_recent_equity_summaries,
    },
    'rates': {
        'log_prefix': '[Rates Summary]',
        'title': 'Rates',
        'persona': 'a fixed income analyst writing the weekly review for the Rates & Yield Curve page',
        'audience': 'Your reader wants to know what rates are saying about the economy and what that means for their portfolio.',
        'focus': (
            "Treasury yields and the curve over the week: what moved yields (data, Fed "
            "expectations, supply), the curve's shape and what it has historically signalled, real "
            "yields against the Fed's stance, breakeven inflation expectations, and what rates mean "
            "for stocks, credit and duration. If the curve is inverted or recently un-inverted, "
            "explain the recession read clearly."
        ),
        'save': save_rates_summary,
        'recent': get_recent_rates_summaries,
    },
    'dollar': {
        'log_prefix': '[Dollar Summary]',
        'title': 'Currency',
        'persona': 'a currency analyst writing the weekly review for the Dollar & Currency page',
        'audience': 'Your reader wants to know what the dollar says about global risk appetite and policy divergence, and what it means for their portfolio.',
        'focus': (
            "The dollar's week: the dollar index and key pairs (especially USD/JPY), Fed "
            "expectations and central bank divergence, where we are in the \"Dollar Smile\", "
            "carry-trade dynamics, and the cross-asset effects on emerging markets, commodities and "
            "multinational earnings."
        ),
        'save': save_dollar_summary,
        'recent': get_recent_dollar_summaries,
    },
    'credit': {
        'log_prefix': '[Credit Summary]',
        'title': 'Credit',
        'persona': 'a credit market analyst writing the weekly review for the Credit Markets page',
        'audience': 'Your reader wants to know what the credit market is pricing in about economic health, default risk and financial conditions.',
        'focus': (
            "Corporate credit over the week: high-yield and investment-grade spreads against "
            "history, CCC vs high-yield (where distress is concentrated), what spreads imply about "
            "default risk and the cycle, how spreads fit the current market conditions quadrant, "
            "and whether credit is confirming or diverging from equities. If high-yield spreads are notably tight, explain the complacency risk."
        ),
        'save': save_credit_summary,
        'recent': get_recent_credit_summaries,
    },
}


def section_system_prompt(section):
    """System prompt for one section's weekly review."""
    cfg = SECTION_REVIEWS[section]
    return f"""You are {cfg['persona']} of MacroClarity, a calm weekly read for individual investors. {cfg['audience']}

Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Never cite z-scores or percentiles; translate them into plain-language magnitude (e.g., "near historic highs," "the largest weekly move in months"). Weekly levels and moves are fine in *What changed*.

FOCUS: {cfg['focus']}

RULES:
- Separate signal from noise: a move that reversed within the week is usually noise; a data surprise, a policy shift or a trend that held all week is signal.
- *What changed* covers only things that happened during the week under review. Never describe an earlier week's event as this week's.
- When you cite a weekly move, use the WEEK IN NUMBERS figures exactly. Never compute a weekly change yourself from the latest levels; for a series not in WEEK IN NUMBERS, cite its level only. The general review is built from yours, so its figures must match.
- Do not repeat themes from earlier weeks' reviews.

You have access to a web search tool. Use it to check the timing of this week's releases and to build next week's calendar for this market.

{weekly_review.copy_rules()}

{weekly_review.output_format_instructions(weekly_review.SECTION)}"""


def generate_section_review(section, data_summary):
    """
    Generate one section's week-in-review briefing (US-490.2).

    Args:
        section: key of SECTION_REVIEWS ('crypto', 'equity', 'rates', 'dollar', 'credit')
        data_summary: String with the section's market data from the
            dashboard's generate_<section>_market_summary()

    Returns:
        dict with 'success', 'summary', 'structured' and 'error' keys
    """
    cfg = SECTION_REVIEWS[section]
    log_prefix = cfg['log_prefix']
    client, error = get_ai_client()
    if client is None:
        return {'success': False, 'summary': None, 'structured': None, 'error': error}

    try:
        week_start, week_end, through = briefing_week.current_week()
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        previous_context = _previous_reviews_block(
            cfg['recent'](days=21), week_start,
            f"YOUR PREVIOUS {section.upper()} REVIEWS")

        # Read the section's news context from stored pipeline data
        news_context = _get_stored_news_context(topic=section)
        news_section = f"\n\n{news_context}" if news_context else ""

        week_numbers = weekly_review.week_in_numbers(section, week_start, through)
        next_start, next_end = weekly_review.next_trading_week(week_end)
        calendar = weekly_review.calendar_instructions(next_start, next_end, is_tavily_configured())

        user_prompt = f"""{weekly_review.week_context_line(week_start, through)} Write this week's {cfg['title'].lower()} review.

{week_numbers}

## LATEST LEVELS (as of the most recent close)
{data_summary}
{news_section}
{previous_context}

{calendar}

Remember: What changed, Why it matters and What to watch next week, in the exact format, scoped to this market."""

        print(f"{log_prefix} Calling AI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=section_system_prompt(section),
            user_prompt=user_prompt,
            max_tokens=16000,
            log_prefix=log_prefix,
            effort=weekly_review.REVIEW_EFFORT,
        )

        if not result['success']:
            return {'success': False, 'summary': None, 'structured': None, 'error': result['error']}

        summary = result['content']
        structured, _ = weekly_review.parse_and_log(
            summary, weekly_review.SECTION, result.get('truncated', False), log_prefix)

        cfg['save'](
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context,
            structured=structured,
        )

        print(f"{log_prefix} Generated {section} review for {week_start} to {through}")
        return {'success': True, 'summary': summary, 'structured': structured, 'error': None}

    except Exception as e:
        print(f"{log_prefix} Error generating summary: {e}")
        return {'success': False, 'summary': None, 'structured': None, 'error': str(e)}


# ============================================================================
# Portfolio Summary Functions
# ============================================================================

PORTFOLIO_SUMMARIES_FILE = DATA_DIR / "portfolio_summaries.json"


def load_portfolio_summaries():
    """Load all stored portfolio AI summaries."""
    if PORTFOLIO_SUMMARIES_FILE.exists():
        try:
            with open(PORTFOLIO_SUMMARIES_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"summaries": []}
    return {"summaries": []}


def save_portfolio_summary_entry(date_str, summary_text, portfolio_context=None):
    """Save a portfolio AI summary to storage."""
    data = load_portfolio_summaries()

    eastern = pytz.timezone('US/Eastern')
    # Update existing or add new
    existing_idx = None
    for idx, s in enumerate(data["summaries"]):
        if s["date"] == date_str:
            existing_idx = idx
            break

    summary_entry = {
        "date": date_str,
        "generated_at": datetime.now(eastern).isoformat(),
        "summary": summary_text,
        "portfolio_context": portfolio_context[:1000] if portfolio_context else None
    }

    if existing_idx is not None:
        data["summaries"][existing_idx] = summary_entry
    else:
        data["summaries"].append(summary_entry)

    # Keep only the last 30 summaries
    data["summaries"] = sorted(data["summaries"], key=lambda x: x["date"])[-30:]

    # Ensure data directory exists
    DATA_DIR.mkdir(exist_ok=True)

    with open(PORTFOLIO_SUMMARIES_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_latest_portfolio_summary():
    """Get the most recent portfolio AI summary."""
    data = load_portfolio_summaries()
    if data["summaries"]:
        return sorted(data["summaries"], key=lambda x: x["date"])[-1]
    return None


def get_recent_portfolio_summaries(days=3):
    """Get portfolio summaries from the last N days for context."""
    return _get_recent_summaries(load_portfolio_summaries(), days)


# =============================================================================
# Database-Backed Portfolio Summary Functions (Multi-User Mode)
# =============================================================================

# Database imports (optional - only available when Flask app is running)
try:
    from extensions import db
    from models.portfolio_summary import PortfolioSummary
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    db = None
    PortfolioSummary = None


def db_save_portfolio_summary(user_id, date_str, summary_text, portfolio_context=None):
    """
    Save a portfolio AI summary to database for a specific user.

    Args:
        user_id: UUID of the user
        date_str: Date string in YYYY-MM-DD format
        summary_text: The AI-generated summary
        portfolio_context: Optional snapshot of portfolio data
    """
    if not DB_AVAILABLE:
        return

    eastern = pytz.timezone('US/Eastern')
    # Check if entry exists for this user and date
    existing = PortfolioSummary.query.filter_by(
        user_id=user_id,
        date=date_str
    ).first()

    if existing:
        # Update existing
        existing.summary = summary_text
        existing.portfolio_context = portfolio_context[:1000] if portfolio_context else None
        existing.generated_at = datetime.now(eastern)
    else:
        # Create new
        summary = PortfolioSummary(
            user_id=user_id,
            date=date_str,
            summary=summary_text,
            portfolio_context=portfolio_context[:1000] if portfolio_context else None
        )
        db.session.add(summary)

    db.session.commit()


def db_get_latest_portfolio_summary(user_id):
    """
    Get the most recent portfolio AI summary for a user.

    Args:
        user_id: UUID of the user

    Returns:
        Dict with summary data or None
    """
    if not DB_AVAILABLE:
        return None

    summary = PortfolioSummary.query.filter_by(user_id=user_id)\
        .order_by(PortfolioSummary.date.desc())\
        .first()

    if summary:
        return summary.to_dict()
    return None


def db_get_recent_portfolio_summaries(user_id, days=3):
    """
    Get recent portfolio summaries for a user for context.

    Args:
        user_id: UUID of the user
        days: Number of recent summaries to return

    Returns:
        List of summary dicts
    """
    if not DB_AVAILABLE:
        return []

    summaries = PortfolioSummary.query.filter_by(user_id=user_id)\
        .order_by(PortfolioSummary.date.desc())\
        .limit(days)\
        .all()

    return [s.to_dict() for s in summaries]


def db_get_portfolio_summary_for_display(user_id):
    """
    Get the latest portfolio summary formatted for display.

    Args:
        user_id: UUID of the user

    Returns:
        Dict with summary data or None
    """
    summary = db_get_latest_portfolio_summary(user_id)
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary']
        }
    return None


def generate_portfolio_summary(portfolio_data, market_context, user_id=None):
    """
    Generate an AI portfolio analysis summary.

    Args:
        portfolio_data: Dict with portfolio holdings and allocations from get_portfolio_summary_for_ai()
        market_context: String with market data and other AI briefings
        user_id: UUID of the user (for per-user summary storage and context).
                 If None, uses shared file storage (legacy mode).

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()

    if client is None:
        return {
            'success': False,
            'summary': None,
            'error': error or 'No AI client available'
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        # Get previous portfolio summaries for context (user-specific if user_id provided)
        if user_id and DB_AVAILABLE:
            recent_summaries = db_get_recent_portfolio_summaries(user_id, days=3)
        else:
            recent_summaries = get_recent_portfolio_summaries(days=3)

        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS PORTFOLIO ANALYSES (for continuity):\n"
            for s in recent_summaries:
                if s["date"] != today:
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Format portfolio data
        portfolio_section = format_portfolio_for_ai(portfolio_data)

        # Portfolio-specific system prompt
        system_prompt = """You are a portfolio analyst providing personalized briefings based on a user's investment allocations and current market conditions. Your audience is an individual investor who wants actionable insight on how their portfolio is positioned relative to current market dynamics.

Your style is conversational but substantive - you connect the dots between the user's holdings, current market conditions, and the insights from the day's other market briefings. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3-4 paragraphs (300-400 words total)
- First paragraph: Portfolio Overview - summarize the current allocation, highlight any concentration risk (>30% in single holding), and assess overall diversification across asset classes. Be specific about percentages.
- Second paragraph: Market Alignment - based on today's market conditions and briefings, evaluate whether the portfolio positioning makes sense. Are they heavy in equities when credit spreads are tight and VIX is low (potentially complacent)? Are they light on gold when safe havens are elevated? Connect their allocation to the current market story.
- Third paragraph: Specific Observations - analyze individual holdings where you have price data. Note any significant daily moves. If they hold tech-heavy ETFs and the equity briefing notes concentration risk, mention that. If they hold gold and it's at extreme percentiles, discuss implications.
- Final paragraph: Adjustments & Considerations - If the analysis reveals clear issues (concentration risk, poor market alignment, missing asset classes during favorable conditions), provide SPECIFIC adjustment recommendations. Be direct: "Consider reducing X from 40% to 25%" or "Adding 5-10% gold exposure would improve diversification." If the portfolio is well-positioned, say so and offer minor optimizations or simply affirm the current approach. Don't hedge excessively - give your honest assessment.

IMPORTANT:
- You are analyzing PERCENTAGES only - you don't know dollar amounts, so focus on relative weights
- Reference the day's AI briefings when relevant to their holdings
- Highlight both risks AND opportunities based on current conditions
- Don't make up price data - only reference prices if they were provided
- Be balanced - acknowledge both positives and areas for attention
- When adjustments are warranted, be SPECIFIC about what to change and by how much. General platitudes are not helpful. If you see a problem, name the solution directly.

You have access to a web search tool if you need to look up additional context about specific holdings or market conditions. Use it sparingly and only when truly helpful."""

        user_prompt = f"""Today is {today}. Generate a portfolio analysis for this allocation:

{portfolio_section}

## CURRENT MARKET CONTEXT:
{market_context}
{previous_context}

Remember: Analyze their allocation against current market conditions, be specific about percentages and risks, and provide SPECIFIC adjustment recommendations when warranted (not vague considerations)."""

        # Make the API call with web search tool support
        print(f"[Portfolio Summary] Calling OpenAI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=16000,
            log_prefix="[Portfolio Summary]"
        )

        if not result['success']:
            return {
                'success': False,
                'summary': None,
                'error': result['error'],
                'usage': result.get('usage', {}),
                'model': result.get('model'),
            }

        summary = result['content']

        # Save the summary (user-specific if user_id provided)
        portfolio_context_str = f"Holdings: {len(portfolio_data.get('holdings', []))}, Total: {portfolio_data.get('total_allocation_pct', 0)}%"

        if user_id and DB_AVAILABLE:
            db_save_portfolio_summary(
                user_id=user_id,
                date_str=today,
                summary_text=summary,
                portfolio_context=portfolio_context_str
            )
        else:
            # Legacy: save to shared file
            save_portfolio_summary_entry(
                date_str=today,
                summary_text=summary,
                portfolio_context=portfolio_context_str
            )

        print(f"[Portfolio Summary] Generated portfolio summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None,
            'usage': result.get('usage', {}),
            'model': result.get('model'),
        }

    except Exception as e:
        print(f"[Portfolio Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'error': str(e)
        }


def format_portfolio_for_ai(portfolio_data):
    """Format portfolio data as a readable string for AI analysis."""
    parts = []

    # Overall summary
    parts.append("## PORTFOLIO ALLOCATIONS")
    parts.append(f"Total Holdings: {portfolio_data.get('total_holdings', 0)}")
    parts.append(f"Total Allocation: {portfolio_data.get('total_allocation_pct', 0)}%")
    parts.append(f"Allocation Valid: {'Yes' if portfolio_data.get('allocation_valid') else 'No (should be ~100%)'}")
    parts.append("")

    # Asset class breakdown
    breakdown = portfolio_data.get('asset_class_breakdown', {})
    if breakdown:
        parts.append("### Asset Class Breakdown:")
        parts.append(f"- Equities (stocks/ETFs/mutual funds): {breakdown.get('equities', 0)}%")
        parts.append(f"- Alternatives (crypto/gold): {breakdown.get('alternatives', 0)}%")
        parts.append(f"- Cash & Savings: {breakdown.get('cash', 0)}%")
        parts.append(f"- Real Estate (farmland/commercial/residential): {breakdown.get('real_estate', 0)}%")
        parts.append(f"- Other: {breakdown.get('other', 0)}%")
        parts.append("")

    # Individual holdings
    holdings = portfolio_data.get('holdings', [])
    if holdings:
        parts.append("### Individual Holdings:")
        for h in holdings:
            line = f"- {h['name']} ({h['type']}): {h['percentage']}%"
            if h.get('symbol'):
                line = f"- {h['name']} [{h['symbol']}] ({h['type']}): {h['percentage']}%"
            if h.get('current_price'):
                line += f" | Price: ${h['current_price']:,.2f}"
                if h.get('daily_change_pct') is not None:
                    change = h['daily_change_pct']
                    direction = "+" if change >= 0 else ""
                    line += f" ({direction}{change:.1f}% today)"
            parts.append(line)
        parts.append("")

    # Concentration warnings
    warnings = portfolio_data.get('concentration_warnings', [])
    if warnings:
        parts.append("### ⚠️ Concentration Warnings:")
        for w in warnings:
            parts.append(f"- {w['name']}: {w['percentage']}% - {w['warning']}")
        parts.append("")

    return "\n".join(parts)


def get_portfolio_summary_for_display():
    """
    Get the current portfolio summary formatted for display.
    Returns dict with summary info or None if not available.
    """
    summary = get_latest_portfolio_summary()
    if summary:
        return {
            'date': summary['date'],
            'generated_at': summary['generated_at'],
            'summary': summary['summary']
        }
    return None
