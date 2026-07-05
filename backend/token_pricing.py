"""
Token Pricing Module
Calculates API costs based on Doubao model token pricing tiers.
"""

from __future__ import annotations

# Pricing per million tokens (yuan) by token range
# Format: (max_tokens, price_per_million)
PRICING = {
    "doubao-seed-2-0-mini-260428": {
        "input":  [(32000, 0.2), (128000, 0.4), (256000, 0.8)],
        "output": [(32000, 2.0), (128000, 4.0), (256000, 8.0)],
        "cached_input": [(32000, 0.04), (128000, 0.08), (256000, 0.16)],
    },
    "doubao-seed-2-0-pro-260215": {
        "input":  [(32000, 0.8), (128000, 1.6), (256000, 3.2)],
        "output": [(32000, 8.0), (128000, 16.0), (256000, 32.0)],
        "cached_input": [(32000, 0.16), (128000, 0.32), (256000, 0.64)],
    },
}

# Multiplier applied to base token prices for final user-facing cost
_PRICE_FACTOR = 2.0


def _get_tier_price(tokens: int, tiers: list[tuple[int, float]]) -> float:
    """Get the price per million tokens for a given token count."""
    for max_tokens, price in tiers:
        if tokens <= max_tokens:
            return price
    return tiers[-1][1]


def calculate_cost(model: str, input_tokens: int, output_tokens: int,
                   cached_input_tokens: int = 0) -> dict:
    """Calculate API cost (yuan) for a single grading call."""
    p = PRICING.get(model)
    if not p:
        # Fallback to mini pricing
        p = PRICING["doubao-seed-2-0-mini-260428"]

    input_price = _get_tier_price(input_tokens, p["input"])
    output_price = _get_tier_price(output_tokens, p["output"])

    input_cost = (input_tokens / 1_000_000) * input_price
    output_cost = (output_tokens / 1_000_000) * output_price
    total_cost = input_cost + output_cost

    if cached_input_tokens > 0:
        cached_price = _get_tier_price(cached_input_tokens, p["cached_input"])
        cached_cost = (cached_input_tokens / 1_000_000) * cached_price
        total_cost += cached_cost
    else:
        cached_cost = 0.0

    return {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cost_yuan": round(total_cost * _PRICE_FACTOR, 6),
    }


def cost_for_display(cost_yuan: float) -> float:
    """Calculate user-facing price."""
    return round(cost_yuan * _PRICE_FACTOR, 6)
