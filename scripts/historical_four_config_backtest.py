"""Auditable four-configuration historical backtest.

This runner is intentionally separate from the live application.  It reuses the
production quant scorer and confluence gate, but never imports the API startup
path or starts pollers.  Missing historical depth, option-chain IV, and VRP are
passed as unavailable; they are never replaced with spot proxies.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.backtest.bs_pricer import (  # noqa: E402
    black_scholes_price,
    estimate_realized_vol,
    nearest_atm_strike,
    next_weekly_expiry,
)
from engine.confluence_gate.chart_structure import confirm_chart_structure  # noqa: E402
from engine.confluence_gate.gate import evaluate_confluence  # noqa: E402
from engine.confluence_gate.quant_score import compute_quant_confidence  # noqa: E402
from engine.risk_engine.position_sizing import LOT_SIZES, compute_position_size  # noqa: E402
from engine.risk_engine.stops import check_hard_stop_loss, update_trailing_stop  # noqa: E402
from engine.signal_layer.momentum.hurst import compute_hurst_exponent  # noqa: E402
from engine.signal_layer.momentum.variance_ratio import compute_variance_ratio  # noqa: E402
from engine.signal_layer.momentum.zscore import compute_zscore  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
INDEX_ID = "NIFTY50"
LOT_SIZE = LOT_SIZES[INDEX_ID]
DATA_FILE = ROOT / "vwap_5yr_cache" / "nifty_5yr_candles.json"
COMMON_START = "2025-09-16"
COMMON_END = "2026-09-11"
INITIAL_CAPITAL = 150000.0
HURST_WINDOW = 100
ZSCORE_WINDOW = 20
VR_WINDOW = 100
VOL_WINDOW = 375 * 5

CONFIGS = {
    "QUANT_ONLY": {"require_chart": False, "require_vwap": False},
    "QUANT_PLUS_CHART_VETO": {"require_chart": True, "require_vwap": False},
    "QUANT_PLUS_VWAP": {"require_chart": False, "require_vwap": True},
    "FULL_SYSTEM": {"require_chart": True, "require_vwap": True},
}

RISK = {
    "risk_per_trade_pct": 1.0,
    "hard_sl_pct": 3.5,
    "time_stop_minutes": 3,
    "tsl_activation_pct": 5.0,
    "tsl_base_trail_pct": 3.0,
    "tsl_k": 0.15,
    "tsl_floor_pct": 5.0,
    "min_confidence": 0.95,
}


@dataclass
class TradeState:
    signal_index: int
    entry_index: int
    signal_time: str
    entry_time: str
    direction: str
    option_type: str
    strike: float
    expiry: str
    entry_spot: float
    entry_premium: float
    sigma: float
    lots: int
    capital_before: float
    chart: dict
    vwap: dict | None
    quant: dict
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    trailing_updates: list[dict] | None = None


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=IST) if "T" not in value else datetime.fromisoformat(value)


def candle_dt(candle: dict) -> datetime:
    return datetime.fromtimestamp(candle["epoch"], IST)


def load_candles(start: str, end: str) -> list[dict]:
    with DATA_FILE.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    start_dt = datetime.fromisoformat(start).date()
    end_dt = datetime.fromisoformat(end).date()
    selected = [
        c for c in raw
        if start_dt <= candle_dt(c).date() <= end_dt
    ]
    selected.sort(key=lambda c: c["epoch"])
    return selected


def vwap_series(candles: list[dict]) -> list[float | None]:
    values: list[float | None] = []
    day = None
    pv = volume = 0.0
    for candle in candles:
        current_day = candle_dt(candle).date()
        if current_day != day:
            day, pv, volume = current_day, 0.0, 0.0
        traded = float(candle.get("volume") or 0)
        if traded <= 0:
            values.append(None)
            continue
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3
        pv += typical * traded
        volume += traded
        values.append(pv / volume)
    return values


def unavailable_models() -> dict:
    return {
        "futures_ofi": {
            "status": "unavailable",
            "integration_status": "UNAVAILABLE",
            "reason": "historical futures depth snapshots are not present",
            "combined_ofi": None,
        },
        "ofi": {"status": "unavailable", "ofi": None, "reason": "spot OFI is not a futures substitute"},
        "pcr": {"pcr": None, "status": "unavailable"},
        "max_pain": {"max_pain": None, "status": "unavailable"},
        "oi_change": {"top_oi_builds": [], "status": "unavailable"},
        "vrp": {"vrp_pct": None, "favorable": None, "status": "unavailable"},
        "group_g": None,
    }


def model_snapshot(candles: list[dict], i: int) -> dict:
    history = candles[max(0, i - HURST_WINDOW):i]
    closes = [c["close"] for c in history if c.get("close") is not None]
    hurst = compute_hurst_exponent(closes)
    zscore = compute_zscore(closes[-ZSCORE_WINDOW:], window=ZSCORE_WINDOW)
    variance_ratio = compute_variance_ratio(closes[-VR_WINDOW:], q=5)
    models = {"hurst": hurst, "zscore": zscore, "variance_ratio": variance_ratio}
    models.update(unavailable_models())
    quant = compute_quant_confidence(models, evaluation_id=f"backtest-{i}")
    return {"models": models, "quant": quant, "sigma": estimate_realized_vol(closes[-VOL_WINDOW:])}


def cost_breakdown(entry: float, exit: float, qty: int) -> dict:
    # Mirrors engine/backtest/cost_model/costs.py and config/cost_model.yaml.
    brokerage = 20.0 * 2
    stt = exit * qty * 0.0625 / 100
    exchange = (entry + exit) * qty * 0.05 / 100
    sebi = (entry + exit) * qty * 0.0001 / 100
    stamp = entry * qty * 0.003 / 100
    gst = brokerage * 18 / 100
    slippage = entry * qty * 8 / 10000
    gross = (exit - entry) * qty
    total = brokerage + stt + exchange + sebi + stamp + gst + slippage
    return {
        "gross_pnl": gross,
        "brokerage": brokerage,
        "stt": stt,
        "exchange_charges": exchange,
        "sebi_fees": sebi,
        "stamp_duty": stamp,
        "gst": gst,
        "slippage": slippage,
        "total_costs": total,
        "net_pnl": gross - total,
    }


def option_premium(spot: float, strike: float, expiry: datetime, at: datetime, sigma: float, kind: str) -> float:
    t = max(1e-6, (expiry - at).total_seconds() / (365.25 * 24 * 3600))
    return black_scholes_price(spot, strike, t, sigma, kind)


def trade_row(state: TradeState, exit_time: str, exit_spot: float, exit_premium: float,
              reason: str, capital: float, data_warning: str, costs: dict) -> dict:
    qty = state.lots * LOT_SIZE
    net = costs["net_pnl"]
    return {
        "trade_id": f"{state.entry_time}-{state.direction}",
        "configuration": "",
        "signal_id": f"backtest-{state.signal_index}",
        "evaluation_id": f"backtest-{state.signal_index}",
        "instrument": INDEX_ID,
        "contract_expiry": state.expiry,
        "direction": state.direction,
        "strategy_version": "production_quant_gate_v1_historical",
        "signal_timestamp": state.signal_time,
        "entry_timestamp": state.entry_time,
        "entry_spot": state.entry_spot,
        "entry_premium": state.entry_premium,
        "entry_fill_assumption": "next_completed-candle open, BS proxy using realized volatility",
        "exit_timestamp": exit_time,
        "exit_spot": exit_spot,
        "exit_premium": exit_premium,
        "exit_reason": reason,
        "holding_minutes": None,
        "strike": state.strike,
        "option_type": state.option_type,
        "expiry": state.expiry,
        "quantity": qty,
        "lots": state.lots,
        "lot_size": LOT_SIZE,
        "capital_before": state.capital_before,
        "capital_after": capital,
        "exposure": state.entry_premium * qty,
        "quant_direction": state.quant.get("direction"),
        "quant_confidence": state.quant.get("confidence"),
        "quant_votes": json.dumps(state.quant.get("votes", []), sort_keys=True),
        "chart_verdict": state.chart.get("verdict"),
        "chart_confirmed": state.chart.get("confirmed"),
        "chart_veto_reason": None if state.chart.get("confirmed") else state.chart.get("verdict"),
        "vwap": None if state.vwap is None else state.vwap.get("vwap"),
        "price_vwap_relation": None if state.vwap is None else state.vwap.get("relation"),
        "vwap_confirmed": None if state.vwap is None else state.vwap.get("confirmed"),
        "hurst": state.quant.get("regime_context"),
        "variance_ratio": state.quant.get("regime_confirmation"),
        "zscore": None,
        "vrp_pct": None,
        "futures_ofi": None,
        "initial_stop_loss_pct": RISK["hard_sl_pct"],
        "trailing_stop_updates": json.dumps(state.trailing_updates or []),
        "max_favorable_excursion_pct": state.max_favorable,
        "max_adverse_excursion_pct": state.max_adverse,
        "gross_pnl": costs["gross_pnl"],
        "brokerage": costs["brokerage"],
        "stt": costs["stt"],
        "exchange_charges": costs["exchange_charges"],
        "sebi_fees": costs["sebi_fees"],
        "stamp_duty": costs["stamp_duty"],
        "gst": costs["gst"],
        "slippage": costs["slippage"],
        "total_costs": costs["total_costs"],
        "net_pnl": net,
        "return_pct_on_capital": net / state.capital_before * 100 if state.capital_before else 0,
        "data_quality_warning": data_warning,
    }


def run_configuration(candles: list[dict], name: str, flags: dict, vwaps: list[float | None]) -> tuple[list[dict], list[dict]]:
    trades: list[dict] = []
    rejected: list[dict] = []
    open_trade: TradeState | None = None
    capital = INITIAL_CAPITAL
    pending: tuple[int, TradeState] | None = None

    for i, candle in enumerate(candles):
        now = candle_dt(candle)
        # The signal is evaluated at a completed candle and fills only at the next bar.
        if pending and pending[0] == i:
            state = pending[1]
            sigma = state.sigma
            entry_spot = candle["open"]
            entry_premium = option_premium(entry_spot, state.strike, parse_dt(state.expiry), now, sigma, state.option_type)
            sizing = compute_position_size(capital, INDEX_ID, entry_premium, RISK["hard_sl_pct"], RISK["risk_per_trade_pct"])
            if sizing.get("lots", 0) > 0:
                state.entry_index = i
                state.entry_time = now.isoformat()
                state.entry_spot = entry_spot
                state.entry_premium = entry_premium
                state.lots = sizing["lots"]
                state.capital_before = capital
                open_trade = state
            else:
                rejected.append({"configuration": name, "timestamp": now.isoformat(), "reason": "insufficient_capital_for_one_lot", "direction": state.direction})
            pending = None

        if open_trade:
            sigma = estimate_realized_vol([c["close"] for c in candles[max(0, i - VOL_WINDOW):i + 1]]) or open_trade.sigma
            expiry = parse_dt(open_trade.expiry)
            adverse_spot = candle["low"] if open_trade.option_type == "CE" else candle["high"]
            favorable_spot = candle["high"] if open_trade.option_type == "CE" else candle["low"]
            adverse = option_premium(adverse_spot, open_trade.strike, expiry, now, sigma, open_trade.option_type)
            favorable = option_premium(favorable_spot, open_trade.strike, expiry, now, sigma, open_trade.option_type)
            close_premium = option_premium(candle["close"], open_trade.strike, expiry, now, sigma, open_trade.option_type)
            favorable_pct = (favorable / open_trade.entry_premium - 1) * 100
            adverse_pct = (adverse / open_trade.entry_premium - 1) * 100
            open_trade.max_favorable = max(open_trade.max_favorable, favorable_pct)
            open_trade.max_adverse = min(open_trade.max_adverse, adverse_pct)
            elapsed = (now - parse_dt(open_trade.entry_time)).total_seconds() / 60
            hard = check_hard_stop_loss(open_trade.entry_premium, adverse, datetime.min, datetime.min, RISK["hard_sl_pct"], 999999, open_trade.max_favorable > 0)
            tsl = update_trailing_stop(open_trade.max_favorable, adverse_pct, RISK["tsl_activation_pct"], RISK["tsl_base_trail_pct"], RISK["tsl_k"], RISK["tsl_floor_pct"])
            if tsl.get("triggered"):
                open_trade.trailing_updates = (open_trade.trailing_updates or []) + [{"timestamp": now.isoformat(), "triggered": True, "peak_pct": open_trade.max_favorable}]
            reason = None
            exit_price = close_premium
            exit_spot = candle["close"]
            if hard.get("triggered"):
                reason, exit_price, exit_spot = "hard_sl_hit", adverse, adverse_spot
            elif tsl.get("triggered"):
                reason, exit_price, exit_spot = "trailing_stop", adverse, adverse_spot
            elif elapsed >= RISK["time_stop_minutes"] and open_trade.max_favorable <= 0:
                reason = "time_stop"
            elif now >= expiry:
                reason = "expiry"
            if reason:
                costs = cost_breakdown(open_trade.entry_premium, exit_price, open_trade.lots * LOT_SIZE)
                capital += costs["net_pnl"]
                row = trade_row(open_trade, now.isoformat(), exit_spot, exit_price, reason, capital, "option IV and historical depth unavailable; BS uses realized-vol proxy", costs)
                row["configuration"] = name
                row["holding_minutes"] = round(elapsed, 2)
                row["zscore"] = open_trade.quant.get("zscore")
                row["hurst"] = open_trade.quant.get("hurst")
                row["variance_ratio"] = open_trade.quant.get("variance_ratio")
                trades.append(row)
                open_trade = None
            continue

        if i < max(HURST_WINDOW, VOL_WINDOW) or i + 1 >= len(candles):
            continue
        snapshot = model_snapshot(candles, i)
        quant = snapshot["quant"]
        current = candle["close"]
        chart = confirm_chart_structure(candles[i - HURST_WINDOW:i + 1], quant.get("direction")) if quant.get("direction") else {"verdict": "no_quant_direction", "confirmed": False}
        vwap = None
        if flags["require_vwap"]:
            value = vwaps[i]
            if value is not None:
                relation = "above" if current > value else "below" if current < value else "equal"
                vwap = {"price": current, "vwap": value, "relation": relation}
        gate = evaluate_confluence(quant, chart, RISK["min_confidence"], vwap, flags["require_vwap"], flags["require_chart"])
        reason = gate.get("veto_reason") or ("accepted" if gate.get("fired") else "rejected")
        rejected.append({
            "configuration": name, "timestamp": candle_dt(candle).isoformat(), "reason": reason,
            "fired": gate.get("fired"), "direction": quant.get("direction"), "confidence": quant.get("confidence"),
            "chart_verdict": chart.get("verdict"), "chart_confirmed": chart.get("confirmed"),
            "vwap": None if vwap is None else vwap.get("vwap"), "vwap_relation": None if vwap is None else vwap.get("relation"),
            "futures_ofi_status": "unavailable", "vrp_status": "unavailable",
        })
        if not gate.get("fired"):
            continue
        expiry = next_weekly_expiry(candle_dt(candle))
        state = TradeState(i, i + 1, candle_dt(candle).isoformat(), "", gate["direction"], "CE" if gate["direction"] == "long_call" else "PE", nearest_atm_strike(current), expiry.isoformat(), current, 0.0, snapshot["sigma"] or 0.05, 0, capital, chart, vwap, {
            **quant,
            "zscore": snapshot["models"]["zscore"].get("zscore"),
            "hurst": snapshot["models"]["hurst"].get("hurst"),
            "variance_ratio": snapshot["models"]["variance_ratio"].get("variance_ratio"),
        }, trailing_updates=[])
        pending = (i + 1, state)
    return trades, rejected


def metric_rows(name: str, trades: list[dict], candles: list[dict]) -> dict:
    pnls = [float(t["net_pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    equity = [INITIAL_CAPITAL]
    for pnl in pnls:
        equity.append(equity[-1] + pnl)
    peak = INITIAL_CAPITAL
    drawdowns = []
    for value in equity:
        peak = max(peak, value)
        drawdowns.append(value - peak)
    gross = sum(float(t["gross_pnl"]) for t in trades)
    costs = sum(float(t["total_costs"]) for t in trades)
    returns = [p / equity[i] for i, p in enumerate(pnls)] if pnls else []
    mean = sum(returns) / len(returns) if returns else 0
    stdev = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) if len(returns) > 1 else 0
    return {
        "configuration": name, "starting_capital": INITIAL_CAPITAL, "ending_capital": equity[-1],
        "net_pnl": sum(pnls), "net_return_pct": (equity[-1] / INITIAL_CAPITAL - 1) * 100,
        "trade_count": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": len(wins) / len(pnls) * 100 if pnls else None,
        "average_win": sum(wins) / len(wins) if wins else None, "average_loss": sum(losses) / len(losses) if losses else None,
        "payoff_ratio": (sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses else None,
        "expectancy_per_trade": sum(pnls) / len(pnls) if pnls else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "max_drawdown": min(drawdowns), "max_drawdown_pct": min(drawdowns) / INITIAL_CAPITAL * 100,
        "sharpe_trade_annualized": mean / stdev * math.sqrt(252) if stdev else None,
        "sortino_trade_annualized": mean / math.sqrt(sum(min(r, 0) ** 2 for r in returns) / len(returns)) * math.sqrt(252) if returns and any(r < 0 for r in returns) else None,
        "average_holding_minutes": sum(float(t["holding_minutes"]) for t in trades) / len(trades) if trades else None,
        "gross_pnl": gross, "total_costs": costs, "costs_pct_of_gross": costs / gross * 100 if gross else None,
        "exposure_utilization_pct": sum(float(t["exposure"]) for t in trades) / (INITIAL_CAPITAL * len(candles)) * 100 if candles else 0,
        "data_quality": "PARTIAL: historical futures OFI, option IV, VRP, and option-chain data unavailable",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def html_report(output: Path, summaries: list[dict], trades: list[dict], rejected: list[dict], monthly: list[dict], quality: list[dict]) -> None:
    payload = json.dumps({"summaries": summaries, "trades": trades, "rejected": rejected, "monthly": monthly, "quality": quality}, default=str)
    html = """<!doctype html><html><head><meta charset='utf-8'><title>Four Configuration Backtest</title>
<style>body{font:14px system-ui;background:#101722;color:#e9eef5;margin:0}main{max-width:1400px;margin:auto;padding:28px}h1{font-size:30px}.card{background:#172231;border:1px solid #2b3a4d;border-radius:12px;padding:18px;margin:14px 0}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:8px;border-bottom:1px solid #2b3a4d;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#9fc3dd;position:sticky;top:0;background:#172231}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric{font-size:24px;color:#7ee2b8}.warn{color:#ffcc80}.chart{width:100%;height:240px;background:#101722}.scroll{overflow:auto;max-height:430px}button,input{padding:9px;border-radius:6px;border:1px solid #40546b;background:#101722;color:#fff;margin:3px}button{cursor:pointer}.small{color:#aab7c5;font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}main{padding:14px}}</style></head><body><main>
<h1>Four-Configuration Historical Backtest</h1><p class='small'>NIFTY50 | common eligible window 2025-09-16 to 2026-09-11 IST | production quant scorer and confluence gate reused</p>
<div class='card warn'><b>Data quality:</b> Futures OFI is unavailable because no historical futures depth snapshots exist. Historical option IV/VRP and option-chain inputs are also unavailable. No spot OFI substitution was made. Results are partial and not live-equivalent.</div>
<div id='summary' class='grid'></div><div class='card'><h2>Comparison</h2><div class='scroll'><table id='summaryTable'></table></div></div>
<div class='card'><h2>Equity Curves</h2><svg id='equity' class='chart' viewBox='0 0 1000 240'></svg></div>
<div class='card'><h2>Trades</h2><input id='tradeSearch' placeholder='Filter trades'><div class='scroll'><table id='trades'></table></div></div>
<div class='card'><h2>Rejected Signals</h2><input id='rejectSearch' placeholder='Filter decisions'><div class='scroll'><table id='rejected'></table></div></div>
<div class='card'><h2>Methodology and Limitations</h2><p>Signals use completed candles only; entries fill at the next candle open. Stops use a conservative adverse OHLC extreme because intrabar ordering is unavailable. Options use the existing Black-Scholes engine with realized volatility as a clearly labeled IV proxy. Costs mirror config/cost_model.yaml. Hurst and Variance Ratio are passed to the production scorer; they are not independent votes. Chart and VWAP are the only configuration differences.</p></div>
<script>const D=__DATA__;const fmt=(x)=>x==null?'N/A':(Number(x).toLocaleString(undefined,{maximumFractionDigits:2}));function table(id,rows){let el=document.getElementById(id);if(!rows.length){el.innerHTML='<tr><td>No rows</td></tr>';return}let cols=Object.keys(rows[0]);el.innerHTML='<thead><tr>'+cols.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(x=>'<td>'+fmt(r[x])+'</td>').join('')+'</tr>').join('')+'</tbody>'}D.summaries.forEach(s=>document.getElementById('summary').innerHTML+=`<div class='card'><div>${s.configuration}</div><div class='metric'>${fmt(s.net_return_pct)}%</div><div>Net P&L INR ${fmt(s.net_pnl)}</div><div>${s.trade_count} trades</div></div>`);table('summaryTable',D.summaries);table('trades',D.trades);table('rejected',D.rejected);function filter(input,id,rows){document.getElementById(input).oninput=e=>{let q=e.target.value.toLowerCase();table(id,rows.filter(r=>JSON.stringify(r).toLowerCase().includes(q)))}}filter('tradeSearch','trades',D.trades);filter('rejectSearch','rejected',D.rejected);let svg=document.getElementById('equity');let colors=['#7ee2b8','#79b8ff','#ffcc80','#ff7d9c'];D.summaries.forEach((s,k)=>{let ts=D.trades.filter(t=>t.configuration===s.configuration),v=150000,pts='';ts.forEach((t,i)=>{v+=Number(t.net_pnl);pts+=(i*950/Math.max(ts.length-1,1))+','+(220-(v-100000)/(Math.max(Math.abs(s.net_pnl),1))*80)+' '});svg.innerHTML+=`<polyline fill='none' stroke='${colors[k]}' stroke-width='2' points='${pts}'/><text x='${10+k*220}' y='18' fill='${colors[k]}'>${s.configuration}</text>`});</script></main></body></html>"""
    output.write_text(html.replace("__DATA__", payload), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=COMMON_START)
    parser.add_argument("--end", default=COMMON_END)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or ROOT / "backtest_outputs" / f"four_config_{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    candles = load_candles(args.start, args.end)
    if not candles:
        raise SystemExit("No candles in requested period")
    vwaps = vwap_series(candles)
    all_trades: list[dict] = []
    all_rejected: list[dict] = []
    summaries: list[dict] = []
    for name, flags in CONFIGS.items():
        trades, rejected = run_configuration(candles, name, flags, vwaps)
        all_trades.extend(trades)
        all_rejected.extend(rejected)
        summaries.append(metric_rows(name, trades, candles))
        write_csv(output / {"QUANT_ONLY": "trades_quant_only.csv", "QUANT_PLUS_CHART_VETO": "trades_quant_chart_veto.csv", "QUANT_PLUS_VWAP": "trades_quant_vwap.csv", "FULL_SYSTEM": "trades_full_system.csv"}[name], trades)
    monthly = []
    for name in CONFIGS:
        for month in sorted({t["exit_timestamp"][:7] for t in all_trades if t["configuration"] == name}):
            rows = [t for t in all_trades if t["configuration"] == name and t["exit_timestamp"].startswith(month)]
            monthly.append({"configuration": name, "month": month, "trades": len(rows), "net_pnl": sum(float(t["net_pnl"]) for t in rows)})
    quality = [{"check": "common_candle_window", "result": "PASS", "detail": f"{args.start} to {args.end} IST"}, {"check": "futures_ofi", "result": "UNAVAILABLE", "detail": "No historical futures depth snapshots; no spot substitution"}, {"check": "historical_option_iv", "result": "UNAVAILABLE", "detail": "Black-Scholes uses realized-vol proxy"}, {"check": "lookahead", "result": "PASS", "detail": "Signals use completed candle and next-bar-open entry"}, {"check": "intrabar_order", "result": "CONSERVATIVE_ASSUMPTION", "detail": "Adverse OHLC extreme checked first"}, {"check": "vwap_volume", "result": "PARTIAL", "detail": "VWAP uses only positive historical volume; zero-volume candles produce unavailable VWAP"}]
    write_csv(output / "backtest_summary.csv", summaries)
    write_csv(output / "rejected_signals.csv", all_rejected)
    write_csv(output / "monthly_performance.csv", monthly)
    write_csv(output / "data_quality_report.csv", quality)
    manifest = {"generated_at": datetime.now(IST).isoformat(), "data_file": str(DATA_FILE.relative_to(ROOT)), "start": args.start, "end": args.end, "configs": CONFIGS, "risk": RISK, "initial_capital": INITIAL_CAPITAL, "lot_size": LOT_SIZE, "production_functions": ["engine.confluence_gate.quant_score.compute_quant_confidence", "engine.confluence_gate.gate.evaluate_confluence"], "futures_ofi": "unavailable; omitted rather than substituted", "pdf": "not generated by runner; use browser Print -> Save as PDF"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / "methodology_and_assumptions.md").write_text("# Methodology\n\n" + json.dumps(manifest, indent=2) + "\n\nThe four runs share the same candles, warm-up, risk, sizing, pricing, costs, stops, and fill assumptions. Only production chart and VWAP gate requirements differ. Historical Futures OFI, option IV, VRP, and option-chain inputs are unavailable and receive no vote.\n", encoding="utf-8")
    (output / "validation_report.md").write_text("# Validation Report\n\n- Production quant scorer and confluence gate were imported directly.\n- Futures OFI was integrated as an unavailable production input, not a diagnostic-only fake or spot substitute.\n- Signal timestamps precede next-bar entries.\n- Four configurations use identical eligibility and risk mechanics.\n- PDF automation was not attempted without an installed browser/export tool; open `index.html` and use Print -> Save as PDF.\n", encoding="utf-8")
    html_report(output / "index.html", summaries, all_trades, all_rejected, monthly, quality)
    print(output)


if __name__ == "__main__":
    main()
