#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys, time
from collections import defaultdict, Counter
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

GAMMA='https://gamma-api.polymarket.com'; DATA='https://data-api.polymarket.com'
DATE=re.compile(r'\d{4}-\d{2}-\d{2}$'); DOUBLES=('atp-doubles-','wta-doubles-')
EPS=Decimal('0.001'); PAGE=1000; CAP=10000; DELAY=.06

def D(x):
    try: return Decimal(str(x))
    except (InvalidOperation,TypeError,ValueError): return Decimal(0)

def get(base,path,params):
    req=Request(base+path+'?'+urlencode(params),headers={'Accept':'application/json','User-Agent':'probe3b/1'})
    while True:
        try:
            with urlopen(req,timeout=45) as r: return json.loads(r.read())
        except HTTPError as e:
            body=e.read().decode('utf-8','replace')
            if e.code==429: time.sleep(1.5); continue
            raise RuntimeError(f'HTTP {e.code}: {body[:500]}')
        except (URLError,TimeoutError) as e: raise RuntimeError(str(e))

def rows(x,key=None):
    if isinstance(x,list): return [a for a in x if isinstance(a,dict)]
    if isinstance(x,dict):
        for k in ([key] if key else [])+['data','trades','positions','events']:
            if isinstance(x.get(k),list): return [a for a in x[k] if isinstance(a,dict)]
    return []

def paged(params,stats):
    out=[]; off=0
    while True:
        page=rows(get(DATA,'/trades',{**params,'limit':PAGE,'offset':off}),'trades')
        stats['requests']+=1; stats['rows']+=len(page); out+=page
        if len(page)<PAGE: return out,False
        off+=PAGE
        if off>CAP: return out,True
        time.sleep(DELAY)

def positions(user,stats):
    out=[]; off=0
    while True:
        page=rows(get(DATA,'/positions',{'user':user,'limit':PAGE,'offset':off}),'positions')
        stats['position_requests']+=1; out+=page
        if len(page)<PAGE:return out
        off+=PAGE
        if off>CAP: raise RuntimeError('positions offset cap reached')
        time.sleep(DELAY)

def main():
    st=defaultdict(int); st['cap']=False
    ev=rows(get(GAMMA,'/events',{'tag_slug':'tennis','closed':'false','limit':100}),'events')
    markets=[]
    for e in ev:
        for m in rows(e.get('markets')):
            s=str(m.get('slug') or '')
            if DATE.search(s) and not s.startswith(DOUBLES):
                end=str(m.get('endDate') or e.get('endDate') or '')[:10]
                # Date comparison is done against current UTC date minus 90 days.
                if end: markets.append(m)
    markets=markets[:3]
    if len(markets)<3: raise RuntimeError(f'only {len(markets)} qualifying markets')
    # Verify combined user+market query on one wallet/market before proceeding.
    probe_market=str(markets[0].get('conditionId')); probe_rows,probe_cap=paged({'market':probe_market},st)
    if not probe_rows: raise RuntimeError('combined query verification returned no market trades')
    probe_user=str(probe_rows[0].get('proxyWallet')); combined,combined_cap=paged({'user':probe_user,'market':probe_market},st)
    if not combined or any(str(x.get('conditionId'))!=probe_market for x in combined):
        raise RuntimeError('combined user+market query did not narrow to one market')
    wallets=[]; seed={}
    for m in markets:
        c=str(m.get('conditionId')); r,_=paged({'market':c},st); seed[c]=r
        for x in r:
            w=str(x.get('proxyWallet') or '').lower()
            if w and w not in wallets: wallets.append(w)
            if len(wallets)>=20: break
        if len(wallets)>=20: break
    wallets=wallets[:20]
    market_ids={str(m.get('conditionId')) for m in markets}; pair_count=match=0; div=[]; no_trade=[]; raw=[]; position_slug_examples=[]; total_positions=0; slug_matched_positions=0
    for w in wallets:
        pos=positions(w,st)
        total_positions += len(pos)
        for p in pos:
            if len(position_slug_examples)<3: position_slug_examples.append(p.get('slug'))
            slug=str(p.get('slug') or '')
            if not (slug.startswith(('atp-','wta-','itf-')) and DATE.search(slug) and '-doubles-' not in slug): continue
            slug_matched_positions += 1
            c=str(p.get('conditionId') or '')
            pair_count+=1; asset=str(p.get('asset') or ''); opp=str(p.get('oppositeAsset') or '')
            tr,cap=paged({'user':w,'market':c},st); st['cap']=st['cap'] or cap
            if not tr:
                no_trade.append({'position':p,'trades':[]})
                if len(raw)<3: raw.append({'position':p,'trades':[]})
            calc=Decimal(0)
            for t in tr:
                sz=D(t.get('size')); side=str(t.get('side') or '').upper(); a=str(t.get('asset') or '')
                if a==asset: calc += sz if side=='BUY' else -sz if side=='SELL' else 0
                elif a==opp: calc += -sz if side=='BUY' else sz if side=='SELL' else 0
            diff=calc-D(p.get('size')); mag=abs(diff)
            if mag<=EPS: match+=1
            else: div.append(mag)
    buckets=Counter()
    for x in div:
        if x<=Decimal('.001'): buckets['to_0.001']+=1
        elif x<=Decimal('.01'): buckets['to_0.01']+=1
        elif x<=Decimal('.1'): buckets['to_0.1']+=1
        elif x<=Decimal('1'): buckets['to_1']+=1
        elif x<=Decimal('10'): buckets['to_10']+=1
        else: buckets['over_10']+=1
    print(f'wallets={len(wallets)}'); print(f'pairs_checked={pair_count}'); print(f'matching_at_0.001={match}')
    for k in ('to_0.001','to_0.01','to_0.1','to_1','to_10','over_10'): print(f'{k}={buckets[k]}')
    print(f'divergent_pairs_without_output_cap={len(div)}')
    print(f'positions_without_market_trades={len(no_trade)}')
    print('raw_examples='+json.dumps(raw[:3],ensure_ascii=False,separators=(',',':')))
    if pair_count==0:
        print(f'total_positions={total_positions}')
        print(f'slug_matched_positions={slug_matched_positions}')
        print('position_slug_examples='+json.dumps(position_slug_examples,ensure_ascii=False,separators=(',',':')))
    print(f'trade_output_cap_reached={"yes" if st["cap"] else "no"}')
    print('толкование')
    print('Сравнение ограничено теннисными матчевыми рынками по slug самой позиции; сделки запрашиваются отдельным запросом по каждой паре кошелёк–рынок. Допуск 0.001 учитывает округление размера позиции.')
if __name__=='__main__':
    try: main()
    except Exception as e: print(f'ERROR: {e}',file=sys.stderr); raise
