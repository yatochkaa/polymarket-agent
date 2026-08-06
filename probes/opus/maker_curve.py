#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# maker_curve.py -- edge on FILLED pairs as a function of the fill window / fill share.
# This is the honest form of "at what fill share does the maker branch stop paying off":
# the window sets both the fill share and the composition of the filled subset.
import json, os, sys, math, csv, time
import numpy as np
from pathlib import Path
from collections import defaultdict
HERE = Path(os.path.dirname(os.path.abspath(__file__))); ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
from src.validate.funnel_a import (accumulate_pairs, filter1_pass, iter_artifact_pairs, day_from_slug, TIERS)
from pm.stats import cluster_mean_se
ART = ROOT / "data" / "collect_window_2026-02-01_2026-04-28.json"
TRADES = ROOT / "data" / "trades_raw_win"
R_TIER = {"atp": 0.6474, "wta": 0.6104}
LIVE = ["0xde9f7f4e","0xa509ae94","0xdbdd4515","0x204f72f3","0x9663a1bc","0xfbe49f06","0x70f96881","0x3d7817cc"]
SPREAD_MED = 0.005
f1, f5, _ = accumulate_pairs(iter_artifact_pairs(ART)); f1p, _ = filter1_pass(f1)
mean_tier = {t: float(np.mean([sum(f5[t][w]["clv"])/len(f5[t][w]["clv"]) for w in sorted(f1p[t]) if f5[t].get(w) and f5[t][w]["clv"]])) for t in TIERS}
d = json.load(open(ART, encoding="utf-8"))
sel = [p for p in d["pairs"] if any(p["wallet"].startswith(x) for x in LIVE)]
spread_map = {}
for r in csv.DictReader(open(HERE/"spread_estimator_nmin_v2.csv", encoding="utf-8")):
    if r["spread_cost_point_60"] != "":
        spread_map[(r["conditionId"], r["proxyWallet"])] = float(r["spread_cost_point_60"])
sp = np.array([spread_map.get((p["cond"],p["wallet"]), SPREAD_MED) for p in sel])
gross = np.array([p["clv"] for p in sel]); tiers=[p["tier"] for p in sel]
days = [day_from_slug(p.get("slug") or "") or p["cond"] for p in sel]
net_obs = gross - sp
net_shr = np.array([mean_tier[t]+R_TIER[t]*(g-mean_tier[t]) for t,g in zip(tiers,gross)]) - sp
man = {json.loads(l)["cond"]: json.loads(l) for l in open(TRADES/"manifest.jsonl", encoding="utf-8")}
WIN = [1,2,5,10,15,30,45,60,90,120,240,480,10**9]
n = len(sel)
first_fill = np.full(n, np.inf); first_fill_s = np.full(n, np.inf)
cache = {}
for i,p in enumerate(sel):
    c = p["cond"]
    if c not in cache:
        m = man[c]; rows = json.load(open(TRADES/m["file"], encoding="utf-8")); gst=m["gst"]
        ts=[];pT=[];bT=[];wal=defaultdict(list)
        for x in rows:
            pr=x.get("price"); t=x.get("timestamp")
            if pr is None or t is None or not (0.0<=pr<=1.0) or t>=gst: continue
            if x.get("outcomeIndex")==0: price_T=pr; tb=(x.get("side")=="BUY")
            else: price_T=1.0-pr; tb=(x.get("side")=="SELL")
            ts.append(t);pT.append(price_T);bT.append(tb);wal[x.get("proxyWallet")].append(t)
        o=np.argsort(np.array(ts,dtype=float),kind="stable")
        cache[c]=dict(ts=np.array(ts,float)[o],p=np.array(pT,float)[o],buy=np.array(bT,bool)[o],
                      wal={w:min(v) for w,v in wal.items()})
    T=cache[c]; e=T["wal"].get(p["wallet"])
    if e is None: continue
    P=p["entry_vwap"]; after=T["ts"]>e
    if p["direction"]>0:
        opt=after&(~T["buy"])&(T["p"]<=P); st=after&(~T["buy"])&(T["p"]<P)
    else:
        opt=after&(T["buy"])&(T["p"]>=P); st=after&(T["buy"])&(T["p"]>P)
    dt=(T["ts"]-e)/60.0
    if opt.any(): first_fill[i]=float(dt[opt].min())
    if st.any(): first_fill_s[i]=float(dt[st].min())
print("=== EDGE ON FILLED vs FILL WINDOW (equal weight, day clusters) ===", flush=True)
print("  %-12s %8s %8s %14s %14s %9s" % ("window_min","n_fill","share","net_observed","net_shrunk","t_shrunk"), flush=True)
rows=[]
for W in WIN:
    mk = first_fill <= W
    if mk.sum() < 5: continue
    dsub=[dd for dd,k in zip(days,mk) if k]
    co=cluster_mean_se(list(net_obs[mk]),dsub); cs=cluster_mean_se(list(net_shr[mk]),dsub)
    lab = "until start" if W==10**9 else str(W)
    print("  %-12s %8d %7.3g%% %14.9g %14.9g %9.4g" % (lab, int(mk.sum()), 100*mk.mean(), co.mean, cs.mean, cs.mean/cs.se), flush=True)
    rows.append(dict(window_min=(None if W==10**9 else W), n_filled=int(mk.sum()), fill_share=float(mk.mean()),
                     net_observed=co.mean, net_shrunk=cs.mean, t_shrunk=cs.mean/cs.se))
print(flush=True)
print("  monotone? observed edge falls as the window widens:", [round(r["net_observed"],6) for r in rows], flush=True)
pos=[r for r in rows if r["net_shrunk"]>0]
print("  windows where SHRUNK filled edge is positive:", [(r["window_min"],round(r["net_shrunk"],8)) for r in pos] or "NONE", flush=True)
print("  fill share at which observed filled edge crosses zero:", flush=True)
zc=[(rows[i-1],rows[i]) for i in range(1,len(rows)) if rows[i-1]["net_observed"]>0>=rows[i]["net_observed"]]
print("   ", ("between share %.4g%% and %.4g%%" % (100*zc[0][0]["fill_share"],100*zc[0][1]["fill_share"])) if zc else "observed edge stays positive across all windows", flush=True)
json.dump(dict(curve=rows), open(HERE/"maker_curve.json","w",encoding="utf-8"), indent=1)
print("[out] maker_curve.json", flush=True)
