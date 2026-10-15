"""All-in-one Group A EWMA validation + optional live forecast shadow monitor.

Run from Project Trade root:
  python .\\group_a_ewma_all_in_one.py             # historical validation, read-only DB
  python .\\group_a_ewma_all_in_one.py --shadow    # live forecast shadow monitor

Historical mode reads SQLite read-only and writes CSV reports only.
Shadow mode also reads SQLite read-only and writes CSV logs only. It never writes to
SQLite, imports live strategy modules, sends orders, or changes app settings.

IMPORTANT: shadow mode validates forecasts, NOT trade profitability or signal changes.
A strategy-level shadow comparison requires integration into the app's signal evaluation
path; this standalone tool deliberately does not guess that integration point.
"""
from __future__ import annotations
import argparse, csv, math, sqlite3, time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

ROOT = Path.cwd()
DB_PATH = ROOT / "data" / "project_trade.db"
REPORT_DIR = ROOT / "reports" / "ewma_all_in_one"
SHADOW_DIR = ROOT / "reports" / "ewma_shadow"
INDEX_ID = "NIFTY50"
LOOKBACK = 60
HORIZON = 30
SAMPLE_EVERY = 30
ANN = math.sqrt(252 * 375)
LAMBDA = 0.94
INIT_N = 14
BOOTSTRAP_REPS = 3000
SEED = 20261013


def valid_ohlc(row):
    _, o, h, l, c, *_ = row
    vals = [o, h, l, c]
    if not all(v is not None and math.isfinite(float(v)) and float(v) > 0 for v in vals): return False
    o, h, l, c = map(float, vals)
    return h >= max(o, c) and l <= min(o, c) and h >= l


def vol_pct(r):
    if len(r) < 2 or not np.all(np.isfinite(r)): return None
    return float(np.std(r, ddof=1) * ANN * 100)


def forecast(closes, init_n):
    closes = np.asarray(closes, dtype=float)
    if len(closes) != LOOKBACK or not np.all(np.isfinite(closes)) or np.any(closes <= 0): return None
    r = np.diff(np.log(closes))
    if len(r) != LOOKBACK - 1 or init_n < 1 or init_n >= len(r): return None
    v = float(np.mean(r[:init_n] ** 2))
    for x in r[init_n:]: v = LAMBDA * v + (1-LAMBDA) * float(x*x)
    z = math.sqrt(max(v, 0)) * ANN * 100
    return z if math.isfinite(z) else None


def metrics(name, preds, actual, ids):
    pairs = [(float(preds[i]), float(actual[i])) for i in ids if preds.get(i) is not None and math.isfinite(float(preds[i]))]
    if not pairs: return {"variant": name, "n": 0}
    p=np.array([a for a,b in pairs]); y=np.array([b for a,b in pairs]); e=p-y
    corr=float(np.corrcoef(p,y)[0,1]) if len(p)>1 and np.std(p)>0 and np.std(y)>0 else float('nan')
    return {"variant":name,"n":len(pairs),"mae":float(np.mean(np.abs(e))),"rmse":float(np.sqrt(np.mean(e*e))),
            "bias_pred_minus_actual":float(np.mean(e)),"median_abs_error":float(np.median(np.abs(e))),
            "p90_abs_error":float(np.percentile(np.abs(e),90)),"pearson_corr":corr}


def write_csv(path, rows):
    if not rows: return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys=[]
    for r in rows:
        for k in r:
            if k not in keys: keys.append(k)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore'); w.writeheader(); w.writerows(rows)


def read_rows(source=None, limit=None):
    if not DB_PATH.exists(): raise FileNotFoundError(f"Database not found: {DB_PATH}. Run from project root.")
    con=sqlite3.connect(DB_PATH.resolve().as_uri()+"?mode=ro", uri=True, timeout=5)
    try:
        q="SELECT minute_bucket, open, high, low, close, volume, source FROM candles WHERE index_id=?"
        args=[INDEX_ID]
        if source is not None: q += " AND source=?"; args.append(source)
        q += " ORDER BY minute_bucket DESC"
        if limit is not None: q += " LIMIT ?"; args.append(int(limit))
        rows=con.execute(q,args).fetchall()
        return list(reversed(rows))
    finally: con.close()


def build_historical_cases(raw):
    valid=[r for r in raw if valid_ohlc(r)]
    runs=[]; run=[]; prev_dt=prev_date=None
    for row in valid:
        dt=datetime.fromisoformat(row[0])
        if run and ((dt-prev_dt).total_seconds()!=60 or dt.date()!=prev_date): runs.append(run); run=[]
        run.append((row,dt)); prev_dt,prev_date=dt,dt.date()
    if run: runs.append(run)
    cases=[]
    for rr in runs:
        for i in range(LOOKBACK-1, len(rr)-HORIZON-1, SAMPLE_EVERY):
            ins=rr[i-LOOKBACK+1:i+1]; fut=rr[i+1:i+HORIZON+1]
            if len(ins)!=LOOKBACK or len(fut)!=HORIZON: continue
            closes=np.array([float(x[0][4]) for x in ins])
            future=[float(x[0][4]) for x in fut]
            target=vol_pct(np.diff(np.log(np.array([closes[-1]]+future))))
            if target is None: continue
            cases.append({"timestamp":ins[-1][1].isoformat(sep=' '),"date":ins[-1][1].date().isoformat(),
                          "closes":closes,"target":target,"input_vol":vol_pct(np.diff(np.log(closes)))})
    return valid,runs,cases


def historical():
    REPORT_DIR.mkdir(parents=True,exist_ok=True)
    raw=read_rows(source='historical')
    valid,runs,cases=build_historical_cases(raw)
    if len(cases)<200: raise RuntimeError(f"Only {len(cases)} valid historical cases; aborting.")
    split=int(len(cases)*.70); dev=list(range(split)); hold=list(range(split,len(cases)))
    actual={i:c['target'] for i,c in enumerate(cases)}
    baseline={i:forecast(c['closes'],1) for i,c in enumerate(cases)}
    candidate={i:forecast(c['closes'],INIT_N) for i,c in enumerate(cases)}
    x=np.array([candidate[i] for i in dev]); y=np.array([actual[i] for i in dev])
    slope,intercept=np.polyfit(x,y,1); calibrated={i:max(0,float(intercept+slope*candidate[i])) for i in range(len(cases))}
    scale=float(np.dot(x,y)/np.dot(x,x)) if np.dot(x,x)>0 else 1.0
    scaled={i:max(0,float(scale*candidate[i])) for i in range(len(cases))}
    variants={"baseline_current_init_1":baseline,"candidate_init14_uncalibrated":candidate,
              "affine_calibration_dev_fit":calibrated,"scale_calibration_dev_fit":scaled}
    overall=[]
    for split_name,ids in [('development',dev),('holdout',hold)]:
        for name,p in variants.items(): overall.append({"split":split_name,**metrics(name,p,actual,ids)})
    # thresholds based only on development actual targets
    qtar=np.quantile([actual[i] for i in dev],[.90,.95,.99])
    spikes=[]
    for lab,thr in zip(['p90','p95','p99'],qtar):
        ids=[i for i in hold if actual[i]>=thr]
        for name,p in variants.items(): spikes.append({"threshold":lab,"dev_threshold":float(thr),**metrics(name,p,actual,ids)})
    months=defaultdict(list)
    for i in hold: months[cases[i]['date'][:7]].append(i)
    monthly=[]
    for m,ids in sorted(months.items()):
        for name,p in variants.items(): monthly.append({"month":m,**metrics(name,p,actual,ids)})
    dv=np.array([cases[i]['input_vol'] for i in dev]); qs=np.quantile(dv,[.2,.4,.6,.8])
    regimes={i:['very_low','low','medium','high','very_high'][int(np.searchsorted(qs,cases[i]['input_vol'],side='right'))] for i in hold}
    regrows=[]
    for reg in ['very_low','low','medium','high','very_high']:
        ids=[i for i in hold if regimes[i]==reg]
        for name,p in variants.items(): regrows.append({"regime":reg,**metrics(name,p,actual,ids)})
    bydate=defaultdict(list)
    for i in hold: bydate[cases[i]['date']].append(i)
    dates=sorted(bydate); rng=np.random.default_rng(SEED); boots=[]
    for name,p in variants.items():
        if name=='baseline_current_init_1': continue
        vals=[]
        for _ in range(BOOTSTRAP_REPS):
            chosen=rng.choice(dates,size=len(dates),replace=True); diffs=[]
            for d in chosen:
                for i in bydate[d]: diffs.append(abs(baseline[i]-actual[i])-abs(p[i]-actual[i]))
            vals.append(float(np.mean(diffs)))
        boots.append({"candidate":name,"reps":BOOTSTRAP_REPS,"holdout_dates":len(dates),"mean_mae_improvement":float(np.mean(vals)),
                      "ci_low":float(np.percentile(vals,2.5)),"ci_high":float(np.percentile(vals,97.5)),
                      "fraction_positive":float(np.mean(np.array(vals)>0))})
    predrows=[]
    for i,c in enumerate(cases):
        predrows.append({"timestamp":c['timestamp'],"date":c['date'],"split":"development" if i<split else "holdout",
                         "actual_future_vol":c['target'],"input_vol":c['input_vol'],"baseline":baseline[i],"candidate_init14":candidate[i],
                         "affine_calibrated":calibrated[i],"scaled_calibrated":scaled[i],"regime":regimes.get(i,''),
                         "actual_p90_spike":c['target']>=qtar[0],"actual_p95_spike":c['target']>=qtar[1],"actual_p99_spike":c['target']>=qtar[2]})
    diagnostics=[{"check":"database_read_mode","result":"SQLite URI mode=ro","status":"PASS"},
                 {"check":"raw_historical_rows","result":len(raw),"status":"INFO"},{"check":"valid_ohlc_rows","result":len(valid),"status":"PASS" if len(valid)>1000 else "WARN"},
                 {"check":"excluded_invalid_rows","result":len(raw)-len(valid),"status":"INFO"},{"check":"contiguous_runs","result":len(runs),"status":"INFO"},
                 {"check":"cases","result":len(cases),"status":"PASS" if len(cases)>1000 else "WARN"},
                 {"check":"dev_holdout","result":f"{len(dev)}/{len(hold)}","status":"INFO"},
                 {"check":"calibration_parameters","result":f"affine actual={intercept:.8f}+{slope:.8f}*forecast; scale={scale:.8f}; fitted on dev only","status":"INFO"},
                 {"check":"production_changed","result":"No","status":"PASS"}]
    write_csv(REPORT_DIR/'overall.csv',overall); write_csv(REPORT_DIR/'spikes.csv',spikes); write_csv(REPORT_DIR/'monthly.csv',monthly)
    write_csv(REPORT_DIR/'regimes.csv',regrows); write_csv(REPORT_DIR/'bootstrap.csv',boots); write_csv(REPORT_DIR/'predictions.csv',predrows); write_csv(REPORT_DIR/'diagnostics.csv',diagnostics)
    print('='*112); print('GROUP A ALL-IN-ONE HISTORICAL VALIDATION — READ ONLY'); print('='*112)
    print(f"Historical rows={len(raw):,}; valid={len(valid):,}; runs={len(runs):,}; cases={len(cases):,}; dev={len(dev):,}; holdout={len(hold):,}")
    print(f"Candidate: lambda={LAMBDA:.2f}, init={INIT_N}; affine intercept={intercept:.6f}, slope={slope:.6f}, scale={scale:.6f}")
    print('\nOVERALL METRICS')
    for r in overall: print(f"{r['split']:<12} {r['variant']:<30} n={r.get('n',0):4} MAE={r.get('mae',float('nan')):.4f} RMSE={r.get('rmse',float('nan')):.4f} bias={r.get('bias_pred_minus_actual',float('nan')):.4f} corr={r.get('pearson_corr',float('nan')):.4f}")
    print('\nBOOTSTRAP: positive MAE improvement favors candidate over baseline')
    for r in boots: print(f"{r['candidate']:<30} mean={r['mean_mae_improvement']:.5f} 95%CI [{r['ci_low']:.5f},{r['ci_high']:.5f}] positive={r['fraction_positive']:.1%}")
    print(f"\nReports: {REPORT_DIR}"); print('No model code, database contents, settings, or orders changed.')
    print('Calibration is diagnostic only; do not deploy calibration without separate justification.')


def shadow(interval=30):
    SHADOW_DIR.mkdir(parents=True,exist_ok=True)
    log_path=SHADOW_DIR/'forecast_shadow.csv'
    pending_path=SHADOW_DIR/'pending_forecasts.csv'
    print('='*100); print('EWMA LIVE FORECAST SHADOW MONITOR — NO TRADES / READ-ONLY DATABASE'); print('='*100)
    print(f"Database: {DB_PATH}\nOutput: {log_path}\nPress Ctrl+C to stop. Run only while the app is already collecting live candles.")
    print('This logs forecasts only; it does not change strategy signals or measure P&L.')
    seen=set()
    # Recover already logged timestamps to avoid duplicate forecast rows after restart.
    if log_path.exists():
        try:
            with log_path.open(newline='',encoding='utf-8-sig') as f:
                for r in csv.DictReader(f): seen.add(r.get('timestamp'))
        except Exception: pass
    fields=['timestamp','observed_at','source','last_close','n_closes','baseline_lam094_init1','candidate_lam094_init14','candidate_minus_baseline','input_window_vol','status']
    try:
        while True:
            try:
                # Prefer recent live rows only; do not silently substitute historical data in shadow mode.
                raw=read_rows(source='live',limit=500)
                valid=[r for r in raw if valid_ohlc(r)]
                if not valid:
                    print(f"[{datetime.now().isoformat(timespec='seconds')}] No valid source='live' candles yet; waiting.")
                    time.sleep(interval); continue
                # Only the newest uninterrupted same-day 60-candle window is eligible.
                seq=[]
                for row in valid:
                    dt=datetime.fromisoformat(row[0])
                    if seq:
                        pdt=datetime.fromisoformat(seq[-1][0])
                        if (dt-pdt).total_seconds()!=60 or dt.date()!=pdt.date(): seq=[]
                    seq.append(row)
                if len(seq)<LOOKBACK:
                    print(f"[{datetime.now().isoformat(timespec='seconds')}] Only {len(seq)} contiguous live candles; need {LOOKBACK}.")
                    time.sleep(interval); continue
                seq=seq[-LOOKBACK:]; ts=seq[-1][0]
                if ts in seen:
                    time.sleep(interval); continue
                closes=np.array([float(r[4]) for r in seq]); rr=np.diff(np.log(closes))
                base=forecast(closes,1); cand=forecast(closes,INIT_N); iv=vol_pct(rr)
                status='OK' if base is not None and cand is not None else 'FORECAST_INVALID'
                row={'timestamp':ts,'observed_at':datetime.now().isoformat(timespec='seconds'),'source':'live','last_close':closes[-1],
                     'n_closes':len(closes),'baseline_lam094_init1':base,'candidate_lam094_init14':cand,
                     'candidate_minus_baseline':(cand-base) if base is not None and cand is not None else None,
                     'input_window_vol':iv,'status':status}
                exists=log_path.exists()
                with log_path.open('a',newline='',encoding='utf-8-sig') as f:
                    w=csv.DictWriter(f,fieldnames=fields); 
                    if not exists or log_path.stat().st_size==0: w.writeheader()
                    w.writerow(row)
                seen.add(ts)
                print(f"[{ts}] baseline={base:.4f} candidate={cand:.4f} delta={cand-base:+.4f} input_vol={iv:.4f} status={status}")
            except sqlite3.OperationalError as e:
                print(f"SQLite read issue: {e}; retrying without modifying DB.")
            except Exception as e:
                print(f"Shadow cycle warning: {type(e).__name__}: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print('\nShadow monitor stopped by user. CSV log retained.')
        print(f"Forecast log: {log_path}")
        print('Forecast outcomes and strategy impact still require a later evaluation after 30 future candles per timestamp.')


def main():
    ap=argparse.ArgumentParser(description='All-in-one Group A EWMA historical validation and optional live shadow forecast logging')
    ap.add_argument('--shadow',action='store_true',help='continuously log baseline/candidate forecasts from source=live candles')
    ap.add_argument('--interval',type=int,default=30,help='shadow polling interval seconds (minimum 10)')
    args=ap.parse_args()
    if args.shadow: shadow(max(10,args.interval))
    else: historical()

if __name__=='__main__': main()
