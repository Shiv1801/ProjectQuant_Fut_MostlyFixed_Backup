"""
Fixed Group A holdout robustness test: date-cluster bootstrap + monthly breakdown.
Run from the Project Trade root:
    python .\test_group_a_robustness_fixed.py
Read-only DB access; no project files/DB/live settings modified.
"""
from __future__ import annotations
import math, sqlite3, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "project_trade.db"
INDEX_ID = "NIFTY50"
LOOKBACK, HORIZON, STEP = 60, 30, 30
ANN = math.sqrt(252 * 375)
BOOTSTRAPS = 3000
SEED = 20261012

def valid(r):
    _, o, h, l, c, _v, _s = r
    vals = [o, h, l, c]
    if not all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in vals):
        return False
    o, h, l, c = map(float, vals)
    return h >= max(o, c) and l <= min(o, c) and h >= l

def target(closes):
    r = np.diff(np.log(np.asarray(closes, float)))
    if len(r) < 2 or not np.all(np.isfinite(r)): return None
    return float(np.std(r, ddof=1) * ANN * 100)

def calc(y, p):
    e = np.asarray(p, float) - np.asarray(y, float)
    return float(np.mean(np.abs(e))), float(np.sqrt(np.mean(e * e)))

def main():
    if not DB_PATH.exists(): raise FileNotFoundError(f"Run from project root: {DB_PATH}")
    gk = ROOT / "engine" / "signal_layer" / "volatility" / "garman_klass.py"
    compile(gk.read_text(encoding="utf-8"), str(gk), "exec")
    from engine.signal_layer.volatility.realized_vol import compute_realized_vol
    from engine.signal_layer.volatility.ewma import EwmaVolatility
    from engine.signal_layer.volatility.parkinson import ParkinsonEstimator
    from engine.signal_layer.volatility.garch import compute_garch_forecast
    from engine.signal_layer.volatility.garman_klass import GarmanKlassEstimator

    models = {"realized_vol":"standalone", "ewma":EwmaVolatility(),
              "parkinson":ParkinsonEstimator(), "garch":"standalone",
              "garman_klass":GarmanKlassEstimator()}
    con = sqlite3.connect(DB_PATH.resolve().as_uri()+"?mode=ro", uri=True)
    try:
        rows = con.execute("""SELECT minute_bucket,open,high,low,close,volume,source
          FROM candles WHERE index_id=? AND source='historical' ORDER BY minute_bucket""",
          (INDEX_ID,)).fetchall()
    finally: con.close()
    rows = [r for r in rows if valid(r)]

    runs, run, prev, prevday = [], [], None, None
    for r in rows:
        dt = datetime.fromisoformat(r[0])
        if run and ((dt-prev).total_seconds()!=60 or dt.date()!=prevday):
            runs.append(run); run=[]
        run.append((r,dt)); prev,prevday=dt,dt.date()
    if run: runs.append(run)

    cases=[]
    for run in runs:
        for j in range(LOOKBACK-1, len(run)-HORIZON-1, STEP):
            ins=[z[0] for z in run[j-LOOKBACK+1:j+1]]
            fut=[z[0] for z in run[j+1:j+HORIZON+1]]
            md={"close_1min":[float(r[4]) for r in ins],
                "open_1min":[float(r[1]) for r in ins],
                "high_1min":[float(r[2]) for r in ins],
                "low_1min":[float(r[3]) for r in ins]}
            y=target([md["close_1min"][-1]]+[float(r[4]) for r in fut])
            if y is not None and math.isfinite(y):
                cases.append((ins[-1][0],md,y))
    split=int(len(cases)*0.70); hold=cases[split:]
    actual=[]; preds={name:[] for name in models}
    print(f"Forecast cases total={len(cases):,}; holdout candidates={len(hold):,}")
    start=time.time()
    for k,(timestamp,md,y) in enumerate(hold):
        actual.append(y)
        for name,model in models.items():
            try:
                if name=="realized_vol": val=compute_realized_vol(md["close_1min"]).get("realized_vol_pct")
                elif name=="garch": val=compute_garch_forecast(md["close_1min"]).get("garch_forecast_vol_pct")
                else: val=model.compute(md).score
                val=float(val) if val is not None and math.isfinite(float(val)) else float("nan")
            except Exception: val=float("nan")
            preds[name].append(val)
        if (k+1)%500==0: print(f"Processed {k+1:,}/{len(hold):,}")

    common=[i for i in range(len(hold)) if all(math.isfinite(preds[n][i]) for n in models)]
    y=np.asarray([actual[i] for i in common],float)
    p={n:np.asarray([preds[n][i] for i in common],float) for n in models}
    ensemble=np.mean(np.vstack([p[n] for n in models]),axis=0)
    ewma=p["ewma"]
    timestamps=[datetime.fromisoformat(hold[i][0]) for i in common]
    print(f"Common complete holdout cases={len(common):,}; unique dates={len(set(d.date() for d in timestamps))}")
    print("\nHOLDOUT METRICS")
    print(f"{'Model':18s} {'N':>6s} {'MAE':>10s} {'RMSE':>10s}")
    print("-"*48)
    for name in list(models)+["ensemble"]:
        v=ensemble if name=="ensemble" else p[name]
        mae,rmse=calc(y,v)
        print(f"{name:18s} {len(y):6d} {mae:10.4f} {rmse:10.4f}")
    if not len(y):
        print("No complete holdout cases; cannot run bootstrap."); return

    day_indices=defaultdict(list)
    for idx,dt in enumerate(timestamps): day_indices[dt.date().isoformat()].append(idx)
    days=list(day_indices); rng=np.random.default_rng(SEED)
    d_mae=[]; d_mse=[]
    for _ in range(BOOTSTRAPS):
        chosen=rng.integers(0,len(days),size=len(days))
        idx=[i for cluster in chosen for i in day_indices[days[cluster]]]
        d_mae.append(float(np.mean(np.abs(ewma[idx]-y[idx])-np.abs(ensemble[idx]-y[idx]))))
        d_mse.append(float(np.mean((ewma[idx]-y[idx])**2-(ensemble[idx]-y[idx])**2)))
    def ci(x): return np.quantile(np.asarray(x),[0.025,0.5,0.975])
    a,b=ci(d_mae),ci(d_mse)
    print("\nDATE-CLUSTER BOOTSTRAP: ENSEMBLE IMPROVEMENT OVER EWMA")
    print(f"Resamples={BOOTSTRAPS}; resampling unit=whole trading dates")
    print(f"MAE improvement (EWMA MAE - ensemble MAE): 95% CI [{a[0]:.5f}, {a[2]:.5f}], median={a[1]:.5f}")
    print(f"MSE improvement (EWMA MSE - ensemble MSE): 95% CI [{b[0]:.5f}, {b[2]:.5f}], median={b[1]:.5f}")
    print("Positive values favor ensemble; intervals containing zero mean improvement is uncertain.")

    print("\nMONTHLY HOLDOUT PERFORMANCE")
    print(f"{'Month':12s} {'N':>5s} {'EWMA MAE':>10s} {'Ens MAE':>10s} {'EWMA RMSE':>11s} {'Ens RMSE':>10s}")
    print("-"*64)
    by_month=defaultdict(list)
    for idx,dt in enumerate(timestamps): by_month[dt.strftime("%Y-%m")].append(idx)
    for month,idx in sorted(by_month.items()):
        em,er=calc(y[idx],ewma[idx]); am,ar=calc(y[idx],ensemble[idx])
        print(f"{month:12s} {len(idx):5d} {em:10.4f} {am:10.4f} {er:11.4f} {ar:10.4f}")
    print(f"\nElapsed={time.time()-start:.1f}s. Read-only test; no project settings changed.")

if __name__=="__main__": main()
