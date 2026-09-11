"""
signals_advanced.py — Advanced signal engine.

Adds to basic Supertrend+RSI+EMA+VWAP:
  - PCR (Put-Call Ratio)    : market sentiment
  - OI Analysis             : support/resistance via open interest
  - Option Greeks           : Delta, Theta, IV filters
  - IV Rank                 : avoid overpriced premiums
  - Confluence scoring      : weighted multi-signal confidence
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

import config
from strategy import (
    compute_supertrend, compute_ema, compute_rsi, compute_vwap,
    Signal, OptionType, StrategyResult
)


# ── Option Chain / PCR Data ───────────────────────────────────

@dataclass
class OptionChainData:
    pcr:              float   # Put-Call Ratio (OI-based)
    atm_iv:           float   # ATM Implied Volatility (%)
    iv_rank:          float   # IV Rank 0-100 (where current IV sits vs 52-week range)
    atm_ce_oi:        int     # ATM Call OI
    atm_pe_oi:        int     # ATM Put OI
    max_pain:         int     # Max pain strike (where most options expire worthless)
    ce_oi_change:     int     # Change in CE OI (buildup = bearish)
    pe_oi_change:     int     # Change in PE OI (buildup = bullish)
    atm_delta:        float   # ATM option delta (0.4–0.6 ideal)
    atm_theta:        float   # Theta (daily decay in points)


_option_chain_warned = False   # log the missing-method warning only once


def fetch_option_chain(smart, spot: float, expiry_token: str) -> Optional[OptionChainData]:
    """
    Fetch option chain from Angel One and compute PCR, OI, IV, Greeks.
    Returns None if fetch fails (bot continues with partial signals).

    Note: getOptionGreeks is not available in smartapi-python 1.5.5.
    When Angel One adds it, this will start working automatically.
    Until then the option chain is skipped and confidence scoring uses
    the 7 non-Greeks indicators only.
    """
    global _option_chain_warned
    try:
        if not hasattr(smart, "getOptionGreeks"):
            if not _option_chain_warned:
                logger.info(
                    "ℹ️  getOptionGreeks not available in this SmartAPI version — "
                    "OI/PCR/Greeks signals disabled. Bot runs on 7/10 indicators."
                )
                _option_chain_warned = True
            return None

        resp = smart.getOptionGreeks({
            "name": config.INSTRUMENT,
            "expirydate": expiry_token,
        })
        if not resp.get("status") or not resp.get("data"):
            if not _option_chain_warned:
                logger.debug("Option chain fetch returned no data — skipping OI/PCR signals")
                _option_chain_warned = True
            return None

        data = resp["data"]
        atm  = round(spot / 50) * 50

        total_ce_oi = 0
        total_pe_oi = 0
        atm_ce_oi   = 0
        atm_pe_oi   = 0
        atm_ce_iv   = 0.0
        atm_pe_iv   = 0.0
        atm_delta   = 0.5
        atm_theta   = -5.0
        ce_oi_chg   = 0
        pe_oi_chg   = 0

        # Pain point: strike with max combined OI
        strike_oi: dict[int, int] = {}

        for row in data:
            strike = int(row.get("strikePrice", 0))
            opt    = row.get("optionType", "").upper()
            oi     = int(row.get("openInterest", 0) or 0)
            oi_chg = int(row.get("changeinOpenInterest", 0) or 0)
            iv     = float(row.get("impliedVolatility", 0) or 0)
            delta  = float(row.get("delta", 0.5) or 0.5)
            theta  = float(row.get("theta", -5) or -5)

            strike_oi[strike] = strike_oi.get(strike, 0) + oi

            if opt == "CE":
                total_ce_oi += oi
                ce_oi_chg   += oi_chg
                if strike == atm:
                    atm_ce_oi = oi
                    atm_ce_iv = iv
                    atm_delta = abs(delta)
                    atm_theta = theta
            elif opt == "PE":
                total_pe_oi += oi
                pe_oi_chg   += oi_chg
                if strike == atm:
                    atm_pe_oi = oi
                    atm_pe_iv = iv

        pcr     = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
        atm_iv  = (atm_ce_iv + atm_pe_iv) / 2
        max_pain = max(strike_oi, key=strike_oi.get) if strike_oi else atm

        # IV Rank: simplified (needs historical IV range — default to 50 if unavailable)
        iv_rank = min(100.0, max(0.0, (atm_iv - 10) / (50 - 10) * 100))

        return OptionChainData(
            pcr=pcr,
            atm_iv=atm_iv,
            iv_rank=iv_rank,
            atm_ce_oi=atm_ce_oi,
            atm_pe_oi=atm_pe_oi,
            max_pain=max_pain,
            ce_oi_change=ce_oi_chg,
            pe_oi_change=pe_oi_chg,
            atm_delta=atm_delta,
            atm_theta=atm_theta,
        )
    except Exception as exc:
        if not _option_chain_warned:
            logger.debug(f"Option chain unavailable: {exc}")
            _option_chain_warned = True
        return None


# ── Advanced Signal Generator ─────────────────────────────────

@dataclass
class AdvancedSignalResult:
    signal:       Signal
    option_type:  Optional[OptionType]
    confidence:   int          # 0–10 (weighted score)
    entry_price_hint: float    # expected premium at entry
    strategy_name: str         # which strategy triggered
    reason:       str          # full explanation

    # Component scores
    score_technical:  int = 0  # Supertrend+EMA+RSI+VWAP  (max 4)
    score_sentiment:  int = 0  # PCR + OI                  (max 3)
    score_greeks:     int = 0  # Delta + IV Rank + Theta   (max 3)

    @property
    def total_score(self) -> int:
        return self.score_technical + self.score_sentiment + self.score_greeks


def generate_advanced_signal(
    df: pd.DataFrame,
    vix: float,
    spot: float,
    option_chain: Optional[OptionChainData] = None,
    ltp_ce: float = 0.0,
    ltp_pe: float = 0.0,
) -> AdvancedSignalResult:
    """
    Full professional signal engine combining:
      Technical  : Supertrend, EMA crossover, RSI, VWAP         (4 pts)
      Sentiment  : PCR, OI buildup direction, Max Pain           (3 pts)
      Greeks     : Delta range, IV Rank filter, Theta check      (3 pts)
      VIX        : Hard veto if VIX > threshold

    Total max score: 10
    BUY CE/PE if score >= 6  (60% confluence)
    HOLD         if score <  6

    Returns AdvancedSignalResult
    """
    _HOLD = AdvancedSignalResult(
        signal=Signal.HOLD, option_type=None, confidence=0,
        entry_price_hint=0, strategy_name="NONE", reason=""
    )

    # ── VIX hard veto ─────────────────────────────────────────
    if vix > config.VIX_MAX_THRESHOLD:
        _HOLD.reason = f"VIX={vix:.1f} > {config.VIX_MAX_THRESHOLD} — HOLD"
        logger.warning(_HOLD.reason)
        return _HOLD

    # ── Technical signals (max 4) ─────────────────────────────
    df = compute_supertrend(df)
    df = compute_ema(df)
    df = compute_rsi(df)
    df = compute_vwap(df)
    df = df.dropna().reset_index(drop=True)

    if len(df) < 25:
        _HOLD.reason = "Insufficient candle data"
        return _HOLD

    row      = df.iloc[-2]   # last COMPLETED candle
    close    = row["close"]
    st_dir   = row["supertrend_dir"]
    ema_fast = row["ema_fast"]
    ema_slow = row["ema_slow"]
    rsi      = row["rsi"]
    vwap     = row["vwap"]

    tech_score = 0
    tech_notes = []

    if st_dir == "UP":
        tech_score += 1; tech_notes.append("ST↑")
    else:
        tech_score -= 1; tech_notes.append("ST↓")

    if ema_fast > ema_slow:
        tech_score += 1; tech_notes.append(f"EMA✓")
    else:
        tech_score -= 1; tech_notes.append(f"EMA✗")

    if 45 <= rsi <= 65:
        tech_score += 1; tech_notes.append(f"RSI{rsi:.0f}✓")
    elif rsi > 70:
        tech_score -= 1; tech_notes.append(f"RSI{rsi:.0f}OB")
    elif rsi < 30:
        tech_score -= 1; tech_notes.append(f"RSI{rsi:.0f}OS")

    if close > vwap:
        tech_score += 1; tech_notes.append("VWAP✓")
    else:
        tech_score -= 1; tech_notes.append("VWAP✗")

    # ── Sentiment signals (max 3) ─────────────────────────────
    sent_score = 0
    sent_notes = []

    if option_chain:
        pcr = option_chain.pcr
        # PCR > 1.2 → more puts than calls → bullish contrarian signal
        # PCR < 0.8 → more calls → bearish contrarian signal
        if pcr > 1.2:
            sent_score += 1; sent_notes.append(f"PCR={pcr:.2f}↑Bull")
        elif pcr < 0.8:
            sent_score -= 1; sent_notes.append(f"PCR={pcr:.2f}↓Bear")
        else:
            sent_notes.append(f"PCR={pcr:.2f}~")

        # OI change direction
        if option_chain.pe_oi_change > option_chain.ce_oi_change:
            sent_score += 1; sent_notes.append("PE_OI_BUILD↑")
        elif option_chain.ce_oi_change > option_chain.pe_oi_change:
            sent_score -= 1; sent_notes.append("CE_OI_BUILD↓")

        # Max pain — if spot is below max pain, market tends to drift up
        if spot < option_chain.max_pain:
            sent_score += 1; sent_notes.append(f"MaxPain={option_chain.max_pain}↑")
        elif spot > option_chain.max_pain:
            sent_score -= 1; sent_notes.append(f"MaxPain={option_chain.max_pain}↓")

    # ── Greeks signals (max 3) ────────────────────────────────
    greek_score = 0
    greek_notes = []

    if option_chain:
        # Delta: ATM options have delta ~0.5 — ideal for directional trades
        delta = option_chain.atm_delta
        if 0.35 <= delta <= 0.65:
            greek_score += 1; greek_notes.append(f"Δ={delta:.2f}✓")
        else:
            greek_score -= 1; greek_notes.append(f"Δ={delta:.2f}✗")

        # IV Rank: buy when IV is LOW (cheaper premiums), avoid when high
        iv_rank = option_chain.iv_rank
        if iv_rank < 30:
            greek_score += 1; greek_notes.append(f"IVR={iv_rank:.0f}✓")
        elif iv_rank > 70:
            greek_score -= 1; greek_notes.append(f"IVR={iv_rank:.0f}HIGH")
        else:
            greek_notes.append(f"IVR={iv_rank:.0f}~")

        # Theta: acceptable decay per day
        theta = abs(option_chain.atm_theta)
        if theta < 8:
            greek_score += 1; greek_notes.append(f"θ={theta:.1f}✓")
        else:
            greek_score -= 1; greek_notes.append(f"θ={theta:.1f}HIGH")

    # ── Final decision ────────────────────────────────────────
    total    = tech_score + sent_score + greek_score
    # Scale to 0–10 confidence
    max_possible = 10 if option_chain else 4
    raw_conf = (total + max_possible) / (2 * max_possible) * 10
    confidence = max(0, min(10, int(raw_conf)))

    full_reason = (
        f"Tech[{'+'.join(tech_notes)}]={tech_score} | "
        f"Sent[{','.join(sent_notes) or 'N/A'}]={sent_score} | "
        f"Greeks[{','.join(greek_notes) or 'N/A'}]={greek_score} | "
        f"Total={total} VIX={vix:.1f}"
    )

    MIN_SCORE = 3 if not option_chain else 6

    if total >= MIN_SCORE:
        return AdvancedSignalResult(
            signal=Signal.BUY, option_type=OptionType.CE,
            confidence=confidence, entry_price_hint=ltp_ce,
            strategy_name="TREND_CONFLUENCE",
            reason=f"BULLISH {full_reason}",
            score_technical=tech_score,
            score_sentiment=sent_score,
            score_greeks=greek_score,
        )
    elif total <= -MIN_SCORE:
        return AdvancedSignalResult(
            signal=Signal.BUY, option_type=OptionType.PE,
            confidence=confidence, entry_price_hint=ltp_pe,
            strategy_name="TREND_CONFLUENCE",
            reason=f"BEARISH {full_reason}",
            score_technical=tech_score,
            score_sentiment=sent_score,
            score_greeks=greek_score,
        )
    else:
        _HOLD.reason = f"MIXED {full_reason}"
        _HOLD.score_technical = tech_score
        _HOLD.score_sentiment = sent_score
        _HOLD.score_greeks    = greek_score
        return _HOLD
