#!/usr/bin/env python3
"""
AI Daily Summary Generator

Generates insightful daily market summaries using AI with web search capability.
Supports both OpenAI and Anthropic APIs, configurable via environment variables.
Summaries are stored historically and provide context for continuity.
"""

import os
import json
from datetime import datetime, timedelta
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

# Anthropic effort levels: low, medium, high, max
# Higher effort = more reasoning depth, slower, more expensive
ANTHROPIC_EFFORT = os.environ.get('ANTHROPIC_EFFORT', 'medium').lower()


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


def call_ai_with_tools(client, system_prompt, user_prompt, max_tokens=600, log_prefix="[AI]", provider=None):
    """
    Make an AI API call with web search tool support.
    Supports both OpenAI and Anthropic APIs.

    Args:
        client: AI client instance (OpenAI or Anthropic)
        system_prompt: System prompt for the model
        user_prompt: User prompt with data/context
        max_tokens: Maximum tokens for completion
        log_prefix: Prefix for log messages (e.g., "[Crypto Summary]")
        provider: 'openai' or 'anthropic' (auto-detected if None)

    Returns:
        dict with 'success', 'content', and 'error' keys
    """
    # Auto-detect provider if not specified
    if provider is None:
        provider = get_ai_provider()

    print(f"{log_prefix} Using provider: {provider}")

    # DEBUG: dump full prompt to file for review
    _dump_prompt_to_file(log_prefix, provider, system_prompt, user_prompt, max_tokens)

    if provider == 'anthropic':
        return _call_anthropic_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix)
    else:
        return _call_openai_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix)


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
            api_params["tool_choice"] = tool_choice

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


def _call_anthropic_with_tools(client, system_prompt, user_prompt, max_tokens, log_prefix):
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

    # Map effort level to thinking budget
    effort_budgets = {
        'low': 1024,
        'medium': 4096,
        'high': 10000,
        'max': 32000
    }
    thinking_budget = effort_budgets.get(ANTHROPIC_EFFORT, 4096)

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
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens + thinking_budget,  # Include budget for thinking
            "system": system_prompt,
            "messages": messages,
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget
            }
        }

        if tools:
            api_params["tools"] = tools

        try:
            response = client.messages.create(**api_params)
        except Exception as e:
            # If thinking mode fails, try without it
            print(f"{log_prefix} Thinking mode failed, retrying without: {e}")
            del api_params["thinking"]
            api_params["max_tokens"] = max_tokens
            response = client.messages.create(**api_params)

        total_usage = accumulate_usage(total_usage, extract_usage_anthropic(response))
        print(f"{log_prefix} Response stop_reason: {response.stop_reason}")

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
        text_blocks = [block for block in response.content if block.type == "text"]
        content = " ".join(block.text for block in text_blocks) if text_blocks else None

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
                'model': ANTHROPIC_MODEL,
            }

        return {
            'success': True,
            'content': content.strip(),
            'error': None,
            'usage': total_usage,
            'model': ANTHROPIC_MODEL,
        }

    # Exceeded max iterations
    print(f"{log_prefix} Exceeded max iterations")
    return {
        'success': False,
        'content': None,
        'error': 'Exceeded maximum tool call iterations',
        'usage': total_usage,
        'model': ANTHROPIC_MODEL,
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


def save_summary(date_str, summary_text, web_search_used=False, news_context=None):
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
        "news_context": news_context[:500] if news_context else None  # Store snippet of news used
    })

    # Keep last 90 days of summaries
    cutoff = (datetime.now(eastern) - timedelta(days=90)).strftime('%Y-%m-%d')
    data["summaries"] = [s for s in data["summaries"] if s["date"] >= cutoff]

    save_summaries(data)


def _get_stored_news_context(topic: str | None = None) -> str | None:
    """
    Read news context from the stored news pipeline data.

    If topic is None, returns the cross-market summary string (for daily briefing).
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
        return f"## Today's {topic_label} News\n{topic_summary}"

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
        return "Market conditions data is currently unavailable. Check back later for the daily briefing."

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


def generate_daily_summary(market_data_summary, top_movers, top_movers_1d=None):
    """
    Generate the daily AI summary.

    Args:
        market_data_summary: String output from generate_market_summary()
        top_movers: List of top 5-day movers from calculate_top_movers(period=5)
        top_movers_1d: List of top 1-day movers from calculate_top_movers(period=1)

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        # AI unavailable — generate rule-based fallback (US-325.1)
        print("[AI Summary] AI client unavailable, generating rule-based fallback briefing")
        conditions = get_market_conditions()
        fallback = _generate_fallback_briefing(conditions)
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')
        save_summary(date_str=today, summary_text=fallback, web_search_used=False, news_context=None)
        return {
            'success': True,
            'summary': fallback,
            'error': None
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        # Get previous summaries for context (14 days for foresight callbacks)
        recent_summaries = get_recent_summaries(days=14)
        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS BRIEFINGS (14 days — reference your earlier predictions when relevant, but don't repeat themes):\n"
            for s in recent_summaries:
                if s["date"] != today:  # Don't include today if regenerating
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Read news context from stored pipeline data
        news_context = _get_stored_news_context()
        news_section = ""
        if news_context:
            news_section = f"\n\n## TODAY'S NEWS CONTEXT:\n{news_context}"

        # Get latest specific market briefings to use as context
        specific_briefings_context = ""
        briefings_found = []

        crypto_summary = get_latest_crypto_summary()
        if crypto_summary and crypto_summary.get("date") == today:
            briefings_found.append(f"### Crypto/Bitcoin Briefing:\n{crypto_summary['summary']}")

        equity_summary = get_latest_equity_summary()
        if equity_summary and equity_summary.get("date") == today:
            briefings_found.append(f"### Equity Markets Briefing:\n{equity_summary['summary']}")

        rates_summary = get_latest_rates_summary()
        if rates_summary and rates_summary.get("date") == today:
            briefings_found.append(f"### Rates & Yield Curve Briefing:\n{rates_summary['summary']}")

        dollar_summary = get_latest_dollar_summary()
        if dollar_summary and dollar_summary.get("date") == today:
            briefings_found.append(f"### Dollar & Currency Briefing:\n{dollar_summary['summary']}")

        credit_summary = get_latest_credit_summary()
        if credit_summary and credit_summary.get("date") == today:
            briefings_found.append(f"### Credit Markets Briefing:\n{credit_summary['summary']}")

        if briefings_found:
            specific_briefings_context = "\n\n## TODAY'S SPECIFIC MARKET BRIEFINGS (synthesize these into your narrative):\n" + "\n\n".join(briefings_found)

        # Format top movers — two sections: 1-day and 5-day (US-325.3)
        movers_text = ""
        if top_movers_1d:
            movers_text += "\n\n## TODAY'S BIGGEST MOVES (1-day, by z-score, most unusual):\n"
            for m in top_movers_1d[:5]:
                direction = "up" if m['change'] > 0 else "down"
                movers_text += f"- {m['name']}: {m['change']:+.1f}{m['unit']} ({direction}, z-score: {m['z_score']:.1f})\n"
        if top_movers:
            movers_text += "\n\n## MOST UNUSUAL 5-DAY MOVES (by z-score):\n"
            for m in top_movers[:5]:
                direction = "up" if m['change'] > 0 else "down"
                movers_text += f"- {m['name']}: {m['change']:+.1f}{m['unit']} ({direction}, z-score: {m['z_score']:.1f})\n"

        # Build market conditions context (US-325.1)
        conditions = get_market_conditions()
        conditions_context = _build_conditions_context(conditions)

        # Build 90-day conditions history context
        history = get_conditions_history()
        conditions_history = _build_conditions_history_context(history, days=90)

        # The system prompt - quadrant-led three-paragraph narrative (US-325.1)
        system_prompt = """You are a brilliant, trusted financial commentator - think of yourself as the host of the most valuable daily financial briefing that exists. Your audience is individual investors who are smart but busy. They could only pick ONE source of financial insight each day, and they've chosen you because:

1. You don't just report numbers - you CONNECT THE DOTS and tell the STORY of what's happening
2. You have a gift for explaining complex market dynamics in clear, engaging language
3. You see patterns others miss and aren't afraid to point out when something is historically unusual
4. You're honest about uncertainty but confident in your analysis
5. You make people SMARTER about markets, not just informed

Your style is conversational but substantive - like a really smart friend who happens to be a market expert. You're not stuffy or formal, but you're also not dumbed down. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3 paragraphs (200-250 words total)
- First paragraph: WHAT'S HAPPENING — Open by naming the current macro quadrant and what it means. Describe today's conditions across the four dimensions (quadrant, liquidity, risk, policy). Ground the reader in where we are.
- Second paragraph: WHAT'S CHANGING — Use the conditions history to identify trends, transitions, and boundary proximity. How long have we been here? Are we drifting toward a different quadrant? Reference your own earlier predictions when relevant ("I noted X last week, and today...").
- Third paragraph: WHAT TO DO — Portfolio implications. Connect conditions to asset class expectations. What should investors watch? End forward-looking.
- DO NOT just list data points or metrics - ANALYZE and INTERPRET
- DO NOT repeat themes from your previous briefings - find fresh angles
- Use only the four quadrants: Goldilocks, Reflation, Stagflation, Deflation Risk
- If specific market briefings (crypto, equity, rates, dollar) are provided, SYNTHESIZE key insights from them into a cohesive narrative rather than summarizing each separately
- If something is at an extreme percentile or historically unusual, HIGHLIGHT it with context
- Reference specific numbers sparingly but meaningfully
- If news events are driving markets, weave them into the narrative

You have access to a web search tool if you need to look up additional context about specific events, companies, or breaking news mentioned in the data. Use it when helpful to provide better context.

You're writing the one thing someone reads about markets today. Make it count."""

        # The user prompt with conditions context
        user_prompt = f"""Today is {today}. Generate today's market briefing.

{conditions_context}
{conditions_history}

{market_data_summary}
{movers_text}
{news_section}
{specific_briefings_context}
{previous_context}

Remember: 3 paragraphs (what's happening, what's changing, what to do), tell the story, don't repeat previous themes, make it the most valuable 30 seconds of their day."""

        # Make the API call with web search tool support
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=800,
            log_prefix="[AI Summary]"
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
            return {
                'success': True,
                'summary': fallback,
                'error': None
            }

        summary = result['content']

        # Save the summary
        save_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context
        )

        print(f"[AI Summary] Generated daily summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None
        }

    except Exception as e:
        print(f"[AI Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
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
            'web_search_used': summary.get('web_search_used', False)
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


def save_crypto_summary(date_str, summary_text, web_search_used=False, news_context=None):
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
        "news_context": news_context[:500] if news_context else None
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
    """
    Generate the daily Crypto/Bitcoin AI summary.

    Args:
        crypto_data_summary: String with crypto market data from generate_crypto_market_summary()

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        return {
            'success': False,
            'summary': None,
            'error': error
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        # Get previous crypto summaries for context
        recent_summaries = get_recent_crypto_summaries(days=14)
        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS CRYPTO SUMMARIES (for continuity - don't repeat these):\n"
            for s in recent_summaries:
                if s["date"] != today:
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Read crypto news context from stored pipeline data
        news_context = _get_stored_news_context(topic='crypto')
        news_section = ""
        if news_context:
            news_section = f"\n\n## TODAY'S CRYPTO NEWS:\n{news_context}"

        # Crypto-specific system prompt
        system_prompt = """You are a Bitcoin/crypto market analyst providing daily briefings for the Crypto page of a financial dashboard. Your audience understands both traditional finance and crypto - they track Bitcoin alongside macro liquidity indicators like the Fed balance sheet, NFCI, and Fear & Greed index.

Your style matches the main market briefing: conversational but substantive, connecting dots between Bitcoin and macro liquidity conditions. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3 paragraphs (250-350 words total)
- First paragraph: The Bitcoin/crypto story TODAY - price action, sentiment shifts, and what's driving it. Include specific price levels and percentage moves.
- Second paragraph: The macro liquidity context - how Fed policy, financial conditions (NFCI), money supply (M2), and dollar strength relate to Bitcoin's current setup. Explain the connection clearly.
- Third paragraph: The actionable takeaway - key support/resistance levels to watch, what would change the thesis, and how to think about positioning given current sentiment extremes.
- Reference specific numbers (Fear & Greed level, Fed balance sheet trend, BTC price levels) meaningfully
- If Fear & Greed is extreme (<25 or >75), explain the historical contrarian implications in detail
- Connect Bitcoin to liquidity trends with clear cause-and-effect reasoning
- DO NOT repeat themes from previous summaries
- Be specific about what price levels matter and why

You have access to a web search tool if you need to look up additional context about specific crypto events, regulatory news, or macro developments. Use it when helpful to provide better context.

Remember: Bitcoin is a liquidity-sensitive asset. Your job is to explain HOW the macro setup affects BTC, what levels matter, and give readers a clear framework for thinking about it."""

        user_prompt = f"""Today is {today}. Generate today's crypto/Bitcoin briefing.

{crypto_data_summary}
{news_section}
{previous_context}

Remember: 3 paragraphs, connect BTC to liquidity conditions, be specific about key levels, make it actionable."""

        # Make the API call with web search tool support
        print(f"[Crypto Summary] Calling OpenAI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=900,
            log_prefix="[Crypto Summary]"
        )

        if not result['success']:
            return {
                'success': False,
                'summary': None,
                'error': result['error']
            }

        summary = result['content']

        # Save the summary
        save_crypto_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context
        )

        print(f"[Crypto Summary] Generated crypto summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None
        }

    except Exception as e:
        print(f"[Crypto Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'error': str(e)
        }


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
            'web_search_used': summary.get('web_search_used', False)
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


def save_equity_summary(date_str, summary_text, web_search_used=False, news_context=None):
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
        "news_context": news_context[:500] if news_context else None
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
    """
    Generate the daily Equity Markets AI summary.

    Args:
        equity_data_summary: String with equity market data from generate_equity_market_summary()

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        return {
            'success': False,
            'summary': None,
            'error': error
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        # Get previous equity summaries for context
        recent_summaries = get_recent_equity_summaries(days=14)
        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS EQUITY SUMMARIES (for continuity - don't repeat these):\n"
            for s in recent_summaries:
                if s["date"] != today:
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Read equity news context from stored pipeline data
        news_context = _get_stored_news_context(topic='equity')
        news_section = ""
        if news_context:
            news_section = f"\n\n## TODAY'S EQUITY NEWS:\n{news_context}"

        # Equity-specific system prompt
        system_prompt = """You are an equity market analyst providing daily briefings for the Equity Markets page of a financial dashboard. Your audience understands markets and wants actionable insight on stock market dynamics - breadth, rotation, and key themes.

Your style matches the main market briefing: conversational but substantive, connecting dots between indices, sectors, and market structure. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3 paragraphs (250-350 words total)
- First paragraph: The equity market story TODAY - what's leading (sectors, styles), what's lagging, and what the price action tells us. Include specific index levels and percentage moves.
- Second paragraph: The structural picture - market breadth, concentration risk, and what the ratios are saying. Explain WHY narrow breadth or extreme concentration matters in plain terms.
- Third paragraph: Style and size rotation signals - growth vs value, small vs large cap dynamics. What are these telling us about economic expectations and risk appetite? End with specific things to watch.
- Reference specific numbers (index levels, percentile rankings, ratio changes) meaningfully
- Highlight any EXTREME readings (>95th or <5th percentile) and explain the historical context
- Connect small cap vs large cap performance to economic expectations with clear reasoning
- If growth/value rotation is notable, explain what it signals for the broader market
- DO NOT repeat themes from previous summaries

You have access to a web search tool if you need to look up additional context about specific companies, earnings, sector news, or market events. Use it when helpful to provide better context.

Remember: Your job is to help investors understand WHAT is happening under the surface of the market, not just whether it's up or down. Give them a mental model for thinking about market structure."""

        user_prompt = f"""Today is {today}. Generate today's equity markets briefing.

{equity_data_summary}
{news_section}
{previous_context}

Remember: 3 paragraphs, tell the story of market structure and rotation, explain why extremes matter, make it actionable."""

        # Make the API call with web search tool support
        print(f"[Equity Summary] Calling OpenAI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=900,
            log_prefix="[Equity Summary]"
        )

        if not result['success']:
            return {
                'success': False,
                'summary': None,
                'error': result['error']
            }

        summary = result['content']

        # Save the summary
        save_equity_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context
        )

        print(f"[Equity Summary] Generated equity summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None
        }

    except Exception as e:
        print(f"[Equity Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'error': str(e)
        }


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
            'web_search_used': summary.get('web_search_used', False)
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


def save_rates_summary(date_str, summary_text, web_search_used=False, news_context=None):
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
        "news_context": news_context[:500] if news_context else None
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
    """
    Generate the daily Rates & Yield Curve AI summary.

    Args:
        rates_data_summary: String with rates data from generate_rates_market_summary()

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        return {
            'success': False,
            'summary': None,
            'error': error
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        # Get previous rates summaries for context
        recent_summaries = get_recent_rates_summaries(days=14)
        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS RATES SUMMARIES (for continuity - don't repeat these):\n"
            for s in recent_summaries:
                if s["date"] != today:
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Read rates news context from stored pipeline data
        news_context = _get_stored_news_context(topic='rates')
        news_section = ""
        if news_context:
            news_section = f"\n\n## TODAY'S RATES NEWS:\n{news_context}"

        # Rates-specific system prompt
        system_prompt = """You are a fixed income analyst providing daily briefings for the Rates & Yield Curve page of a financial dashboard. Your audience understands markets and wants actionable insight on interest rates, the yield curve, and their implications.

Your style matches the main market briefing: conversational but substantive, connecting dots between rates, the curve, inflation expectations, and what they mean for other assets. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3 paragraphs (250-350 words total)
- First paragraph: The rates story TODAY - what's moving in yields and spreads, why (economic data, Fed expectations, supply/demand), and what it signals. Include specific yield levels and basis point changes.
- Second paragraph: The yield curve and inflation picture - explain the curve shape (inverted/flat/steep) and what it historically signals. Connect real yields to Fed policy stance and breakeven inflation to market inflation expectations. Use plain language to explain the implications.
- Third paragraph: The "so what" for investors - how do current rates affect equity valuations, credit, and duration positioning? What specific levels would change the outlook? End with actionable guidance.
- Reference specific numbers (yields, spreads in basis points, percentile rankings) meaningfully
- Highlight any EXTREME readings (>95th or <5th percentile) or key threshold crossings with historical context
- If the yield curve is inverted or recently un-inverted, explain the recession implications clearly
- Connect real yields to Fed policy stance (restrictive/accommodative) with clear reasoning
- DO NOT repeat themes from previous summaries

You have access to a web search tool if you need to look up additional context about Fed policy, economic data releases, or bond market developments. Use it when helpful to provide better context.

Remember: Your job is to help investors understand what rates are telling us about the economy and what it means for their portfolios. Make the fixed income world accessible."""

        user_prompt = f"""Today is {today}. Generate today's rates & yield curve briefing.

{rates_data_summary}
{news_section}
{previous_context}

Remember: 3 paragraphs, tell the rates story clearly, explain curve signals, make it actionable for investors."""

        # Make the API call with web search tool support
        print(f"[Rates Summary] Calling OpenAI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=900,
            log_prefix="[Rates Summary]"
        )

        if not result['success']:
            return {
                'success': False,
                'summary': None,
                'error': result['error']
            }

        summary = result['content']

        # Save the summary
        save_rates_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context
        )

        print(f"[Rates Summary] Generated rates summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None
        }

    except Exception as e:
        print(f"[Rates Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'error': str(e)
        }


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
            'web_search_used': summary.get('web_search_used', False)
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


def save_dollar_summary(date_str, summary_text, web_search_used=False, news_context=None):
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
        "news_context": news_context[:500] if news_context else None
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
    """
    Generate the daily Dollar & Currency AI summary.

    Args:
        dollar_data_summary: String with dollar data from generate_dollar_market_summary()

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        return {
            'success': False,
            'summary': None,
            'error': error
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        # Get previous dollar summaries for context
        recent_summaries = get_recent_dollar_summaries(days=14)
        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS DOLLAR SUMMARIES (for continuity - don't repeat these):\n"
            for s in recent_summaries:
                if s["date"] != today:
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Read dollar news context from stored pipeline data
        news_context = _get_stored_news_context(topic='dollar')
        news_section = ""
        if news_context:
            news_section = f"\n\n## TODAY'S DOLLAR NEWS:\n{news_context}"

        # Dollar-specific system prompt
        system_prompt = """You are a currency analyst providing daily briefings for the Dollar & Currency page of a financial dashboard. Your audience understands markets and wants actionable insight on the US dollar, currency dynamics, and their cross-asset implications.

Your style matches the main market briefing: conversational but substantive, connecting dots between the dollar, Fed policy, global central bank divergence, and implications for other assets. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3 paragraphs (250-350 words total)
- First paragraph: The dollar story TODAY - what's moving DXY and key pairs (especially USD/JPY), why (Fed expectations, economic data, central bank divergence), and what it signals. Include specific levels and percentage moves.
- Second paragraph: The macro context - explain where we are in the "Dollar Smile" (risk-off strength vs growth/yield strength vs weak middle), what carry trade dynamics are telling us, and how central bank policy divergence is driving flows. Connect to rate differentials clearly.
- Third paragraph: The cross-asset implications - how does dollar strength/weakness affect emerging markets, commodities, multinational earnings, and positioning? What specific DXY levels would change the outlook? End with actionable guidance.
- Reference specific numbers (DXY level, USD/JPY, percentile rankings) meaningfully
- Highlight any EXTREME readings (>95th or <5th percentile) or key threshold crossings with historical context
- If USD/JPY is at notable levels, explain the carry trade implications and BOJ policy dynamics
- Connect dollar moves to Fed policy expectations clearly
- DO NOT repeat themes from previous summaries

You have access to a web search tool if you need to look up additional context about Fed policy, central bank decisions, or currency market developments. Use it when helpful to provide better context.

Remember: Your job is to help investors understand what the dollar is telling us about global risk appetite and policy divergence, and what it means for their portfolios."""

        user_prompt = f"""Today is {today}. Generate today's dollar & currency briefing.

{dollar_data_summary}
{news_section}
{previous_context}

Remember: 3 paragraphs, tell the dollar story clearly, explain the Dollar Smile and carry dynamics, make it actionable for investors."""

        # Make the API call with web search tool support
        print(f"[Dollar Summary] Calling OpenAI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=900,
            log_prefix="[Dollar Summary]"
        )

        if not result['success']:
            return {
                'success': False,
                'summary': None,
                'error': result['error']
            }

        summary = result['content']

        # Save the summary
        save_dollar_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context
        )

        print(f"[Dollar Summary] Generated dollar summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None
        }

    except Exception as e:
        print(f"[Dollar Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'error': str(e)
        }


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
            'web_search_used': summary.get('web_search_used', False)
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


def save_credit_summary(date_str, summary_text, web_search_used=False, news_context=None):
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
        "news_context": news_context[:500] if news_context else None
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
    """
    Generate the daily Credit Markets AI summary.

    Args:
        credit_data_summary: String with credit market data from generate_credit_market_summary()

    Returns:
        dict with 'success', 'summary', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        return {
            'success': False,
            'summary': None,
            'error': error
        }

    try:
        eastern = pytz.timezone('US/Eastern')
        today = datetime.now(eastern).strftime('%Y-%m-%d')

        recent_summaries = get_recent_credit_summaries(days=14)
        previous_context = ""
        if recent_summaries:
            previous_context = "\n\n## YOUR PREVIOUS CREDIT SUMMARIES (for continuity - don't repeat these):\n"
            for s in recent_summaries:
                if s["date"] != today:
                    previous_context += f"\n### {s['date']}:\n{s['summary']}\n"

        # Read credit news context from stored pipeline data
        news_context = _get_stored_news_context(topic='credit')
        news_section = ""
        if news_context:
            news_section = f"\n\n## TODAY'S CREDIT NEWS:\n{news_context}"

        system_prompt = """You are a credit market analyst providing daily briefings for the Credit Markets page of a financial dashboard. Your audience understands markets and wants actionable insight on corporate credit conditions, spread dynamics, and what credit is signaling about the economy.

Your style matches the main market briefing: conversational but substantive, connecting dots between spread levels, market conditions, default risk, and cross-asset implications. Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., "near historic highs," "the largest move in months").

CRITICAL RULES:
- Write EXACTLY 3 paragraphs (250-350 words total)
- First paragraph: The credit story TODAY - where HY and IG spreads are, whether they're tight/normal/wide versus history (percentile), and what the trend has been. Include specific spread levels in basis points.
- Second paragraph: The macro interpretation - what current spread levels imply about default risk, the economic cycle, and risk appetite. Connect to the current market conditions quadrant and explain whether credit conditions are confirming or diverging from equity market signals.
- Third paragraph: What to watch - key spread thresholds that would change the outlook, CCC vs HY dynamics (distress concentration), and the main risk scenarios (spread widening triggers, liquidity conditions). End with specific actionable guidance.
- Reference specific numbers (spread levels in bps, percentile rankings) meaningfully
- Highlight any EXTREME readings (>95th or <5th percentile) with historical context
- If HY is at notably tight levels, explain the complacency risk and what could trigger widening
- Connect credit spread moves to the broader economic outlook clearly
- DO NOT repeat themes from previous summaries

You have access to a web search tool if you need to look up additional context about credit market conditions, major bond issuances, or corporate default news. Use it when helpful to provide better context.

Remember: Your job is to help investors understand what the credit market is pricing in about economic health, default risk, and financial conditions — and what that means for portfolio positioning."""

        user_prompt = f"""Today is {today}. Generate today's credit markets briefing.

{credit_data_summary}
{news_section}
{previous_context}

Remember: 3 paragraphs, tell the credit story clearly, explain what spread levels mean for economic outlook and risk, make it actionable for investors."""

        print(f"[Credit Summary] Calling OpenAI with {len(user_prompt)} chars of input...")
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=900,
            log_prefix="[Credit Summary]"
        )

        if not result['success']:
            return {
                'success': False,
                'summary': None,
                'error': result['error']
            }

        summary = result['content']

        save_credit_summary(
            date_str=today,
            summary_text=summary,
            web_search_used=bool(news_context),
            news_context=news_context
        )

        print(f"[Credit Summary] Generated credit summary for {today}")
        return {
            'success': True,
            'summary': summary,
            'error': None
        }

    except Exception as e:
        print(f"[Credit Summary] Error generating summary: {e}")
        return {
            'success': False,
            'summary': None,
            'error': str(e)
        }


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
            'web_search_used': summary.get('web_search_used', False)
        }
    return None


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
            max_tokens=1000,
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
