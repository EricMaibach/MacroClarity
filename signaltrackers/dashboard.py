#!/usr/bin/env python3
"""
Market Divergence Dashboard
A comprehensive web dashboard for tracking the historic market divergence.
"""

from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, abort
from flask_login import login_user, logout_user, login_required, current_user
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta, date as _date
import hmac
import json
import subprocess
import threading
import os
import atexit
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from web_search import SEARCH_FUNCTION_DEFINITION, execute_search_function, is_tavily_configured
from ai_summary import (
    generate_daily_summary, get_summary_for_display, get_latest_summary,
    generate_crypto_summary, get_crypto_summary_for_display,
    generate_equity_summary, get_equity_summary_for_display,
    generate_rates_summary, get_rates_summary_for_display,
    generate_dollar_summary, get_dollar_summary_for_display,
    generate_credit_summary, get_credit_summary_for_display,
    get_ai_client, call_ai_with_tools,
    _build_conditions_context, _build_conditions_history_context
)
from metric_tools import (
    LIST_METRICS_FUNCTION,
    GET_METRIC_FUNCTION,
    execute_metric_function
)
from portfolio import (
    load_portfolio, save_portfolio, add_allocation, update_allocation,
    delete_allocation, validate_allocation_total, validate_symbol,
    get_portfolio_with_prices, get_portfolio_summary_for_ai,
    # Database-backed functions for multi-user mode
    db_load_portfolio, db_add_allocation, db_update_allocation,
    db_delete_allocation, db_validate_allocation_total,
    db_get_portfolio_with_prices, db_get_portfolio_summary_for_ai
)
from config import get_config
from extensions import init_extensions, db, limiter, csrf
from models import User
from scheduler import init_scheduler as init_apscheduler, shutdown_scheduler
from recession_probability import get_recession_probability, update_recession_probability
from sector_tone_pipeline import get_sector_management_tone, update_sector_management_tone
from credit_interpretation_config import get_credit_interpretation
from trade_interpretation_config import get_trade_interpretation
from conditions_config import (
    CATEGORY_CONDITIONS_CONTEXT,
    SIGNAL_CONDITIONS_ANNOTATIONS,
    get_category_conditions_context,
    get_simplified_liquidity,
)
from property_interpretation_config import get_property_interpretation
from market_conditions import update_market_conditions_cache, get_market_conditions, get_conditions_history, build_implications_matrix
from services.rate_limiting import anonymous_rate_limit, CATEGORY_CHATBOT, CATEGORY_ANALYSIS
from billing import init_stripe, is_stripe_configured, get_webhook_secret


app = Flask(__name__)

# Load configuration
app.config.from_object(get_config())

# Initialize extensions (database, login manager, CSRF, rate limiting)
init_extensions(app)

# Initialize Stripe billing (gracefully skipped when env vars are absent)
init_stripe(app)

# Initialize OpenAI client (only if API key is available)
openai_api_key = os.environ.get('OPENAI_API_KEY')
openai_client = OpenAI(api_key=openai_api_key) if openai_api_key else None

# Track data reload status
reload_status = {
    'in_progress': False,
    'last_reload': None,
    'error': None,
    'status': None,
    'success': None
}

DATA_DIR = Path("data")

# Background scheduler for automatic data refresh
scheduler = None


# Template filters
@app.template_filter('format_datetime')
def format_datetime(dt):
    """Format datetime for display."""
    if not dt:
        return 'N/A'

    now = datetime.utcnow()
    diff = now - dt

    if diff < timedelta(minutes=1):
        return 'Just now'
    elif diff < timedelta(hours=1):
        mins = int(diff.total_seconds() / 60)
        return f'{mins} minute{"s" if mins > 1 else ""} ago'
    elif diff < timedelta(days=1):
        hours = int(diff.total_seconds() / 3600)
        return f'{hours} hour{"s" if hours > 1 else ""} ago'
    elif diff < timedelta(days=7):
        days = diff.days
        return f'{days} day{"s" if days > 1 else ""} ago'
    else:
        return dt.strftime('%b %d, %Y at %I:%M %p')


@app.template_filter('format_number')
def format_number(value):
    """Format number for display."""
    if value is None:
        return 'N/A'

    if isinstance(value, float):
        if 0 <= abs(value) <= 1:
            return f'{value * 100:.2f}%'
        elif abs(value) > 100:
            return f'{value:,.1f}'
        else:
            return f'{value:.2f}'

    return str(value)


# Context processors
@app.context_processor
def inject_unread_alerts():
    """Inject unread alert count into all templates."""
    # Handle case where current_user doesn't exist (background jobs, email rendering)
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            from models.alert import Alert
            count = Alert.query.filter_by(
                user_id=current_user.id,
                read=False
            ).count()
            return {'unread_alert_count': count}
    except (AttributeError, RuntimeError):
        # current_user doesn't exist in this context (background jobs)
        pass

    return {'unread_alert_count': 0}


@app.context_processor
def inject_conditions_context():
    """Inject conditions annotations and context into all templates."""
    return {
        'signal_conditions_annotations': SIGNAL_CONDITIONS_ANNOTATIONS,
        'category_conditions_context': CATEGORY_CONDITIONS_CONTEXT,
    }


@app.context_processor
def inject_recession_probability():
    """Inject recession probability data into all templates (US-146.1)."""
    try:
        data = get_recession_probability()
    except Exception:
        data = None
    return {'recession_probability': data}


@app.context_processor
def inject_market_conditions():
    """Inject market conditions data into all templates for conditions strip (US-322.1)."""
    try:
        data = get_market_conditions()
        if data:
            # Check staleness (>48 hours)
            updated_at = data.get('updated_at', '')
            if updated_at:
                from datetime import timezone
                updated_dt = datetime.fromisoformat(updated_at)
                age = datetime.now(timezone.utc) - updated_dt
                if age.total_seconds() > 48 * 3600:
                    data = None
    except Exception:
        data = None
    return {'market_conditions': data}



@app.context_processor
def inject_sector_management_tone():
    """Inject sector management tone data into all templates (US-123.1).

    Returns a dict with data_available=True (pipeline ran) or
    data_available=False (pipeline has not yet completed a full quarter's run).
    Always returns the sector_management_tone key so templates can check
    data_available without an outer None guard.
    """
    try:
        data = get_sector_management_tone()
        if data is None:
            # Pipeline has not yet completed a full quarter's run
            from datetime import datetime as _dt
            now = _dt.utcnow()
            q_map = {1: 'Q1', 2: 'Q1', 3: 'Q1', 4: 'Q2', 5: 'Q2', 6: 'Q2',
                     7: 'Q3', 8: 'Q3', 9: 'Q3', 10: 'Q4', 11: 'Q4', 12: 'Q4'}
            data = {
                'quarter': q_map[now.month],
                'year': now.year,
                'data_available': False,
                'sectors': [],
            }
    except Exception:
        data = {'quarter': '', 'year': 0, 'data_available': False, 'sectors': []}
    return {'sector_management_tone': data}


def init_scheduler():
    """Initialize background scheduler for automatic data refresh and alerts."""
    global scheduler

    # Initialize the APScheduler with all configured jobs (alerts, emails, etc.)
    scheduler = init_apscheduler(app)

    # Add the data refresh job to the scheduler
    eastern = pytz.timezone('US/Eastern')
    scheduler.add_job(
        scheduled_data_refresh,
        CronTrigger(hour=17, minute=30, day_of_week='mon-fri', timezone=eastern),
        id='daily_refresh',
        replace_existing=True,
        name='Daily Data Refresh'
    )

    print(f"Scheduler started. Next refresh: {scheduler.get_job('daily_refresh').next_run_time}")

    # Ensure scheduler shuts down on exit
    atexit.register(shutdown_scheduler)

    return scheduler


def scheduled_data_refresh():
    """Background job to refresh all data and AI briefings."""
    print(f"[{datetime.now()}] Starting scheduled data refresh...")

    # Use the existing run_data_collection function
    # which handles all data collection and AI summary generation
    run_data_collection()

    print(f"[{datetime.now()}] Scheduled data refresh completed.")


def get_scheduler_status():
    """Get current scheduler status for API endpoint."""
    if scheduler is None:
        return {
            'enabled': False,
            'message': 'Scheduler not initialized'
        }

    job = scheduler.get_job('daily_refresh')
    if job:
        return {
            'enabled': True,
            'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
            'job_name': job.name,
            'schedule': 'Daily at 5:30 PM Eastern (Mon-Fri)'
        }

    return {
        'enabled': True,
        'next_run': None,
        'message': 'No scheduled job found'
    }


# Metric descriptions for educational purposes
METRIC_DESCRIPTIONS = {
    'divergence_gap': {
        'what': 'The gap between what gold prices are "saying" credit spreads should be versus what they actually are. Calculated as: Gold-Implied Spread minus Actual HY Spread.',
        'why': 'A massive divergence indicates one of these markets is catastrophically wrong. When safe havens (gold) are pricing extreme crisis while credit markets remain calm, a major dislocation is likely.',
        'watch': 'Gap >900 bp is unprecedented. Watch for: (1) Gap widening = crisis building, (2) Gap narrowing via credit widening = crisis arriving, (3) Gap narrowing via gold falling = false alarm resolving.'
    },
    'high_yield_spread': {
        'what': 'The yield premium investors demand to hold junk bonds (BB-rated or below) versus Treasury bonds. Measured in basis points (100 bp = 1%).',
        'why': 'Primary gauge of credit market stress. When spreads are tight (<300 bp), markets see low default risk. When spreads blow out (>500 bp), credit crisis is underway.',
        'watch': 'Current: 276 bp (7th percentile - extremely tight). Key levels: >300 bp = early warning, >400 bp = deteriorating, >500 bp = crisis mode, >800 bp = severe crisis.'
    },
    'investment_grade_spread': {
        'what': 'Yield premium on investment-grade corporate bonds (BBB to AAA-rated) versus Treasuries. Lower risk than high-yield.',
        'why': 'Reflects stress in higher-quality corporate credit. Tends to lead high-yield spreads by a few weeks when widening.',
        'watch': 'Current: 79 bp (extremely tight). Key levels: >100 bp = caution, >150 bp = stress building, >200 bp = crisis.'
    },
    'ccc_spread': {
        'what': 'Spread on CCC-rated bonds - the most distressed segment of the high-yield market (one step above default).',
        'why': 'Leading indicator of default risk. CCC/HY ratio shows if distressed credits are lagging or leading the broader market.',
        'watch': 'CCC/HY ratio: 3.14x (normal range: 2.5-4x). Rising ratio = distressed credits under pressure first. Falling ratio = broad HY catching down.'
    },
    'gold_price': {
        'what': 'Price of gold per troy ounce (tracking via GLD ETF). Primary safe-haven asset.',
        'why': 'Gold rises when investors fear currency debasement, geopolitical risk, or structural economic crisis. At $4,145 (89th percentile), gold is pricing extreme crisis.',
        'watch': 'Key levels: >$4,000 = crisis signal, >$4,500 = severe crisis, >$5,000 = panic. Below $4,000 = fears moderating.'
    },
    'bitcoin_price': {
        'what': 'Price of Bitcoin in USD. Risk asset and liquidity gauge.',
        'why': 'Bitcoin crashed 24% from $120k peak 3 months ago - major liquidity warning. Acts as early warning for risk asset stress.',
        'watch': 'Peak: $120k, Current: ~$91k. Further crashes = liquidity crisis deepening. Recovery toward $100k+ = risk appetite returning.'
    },
    'vix_price': {
        'what': 'CBOE Volatility Index - measures expected S&P 500 volatility over next 30 days. Known as the "fear gauge".',
        'why': 'Low VIX (14-15) despite other warning signs = dangerous complacency. VIX spike typically precedes or coincides with equity selloffs.',
        'watch': 'Current: 14.49 (low). Key levels: >20 = fear rising, >25 = significant stress, >30 = crisis mode, >40 = panic.'
    },
    'sp500_price': {
        'what': 'S&P 500 index tracking 500 largest US stocks (via SPY ETF).',
        'why': 'Benchmark for US equity market. Currently near all-time highs while credit spreads are tight and gold is screaming - unprecedented divergence.',
        'watch': 'Watch for: 5-10% drawdown = early weakness, 15-20% = correction, >20% = bear market.'
    },
    'market_breadth_ratio': {
        'what': 'Equal-weight S&P 500 (RSP) divided by market-cap-weight S&P 500 (SPY). Measures market concentration.',
        'why': 'Low ratio = narrow market rally driven by few mega-cap stocks. High concentration = fragile market vulnerable to rotation.',
        'watch': 'Current: Very narrow rally. Ratio falling = concentration increasing (risk). Ratio rising = broadening (healthier).'
    },
    'nasdaq_price': {
        'what': 'Nasdaq 100 index - tech-heavy index of 100 largest non-financial stocks (via QQQ ETF).',
        'why': 'Heavily weighted to mega-cap tech (Mag 7). Performance gap vs S&P 500 shows tech leadership or stress.',
        'watch': 'Outperformance = tech leadership continues. Underperformance = rotation or tech stress.'
    },
    'small_cap_price': {
        'what': 'Russell 2000 small-cap index (via IWM ETF).',
        'why': 'Small caps typically underperform in late cycle or when credit conditions tighten. Leading indicator of economic stress.',
        'watch': 'Underperformance vs large caps = economic stress signal. Outperformance = broadening rally.'
    },
    'leveraged_loan_price': {
        'what': 'Price of leveraged loan ETF (BKLN) - floating-rate senior secured loans to sub-investment grade companies.',
        'why': 'Senior in capital structure, so less risky than high-yield bonds. Price stability suggests loan market is calm despite warnings.',
        'watch': 'Price stability = loan market comfortable. Sharp decline = credit stress spreading to senior secured debt.'
    },
    'high_yield_credit_price': {
        'what': 'Price of high-yield bond ETF (HYG) - junk bond market proxy.',
        'why': 'Price reflects both spread changes and Treasury moves. Falling price = credit stress or rising rates.',
        'watch': 'Stable/rising price = credit markets calm. Sharp decline = spreads widening or credit stress.'
    },
    'investment_grade_credit_price': {
        'what': 'Price of investment-grade corporate bond ETF (LQD).',
        'why': 'Higher quality than HYG. Price decline = either IG spreads widening or Treasury yields rising.',
        'watch': 'Watch for divergence from HYG. LQD declining while HYG stable = quality flight.'
    },
    'treasury_7_10yr_price': {
        'what': 'Price of 7-10 year Treasury ETF (IEF) - medium-term US government bonds.',
        'why': 'Safe haven during equity stress. Price rises (yields fall) when investors flee to safety.',
        'watch': 'Rising price = safety bid. Falling price = rising yields or inflation fears.'
    },
    'treasury_20yr_price': {
        'what': 'Price of 20+ year Treasury ETF (TLT) - long-term US government bonds.',
        'why': 'Most sensitive to interest rate changes. Large price moves signal major shifts in rate expectations.',
        'watch': 'Rising sharply = flight to safety or recession fears. Falling = inflation concerns or Fed hawkishness.'
    },
    'treasury_short_price': {
        'what': 'Price of short-term Treasury ETF (SHY) - 1-3 year US government bonds.',
        'why': 'Anchored to Fed policy. Minimal volatility. Price reflects front-end rate expectations.',
        'watch': 'Relatively stable. Large moves = major Fed policy shift.'
    },
    'tips_inflation_price': {
        'what': 'Treasury Inflation-Protected Securities (TIP) - bonds that adjust for inflation.',
        'why': 'Real yield gauge. TIPS outperform when inflation expectations rise.',
        'watch': 'Outperforming nominal Treasuries = inflation concerns. Underperforming = deflation fears.'
    },
    'gold_miners_price': {
        'what': 'Gold mining stocks ETF (GDX) - levered play on gold prices.',
        'why': 'Typically outperform physical gold in bull markets due to operating leverage. Underperformance = margin pressure.',
        'watch': 'Outperforming gold = strong gold bull market. Lagging gold = margin or execution concerns.'
    },
    'silver_price': {
        'what': 'Silver price (via SLV ETF) - both industrial metal and monetary metal.',
        'why': 'More volatile than gold. Outperformance = risk appetite in precious metals. Underperformance = defensive gold positioning.',
        'watch': 'Gold/Silver ratio: High ratio = defensive, low ratio = aggressive.'
    },
    'dollar_index_price': {
        'what': 'US Dollar Index (via UUP ETF) - dollar strength vs basket of currencies.',
        'why': 'Strong dollar = global USD shortage or flight to safety. Weak dollar = risk-on or inflation fears.',
        'watch': 'Rising with gold = extreme crisis. Falling with gold rising = currency debasement fears.'
    },
    'commodities_price': {
        'what': 'Broad commodity index (DBC) - oil, metals, agriculture.',
        'why': 'Inflation gauge and economic activity indicator. Rising = growth or inflation. Falling = recession fears.',
        'watch': 'Divergence from equities = economic stress signal.'
    },
    'oil_price': {
        'what': 'Crude oil price (via USO ETF).',
        'why': 'Key inflation and economic activity indicator. Crash = recession fears. Spike = inflation/supply shock.',
        'watch': 'Sharp decline with equities = recession signal. Rising with falling equities = stagflation risk.'
    },
    'lqd_treasury_spread': {
        'what': 'Spread between LQD (investment-grade credit) yield and Treasury yield.',
        'why': 'Alternative measure of IG credit stress. Widening = IG credit stress building.',
        'watch': 'Watch for divergence from official IG spread data.'
    },
    'hyg_treasury_spread': {
        'what': 'Spread between HYG (high-yield credit) yield and Treasury yield.',
        'why': 'Market-based measure of HY credit risk using ETF yields.',
        'watch': 'Should track official HY spread. Divergence = ETF-specific flows or technical factors.'
    },
    'real_yield_proxy': {
        'what': 'Approximation of real yields using TIP (inflation-protected bonds).',
        'why': 'Real yields determine discount rate for growth stocks. Negative real yields = bullish for long-duration assets.',
        'watch': 'Rising real yields = pressure on growth stocks and gold. Falling = support for risk assets.'
    },
    'gold_silver_ratio': {
        'what': 'Gold price divided by silver price.',
        'why': 'High ratio (>80) = defensive positioning, fear. Low ratio (<70) = risk appetite in precious metals.',
        'watch': 'Current ratio tells you if gold move is fear-driven (high ratio) or inflation-driven (low ratio).'
    },
    'smh_spy_ratio': {
        'what': 'Semiconductor ETF (SMH) price divided by S&P 500 (SPY) price, scaled to 100. Measures semiconductor sector concentration.',
        'why': 'Semiconductors are at the heart of the AI boom. Rising ratio = semiconductor outperformance and increasing AI/tech concentration risk.',
        'watch': 'High and rising ratio = AI bubble concerns. Sharp decline = potential tech/AI selloff beginning. Compare to 2000 tech bubble.'
    },
    'xlk_spy_ratio': {
        'what': 'Technology Sector ETF (XLK) price divided by S&P 500 (SPY) price, scaled to 100. Measures tech sector weight in the market.',
        'why': 'Tech sector dominance indicator. When tech becomes too large a portion of the index, market becomes vulnerable to tech-specific risks.',
        'watch': 'Ratio >35 historically signals excessive tech concentration. Rising ratio = tech leadership. Falling ratio = rotation away from tech.'
    },
    'growth_value_ratio': {
        'what': 'Russell 1000 Growth (IWF) divided by Russell 1000 Value (IWD), scaled to 100. Measures growth vs value style preference.',
        'why': 'Growth stocks (including AI/tech) outperform when investors chase future earnings. Extreme growth outperformance often precedes corrections.',
        'watch': 'High ratio = growth bubble territory, similar to late 1990s. Falling ratio = value rotation, often signals economic slowdown fears.'
    },
    'iwm_spy_ratio': {
        'what': 'Russell 2000 Small Cap (IWM) divided by S&P 500 (SPY), scaled to 100. Measures small cap vs large cap relative performance.',
        'why': 'Small caps are more economically sensitive. Rising ratio = risk appetite, economic optimism. Falling ratio = flight to quality, economic concern.',
        'watch': 'Low ratio = large caps dominating (current environment). Rising ratio = small cap rotation, often bullish for broader economy. Watch for divergence from SPY.'
    },
    'financials_sector_price': {
        'what': 'Financial Select Sector ETF (XLF) - tracks major banks, insurance, and financial services companies.',
        'why': 'Banks are sensitive to yield curve, credit conditions, and economic health. Often leads the market during recoveries.',
        'watch': 'Strength = economic optimism, steepening yield curve. Weakness = credit concerns, recession fears. Compare to broader market for rotation signals.'
    },
    'energy_sector_price': {
        'what': 'Energy Select Sector ETF (XLE) - tracks major oil, gas, and energy equipment companies.',
        'why': 'Energy sector performance reflects oil prices and inflation expectations. Counter-cyclical to tech in some environments.',
        'watch': 'Strength = inflation hedge, commodity cycle. Weakness = deflation fears or oversupply. Often moves opposite to growth stocks.'
    },
    'usdjpy_price': {
        'what': 'USD/JPY exchange rate - how many Japanese Yen per 1 US Dollar.',
        'why': 'Critical for monitoring the yen carry trade. Investors borrow cheap yen to invest in higher-yielding assets. When yen strengthens (USD/JPY falls), carry trades unwind violently.',
        'watch': 'Rising USD/JPY = yen weakening, carry trade expanding. Sharp USD/JPY drop = carry trade unwinding, risk-off. Key levels: <140 = potential crisis, >155 = extreme yen weakness.'
    },
    'japan_10y_yield': {
        'what': 'Japan 10-Year Government Bond (JGB) yield. The benchmark interest rate for yen-denominated borrowing.',
        'why': 'Ultra-low Japanese rates enable the carry trade. When BOJ allows yields to rise, it signals potential end of easy money and triggers global risk-off.',
        'watch': 'Near-zero yields = carry trade intact. Rising yields (>0.5%) = BOJ tightening, carry trade at risk. BOJ yield curve control changes are major market events.'
    },
    'consumer_confidence': {
        'what': 'University of Michigan Consumer Sentiment Index. Survey-based measure of how optimistic consumers feel about the economy.',
        'why': 'Leading indicator of consumer spending, which drives ~70% of US GDP. Falling confidence often precedes recessions. Divergence from markets = warning sign.',
        'watch': 'Index <70 = recession territory. Index >100 = strong optimism. Sharp drops (>10 points) historically precede economic downturns. Compare to market levels for divergence signals.'
    },
    'm2_money_supply': {
        'what': 'M2 Money Stock - includes cash, checking deposits, savings deposits, and money market funds. Measures total "spendable" money in the economy.',
        'why': 'Tracks Fed liquidity injections/withdrawals. Rapid M2 growth = potential inflation. M2 contraction (rare) = deflationary pressure. Current cycle unprecedented.',
        'watch': 'YoY growth >10% = inflationary. YoY contraction = highly unusual, last seen in Great Depression. M2/GDP ratio shows excess liquidity relative to economy size.'
    },
    'cpi': {
        'what': 'Consumer Price Index for All Urban Consumers - measures average change in prices paid by consumers for goods and services.',
        'why': 'Primary inflation gauge used by Fed for policy decisions. High CPI = Fed tightening. Low/negative CPI = deflation risk and potential easing.',
        'watch': 'Fed target is ~2% YoY. >3% = elevated inflation. >5% = high inflation requiring aggressive response. Negative readings = deflation, major crisis signal.'
    },
    'yield_curve_10y2y': {
        'what': '10-Year Treasury yield minus 2-Year Treasury yield. The classic yield curve spread used to predict recessions.',
        'why': 'When short-term rates exceed long-term rates (inversion), it signals markets expect economic weakness ahead. Has preceded every US recession since 1970.',
        'watch': 'Positive spread = normal (economy healthy). Negative (inverted) = recession warning (typically 12-18 months ahead). Re-steepening after inversion often signals recession is imminent.'
    },
    'yield_curve_10y3m': {
        'what': '10-Year Treasury yield minus 3-Month Treasury yield. The Fed\'s preferred yield curve measure for recession prediction.',
        'why': 'More directly reflects Fed policy vs market expectations. 3-month rate is tightly tied to Fed Funds rate, while 10-year reflects growth/inflation expectations.',
        'watch': 'Inversion here is considered more significant by economists. Extended inversion (>3 months) is a stronger recession signal than brief inversions.'
    },
    'initial_claims': {
        'what': 'Weekly count of new unemployment insurance filings. Leading indicator of labor market health.',
        'why': 'First signal of labor market stress. Rising claims = companies laying off workers. Tends to spike quickly at recession onset.',
        'watch': 'Current healthy range: 200-250k. Warning zone: 250-300k. Recession signal: >300k sustained. Crisis: >400k. Watch for trend, not single weeks.'
    },
    'continuing_claims': {
        'what': 'Total number of people receiving unemployment benefits. Lagging confirmation of labor market conditions.',
        'why': 'Shows how long people stay unemployed. Rising continuing claims = harder to find new jobs = weakening economy.',
        'watch': 'Confirms initial claims trends. Sustained rise indicates recession has begun. Watch for divergence from initial claims (longer unemployment duration).'
    },
    'fed_balance_sheet': {
        'what': 'Total assets held by the Federal Reserve in billions of dollars. Includes Treasury securities, mortgage-backed securities, and other assets acquired through QE programs.',
        'why': 'Primary measure of Fed liquidity injection. Balance sheet expansion (QE) = adding liquidity = bullish for risk assets. Quantitative tightening (QT) = draining liquidity = headwind for markets.',
        'watch': 'Peak was ~$9 trillion in 2022. Currently shrinking via QT. Sharp expansion = crisis response. Correlation with Bitcoin and risk assets is strong.'
    },
    'reverse_repo': {
        'what': 'Overnight Reverse Repurchase Agreement Facility usage in billions. Money market funds park excess cash at the Fed overnight.',
        'why': 'Measures excess liquidity in the system. High RRP = too much cash chasing too few safe assets. Declining RRP = liquidity being absorbed by Treasury issuance or markets.',
        'watch': 'Peak was ~$2.5 trillion. Declining = liquidity draining from system. Near zero = potential liquidity stress. Rapid drop can signal tightening conditions.'
    },
    'nfci': {
        'what': 'Chicago Fed National Financial Conditions Index. Weighted average of 105 financial indicators including risk, credit, and leverage measures.',
        'why': 'Comprehensive measure of financial stress. Negative values = loose conditions (below average tightness). Positive values = tight conditions (stress building).',
        'watch': 'Zero = average conditions. >0.5 = tightening, watch for stress. <-0.5 = very loose, risk-on. Spikes precede or coincide with market stress events.'
    },
    'fear_greed_index': {
        'what': 'Alternative.me Crypto Fear & Greed Index (0-100 scale). Composite of volatility, momentum, social media, surveys, and dominance metrics.',
        'why': 'Sentiment indicator for crypto markets. Extreme fear often marks bottoms, extreme greed marks tops. Contrarian signal.',
        'watch': '0-25 = Extreme Fear (potential buying opportunity). 26-46 = Fear. 47-54 = Neutral. 55-75 = Greed. 76-100 = Extreme Greed (potential top).'
    },
    'btc_gold_ratio': {
        'what': 'Bitcoin price divided by gold price (ounces of gold per BTC). Measures Bitcoin\'s value relative to the traditional store of value.',
        'why': 'Compares two competing store-of-value assets. Rising ratio = Bitcoin outperforming gold. Falling ratio = gold outperforming Bitcoin.',
        'watch': 'Ratio expansion during risk-on periods, contraction during risk-off. Track alongside Fed liquidity - both assets respond to monetary policy.'
    },
    'real_yield_10y': {
        'what': '10-Year Treasury Inflation-Protected Securities (TIPS) yield. Represents the real (inflation-adjusted) return on 10-year government bonds.',
        'why': 'Key driver of gold prices - gold has strong inverse correlation with real yields. When real yields fall, gold becomes more attractive.',
        'watch': 'Rising real yields = headwind for gold. Falling real yields = tailwind for gold. Watch for Fed policy shifts affecting real rates.'
    },
    'breakeven_inflation_10y': {
        'what': '10-Year breakeven inflation rate (nominal Treasury yield minus TIPS yield). Market\'s expectation for average inflation over next 10 years.',
        'why': 'Measures inflation expectations. Higher breakevens = market expects more inflation, which can support gold prices.',
        'watch': 'Rising breakevens often coincide with gold rallies. Track alongside CPI and Fed inflation targets (2%).'
    },
    'gdx_gld_ratio': {
        'what': 'Gold miners ETF (GDX) divided by gold price ETF (GLD). Measures how miners are performing relative to gold itself.',
        'why': 'Miners have leverage to gold moves. When miners lead gold higher, it\'s bullish. When miners lag, it can signal exhaustion.',
        'watch': 'Rising ratio = miners outperforming (bullish), falling ratio = miners lagging (bearish divergence). Miners often lead gold at turning points.'
    },
    'treasury_10y': {
        'what': '10-Year Treasury Constant Maturity Rate (DGS10). The nominal yield on 10-year US government bonds.',
        'why': 'Benchmark risk-free rate for the US. Nominal yield = Real yield + Breakeven inflation. Key reference for all financial assets.',
        'watch': 'Rising yields can pressure gold and growth stocks. Compare to real yields to understand inflation expectations.'
    },
    'eurusd_price': {
        'what': 'EUR/USD exchange rate - how many US Dollars per 1 Euro. The most traded currency pair in the world.',
        'why': 'Primary gauge of dollar strength vs Europe. Driven by Fed/ECB policy divergence, growth differentials, and risk appetite. EUR is ~57% of DXY.',
        'watch': 'Rising EUR/USD = dollar weakening vs euro. Above 1.10 = euro strength. Below 1.05 = dollar strength. Rate differentials (US-Germany 10Y) are key driver.'
    },
    'germany_10y_yield': {
        'what': 'Germany 10-Year Government Bond (Bund) yield. The benchmark risk-free rate for the Eurozone.',
        'why': 'ECB policy gauge. Germany/US rate differential drives EUR/USD. Rising Bund yields = ECB tightening, supports euro. Falling = ECB dovish, weakens euro.',
        'watch': 'Negative yields (pre-2022) = extreme monetary accommodation. Positive and rising = policy normalization. Compare to US 10Y for rate differential.'
    },
    'us_japan_10y_spread': {
        'what': 'US 10-Year Treasury yield minus Japan 10-Year Government Bond yield. The rate differential that drives the yen carry trade.',
        'why': 'The carry trade: borrow cheap in yen, invest in higher-yielding USD assets. Wider spread = more incentive for carry trade = yen weakness, supports USD/JPY.',
        'watch': 'Spread >3% = strong carry trade incentive. Spread narrowing (Japan yields rising or US falling) = carry unwind risk, supports yen. BOJ policy is key.'
    },
    'us_germany_10y_spread': {
        'what': 'US 10-Year Treasury yield minus Germany 10-Year Bund yield. The rate differential that drives EUR/USD flows.',
        'why': 'Wider spread = US yields more attractive = capital flows to USD = EUR/USD falls. Narrowing spread = euro support as European yields become more competitive.',
        'watch': 'Spread >1.5% typically supports dollar. Spread narrowing often coincides with EUR/USD strength. Watch Fed vs ECB policy trajectory.'
    },
    # --- Market Conditions Framework series (US-296.1) ---
    'treasury_general_account': {
        'what': 'US Treasury General Account (TGA) balance held at the Federal Reserve, in millions of dollars. The government\'s checking account.',
        'why': 'TGA drawdowns inject liquidity into the banking system (reserves rise). TGA build-ups drain liquidity. Part of the Fed Net Liquidity calculation: WALCL − TGA − RRP.',
        'watch': 'Declining TGA = liquidity injection (bullish for risk assets). Rising TGA (e.g., post-debt ceiling) = liquidity drain. Watch alongside WALCL and RRP for net liquidity picture.'
    },
    'ecb_total_assets': {
        'what': 'European Central Bank total assets in millions of euros. Includes bonds purchased under QE programs, lending facilities, and other assets.',
        'why': 'Measures ECB balance sheet size — proxy for European monetary stimulus. Expanding = ECB adding liquidity. Contracting = quantitative tightening. Converted to USD for global liquidity composite.',
        'watch': 'ECB balance sheet peaked ~€8.8T in 2022. QT began mid-2023. Compare pace of ECB QT vs Fed QT for relative liquidity trends.'
    },
    'boj_total_assets': {
        'what': 'Bank of Japan total assets in 100 million yen. Includes Japanese Government Bonds (JGBs), ETFs, and other assets from decades of monetary easing.',
        'why': 'BOJ has the largest balance sheet relative to GDP of any major central bank. Changes in BOJ asset purchases affect global liquidity and yen carry trade dynamics.',
        'watch': 'BOJ balance sheet has been largely flat since YCC adjustments began. Any acceleration or reduction signals major policy shift. Converted to USD for global liquidity composite.'
    },
    'fx_eur_usd': {
        'what': 'EUR/USD spot exchange rate from FRED (daily). Number of US dollars per one euro.',
        'why': 'Used to convert ECB total assets from EUR to USD for the global liquidity composite. Also reflects relative monetary policy stance between Fed and ECB.',
        'watch': 'EUR/USD above 1.10 = euro strength (dollar weakness). Below 1.05 = dollar strength. Rate differentials between US and German 10Y bonds are the primary driver.'
    },
    'fx_jpy_usd': {
        'what': 'JPY/USD spot exchange rate from FRED (daily). Number of Japanese yen per one US dollar.',
        'why': 'Used to convert BOJ total assets from JPY to USD for the global liquidity composite. Reflects carry trade dynamics — yen weakens when rate differentials favor USD.',
        'watch': 'USD/JPY above 150 = extreme yen weakness, carry trade extended. Sharp yen strengthening (USD/JPY dropping) = carry trade unwind risk, global risk-off catalyst.'
    },
    'industrial_production': {
        'what': 'Industrial Production Index (INDPRO) — measures real output of manufacturing, mining, and utilities sectors. Base year 2017 = 100.',
        'why': 'Hard economic data (not survey-based). YoY acceleration/deceleration feeds the Growth dimension of the Market Conditions quadrant. Declining IP often leads recessions.',
        'watch': 'YoY growth >3% = strong expansion. Negative YoY = contraction warning. Compare to ISM Manufacturing PMI for leading vs coincident signals.'
    },
    'building_permits': {
        'what': 'New privately-owned housing units authorized by building permits, in thousands of units (seasonally adjusted annual rate).',
        'why': 'Leading indicator of housing activity and broader economic growth. Permits lead housing starts by 1-2 months. YoY changes feed the Growth dimension of Market Conditions.',
        'watch': 'Sustained decline from peak = housing slowdown ahead. Below 1.2M SAAR = weak. Above 1.6M = strong. Compare to mortgage rates for affordability signal.'
    },
    'breakeven_inflation_5y': {
        'what': '5-Year breakeven inflation rate — the difference between nominal 5-Year Treasury yield and 5-Year TIPS yield. Market\'s expected average inflation over next 5 years.',
        'why': 'Shorter-horizon inflation expectations than the 10Y breakeven. More responsive to near-term inflation shocks. Feeds the Inflation dimension of the Market Conditions quadrant.',
        'watch': 'Above 2.5% = elevated inflation expectations. Below 1.5% = deflation concerns. Compare to CPI and Core PCE for expectations vs reality gap.'
    },
    'core_pce_price_index': {
        'what': 'Personal Consumption Expenditures Price Index excluding food and energy (Core PCE). The Fed\'s preferred inflation measure.',
        'why': 'Core PCE is what the Fed actually targets at 2%. Less volatile than CPI because it excludes food/energy and adjusts for consumer substitution. Used in Taylor Rule calculations.',
        'watch': 'Fed target: 2% YoY. Above 3% = Fed likely hawkish. Below 2% = room for easing. Watch for gap between Core PCE and CPI — divergence signals composition shifts.'
    },
    'vix_3month': {
        'what': 'CBOE 3-Month Volatility Index (VIX3M, formerly VXV). Measures expected S&P 500 volatility over the next 3 months.',
        'why': 'Used with VIX to compute the VIX term structure ratio (VIX/VIX3M). Ratio >1 = backwardation (near-term fear exceeds longer-term). Part of the Risk dimension in Market Conditions.',
        'watch': 'VIX/VIX3M ratio >1.0 = inverted term structure (acute stress). Ratio <0.85 = steep contango (complacency). History starts Dec 2007.'
    },
    'stl_financial_stress': {
        'what': 'St. Louis Fed Financial Stress Index (STLFSI4). Weekly composite of 18 financial market indicators including yield spreads, volatility, and funding costs.',
        'why': 'Comprehensive measure of US financial system stress. Zero = average conditions. Positive = above-average stress. Feeds into the Risk dimension of Market Conditions alongside VIX.',
        'watch': 'Below 0 = loose financial conditions. Above 1.0 = notable stress. Above 2.0 = severe stress (seen in 2008, 2020). Tends to spike before equity drawdowns.'
    },
    'fed_funds_upper_target': {
        'what': 'Federal Funds Target Rate — Upper Limit (DFEDTARU). The top of the Fed\'s target range for overnight bank lending.',
        'why': 'The Fed\'s primary policy tool. Used in Taylor Rule gap calculation: actual rate minus Taylor-implied rate. Positive gap = restrictive stance, negative = accommodative.',
        'watch': 'Compare to Taylor Rule implied rate. If actual rate far exceeds Taylor rate → overly restrictive. If below → accommodative. Direction changes (cuts vs hikes) drive all asset classes.'
    },
    'pce_price_index': {
        'what': 'Personal Consumption Expenditures Price Index (headline PCE). Broader than Core PCE — includes food and energy.',
        'why': 'Input to the Taylor Rule calculation for inflation. YoY PCE change approximates the inflation term in i* = 1.0 + 1.5π + 0.5(output gap). Distinct from Core PCE.',
        'watch': 'Used alongside Core PCE. Headline PCE diverging from Core signals food/energy shocks. Falling PCE = disinflation (supports rate cuts). Rising = Fed stays tight.'
    },
    'real_gdp': {
        'what': 'Real Gross Domestic Product in billions of chained 2017 dollars (quarterly). Inflation-adjusted measure of total economic output.',
        'why': 'Used to calculate the output gap (actual GDP vs potential GDP) for the Taylor Rule. Positive gap = overheating. Negative gap = slack. Released with ~1 month lag.',
        'watch': 'QoQ annualized growth <0% for 2 quarters = recession. Above 3% = strong growth. Output gap = ln(Real GDP / Potential GDP) × 100 for Taylor Rule.'
    },
    'potential_gdp': {
        'what': 'Congressional Budget Office (CBO) estimate of potential GDP in billions of dollars (quarterly). The economy\'s maximum sustainable output.',
        'why': 'Benchmark for the output gap in Taylor Rule. Potential GDP is a smooth trend — actual GDP oscillates around it. Gap measures economic slack or overheating.',
        'watch': 'Potential GDP grows ~2% per year. Subject to large revisions (CBO updates twice yearly). Compare to Real GDP: actual > potential = overheating, actual < potential = slack.'
    },
    'natural_unemployment_rate': {
        'what': 'CBO Natural Rate of Unemployment (NAIRU) — the unemployment rate consistent with stable inflation (quarterly).',
        'why': 'Used in Okun\'s Law to estimate the output gap: gap ≈ −2 × (actual unemployment − NAIRU). When unemployment is below NAIRU, economy is running hot.',
        'watch': 'NAIRU is currently ~4.4%. Actual unemployment below NAIRU = tight labor market (inflationary). Above NAIRU = slack (disinflationary). Subject to CBO revision.'
    },
    'unemployment_rate': {
        'what': 'US civilian unemployment rate as a percentage of the labor force (monthly, seasonally adjusted).',
        'why': 'Key labor market indicator and input to Okun\'s Law output gap calculation. Low unemployment = tight labor market. Rising unemployment often confirms recession.',
        'watch': 'Below 4% = historically tight. Above 5% = weakening. Sahm Rule: 0.5pp rise from 12-month low = recession signal. Compare to NAIRU for policy implications.'
    },
}


# --- Market Conditions dimension categories for Explorer grouping (US-296.1) ---
METRIC_CATEGORIES = {
    # Market Conditions — Liquidity
    'fed_balance_sheet': 'Conditions: Liquidity',
    'treasury_general_account': 'Conditions: Liquidity',
    'reverse_repo': 'Conditions: Liquidity',
    'ecb_total_assets': 'Conditions: Liquidity',
    'boj_total_assets': 'Conditions: Liquidity',
    'fx_eur_usd': 'Conditions: Liquidity',
    'fx_jpy_usd': 'Conditions: Liquidity',
    'm2_money_supply': 'Conditions: Liquidity',
    # Market Conditions — Growth × Inflation
    'industrial_production': 'Conditions: Growth × Inflation',
    'building_permits': 'Conditions: Growth × Inflation',
    'breakeven_inflation_5y': 'Conditions: Growth × Inflation',
    'cpi': 'Conditions: Growth × Inflation',
    'core_pce_price_index': 'Conditions: Growth × Inflation',
    'median_cpi': 'Conditions: Growth × Inflation',
    'inflation_expectations_5y5y': 'Conditions: Growth × Inflation',
    'michigan_inflation_expectations': 'Conditions: Growth × Inflation',
    # Market Conditions — Risk
    'vix_3month': 'Conditions: Risk',
    'stl_financial_stress': 'Conditions: Risk',
    'vix_price': 'Conditions: Risk',
    'nfci': 'Conditions: Risk',
    # Market Conditions — Policy
    'fed_funds_upper_target': 'Conditions: Policy',
    'fed_funds_rate': 'Conditions: Policy',
    'pce_price_index': 'Conditions: Policy',
    'real_gdp': 'Conditions: Policy',
    'potential_gdp': 'Conditions: Policy',
    'unemployment_rate': 'Conditions: Policy',
    'natural_unemployment_rate': 'Conditions: Policy',
    # Credit
    'high_yield_spread': 'Credit',
    'investment_grade_spread': 'Credit',
    'ccc_spread': 'Credit',
    'high_yield_credit_price': 'Credit',
    'investment_grade_credit_price': 'Credit',
    'leveraged_loan_price': 'Credit',
    'lqd_treasury_spread': 'Credit',
    'hyg_treasury_spread': 'Credit',
    'divergence_gap': 'Credit',
    # Equities
    'sp500_price': 'Equities',
    'nasdaq_price': 'Equities',
    'small_cap_price': 'Equities',
    'market_breadth_ratio': 'Equities',
    'smh_spy_ratio': 'Equities',
    'xlk_spy_ratio': 'Equities',
    'growth_value_ratio': 'Equities',
    'iwm_spy_ratio': 'Equities',
    'financials_sector_price': 'Equities',
    'energy_sector_price': 'Equities',
    'growth_price': 'Equities',
    'value_price': 'Equities',
    'sp500_equal_weight_price': 'Equities',
    'semiconductor_price': 'Equities',
    'tech_sector_price': 'Equities',
    # Rates
    'treasury_10y': 'Rates',
    'treasury_20yr_price': 'Rates',
    'treasury_7_10yr_price': 'Rates',
    'treasury_short_price': 'Rates',
    'tips_inflation_price': 'Rates',
    'real_yield_10y': 'Rates',
    'real_yield_proxy': 'Rates',
    'breakeven_inflation_10y': 'Rates',
    'yield_curve_10y2y': 'Rates',
    'yield_curve_10y3m': 'Rates',
    # Safe Havens
    'gold_price': 'Safe Havens',
    'silver_price': 'Safe Havens',
    'gold_miners_price': 'Safe Havens',
    'gold_silver_ratio': 'Safe Havens',
    'gdx_gld_ratio': 'Safe Havens',
    # Crypto
    'bitcoin_price': 'Crypto',
    'ethereum_price': 'Crypto',
    'btc_gold_ratio': 'Crypto',
    'fear_greed_index': 'Crypto',
    # Dollar & FX
    'dollar_index_price': 'Dollar & FX',
    'eurusd_price': 'Dollar & FX',
    'usdjpy_price': 'Dollar & FX',
    'germany_10y_yield': 'Dollar & FX',
    'japan_10y_yield': 'Dollar & FX',
    'us_japan_10y_spread': 'Dollar & FX',
    'us_germany_10y_spread': 'Dollar & FX',
    # Economy
    'consumer_confidence': 'Economy',
    'initial_claims': 'Economy',
    'continuing_claims': 'Economy',
    'trade_balance': 'Economy',
    # Commodities
    'commodities_price': 'Commodities',
    'oil_price': 'Commodities',
}

# Custom display names for metrics that need better labels than auto-generated title case
METRIC_DISPLAY_NAMES = {
    'boj_total_assets': 'BOJ Total Assets (100M JPY)',
    'breakeven_inflation_5y': '5-Year Breakeven Inflation',
    'breakeven_inflation_10y': '10-Year Breakeven Inflation',
    'btc_gold_ratio': 'Bitcoin/Gold Ratio',
    'ccc_spread': 'CCC Spread',
    'cpi': 'CPI (Consumer Price Index)',
    'core_pce_price_index': 'Core PCE Price Index',
    'median_cpi': 'Cleveland Fed Median CPI',
    'inflation_expectations_5y5y': '5Y5Y Forward Inflation Expectations',
    'michigan_inflation_expectations': 'Michigan 1-Year Inflation Expectations',
    'ecb_total_assets': 'ECB Total Assets (M EUR)',
    'eurusd_price': 'EUR/USD Exchange Rate',
    'fed_balance_sheet': 'Fed Balance Sheet (WALCL)',
    'fed_funds_rate': 'Fed Funds Rate',
    'fed_funds_upper_target': 'Fed Funds Upper Target Rate',
    'fx_eur_usd': 'EUR/USD FX Rate (FRED)',
    'fx_jpy_usd': 'JPY/USD FX Rate',
    'gdx_gld_ratio': 'GDX/GLD Ratio (Miners vs Gold)',
    'gold_silver_ratio': 'Gold/Silver Ratio',
    'growth_value_ratio': 'Growth/Value Ratio',
    'hyg_treasury_spread': 'HYG/Treasury Spread',
    'iwm_spy_ratio': 'IWM/SPY Ratio (Small/Large Cap)',
    'lqd_treasury_spread': 'LQD/Treasury Spread',
    'm2_money_supply': 'M2 Money Supply',
    'natural_unemployment_rate': 'Natural Unemployment Rate (NAIRU)',
    'nfci': 'NFCI (Financial Conditions Index)',
    'pce_price_index': 'PCE Price Index (Headline)',
    'real_gdp': 'Real GDP',
    'real_yield_10y': '10-Year Real Yield (TIPS)',
    'real_yield_proxy': 'Real Yield Proxy',
    'reverse_repo': 'Reverse Repo (RRP)',
    'smh_spy_ratio': 'SMH/SPY Ratio (Semiconductors)',
    'sp500_equal_weight_price': 'S&P 500 Equal Weight',
    'sp500_price': 'S&P 500',
    'stl_financial_stress': 'St. Louis Financial Stress Index',
    'tips_inflation_price': 'TIPS (Inflation-Protected)',
    'treasury_10y': '10-Year Treasury Yield',
    'treasury_20yr_price': '20+ Year Treasury (TLT)',
    'treasury_7_10yr_price': '7-10 Year Treasury (IEF)',
    'treasury_general_account': 'Treasury General Account (TGA)',
    'treasury_short_price': 'Short-Term Treasury (SHY)',
    'us_germany_10y_spread': 'US-Germany 10Y Spread',
    'us_japan_10y_spread': 'US-Japan 10Y Spread',
    'usdjpy_price': 'USD/JPY Exchange Rate',
    'vix_3month': 'VIX 3-Month (VIX3M)',
    'vix_price': 'VIX (Volatility Index)',
    'xlk_spy_ratio': 'XLK/SPY Ratio (Tech Sector)',
    'yield_curve_10y2y': 'Yield Curve (10Y-2Y)',
    'yield_curve_10y3m': 'Yield Curve (10Y-3M)',
    'potential_gdp': 'Potential GDP (CBO Estimate)',
}


def load_csv_data(filename):
    """Load CSV file and return as DataFrame."""
    filepath = DATA_DIR / filename
    if filepath.exists():
        df = pd.read_csv(filepath)
        # us_recessions.csv has start_date/end_date, not date
        if filename != 'us_recessions.csv':
            df['date'] = pd.to_datetime(df['date'])
        return df
    return None


def calculate_returns(df, column, periods=[1, 5, 20]):
    """Calculate returns over multiple periods."""
    if df is None or len(df) < 2:
        return {}

    returns = {}
    for period in periods:
        if len(df) > period:
            current = df.iloc[-1][column]
            past = df.iloc[-(period+1)][column]
            returns[f'{period}d'] = ((current / past) - 1) * 100
    return returns


def calculate_percentile_rank(series, current_value):
    """Calculate percentile rank of current_value within a pandas Series.

    Uses rolling 20-year history (or full available history if < 20 years).
    Returns a float 0–100, or None if series is empty.
    """
    import numpy as np
    if series is None or len(series) == 0:
        return None
    if len(series) == 1:
        return 100.0 if current_value >= series.iloc[0] else 0.0

    # Apply 20-year rolling window where possible
    cutoff = pd.Timestamp.today() - pd.DateOffset(years=20)
    # series index expected to be dates; filter if index is datetime-like
    try:
        windowed = series[series.index >= cutoff]
        if len(windowed) < 2:
            windowed = series
    except TypeError:
        windowed = series

    count_below = (windowed < current_value).sum()
    return float(count_below / len(windowed) * 100)


def _percentile_label(pct):
    """Return a human-readable label for a percentile rank."""
    if pct is None:
        return 'unavailable'
    if pct <= 10:
        return 'near historical tights'
    if pct <= 25:
        return 'historically tight'
    if pct <= 50:
        return 'below median'
    if pct <= 75:
        return 'above median'
    if pct <= 90:
        return 'historically wide'
    return 'near historical wides'


def calculate_top_movers(num_movers=5, period=5):
    """
    Calculate top movers based on z-score of N-day changes.
    Returns metrics with most unusual moves relative to their historical volatility.

    Args:
        num_movers: Number of top movers to return.
        period: Change period in days (1 for daily, 5 for weekly).
    """
    import os
    import numpy as np

    movers = []

    # Define which metrics use absolute change vs % change for display
    # Also define friendly names and explorer links
    metric_config = {
        # Spreads - use absolute bp change
        'high_yield_spread': {'display': 'abs', 'unit': 'bp', 'name': 'HY Spread', 'link': '/explorer?metric=high_yield_spread', 'multiplier': 100},
        'investment_grade_spread': {'display': 'abs', 'unit': 'bp', 'name': 'IG Spread', 'link': '/explorer?metric=investment_grade_spread', 'multiplier': 100},
        'ccc_spread': {'display': 'abs', 'unit': 'bp', 'name': 'CCC Spread', 'link': '/explorer?metric=ccc_spread', 'multiplier': 100},

        # Prices - use % change
        'gold_price': {'display': 'pct', 'unit': '%', 'name': 'Gold', 'link': '/explorer?metric=gold_price'},
        'silver_price': {'display': 'pct', 'unit': '%', 'name': 'Silver', 'link': '/explorer?metric=silver_price'},
        'bitcoin_price': {'display': 'pct', 'unit': '%', 'name': 'Bitcoin', 'link': '/explorer?metric=bitcoin_price'},
        'sp500_price': {'display': 'pct', 'unit': '%', 'name': 'S&P 500', 'link': '/explorer?metric=sp500_price'},
        'nasdaq_price': {'display': 'pct', 'unit': '%', 'name': 'Nasdaq', 'link': '/explorer?metric=nasdaq_price'},
        'vix_price': {'display': 'pct', 'unit': '%', 'name': 'VIX', 'link': '/explorer?metric=vix_price'},
        'oil_price': {'display': 'pct', 'unit': '%', 'name': 'Oil', 'link': '/explorer?metric=oil_price'},
        'commodities_price': {'display': 'pct', 'unit': '%', 'name': 'Commodities', 'link': '/explorer?metric=commodities_price'},
        'gold_miners_price': {'display': 'pct', 'unit': '%', 'name': 'Gold Miners', 'link': '/explorer?metric=gold_miners_price'},
        'small_cap_price': {'display': 'pct', 'unit': '%', 'name': 'Small Caps', 'link': '/explorer?metric=small_cap_price'},
        'tech_sector_price': {'display': 'pct', 'unit': '%', 'name': 'Tech Sector', 'link': '/explorer?metric=tech_sector_price'},
        'semiconductor_price': {'display': 'pct', 'unit': '%', 'name': 'Semiconductors', 'link': '/explorer?metric=semiconductor_price'},
        'financials_sector_price': {'display': 'pct', 'unit': '%', 'name': 'Financials', 'link': '/explorer?metric=financials_sector_price'},
        'energy_sector_price': {'display': 'pct', 'unit': '%', 'name': 'Energy', 'link': '/explorer?metric=energy_sector_price'},
        'treasury_20yr_price': {'display': 'pct', 'unit': '%', 'name': 'Treasury 20Y', 'link': '/explorer?metric=treasury_20yr_price'},
        'dollar_index_price': {'display': 'pct', 'unit': '%', 'name': 'Dollar Index', 'link': '/explorer?metric=dollar_index_price'},
        'usdjpy_price': {'display': 'pct', 'unit': '%', 'name': 'USD/JPY', 'link': '/explorer?metric=usdjpy_price'},

        # Ratios - use % change
        'gold_silver_ratio': {'display': 'pct', 'unit': '%', 'name': 'Gold/Silver Ratio', 'link': '/explorer?metric=gold_silver_ratio'},
        'smh_spy_ratio': {'display': 'pct', 'unit': '%', 'name': 'Semiconductor/SPY', 'link': '/explorer?metric=smh_spy_ratio'},
        'xlk_spy_ratio': {'display': 'pct', 'unit': '%', 'name': 'Tech/SPY', 'link': '/explorer?metric=xlk_spy_ratio'},
        'growth_value_ratio': {'display': 'pct', 'unit': '%', 'name': 'Growth/Value', 'link': '/explorer?metric=growth_value_ratio'},
        'iwm_spy_ratio': {'display': 'pct', 'unit': '%', 'name': 'Small/Large Cap', 'link': '/explorer?metric=iwm_spy_ratio'},
        'market_breadth_ratio': {'display': 'pct', 'unit': '%', 'name': 'Market Breadth', 'link': '/explorer?metric=market_breadth_ratio'},

        # Yields - use absolute change
        'japan_10y_yield': {'display': 'abs', 'unit': '%', 'name': 'Japan 10Y Yield', 'link': '/explorer?metric=japan_10y_yield', 'multiplier': 1},
        'real_yield_proxy': {'display': 'pct', 'unit': '%', 'name': 'Real Yield Proxy', 'link': '/explorer?metric=real_yield_proxy'},
        'treasury_10y': {'display': 'abs', 'unit': '%', 'name': '10Y Treasury', 'link': '/explorer?metric=treasury_10y', 'multiplier': 1},

        # ETF spreads
        'hyg_treasury_spread': {'display': 'pct', 'unit': '%', 'name': 'HYG-Treasury Spread', 'link': '/explorer?metric=hyg_treasury_spread'},
        'lqd_treasury_spread': {'display': 'pct', 'unit': '%', 'name': 'LQD-Treasury Spread', 'link': '/explorer?metric=lqd_treasury_spread'},

        # Economic Indicators - use % change
        'consumer_confidence': {'display': 'pct', 'unit': '%', 'name': 'Consumer Confidence', 'link': '/explorer?metric=consumer_confidence'},
        'm2_money_supply': {'display': 'pct', 'unit': '%', 'name': 'M2 Money Supply', 'link': '/explorer?metric=m2_money_supply'},
        'cpi': {'display': 'pct', 'unit': '%', 'name': 'CPI (Inflation)', 'link': '/explorer?metric=cpi'},

        # Yield Curve - use absolute change (spread in %)
        'yield_curve_10y2y': {'display': 'abs', 'unit': '%', 'name': '10Y-2Y Yield Curve', 'link': '/explorer?metric=yield_curve_10y2y', 'multiplier': 1},
        'yield_curve_10y3m': {'display': 'abs', 'unit': '%', 'name': '10Y-3M Yield Curve', 'link': '/explorer?metric=yield_curve_10y3m', 'multiplier': 1},

        # Labor Market - use % change
        'initial_claims': {'display': 'pct', 'unit': '%', 'name': 'Initial Claims', 'link': '/explorer?metric=initial_claims'},
        'continuing_claims': {'display': 'pct', 'unit': '%', 'name': 'Continuing Claims', 'link': '/explorer?metric=continuing_claims'},

        # Fed Liquidity & Financial Conditions - use % change
        'fed_balance_sheet': {'display': 'pct', 'unit': '%', 'name': 'Fed Balance Sheet', 'link': '/explorer?metric=fed_balance_sheet'},
        'reverse_repo': {'display': 'pct', 'unit': '%', 'name': 'Reverse Repo', 'link': '/explorer?metric=reverse_repo'},
        'nfci': {'display': 'abs', 'unit': 'index', 'name': 'NFCI', 'link': '/explorer?metric=nfci', 'multiplier': 1},

        # Crypto Sentiment
        'fear_greed_index': {'display': 'abs', 'unit': 'pts', 'name': 'Crypto Fear/Greed', 'link': '/explorer?metric=fear_greed_index', 'multiplier': 1},
        'btc_gold_ratio': {'display': 'pct', 'unit': '%', 'name': 'BTC/Gold Ratio', 'link': '/explorer?metric=btc_gold_ratio'},

        # Real Yields & Inflation (Gold Drivers)
        'real_yield_10y': {'display': 'abs', 'unit': '%', 'name': '10Y Real Yield', 'link': '/explorer?metric=real_yield_10y', 'multiplier': 1},
        'breakeven_inflation_10y': {'display': 'abs', 'unit': '%', 'name': '10Y Breakeven', 'link': '/explorer?metric=breakeven_inflation_10y', 'multiplier': 1},
        'gdx_gld_ratio': {'display': 'pct', 'unit': '%', 'name': 'GDX/GLD Ratio', 'link': '/explorer?metric=gdx_gld_ratio'},
    }

    # Process each metric file
    csv_files = [f for f in os.listdir(DATA_DIR) if f.endswith('.csv') and f != 'us_recessions.csv']

    for filename in csv_files:
        metric_name = filename.replace('.csv', '')
        config = metric_config.get(metric_name)

        # Skip metrics not in our config (like etf duplicates)
        if not config:
            continue

        df = load_csv_data(filename)
        if df is None or len(df) < 60:  # Need enough history for z-score
            continue

        # Check data freshness - only include metrics with data from past 5 days
        last_date = df['date'].iloc[-1]
        days_since_update = (pd.Timestamp.now() - pd.Timestamp(last_date)).days
        if days_since_update > 5:
            continue

        col = df.columns[1]  # Data column
        multiplier = config.get('multiplier', 1)

        # Calculate N-day changes for z-score
        # Rolling window scales with period: 60 for 5d, 30 for 1d
        rolling_window = max(30, period * 12)
        min_rows = rolling_window + period + 5

        if config['display'] == 'pct':
            df['change_nd'] = df[col].pct_change(period) * 100
        else:
            df['change_nd'] = (df[col] - df[col].shift(period)) * multiplier

        df['rolling_std'] = df['change_nd'].rolling(rolling_window).std()
        df['rolling_mean'] = df['change_nd'].rolling(rolling_window).mean()

        if len(df) < min_rows:
            continue

        current_change = df['change_nd'].iloc[-1]
        rolling_std = df['rolling_std'].iloc[-1]
        rolling_mean = df['rolling_mean'].iloc[-1]

        if pd.isna(current_change) or pd.isna(rolling_std) or rolling_std == 0:
            continue

        z_score = (current_change - rolling_mean) / rolling_std

        # Get current value (apply multiplier for display)
        raw_value = df[col].iloc[-1]
        if pd.isna(raw_value):
            continue
        current_value = float(raw_value) * multiplier

        change_val = round(float(current_change), 2)
        mover = {
            'metric': metric_name,
            'name': config['name'],
            'link': config['link'],
            'change': change_val,
            f'change_{period}d': change_val,
            'z_score': round(float(z_score), 2),
            'unit': config['unit'],
            'display_type': config['display'],
            'current_value': round(current_value, 2)
        }
        movers.append(mover)

    # Add calculated metrics (divergence gap, CCC/HY ratio)
    # Divergence Gap
    gold_df = load_csv_data('gold_price.csv')
    hy_df = load_csv_data('high_yield_spread.csv')

    if gold_df is not None and hy_df is not None and len(gold_df) > 65 and len(hy_df) > 65:
        # Calculate divergence gap series
        merged = pd.merge(gold_df, hy_df, on='date', suffixes=('_gold', '_hy'))
        merged['gold_price'] = merged[merged.columns[1]] * 10
        merged['hy_spread'] = merged[merged.columns[2]] * 100
        merged['gold_implied'] = ((merged['gold_price'] / 2000) ** 1.5) * 400
        merged['divergence_gap'] = merged['gold_implied'] - merged['hy_spread']

        rolling_window = max(30, period * 12)
        merged['change_nd'] = merged['divergence_gap'] - merged['divergence_gap'].shift(period)
        merged['rolling_std'] = merged['change_nd'].rolling(rolling_window).std()
        merged['rolling_mean'] = merged['change_nd'].rolling(rolling_window).mean()

        if len(merged) > rolling_window + period + 5:
            current_change = merged['change_nd'].iloc[-1]
            rolling_std = merged['rolling_std'].iloc[-1]
            rolling_mean = merged['rolling_mean'].iloc[-1]

            if not pd.isna(current_change) and not pd.isna(rolling_std) and rolling_std > 0:
                z_score = (current_change - rolling_mean) / rolling_std
                current_divergence = merged['divergence_gap'].iloc[-1]
                if not pd.isna(current_divergence):
                    change_val = int(round(float(current_change), 0))
                    movers.append({
                        'metric': 'divergence_gap',
                        'name': 'Divergence Gap',
                        'link': '/explorer?metric=divergence_gap',
                        'change': change_val,
                        f'change_{period}d': change_val,
                        'z_score': round(float(z_score), 2),
                        'unit': 'bp',
                        'display_type': 'abs',
                        'current_value': int(round(float(current_divergence), 0))
                    })

    # Sort by absolute z-score and return top N
    movers.sort(key=lambda x: abs(x['z_score']), reverse=True)
    return movers[:num_movers]


def calculate_crisis_score():
    """Calculate comprehensive crisis warning score."""
    score = 0
    warnings = []

    # Load data
    hy_df = load_csv_data('high_yield_spread.csv')
    ig_df = load_csv_data('investment_grade_spread.csv')
    gold_df = load_csv_data('gold_price.csv')
    vix_df = load_csv_data('vix_price.csv')
    btc_df = load_csv_data('bitcoin_price.csv')
    breadth_df = load_csv_data('market_breadth_ratio.csv')

    # Credit spreads (0-25 points)
    if hy_df is not None:
        hy_current = hy_df.iloc[-1][hy_df.columns[1]] * 100
        if hy_current < 250:
            score += 15
            warnings.append(f"HY spreads extremely tight ({hy_current:.0f} bp)")
        elif hy_current < 300:
            score += 10
            warnings.append(f"HY spreads very tight ({hy_current:.0f} bp)")

    if ig_df is not None:
        ig_current = ig_df.iloc[-1][ig_df.columns[1]] * 100
        if ig_current < 80:
            score += 10
            warnings.append(f"IG spreads extremely tight ({ig_current:.0f} bp)")

    # Gold (0-25 points)
    if gold_df is not None:
        gold_price = gold_df.iloc[-1][gold_df.columns[1]] * 10
        if gold_price > 4500:
            score += 25
            warnings.append(f"Gold EXTREME (${gold_price:.0f})")
        elif gold_price > 4000:
            score += 20
            warnings.append(f"Gold very elevated (${gold_price:.0f})")
        elif gold_price > 3500:
            score += 15
            warnings.append(f"Gold elevated (${gold_price:.0f})")

    # Divergence gap (0-25 points)
    if gold_df is not None and hy_df is not None:
        gold_price = gold_df.iloc[-1][gold_df.columns[1]] * 10
        hy_current = hy_df.iloc[-1][hy_df.columns[1]] * 100
        gold_implied = ((gold_price / 2000) ** 1.5) * 400
        divergence_gap = gold_implied - hy_current

        if divergence_gap > 800:
            score += 25
            warnings.append(f"EXTREME divergence gap ({divergence_gap:.0f} bp)")
        elif divergence_gap > 600:
            score += 20
            warnings.append(f"Very large divergence gap ({divergence_gap:.0f} bp)")
        elif divergence_gap > 400:
            score += 15
            warnings.append(f"Large divergence gap ({divergence_gap:.0f} bp)")

    # Market breadth (0-15 points)
    if breadth_df is not None and len(breadth_df) > 0:
        breadth = breadth_df.iloc[-1][breadth_df.columns[1]]
        if breadth < 95:
            score += 10
            warnings.append("Market breadth weak - narrow rally")

    # VIX (0-10 points)
    if vix_df is not None:
        vix_current = vix_df.iloc[-1][vix_df.columns[1]]
        if vix_current > 30:
            score += 10
            warnings.append(f"VIX elevated ({vix_current:.1f})")
        elif vix_current < 15 and score > 30:
            score += 5
            warnings.append(f"VIX low despite warnings ({vix_current:.1f}) - complacency")

    # Bitcoin (0-10 points)
    if btc_df is not None and len(btc_df) > 20:
        btc_returns = calculate_returns(btc_df, btc_df.columns[1], [20])
        if '20d' in btc_returns:
            if btc_returns['20d'] < -10:
                score += 10
                warnings.append(f"Bitcoin weak ({btc_returns['20d']:.1f}% 30d)")
            elif btc_returns['20d'] < -5:
                score += 5
                warnings.append(f"Bitcoin declining ({btc_returns['20d']:.1f}% 30d)")

    return min(score, 100), warnings


def get_dashboard_data():
    """Get all dashboard data."""
    eastern = pytz.timezone('US/Eastern')
    data = {
        'timestamp': datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S'),
        'metrics': {},
        'charts': {},
        'warnings': []
    }

    # Calculate crisis score
    crisis_score, warnings = calculate_crisis_score()
    data['metrics']['crisis_score'] = crisis_score
    data['warnings'] = warnings

    # Load key metrics
    hy_df = load_csv_data('high_yield_spread.csv')
    ig_df = load_csv_data('investment_grade_spread.csv')
    ccc_df = load_csv_data('ccc_spread.csv')
    gold_df = load_csv_data('gold_price.csv')
    btc_df = load_csv_data('bitcoin_price.csv')
    spy_df = load_csv_data('sp500_price.csv')
    vix_df = load_csv_data('vix_price.csv')

    # HY Spreads
    if hy_df is not None:
        hy_current = hy_df.iloc[-1][hy_df.columns[1]] * 100
        hy_change_30d = (hy_df.iloc[-1][hy_df.columns[1]] - hy_df.iloc[max(0, len(hy_df)-21)][hy_df.columns[1]]) * 100
        hy_change_5d = (hy_df.iloc[-1][hy_df.columns[1]] - hy_df.iloc[max(0, len(hy_df)-6)][hy_df.columns[1]]) * 100
        data['metrics']['hy_spread'] = {
            'current': round(hy_current, 0),
            'change_5d': round(hy_change_5d, 0),
            'change_30d': round(hy_change_30d, 0),
            'status': 'tight' if hy_current < 300 else 'normal' if hy_current < 600 else 'wide'
        }

    # Gold
    if gold_df is not None:
        gold_price = gold_df.iloc[-1][gold_df.columns[1]] * 10
        gold_returns = calculate_returns(gold_df, gold_df.columns[1], [5, 20])
        data['metrics']['gold'] = {
            'price': round(gold_price, 0),
            'change_5d': round(gold_returns.get('5d', 0), 2),
            'change_30d': round(gold_returns.get('20d', 0), 2)
        }

    # Bitcoin
    if btc_df is not None:
        btc_price = btc_df.iloc[-1][btc_df.columns[1]]
        btc_returns = calculate_returns(btc_df, btc_df.columns[1], [5, 20])

        # Calculate peak and decline
        btc_peak = btc_df[btc_df.columns[1]].max()
        btc_decline = ((btc_price / btc_peak) - 1) * 100

        data['metrics']['bitcoin'] = {
            'price': round(btc_price, 0),
            'peak': round(btc_peak, 0),
            'decline_from_peak': round(btc_decline, 1),
            'change_5d': round(btc_returns.get('5d', 0), 2),
            'change_30d': round(btc_returns.get('20d', 0), 2)
        }

    # Divergence Gap
    if gold_df is not None and hy_df is not None:
        gold_price = gold_df.iloc[-1][gold_df.columns[1]] * 10
        hy_current = hy_df.iloc[-1][hy_df.columns[1]] * 100
        gold_implied = ((gold_price / 2000) ** 1.5) * 400
        divergence_gap = gold_implied - hy_current

        # Calculate 5d and 30d changes for divergence gap
        gap_5d_ago = None
        gap_30d_ago = None
        if len(gold_df) > 5 and len(hy_df) > 5:
            gold_5d = gold_df.iloc[-6][gold_df.columns[1]] * 10
            hy_5d = hy_df.iloc[-6][hy_df.columns[1]] * 100
            implied_5d = ((gold_5d / 2000) ** 1.5) * 400
            gap_5d_ago = implied_5d - hy_5d
        if len(gold_df) > 20 and len(hy_df) > 20:
            gold_30d = gold_df.iloc[-21][gold_df.columns[1]] * 10
            hy_30d = hy_df.iloc[-21][hy_df.columns[1]] * 100
            implied_30d = ((gold_30d / 2000) ** 1.5) * 400
            gap_30d_ago = implied_30d - hy_30d

        # Calculate historical percentile for divergence gap
        divergence_percentile = 50  # Default
        try:
            # Merge gold and HY data to calculate historical divergence series
            merged = pd.merge(gold_df, hy_df, on='date', how='inner')
            if len(merged) > 0:
                gold_col = [c for c in merged.columns if c != 'date'][0]
                hy_col = [c for c in merged.columns if c != 'date'][1]
                gold_prices_hist = merged[gold_col] * 10
                hy_spreads_hist = merged[hy_col] * 100
                gold_implied_hist = ((gold_prices_hist / 2000) ** 1.5) * 400
                divergence_hist = gold_implied_hist - hy_spreads_hist
                # Calculate percentile: what percentage of historical values are below current
                divergence_percentile = (divergence_hist < divergence_gap).sum() / len(divergence_hist) * 100
        except Exception as e:
            print(f"Error calculating divergence percentile: {e}")

        data['metrics']['divergence'] = {
            'gap': round(divergence_gap, 0),
            'gold_implied_spread': round(gold_implied, 0),
            'actual_spread': round(hy_current, 0),
            'gap_change_5d': round(divergence_gap - gap_5d_ago, 0) if gap_5d_ago is not None else 0,
            'gap_change_30d': round(divergence_gap - gap_30d_ago, 0) if gap_30d_ago is not None else 0,
            'percentile': round(divergence_percentile, 1)
        }

    # VIX
    if vix_df is not None:
        vix_current = vix_df.iloc[-1][vix_df.columns[1]]
        vix_returns = calculate_returns(vix_df, vix_df.columns[1], [5, 20])
        # Calculate VIX percentile
        vix_values = vix_df[vix_df.columns[1]].dropna()
        vix_percentile = (vix_values < vix_current).sum() / len(vix_values) * 100
        data['metrics']['vix'] = {
            'current': round(vix_current, 2),
            'change_5d': round(vix_returns.get('5d', 0), 2),
            'change_30d': round(vix_returns.get('20d', 0), 2),
            'status': 'low' if vix_current < 16 else 'normal' if vix_current < 25 else 'elevated',
            'percentile': round(vix_percentile, 1)
        }

    # CCC/HY Ratio
    if ccc_df is not None and hy_df is not None:
        ccc_current = ccc_df.iloc[-1][ccc_df.columns[1]] * 100
        hy_current = hy_df.iloc[-1][hy_df.columns[1]] * 100
        ratio = ccc_current / hy_current

        # Calculate 5d and 30d changes for ratio
        ratio_5d_ago = None
        ratio_30d_ago = None
        if len(ccc_df) > 5 and len(hy_df) > 5:
            ccc_5d = ccc_df.iloc[-6][ccc_df.columns[1]] * 100
            hy_5d = hy_df.iloc[-6][hy_df.columns[1]] * 100
            ratio_5d_ago = ccc_5d / hy_5d if hy_5d > 0 else None
        if len(ccc_df) > 20 and len(hy_df) > 20:
            ccc_30d = ccc_df.iloc[-21][ccc_df.columns[1]] * 100
            hy_30d = hy_df.iloc[-21][hy_df.columns[1]] * 100
            ratio_30d_ago = ccc_30d / hy_30d if hy_30d > 0 else None

        data['metrics']['ccc_hy_ratio'] = {
            'current': round(ratio, 2),
            'change_5d': round(ratio - ratio_5d_ago, 2) if ratio_5d_ago else 0,
            'change_30d': round(ratio - ratio_30d_ago, 2) if ratio_30d_ago else 0
        }

    # SPY
    if spy_df is not None:
        spy_price = spy_df.iloc[-1][spy_df.columns[1]]
        spy_returns = calculate_returns(spy_df, spy_df.columns[1], [5, 20])
        data['metrics']['spy'] = {
            'price': round(spy_price, 2),
            'change_5d': round(spy_returns.get('5d', 0), 2),
            'change_30d': round(spy_returns.get('20d', 0), 2)
        }

    # Market Concentration Ratios
    smh_spy_df = load_csv_data('smh_spy_ratio.csv')
    xlk_spy_df = load_csv_data('xlk_spy_ratio.csv')
    growth_value_df = load_csv_data('growth_value_ratio.csv')

    concentration_metrics = {}

    if smh_spy_df is not None and len(smh_spy_df) > 0:
        smh_spy_current = smh_spy_df.iloc[-1][smh_spy_df.columns[1]]
        smh_spy_returns = calculate_returns(smh_spy_df, smh_spy_df.columns[1], [5, 20])
        # Calculate percentile
        all_values = smh_spy_df[smh_spy_df.columns[1]].dropna()
        percentile = (all_values < smh_spy_current).sum() / len(all_values) * 100
        concentration_metrics['smh_spy'] = {
            'current': round(smh_spy_current, 2),
            'change_5d': round(smh_spy_returns.get('5d', 0), 2),
            'change_30d': round(smh_spy_returns.get('20d', 0), 2),
            'percentile': round(percentile, 1),
            'label': 'Semiconductor/SPY'
        }

    if xlk_spy_df is not None and len(xlk_spy_df) > 0:
        xlk_spy_current = xlk_spy_df.iloc[-1][xlk_spy_df.columns[1]]
        xlk_spy_returns = calculate_returns(xlk_spy_df, xlk_spy_df.columns[1], [5, 20])
        all_values = xlk_spy_df[xlk_spy_df.columns[1]].dropna()
        percentile = (all_values < xlk_spy_current).sum() / len(all_values) * 100
        concentration_metrics['xlk_spy'] = {
            'current': round(xlk_spy_current, 2),
            'change_5d': round(xlk_spy_returns.get('5d', 0), 2),
            'change_30d': round(xlk_spy_returns.get('20d', 0), 2),
            'percentile': round(percentile, 1),
            'label': 'Tech Sector/SPY'
        }

    if growth_value_df is not None and len(growth_value_df) > 0:
        gv_current = growth_value_df.iloc[-1][growth_value_df.columns[1]]
        gv_returns = calculate_returns(growth_value_df, growth_value_df.columns[1], [5, 20])
        all_values = growth_value_df[growth_value_df.columns[1]].dropna()
        percentile = (all_values < gv_current).sum() / len(all_values) * 100
        concentration_metrics['growth_value'] = {
            'current': round(gv_current, 2),
            'change_5d': round(gv_returns.get('5d', 0), 2),
            'change_30d': round(gv_returns.get('20d', 0), 2),
            'percentile': round(percentile, 1),
            'label': 'Growth/Value'
        }

    if concentration_metrics:
        data['metrics']['concentration'] = concentration_metrics

    # Yen Carry Trade Metrics
    usdjpy_df = load_csv_data('usdjpy_price.csv')
    japan_10y_df = load_csv_data('japan_10y_yield.csv')

    carry_trade_metrics = {}

    if usdjpy_df is not None and len(usdjpy_df) > 0:
        usdjpy_current = usdjpy_df.iloc[-1][usdjpy_df.columns[1]]
        usdjpy_returns = calculate_returns(usdjpy_df, usdjpy_df.columns[1], [5, 20])
        all_values = usdjpy_df[usdjpy_df.columns[1]].dropna()
        percentile = (all_values < usdjpy_current).sum() / len(all_values) * 100
        carry_trade_metrics['usdjpy'] = {
            'current': round(usdjpy_current, 2),
            'change_5d': round(usdjpy_returns.get('5d', 0), 2),
            'change_30d': round(usdjpy_returns.get('20d', 0), 2),
            'percentile': round(percentile, 1),
            'label': 'USD/JPY'
        }

    if japan_10y_df is not None and len(japan_10y_df) > 0:
        japan_10y_current = japan_10y_df.iloc[-1][japan_10y_df.columns[1]]
        japan_10y_returns = calculate_returns(japan_10y_df, japan_10y_df.columns[1], [5, 20])
        all_values = japan_10y_df[japan_10y_df.columns[1]].dropna()
        percentile = (all_values < japan_10y_current).sum() / len(all_values) * 100
        carry_trade_metrics['japan_10y'] = {
            'current': round(japan_10y_current, 2),
            'change_5d': round(japan_10y_returns.get('5d', 0), 2),
            'change_30d': round(japan_10y_returns.get('20d', 0), 2),
            'percentile': round(percentile, 1),
            'label': 'Japan 10Y Yield'
        }

    if carry_trade_metrics:
        data['metrics']['carry_trade'] = carry_trade_metrics

    # Economic Indicators
    consumer_confidence_df = load_csv_data('consumer_confidence.csv')
    m2_df = load_csv_data('m2_money_supply.csv')
    cpi_df = load_csv_data('cpi.csv')

    economic_indicators = {}

    if consumer_confidence_df is not None and len(consumer_confidence_df) > 0:
        cc_current = consumer_confidence_df.iloc[-1][consumer_confidence_df.columns[1]]
        cc_returns = calculate_returns(consumer_confidence_df, consumer_confidence_df.columns[1], [20])
        all_values = consumer_confidence_df[consumer_confidence_df.columns[1]].dropna()
        percentile = (all_values < cc_current).sum() / len(all_values) * 100
        economic_indicators['consumer_confidence'] = {
            'current': round(cc_current, 1),
            'change_30d': round(cc_returns.get('20d', 0), 2),
            'percentile': round(percentile, 1),
            'label': 'Consumer Confidence'
        }

    if m2_df is not None and len(m2_df) > 12:
        m2_current = m2_df.iloc[-1][m2_df.columns[1]]
        # Calculate YoY change (M2 is monthly, so 12 periods back)
        if len(m2_df) > 12:
            m2_year_ago = m2_df.iloc[-13][m2_df.columns[1]]
            m2_yoy = ((m2_current / m2_year_ago) - 1) * 100
        else:
            m2_yoy = 0
        all_values = m2_df[m2_df.columns[1]].dropna()
        percentile = (all_values < m2_current).sum() / len(all_values) * 100
        economic_indicators['m2_money_supply'] = {
            'current': round(m2_current / 1000, 2),  # Convert to trillions
            'yoy_change': round(m2_yoy, 1),
            'percentile': round(percentile, 1),
            'label': 'M2 Money Supply'
        }

    if cpi_df is not None and len(cpi_df) > 12:
        cpi_current = cpi_df.iloc[-1][cpi_df.columns[1]]
        # Calculate YoY change (CPI is monthly, so 12 periods back)
        if len(cpi_df) > 12:
            cpi_year_ago = cpi_df.iloc[-13][cpi_df.columns[1]]
            cpi_yoy = ((cpi_current / cpi_year_ago) - 1) * 100
        else:
            cpi_yoy = 0
        all_values = cpi_df[cpi_df.columns[1]].dropna()
        percentile = (all_values < cpi_current).sum() / len(all_values) * 100
        economic_indicators['cpi'] = {
            'current': round(cpi_current, 1),
            'yoy_change': round(cpi_yoy, 1),
            'percentile': round(percentile, 1),
            'label': 'CPI'
        }

    if economic_indicators:
        data['metrics']['economic_indicators'] = economic_indicators

    # Calculate top movers
    data['top_movers'] = calculate_top_movers(5)

    # ========================================
    # Market Conditions Grid Data (US-1.1.2)
    # ========================================
    market_grid = {}

    # Credit Section (HY Spread already loaded, add IG Spread)
    credit_data = {}
    if hy_df is not None and len(hy_df) > 0:
        hy_val = hy_df.iloc[-1][hy_df.columns[1]] * 100
        hy_5d_val = hy_df.iloc[max(0, len(hy_df)-6)][hy_df.columns[1]] * 100 if len(hy_df) > 5 else hy_val
        credit_data['hy_spread'] = {
            'value': round(hy_val, 0),
            'change_5d': round(hy_val - hy_5d_val, 0)
        }
    if ig_df is not None and len(ig_df) > 0:
        ig_val = ig_df.iloc[-1][ig_df.columns[1]] * 100
        ig_5d_val = ig_df.iloc[max(0, len(ig_df)-6)][ig_df.columns[1]] * 100 if len(ig_df) > 5 else ig_val
        credit_data['ig_spread'] = {
            'value': round(ig_val, 0),
            'change_5d': round(ig_val - ig_5d_val, 0)
        }
    if ccc_df is not None and hy_df is not None and len(ccc_df) > 0 and len(hy_df) > 0:
        ccc_val = ccc_df.iloc[-1][ccc_df.columns[1]] * 100
        hy_val = hy_df.iloc[-1][hy_df.columns[1]] * 100
        ratio = ccc_val / hy_val if hy_val > 0 else 0
        ratio_5d = None
        if len(ccc_df) > 5 and len(hy_df) > 5:
            ccc_5d = ccc_df.iloc[-6][ccc_df.columns[1]] * 100
            hy_5d = hy_df.iloc[-6][hy_df.columns[1]] * 100
            ratio_5d = ccc_5d / hy_5d if hy_5d > 0 else ratio
        credit_data['ccc_ratio'] = {
            'value': round(ratio, 2),
            'change_5d': round(ratio - ratio_5d, 2) if ratio_5d else 0
        }
    market_grid['credit'] = credit_data

    # Equities Section (SPY, VIX already loaded, add Russell 2000)
    equities_data = {}
    if spy_df is not None and len(spy_df) > 0:
        spy_val = spy_df.iloc[-1][spy_df.columns[1]]
        spy_returns = calculate_returns(spy_df, spy_df.columns[1], [5])
        equities_data['spy'] = {
            'value': round(spy_val, 2),
            'change_5d': round(spy_returns.get('5d', 0), 2)
        }
    russell_df = load_csv_data('small_cap_price.csv')  # IWM - Russell 2000 ETF
    if russell_df is not None and len(russell_df) > 0:
        russell_val = russell_df.iloc[-1][russell_df.columns[1]]
        russell_returns = calculate_returns(russell_df, russell_df.columns[1], [5])
        equities_data['russell'] = {
            'value': round(russell_val, 2),
            'change_5d': round(russell_returns.get('5d', 0), 2)
        }
    if vix_df is not None and len(vix_df) > 0:
        vix_val = vix_df.iloc[-1][vix_df.columns[1]]
        vix_returns = calculate_returns(vix_df, vix_df.columns[1], [5])
        equities_data['vix'] = {
            'value': round(vix_val, 2),
            'change_5d': round(vix_returns.get('5d', 0), 2)
        }
    market_grid['equities'] = equities_data

    # Rates Section (10Y, 2Y, Curve)
    rates_data = {}
    treasury_10y_df = load_csv_data('treasury_10y.csv')
    yield_curve_df = load_csv_data('yield_curve_10y2y.csv')
    if treasury_10y_df is not None and len(treasury_10y_df) > 0:
        t10y_val = treasury_10y_df.iloc[-1][treasury_10y_df.columns[1]]
        t10y_5d_val = treasury_10y_df.iloc[max(0, len(treasury_10y_df)-6)][treasury_10y_df.columns[1]] if len(treasury_10y_df) > 5 else t10y_val
        rates_data['treasury_10y'] = {
            'value': round(t10y_val, 2),
            'change_5d': round((t10y_val - t10y_5d_val) * 100, 0)  # Change in bp
        }
    # Calculate 2Y from 10Y and curve spread
    if yield_curve_df is not None and len(yield_curve_df) > 0 and treasury_10y_df is not None:
        curve_val = yield_curve_df.iloc[-1][yield_curve_df.columns[1]]
        curve_5d_val = yield_curve_df.iloc[max(0, len(yield_curve_df)-6)][yield_curve_df.columns[1]] if len(yield_curve_df) > 5 else curve_val
        t10y_val = treasury_10y_df.iloc[-1][treasury_10y_df.columns[1]]
        # 2Y = 10Y - curve_spread
        t2y_val = t10y_val - curve_val
        t2y_5d_val = (treasury_10y_df.iloc[max(0, len(treasury_10y_df)-6)][treasury_10y_df.columns[1]] if len(treasury_10y_df) > 5 else t10y_val) - curve_5d_val
        rates_data['treasury_2y'] = {
            'value': round(t2y_val, 2),
            'change_5d': round((t2y_val - t2y_5d_val) * 100, 0)  # Change in bp
        }
        # Use the curve data directly
        rates_data['curve'] = {
            'value': round(curve_val, 2),
            'change_5d': round((curve_val - curve_5d_val) * 100, 0)  # Change in bp
        }
    market_grid['rates'] = rates_data

    # Safe Havens Section (Gold already loaded, add Silver, TLT)
    havens_data = {}
    if gold_df is not None and len(gold_df) > 0:
        gold_val = gold_df.iloc[-1][gold_df.columns[1]] * 10
        gold_returns = calculate_returns(gold_df, gold_df.columns[1], [5, 20])
        havens_data['gold'] = {
            'value': round(gold_val, 0),
            'change_5d': round(gold_returns.get('5d', 0), 2),
            'change_30d': round(gold_returns.get('20d', 0), 2)
        }
    silver_df = load_csv_data('silver_price.csv')
    if silver_df is not None and len(silver_df) > 0:
        silver_val = silver_df.iloc[-1][silver_df.columns[1]]
        silver_returns = calculate_returns(silver_df, silver_df.columns[1], [5])
        havens_data['silver'] = {
            'value': round(silver_val, 2),
            'change_5d': round(silver_returns.get('5d', 0), 2)
        }
    tlt_df = load_csv_data('treasury_20yr_price.csv')
    if tlt_df is not None and len(tlt_df) > 0:
        tlt_val = tlt_df.iloc[-1][tlt_df.columns[1]]
        tlt_returns = calculate_returns(tlt_df, tlt_df.columns[1], [5])
        havens_data['tlt'] = {
            'value': round(tlt_val, 2),
            'change_5d': round(tlt_returns.get('5d', 0), 2)
        }
    market_grid['havens'] = havens_data

    # Crypto Section (Bitcoin already loaded, add Ethereum, BTC Dominance)
    crypto_data = {}
    if btc_df is not None and len(btc_df) > 0:
        btc_val = btc_df.iloc[-1][btc_df.columns[1]]
        btc_returns = calculate_returns(btc_df, btc_df.columns[1], [5, 20])
        crypto_data['btc'] = {
            'value': round(btc_val, 0),
            'change_5d': round(btc_returns.get('5d', 0), 2),
            'change_30d': round(btc_returns.get('20d', 0), 2)
        }
    eth_df = load_csv_data('ethereum_price.csv')
    if eth_df is not None and len(eth_df) > 0:
        eth_val = eth_df.iloc[-1][eth_df.columns[1]]
        eth_returns = calculate_returns(eth_df, eth_df.columns[1], [5])
        crypto_data['eth'] = {
            'value': round(eth_val, 0),
            'change_5d': round(eth_returns.get('5d', 0), 2)
        }
    btc_dom_df = load_csv_data('btc_dominance.csv')
    if btc_dom_df is not None and len(btc_dom_df) > 0:
        btc_dom_val = btc_dom_df.iloc[-1][btc_dom_df.columns[1]]
        btc_dom_returns = calculate_returns(btc_dom_df, btc_dom_df.columns[1], [5])
        crypto_data['btc_dominance'] = {
            'value': round(btc_dom_val, 1),
            'change_5d': round(btc_dom_returns.get('5d', 0), 2)
        }
    market_grid['crypto'] = crypto_data

    # Dollar Section (add DXY, EUR/USD, USD/JPY)
    dollar_data = {}
    dxy_df = load_csv_data('dollar_index_price.csv')
    if dxy_df is not None and len(dxy_df) > 0:
        dxy_val = dxy_df.iloc[-1][dxy_df.columns[1]]
        dxy_returns = calculate_returns(dxy_df, dxy_df.columns[1], [5])
        dollar_data['dxy'] = {
            'value': round(dxy_val, 2),
            'change_5d': round(dxy_returns.get('5d', 0), 2)
        }
    eurusd_df = load_csv_data('eurusd_price.csv')
    if eurusd_df is not None and len(eurusd_df) > 0:
        eurusd_val = eurusd_df.iloc[-1][eurusd_df.columns[1]]
        eurusd_returns = calculate_returns(eurusd_df, eurusd_df.columns[1], [5])
        dollar_data['eurusd'] = {
            'value': round(eurusd_val, 4),
            'change_5d': round(eurusd_returns.get('5d', 0), 2)
        }
    if usdjpy_df is not None and len(usdjpy_df) > 0:
        usdjpy_val = usdjpy_df.iloc[-1][usdjpy_df.columns[1]]
        usdjpy_returns = calculate_returns(usdjpy_df, usdjpy_df.columns[1], [5])
        dollar_data['usdjpy'] = {
            'value': round(usdjpy_val, 2),
            'change_5d': round(usdjpy_returns.get('5d', 0), 2)
        }
    market_grid['dollar'] = dollar_data

    data['market_grid'] = market_grid

    return data


# =============================================================================
# Authentication Routes
# =============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """User login page."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember', False)

        if not username or not password:
            flash('Please enter both username and password.', 'danger')
            return render_template('auth/login.html')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash('This account has been deactivated.', 'danger')
                return render_template('auth/login.html')

            login_user(user, remember=bool(remember))
            user.update_last_login()
            db.session.commit()

            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('index'))

        flash('Invalid username or password.', 'danger')

    return render_template('auth/login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """User registration page."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        invite_code = request.form.get('invite_code', '').strip()

        errors = []

        # Invite code validation (when configured)
        expected_code = app.config.get('INVITE_CODE', '')
        if expected_code and not hmac.compare_digest(invite_code, expected_code):
            errors.append('Registration is currently invite-only. Contact us for access.')

        # Validation
        if not username:
            errors.append('Username is required.')
        elif len(username) < 3:
            errors.append('Username must be at least 3 characters.')
        elif len(username) > 80:
            errors.append('Username must be less than 80 characters.')
        elif User.query.filter_by(username=username).first():
            errors.append('Username already exists.')

        if not email:
            errors.append('Email is required.')
        elif User.query.filter_by(email=email).first():
            errors.append('Email already registered.')

        if not password:
            errors.append('Password is required.')
        elif len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        elif not any(c.isupper() for c in password):
            errors.append('Password must contain at least one uppercase letter.')
        elif not any(c.isdigit() for c in password):
            errors.append('Password must contain at least one number.')

        if password != confirm_password:
            errors.append('Passwords do not match.')

        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('auth/register.html')

        # Create user
        user = User(username=username, email=email)
        user.set_password(password)

        # Create default alert preferences
        from models.alert import AlertPreference
        alert_prefs = AlertPreference(user=user)

        db.session.add(user)
        db.session.add(alert_prefs)
        db.session.commit()

        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('auth/register.html')


@app.route('/logout')
@login_required
def logout():
    """Log out current user."""
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


# =============================================================================
# Page Routes
# =============================================================================

def _get_trade_balance_context():
    """Build the template context dict for the Global Trade Pulse panel (US-206.1).

    Loads trade_balance.csv, computes current value, reading period, YoY change,
    10-year rolling percentile, and trade condition, then looks up the
    interpretation copy.

    Returns a dict with all trade_* keys; values default to None on any error.
    """
    ctx = {
        'trade_balance_value': None,
        'trade_balance_period': None,
        'trade_yoy_change': None,
        'trade_yoy_direction': None,
        'trade_percentile': None,
        'trade_condition': None,
        'trade_interpretation_label': None,
        'trade_interpretation_body': None,
        'trade_last_updated': None,
    }
    try:
        df = load_csv_data('trade_balance.csv')
        if df is None or len(df) < 2:
            return ctx

        df = df.dropna(subset=['trade_balance'])
        if len(df) < 2:
            return ctx

        df = df.sort_values('date').reset_index(drop=True)
        df['date'] = pd.to_datetime(df['date'])

        current_row = df.iloc[-1]
        current_value = float(current_row['trade_balance'])
        current_date = current_row['date']

        ctx['trade_balance_value'] = round(current_value, 1)
        ctx['trade_balance_period'] = current_date.strftime('%b %Y')
        ctx['trade_last_updated'] = current_date.strftime('%b %Y')

        # YoY: find the row with the same month in the prior year
        prior_year = current_date.year - 1
        prior_month = current_date.month
        prior_rows = df[(df['date'].dt.year == prior_year) & (df['date'].dt.month == prior_month)]
        if not prior_rows.empty:
            prior_value = float(prior_rows.iloc[-1]['trade_balance'])
            yoy_change = round(current_value - prior_value, 1)
            ctx['trade_yoy_change'] = yoy_change
            ctx['trade_yoy_direction'] = 'up' if yoy_change >= 0 else 'down'
        else:
            yoy_change = None

        # 10-year rolling percentile
        series = df.set_index('date')['trade_balance']
        cutoff = pd.Timestamp.today() - pd.DateOffset(years=10)
        try:
            windowed = series[series.index >= cutoff]
            if len(windowed) < 2:
                windowed = series
        except TypeError:
            windowed = series

        count_below = int((windowed < current_value).sum())
        percentile = round(float(count_below / len(windowed) * 100), 1)
        ctx['trade_percentile'] = percentile

        # Trade condition
        if yoy_change is not None:
            if current_value < 0:
                condition = 'widening_deficit' if yoy_change < 0 else 'narrowing_deficit'
            else:
                condition = 'widening_surplus' if yoy_change > 0 else 'narrowing_surplus'
            ctx['trade_condition'] = condition
        else:
            condition = None

        label, body = get_trade_interpretation(None, condition, percentile)
        ctx['trade_interpretation_label'] = label
        ctx['trade_interpretation_body'] = body

    except Exception:
        pass  # Graceful empty state — missing CSV returns None values

    return ctx


@app.route('/')
def index():
    """Main dashboard page (US-323.1 redesign)."""
    ctx = _get_trade_balance_context()

    # Market conditions data for §1 hero card + dimension cards
    try:
        conditions = get_market_conditions()
    except Exception:
        conditions = None
    ctx['conditions'] = conditions

    # Trajectory trail for quadrant visualization (last 6 monthly entries)
    trajectory = []
    try:
        history = get_conditions_history()
        if history:
            sorted_dates = sorted(history.keys(), reverse=True)
            for dt_str in sorted_dates[:6]:
                entry = history[dt_str]
                # Prefer top-level scores (bug #337), fall back to nested
                dims = entry.get('dimensions', {})
                quad_dims = dims.get('quadrant', {})
                gs = entry.get('growth_score')
                gc = gs if gs is not None else quad_dims.get('growth_composite')
                ics = entry.get('inflation_score')
                ic = ics if ics is not None else quad_dims.get('inflation_composite')
                if gc is not None and ic is not None:
                    trajectory.append({
                        'date': dt_str,
                        'growth': gc,
                        'inflation': ic,
                        'quadrant': entry.get('quadrant', ''),
                    })
    except Exception:
        pass
    ctx['quadrant_trajectory'] = trajectory

    # Liquidity sparkline points for expanded card (last 14 weekly entries)
    liq_sparkline_points = ''
    try:
        if not history:
            history = get_conditions_history()
        if history:
            sorted_dates = sorted(history.keys())
            liq_scores = []
            for dt_str in sorted_dates:
                entry = history[dt_str]
                dims = entry.get('dimensions', {})
                liq_dim = dims.get('liquidity', {})
                sc = liq_dim.get('score')
                if sc is not None:
                    liq_scores.append(sc)
            # Take last 14 entries (weekly cadence approximation from daily snapshots)
            liq_scores = liq_scores[-14:]
            if len(liq_scores) >= 2:
                min_s = min(liq_scores)
                max_s = max(liq_scores)
                rng = max_s - min_s if max_s != min_s else 1.0
                pts = []
                for i, s in enumerate(liq_scores):
                    x = round(i / (len(liq_scores) - 1) * 100, 1)
                    y = round((1 - (s - min_s) / rng) * 32, 1)
                    pts.append(f'{x},{y}')
                liq_sparkline_points = ' '.join(pts)
    except Exception:
        pass
    ctx['liq_sparkline_points'] = liq_sparkline_points

    # Recession probability summary for Risk expand card
    try:
        recession = get_recession_probability()
        if recession:
            probs = [recession.get(k) for k in ('ny_fed', 'chauvet_piger', 'richmond_sos')
                     if recession.get(k) is not None]
            ctx['recession_highest'] = round(max(probs), 1) if probs else None
        else:
            ctx['recession_highest'] = None
    except Exception:
        ctx['recession_highest'] = None

    # §2 Portfolio Implications matrix (per-dimension signal breakdown)
    implications = []
    try:
        if conditions:
            dims = conditions.get('dimensions', {})
            quad = conditions.get('quadrant', 'Goldilocks')
            liq_state = dims.get('liquidity', {}).get('state', 'Neutral')
            risk_state = dims.get('risk', {}).get('state', 'Normal')
            pol_dir = dims.get('policy', {}).get('direction', 'Paused')
            implications = build_implications_matrix(quad, liq_state, risk_state, pol_dir)
    except Exception:
        pass
    ctx['implications'] = implications

    # Historical context sentence for §2
    ctx['implications_context'] = ''
    try:
        if conditions and trajectory:
            quad = conditions.get('quadrant', '')
            liq_dims = conditions.get('dimensions', {}).get('liquidity', {})
            liq_state = liq_dims.get('state', '')
            # Count matching periods in history
            matching = 0
            if history:
                for entry in history.values():
                    e_quad = entry.get('quadrant', entry.get('raw_quadrant', ''))
                    e_liq = entry.get('dimensions', {}).get('liquidity', {}).get('state', '')
                    if e_quad == quad and e_liq == liq_state:
                        matching += 1
            if matching > 1:
                ctx['implications_context'] = (
                    f"In prior {quad} + {liq_state} Liquidity periods "
                    f"(n={matching} since 2003), conditions have historically "
                    f"favored risk assets when sustained for 3+ months."
                )
    except Exception:
        pass

    return render_template('index.html', **ctx)


@app.route('/equity')
def equity():
    """Equity markets page (US-324.1: includes relocated sector tone + trade pulse)."""
    ctx = _get_trade_balance_context()
    return render_template('equity.html', **ctx)


@app.route('/safe-havens')
def safe_havens():
    """Safe havens page."""
    return render_template('safe_havens.html')


@app.route('/credit')
def credit():
    """Credit markets page with HY/IG spread data and percentile rankings."""
    ctx = {
        'hy_percentile': None,
        'ig_percentile': None,
        'hy_percentile_label': 'unavailable',
        'ig_percentile_label': 'unavailable',
        'hy_current_bps': None,
        'ig_current_bps': None,
        'ccc_current_bps': None,
        'data_date': None,
    }
    try:
        hy_df = load_csv_data('high_yield_spread.csv')
        ig_df = load_csv_data('investment_grade_spread.csv')
        ccc_df = load_csv_data('ccc_spread.csv')

        if hy_df is not None and len(hy_df) > 1:
            hy_df = hy_df.dropna(subset=['high_yield_spread'])
            hy_df = hy_df.set_index('date')
            hy_series = hy_df['high_yield_spread']
            hy_current = float(hy_series.iloc[-1])
            ctx['hy_current_bps'] = round(hy_current * 100)
            ctx['hy_percentile'] = round(calculate_percentile_rank(hy_series, hy_current), 1)
            ctx['hy_percentile_label'] = _percentile_label(ctx['hy_percentile'])
            ctx['data_date'] = hy_df.index[-1].strftime('%B %d, %Y')

        if ig_df is not None and len(ig_df) > 1:
            ig_df = ig_df.dropna(subset=['investment_grade_spread'])
            ig_df = ig_df.set_index('date')
            ig_series = ig_df['investment_grade_spread']
            ig_current = float(ig_series.iloc[-1])
            ctx['ig_current_bps'] = round(ig_current * 100)
            ctx['ig_percentile'] = round(calculate_percentile_rank(ig_series, ig_current), 1)
            ctx['ig_percentile_label'] = _percentile_label(ctx['ig_percentile'])

        if ccc_df is not None and len(ccc_df) > 1:
            ccc_df = ccc_df.dropna(subset=['ccc_spread'])
            ccc_current = float(ccc_df['ccc_spread'].iloc[-1])
            ctx['ccc_current_bps'] = round(ccc_current * 100)

    except Exception:
        pass  # Graceful empty state — missing CSV returns None values

    try:
        interpretation, spread_bucket = get_credit_interpretation(
            None, ctx['hy_percentile']
        )
        ctx['credit_interpretation'] = interpretation
        ctx['credit_interpretation_bucket'] = spread_bucket
    except Exception:
        ctx['credit_interpretation'] = None
        ctx['credit_interpretation_bucket'] = None

    return render_template('credit.html', **ctx)


@app.route('/divergence')
def divergence():
    """Redirect old divergence page to credit page for backward compatibility."""
    return redirect(url_for('credit'), code=301)


@app.route('/crypto')
def crypto():
    """Crypto/Bitcoin analysis page."""
    return render_template('crypto.html')


@app.route('/rates')
def rates():
    """Rates & Yield Curve page."""
    return render_template('rates.html')


@app.route('/dollar')
def dollar():
    """Dollar & Currency page."""
    return render_template('dollar.html')


@app.route('/property')
def property_macro():
    """Property macro page — residential real estate and farmland indicators."""
    ctx = {
        # Case-Shiller HPI
        'hpi_current': None,
        'hpi_yoy_pct': None,
        'hpi_percentile': None,
        'hpi_percentile_label': 'unavailable',
        # CPI Rent
        'cpi_rent_current': None,
        'cpi_rent_yoy_pct': None,
        'cpi_rent_mom_pct': None,
        'cpi_rent_percentile': None,
        'cpi_rent_percentile_label': 'unavailable',
        # Rental Vacancy Rate
        'vacancy_current': None,
        'vacancy_prior': None,
        'vacancy_direction': None,  # 'tightening' | 'loosening'
        'vacancy_percentile': None,
        'vacancy_percentile_label': 'unavailable',
        # USDA Farmland
        'farmland_year': None,
        'farmland_farm_re': None,
        'farmland_cropland': None,
        'farmland_pasture': None,
        'farmland_yoy_pct': None,
        'property_interpretation': None,
        'property_interpretation_bucket': None,
        # Dates
        'hpi_date': None,
        'rent_date': None,
        'vacancy_date': None,
    }

    try:
        # Case-Shiller HPI (CSUSHPISA → stored as property_hpi.csv)
        hpi_df = load_csv_data('property_hpi.csv')
        if hpi_df is not None and len(hpi_df) >= 13:
            hpi_df = hpi_df.dropna(subset=['property_hpi'])
            hpi_df = hpi_df.set_index('date').sort_index()
            hpi_series = hpi_df['property_hpi']
            hpi_current = float(hpi_series.iloc[-1])
            hpi_year_ago = float(hpi_series.iloc[-13])  # 12 months prior
            ctx['hpi_current'] = round(hpi_current, 1)
            ctx['hpi_yoy_pct'] = round((hpi_current / hpi_year_ago - 1) * 100, 1)
            ctx['hpi_percentile'] = round(calculate_percentile_rank(hpi_series, hpi_current), 1)
            ctx['hpi_percentile_label'] = _percentile_label(ctx['hpi_percentile'])
            ctx['hpi_date'] = hpi_df.index[-1].strftime('%B %Y')
    except Exception:
        pass

    try:
        # CPI Rent (CUUR0000SEHA → stored as property_cpi_rent.csv)
        rent_df = load_csv_data('property_cpi_rent.csv')
        if rent_df is not None and len(rent_df) >= 13:
            rent_df = rent_df.dropna(subset=['property_cpi_rent'])
            rent_df = rent_df.set_index('date').sort_index()
            rent_series = rent_df['property_cpi_rent']
            rent_current = float(rent_series.iloc[-1])
            rent_year_ago = float(rent_series.iloc[-13])
            ctx['cpi_rent_current'] = round(rent_current, 3)
            ctx['cpi_rent_yoy_pct'] = round((rent_current / rent_year_ago - 1) * 100, 1)
            if len(rent_series) >= 2:
                rent_prior_month = float(rent_series.iloc[-2])
                ctx['cpi_rent_mom_pct'] = round((rent_current / rent_prior_month - 1) * 100, 2)
            ctx['cpi_rent_percentile'] = round(calculate_percentile_rank(rent_series, rent_current), 1)
            ctx['cpi_rent_percentile_label'] = _percentile_label(ctx['cpi_rent_percentile'])
            ctx['rent_date'] = rent_df.index[-1].strftime('%B %Y')
    except Exception:
        pass

    try:
        # Rental Vacancy Rate (RRVRUSQ156N → stored as property_vacancy.csv)
        vacancy_df = load_csv_data('property_vacancy.csv')
        if vacancy_df is not None and len(vacancy_df) >= 2:
            vacancy_df = vacancy_df.dropna(subset=['property_vacancy'])
            vacancy_df = vacancy_df.set_index('date').sort_index()
            vacancy_series = vacancy_df['property_vacancy']
            vacancy_current = float(vacancy_series.iloc[-1])
            vacancy_prior = float(vacancy_series.iloc[-2])
            ctx['vacancy_current'] = round(vacancy_current, 1)
            ctx['vacancy_prior'] = round(vacancy_prior, 1)
            ctx['vacancy_direction'] = 'tightening' if vacancy_current < vacancy_prior else 'loosening'
            ctx['vacancy_percentile'] = round(calculate_percentile_rank(vacancy_series, vacancy_current), 1)
            ctx['vacancy_percentile_label'] = _percentile_label(ctx['vacancy_percentile'])
            ctx['vacancy_date'] = vacancy_df.index[-1].strftime('Q%q %Y') if hasattr(vacancy_df.index[-1], 'quarter') else vacancy_df.index[-1].strftime('%B %Y')
    except Exception:
        pass

    try:
        # USDA Farmland (property_farmland.csv)
        farmland_path = DATA_DIR / 'property_farmland.csv'
        if farmland_path.exists():
            farmland_df = pd.read_csv(farmland_path)
            farmland_df['date'] = pd.to_datetime(farmland_df['date'])
            farmland_df = farmland_df.sort_values('date')
            if len(farmland_df) >= 1:
                latest = farmland_df.iloc[-1]
                ctx['farmland_year'] = int(latest['date'].year)
                ctx['farmland_farm_re'] = round(float(latest['farm_re']), 0) if pd.notna(latest.get('farm_re')) else None
                ctx['farmland_cropland'] = round(float(latest['cropland']), 0) if pd.notna(latest.get('cropland')) else None
                ctx['farmland_pasture'] = round(float(latest['pasture']), 0) if pd.notna(latest.get('pasture')) else None
            if len(farmland_df) >= 2:
                prev = farmland_df.iloc[-2]
                if pd.notna(prev.get('farm_re')) and ctx['farmland_farm_re']:
                    ctx['farmland_yoy_pct'] = round(
                        (ctx['farmland_farm_re'] / float(prev['farm_re']) - 1) * 100, 1
                    )
    except Exception:
        pass

    try:
        interpretation, hpi_trend = get_property_interpretation(
            None, ctx['hpi_yoy_pct']
        )
        ctx['property_interpretation'] = interpretation
        ctx['property_interpretation_bucket'] = hpi_trend
    except Exception:
        ctx['property_interpretation'] = None
        ctx['property_interpretation_bucket'] = None

    return render_template('property.html', **ctx)


@app.route('/news')
def news():
    """Daily macro news summary page."""
    from urllib.parse import urlparse
    from news_pipeline import get_stored_news

    stored = get_stored_news()
    today_str = _date.today().isoformat()

    if stored:
        summary_text = stored.get('summary')
        sources = stored.get('articles', [])
        record_date_str = stored.get('date', '')
        try:
            summary_date = _date.fromisoformat(record_date_str)
        except (ValueError, TypeError):
            summary_date = None
        is_stale = bool(record_date_str and record_date_str != today_str)
        stale_date = summary_date if is_stale else None
    else:
        summary_text = None
        sources = []
        summary_date = None
        is_stale = False
        stale_date = None

    # Domain extraction helper passed to template context
    def extract_domain(url):
        try:
            netloc = urlparse(url).netloc
            if netloc.startswith('www.'):
                netloc = netloc[4:]
            return netloc
        except Exception:
            return url

    return render_template(
        'news.html',
        summary_text=summary_text,
        sources=sources,
        summary_date=summary_date,
        is_stale=is_stale,
        stale_date=stale_date,
        extract_domain=extract_domain,
    )


@app.route('/portfolio')
@login_required
def portfolio():
    """Personal portfolio tracking page."""
    return render_template('portfolio.html')


@app.route('/settings')
@login_required
def settings():
    """User settings page."""
    return render_template('settings.html')


@app.route('/alerts/history')
@login_required
def alert_history():
    """Show user's alert history."""
    from models.alert import Alert

    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Query alerts for current user, newest first
    pagination = Alert.query.filter_by(user_id=current_user.id)\
        .order_by(Alert.triggered_at.desc())\
        .paginate(page=page, per_page=per_page, error_out=False)

    return render_template('alerts.html',
                         alerts=pagination.items,
                         pagination=pagination)


@app.route('/alerts/<int:alert_id>/mark-read', methods=['POST'])
@login_required
def mark_alert_read(alert_id):
    """Mark an alert as read."""
    from models.alert import Alert

    alert = Alert.query.get_or_404(alert_id)

    # Security: ensure user owns this alert
    if alert.user_id != current_user.id:
        abort(403)

    alert.read = True
    alert.read_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'success': True})


@app.route('/settings/alerts')
@login_required
def settings_alerts():
    """Alert preferences settings page."""
    # Get or create alert preferences for the current user
    prefs = current_user.alert_preferences
    if not prefs:
        prefs = current_user.create_default_alert_preferences()

    # Get list of common timezones
    timezones = [
        'America/New_York',
        'America/Chicago',
        'America/Denver',
        'America/Los_Angeles',
        'America/Anchorage',
        'Pacific/Honolulu',
        'Europe/London',
        'Europe/Paris',
        'Asia/Tokyo',
        'Asia/Shanghai',
        'Asia/Dubai',
        'Australia/Sydney',
    ]

    return render_template('settings_alerts.html', prefs=prefs, timezones=timezones)


@app.route('/settings/alerts/save', methods=['POST'])
@login_required
def save_alert_settings():
    """Save alert preferences."""
    try:
        # Get or create alert preferences
        prefs = current_user.alert_preferences
        if not prefs:
            prefs = current_user.create_default_alert_preferences()

        # Daily briefing settings
        prefs.daily_briefing_enabled = request.form.get('daily_briefing_enabled') == 'on'
        prefs.briefing_frequency = request.form.get('briefing_frequency', 'daily')

        # Parse time
        time_str = request.form.get('briefing_time', '07:00')
        prefs.briefing_time = datetime.strptime(time_str, '%H:%M').time()

        prefs.briefing_timezone = request.form.get('briefing_timezone', 'America/New_York')
        prefs.include_portfolio_analysis = request.form.get('include_portfolio_analysis') == 'on'

        # Alert settings
        prefs.alerts_enabled = request.form.get('alerts_enabled') == 'on'

        # 3-layer toggles (US-237.3)
        prefs.layer_1_enabled = request.form.get('layer_1_enabled') == 'on'
        prefs.layer_2_enabled = request.form.get('layer_2_enabled') == 'on'
        prefs.layer_3_enabled = request.form.get('layer_3_enabled') == 'on'

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Alert preferences saved successfully!'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error saving preferences: {str(e)}'
        }), 500


@app.route('/unsubscribe/alerts/<int:user_id>')
def unsubscribe_alerts(user_id):
    """Unsubscribe from alert emails."""
    from models.user import User

    user = User.query.get_or_404(user_id)
    prefs = user.alert_preferences

    if prefs:
        prefs.alerts_enabled = False
        db.session.commit()

    return render_template('unsubscribe_success.html', email_type='alert notifications')


@app.route('/unsubscribe/briefing/<user_id>')
def unsubscribe_briefing(user_id):
    """Unsubscribe from daily briefing emails."""
    from models.user import User
    from models.alert import AlertPreference

    user = User.query.get_or_404(user_id)
    prefs = user.alert_preferences

    if prefs:
        prefs.daily_briefing_enabled = False
        db.session.commit()

    return render_template('unsubscribe_success.html', email_type='daily briefing')


@app.route('/api/dashboard')
def api_dashboard():
    """API endpoint for dashboard data."""
    return jsonify(get_dashboard_data())


@app.route('/api/chart/<chart_type>')
def api_chart(chart_type):
    """API endpoint for chart data."""

    if chart_type == 'divergence_gap':
        hy_df = load_csv_data('high_yield_spread.csv')
        gold_df = load_csv_data('gold_price.csv')

        if hy_df is not None and gold_df is not None:
            # Merge on date
            merged = pd.merge(hy_df, gold_df, on='date', how='inner')

            dates = merged['date'].dt.strftime('%Y-%m-%d').tolist()
            hy_spreads = (merged[hy_df.columns[1]] * 100).tolist()
            gold_prices = (merged[gold_df.columns[1]] * 10).tolist()
            gold_implied = [((g / 2000) ** 1.5) * 400 for g in gold_prices]
            divergence = [impl - actual for impl, actual in zip(gold_implied, hy_spreads)]

            return jsonify({
                'dates': dates,
                'actual_spread': hy_spreads,
                'implied_spread': gold_implied,
                'divergence_gap': divergence
            })

    elif chart_type == 'credit_spreads':
        hy_df = load_csv_data('high_yield_spread.csv')
        ig_df = load_csv_data('investment_grade_spread.csv')
        ccc_df = load_csv_data('ccc_spread.csv')

        if hy_df is not None:
            dates = hy_df['date'].dt.strftime('%Y-%m-%d').tolist()
            hy_data = (hy_df[hy_df.columns[1]] * 100).tolist()

            result = {
                'dates': dates,
                'hy_spread': hy_data
            }

            if ig_df is not None:
                ig_data = (ig_df[ig_df.columns[1]] * 100).tolist()
                result['ig_spread'] = ig_data

            if ccc_df is not None:
                ccc_data = (ccc_df[ccc_df.columns[1]] * 100).tolist()
                result['ccc_spread'] = ccc_data

            return jsonify(result)

    elif chart_type == 'gold_bitcoin':
        gold_df = load_csv_data('gold_price.csv')
        btc_df = load_csv_data('bitcoin_price.csv')

        if gold_df is not None and btc_df is not None:
            # Merge on date
            merged = pd.merge(gold_df, btc_df, on='date', how='inner')

            dates = merged['date'].dt.strftime('%Y-%m-%d').tolist()
            gold_data = (merged[gold_df.columns[1]] * 10).tolist()
            btc_data = merged[btc_df.columns[1]].tolist()

            return jsonify({
                'dates': dates,
                'gold': gold_data,
                'bitcoin': btc_data
            })

    elif chart_type == 'equity_indices':
        spy_df = load_csv_data('sp500_price.csv')
        qqq_df = load_csv_data('nasdaq_price.csv')
        iwm_df = load_csv_data('small_cap_price.csv')

        if spy_df is not None:
            dates = spy_df['date'].dt.strftime('%Y-%m-%d').tolist()

            result = {'dates': dates}

            if spy_df is not None:
                result['spy'] = spy_df[spy_df.columns[1]].tolist()
            if qqq_df is not None:
                result['qqq'] = qqq_df[qqq_df.columns[1]].tolist()
            if iwm_df is not None:
                result['iwm'] = iwm_df[iwm_df.columns[1]].tolist()

            return jsonify(result)

    elif chart_type == 'vix':
        vix_df = load_csv_data('vix_price.csv')

        if vix_df is not None:
            dates = vix_df['date'].dt.strftime('%Y-%m-%d').tolist()
            vix_data = vix_df[vix_df.columns[1]].tolist()

            return jsonify({
                'dates': dates,
                'vix': vix_data
            })

    return jsonify({'error': 'Chart type not found'}), 404


@app.route('/metric/<metric_name>')
def metric_detail(metric_name):
    """Redirect old metric detail URLs to the explorer page."""
    # Map old friendly names to the actual metric names used by explorer
    metric_map = {
        'divergence_gap': 'divergence_gap',
        'hy_spread': 'high_yield_spread',
        'ig_spread': 'investment_grade_spread',
        'ccc_spread': 'ccc_spread',
        'gold_price': 'gold_price',
        'bitcoin_price': 'bitcoin_price',
        'vix': 'vix_price',
        'sp500': 'sp500_price',
        'market_breadth': 'market_breadth_ratio'
    }

    # Get the actual metric name for explorer
    explorer_metric = metric_map.get(metric_name, metric_name)
    return redirect(f'/explorer?metric={explorer_metric}')


@app.route('/explorer')
def explorer():
    """Metric explorer page with dropdown selector."""
    return render_template('explorer.html')


@app.route('/api/metrics/description/<metric_name>')
def api_metric_description(metric_name):
    """API endpoint to get description for a specific metric."""
    if metric_name in METRIC_DESCRIPTIONS:
        return jsonify(METRIC_DESCRIPTIONS[metric_name])
    return jsonify({'error': 'Description not available'}), 404


@app.route('/api/metrics/list')
def api_metrics_list():
    """API endpoint to list all available metrics."""
    import os

    metrics = []

    # Add calculated metrics first (not stored as CSV files)
    calculated_metrics = [
        {'value': 'divergence_gap', 'label': 'Divergence Gap', 'filename': None,
         'calculated': True, 'category': METRIC_CATEGORIES.get('divergence_gap', '')},
    ]
    metrics.extend(calculated_metrics)

    # Skip non-metric CSV files
    skip_files = {'us_recessions.csv', 'property_farmland.csv'}

    # Add CSV-based metrics
    csv_files = sorted([f for f in os.listdir(DATA_DIR) if f.endswith('.csv')])

    for filename in csv_files:
        if filename in skip_files:
            continue
        metric_name = filename.replace('.csv', '')
        # Use custom display name if available, otherwise auto-generate from filename
        friendly_name = METRIC_DISPLAY_NAMES.get(
            metric_name, metric_name.replace('_', ' ').title()
        )
        metrics.append({
            'value': metric_name,
            'label': friendly_name,
            'filename': filename,
            'category': METRIC_CATEGORIES.get(metric_name, ''),
        })

    return jsonify(metrics)


@app.route('/api/recessions')
def api_recessions():
    """API endpoint to get US recession periods for chart shading."""
    filepath = DATA_DIR / 'us_recessions.csv'
    if filepath.exists():
        df = pd.read_csv(filepath)
        recessions = []
        for _, row in df.iterrows():
            recessions.append({
                'start_date': row['start_date'],
                'end_date': row['end_date'],
                'name': row['name'],
                'duration_months': int(row['duration_months']) if pd.notna(row['duration_months']) else None
            })
        return jsonify(recessions)
    return jsonify([])


@app.route('/api/metrics/<metric_name>')
def api_metric_data(metric_name):
    """API endpoint to get data for a specific metric."""

    # Metric name aliases - map common/short names to actual CSV file names
    metric_aliases = {
        'cpi_yoy': 'cpi',
        'hy_spread': 'high_yield_spread',
        'ig_spread': 'investment_grade_spread',
        'vix': 'vix_price',
        'sp500': 'sp500_price',
        'market_breadth': 'market_breadth_ratio',
        # Treasury ETF ticker aliases
        'tlt_price': 'treasury_20yr_price',
        'ief_price': 'treasury_7_10yr_price',
        'shy_price': 'treasury_short_price',
        'tip_price': 'tips_inflation_price',
    }

    # Apply alias if one exists
    metric_name = metric_aliases.get(metric_name, metric_name)

    # Special handling for divergence_gap (calculated metric)
    if metric_name == 'divergence_gap':
        gold_df = load_csv_data('gold_price.csv')
        hy_df = load_csv_data('high_yield_spread.csv')

        if gold_df is not None and hy_df is not None:
            # Merge on date
            merged = pd.merge(gold_df, hy_df, on='date', how='inner')

            # Calculate gold-implied spread and divergence gap
            gold_prices = merged['gold_price'].values
            hy_spreads = merged['high_yield_spread'].values * 100  # Convert to bp

            gold_implied = ((gold_prices / 200) ** 1.5) * 400
            divergence = gold_implied - hy_spreads

            dates = merged['date'].dt.strftime('%Y-%m-%d').tolist()
            values = divergence.tolist()

            # Calculate stats
            current_value = values[-1] if values else None
            min_value = min(values) if values else None
            max_value = max(values) if values else None
            avg_value = sum(values) / len(values) if values else None

            # Calculate change
            change_1d = None
            change_30d = None
            if len(values) >= 2:
                change_1d = values[-1] - values[-2]
            if len(values) >= 30:
                change_30d = values[-1] - values[-30]

            return jsonify({
                'dates': dates,
                'values': values,
                'column_name': 'divergence_gap',
                'stats': {
                    'current': current_value,
                    'min': min_value,
                    'max': max_value,
                    'average': avg_value,
                    'change_1d': change_1d,
                    'change_30d': change_30d
                }
            })

    # Try loading with .csv extension if not present
    filename = metric_name if metric_name.endswith('.csv') else f"{metric_name}.csv"
    df = load_csv_data(filename)

    if df is not None and len(df) > 0:
        dates = df['date'].dt.strftime('%Y-%m-%d').tolist()

        # Get the value column (second column, typically)
        value_column = df.columns[1]
        values = df[value_column].tolist()

        # Calculate stats
        current_value = values[-1] if values else None
        min_value = min(values) if values else None
        max_value = max(values) if values else None
        avg_value = sum(values) / len(values) if values else None

        # Calculate change
        change_1d = None
        change_30d = None
        if len(values) >= 2:
            change_1d = values[-1] - values[-2]
        if len(values) >= 30:
            change_30d = values[-1] - values[-30]

        return jsonify({
            'dates': dates,
            'values': values,
            'column_name': value_column,
            'stats': {
                'current': current_value,
                'min': min_value,
                'max': max_value,
                'average': avg_value,
                'change_1d': change_1d,
                'change_30d': change_30d
            }
        })

    return jsonify({'error': 'Metric not found or no data available'}), 404


def run_data_collection():
    """Run the data collection scripts in the background."""
    global reload_status

    try:
        reload_status['in_progress'] = True
        reload_status['error'] = None
        reload_status['success'] = None
        reload_status['status'] = 'Collecting market data...'

        # Run market_signals.py (default ~35-year lookback, 12775 days in market_signals.py)
        print("Running market_signals.py...")
        result1 = subprocess.run(
            ['python', 'market_signals.py'],
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        if result1.returncode != 0:
            raise Exception(f"market_signals.py failed: {result1.stderr}")

        # Run divergence_analysis.py
        reload_status['status'] = 'Running divergence analysis...'
        print("Running divergence_analysis.py...")
        result2 = subprocess.run(
            ['python', 'divergence_analysis.py'],
            capture_output=True,
            text=True,
            timeout=60  # 1 minute timeout
        )

        if result2.returncode != 0:
            raise Exception(f"divergence_analysis.py failed: {result2.stderr}")

        # Update recession probability panel data (US-146.1)
        reload_status['status'] = 'Updating recession probability data...'
        print("Updating recession probability data...")
        try:
            update_recession_probability()
        except Exception as recession_error:
            print(f"Recession probability update error (non-fatal): {recession_error}")

        # Update market conditions cache (US-294.3)
        reload_status['status'] = 'Updating market conditions...'
        print("Updating market conditions...")
        try:
            update_market_conditions_cache()
        except Exception as conditions_error:
            print(f"Market conditions update error (non-fatal): {conditions_error}")

        eastern = pytz.timezone('US/Eastern')
        reload_status['last_reload'] = datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')
        print("Data reload completed successfully!")

        # Fetch and store daily news BEFORE briefing generation so briefings can use it
        reload_status['status'] = 'Fetching daily news...'
        print("Fetching daily news...")
        try:
            from news_pipeline import run_news_pipeline
            run_news_pipeline()
        except Exception as news_pipeline_error:
            print(f"News pipeline error (non-fatal): {news_pipeline_error}")

        # Generate market-specific briefings FIRST so they can be included in general summary
        # Generate Crypto AI summary
        reload_status['status'] = 'Generating Crypto AI summary...'
        print("Generating Crypto AI summary...")
        try:
            crypto_summary_data = generate_crypto_market_summary()
            crypto_result = generate_crypto_summary(crypto_summary_data)
            if crypto_result['success']:
                print("Crypto AI summary generated successfully!")
            else:
                print(f"Crypto AI summary generation failed: {crypto_result['error']}")
        except Exception as crypto_summary_error:
            print(f"Crypto AI summary error (non-fatal): {crypto_summary_error}")

        # Generate Equity AI summary
        reload_status['status'] = 'Generating Equity AI summary...'
        print("Generating Equity Markets AI summary...")
        try:
            equity_summary_data = generate_equity_market_summary()
            equity_result = generate_equity_summary(equity_summary_data)
            if equity_result['success']:
                print("Equity AI summary generated successfully!")
            else:
                print(f"Equity AI summary generation failed: {equity_result['error']}")
        except Exception as equity_summary_error:
            print(f"Equity AI summary error (non-fatal): {equity_summary_error}")

        # Generate Rates AI summary
        reload_status['status'] = 'Generating Rates AI summary...'
        print("Generating Rates & Yield Curve AI summary...")
        try:
            rates_summary_data = generate_rates_market_summary()
            rates_result = generate_rates_summary(rates_summary_data)
            if rates_result['success']:
                print("Rates AI summary generated successfully!")
            else:
                print(f"Rates AI summary generation failed: {rates_result['error']}")
        except Exception as rates_summary_error:
            print(f"Rates AI summary error (non-fatal): {rates_summary_error}")

        # Generate Dollar & Currency AI summary
        reload_status['status'] = 'Generating Dollar AI summary...'
        print("Generating Dollar & Currency AI summary...")
        try:
            dollar_summary_data = generate_dollar_market_summary()
            dollar_result = generate_dollar_summary(dollar_summary_data)
            if dollar_result['success']:
                print("Dollar AI summary generated successfully!")
            else:
                print(f"Dollar AI summary generation failed: {dollar_result['error']}")
        except Exception as dollar_summary_error:
            print(f"Dollar AI summary error (non-fatal): {dollar_summary_error}")

        # Generate Credit Markets AI summary
        reload_status['status'] = 'Generating Credit AI summary...'
        print("Generating Credit AI summary...")
        try:
            credit_summary_data = generate_credit_market_summary()
            credit_result = generate_credit_summary(credit_summary_data)
            if credit_result['success']:
                print("Credit AI summary generated successfully!")
            else:
                print(f"Credit AI summary generation failed: {credit_result['error']}")
        except Exception as credit_summary_error:
            print(f"Credit AI summary error (non-fatal): {credit_summary_error}")

        # Generate general AI summary AFTER market-specific briefings
        # This allows it to include crypto/equity/rates/dollar briefings as context
        reload_status['status'] = 'Generating AI daily summary...'
        print("Generating AI daily summary...")
        try:
            market_summary = generate_market_summary()
            top_movers = calculate_top_movers(5, period=5)
            top_movers_1d = calculate_top_movers(5, period=1)
            result = generate_daily_summary(market_summary, top_movers, top_movers_1d)
            if result['success']:
                print("AI summary generated successfully!")
            else:
                print(f"AI summary generation failed: {result['error']}")
        except Exception as summary_error:
            print(f"AI summary error (non-fatal): {summary_error}")

        # Generate Market Conditions Synthesis (one-liner)
        reload_status['status'] = 'Generating market conditions synthesis...'
        print("Generating Market Conditions Synthesis...")
        try:
            synthesis_result = generate_market_conditions_synthesis()
            if synthesis_result['success']:
                print("Market conditions synthesis generated successfully!")
            else:
                print(f"Market synthesis generation failed: {synthesis_result['error']}")
        except Exception as synthesis_error:
            print(f"Market synthesis error (non-fatal): {synthesis_error}")

        # Update sector management tone data (US-123.1)
        # Note: this pipeline is intentionally skipped during the daily refresh
        # because FinBERT scoring is a long-running quarterly batch job.
        # update_sector_management_tone() should be run manually or via a
        # quarterly cron job after each earnings season completes.
        # The context processor reads from the cache file set by that job.

        # Mark as successful
        reload_status['success'] = True
        reload_status['status'] = 'Complete!'

    except Exception as e:
        reload_status['error'] = str(e)
        reload_status['success'] = False
        reload_status['status'] = 'Error occurred'
        print(f"Data reload error: {e}")

    finally:
        reload_status['in_progress'] = False


@csrf.exempt
@app.route('/api/reload-data', methods=['POST'])
def api_reload_data():
    """Trigger data reload in background."""
    global reload_status

    if reload_status['in_progress']:
        return jsonify({
            'status': 'already_running',
            'message': 'Data reload is already in progress'
        }), 409

    # Start data collection in background thread
    thread = threading.Thread(target=run_data_collection)
    thread.daemon = True
    thread.start()

    return jsonify({
        'status': 'started',
        'message': 'Data reload started'
    })


@app.route('/api/reload-status')
def api_reload_status():
    """Get current reload status."""
    return jsonify({
        'running': reload_status['in_progress'],
        'status': reload_status['status'],
        'last_run': {
            'success': reload_status['success'],
            'error': reload_status['error'],
            'timestamp': reload_status['last_reload']
        } if reload_status['last_reload'] or reload_status['error'] else None
    })


@app.route('/api/scheduler-status')
def api_scheduler_status():
    """Get current scheduler status including next refresh time."""
    status = get_scheduler_status()
    status['last_reload'] = reload_status['last_reload']
    return jsonify(status)




@app.route('/api/ai-summary')
def api_ai_summary():
    """Get the current AI-generated daily summary."""
    summary = get_summary_for_display()
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No summary available',
        'message': 'Run a data reload to generate a summary'
    }), 404


@app.route('/api/ai-summary/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_summary():
    """Manually trigger AI summary generation."""
    try:
        market_summary = generate_market_summary()
        top_movers = calculate_top_movers(5, period=5)
        top_movers_1d = calculate_top_movers(5, period=1)
        result = generate_daily_summary(market_summary, top_movers, top_movers_1d)

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


@app.route('/api/crypto-summary')
def api_crypto_summary():
    """Get the current AI-generated crypto/Bitcoin summary."""
    summary = get_crypto_summary_for_display()
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No crypto summary available',
        'message': 'Run a data reload to generate a summary'
    }), 404


@app.route('/api/crypto-summary/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_crypto_summary():
    """Manually trigger crypto AI summary generation."""
    try:
        crypto_summary_data = generate_crypto_market_summary()
        result = generate_crypto_summary(crypto_summary_data)

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


@app.route('/api/equity-summary')
def api_equity_summary():
    """Get the current AI-generated equity markets summary."""
    summary = get_equity_summary_for_display()
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No equity summary available',
        'message': 'Run a data reload to generate a summary'
    }), 404


@app.route('/api/equity-summary/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_equity_summary():
    """Manually trigger equity AI summary generation."""
    try:
        equity_summary_data = generate_equity_market_summary()
        result = generate_equity_summary(equity_summary_data)

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


@app.route('/api/rates-summary')
def api_rates_summary():
    """Get the current AI-generated rates & yield curve summary."""
    summary = get_rates_summary_for_display()
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No rates summary available',
        'message': 'Run a data reload to generate a summary'
    }), 404


@app.route('/api/rates-summary/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_rates_summary():
    """Manually trigger rates AI summary generation."""
    try:
        rates_summary_data = generate_rates_market_summary()
        result = generate_rates_summary(rates_summary_data)

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


@app.route('/api/dollar-summary')
def api_dollar_summary():
    """Get the current dollar AI summary."""
    summary = get_dollar_summary_for_display()
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No dollar summary available',
        'message': 'Run a data reload to generate a summary'
    }), 404


@app.route('/api/dollar-summary/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_dollar_summary():
    """Manually trigger dollar AI summary generation."""
    try:
        dollar_summary_data = generate_dollar_market_summary()
        result = generate_dollar_summary(dollar_summary_data)

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


@app.route('/api/credit-summary')
def api_credit_summary():
    """Get the current credit AI summary."""
    summary = get_credit_summary_for_display()
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No credit summary available',
        'message': 'Run a data reload to generate a summary'
    }), 404


@app.route('/api/credit-summary/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_credit_summary():
    """Manually trigger credit AI summary generation."""
    try:
        credit_summary_data = generate_credit_market_summary()
        result = generate_credit_summary(credit_summary_data)

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


# ============================================================================
# Chatbot API Endpoint (Feature 3.2, US-3.2.2)
# ============================================================================


def _build_chatbot_enrichment_context():
    """Build enriched context for chatbot: market data, briefings, and news.

    Returns a string with all available context, sectioned and labeled.
    Gracefully degrades — missing data is simply omitted.
    """
    parts = []

    # 1. Full market data summary (same data sent to daily briefing)
    try:
        market_summary = generate_market_summary()
        if market_summary and market_summary != "Market data summary unavailable.":
            parts.append(market_summary)
    except Exception:
        pass

    # 2. Top movers (1-day and 5-day by z-score)
    try:
        top_movers_5d = calculate_top_movers(num_movers=5, period=5)
        top_movers_1d = calculate_top_movers(num_movers=5, period=1)
        if top_movers_1d:
            movers_text = "## TODAY'S BIGGEST MOVES (1-day, by z-score)\n"
            for m in top_movers_1d[:5]:
                direction = "up" if m['change'] > 0 else "down"
                movers_text += f"- {m['name']}: {m['change']:+.1f}{m['unit']} ({direction}, z-score: {m['z_score']:.1f})\n"
            parts.append(movers_text)
        if top_movers_5d:
            movers_text = "## MOST UNUSUAL 5-DAY MOVES (by z-score)\n"
            for m in top_movers_5d[:5]:
                direction = "up" if m['change'] > 0 else "down"
                movers_text += f"- {m['name']}: {m['change']:+.1f}{m['unit']} ({direction}, z-score: {m['z_score']:.1f})\n"
            parts.append(movers_text)
    except Exception:
        pass

    # 3. Today's AI briefings
    try:
        from ai_summary import (
            get_latest_summary, get_latest_crypto_summary,
            get_latest_equity_summary, get_latest_rates_summary,
            get_latest_dollar_summary, get_latest_credit_summary
        )

        briefings_parts = []

        general = get_latest_summary()
        if general and general.get('summary'):
            briefings_parts.append(f"### General Daily Briefing ({general.get('date', 'unknown')}):\n{general['summary']}")

        for name, getter in [
            ('Crypto', get_latest_crypto_summary),
            ('Equity', get_latest_equity_summary),
            ('Rates', get_latest_rates_summary),
            ('Dollar', get_latest_dollar_summary),
            ('Credit', get_latest_credit_summary),
        ]:
            summary = getter()
            if summary and summary.get('summary'):
                briefings_parts.append(f"### {name} Briefing ({summary.get('date', 'unknown')}):\n{summary['summary']}")

        if briefings_parts:
            parts.append("## TODAY'S AI BRIEFINGS\n" + "\n\n".join(briefings_parts))
    except Exception:
        pass

    # 4. Cross-market news summary
    try:
        from ai_summary import _get_stored_news_context
        news = _get_stored_news_context()
        if news:
            parts.append(f"## TODAY'S CROSS-MARKET NEWS SUMMARY\n{news}")
    except Exception:
        pass

    return "\n\n".join(parts) if parts else ""


@app.route('/api/chatbot', methods=['POST'])
@csrf.exempt
@login_required
@anonymous_rate_limit(CATEGORY_CHATBOT)
def api_chatbot():
    """Handle AI chatbot conversation requests using the system API key."""
    from services.ai_service import get_system_ai_client, get_system_chatbot_model

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    user_message = data.get('message', '').strip()
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    conversation_history = data.get('conversation', [])
    context = data.get('context', {})
    page = context.get('page', '/')
    section = context.get('section') or None
    section_name = context.get('section_name') or None

    client, provider = get_system_ai_client()
    if client is None:
        app.logger.error(f'System AI client unavailable: {provider}')
        return jsonify({'error': 'AI service unavailable'}), 503

    model = get_system_chatbot_model()

    section_context = (
        f" The user is focused on the '{section_name}' section of the dashboard."
        if section_name else ""
    )

    # Build enriched market context (US-325.7)
    enrichment_context = ""
    try:
        enrichment_context = _build_chatbot_enrichment_context()
        if enrichment_context:
            enrichment_context = "\n\n" + enrichment_context
    except Exception as e:
        app.logger.warning(f'Chatbot enrichment context error: {e}')

    # Build market conditions context (US-325.2)
    conditions_context = ""
    try:
        conditions = get_market_conditions()
        conditions_text = _build_conditions_context(conditions)
        history = get_conditions_history()
        history_text = _build_conditions_history_context(history, days=90)
        if conditions_text or history_text:
            conditions_context = "\n\n" + conditions_text
            if history_text:
                conditions_context += "\n" + history_text
    except Exception as e:
        app.logger.warning(f'Chatbot conditions context error: {e}')

    # Check tool availability
    web_search_available = is_tavily_configured()

    system_prompt = (
        "You are an AI assistant helping an individual investor understand macro financial markets. "
        "Write for a financially literate non-professional — someone who reads the WSJ and owns ETFs but doesn't work in finance. "
        "Use financial terms freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — translate these into plain-language magnitude (e.g., 'near historic highs,' 'the largest move in months'). "
        "You provide clear, concise explanations of market conditions, economic indicators, and financial concepts. "
        "The dashboard uses a four-quadrant conditions framework (Goldilocks, Reflation, Stagflation, Deflation Risk) "
        "with four dimensions: quadrant (growth vs inflation), liquidity, risk, and policy. "
        "Use this terminology consistently. "
        f"The user is currently viewing the dashboard page: {page}.{section_context}"
        f"{conditions_context}"
        f"{enrichment_context}\n\n"
        "You have full access to today's market data, AI briefings, and news above. "
        "Use this context to answer questions directly — only use tools when you need the latest real-time value "
        "or detailed time series for a specific metric.\n\n"
        "AVAILABLE TOOLS:\n"
        "1. **list_available_metrics** - Discover all available market data series (credit spreads, equities, safe havens, etc.)\n"
        "2. **get_metric_data** - Fetch detailed data for any metric (current value, percentile, historical stats, recent changes)\n"
        + ("3. **search_web** - Search for current news, market commentary, or recent events\n" if web_search_available else "")
        + "\n"
        "HOW TO USE TOOLS EFFECTIVELY:\n"
        "- When users ask about specific metrics, USE get_metric_data to fetch current data rather than guessing\n"
        "- When users ask what data is available or about a metric you're unsure of, USE list_available_metrics first\n"
        "- Only search the web for news/current events - use metric tools for market data\n"
        "- ALWAYS use tools to get current data when answering about specific metrics — don't rely on stale information\n\n"
        "Be helpful, accurate, and focused on the investor's understanding needs. "
        "Keep responses concise (2-4 paragraphs) unless more detail is clearly needed."
    )

    # DEBUG: dump chatbot prompt to file for review
    try:
        from pathlib import Path as _Path
        _dump_dir = _Path("data/prompt_dumps")
        _dump_dir.mkdir(parents=True, exist_ok=True)
        _tool_names = ['list_available_metrics', 'get_metric_data']
        if web_search_available:
            _tool_names.append('search_web')
        with open(_dump_dir / "chatbot.txt", "w") as _f:
            _f.write(f"=== PROMPT DUMP: Chatbot ===\n")
            _f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            _f.write(f"Provider: {provider}\n")
            _f.write(f"Model: {model}\n")
            _f.write(f"Prompt caching: {'enabled (Anthropic ephemeral)' if provider == 'anthropic' else 'automatic (OpenAI)'}\n")
            _f.write(f"Page: {page}\n")
            _f.write(f"Section: {section_name or 'None'}\n")
            _f.write(f"Enrichment context: {'included' if enrichment_context else 'none'}\n")
            _f.write(f"Web search available: {web_search_available}\n")
            _f.write(f"Tools: {', '.join(_tool_names)}\n")
            _f.write(f"\n{'='*60}\n")
            _f.write(f"SYSTEM PROMPT\n{'='*60}\n")
            _f.write(system_prompt)
            _f.write(f"\n\n{'='*60}\n")
            _f.write(f"TOOL DEFINITIONS\n{'='*60}\n")
            _f.write(json.dumps(LIST_METRICS_FUNCTION, indent=2))
            _f.write("\n\n")
            _f.write(json.dumps(GET_METRIC_FUNCTION, indent=2))
            if web_search_available:
                _f.write("\n\n")
                _f.write(json.dumps(SEARCH_FUNCTION_DEFINITION, indent=2))
            _f.write(f"\n\n{'='*60}\n")
            _f.write(f"CONVERSATION HISTORY ({len(conversation_history)} messages)\n{'='*60}\n")
            for msg in conversation_history:
                _f.write(f"\n[{msg.get('role', '?').upper()}]\n{msg.get('content', '')}\n")
            _f.write(f"\n{'='*60}\n")
            _f.write(f"USER MESSAGE\n{'='*60}\n")
            _f.write(user_message)
            _f.write("\n")
    except Exception:
        pass

    # Build tool definitions based on provider
    if provider == 'anthropic':
        tools = [
            {
                "name": LIST_METRICS_FUNCTION["name"],
                "description": LIST_METRICS_FUNCTION["description"],
                "input_schema": LIST_METRICS_FUNCTION.get("parameters", {"type": "object", "properties": {}})
            },
            {
                "name": GET_METRIC_FUNCTION["name"],
                "description": GET_METRIC_FUNCTION["description"],
                "input_schema": GET_METRIC_FUNCTION.get("parameters", {"type": "object", "properties": {}})
            }
        ]
        if web_search_available:
            tools.append({
                "name": SEARCH_FUNCTION_DEFINITION["name"],
                "description": SEARCH_FUNCTION_DEFINITION["description"],
                "input_schema": SEARCH_FUNCTION_DEFINITION.get("parameters", {"type": "object", "properties": {}})
            })
    else:  # OpenAI
        tools = [
            {"type": "function", "function": LIST_METRICS_FUNCTION},
            {"type": "function", "function": GET_METRIC_FUNCTION}
        ]
        if web_search_available:
            tools.append({"type": "function", "function": SEARCH_FUNCTION_DEFINITION})

    # Usage metering: accumulate tokens across agentic loop iterations
    from services.usage_metering import extract_usage, accumulate_usage, record_usage
    total_usage = {}

    try:
        if provider == 'anthropic':
            from anthropic_request import refusal_reason

            # Use structured system prompt with cache_control for prompt caching (US-325.7)
            # The system prompt is cached across messages in the same conversation,
            # reducing cost by ~90% on follow-up messages.
            system_prompt_blocks = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }
            ]

            messages = []
            for msg in conversation_history:
                role = 'user' if msg.get('role') == 'user' else 'assistant'
                messages.append({'role': role, 'content': msg.get('content', '')})
            messages.append({'role': 'user', 'content': user_message})

            max_iterations = 5
            iteration = 0
            ai_response = None

            while iteration < max_iterations:
                iteration += 1
                print(f"[CHATBOT-ANTHROPIC] Iteration {iteration}/{max_iterations}")

                response = client.messages.create(
                    model=model,
                    # Combined reasoning + visible-answer ceiling: thinking is
                    # always on and is generated against this same budget.
                    max_tokens=16000,
                    system=system_prompt_blocks,
                    messages=messages,
                    tools=tools,
                    # Adaptive is the only accepted on-mode on current models;
                    # budget_tokens returns 400. Left at the default
                    # display ("omitted") — the chat UI does not show reasoning.
                    thinking={"type": "adaptive"},
                )

                # Log cache usage for cost monitoring
                usage = response.usage
                cache_creation = getattr(usage, 'cache_creation_input_tokens', 0) or 0
                cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
                print(f"[CHATBOT-ANTHROPIC] stop_reason: {response.stop_reason}, "
                      f"input_tokens: {usage.input_tokens}, "
                      f"cache_creation: {cache_creation}, cache_read: {cache_read}")

                total_usage = accumulate_usage(total_usage, extract_usage(response, 'anthropic'))

                refusal = refusal_reason(response)
                if refusal:
                    # Checked before content is read; stop_details is None for
                    # every other stop reason.
                    app.logger.warning('[CHATBOT-ANTHROPIC] Refused: %s', refusal)
                    ai_response = None
                    break

                if response.stop_reason == 'max_tokens':
                    app.logger.warning(
                        '[CHATBOT-ANTHROPIC] Hit max_tokens — reasoning may have '
                        'consumed the budget before any visible text.'
                    )

                tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
                text_blocks = [block for block in response.content if block.type == "text"]

                if tool_use_blocks:
                    print(f"[CHATBOT-ANTHROPIC] Tool uses: {[t.name for t in tool_use_blocks]}")
                    messages.append({"role": "assistant", "content": response.content})

                    tool_results = []
                    for tool_use in tool_use_blocks:
                        result = _execute_tool(tool_use.name, tool_use.input or {})
                        print(f"[CHATBOT-ANTHROPIC] {tool_use.name} returned {len(result)} chars")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result
                        })

                    messages.append({"role": "user", "content": tool_results})
                else:
                    if text_blocks:
                        ai_response = "\n".join(block.text for block in text_blocks)
                    break

        else:  # OpenAI (default)
            messages = [{'role': 'system', 'content': system_prompt}]
            for msg in conversation_history:
                role = 'user' if msg.get('role') == 'user' else 'assistant'
                messages.append({'role': role, 'content': msg.get('content', '')})
            messages.append({'role': 'user', 'content': user_message})

            max_iterations = 5
            iteration = 0
            ai_response = None

            while iteration < max_iterations:
                iteration += 1
                print(f"[CHATBOT-OPENAI] Iteration {iteration}/{max_iterations}")

                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=4096,
                    tools=tools,
                    tool_choice="auto"
                )
                total_usage = accumulate_usage(total_usage, extract_usage(response, 'openai'))
                response_message = response.choices[0].message

                print(f"[CHATBOT-OPENAI] finish_reason: {response.choices[0].finish_reason}")

                if response_message.tool_calls:
                    print(f"[CHATBOT-OPENAI] Tool calls: {[tc.function.name for tc in response_message.tool_calls]}")
                    messages.append(response_message)

                    for tool_call in response_message.tool_calls:
                        function_name = tool_call.function.name
                        function_args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}

                        result = _execute_tool(function_name, function_args)
                        print(f"[CHATBOT-OPENAI] {function_name} returned {len(result)} chars")

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": function_name,
                            "content": result
                        })
                else:
                    ai_response = response_message.content
                    break

        if not ai_response or not ai_response.strip():
            ai_response = "I apologize, but I wasn't able to generate a response. Please try again."

        ai_response = _filter_reasoning_artifacts(ai_response)

        # Record usage metering for authenticated users (US-12.2.2)
        try:
            if current_user.is_authenticated:
                # Detect sentence drill-in vs regular chatbot
                is_drill_in = bool(context.get('briefing_text'))
                interaction_type = 'sentence_drill_in' if is_drill_in else 'chatbot'
                record_usage(
                    user_id=current_user.id,
                    interaction_type=interaction_type,
                    model_name=model,
                    **total_usage,
                )
        except Exception:
            app.logger.exception('Chatbot metering error (non-fatal)')

        return jsonify({
            'response': ai_response,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        app.logger.error(f'Chatbot AI error: {e}')
        return jsonify({'error': 'AI service unavailable'}), 503


# ============================================================================
# Section Opening API (US-258.5)
# ============================================================================

# Allowed section IDs — validated as dict key only, never used as file path,
# SQL parameter, or shell argument.
_SECTION_OPENING_ALLOWED = {
    'briefing-section', 'sector-tone-section',
    'market-conditions', 'movers-section', 'signals-section',
    'recession-panel-section', 'trade-pulse-section',
    'asset-credit', 'asset-equity', 'asset-rates', 'asset-dollar',
    'asset-crypto', 'asset-safe-havens', 'asset-property',
}

_SECTION_NAMES = {
    'briefing-section': 'AI Market Briefing',
    'sector-tone-section': 'Sector Management Tone',
    'market-conditions': 'Market Conditions at a Glance',
    'movers-section': "What's Moving Today",
    'signals-section': 'Cross-Market Indicators',
    'recession-panel-section': 'Recession Probability',
    'trade-pulse-section': 'Global Trade Pulse',
    'asset-credit': 'Credit Markets',
    'asset-equity': 'Equity Markets',
    'asset-rates': 'Rates & Fixed Income',
    'asset-dollar': 'US Dollar',
    'asset-crypto': 'Crypto / Bitcoin',
    'asset-safe-havens': 'Safe Havens',
    'asset-property': 'Property Macro',
}


def _get_section_live_data(section_id: str) -> str:
    """Return a plain-text summary of live data for the given section.

    Injected into the AI system prompt so the model can reference real values.
    Returns a generic fallback string if data is unavailable.
    """
    try:
        if section_id == 'briefing-section':
            from ai_summary import get_summary_for_display
            briefing = get_summary_for_display()
            if briefing and briefing.get('summary'):
                date = briefing.get('date', 'today')
                return f"Today's AI market briefing ({date}):\n{briefing['summary']}"
            return "AI market briefing not yet generated for today."

        if section_id == 'sector-tone-section':
            cache_path = Path('data/sector_tone_cache.json')
            if cache_path.exists():
                import json as _json
                with open(cache_path) as f:
                    tone_data = _json.load(f)
                sectors = tone_data.get('sectors', [])
                if sectors:
                    lines = [
                        f"{s['short_name']}: {s['current_tone']}"
                        for s in sectors
                    ]
                    quarter = tone_data.get('quarter', '')
                    year = tone_data.get('year', '')
                    return (
                        f"Sector management tone ({quarter} {year}): "
                        + ', '.join(lines) + '.'
                    )
            return "Sector tone data not yet available."

        if section_id == 'recession-panel-section':
            from recession_probability import get_recession_probability
            rec = get_recession_probability()
            if rec:
                ensemble = rec.get('ensemble_probability')
                ny_fed = rec.get('ny_fed')
                cp = rec.get('chauvet_piger')
                sos = rec.get('richmond_sos')
                divergence = rec.get('divergence_flag', False)
                parts = []
                if ensemble is not None:
                    parts.append(f"Ensemble probability: {ensemble:.1f}%")
                if ny_fed is not None:
                    parts.append(f"NY Fed 12-month model: {ny_fed:.1f}%")
                if cp is not None:
                    parts.append(f"Chauvet-Piger coincident: {cp:.1f}%")
                if sos is not None:
                    parts.append(f"Richmond SOS: {sos:.1f}%")
                if divergence:
                    parts.append("Models show significant divergence")
                return 'Recession probability — ' + '; '.join(parts) + '.'
            return "Recession probability data not yet available."

        if section_id == 'trade-pulse-section':
            ctx = _get_trade_balance_context()
            cond = get_market_conditions()
            quadrant = cond.get('quadrant', 'Unknown') if cond else 'Unknown'
            val = ctx.get('trade_balance_value')
            period = ctx.get('trade_balance_period', '')
            yoy = ctx.get('trade_balance_yoy_change')
            interp = ctx.get('trade_balance_interpretation', '')
            parts = [f"Market conditions: {quadrant} quadrant"]
            if val is not None:
                parts.append(f"Trade balance: ${val:.1f}B ({period})")
            if yoy is not None:
                direction = 'improved' if yoy > 0 else 'worsened'
                parts.append(f"YoY change: {direction} by ${abs(yoy):.1f}B")
            if interp:
                parts.append(f"Interpretation: {interp}")
            return '; '.join(parts) + '.'

        if section_id == 'asset-credit':
            hy_df = load_csv_data('high_yield_spread.csv')
            ig_df = load_csv_data('investment_grade_spread.csv')
            parts = []
            if hy_df is not None and len(hy_df):
                hy_val = hy_df.iloc[-1][hy_df.columns[1]] * 100
                stats = get_metric_stats(hy_df)
                pct = round(stats.get('percentile', 50), 0) if stats else None
                parts.append(f"HY spread: {hy_val:.0f}bps" + (f" ({pct:.0f}th percentile)" if pct is not None else ''))
            if ig_df is not None and len(ig_df):
                ig_val = ig_df.iloc[-1][ig_df.columns[1]] * 100
                stats = get_metric_stats(ig_df)
                pct = round(stats.get('percentile', 50), 0) if stats else None
                parts.append(f"IG spread: {ig_val:.0f}bps" + (f" ({pct:.0f}th percentile)" if pct is not None else ''))
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Credit market data: ' + '; '.join(parts) + '.') if parts else "Credit market data loading."

        if section_id == 'asset-equity':
            spy_df = load_csv_data('sp500_price.csv')
            vix_df = load_csv_data('vix_price.csv')
            parts = []
            if spy_df is not None and len(spy_df) > 5:
                price = spy_df.iloc[-1][spy_df.columns[1]]
                chg_5d = ((price / spy_df.iloc[-6][spy_df.columns[1]]) - 1) * 100
                parts.append(f"S&P 500: {price:.0f} ({chg_5d:+.1f}% 5-day)")
            if vix_df is not None and len(vix_df):
                vix = vix_df.iloc[-1][vix_df.columns[1]]
                parts.append(f"VIX: {vix:.1f}")
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Equity market data: ' + '; '.join(parts) + '.') if parts else "Equity market data loading."

        if section_id == 'asset-rates':
            bei_df = load_csv_data('breakeven_inflation_10y.csv')
            fed_df = load_csv_data('fed_funds_rate.csv')
            parts = []
            if bei_df is not None and len(bei_df):
                bei = bei_df.iloc[-1][bei_df.columns[1]]
                parts.append(f"10-year breakeven inflation: {bei:.2f}%")
            if fed_df is not None and len(fed_df):
                ffr = fed_df.iloc[-1][fed_df.columns[1]]
                parts.append(f"Fed funds rate: {ffr:.2f}%")
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Rates data: ' + '; '.join(parts) + '.') if parts else "Rates data loading."

        if section_id == 'asset-dollar':
            dxy_df = load_csv_data('dollar_index_price.csv')
            eurusd_df = load_csv_data('eurusd_price.csv')
            parts = []
            if dxy_df is not None and len(dxy_df):
                dxy = dxy_df.iloc[-1][dxy_df.columns[1]]
                stats = get_metric_stats(dxy_df)
                pct = round(stats.get('percentile', 50), 0) if stats else None
                parts.append(f"DXY: {dxy:.1f}" + (f" ({pct:.0f}th percentile)" if pct is not None else ''))
            if eurusd_df is not None and len(eurusd_df):
                eurusd = eurusd_df.iloc[-1][eurusd_df.columns[1]]
                parts.append(f"EUR/USD: {eurusd:.4f}")
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Dollar data: ' + '; '.join(parts) + '.') if parts else "Dollar data loading."

        if section_id == 'asset-crypto':
            btc_df = load_csv_data('bitcoin_price.csv')
            parts = []
            if btc_df is not None and len(btc_df) > 5:
                btc = btc_df.iloc[-1][btc_df.columns[1]]
                peak = btc_df[btc_df.columns[1]].max()
                decline = ((btc / peak) - 1) * 100
                chg_5d = ((btc / btc_df.iloc[-6][btc_df.columns[1]]) - 1) * 100
                parts.append(f"Bitcoin: ${btc:,.0f} ({chg_5d:+.1f}% 5-day)")
                parts.append(f"Off peak: {decline:.1f}% (peak: ${peak:,.0f})")
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Crypto data: ' + '; '.join(parts) + '.') if parts else "Crypto data loading."

        if section_id == 'asset-safe-havens':
            gold_df = load_csv_data('gold_price.csv')
            parts = []
            if gold_df is not None and len(gold_df) > 5:
                gold = gold_df.iloc[-1][gold_df.columns[1]] * 10
                chg_5d = ((gold_df.iloc[-1][gold_df.columns[1]] / gold_df.iloc[-6][gold_df.columns[1]]) - 1) * 100
                parts.append(f"Gold: ${gold:.0f}/oz ({chg_5d:+.1f}% 5-day)")
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Safe havens data: ' + '; '.join(parts) + '.') if parts else "Safe havens data loading."

        if section_id == 'asset-property':
            parts = []
            hpi_df = load_csv_data('property_hpi.csv')
            if hpi_df is not None and len(hpi_df) >= 13:
                hpi_df = hpi_df.dropna(subset=['property_hpi']).set_index('date').sort_index()
                hpi_val = float(hpi_df['property_hpi'].iloc[-1])
                hpi_ago = float(hpi_df['property_hpi'].iloc[-13])
                hpi_yoy = (hpi_val / hpi_ago - 1) * 100
                parts.append(f"Case-Shiller HPI: {hpi_val:.1f} ({hpi_yoy:+.1f}% YoY)")
            rent_df = load_csv_data('property_cpi_rent.csv')
            if rent_df is not None and len(rent_df) >= 13:
                rent_df = rent_df.dropna(subset=['property_cpi_rent']).set_index('date').sort_index()
                rent_val = float(rent_df['property_cpi_rent'].iloc[-1])
                rent_ago = float(rent_df['property_cpi_rent'].iloc[-13])
                rent_yoy = (rent_val / rent_ago - 1) * 100
                parts.append(f"CPI Rent YoY: {rent_yoy:+.1f}%")
            vacancy_df = load_csv_data('property_vacancy.csv')
            if vacancy_df is not None and len(vacancy_df) >= 2:
                vacancy_df = vacancy_df.dropna(subset=['property_vacancy']).set_index('date').sort_index()
                vac = float(vacancy_df['property_vacancy'].iloc[-1])
                parts.append(f"Rental vacancy: {vac:.1f}%")
            cond = get_market_conditions()
            if cond:
                parts.append(f"Market conditions: {cond.get('quadrant', 'Unknown')} quadrant")
            return ('Property data: ' + '; '.join(parts) + '.') if parts else "Property data loading."

        if section_id == 'market-conditions':
            conditions = get_market_conditions()
            if conditions:
                conditions_text = _build_conditions_context(conditions)
                if conditions_text:
                    return conditions_text
            return "Market conditions data not yet available."

        # Generic fallback for movers-section, signals-section
        conditions = get_market_conditions()
        if conditions:
            quadrant = conditions.get('quadrant', 'Unknown')
            dims = conditions.get('dimensions', {})
            liq = dims.get('liquidity', {}).get('state', 'Unknown')
            risk = dims.get('risk', {}).get('state', 'Unknown')
            return f"Current market conditions: {quadrant} quadrant, liquidity {liq}, risk {risk}."
        return "Dashboard data loading."

    except Exception as e:
        app.logger.warning(f'_get_section_live_data({section_id}) error: {e}')
        return "Live data temporarily unavailable."


@app.route('/api/chatbot/section-opening', methods=['POST'])
@csrf.exempt
@login_required
@anonymous_rate_limit(CATEGORY_CHATBOT)
def api_chatbot_section_opening():
    """Generate an AI-powered opening message for a section AI button click.

    US-258.5: Replaces static hardcoded opening text with a real AI-generated
    response that includes live section data.
    """
    from services.ai_service import get_system_ai_client, get_system_ai_model

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    section_id = data.get('section_id', '').strip()

    # Validate — only allow known section keys; never use section_id as a
    # file path, SQL parameter, or shell argument.
    if not section_id or section_id not in _SECTION_OPENING_ALLOWED:
        return jsonify({'error': 'Unknown section_id'}), 400

    section_name = _SECTION_NAMES[section_id]

    client, provider = get_system_ai_client()
    if client is None:
        app.logger.error(f'System AI client unavailable: {provider}')
        return jsonify({'error': 'AI service unavailable'}), 503

    model = get_system_ai_model()
    live_data = _get_section_live_data(section_id)

    system_prompt = (
        "You are MacroClarity AI, a financial market assistant helping an individual investor "
        "understand macro financial markets. Write for a financially literate non-professional — "
        "someone who reads the WSJ and owns ETFs but doesn't work in finance. Use financial terms "
        "freely, but always make the implication clear in plain language. Avoid z-scores, basis point counts, and percentile references — "
        "translate these into plain-language magnitude (e.g., \"near historic highs,\" \"the largest move in months\"). Provide clear, concise "
        "explanations of market conditions, economic indicators, and financial concepts. Be direct "
        "and data-driven. Keep your response to 2-3 short paragraphs."
    )
    user_message = (
        f"The user just opened the '{section_name}' section of the MacroClarity dashboard. "
        f"Here is the current live data for this section:\n\n{live_data}\n\n"
        "Give a holistic explanation of what this section shows and what the current data "
        "means for investors in plain language. Reference the specific data values above. "
        "Close with a brief invitation for the user to ask follow-up questions."
    )

    # DEBUG: dump section-opening prompt to file for review
    try:
        from pathlib import Path as _Path
        _dump_dir = _Path("data/prompt_dumps")
        _dump_dir.mkdir(parents=True, exist_ok=True)
        with open(_dump_dir / f"section_opening_{section_id}.txt", "w") as _f:
            _f.write(f"=== PROMPT DUMP: Section Opening ({section_name}) ===\n")
            _f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            _f.write(f"Provider: {provider}\n")
            _f.write(f"Model: {model}\n")
            _f.write(f"Section ID: {section_id}\n")
            _f.write(f"\n{'='*60}\n")
            _f.write(f"SYSTEM PROMPT\n{'='*60}\n")
            _f.write(system_prompt)
            _f.write(f"\n\n{'='*60}\n")
            _f.write(f"USER MESSAGE\n{'='*60}\n")
            _f.write(user_message)
            _f.write("\n")
    except Exception:
        pass

    try:
        if provider == 'anthropic':
            from anthropic_request import (
                build_request_params, extract_text, refusal_reason,
                served_model, warn_if_truncated,
            )
            # Short-output path (a single opening paragraph) on the briefing
            # model. Thinking is always on and shares max_tokens with the
            # visible answer, so the old 512 ceiling — sized when thinking was
            # off — would be consumed by reasoning before any text. Low effort
            # keeps that reasoning short; the raised ceiling keeps it from
            # truncating the answer.
            section_max_tokens = 4000
            response = client.beta.messages.create(
                **build_request_params(
                    model=model,
                    max_tokens=section_max_tokens,
                    system=system_prompt,
                    messages=[{'role': 'user', 'content': user_message}],
                    effort='low',
                )
            )
            refusal = refusal_reason(response)
            if refusal:
                # A refusal is not content — never render it to the reader.
                app.logger.warning('Section opening refused: %s', refusal)
                return jsonify({'error': 'AI service unavailable'}), 503
            warn_if_truncated(response, '[SECTION-OPENING]', section_max_tokens)
            # Never index content[0] — the first block is a thinking block.
            ai_response = extract_text(response)
            # A server-side fallback can serve the turn on a different model;
            # meter what actually ran, not what we asked for.
            metering_model = served_model(response, model)
        else:  # OpenAI
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_message}
                ],
                max_tokens=512
            )
            ai_response = response.choices[0].message.content
            metering_model = model

        # Record usage metering for authenticated users (US-12.2.2)
        try:
            if current_user.is_authenticated:
                from services.usage_metering import extract_usage, record_usage
                usage_data = extract_usage(response, provider)
                record_usage(
                    user_id=current_user.id,
                    interaction_type='section_ai',
                    model_name=metering_model,
                    **usage_data,
                )
        except Exception:
            app.logger.exception('Section opening metering error (non-fatal)')

        return jsonify({
            'response': ai_response,
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        app.logger.error(f'Section opening AI error (section={section_id}): {e}')
        return jsonify({'error': 'AI service unavailable'}), 503


# ============================================================================
# Market Conditions Synthesis API (US-1.2.2)
# ============================================================================

# Caching for market conditions synthesis
MARKET_SYNTHESIS_FILE = Path("data/market_synthesis.json")


def calculate_market_statuses(dashboard_data: dict) -> dict:
    """
    Calculate market status labels from dashboard data.
    Ports the status calculation logic from frontend JS to Python.

    Args:
        dashboard_data: Output from get_dashboard_data()

    Returns:
        Dict with status labels for each market dimension
    """
    metrics = dashboard_data.get('metrics', {})
    market_grid = dashboard_data.get('market_grid', {})

    # Credit Status (based on HY spread)
    hy_spread = metrics.get('hy_spread', {}).get('current', 0)
    if hy_spread > 600:
        credit = 'crisis'
    elif hy_spread > 450:
        credit = 'stressed'
    elif hy_spread > 350:
        credit = 'tight'
    elif hy_spread < 300:
        credit = 'calm'
    else:
        credit = 'normal'

    # Equities Status (based on VIX)
    vix = metrics.get('vix', {}).get('current', 15)
    if vix > 30:
        equities = 'fear'
    elif vix > 25:
        equities = 'risk-off'
    elif vix > 20:
        equities = 'cautious'
    elif vix < 15:
        equities = 'risk-on'
    else:
        equities = 'neutral'

    # Rates Status (10Y - 2Y curve)
    t10y = market_grid.get('rates', {}).get('treasury_10y', {}).get('value', 4.0)
    t2y = market_grid.get('rates', {}).get('treasury_2y', {}).get('value', 4.0)
    curve = t10y - t2y
    if curve < -0.5:
        rates = 'deeply-inverted'
    elif curve < 0:
        rates = 'inverted'
    elif curve > 1:
        rates = 'steep'
    elif curve < 0.25:
        rates = 'flat'
    else:
        rates = 'normal'

    # Volatility Status (based on VIX)
    if vix > 30:
        volatility = 'spiking'
    elif vix > 20:
        volatility = 'elevated'
    else:
        volatility = 'calm'

    # Dollar Status (based on DXY)
    dxy = market_grid.get('dollar', {}).get('dxy', {}).get('value', 100)
    if dxy > 107:
        dollar = 'very-strong'
    elif dxy > 104:
        dollar = 'strong'
    elif dxy < 97:
        dollar = 'very-weak'
    elif dxy < 100:
        dollar = 'weak'
    else:
        dollar = 'neutral'

    # Liquidity Status (based on M2 YoY change)
    m2_yoy = metrics.get('economic_indicators', {}).get('m2_money_supply', {}).get('yoy_change', 0)
    if m2_yoy > 8:
        liquidity = 'expanding'
    elif m2_yoy < 0:
        liquidity = 'contracting'
    else:
        liquidity = 'stable'

    return {
        'credit': credit,
        'equities': equities,
        'rates': rates,
        'volatility': volatility,
        'dollar': dollar,
        'liquidity': liquidity
    }


def load_market_synthesis():
    """Load cached market synthesis from file."""
    if MARKET_SYNTHESIS_FILE.exists():
        try:
            with open(MARKET_SYNTHESIS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_market_synthesis(synthesis_data: dict):
    """Save market synthesis to cache file."""
    Path("data").mkdir(exist_ok=True)
    with open(MARKET_SYNTHESIS_FILE, 'w') as f:
        json.dump(synthesis_data, f, indent=2)


def generate_market_conditions_synthesis():
    """
    Generate an AI one-liner synthesis of current market conditions.

    Returns:
        dict with 'success', 'synthesis', 'statuses', 'generated_at', and 'error' keys
    """
    client, error = get_ai_client()
    if client is None:
        return {
            'success': False,
            'synthesis': None,
            'statuses': {},
            'generated_at': None,
            'error': error
        }

    try:
        # Get current dashboard data and calculate statuses
        dashboard_data = get_dashboard_data()
        statuses = calculate_market_statuses(dashboard_data)

        # Build the AI prompt
        system_prompt = """You are a financial market analyst. Given market condition statuses, write a single sentence (max 150 characters) that synthesizes the overall market environment. Be concise, objective, and highlight the 2-3 most important conditions. Write only the synthesis sentence, nothing else."""

        user_prompt = f"""Current Market Statuses:
- Credit: {statuses['credit']}
- Equities: {statuses['equities']}
- Rates: {statuses['rates']}
- Volatility: {statuses['volatility']}
- Dollar: {statuses['dollar']}
- Liquidity: {statuses['liquidity']}

Write a single sentence synthesis (max 150 characters)."""

        # Make the API call
        result = call_ai_with_tools(
            client=client,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            # Combined reasoning + output ceiling: thinking is always on and
            # shares this budget. The visible answer is one sentence; the rest
            # is headroom so reasoning cannot truncate it.
            max_tokens=4000,
            log_prefix="[Market Synthesis]"
        )

        if not result['success']:
            return {
                'success': False,
                'synthesis': None,
                'statuses': statuses,
                'generated_at': None,
                'error': result['error']
            }

        synthesis = result['content'].strip()

        # Truncate if over 200 chars (hard max)
        if len(synthesis) > 200:
            synthesis = synthesis[:197] + "..."

        generated_at = datetime.utcnow().isoformat() + 'Z'

        # Cache the result
        synthesis_data = {
            'synthesis': synthesis,
            'statuses': statuses,
            'generated_at': generated_at
        }
        save_market_synthesis(synthesis_data)

        print(f"[Market Synthesis] Generated synthesis: {synthesis}")

        return {
            'success': True,
            'synthesis': synthesis,
            'statuses': statuses,
            'generated_at': generated_at,
            'error': None
        }

    except Exception as e:
        print(f"[Market Synthesis] Error generating synthesis: {e}")
        return {
            'success': False,
            'synthesis': None,
            'statuses': {},
            'generated_at': None,
            'error': str(e)
        }


def get_market_synthesis_for_display():
    """
    Get cached market synthesis for display.
    Returns dict with synthesis info or None if not available.
    """
    cached = load_market_synthesis()
    if cached and cached.get('synthesis'):
        return cached
    return None


@app.route('/api/market-conditions-synthesis')
def api_market_conditions_synthesis():
    """Get the current AI-generated market conditions synthesis."""
    cached = get_market_synthesis_for_display()
    if cached:
        return jsonify(cached)
    # Return null synthesis if no cache exists
    return jsonify({
        'synthesis': None,
        'statuses': {},
        'generated_at': None
    })


@app.route('/api/market-conditions-synthesis/generate', methods=['POST'])
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_market_synthesis():
    """Manually trigger market conditions synthesis generation."""
    try:
        result = generate_market_conditions_synthesis()

        if result['success']:
            return jsonify({
                'status': 'success',
                'synthesis': result['synthesis'],
                'statuses': result['statuses'],
                'generated_at': result['generated_at']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


# ============================================================================
# Portfolio API Endpoints
# ============================================================================

@app.route('/api/portfolio')
@login_required
def api_portfolio_get():
    """Get all portfolio allocations with current prices for current user."""
    try:
        portfolio_data = db_get_portfolio_with_prices(current_user.id)
        return jsonify(portfolio_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio', methods=['POST'])
@csrf.exempt
@login_required
def api_portfolio_add():
    """Add a new allocation to the portfolio."""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        asset_type = data.get('asset_type')
        symbol = data.get('symbol')
        name = data.get('name')
        percentage = data.get('percentage')

        if not asset_type or not name or percentage is None:
            return jsonify({'error': 'Missing required fields: asset_type, name, percentage'}), 400

        try:
            percentage = float(percentage)
        except (ValueError, TypeError):
            return jsonify({'error': 'Percentage must be a number'}), 400

        result = db_add_allocation(current_user.id, asset_type, symbol, name, percentage)

        if 'error' in result:
            return jsonify(result), 400

        return jsonify(result), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/<allocation_id>', methods=['PUT'])
@csrf.exempt
@login_required
def api_portfolio_update(allocation_id):
    """Update an existing allocation."""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        percentage = data.get('percentage')
        name = data.get('name')
        symbol = data.get('symbol')

        if percentage is not None:
            try:
                percentage = float(percentage)
            except (ValueError, TypeError):
                return jsonify({'error': 'Percentage must be a number'}), 400

        result = db_update_allocation(current_user.id, allocation_id, percentage=percentage, name=name, symbol=symbol)

        if 'error' in result:
            return jsonify(result), 404

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/<allocation_id>', methods=['DELETE'])
@csrf.exempt
@login_required
def api_portfolio_delete(allocation_id):
    """Delete an allocation from current user's portfolio."""
    try:
        result = db_delete_allocation(current_user.id, allocation_id)

        if 'error' in result:
            return jsonify(result), 404

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/validate')
@login_required
def api_portfolio_validate():
    """Validate current user's portfolio allocation total."""
    try:
        result = db_validate_allocation_total(current_user.id)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/validate-symbol/<symbol>')
def api_portfolio_validate_symbol(symbol):
    """Validate a ticker symbol and get info."""
    try:
        result = validate_symbol(symbol)
        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/portfolio/summary')
@login_required
def api_portfolio_summary():
    """Get the current user's portfolio AI summary."""
    from ai_summary import db_get_portfolio_summary_for_display
    summary = db_get_portfolio_summary_for_display(current_user.id)
    if summary:
        return jsonify(summary)
    return jsonify({
        'error': 'No portfolio summary available',
        'message': 'Click refresh to generate an AI analysis'
    }), 404


@app.route('/api/portfolio/summary/generate', methods=['POST'])
@csrf.exempt
@login_required
@anonymous_rate_limit(CATEGORY_ANALYSIS)
def api_generate_portfolio_summary():
    """Trigger portfolio AI summary generation.

    Authenticated users get analysis of their saved portfolio.
    Anonymous users get a general portfolio guidance response.
    """
    from ai_summary import generate_portfolio_summary

    try:
        is_authenticated = current_user.is_authenticated
        user_id = current_user.id if is_authenticated else None

        if is_authenticated:
            # Get current user's portfolio data for AI
            portfolio_data = db_get_portfolio_summary_for_ai(user_id)

            if 'error' in portfolio_data:
                return jsonify({
                    'status': 'error',
                    'error': portfolio_data['error']
                }), 500

            if not portfolio_data.get('holdings'):
                return jsonify({
                    'status': 'error',
                    'error': 'No holdings in portfolio. Add some allocations first.'
                }), 400
        else:
            # Anonymous user — no portfolio data available
            return jsonify({
                'status': 'success',
                'summary': (
                    'Portfolio AI analysis requires a saved portfolio. '
                    'Subscribe to add your holdings and get '
                    'personalized AI analysis of how your portfolio aligns '
                    'with current market conditions.'
                ),
                'anonymous': True
            })

        # Generate market summary for context
        market_summary = generate_portfolio_market_context()

        # Generate AI summary using system API key
        result = generate_portfolio_summary(
            portfolio_data,
            market_summary,
            user_id=user_id
        )

        # Record usage metering for authenticated users (US-12.2.2)
        try:
            if is_authenticated:
                usage_data = result.get('usage', {})
                model_used = result.get('model')
                if model_used:
                    from services.usage_metering import record_usage
                    record_usage(
                        user_id=user_id,
                        interaction_type='portfolio_analysis',
                        model_name=model_used,
                        **usage_data,
                    )
        except Exception:
            app.logger.exception('Portfolio summary metering error (non-fatal)')

        if result['success']:
            return jsonify({
                'status': 'success',
                'summary': result['summary']
            })
        else:
            return jsonify({
                'status': 'error',
                'error': result['error']
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


def generate_portfolio_market_context():
    """Generate market context for portfolio AI analysis."""
    from ai_summary import (
        get_latest_summary, get_latest_crypto_summary,
        get_latest_equity_summary, get_latest_rates_summary,
        get_latest_dollar_summary, get_latest_credit_summary
    )

    context_parts = []

    # Get all AI briefings from today
    briefings = []

    general = get_latest_summary()
    if general:
        briefings.append(f"**General Market Briefing:**\n{general.get('summary', 'N/A')}")

    crypto = get_latest_crypto_summary()
    if crypto:
        briefings.append(f"**Crypto Briefing:**\n{crypto.get('summary', 'N/A')}")

    equity = get_latest_equity_summary()
    if equity:
        briefings.append(f"**Equity Briefing:**\n{equity.get('summary', 'N/A')}")

    rates = get_latest_rates_summary()
    if rates:
        briefings.append(f"**Rates Briefing:**\n{rates.get('summary', 'N/A')}")

    dollar = get_latest_dollar_summary()
    if dollar:
        briefings.append(f"**Dollar Briefing:**\n{dollar.get('summary', 'N/A')}")

    credit = get_latest_credit_summary()
    if credit:
        briefings.append(f"**Credit Briefing:**\n{credit.get('summary', 'N/A')}")

    if briefings:
        context_parts.append("## Today's AI Briefings\n" + "\n\n".join(briefings))

    # Get key market metrics
    try:
        metrics_summary = []

        # Credit spreads
        hy_df = load_csv_data('high_yield_spread.csv')
        if hy_df is not None and not hy_df.empty:
            hy_val = hy_df.iloc[-1][hy_df.columns[1]] * 100
            metrics_summary.append(f"- HY Spread: {hy_val:.0f} bp")

        ig_df = load_csv_data('investment_grade_spread.csv')
        if ig_df is not None and not ig_df.empty:
            ig_val = ig_df.iloc[-1][ig_df.columns[1]] * 100
            metrics_summary.append(f"- IG Spread: {ig_val:.0f} bp")

        # VIX
        vix_df = load_csv_data('vix_price.csv')
        if vix_df is not None and not vix_df.empty:
            vix_val = vix_df.iloc[-1][vix_df.columns[1]]
            metrics_summary.append(f"- VIX: {vix_val:.1f}")

        # Gold
        gold_df = load_csv_data('gold_price.csv')
        if gold_df is not None and not gold_df.empty:
            gold_val = gold_df.iloc[-1][gold_df.columns[1]] * 10
            metrics_summary.append(f"- Gold: ${gold_val:.0f}")

        # Bitcoin
        btc_df = load_csv_data('bitcoin_price.csv')
        if btc_df is not None and not btc_df.empty:
            btc_val = btc_df.iloc[-1][btc_df.columns[1]]
            metrics_summary.append(f"- Bitcoin: ${btc_val:,.0f}")

        # S&P 500
        spy_df = load_csv_data('sp500_price.csv')
        if spy_df is not None and not spy_df.empty:
            spy_val = spy_df.iloc[-1][spy_df.columns[1]]
            metrics_summary.append(f"- S&P 500 (SPY): ${spy_val:.2f}")

        # Yield curve
        yc_df = load_csv_data('yield_curve_10y2y.csv')
        if yc_df is not None and not yc_df.empty:
            yc_val = yc_df.iloc[-1][yc_df.columns[1]]
            metrics_summary.append(f"- 10Y-2Y Yield Curve: {yc_val:.2f}%")

        # DXY
        dxy_df = load_csv_data('dxy.csv')
        if dxy_df is not None and not dxy_df.empty:
            dxy_val = dxy_df.iloc[-1][dxy_df.columns[1]]
            metrics_summary.append(f"- DXY: {dxy_val:.1f}")

        if metrics_summary:
            context_parts.append("## Current Key Metrics\n" + "\n".join(metrics_summary))

    except Exception as e:
        print(f"Error generating market metrics context: {e}")

    return "\n\n".join(context_parts)


@app.route('/api/debug/web-search-status')
def api_debug_web_search():
    """Debug endpoint to check web search configuration."""
    from web_search import TAVILY_API_KEY
    return jsonify({
        'tavily_configured': is_tavily_configured(),
        'tavily_api_key_in_module': bool(TAVILY_API_KEY),
        'tavily_api_key_in_environ': bool(os.environ.get('TAVILY_API_KEY')),
        'tavily_api_key_preview': os.environ.get('TAVILY_API_KEY', '')[:8] + '...' if os.environ.get('TAVILY_API_KEY') else None
    })


def get_metric_stats(df):
    """Compute standard stats for a metric DataFrame.

    Returns a dict with: current, percentile, change_1d, change_5d, change_30d,
    pct_change_1d, pct_change_5d, pct_change_30d, high_52w, low_52w, min, max.
    Returns None if df is None or has fewer than 2 rows.
    """
    if df is None or len(df) < 2:
        return None
    col = [c for c in df.columns if c != 'date'][0]
    values = df[col].dropna()
    if len(values) < 2:
        return None
    current = float(values.iloc[-1])
    change_1d = float(values.iloc[-1] - values.iloc[-2]) if len(values) >= 2 else 0.0
    change_5d = float(values.iloc[-1] - values.iloc[-6]) if len(values) >= 6 else 0.0
    change_30d = float(values.iloc[-1] - values.iloc[-31]) if len(values) >= 31 else 0.0
    pct_change_1d = (
        float((current / float(values.iloc[-2])) - 1) * 100
        if len(values) >= 2 and float(values.iloc[-2]) != 0 else 0.0
    )
    pct_change_5d = (
        float((current / float(values.iloc[-6])) - 1) * 100
        if len(values) >= 6 and float(values.iloc[-6]) != 0 else 0.0
    )
    pct_change_30d = (
        float((current / float(values.iloc[-31])) - 1) * 100
        if len(values) >= 31 and float(values.iloc[-31]) != 0 else 0.0
    )
    high_52w = float(values.tail(252).max()) if len(values) > 252 else float(values.max())
    low_52w = float(values.tail(252).min()) if len(values) > 252 else float(values.min())
    return {
        'current': current,
        'percentile': float((values < current).sum() / len(values) * 100),
        'change_1d': change_1d,
        'change_5d': change_5d,
        'change_30d': change_30d,
        'pct_change_1d': pct_change_1d,
        'pct_change_5d': pct_change_5d,
        'pct_change_30d': pct_change_30d,
        'high_52w': high_52w,
        'low_52w': low_52w,
        'min': float(values.min()),
        'max': float(values.max()),
    }


def get_last_updated_date(df):
    """Get the most recent date from a DataFrame's date column as a string.

    Returns 'YYYY-MM-DD' or None if unavailable.
    """
    if df is None or 'date' not in df.columns or df.empty:
        return None
    last_date = df['date'].dropna().max()
    if pd.isna(last_date):
        return None
    return pd.Timestamp(last_date).strftime('%Y-%m-%d')


def generate_market_summary():
    """Generate a comprehensive market data summary for AI context."""
    try:
        eastern = pytz.timezone('US/Eastern')
        summary_parts = []
        summary_parts.append("# CURRENT MARKET DATA SUMMARY")
        summary_parts.append(f"Generated: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')} ET")
        summary_parts.append("")

        # Load all key metrics
        divergence_df = load_csv_data('divergence_gap.csv')
        hy_spread_df = load_csv_data('high_yield_spread.csv')
        ig_spread_df = load_csv_data('investment_grade_spread.csv')
        ccc_spread_df = load_csv_data('ccc_spread.csv')
        gold_df = load_csv_data('gold_price.csv')
        silver_df = load_csv_data('silver_price.csv')
        vix_df = load_csv_data('vix_price.csv')
        sp500_df = load_csv_data('sp500_price.csv')
        bitcoin_df = load_csv_data('bitcoin_price.csv')

        # Market Concentration
        smh_spy_df = load_csv_data('smh_spy_ratio.csv')
        xlk_spy_df = load_csv_data('xlk_spy_ratio.csv')
        growth_value_df = load_csv_data('growth_value_ratio.csv')
        breadth_df = load_csv_data('market_breadth_ratio.csv')

        # Yen Carry Trade
        usdjpy_df = load_csv_data('usdjpy_price.csv')
        japan_10y_df = load_csv_data('japan_10y_yield.csv')

        # Yield Curve
        yield_curve_10y2y_df = load_csv_data('yield_curve_10y2y.csv')
        yield_curve_10y3m_df = load_csv_data('yield_curve_10y3m.csv')

        # Labor Market
        initial_claims_df = load_csv_data('initial_claims.csv')
        continuing_claims_df = load_csv_data('continuing_claims.csv')

        # Economic Indicators
        consumer_confidence_df = load_csv_data('consumer_confidence.csv')
        m2_df = load_csv_data('m2_money_supply.csv')
        cpi_df = load_csv_data('cpi.csv')

        # Macro/Liquidity (new)
        fed_balance_df = load_csv_data('fed_balance_sheet.csv')
        nfci_df = load_csv_data('nfci.csv')
        reverse_repo_df = load_csv_data('reverse_repo.csv')
        fed_funds_df = load_csv_data('fed_funds_rate.csv')
        tga_df = load_csv_data('treasury_general_account.csv')
        stl_stress_df = load_csv_data('stl_financial_stress.csv')

        # Rates (new)
        treasury_10y_df = load_csv_data('treasury_10y.csv')
        breakeven_df = load_csv_data('breakeven_inflation_10y.csv')
        real_yield_df = load_csv_data('real_yield_10y.csv')

        # Equities (new)
        nasdaq_df = load_csv_data('nasdaq_price.csv')
        small_cap_df = load_csv_data('small_cap_price.csv')
        iwm_spy_df = load_csv_data('iwm_spy_ratio.csv')

        # Currency (new)
        dxy_df = load_csv_data('dollar_index_price.csv')

        # Commodities (new)
        oil_df = load_csv_data('oil_price.csv')
        commodities_df = load_csv_data('commodities_price.csv')

        # Crypto/Sentiment (new)
        fear_greed_df = load_csv_data('fear_greed_index.csv')
        ethereum_df = load_csv_data('ethereum_price.csv')

        # Monthly economic (new)
        unemployment_df = load_csv_data('unemployment_rate.csv')
        core_pce_df = load_csv_data('core_pce_price_index.csv')
        industrial_prod_df = load_csv_data('industrial_production.csv')
        building_permits_df = load_csv_data('building_permits.csv')
        trade_balance_df = load_csv_data('trade_balance.csv')
        cli_df = load_csv_data('cli.csv')
        property_cpi_rent_df = load_csv_data('property_cpi_rent.csv')
        property_hpi_df = load_csv_data('property_hpi.csv')
        property_vacancy_df = load_csv_data('property_vacancy.csv')
        ism_manufacturing_df = load_csv_data('ism_manufacturing.csv')
        pce_price_df = load_csv_data('pce_price_index.csv')
        property_farmland_df = load_csv_data('property_farmland.csv')

        # Quarterly economic (new)
        natural_unemployment_df = load_csv_data('natural_unemployment_rate.csv')
        real_gdp_df = load_csv_data('real_gdp.csv')
        potential_gdp_df = load_csv_data('potential_gdp.csv')

        # Helper for YoY change (for monthly data)
        def get_yoy(df):
            if df is None or len(df) < 13:
                return None
            values = df[df.columns[1]].dropna()
            if len(values) < 13:
                return None
            current = float(values.iloc[-1])
            year_ago = float(values.iloc[-13])
            return ((current / year_ago) - 1) * 100 if year_ago != 0 else 0

        # Credit Spreads Section (daily data)
        summary_parts.append("## CREDIT SPREADS")
        if hy_spread_df is not None:
            stats = get_metric_stats(hy_spread_df)
            if stats:
                summary_parts.append(f"High Yield (HY): {stats['current']*100:.0f} bp ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if ig_spread_df is not None:
            stats = get_metric_stats(ig_spread_df)
            if stats:
                summary_parts.append(f"Investment Grade (IG): {stats['current']*100:.0f} bp ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if ccc_spread_df is not None:
            stats = get_metric_stats(ccc_spread_df)
            if stats:
                summary_parts.append(f"CCC-rated: {stats['current']*100:.0f} bp ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Safe Havens Section (daily data)
        summary_parts.append("## SAFE HAVEN ASSETS")
        if gold_df is not None:
            stats = get_metric_stats(gold_df)
            if stats:
                summary_parts.append(f"Gold (GLD×10): ${stats['current']*10:,.0f}/oz equiv ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                pct_from_52h = (stats['current'] / stats['high_52w'] - 1) * 100 if stats['high_52w'] != 0 else 0
                summary_parts.append(f"  52w range: ${stats['low_52w']*10:,.0f}–${stats['high_52w']*10:,.0f} (currently {pct_from_52h:+.1f}% from 52w high)")
        if silver_df is not None:
            stats = get_metric_stats(silver_df)
            if stats:
                summary_parts.append(f"Silver (SLV): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if bitcoin_df is not None:
            stats = get_metric_stats(bitcoin_df)
            if stats:
                summary_parts.append(f"Bitcoin: ${stats['current']:,.0f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                pct_from_52h = (stats['current'] / stats['high_52w'] - 1) * 100 if stats['high_52w'] != 0 else 0
                summary_parts.append(f"  52w range: ${stats['low_52w']:,.0f}–${stats['high_52w']:,.0f} (currently {pct_from_52h:+.1f}% from 52w high)")
        if ethereum_df is not None:
            stats = get_metric_stats(ethereum_df)
            if stats:
                summary_parts.append(f"Ethereum: ${stats['current']:,.0f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if fear_greed_df is not None:
            stats = get_metric_stats(fear_greed_df)
            if stats:
                summary_parts.append(f"Crypto Fear & Greed: {stats['current']:.0f}/100 ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']:+.0f} pts | 5d: {stats['change_5d']:+.0f} pts")
        summary_parts.append("")

        # Equity Markets (daily data)
        summary_parts.append("## EQUITY MARKETS")
        if sp500_df is not None:
            stats = get_metric_stats(sp500_df)
            if stats:
                summary_parts.append(f"S&P 500: ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                pct_from_52h = (stats['current'] / stats['high_52w'] - 1) * 100 if stats['high_52w'] != 0 else 0
                summary_parts.append(f"  52w range: ${stats['low_52w']:.0f}–${stats['high_52w']:.0f} (currently {pct_from_52h:+.1f}% from 52w high)")
        if nasdaq_df is not None:
            stats = get_metric_stats(nasdaq_df)
            if stats:
                summary_parts.append(f"Nasdaq 100: ${stats['current']:,.0f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if small_cap_df is not None:
            stats = get_metric_stats(small_cap_df)
            if stats:
                summary_parts.append(f"Russell 2000 (Small Caps): ${stats['current']:,.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if vix_df is not None:
            stats = get_metric_stats(vix_df)
            if stats:
                summary_parts.append(f"VIX (Fear Index): {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Market Concentration (daily data)
        summary_parts.append("## MARKET CONCENTRATION (Higher = More AI/Tech Concentration)")
        if smh_spy_df is not None:
            stats = get_metric_stats(smh_spy_df)
            if stats:
                summary_parts.append(f"Semiconductor/SPY: {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if xlk_spy_df is not None:
            stats = get_metric_stats(xlk_spy_df)
            if stats:
                summary_parts.append(f"Tech Sector/SPY: {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if growth_value_df is not None:
            stats = get_metric_stats(growth_value_df)
            if stats:
                summary_parts.append(f"Growth/Value: {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if breadth_df is not None:
            stats = get_metric_stats(breadth_df)
            if stats:
                summary_parts.append(f"Market Breadth (SPY/RSP): {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if iwm_spy_df is not None:
            stats = get_metric_stats(iwm_spy_df)
            if stats:
                summary_parts.append(f"Small Cap/Large Cap (IWM/SPY): {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Yen Carry Trade (daily data)
        summary_parts.append("## YEN CARRY TRADE MONITOR")
        if usdjpy_df is not None:
            stats = get_metric_stats(usdjpy_df)
            if stats:
                summary_parts.append(f"USD/JPY: {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Lower = stronger yen = carry trade unwinding risk)")
        if japan_10y_df is not None:
            stats = get_metric_stats(japan_10y_df)
            if stats:
                summary_parts.append(f"Japan 10Y Yield: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']:+.3f}% | 5d: {stats['change_5d']:+.3f}% | 30d: {stats['change_30d']:+.3f}%")
                summary_parts.append("  (Rising yields = BOJ tightening = carry trade at risk)")
        summary_parts.append("")

        # Yield Curve (Recession Indicators) (daily data)
        summary_parts.append("## YIELD CURVE (Recession Indicators)")
        if yield_curve_10y2y_df is not None:
            stats = get_metric_stats(yield_curve_10y2y_df)
            if stats:
                status = "INVERTED" if stats['current'] < 0 else "Normal"
                summary_parts.append(f"10Y-2Y Spread: {stats['current']:.2f}% [{status}] ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']:+.2f}% | 5d: {stats['change_5d']:+.2f}% | 30d: {stats['change_30d']:+.2f}%")
        if yield_curve_10y3m_df is not None:
            stats = get_metric_stats(yield_curve_10y3m_df)
            if stats:
                status = "INVERTED" if stats['current'] < 0 else "Normal"
                summary_parts.append(f"10Y-3M Spread: {stats['current']:.2f}% [{status}] ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']:+.2f}% | 5d: {stats['change_5d']:+.2f}% | 30d: {stats['change_30d']:+.2f}%")
        summary_parts.append("  (Negative = inverted yield curve, historically precedes recessions by 12-18 months)")
        summary_parts.append("")

        # Rates & Inflation (daily data)
        summary_parts.append("## RATES & INFLATION")
        if treasury_10y_df is not None:
            stats = get_metric_stats(treasury_10y_df)
            if stats:
                summary_parts.append(f"10Y Treasury: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 5d: {stats['change_5d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
        if breakeven_df is not None:
            stats = get_metric_stats(breakeven_df)
            if stats:
                summary_parts.append(f"10Y Breakeven Inflation: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 5d: {stats['change_5d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
        if real_yield_df is not None:
            stats = get_metric_stats(real_yield_df)
            if stats:
                summary_parts.append(f"10Y Real Yield (TIPS): {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 5d: {stats['change_5d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
        if fed_funds_df is not None:
            stats = get_metric_stats(fed_funds_df)
            if stats:
                summary_parts.append(f"Fed Funds Rate: {stats['current']:.2f}%")
        summary_parts.append("")

        # Macro Liquidity
        summary_parts.append("## MACRO LIQUIDITY")
        if fed_balance_df is not None:
            stats = get_metric_stats(fed_balance_df)
            if stats:
                trend = "EXPANDING" if stats['change_30d'] > 0 else "SHRINKING"
                summary_parts.append(f"Fed Balance Sheet: ${stats['current']/1000:.2f}T [{trend}] | 5d: {stats['pct_change_5d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if reverse_repo_df is not None:
            stats = get_metric_stats(reverse_repo_df)
            if stats:
                summary_parts.append(f"Reverse Repo (RRP): ${stats['current']:.0f}B ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if tga_df is not None:
            stats = get_metric_stats(tga_df)
            if stats:
                summary_parts.append(f"Treasury General Account: ${stats['current']:.0f}B ({stats['percentile']:.1f}th %ile) | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if nfci_df is not None:
            stats = get_metric_stats(nfci_df)
            if stats:
                condition = "TIGHT" if stats['current'] > 0 else "Loose"
                summary_parts.append(f"NFCI (Financial Conditions): {stats['current']:.2f} [{condition}] ({stats['percentile']:.1f}th %ile) | 5d: {stats['change_5d']:+.2f} | 30d: {stats['change_30d']:+.2f}")
        if stl_stress_df is not None:
            stats = get_metric_stats(stl_stress_df)
            if stats:
                condition = "ELEVATED" if stats['current'] > 0 else "Below average"
                summary_parts.append(f"St. Louis Financial Stress: {stats['current']:.2f} [{condition}] ({stats['percentile']:.1f}th %ile) | 5d: {stats['change_5d']:+.2f} | 30d: {stats['change_30d']:+.2f}")
        summary_parts.append("")

        # Currency (daily data)
        summary_parts.append("## CURRENCY")
        if dxy_df is not None:
            stats = get_metric_stats(dxy_df)
            if stats:
                summary_parts.append(f"Dollar Index (DXY): {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 5d: {stats['pct_change_5d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        summary_parts.append("")

        # Commodities (daily data)
        summary_parts.append("## COMMODITIES")
        if oil_df is not None:
            stats = get_metric_stats(oil_df)
            if stats:
                summary_parts.append(f"Oil (USO): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if commodities_df is not None:
            stats = get_metric_stats(commodities_df)
            if stats:
                summary_parts.append(f"Broad Commodities (DJP): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Labor Market (weekly data — no 1d change)
        summary_parts.append("## LABOR MARKET (Weekly Data)")
        if initial_claims_df is not None:
            stats = get_metric_stats(initial_claims_df)
            if stats:
                level = "Low" if stats['current'] < 250 else "Elevated" if stats['current'] > 300 else "Normal"
                summary_parts.append(f"Initial Claims: {stats['current']:.0f}k [{level}] ({stats['percentile']:.1f}th %ile) | 5d: {stats['change_5d']:+.0f}k | 30d: {stats['change_30d']:+.0f}k")
        if continuing_claims_df is not None:
            stats = get_metric_stats(continuing_claims_df)
            if stats:
                summary_parts.append(f"Continuing Claims: {stats['current']:.0f}k ({stats['percentile']:.1f}th %ile) | 5d: {stats['change_5d']:+.0f}k | 30d: {stats['change_30d']:+.0f}k")
        summary_parts.append("  (Rising claims = labor market weakening; Initial >300k = warning, >400k = recession signal)")
        summary_parts.append("")

        # Economic Indicators
        summary_parts.append("## ECONOMIC INDICATORS (Monthly Data — last-updated dates shown)")
        if consumer_confidence_df is not None:
            stats = get_metric_stats(consumer_confidence_df)
            updated = get_last_updated_date(consumer_confidence_df)
            if stats:
                summary_parts.append(f"Consumer Confidence (UMich): {stats['current']:.1f} ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
                summary_parts.append("  (<70 = recession territory, >100 = strong optimism)")
        if m2_df is not None:
            stats = get_metric_stats(m2_df)
            yoy = get_yoy(m2_df)
            updated = get_last_updated_date(m2_df)
            if stats:
                summary_parts.append(f"M2 Money Supply: ${stats['current']/1000:.2f} trillion | YoY: {yoy:+.1f}% | Last updated: {updated}")
                summary_parts.append("  (Negative YoY = highly unusual, deflationary)")
        if cpi_df is not None:
            stats = get_metric_stats(cpi_df)
            yoy = get_yoy(cpi_df)
            updated = get_last_updated_date(cpi_df)
            if stats:
                summary_parts.append(f"CPI Index: {stats['current']:.1f} | YoY Inflation: {yoy:+.1f}% | Last updated: {updated}")
                summary_parts.append("  (Fed target ~2%, >5% = high inflation)")
        if unemployment_df is not None:
            stats = get_metric_stats(unemployment_df)
            updated = get_last_updated_date(unemployment_df)
            if stats:
                summary_parts.append(f"Unemployment Rate: {stats['current']:.1f}% ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
        if core_pce_df is not None:
            stats = get_metric_stats(core_pce_df)
            yoy = get_yoy(core_pce_df)
            updated = get_last_updated_date(core_pce_df)
            if stats:
                yoy_str = f" | YoY: {yoy:+.1f}%" if yoy is not None else ""
                summary_parts.append(f"Core PCE Price Index: {stats['current']:.2f}{yoy_str} | Last updated: {updated}")
                summary_parts.append("  (Fed's preferred inflation gauge)")
        if industrial_prod_df is not None:
            stats = get_metric_stats(industrial_prod_df)
            updated = get_last_updated_date(industrial_prod_df)
            if stats:
                summary_parts.append(f"Industrial Production: {stats['current']:.1f} ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
        if building_permits_df is not None:
            stats = get_metric_stats(building_permits_df)
            updated = get_last_updated_date(building_permits_df)
            if stats:
                summary_parts.append(f"Building Permits: {stats['current']:.0f}k ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
        if trade_balance_df is not None:
            stats = get_metric_stats(trade_balance_df)
            updated = get_last_updated_date(trade_balance_df)
            if stats:
                summary_parts.append(f"Trade Balance: ${stats['current']:.1f}B ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
        if cli_df is not None:
            stats = get_metric_stats(cli_df)
            updated = get_last_updated_date(cli_df)
            if stats:
                summary_parts.append(f"Leading Economic Index (CLI): {stats['current']:.1f} ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
                summary_parts.append("  (Declining = economic slowdown ahead)")
        if ism_manufacturing_df is not None:
            stats = get_metric_stats(ism_manufacturing_df)
            updated = get_last_updated_date(ism_manufacturing_df)
            if stats:
                condition = "Expanding" if stats['current'] > 50 else "Contracting"
                summary_parts.append(f"ISM Manufacturing PMI: {stats['current']:.1f} [{condition}] ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
                summary_parts.append("  (>50 = expansion, <50 = contraction)")
        if pce_price_df is not None:
            stats = get_metric_stats(pce_price_df)
            yoy = get_yoy(pce_price_df)
            updated = get_last_updated_date(pce_price_df)
            if stats:
                yoy_str = f" | YoY: {yoy:+.1f}%" if yoy is not None else ""
                summary_parts.append(f"PCE Price Index: {stats['current']:.2f}{yoy_str} | Last updated: {updated}")
        if property_hpi_df is not None:
            stats = get_metric_stats(property_hpi_df)
            yoy = get_yoy(property_hpi_df)
            updated = get_last_updated_date(property_hpi_df)
            if stats:
                yoy_str = f" | YoY: {yoy:+.1f}%" if yoy is not None else ""
                summary_parts.append(f"Case-Shiller Home Price Index: {stats['current']:.1f}{yoy_str} | Last updated: {updated}")
        if property_cpi_rent_df is not None:
            stats = get_metric_stats(property_cpi_rent_df)
            yoy = get_yoy(property_cpi_rent_df)
            updated = get_last_updated_date(property_cpi_rent_df)
            if stats:
                yoy_str = f" | YoY: {yoy:+.1f}%" if yoy is not None else ""
                summary_parts.append(f"CPI Rent of Primary Residence: {stats['current']:.1f}{yoy_str} | Last updated: {updated}")
        if property_vacancy_df is not None:
            stats = get_metric_stats(property_vacancy_df)
            updated = get_last_updated_date(property_vacancy_df)
            if stats:
                summary_parts.append(f"Rental Vacancy Rate: {stats['current']:.1f}% ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
        if property_farmland_df is not None:
            stats = get_metric_stats(property_farmland_df)
            updated = get_last_updated_date(property_farmland_df)
            if stats:
                summary_parts.append(f"USDA Farmland Value: ${stats['current']:,.0f}/acre ({stats['percentile']:.1f}th %ile) | Last updated: {updated}")
        summary_parts.append("")

        # Quarterly Economic Data
        summary_parts.append("## QUARTERLY ECONOMIC DATA")
        if real_gdp_df is not None:
            stats = get_metric_stats(real_gdp_df)
            updated = get_last_updated_date(real_gdp_df)
            if stats:
                summary_parts.append(f"Real GDP: ${stats['current']:,.0f}B (chained 2017$) | Last updated: {updated}")
                if potential_gdp_df is not None:
                    pot_stats = get_metric_stats(potential_gdp_df)
                    if pot_stats:
                        output_gap = ((stats['current'] / pot_stats['current']) - 1) * 100
                        summary_parts.append(f"  Output gap (actual vs potential): {output_gap:+.1f}%")
        if potential_gdp_df is not None:
            stats = get_metric_stats(potential_gdp_df)
            updated = get_last_updated_date(potential_gdp_df)
            if stats:
                summary_parts.append(f"CBO Potential GDP: ${stats['current']:,.0f}B | Last updated: {updated}")
        if natural_unemployment_df is not None:
            stats = get_metric_stats(natural_unemployment_df)
            updated = get_last_updated_date(natural_unemployment_df)
            if stats:
                summary_parts.append(f"Natural Unemployment Rate (NAIRU): {stats['current']:.1f}% | Last updated: {updated}")
                if unemployment_df is not None:
                    u_stats = get_metric_stats(unemployment_df)
                    if u_stats:
                        gap = u_stats['current'] - stats['current']
                        summary_parts.append(f"  Unemployment gap (actual - natural): {gap:+.1f}pp")
        summary_parts.append("")

        # Divergence Gap (Gold vs Credit)
        if divergence_df is not None:
            stats = get_metric_stats(divergence_df)
            if stats:
                summary_parts.append("## DIVERGENCE GAP (Gold-Implied Spread minus Actual HY Spread)")
                summary_parts.append(f"Current: {stats['current']:.0f} bp ({stats['percentile']:.1f}th percentile)")
                summary_parts.append(f"1-Day Change: {stats['change_1d']:+.0f} bp | 5-Day Change: {stats['change_5d']:+.0f} bp | 30-Day Change: {stats['change_30d']:+.0f} bp")
                summary_parts.append(f"Historical Range: {stats['min']:.0f} - {stats['max']:.0f} bp")
                summary_parts.append("")


        # Recession Probability Models
        try:
            recession = get_recession_probability()
            if recession:
                model_parts = []
                if recession.get('ny_fed') is not None:
                    model_parts.append(
                        f"NY Fed 12m: {recession['ny_fed']:.1f}% ({recession.get('ny_fed_risk', 'N/A')})"
                    )
                if recession.get('chauvet_piger') is not None:
                    model_parts.append(
                        f"Chauvet-Piger: {recession['chauvet_piger']:.1f}% ({recession.get('chauvet_piger_risk', 'N/A')})"
                    )
                if recession.get('richmond_sos') is not None:
                    model_parts.append(
                        f"Richmond SOS: {recession['richmond_sos']:.1f}% ({recession.get('richmond_sos_risk', 'N/A')})"
                    )
                if model_parts:
                    summary_parts.append("## RECESSION PROBABILITY MODELS")
                    summary_parts.append(", ".join(model_parts))
                    summary_parts.append("")
        except Exception:
            pass  # Skip if recession data unavailable

        return "\n".join(summary_parts)

    except Exception as e:
        print(f"Error generating market summary: {e}")
        return "Market data summary unavailable."


def generate_crypto_market_summary():
    """Generate a crypto-focused market data summary for the Crypto AI briefing."""
    try:
        eastern = pytz.timezone('US/Eastern')
        summary_parts = []
        summary_parts.append("# CRYPTO/BITCOIN MARKET DATA SUMMARY")
        summary_parts.append(f"Generated: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')} ET")
        summary_parts.append("")

        # Load crypto-relevant metrics
        bitcoin_df = load_csv_data('bitcoin_price.csv')
        gold_df = load_csv_data('gold_price.csv')
        btc_gold_ratio_df = load_csv_data('btc_gold_ratio.csv')
        fear_greed_df = load_csv_data('fear_greed_index.csv')
        fed_balance_df = load_csv_data('fed_balance_sheet.csv')
        reverse_repo_df = load_csv_data('reverse_repo.csv')
        nfci_df = load_csv_data('nfci.csv')
        m2_df = load_csv_data('m2_money_supply.csv')
        vix_df = load_csv_data('vix_price.csv')
        dxy_df = load_csv_data('dollar_index_price.csv')
        sp500_df = load_csv_data('sp500_price.csv')
        nasdaq_df = load_csv_data('nasdaq_price.csv')
        gold_miners_df = load_csv_data('gold_miners_price.csv')
        gdx_gld_ratio_df = load_csv_data('gdx_gld_ratio.csv')
        gold_silver_ratio_df = load_csv_data('gold_silver_ratio.csv')

        # Bitcoin Section
        summary_parts.append("## BITCOIN")
        if bitcoin_df is not None:
            stats = get_metric_stats(bitcoin_df)
            if stats:
                summary_parts.append(f"Price: ${stats['current']:,.0f} ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append(f"  52-week range: ${stats['low_52w']:,.0f} - ${stats['high_52w']:,.0f}")

                # Distance from 52w high/low
                dist_from_high = ((stats['current'] / stats['high_52w']) - 1) * 100
                dist_from_low = ((stats['current'] / stats['low_52w']) - 1) * 100
                summary_parts.append(f"  Distance from 52w high: {dist_from_high:.1f}% | from low: +{dist_from_low:.1f}%")
        summary_parts.append("")

        # Fear & Greed Index
        summary_parts.append("## CRYPTO SENTIMENT")
        if fear_greed_df is not None:
            stats = get_metric_stats(fear_greed_df)
            if stats:
                # Interpret the level
                level = stats['current']
                if level <= 25:
                    interpretation = "EXTREME FEAR (historically good buying zone)"
                elif level <= 46:
                    interpretation = "Fear"
                elif level <= 54:
                    interpretation = "Neutral"
                elif level <= 75:
                    interpretation = "Greed"
                else:
                    interpretation = "EXTREME GREED (historically poor time to buy)"

                summary_parts.append(f"Fear & Greed Index: {level:.0f}/100 [{interpretation}]")
                summary_parts.append(f"  1-day change: {stats['change_1d']:+.0f} pts | 5-day: {stats['change_5d']:+.0f} pts | 30-day: {stats['change_30d']:+.0f} pts")
                summary_parts.append(f"  Historical context: {stats['percentile']:.1f}th percentile")

                # Key levels
                summary_parts.append("  KEY LEVELS: <25 = extreme fear (contrarian buy), >75 = extreme greed (caution)")
        summary_parts.append("")

        # BTC/Gold Ratio
        summary_parts.append("## BTC/GOLD RATIO")
        if btc_gold_ratio_df is not None:
            stats = get_metric_stats(btc_gold_ratio_df)
            if stats:
                summary_parts.append(f"BTC priced in gold: {stats['current']:.1f} oz ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Rising = BTC outperforming gold, risk-on; Falling = gold outperforming, risk-off)")
        summary_parts.append("")

        # Gold Mining & Precious Metals
        summary_parts.append("## PRECIOUS METALS CONTEXT")
        if gold_miners_df is not None:
            stats = get_metric_stats(gold_miners_df)
            if stats:
                summary_parts.append(f"Gold Miners (GDX): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if gdx_gld_ratio_df is not None:
            stats = get_metric_stats(gdx_gld_ratio_df)
            if stats:
                summary_parts.append(f"GDX/GLD Ratio: {stats['current']:.3f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Miners outperforming gold = risk-on precious metals sentiment)")
        if gold_silver_ratio_df is not None:
            stats = get_metric_stats(gold_silver_ratio_df)
            if stats:
                summary_parts.append(f"Gold/Silver Ratio: {stats['current']:.1f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Rising = risk-off; Falling = industrial demand/risk-on)")
        summary_parts.append("")

        # Liquidity Indicators
        summary_parts.append("## LIQUIDITY INDICATORS (Key BTC Drivers)")

        if fed_balance_df is not None:
            stats = get_metric_stats(fed_balance_df)
            if stats:
                trend = "EXPANDING (bullish for BTC)" if stats['change_30d'] > 0 else "SHRINKING (headwind for BTC)"
                summary_parts.append(f"Fed Balance Sheet: ${stats['current']/1000:.2f} trillion [{trend}]")
                summary_parts.append(f"  5-day: {stats['pct_change_5d']:+.2f}% | 30-day: {stats['pct_change_30d']:+.2f}%")

        if reverse_repo_df is not None:
            stats = get_metric_stats(reverse_repo_df)
            if stats:
                trend = "Rising (liquidity parking at Fed)" if stats['change_30d'] > 0 else "Declining (liquidity entering markets)"
                summary_parts.append(f"Reverse Repo (RRP): ${stats['current']:.0f}B [{trend}]")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")

        if nfci_df is not None:
            stats = get_metric_stats(nfci_df)
            if stats:
                if stats['current'] > 0.5:
                    condition = "TIGHT (bearish for risk assets)"
                elif stats['current'] > 0:
                    condition = "Tightening"
                elif stats['current'] > -0.5:
                    condition = "Loose (bullish for risk assets)"
                else:
                    condition = "VERY LOOSE (bullish for risk assets)"
                summary_parts.append(f"NFCI (Financial Conditions): {stats['current']:.2f} [{condition}]")
                summary_parts.append(f"  5-day: {stats['change_5d']:+.2f} | 30-day: {stats['change_30d']:+.2f}")
                summary_parts.append("  (Negative = loose conditions favor BTC; Positive = tight conditions = headwind)")

        if m2_df is not None:
            stats = get_metric_stats(m2_df)
            if stats:
                # Calculate YoY change
                if len(m2_df) > 12:
                    yoy = ((stats['current'] / m2_df.iloc[-13][[c for c in m2_df.columns if c != 'date'][0]]) - 1) * 100
                else:
                    yoy = 0
                summary_parts.append(f"M2 Money Supply: ${stats['current']/1000:.1f} trillion | YoY: {yoy:+.1f}%")
                summary_parts.append("  (BTC correlates ~0.7-0.8 with global M2)")
        summary_parts.append("")

        # Risk Context
        summary_parts.append("## RISK CONTEXT")
        if vix_df is not None:
            stats = get_metric_stats(vix_df)
            if stats:
                level = "HIGH FEAR" if stats['current'] > 30 else "Elevated" if stats['current'] > 20 else "Low"
                summary_parts.append(f"VIX: {stats['current']:.1f} [{level}] | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}%")

        if dxy_df is not None:
            stats = get_metric_stats(dxy_df)
            if stats:
                trend = "Strengthening (BTC headwind)" if stats['change_30d'] > 0 else "Weakening (BTC tailwind)"
                summary_parts.append(f"Dollar Index (DXY): {stats['current']:.2f} [{trend}] | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")

        if sp500_df is not None:
            stats = get_metric_stats(sp500_df)
            if stats:
                summary_parts.append(f"S&P 500: {stats['current']:,.0f} | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")

        if nasdaq_df is not None:
            stats = get_metric_stats(nasdaq_df)
            if stats:
                summary_parts.append(f"Nasdaq 100: {stats['current']:,.0f} | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Key Insights
        summary_parts.append("## KEY RELATIONSHIPS TO MONITOR")
        summary_parts.append("- Fed Balance Sheet expanding + NFCI negative = bullish liquidity setup for BTC")
        summary_parts.append("- Fear & Greed <25 + Fed not tightening = historical buying opportunity")
        summary_parts.append("- BTC/Gold ratio falling while gold rises = risk-off rotation")
        summary_parts.append("- VIX spiking + NFCI positive = expect BTC volatility/downside")
        summary_parts.append("")

        return "\n".join(summary_parts)

    except Exception as e:
        print(f"Error generating crypto market summary: {e}")
        return "Crypto market data summary unavailable."


def generate_equity_market_summary():
    """Generate an equity-focused market data summary for the Equity AI briefing."""
    try:
        eastern = pytz.timezone('US/Eastern')
        summary_parts = []
        summary_parts.append("# EQUITY MARKETS DATA SUMMARY")
        summary_parts.append(f"Generated: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')} ET")
        summary_parts.append("")

        # Load equity-relevant metrics
        sp500_df = load_csv_data('sp500_price.csv')
        nasdaq_df = load_csv_data('nasdaq_price.csv')
        small_cap_df = load_csv_data('small_cap_price.csv')
        vix_df = load_csv_data('vix_price.csv')
        vix_3m_df = load_csv_data('vix_3month.csv')
        breadth_df = load_csv_data('market_breadth_ratio.csv')
        smh_spy_df = load_csv_data('smh_spy_ratio.csv')
        growth_value_df = load_csv_data('growth_value_ratio.csv')
        iwm_spy_df = load_csv_data('iwm_spy_ratio.csv')
        qqq_spy_df = load_csv_data('qqq_spy_ratio.csv')
        financials_df = load_csv_data('financials_sector_price.csv')
        energy_df = load_csv_data('energy_sector_price.csv')
        semi_df = load_csv_data('semiconductor_price.csv')
        tech_df = load_csv_data('tech_sector_price.csv')
        sp500_ew_df = load_csv_data('sp500_equal_weight_price.csv')
        growth_df = load_csv_data('growth_price.csv')
        value_df = load_csv_data('value_price.csv')

        # Core Indices Section
        summary_parts.append("## CORE INDICES")
        if sp500_df is not None:
            stats = get_metric_stats(sp500_df)
            if stats:
                summary_parts.append(f"S&P 500: {stats['current']:,.0f} ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")

        if nasdaq_df is not None:
            stats = get_metric_stats(nasdaq_df)
            if stats:
                summary_parts.append(f"Nasdaq 100: {stats['current']:,.0f} ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")

        if small_cap_df is not None:
            stats = get_metric_stats(small_cap_df)
            if stats:
                summary_parts.append(f"Russell 2000 (Small Caps): ${stats['current']:,.2f} ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # VIX Section
        summary_parts.append("## VOLATILITY")
        if vix_df is not None:
            stats = get_metric_stats(vix_df)
            if stats:
                if stats['current'] < 15:
                    level = "LOW (complacency)"
                elif stats['current'] < 20:
                    level = "Normal"
                elif stats['current'] < 30:
                    level = "Elevated"
                else:
                    level = "HIGH FEAR"
                summary_parts.append(f"VIX: {stats['current']:.1f} [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  KEY LEVELS: <15 = complacent, 15-20 = normal, 20-30 = elevated, >30 = fear")
        if vix_3m_df is not None:
            stats = get_metric_stats(vix_3m_df)
            if stats:
                summary_parts.append(f"VIX 3-Month: {stats['current']:.1f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
                if vix_df is not None:
                    vix_stats = get_metric_stats(vix_df)
                    if vix_stats:
                        term_structure = "Contango (normal)" if stats['current'] > vix_stats['current'] else "Backwardation (elevated fear)"
                        summary_parts.append(f"  VIX term structure: [{term_structure}]")
        summary_parts.append("")

        # Market Structure Section
        summary_parts.append("## MARKET STRUCTURE (Breadth & Concentration)")
        if breadth_df is not None:
            stats = get_metric_stats(breadth_df)
            if stats:
                if stats['percentile'] < 20:
                    condition = "VERY NARROW (concentrated)"
                elif stats['percentile'] < 40:
                    condition = "Narrow"
                elif stats['percentile'] < 60:
                    condition = "Normal"
                elif stats['percentile'] < 80:
                    condition = "Broad"
                else:
                    condition = "VERY BROAD (healthy)"
                summary_parts.append(f"Market Breadth (RSP/SPY): {stats['current']:.2f} [{condition}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Low = few stocks driving gains; High = broad participation)")

        if smh_spy_df is not None:
            stats = get_metric_stats(smh_spy_df)
            if stats:
                if stats['percentile'] > 90:
                    level = "EXTREME concentration"
                elif stats['percentile'] > 75:
                    level = "High concentration"
                else:
                    level = "Normal"
                summary_parts.append(f"Semiconductor/SPY Ratio: {stats['current']:.1f} [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Proxy for AI/Mag 7 concentration)")

        if qqq_spy_df is not None:
            stats = get_metric_stats(qqq_spy_df)
            if stats:
                if stats['percentile'] > 80:
                    level = "EXTREME tech tilt"
                elif stats['percentile'] > 60:
                    level = "Growth-heavy"
                else:
                    level = "Normal"
                summary_parts.append(f"QQQ/SPY Ratio: {stats['current']:.1f} [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Tech/growth vs broad market leadership)")
        summary_parts.append("")

        # Style Rotation Section
        summary_parts.append("## STYLE & SIZE ROTATION")
        if growth_value_df is not None:
            stats = get_metric_stats(growth_value_df)
            if stats:
                if stats['percentile'] > 80:
                    bias = "Strong GROWTH leadership"
                elif stats['percentile'] > 60:
                    bias = "Growth favored"
                elif stats['percentile'] > 40:
                    bias = "Balanced"
                elif stats['percentile'] > 20:
                    bias = "Value favored"
                else:
                    bias = "Strong VALUE leadership"
                summary_parts.append(f"Growth/Value Ratio: {stats['current']:.1f} [{bias}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")

        if iwm_spy_df is not None:
            stats = get_metric_stats(iwm_spy_df)
            if stats:
                if stats['percentile'] > 70:
                    bias = "Small caps leading (risk-on)"
                elif stats['percentile'] > 40:
                    bias = "Balanced"
                else:
                    bias = "Large caps leading (quality flight)"
                summary_parts.append(f"Small Cap/Large Cap Ratio: {stats['current']:.1f} [{bias}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.1f}% | 5-day: {stats['pct_change_5d']:+.1f}% | 30-day: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Key Sectors Section
        summary_parts.append("## KEY SECTORS")
        if semi_df is not None:
            stats = get_metric_stats(semi_df)
            if stats:
                summary_parts.append(f"Semiconductors (SMH): ${stats['current']:,.2f} | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")

        if financials_df is not None:
            stats = get_metric_stats(financials_df)
            if stats:
                summary_parts.append(f"Financials (XLF): ${stats['current']:,.2f} | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")

        if energy_df is not None:
            stats = get_metric_stats(energy_df)
            if stats:
                summary_parts.append(f"Energy (XLE): ${stats['current']:,.2f} | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if tech_df is not None:
            stats = get_metric_stats(tech_df)
            if stats:
                summary_parts.append(f"Technology (XLK): ${stats['current']:,.2f} | 1d: {stats['pct_change_1d']:+.1f}% | 5d: {stats['pct_change_5d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Style ETFs
        summary_parts.append("## STYLE & BREADTH ETFs")
        if sp500_ew_df is not None:
            stats = get_metric_stats(sp500_ew_df)
            if stats:
                summary_parts.append(f"S&P 500 Equal Weight (RSP): ${stats['current']:,.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if growth_df is not None:
            stats = get_metric_stats(growth_df)
            if stats:
                summary_parts.append(f"Growth (VUG): ${stats['current']:,.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        if value_df is not None:
            stats = get_metric_stats(value_df)
            if stats:
                summary_parts.append(f"Value (VTV): ${stats['current']:,.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.1f}% | 30d: {stats['pct_change_30d']:+.1f}%")
        summary_parts.append("")

        # Key Insights
        summary_parts.append("## KEY RELATIONSHIPS TO MONITOR")
        summary_parts.append("- Breadth low + indices high = fragile rally dependent on few stocks")
        summary_parts.append("- Small caps leading = risk appetite, economic optimism")
        summary_parts.append("- Growth/Value ratio falling = potential rotation to defensives")
        summary_parts.append("- VIX <15 with indices at highs = complacency (watch for reversal)")
        summary_parts.append("")

        return "\n".join(summary_parts)

    except Exception as e:
        print(f"Error generating equity market summary: {e}")
        return "Equity market data summary unavailable."


def generate_rates_market_summary():
    """Generate a rates-focused market data summary for the Rates AI briefing."""
    try:
        eastern = pytz.timezone('US/Eastern')
        summary_parts = []
        summary_parts.append("# RATES & YIELD CURVE DATA SUMMARY")
        summary_parts.append(f"Generated: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')} ET")
        summary_parts.append("")

        # Load rates-relevant metrics
        treasury_10y_df = load_csv_data('treasury_10y.csv')
        yield_curve_10y2y_df = load_csv_data('yield_curve_10y2y.csv')
        yield_curve_10y3m_df = load_csv_data('yield_curve_10y3m.csv')
        real_yield_df = load_csv_data('real_yield_10y.csv')
        breakeven_df = load_csv_data('breakeven_inflation_10y.csv')
        cpi_df = load_csv_data('cpi_yoy.csv')
        fed_funds_df = load_csv_data('fed_funds_rate.csv')
        sp500_df = load_csv_data('sp500_price.csv')
        gold_df = load_csv_data('gold_price.csv')
        germany_10y_df = load_csv_data('germany_10y_yield.csv')
        us_germany_spread_df = load_csv_data('us_germany_10y_spread.csv')
        us_japan_spread_df = load_csv_data('us_japan_10y_spread.csv')
        boj_df = load_csv_data('boj_total_assets.csv')
        ecb_df = load_csv_data('ecb_total_assets.csv')
        breakeven_5y_df = load_csv_data('breakeven_inflation_5y.csv')
        median_cpi_df = load_csv_data('median_cpi.csv')
        inflation_5y5y_df = load_csv_data('inflation_expectations_5y5y.csv')
        michigan_inflation_df = load_csv_data('michigan_inflation_expectations.csv')
        fed_funds_upper_df = load_csv_data('fed_funds_upper_target.csv')
        real_yield_proxy_df = load_csv_data('real_yield_proxy.csv')
        tips_df = load_csv_data('tips_inflation_price.csv')
        treasury_20y_df = load_csv_data('treasury_20yr_price.csv')
        treasury_710y_df = load_csv_data('treasury_7_10yr_price.csv')
        treasury_short_df = load_csv_data('treasury_short_price.csv')

        # Treasury Yields Section
        summary_parts.append("## TREASURY YIELDS")
        if treasury_10y_df is not None:
            stats = get_metric_stats(treasury_10y_df)
            if stats:
                if stats['current'] > 4.5:
                    level = "ELEVATED (restrictive)"
                elif stats['current'] > 3.5:
                    level = "Normal"
                else:
                    level = "LOW (accommodative)"
                summary_parts.append(f"10-Year Treasury: {stats['current']:.2f}% [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bps | 5-day: {stats['change_5d']*100:+.0f} bps | 30-day: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append(f"  52-week range: {stats['low_52w']:.2f}% - {stats['high_52w']:.2f}%")
                summary_parts.append("  KEY LEVELS: 4.0% (psychological), 5.0% (restrictive)")
        summary_parts.append("")

        # Yield Curve Section
        summary_parts.append("## YIELD CURVE (Recession Indicator)")
        if yield_curve_10y2y_df is not None:
            stats = get_metric_stats(yield_curve_10y2y_df)
            if stats:
                spread_bps = stats['current'] * 100
                if stats['current'] < 0:
                    status = "INVERTED (recession warning)"
                elif stats['current'] < 0.25:
                    status = "Flat"
                else:
                    status = "Normal (positive slope)"
                summary_parts.append(f"10Y-2Y Spread: {spread_bps:.0f} bps [{status}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bps | 5-day: {stats['change_5d']*100:+.0f} bps | 30-day: {stats['change_30d']*100:+.0f} bps")

        if yield_curve_10y3m_df is not None:
            stats = get_metric_stats(yield_curve_10y3m_df)
            if stats:
                spread_bps = stats['current'] * 100
                if stats['current'] < 0:
                    status = "INVERTED"
                elif stats['current'] < 0.25:
                    status = "Flat"
                else:
                    status = "Normal"
                summary_parts.append(f"10Y-3M Spread (Fed's preferred): {spread_bps:.0f} bps [{status}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bps | 5-day: {stats['change_5d']*100:+.0f} bps | 30-day: {stats['change_30d']*100:+.0f} bps")
        summary_parts.append("  CONTEXT: Inversion has preceded every US recession since 1970 (6-18 month lead)")
        summary_parts.append("")

        # Real Yields Section
        summary_parts.append("## REAL YIELDS & INFLATION")
        if real_yield_df is not None:
            stats = get_metric_stats(real_yield_df)
            if stats:
                if stats['current'] > 2:
                    policy = "RESTRICTIVE"
                elif stats['current'] > 0.5:
                    policy = "Tight"
                elif stats['current'] > 0:
                    policy = "Neutral"
                else:
                    policy = "ACCOMMODATIVE"
                summary_parts.append(f"10Y Real Yield (TIPS): {stats['current']:.2f}% [{policy}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bps | 5-day: {stats['change_5d']*100:+.0f} bps | 30-day: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Real yield = nominal yield minus inflation expectations)")

        if breakeven_df is not None:
            stats = get_metric_stats(breakeven_df)
            if stats:
                if stats['current'] > 2.5:
                    expectation = "Above target (inflation concerns)"
                elif stats['current'] >= 2.0:
                    expectation = "At Fed target"
                else:
                    expectation = "Below target"
                summary_parts.append(f"10Y Breakeven Inflation: {stats['current']:.2f}% [{expectation}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bps | 5-day: {stats['change_5d']*100:+.0f} bps | 30-day: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Market's 10-year inflation forecast)")
        if breakeven_5y_df is not None:
            stats = get_metric_stats(breakeven_5y_df)
            if stats:
                summary_parts.append(f"5Y Breakeven Inflation: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Shorter-term inflation expectations)")
        if real_yield_proxy_df is not None:
            stats = get_metric_stats(real_yield_proxy_df)
            if stats:
                summary_parts.append(f"Real Yield Proxy (TIP ETF): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if tips_df is not None:
            stats = get_metric_stats(tips_df)
            if stats:
                summary_parts.append(f"TIPS ETF (STIP): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")

        if cpi_df is not None:
            stats = get_metric_stats(cpi_df)
            if stats:
                summary_parts.append(f"CPI (YoY): {stats['current']:.1f}%")
        if median_cpi_df is not None:
            stats = get_metric_stats(median_cpi_df)
            if stats:
                summary_parts.append(f"Cleveland Fed Median CPI: {stats['current']:.1f}% ({stats['percentile']:.1f}th %ile) | 30d: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Noise-filtered CPI trend — strips extreme price changes)")
        if inflation_5y5y_df is not None:
            stats = get_metric_stats(inflation_5y5y_df)
            if stats:
                summary_parts.append(f"5Y5Y Forward Inflation Expectations: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Fed's preferred long-run expectations anchor — strips near-term noise)")
        if michigan_inflation_df is not None:
            stats = get_metric_stats(michigan_inflation_df)
            if stats:
                summary_parts.append(f"Michigan 1-Year Inflation Expectations: {stats['current']:.1f}% ({stats['percentile']:.1f}th %ile)")
                summary_parts.append("  (Consumer expectations — divergence from market breakevens signals unanchoring risk)")
        summary_parts.append("")

        # Fed Policy Section
        summary_parts.append("## FED POLICY")
        if fed_funds_df is not None:
            stats = get_metric_stats(fed_funds_df)
            if stats:
                summary_parts.append(f"Fed Funds Rate: {stats['current']:.2f}%")
                if treasury_10y_df is not None:
                    t10y_stats = get_metric_stats(treasury_10y_df)
                    if t10y_stats:
                        term_premium = t10y_stats['current'] - stats['current']
                        summary_parts.append(f"  Term Premium (10Y - Fed Funds): {term_premium:.2f}%")
        if fed_funds_upper_df is not None:
            stats = get_metric_stats(fed_funds_upper_df)
            if stats:
                summary_parts.append(f"Fed Funds Upper Target: {stats['current']:.2f}%")
        summary_parts.append("")

        # Treasury Bond ETFs (Duration Spectrum)
        summary_parts.append("## TREASURY BOND ETFs")
        if treasury_short_df is not None:
            stats = get_metric_stats(treasury_short_df)
            if stats:
                summary_parts.append(f"Short-Term Treasury (SHV): ${stats['current']:.2f} | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if treasury_710y_df is not None:
            stats = get_metric_stats(treasury_710y_df)
            if stats:
                summary_parts.append(f"7-10Y Treasury (IEF): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if treasury_20y_df is not None:
            stats = get_metric_stats(treasury_20y_df)
            if stats:
                summary_parts.append(f"20+ Year Treasury (TLT): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        summary_parts.append("  (Long-duration bonds most sensitive to rate changes)")
        summary_parts.append("")

        # Cross-Asset Context
        summary_parts.append("## RATES IMPACT ON OTHER ASSETS")
        if sp500_df is not None and treasury_10y_df is not None:
            sp_stats = get_metric_stats(sp500_df)
            t10y_stats = get_metric_stats(treasury_10y_df)
            if sp_stats and t10y_stats:
                # Equity risk premium approximation (rough)
                earnings_yield = 100 / 20  # Assume ~20x P/E
                erp = earnings_yield - t10y_stats['current']
                summary_parts.append(f"S&P 500 Equity Risk Premium (est): {erp:.1f}%")
                summary_parts.append("  (Higher rates compress equity valuations)")

        if gold_df is not None and real_yield_df is not None:
            gold_stats = get_metric_stats(gold_df)
            ry_stats = get_metric_stats(real_yield_df)
            if gold_stats and ry_stats:
                summary_parts.append(f"Gold vs Real Yields: Gold {gold_stats['pct_change_30d']:+.1f}% (30d) | Real yield {ry_stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Gold typically inversely correlated with real yields)")
        summary_parts.append("")

        # Global Rates & Central Banks
        summary_parts.append("## GLOBAL RATES & CENTRAL BANKS")
        if germany_10y_df is not None:
            stats = get_metric_stats(germany_10y_df)
            if stats:
                summary_parts.append(f"Germany 10Y Bund: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 5d: {stats['change_5d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
        if us_germany_spread_df is not None:
            stats = get_metric_stats(us_germany_spread_df)
            if stats:
                summary_parts.append(f"US-Germany 10Y Spread: {stats['current']*100:.0f} bps ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Wider = USD yield advantage, supports dollar)")
        if us_japan_spread_df is not None:
            stats = get_metric_stats(us_japan_spread_df)
            if stats:
                summary_parts.append(f"US-Japan 10Y Spread: {stats['current']*100:.0f} bps ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']*100:+.0f} bps | 30d: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Key carry trade driver)")
        if boj_df is not None:
            stats = get_metric_stats(boj_df)
            updated = get_last_updated_date(boj_df)
            if stats:
                trend = "EXPANDING" if stats['change_30d'] > 0 else "SHRINKING"
                summary_parts.append(f"BOJ Total Assets: {stats['current']/1e6:.1f}T yen [{trend}] | Last updated: {updated}")
        if ecb_df is not None:
            stats = get_metric_stats(ecb_df)
            updated = get_last_updated_date(ecb_df)
            if stats:
                trend = "EXPANDING" if stats['change_30d'] > 0 else "SHRINKING"
                summary_parts.append(f"ECB Total Assets: {stats['current']/1e6:.1f}T EUR [{trend}] | Last updated: {updated}")
        summary_parts.append("")

        # Credit Spreads Section
        summary_parts.append("## CREDIT SPREADS (Risk Appetite)")
        hy_spread_df = load_csv_data('hy_spread.csv')
        ig_spread_df = load_csv_data('ig_spread.csv')
        ccc_spread_df = load_csv_data('ccc_spread.csv')

        if hy_spread_df is not None:
            stats = get_metric_stats(hy_spread_df)
            if stats:
                if stats['current'] > 500:
                    status = "STRESS (significant risk aversion)"
                elif stats['current'] > 350:
                    status = "Elevated"
                elif stats['current'] > 300:
                    status = "Normal"
                else:
                    status = "TIGHT (complacency)"
                summary_parts.append(f"High-Yield Spread: {stats['current']:.0f} bp [{status}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']:+.0f} bp | 5-day: {stats['change_5d']:+.0f} bp | 30-day: {stats['change_30d']:+.0f} bp")
                summary_parts.append("  KEY LEVELS: 300bp (first warning), 500bp (stress), 800bp (crisis)")

        if ig_spread_df is not None:
            stats = get_metric_stats(ig_spread_df)
            if stats:
                if stats['current'] > 150:
                    status = "STRESS"
                elif stats['current'] > 120:
                    status = "Elevated"
                elif stats['current'] > 100:
                    status = "Normal"
                else:
                    status = "Tight"
                summary_parts.append(f"Investment Grade Spread: {stats['current']:.0f} bp [{status}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']:+.0f} bp | 5-day: {stats['change_5d']:+.0f} bp | 30-day: {stats['change_30d']:+.0f} bp")

        if ccc_spread_df is not None and hy_spread_df is not None:
            ccc_stats = get_metric_stats(ccc_spread_df)
            hy_stats = get_metric_stats(hy_spread_df)
            if ccc_stats and hy_stats and hy_stats['current'] > 0:
                ccc_hy_ratio = ccc_stats['current'] / hy_stats['current']
                if ccc_hy_ratio > 3.5:
                    ratio_status = "DISTRESSED (quality bifurcation)"
                elif ccc_hy_ratio > 3.0:
                    ratio_status = "Elevated"
                else:
                    ratio_status = "Normal"
                summary_parts.append(f"CCC/HY Ratio: {ccc_hy_ratio:.2f}x [{ratio_status}]")
                summary_parts.append("  (Higher ratio = market discriminating against lowest quality)")
        summary_parts.append("  CONTEXT: Tight spreads = complacency, historically precedes sharp widening")
        summary_parts.append("")

        # Key Insights
        summary_parts.append("## KEY RELATIONSHIPS TO MONITOR")
        summary_parts.append("- Inverted curve + steepening = recession typically imminent")
        summary_parts.append("- Rising real yields = headwind for growth stocks and gold")
        summary_parts.append("- Breakevens > 2.5% = inflation expectations unanchored")
        summary_parts.append("- 10Y above 5% = significant equity valuation pressure")
        summary_parts.append("- Credit spreads widening while equities rise = divergence warning")
        summary_parts.append("- HY spreads at extremes (<300bp or >500bp) often precede regime change")
        summary_parts.append("")

        return "\n".join(summary_parts)

    except Exception as e:
        print(f"Error generating rates market summary: {e}")
        return "Rates market data summary unavailable."


def generate_dollar_market_summary():
    """Generate a dollar-focused market data summary for the Dollar AI briefing."""
    try:
        eastern = pytz.timezone('US/Eastern')
        summary_parts = []
        summary_parts.append("# DOLLAR & CURRENCY DATA SUMMARY")
        summary_parts.append(f"Generated: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')} ET")
        summary_parts.append("")

        # Load dollar-relevant metrics
        dxy_df = load_csv_data('dollar_index_price.csv')
        usdjpy_df = load_csv_data('usdjpy_price.csv')
        treasury_10y_df = load_csv_data('treasury_10y.csv')
        fed_funds_df = load_csv_data('fed_funds_rate.csv')
        gold_df = load_csv_data('gold_price.csv')
        sp500_df = load_csv_data('sp500_price.csv')
        bitcoin_df = load_csv_data('bitcoin_price.csv')
        vix_df = load_csv_data('vix_price.csv')
        eurusd_df = load_csv_data('eurusd_price.csv')
        fx_eur_usd_df = load_csv_data('fx_eur_usd.csv')
        fx_jpy_usd_df = load_csv_data('fx_jpy_usd.csv')
        boj_df = load_csv_data('boj_total_assets.csv')
        ecb_df = load_csv_data('ecb_total_assets.csv')

        # Dollar Index Section
        summary_parts.append("## US DOLLAR INDEX (DXY)")
        if dxy_df is not None:
            stats = get_metric_stats(dxy_df)
            if stats:
                # Dollar Smile framework interpretation
                if stats['current'] > 105:
                    level = "STRONG (risk-off or yield advantage)"
                elif stats['current'] > 100:
                    level = "Firm"
                elif stats['current'] > 95:
                    level = "Neutral"
                else:
                    level = "WEAK (risk-on or policy concerns)"
                summary_parts.append(f"DXY: {stats['current']:.2f} [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.2f}% | 5-day: {stats['pct_change_5d']:+.2f}% | 30-day: {stats['pct_change_30d']:+.2f}%")
                summary_parts.append(f"  52-week range: {stats['low_52w']:.2f} - {stats['high_52w']:.2f}")
                summary_parts.append("  KEY LEVELS: 100 (psychological), 105 (strong dollar), 95 (weak dollar)")
        summary_parts.append("")

        # USD/JPY Section (Carry Trade Indicator)
        summary_parts.append("## USD/JPY (Carry Trade Barometer)")
        if usdjpy_df is not None:
            stats = get_metric_stats(usdjpy_df)
            if stats:
                # Carry trade dynamics
                if stats['current'] > 150:
                    status = "EXTENDED (carry trade stress risk)"
                elif stats['current'] > 145:
                    status = "Elevated (BOJ intervention zone)"
                elif stats['current'] > 140:
                    status = "Normal carry trade range"
                else:
                    status = "Yen strength (carry unwind risk)"
                summary_parts.append(f"USD/JPY: {stats['current']:.2f} [{status}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['pct_change_1d']:+.2f}% | 5-day: {stats['pct_change_5d']:+.2f}% | 30-day: {stats['pct_change_30d']:+.2f}%")
                summary_parts.append(f"  52-week range: {stats['low_52w']:.2f} - {stats['high_52w']:.2f}")
                summary_parts.append("  CONTEXT: Japan's ultra-low rates make JPY funding currency for global carry trades")
                summary_parts.append("  KEY LEVELS: 150 (BOJ red line), 145 (intervention risk), 140 (support)")
        summary_parts.append("")

        # Major Currency Pairs
        summary_parts.append("## MAJOR CURRENCY PAIRS")
        if eurusd_df is not None:
            stats = get_metric_stats(eurusd_df)
            if stats:
                summary_parts.append(f"EUR/USD (ETF): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 5d: {stats['pct_change_5d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if fx_eur_usd_df is not None:
            stats = get_metric_stats(fx_eur_usd_df)
            if stats:
                summary_parts.append(f"EUR/USD (spot): {stats['current']:.4f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if fx_jpy_usd_df is not None:
            stats = get_metric_stats(fx_jpy_usd_df)
            if stats:
                summary_parts.append(f"JPY/USD (spot): {stats['current']:.4f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        summary_parts.append("")

        # Central Bank Balance Sheets
        summary_parts.append("## CENTRAL BANK BALANCE SHEETS")
        if boj_df is not None:
            stats = get_metric_stats(boj_df)
            updated = get_last_updated_date(boj_df)
            if stats:
                trend = "EXPANDING" if stats['change_30d'] > 0 else "SHRINKING"
                summary_parts.append(f"BOJ Total Assets: {stats['current']/1e6:.1f}T yen [{trend}] | 30d: {stats['pct_change_30d']:+.2f}% | Last updated: {updated}")
        if ecb_df is not None:
            stats = get_metric_stats(ecb_df)
            updated = get_last_updated_date(ecb_df)
            if stats:
                trend = "EXPANDING" if stats['change_30d'] > 0 else "SHRINKING"
                summary_parts.append(f"ECB Total Assets: {stats['current']/1e6:.1f}T EUR [{trend}] | 30d: {stats['pct_change_30d']:+.2f}% | Last updated: {updated}")
        summary_parts.append("  CONTEXT: CB balance sheet divergence drives relative currency values")
        summary_parts.append("")

        # Dollar Smile Framework
        summary_parts.append("## DOLLAR SMILE FRAMEWORK")
        summary_parts.append("The 'Dollar Smile' describes three regimes where USD strengthens:")
        summary_parts.append("  LEFT SIDE: Risk-off panic (flight to safety → USD bid)")
        summary_parts.append("  MIDDLE: Weak dollar (calm markets, relative growth elsewhere)")
        summary_parts.append("  RIGHT SIDE: US growth/yield advantage (risk-on but US outperforms)")
        summary_parts.append("")

        # Rate Differentials
        summary_parts.append("## RATE DIFFERENTIALS (Dollar Driver)")
        if treasury_10y_df is not None:
            stats = get_metric_stats(treasury_10y_df)
            if stats:
                summary_parts.append(f"US 10Y Treasury: {stats['current']:.2f}%")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bps | 5-day: {stats['change_5d']*100:+.0f} bps | 30-day: {stats['change_30d']*100:+.0f} bps")
                summary_parts.append("  (Higher US rates typically support USD)")

        if fed_funds_df is not None:
            stats = get_metric_stats(fed_funds_df)
            if stats:
                summary_parts.append(f"Fed Funds Rate: {stats['current']:.2f}%")
                summary_parts.append("  CONTEXT: Rate differential vs ECB/BOJ drives FX flows")
        summary_parts.append("")

        # Cross-Asset Context
        summary_parts.append("## DOLLAR IMPACT ON OTHER ASSETS")
        if gold_df is not None and dxy_df is not None:
            gold_stats = get_metric_stats(gold_df)
            dxy_stats = get_metric_stats(dxy_df)
            if gold_stats and dxy_stats:
                summary_parts.append(f"Gold: ${gold_stats['current']:.2f} ({gold_stats['pct_change_30d']:+.1f}% 30d) vs DXY {dxy_stats['pct_change_30d']:+.1f}%")
                summary_parts.append("  (Gold typically inversely correlated with USD)")

        if sp500_df is not None:
            stats = get_metric_stats(sp500_df)
            if stats:
                summary_parts.append(f"S&P 500: {stats['current']:.2f} ({stats['pct_change_30d']:+.1f}% 30d)")
                summary_parts.append("  (Strong dollar headwind for multinational earnings)")

        if bitcoin_df is not None:
            stats = get_metric_stats(bitcoin_df)
            if stats:
                summary_parts.append(f"Bitcoin: ${stats['current']:,.0f} ({stats['pct_change_30d']:+.1f}% 30d)")
                summary_parts.append("  (Crypto sensitive to dollar liquidity conditions)")
        summary_parts.append("")

        # Risk Sentiment
        summary_parts.append("## RISK SENTIMENT CONTEXT")
        if vix_df is not None:
            stats = get_metric_stats(vix_df)
            if stats:
                if stats['current'] > 25:
                    regime = "RISK-OFF (supports USD safe-haven bid)"
                elif stats['current'] > 18:
                    regime = "Elevated uncertainty"
                else:
                    regime = "Risk-on (may weaken USD)"
                summary_parts.append(f"VIX: {stats['current']:.2f} [{regime}]")
        summary_parts.append("")

        # Key Insights
        summary_parts.append("## KEY RELATIONSHIPS TO MONITOR")
        summary_parts.append("- DXY > 105 with VIX > 25 = risk-off dollar strength (left side of smile)")
        summary_parts.append("- DXY > 105 with VIX < 18 = yield/growth advantage (right side of smile)")
        summary_parts.append("- USD/JPY > 150 = BOJ intervention risk, carry trade stress")
        summary_parts.append("- DXY falling while VIX low = middle of smile (dollar weakness)")
        summary_parts.append("- Sharp yen strength = carry trade unwind, risk-off signal")
        summary_parts.append("- Dollar strength headwind for EM assets, commodities, multinational earnings")
        summary_parts.append("")

        return "\n".join(summary_parts)

    except Exception as e:
        print(f"Error generating dollar market summary: {e}")
        return "Dollar market data summary unavailable."


def generate_credit_market_summary():
    """Generate a credit-focused market data summary for the Credit AI briefing."""
    try:
        eastern = pytz.timezone('US/Eastern')
        summary_parts = []
        summary_parts.append("# CREDIT MARKETS DATA SUMMARY")
        summary_parts.append(f"Generated: {datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')} ET")
        summary_parts.append("")

        # Load credit-relevant metrics
        hy_df = load_csv_data('high_yield_spread.csv')
        ig_df = load_csv_data('investment_grade_spread.csv')
        ccc_df = load_csv_data('ccc_spread.csv')
        hy_price_df = load_csv_data('high_yield_credit_price.csv')
        ig_price_df = load_csv_data('investment_grade_credit_price.csv')
        vix_df = load_csv_data('vix_price.csv')
        treasury_10y_df = load_csv_data('treasury_10y.csv')
        sp500_df = load_csv_data('sp500_price.csv')
        hyg_treasury_df = load_csv_data('hyg_treasury_spread.csv')
        lqd_treasury_df = load_csv_data('lqd_treasury_spread.csv')
        leveraged_loan_df = load_csv_data('leveraged_loan_price.csv')

        # HY OAS Section
        summary_parts.append("## HIGH YIELD SPREADS (OAS)")
        if hy_df is not None:
            stats = get_metric_stats(hy_df)
            if stats:
                hy_bps = stats['current'] * 100
                if hy_bps < 300:
                    level = "TIGHT (complacent, low default pricing)"
                elif hy_bps < 450:
                    level = "Normal"
                elif hy_bps < 600:
                    level = "Wide (elevated stress)"
                else:
                    level = "VERY WIDE (crisis/default pricing)"
                summary_parts.append(f"HY OAS: {hy_bps:.0f} bp [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bp | 5-day: {stats['change_5d']*100:+.0f} bp | 30-day: {stats['change_30d']*100:+.0f} bp")
                summary_parts.append(f"  52-week range: {stats['low_52w']*100:.0f} - {stats['high_52w']*100:.0f} bp")
                summary_parts.append("  KEY LEVELS: 300 bp (tight/complacent), 450 bp (normal), 600 bp (stress), 900+ bp (crisis)")
        summary_parts.append("")

        # IG OAS Section
        summary_parts.append("## INVESTMENT GRADE SPREADS (OAS)")
        if ig_df is not None:
            stats = get_metric_stats(ig_df)
            if stats:
                ig_bps = stats['current'] * 100
                if ig_bps < 80:
                    level = "TIGHT"
                elif ig_bps < 150:
                    level = "Normal"
                else:
                    level = "Wide (stress)"
                summary_parts.append(f"IG OAS: {ig_bps:.0f} bp [{level}] ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bp | 5-day: {stats['change_5d']*100:+.0f} bp | 30-day: {stats['change_30d']*100:+.0f} bp")
                summary_parts.append(f"  52-week range: {stats['low_52w']*100:.0f} - {stats['high_52w']*100:.0f} bp")
                summary_parts.append("  KEY LEVELS: 80 bp (tight), 150 bp (normal upper), 200+ bp (stress)")
        summary_parts.append("")

        # CCC Section
        summary_parts.append("## CCC-RATED SPREADS (Distressed Credit)")
        if ccc_df is not None:
            stats = get_metric_stats(ccc_df)
            if stats:
                ccc_bps = stats['current'] * 100
                summary_parts.append(f"CCC OAS: {ccc_bps:.0f} bp ({stats['percentile']:.1f}th %ile)")
                summary_parts.append(f"  1-day: {stats['change_1d']*100:+.0f} bp | 5-day: {stats['change_5d']*100:+.0f} bp | 30-day: {stats['change_30d']*100:+.0f} bp")
                # CCC/HY ratio for distress concentration
                if hy_df is not None:
                    hy_stats = get_metric_stats(hy_df)
                    if hy_stats and hy_stats['current'] > 0:
                        ratio = stats['current'] / hy_stats['current']
                        summary_parts.append(f"  CCC/HY ratio: {ratio:.2f}x (normal 2.5-4x) - {'rising distress' if ratio > 4 else 'contained' if ratio < 3 else 'normal'}")
                summary_parts.append("  CONTEXT: CCC spreads lead HY in defaults; rising CCC/HY ratio = distressed credits under pressure first")
        summary_parts.append("")

        # Credit ETF Prices (Total Return Signal)
        summary_parts.append("## CREDIT ETF PRICES (Total Return Signal)")
        if hy_price_df is not None:
            stats = get_metric_stats(hy_price_df)
            if stats:
                summary_parts.append(f"HYG (HY ETF): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 5d: {stats['pct_change_5d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if ig_price_df is not None:
            stats = get_metric_stats(ig_price_df)
            if stats:
                summary_parts.append(f"LQD (IG ETF): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 5d: {stats['pct_change_5d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        if leveraged_loan_df is not None:
            stats = get_metric_stats(leveraged_loan_df)
            if stats:
                summary_parts.append(f"Leveraged Loans (BKLN): ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['pct_change_1d']:+.2f}% | 5d: {stats['pct_change_5d']:+.2f}% | 30d: {stats['pct_change_30d']:+.2f}%")
        summary_parts.append("  (Falling prices = spread widening and/or rising Treasury yields)")
        summary_parts.append("")

        # Treasury Spread Analysis
        summary_parts.append("## CREDIT vs TREASURY SPREADS")
        if hyg_treasury_df is not None:
            stats = get_metric_stats(hyg_treasury_df)
            if stats:
                summary_parts.append(f"HYG-Treasury Spread: {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']:+.3f} | 5d: {stats['change_5d']:+.3f} | 30d: {stats['change_30d']:+.3f}")
        if lqd_treasury_df is not None:
            stats = get_metric_stats(lqd_treasury_df)
            if stats:
                summary_parts.append(f"LQD-Treasury Spread: {stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 1d: {stats['change_1d']:+.3f} | 5d: {stats['change_5d']:+.3f} | 30d: {stats['change_30d']:+.3f}")
        summary_parts.append("  (ETF-based spread proxies — widening = credit deterioration)")
        summary_parts.append("")

        # Cross-Asset Context
        summary_parts.append("## CROSS-ASSET CREDIT CONTEXT")
        if vix_df is not None:
            stats = get_metric_stats(vix_df)
            if stats:
                summary_parts.append(f"VIX: {stats['current']:.1f} ({stats['percentile']:.1f}th %ile) - {'Risk-off' if stats['current'] > 25 else 'Normal' if stats['current'] > 15 else 'Complacent'}")
        if treasury_10y_df is not None:
            stats = get_metric_stats(treasury_10y_df)
            if stats:
                summary_parts.append(f"10Y Treasury: {stats['current']:.2f}% ({stats['percentile']:.1f}th %ile) - affects credit all-in yields")
        if sp500_df is not None:
            stats = get_metric_stats(sp500_df)
            if stats:
                summary_parts.append(f"S&P 500: ${stats['current']:.2f} ({stats['percentile']:.1f}th %ile) | 30d: {stats['pct_change_30d']:+.2f}%")
        summary_parts.append("  CONTEXT: Tight credit + rising equities = risk-on; Credit widening ahead of equity decline = early warning signal")
        summary_parts.append("")


        # Interpretation Framework
        summary_parts.append("## CREDIT SPREAD INTERPRETATION FRAMEWORK")
        summary_parts.append("- HY < 300 bp: Markets pricing near-zero defaults; complacency risk")
        summary_parts.append("- HY 300-450 bp: Normal credit conditions; healthy risk appetite")
        summary_parts.append("- HY 450-600 bp: Elevated stress; de-risking underway")
        summary_parts.append("- HY > 600 bp: Crisis territory; distress/default cycle underway")
        summary_parts.append("- IG typically 60-70% of HY move; IG leading HY = early warning")
        summary_parts.append("- Credit leading equities: spread widening before equity decline = macro alarm")
        summary_parts.append("")

        return "\n".join(summary_parts)

    except Exception as e:
        print(f"Error generating credit market summary: {e}")
        return "Credit market data summary unavailable."



def _execute_tool(function_name, function_args):
    """Execute a tool function and return the result."""
    if function_name == "search_web":
        return execute_search_function(function_args)
    elif function_name in ["list_available_metrics", "get_metric_data"]:
        return execute_metric_function(function_name, function_args)
    else:
        return json.dumps({"error": f"Unknown function: {function_name}"})


def _filter_reasoning_artifacts(message):
    """Filter out any reasoning artifacts that might leak through."""
    import re
    lines = message.split('\n')
    filtered_lines = []
    for line in lines:
        if re.match(r'^(Need |Oops |Let me |I should |Thinking:|<think>|</think>)', line.strip()):
            print(f"[CHAT] Filtering out reasoning artifact: {line[:50]}")
            continue
        filtered_lines.append(line)
    result = '\n'.join(filtered_lines).strip()

    if not result:
        return "I apologize, but I wasn't able to generate a response. Please try asking your question again."
    return result


# Admin endpoints
from decorators import admin_required


@app.route('/admin/analytics')
@admin_required
def admin_analytics():
    """Admin usage analytics dashboard."""
    from services.admin_analytics import (
        get_today_summary, get_daily_trend, get_top_users, get_anon_cap_status,
    )

    return render_template(
        'admin/analytics.html',
        today=get_today_summary(),
        trend=get_daily_trend(30),
        top_users=get_top_users(10),
        anon_cap=get_anon_cap_status(),
    )


@app.route('/admin/trigger-alert-check')
@admin_required
def trigger_alert_check():
    """Manually trigger alert check (admin only)"""
    from jobs.alert_jobs import check_alert_thresholds
    check_alert_thresholds()

    return jsonify({'status': 'Alert check triggered successfully'})


@app.route('/admin/trigger-daily-briefing')
@admin_required
def trigger_daily_briefing():
    """Manually trigger daily briefing (admin only)"""
    from jobs.email_jobs import send_daily_briefings
    send_daily_briefings()

    return jsonify({'status': 'Daily briefing triggered successfully'})


# ---------------------------------------------------------------------------
# Stripe Webhook Endpoint
# ---------------------------------------------------------------------------

@csrf.exempt
@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """Receive and process Stripe webhook events.

    Verifies the event signature before dispatching to handlers.
    Returns 200 for all valid requests (even unrecognised event types)
    so Stripe does not retry.
    """
    import stripe as _stripe
    from billing.webhooks import handle_webhook_event

    if not is_stripe_configured():
        return jsonify({'error': 'Billing not configured'}), 503

    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = get_webhook_secret()

    if not sig_header:
        app.logger.warning("Stripe webhook: missing Stripe-Signature header")
        return jsonify({'error': 'Missing signature'}), 400

    if not webhook_secret:
        app.logger.error("Stripe webhook: STRIPE_WEBHOOK_SECRET not configured")
        return jsonify({'error': 'Webhook not configured'}), 503

    try:
        event = _stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError:
        app.logger.warning("Stripe webhook: invalid payload")
        return jsonify({'error': 'Invalid payload'}), 400
    except _stripe.error.SignatureVerificationError:
        app.logger.warning("Stripe webhook: signature verification failed")
        return jsonify({'error': 'Invalid signature'}), 400

    try:
        result = handle_webhook_event(event)
        return jsonify(result), 200
    except Exception:
        app.logger.exception("Stripe webhook handler error for event %s",
                             event.get('id'))
        return jsonify({'error': 'Internal error'}), 500


# Initialize scheduler at module load time (works with both direct run and gunicorn --preload)
# This runs when the module is imported, ensuring the scheduler starts regardless of how the app is launched
with app.app_context():
    db.create_all()
init_scheduler()


if __name__ == '__main__':
    # Run Flask app in development mode
    # Note: For production, use Gunicorn with --preload flag:
    # gunicorn -w 1 --preload dashboard:app
    app.run(debug=True, host='0.0.0.0', port=5000)
