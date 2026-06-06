"""
cost_tracker.py
---------------
Thread-safe per-stage token accumulator and USD cost calculator.

Pricing constants are sourced from Anthropic's published rates (June 2026).
Override at runtime via env vars if rates change:
  HAIKU_PRICE_IN, HAIKU_PRICE_OUT
  SONNET_PRICE_IN, SONNET_PRICE_OUT
  OPUS_PRICE_IN,   OPUS_PRICE_OUT
All values are USD per million tokens (MTok).
"""

import os
import threading
from dataclasses import dataclass, field


# ── Pricing table (USD per million tokens) ───────────────────────────────────

def _price(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except ValueError:
        return default

HAIKU_PRICE_IN   = _price("HAIKU_PRICE_IN",   0.80)
HAIKU_PRICE_OUT  = _price("HAIKU_PRICE_OUT",   4.00)
SONNET_PRICE_IN  = _price("SONNET_PRICE_IN",   3.00)
SONNET_PRICE_OUT = _price("SONNET_PRICE_OUT", 15.00)
OPUS_PRICE_IN    = _price("OPUS_PRICE_IN",    15.00)
OPUS_PRICE_OUT   = _price("OPUS_PRICE_OUT",   75.00)

# Lookup by model name prefix
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku":  (HAIKU_PRICE_IN,  HAIKU_PRICE_OUT),
    "claude-sonnet": (SONNET_PRICE_IN, SONNET_PRICE_OUT),
    "claude-opus":   (OPUS_PRICE_IN,   OPUS_PRICE_OUT),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return USD cost for a single API call given model name and token counts."""
    price_in, price_out = HAIKU_PRICE_IN, HAIKU_PRICE_OUT  # safe default
    model_lower = model.lower()
    for prefix, prices in _MODEL_PRICES.items():
        if model_lower.startswith(prefix):
            price_in, price_out = prices
            break
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


# ── Per-audit accumulator ─────────────────────────────────────────────────────

@dataclass
class CostAccumulator:
    """
    Thread-safe accumulator for token counts and USD costs across pipeline stages.

    Usage:
        acc = CostAccumulator()
        acc.add_haiku(input_tokens=1200, output_tokens=80)   # from triage batch
        acc.add_sonnet(input_tokens=4500, output_tokens=890) # from analyse_clause
        acc.add_opus(input_tokens=3100, output_tokens=720)   # from analyse_clause
        costs = acc.to_dict()  # serialise for Supabase
    """
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    haiku_input_tokens:  int   = 0
    haiku_output_tokens: int   = 0
    sonnet_input_tokens: int   = 0
    sonnet_output_tokens: int  = 0
    opus_input_tokens:   int   = 0
    opus_output_tokens:  int   = 0

    def add_haiku(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.haiku_input_tokens  += input_tokens
            self.haiku_output_tokens += output_tokens

    def add_sonnet(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.sonnet_input_tokens  += input_tokens
            self.sonnet_output_tokens += output_tokens

    def add_opus(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.opus_input_tokens  += input_tokens
            self.opus_output_tokens += output_tokens

    def add_by_model(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """Route to the correct bucket based on model name prefix."""
        m = model.lower()
        if "haiku" in m:
            self.add_haiku(input_tokens, output_tokens)
        elif "opus" in m:
            self.add_opus(input_tokens, output_tokens)
        else:
            self.add_sonnet(input_tokens, output_tokens)

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def haiku_cost_usd(self) -> float:
        return (
            self.haiku_input_tokens  * HAIKU_PRICE_IN +
            self.haiku_output_tokens * HAIKU_PRICE_OUT
        ) / 1_000_000

    @property
    def sonnet_cost_usd(self) -> float:
        return (
            self.sonnet_input_tokens  * SONNET_PRICE_IN +
            self.sonnet_output_tokens * SONNET_PRICE_OUT
        ) / 1_000_000

    @property
    def opus_cost_usd(self) -> float:
        return (
            self.opus_input_tokens  * OPUS_PRICE_IN +
            self.opus_output_tokens * OPUS_PRICE_OUT
        ) / 1_000_000

    @property
    def total_input_tokens(self) -> int:
        return self.haiku_input_tokens + self.sonnet_input_tokens + self.opus_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.haiku_output_tokens + self.sonnet_output_tokens + self.opus_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return self.haiku_cost_usd + self.sonnet_cost_usd + self.opus_cost_usd

    def to_dict(self) -> dict:
        """Serialise to JSONB-ready dict for Supabase stage_costs column."""
        return {
            "haiku_input_tokens":   self.haiku_input_tokens,
            "haiku_output_tokens":  self.haiku_output_tokens,
            "haiku_cost_usd":       round(self.haiku_cost_usd,  6),
            "sonnet_input_tokens":  self.sonnet_input_tokens,
            "sonnet_output_tokens": self.sonnet_output_tokens,
            "sonnet_cost_usd":      round(self.sonnet_cost_usd, 6),
            "opus_input_tokens":    self.opus_input_tokens,
            "opus_output_tokens":   self.opus_output_tokens,
            "opus_cost_usd":        round(self.opus_cost_usd,   6),
            "total_input_tokens":   self.total_input_tokens,
            "total_output_tokens":  self.total_output_tokens,
            "total_cost_usd":       round(self.total_cost_usd,  6),
        }
