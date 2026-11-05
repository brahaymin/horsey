#!/usr/bin/env python3
"""Public delayed SPY gamma/regime monitor; no trading signals or dealer-position claims.
Run: /workspace/spy_gamma_venv/bin/python /workspace/spy_gamma_monitor.py
Dependencies: requests, yfinance, pandas, numpy, scipy. JSON on stdout, Markdown on stderr.
Use --offline to replay saved inputs, --asof YYYY-MM-DD for daily metrics only.
A current option chain is NEVER represented as a historical chain. OI is normally prior-day.
"""
import argparse, concurrent.futures as cf, datetime as dt, hashlib, json, math, re, sys
from pathlib import Path
from decimal import Decimal
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests
from scipy.optimize import brentq
import yfinance as yf

NY=ZoneInfo('America/New_York'); UTC=dt.timezone.utc
BASE='https://cdn.cboe.com/api/global/delayed_quotes/'
SYMS=['SPY','SMH','SOXX','QQQ','XLK','NVDA','AVGO','TSM','MSFT','VRT','CEG','ANET','KBWB','^BKX','^SOX','^VIX','^VVIX','^VIX9D','^VIX3M','ES=F','NQ=F','RTY=F']
def key(s): return s.replace('^','_').replace('=','_')
def clean(x):
    if isinstance(x,dict): return {str(k):clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)): return [clean(v) for v in x]
    if isinstance(x,(np.integer,)): return int(x)
    if isinstance(x,(np.floating,float)): return float(x) if math.isfinite(float(x)) else None
    if isinstance(x,(dt.datetime,dt.date,pd.Timestamp)): return x.isoformat()
    return x

def metrics(h,asof):
    if h is None or h.empty:return {'status':'unavailable'}
    h=h[[d<=asof for d in h.index.date]].dropna(subset=['Close']).copy()
    if len(h)<2:return {'status':'unavailable_insufficient_history'}
    # Yahoo raw OHLC commonly contains float32 artifacts. Snap quotes to $0.0001;
    # Decimal averaging then avoids numerical noise and preserves sub-cent index prices.
    c=[Decimal(str(round(float(v),4))) for v in h.Close];last=h.iloc[-1];close=float(c[-1]); prev=h.iloc[-2]
    out={'status':'ok' if h.index[-1].date()==asof else 'stale','session':h.index[-1].date().isoformat(),
         'close':close,'open':round(float(last.Open),4),'high':round(float(last.High),4),'low':round(float(last.Low),4),
         'previous_close':float(c[-2]),'change_dollars':float(c[-1]-c[-2]),'volume':int(last.Volume),
         'returns_pct':{str(n):float((c[-1]/c[-1-n]-1)*100) if len(c)>n else None for n in [1,5,20]},'sma':{}}
    for n in [20,50,200]:
        if len(c)>=n:
            a=sum(c[-n:])/Decimal(n);out['sma'][str(n)]={'value':float(a),'distance_dollars':float(c[-1]-a),'distance_pct':float((c[-1]/a-1)*100),'above':c[-1]>a}
    v=h.Volume.iloc[-21:-1]
    out['volume_average_prior_20']=float(v.mean()) if len(v)==20 else None
    out['volume_vs_prior_20']=float(last.Volume/v.mean()) if len(v)==20 and v.mean()>0 else None
    out['volume_average_including_today_20']=float(h.Volume.iloc[-20:].mean())
    out['volume_vs_prior_day']=float(last.Volume/prev.Volume) if prev.Volume>0 else None
    out['recent_levels']={}
    for n in [1,5,20]:
        p=h.iloc[-n-1:-1];hi=round(float(p.High.max()),4);lo=round(float(p.Low.min()),4)
        out['recent_levels'][str(n)]={'prior_high':hi,'prior_low':lo,'traded_above_high':out['high']>hi,'traded_below_low':out['low']<lo,'swept_low_and_reclaimed_close':out['low']<lo<close,'swept_high_and_rejected_close':out['high']>hi>close}
    return out

def chain_rows(j,now):
    rows=[];excluded={};expired=0
    for o in j['data']['options']:
        m=re.fullmatch(r'(.+?)(\d{6})([CP])(\d{8})',o.get('option',''))
        if not m:continue
        root,ds,cp,k=m.groups()
        if root!='SPY':excluded[root]=excluded.get(root,0)+1;continue
        expiry=dt.datetime.strptime(ds,'%y%m%d').replace(hour=16,minute=15,tzinfo=NY)
        if expiry<=now:expired+=1;continue
        oi=o.get('open_interest');g=o.get('gamma');vol=o.get('volume')
        if oi is None or g is None or vol is None:continue
        if not all(math.isfinite(float(v)) and float(v)>=0 for v in [oi,g,vol]):continue
        rows.append({'contract':o['option'],'expiry':expiry.date().isoformat(),'expiry_at':expiry.isoformat(),'strike':int(k)/1000,'type':cp,'oi':float(oi),'volume':float(vol),'gamma':float(g),'iv':float(o.get('iv') or 0),'delta':o.get('delta'),'dte':(expiry.date()-now.astimezone(NY).date()).days})
    return rows,{'nonstandard_roots_excluded':excluded,'expired_contracts_excluded':expired}

def aggregate(rows,spot):
    a={}
    for o in rows:
        k=o['strike'];z=a.setdefault(k,{'strike':k,'call_oi':0,'put_oi':0,'call_volume':0,'put_volume':0,'call_gamma_oi':0.,'put_gamma_oi':0.,'call_contract_gamma_sum':0.,'put_contract_gamma_sum':0.})
        side='call' if o['type']=='C' else 'put'
        z[side+'_oi']+=int(o['oi']);z[side+'_volume']+=int(o['volume']);z[side+'_gamma_oi']+=o['gamma']*o['oi'];z[side+'_contract_gamma_sum']+=o['gamma']
    for z in a.values():
        z['call_gex']=z['call_gamma_oi']*spot**2;z['put_gex']=-z['put_gamma_oi']*spot**2;z['net_gex']=z['call_gex']+z['put_gex']
        for side in ['call','put']:z[side+'_oi_weighted_gamma']=z[side+'_gamma_oi']/z[side+'_oi'] if z[side+'_oi'] else None
    return sorted(a.values(),key=lambda z:z['strike'])

def flip_model(rows,spot,now,r,q):
    valid=[o for o in rows if o['oi']>0 and o['iv']>0 and math.isfinite(o['iv'])]
    if not valid:return {'status':'unavailable_no_iv'}
    K=np.array([o['strike'] for o in valid]);T=np.array([(dt.datetime.fromisoformat(o['expiry_at'])-now).total_seconds()/(365*86400) for o in valid]);iv=np.array([o['iv'] for o in valid]);oi=np.array([o['oi']*(1 if o['type']=='C' else -1) for o in valid]);sq=iv*np.sqrt(T)
    def f(s):
        d1=(np.log(s/K)+(r-q+iv**2/2)*T)/sq
        gamma=np.exp(-q*T)*np.exp(-d1**2/2)/(math.sqrt(2*math.pi)*s*sq)
        return float(np.sum(gamma*oi)*s*s)
    grid=np.linspace(spot*.7,spot*1.3,601);roots=[]
    for x,y in zip(grid[:-1],grid[1:]):
        a,b=f(x),f(y)
        if a*b<0:
            root=float(brentq(f,x,y));roots.append({'spot':root,'direction':'negative_to_positive' if a<0 else 'positive_to_negative'})
    np_roots=[z['spot'] for z in roots if z['direction']=='negative_to_positive']
    return {'status':'model_estimate','zero_gamma_level':min(np_roots,key=lambda x:abs(x-spot)) if np_roots else None,'roots':roots,'net_gex_at_reference_spot':f(spot),'search_spot_range':[spot*.7,spot*1.3],'contracts_used':len(valid),'contracts_total':len(rows),'oi_used':sum(o['oi'] for o in valid),'oi_total':sum(o['oi'] for o in rows),'rate_assumption':r,'dividend_yield_assumption':q,'time_basis':'ACT/365; expiry 16:15 America/New_York; frozen IV; European Black-Scholes approximation, not observed dealer positioning'}

def scope(rows,spot,now,r,q):
    p=aggregate(rows,spot)
    totals={k:sum(z[k] for z in p) for k in ['call_oi','put_oi','call_volume','put_volume','call_gex','put_gex','net_gex','call_gamma_oi','put_gamma_oi']}
    totals['put_call_oi_ratio']=totals['put_oi']/totals['call_oi'] if totals['call_oi'] else None
    totals['put_call_volume_ratio']=totals['put_volume']/totals['call_volume'] if totals['call_volume'] else None
    totals['regime']='negative_short_gamma_proxy' if totals['net_gex']<0 else 'positive_long_gamma_proxy' if totals['net_gex']>0 else 'neutral'
    cwall=sorted(p,key=lambda z:z['call_gex'],reverse=True)[:5];pwall=sorted(p,key=lambda z:z['put_gex'])[:5]
    cumulative=0.;cross=[];previous=None
    for z in p:
        cumulative+=z['net_gex'];z['cumulative_net_gex']=cumulative
        if previous and previous[1]<0<=cumulative:cross.append({'lower_strike':previous[0],'upper_strike':z['strike']})
        previous=(z['strike'],cumulative)
    return {'contracts':len(rows),'expirations':sorted(set(o['expiry'] for o in rows)),'totals':totals,'call_walls_by_gex':cwall,'put_walls_by_gex':pwall,'call_walls_by_oi':sorted(p,key=lambda z:z['call_oi'],reverse=True)[:5],'put_walls_by_oi':sorted(p,key=lambda z:z['put_oi'],reverse=True)[:5],'node_735':next((z for z in p if z['strike']==735),None),'node_735_contracts':[o for o in rows if o['strike']==735],'zero_gamma_repriced_model':flip_model(rows,spot,now,r,q),'cumulative_strike_crossings_not_a_spot_gamma_flip':cross,'strike_profile':p}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--asof');ap.add_argument('--offline',action='store_true');ap.add_argument('--data-dir',default='/workspace/spy_gamma_data');ap.add_argument('--output',default='/workspace/spy_gamma_snapshot.json');ap.add_argument('--summary',default='/workspace/spy_gamma_summary.md');ap.add_argument('--rate',type=float,default=.04);ap.add_argument('--dividend-yield',type=float,default=.012);ap.add_argument('--headlines-json',default='/workspace/spy_gamma_headlines.json');a=ap.parse_args()
    now=dt.datetime.now(UTC);local=now.astimezone(NY);asof=dt.date.fromisoformat(a.asof) if a.asof else local.date() if local.hour>=16 else local.date()-dt.timedelta(days=1)
    directory=Path(a.data_dir);directory.mkdir(parents=True,exist_ok=True);errors=[];sources=[]
    def fetch_json(url,name):
        p=directory/name
        if a.offline:
            return json.loads(p.read_text()) if p.exists() else None
        try:
            response=requests.get(url,timeout=25);response.raise_for_status();j=response.json();p.write_text(json.dumps(j));sources.append({'url':url,'retrieved_at':dt.datetime.now(UTC).isoformat(),'feed_timestamp':j.get('timestamp'),'sha256':hashlib.sha256(response.content).hexdigest()});return j
        except Exception as e:errors.append({'source':url,'error':str(e)});return None
    def yf_fetch(s):
        p=directory/('yf_'+key(s)+'_history.json');mp=directory/('yf_'+key(s)+'_meta.json');ip=directory/('yf_'+key(s)+'_intraday.json')
        try:
            if a.offline:
                h=pd.read_json(p,orient='table');meta=json.loads(mp.read_text()) if mp.exists() else {};intra=pd.read_json(ip,orient='table') if ip.exists() else None
            else:
                t=yf.Ticker(s);h=t.history(period='2y',auto_adjust=False,timeout=20);meta=dict(t.history_metadata);intra=None
                if h.empty:raise ValueError('empty daily history')
                h.to_json(p,orient='table',date_format='iso');mp.write_text(json.dumps(meta,default=str))
                if s.endswith('=F'):
                    intra=t.history(period='2d',interval='1m',prepost=True,timeout=20);intra.to_json(ip,orient='table',date_format='iso')
            sources.append({'source':'Yahoo Finance via yfinance','symbol':s,'latest_daily_session':str(h.index[-1].date()),'metadata_regular_market_time':str(meta.get('regularMarketTime')),'retrieved_at':now.isoformat() if not a.offline else None})
            return s,h,meta,intra
        except Exception as e:errors.append({'source':'yfinance','symbol':s,'error':str(e)});return s,None,{},None
    with cf.ThreadPoolExecutor(max_workers=5) as ex:ys={v[0]:v[1:] for v in ex.map(yf_fetch,SYMS)}
    # Public feeds may contain partly split-adjusted Yahoo SOXX bars. Cross-check
    # each date against Cboe before normalizing; never divide the entire series twice.
    adjustments=[]
    soxx_hist=fetch_json(BASE+'charts/historical/SOXX.json','charts_historical_SOXX.json')
    if soxx_hist and ys['SOXX'][0] is not None:
        h,meta,intra=ys['SOXX'];h=h.copy();refs={z['date']:float(z['close']) for z in soxx_hist['data']}
        factors=[float(z) for z in h.get('Stock Splits',pd.Series(dtype=float)) if float(z)>1]
        for index,row in h.iterrows():
            ref=refs.get(index.date().isoformat())
            if not ref:continue
            ratio=float(row.Close)/ref
            factor=next((f for f in factors if abs(ratio/f-1)<.001),None)
            if factor:
                for col in ['Open','High','Low','Close','Adj Close']:
                    if col in h:h.loc[index,col]=h.loc[index,col]/factor
                h.loc[index,'Volume']=round(h.loc[index,'Volume']*factor)
                adjustments.append({'symbol':'SOXX','date':str(index.date()),'factor':factor,'reason':'Yahoo bar versus independent split-adjusted Cboe history ratio agrees with reported split'})
        ys['SOXX']=(h,meta,intra)
    # Yahoo can overwrite the date-labelled futures daily bar with the newly
    # reopened session. Replace it with an explicitly reconstructed COMPLETED
    # 18:00-17:00 ET session for daily comparisons; keep new quotes separate.
    futures_recon={}
    for sym in ['ES=F','NQ=F','RTY=F']:
        h,meta,intra=ys[sym]
        if h is None or intra is None or intra.empty:continue
        idx=intra.index.tz_convert(NY)
        end=dt.datetime.combine(asof,dt.time(17),NY);start=end-dt.timedelta(hours=23)
        bars=intra[(idx>=start)&(idx<end)]
        if not bars.empty and bars.index[-1].tz_convert(NY)>=end-dt.timedelta(minutes=3):
            close=float(bars.Close.iloc[-1]);futures_recon[sym]={'close':close,'close_time':str(bars.index[-1]),'high':float(bars.High.max()),'low':float(bars.Low.min()),'volume':int(bars.Volume.sum()),'note':'completed 18:00-17:00 ET session reconstructed from minute bars; volume may omit zero/missing bars; not exchange settlement'}
            h=h.copy();matches=[x for x in h.index if x.date()==asof]
            if matches:
                ix=matches[-1]
                for col,val in [('Open',float(bars.Open.iloc[0])),('High',float(bars.High.max())),('Low',float(bars.Low.min())),('Close',close),('Volume',int(bars.Volume.sum()))]:h.loc[ix,col]=val
            ys[sym]=(h,meta,intra)
    assets={s:metrics(v[0],asof) for s,v in ys.items()};spy=assets['SPY']
    # If a holiday, use the most recent completed SPY session for comparison.
    if spy.get('session') and not a.asof:asof=dt.date.fromisoformat(spy['session'])
    cboe=fetch_json(BASE+'options/SPY.json','cboe_SPY_options.json');alt=fetch_json(BASE+'options/_SPY.json','cboe__SPY_options.json')
    quote=fetch_json(BASE+'quotes/SPY.json','quotes_SPY.json');vol={}
    for s in ['_VIX','_VVIX','_VIX9D','_VIX3M','_SOX','_BKX']:
        j=fetch_json(BASE+'quotes/'+s+'.json','quotes_'+s+'.json')
        if j:vol[s]={'feed_timestamp':j.get('timestamp'),'data':j['data']}
    if quote:spy['cboe_quote']={'feed_timestamp':quote.get('timestamp'),'data':quote['data'],'note':'current_price is delayed last/post-close; last_trade_time may refer only to regular session, so exact AH tick time is not verified'}
    if cboe:
        spy['cboe_chain_underlying']={k:v for k,v in cboe['data'].items() if k!='options'}
    # Compare every asset over identical trading-day windows; no inferred signed flows.
    for s,m in assets.items():
        if m.get('status')=='ok' and spy.get('status')=='ok':m['relative_to_spy_pp']={n:m['returns_pct'][n]-spy['returns_pct'][n] for n in ['1','5','20'] if m['returns_pct'][n] is not None}
    options={'status':'unavailable'}
    if cboe:
        ft=dt.datetime.fromisoformat(cboe['timestamp']).replace(tzinfo=UTC);rows,excluded=chain_rows(cboe,now)
        if (now-ft).total_seconds()>36*3600 or ft.astimezone(NY).date()!=asof:
            options={'status':'stale_or_not_asof','chain_timestamp':cboe['timestamp'],'requested_session':asof.isoformat()}
        else:
            spot=float(cboe['data']['current_price']);dates=sorted(set(o['expiry'] for o in rows));nextday=dates[0] if dates else None
            month=asof.replace(day=1);fridays=[month+dt.timedelta(days=i) for i in range(31) if (month+dt.timedelta(days=i)).month==month.month and (month+dt.timedelta(days=i)).weekday()==4];opex=fridays[2]
            if opex<asof:
                month=(month+dt.timedelta(days=32)).replace(day=1);fridays=[month+dt.timedelta(days=i) for i in range(28) if (month+dt.timedelta(days=i)).weekday()==4];opex=fridays[2]
            groups={'upcoming_45d':[o for o in rows if o['dte']<=45],'upcoming_all':rows,'next_session':[o for o in rows if o['expiry']==nextday],'monthly_opex':[o for o in rows if o['expiry']==opex.isoformat()]}
            options={'status':'ok','chain_timestamp':cboe['timestamp'],'timestamp_timezone_assumed':'UTC for top-level Cboe cache timestamp; naive contract times are America/New_York','gex_spot':spot,'close_spot_sensitivity':spy.get('close') or cboe['data']['close'],'exclusions':excluded,'next_session_expiry':nextday,'monthly_opex_expiry':opex.isoformat(),'units':'USD delta-notional change for 1% underlying move','dealer_assumption':'long call gamma / short put gamma; OI-side proxy only, not actual dealer inventory','formula':'sign * gamma * open_interest * 100 * spot^2 * 0.01','scopes':{k:scope(v,spot,now,a.rate,a.dividend_yield) for k,v in groups.items()}}
            for z in options['scopes'].values():z['net_gex_rescaled_to_regular_close_same_gamma']=z['totals']['net_gex']*((spy.get('close') or cboe['data']['close'])/spot)**2
            if not a.offline:
                try:
                    oc=yf.Ticker('SPY').option_chain(nextday);cross=[]
                    for side,df in [('C',oc.calls),('P',oc.puts)]:
                        for o in rows:
                            if o['expiry']!=nextday:continue
                            if o['type']!=side:continue
                            v=df[df.contractSymbol==o['contract']]
                            if len(v):cross.append({'contract':o['contract'],'strike':o['strike'],'cboe_oi':o['oi'],'yahoo_oi':float(v.iloc[0].openInterest),'cboe_volume':o['volume'],'yahoo_volume':float(v.iloc[0].volume) if pd.notna(v.iloc[0].volume) else None})
                    (directory/'yahoo_option_crosscheck.json').write_text(json.dumps(clean(cross)));options['yahoo_crosscheck']={'matched_contracts':len(cross),'oi_exact_matches':sum(x['cboe_oi']==x['yahoo_oi'] for x in cross),'node_735':[x for x in cross if x['strike']==735],'note':'OI and session volume may differ by snapshot; Yahoo does not supply gamma'}
                except Exception as e:errors.append({'source':'yfinance option crosscheck','error':str(e)})
    futures={}
    for s in ['ES=F','NQ=F','RTY=F']:
        h,meta,intra=ys[s];m=assets[s];entry={'daily':m,'quote':{'price':meta.get('regularMarketPrice'),'timestamp':str(meta.get('regularMarketTime')),'displayed_change':meta.get('fulldayChange'),'displayed_change_pct':meta.get('regularMarketChangePercent'),'implied_quote_reference':meta.get('regularMarketPrice')-meta.get('fulldayChange') if meta.get('regularMarketPrice') is not None and meta.get('fulldayChange') is not None else None,'note':'Displayed Yahoo change/reference is not independently verified exchange settlement'},'completed_session_reconstruction':futures_recon.get(s),'overnight':None}
        # Identify NEW session from 18:00 ET, never relabel the previous session range.
        start=dt.datetime.combine(local.date(),dt.time(18),NY)
        if local.hour<18:start-=dt.timedelta(days=1)
        if intra is not None and not intra.empty:
            idx=intra.index.tz_convert(NY);bars=intra[(idx>=start)&(idx<=local)]
            entry['latest_minute_bar_time']=str(idx[-1]);entry['overnight_session_start']=start.isoformat()
            if not bars.empty:
                price=float(bars.Close.iloc[-1]);base=m.get('close');entry['overnight']={'latest':price,'timestamp':str(bars.index[-1]),'high':float(bars.High.max()),'low':float(bars.Low.min()),'open':float(bars.Open.iloc[0]),'volume':int(bars.Volume.sum()) if bars.Volume.sum()>0 else None,'change_vs_completed_daily_close_pct':(price/base-1)*100 if base else None,'reference_close':base,'reference_type':'Yahoo completed daily close; not certified settlement'}
            else:entry['overnight_status']='unavailable_no_bars_since_18_ET; do not infer Globex bias from stale prior-session data'
        futures[s]=entry
    vx=assets.get('^VIX',{}).get('close');v3=assets.get('^VIX3M',{}).get('close');v9=assets.get('^VIX9D',{}).get('close')
    term={'vix_over_vix3m':vx/v3 if vx and v3 else None,'vix9d_minus_vix':v9-vx if vx and v9 else None,'vix_minus_vix3m':vx-v3 if vx and v3 else None,'regime':'backwardated_implied_volatility_tenors' if vx and v3 and vx>v3 else 'contango_implied_volatility_tenors' if vx and v3 else 'unavailable','note':'spot implied-volatility horizons, NOT a VIX futures curve'}
    headlines=[]
    if Path(a.headlines_json).exists():headlines=[x for x in json.loads(Path(a.headlines_json).read_text()) if x.get('session')==asof.isoformat()]
    result={'schema_version':'1.0','generated_at_utc':now.isoformat(),'requested_completed_session':asof.isoformat(),'replay_mode':a.offline,'spy':spy,'options_gex':options,'volatility':{'term_structure':term,'cboe_crosschecks':vol},'assets':assets,'futures':futures,'verified_headlines':headlines,'headlines_status':'dated manually verified public reporting; no automatic claim of verification' if headlines else 'unavailable; supply dated verified article metadata via --headlines-json','sources':sources,'data_normalizations':adjustments,'errors':errors,'methodology':{'sma':'arithmetic mean of last N unadjusted Close observations through completed session; source quotes snapped to 0.0001 to suppress Yahoo float32 artifacts; no dividends adjustment','returns':'close-to-close; 1, 5, 20 trading observations; split-adjusted Yahoo OHLC, dividends unadjusted','volume':'session shares / mean prior 20 completed sessions, excluding today; share volume is NOT signed fund flow','gamma_flip':'reprice all unexpired gammas at trial spot using frozen IV Black-Scholes; cumulative strike crossing separately named and is not a dealer zero-gamma spot','data_limits':'public delayed data; OI typically prior session and not verified intraday; rounded source gamma; actual dealer position unknown; no level independently validates actual dealer hedging','rate_and_dividend':'explicit configurable model assumptions, NOT measured market inputs'}}
    lines=['# SPY Gamma Structure & Regime Snapshot',f'Generated {now.isoformat()} | completed session {asof} | replay={a.offline}','', '| Symbol | Close | 1D % | 5D % | 20D % | Volume | Vol / prior20 | 200 SMA |','|---|---:|---:|---:|---:|---:|---:|---:|']
    fmt=lambda v,n=4:'NA' if v is None else f'{v:,.{n}f}'
    for s in SYMS:
        m=assets[s];r=m.get('returns_pct',{});lines.append(f"| {s} | {fmt(m.get('close'))} | {fmt(r.get('1'))} | {fmt(r.get('5'))} | {fmt(r.get('20'))} | {fmt(m.get('volume'),0)} | {fmt(m.get('volume_vs_prior_20'))} | {fmt(m.get('sma',{}).get('200',{}).get('value'),6)} |")
    if options.get('status')=='ok':
        lines+=['','| GEX scope | Net GEX ($/1%) | Call wall | Put wall | Model gamma flip |','|---|---:|---:|---:|---:|']
        for k,z in options['scopes'].items():lines.append(f"| {k} | {fmt(z['totals']['net_gex'],2)} | {z['call_walls_by_gex'][0]['strike'] if z['call_walls_by_gex'] else 'NA'} | {z['put_walls_by_gex'][0]['strike'] if z['put_walls_by_gex'] else 'NA'} | {fmt(z['zero_gamma_repriced_model'].get('zero_gamma_level'))} |")
        lines+=['','## $735 strike','| Scope | Call OI | Put OI | Call volume | Put volume | Call gamma x OI | Put gamma x OI | Net GEX $/1% |','|---|---:|---:|---:|---:|---:|---:|---:|']
        for k,z in options['scopes'].items():
            n=z['node_735']
            if n:lines.append('| '+k+' | '+' | '.join(fmt(n.get(v),4 if 'gamma' in v else 2 if v=='net_gex' else 0) for v in ['call_oi','put_oi','call_volume','put_volume','call_gamma_oi','put_gamma_oi','net_gex'])+' |')
    lines+=['','## Methodology and limitations']+[f'- {k}: {v}' for k,v in result['methodology'].items()]
    lines+=['','## Verified dated headlines']+[f"- [{h['title']}]({h['url']}): {h['summary']}" for h in headlines]
    lines+=['','## SPY price / moving averages']
    lines.append(f"- Regular close: {spy.get('close')}; day change: {spy.get('change_dollars')}; volume: {spy.get('volume')}; prior-20 mean: {spy.get('volume_average_prior_20')}; relative volume: {spy.get('volume_vs_prior_20')}")
    if quote:lines.append(f"- Delayed current/post-close quote: {quote['data'].get('current_price')} (cache timestamp {quote.get('timestamp')}; exact post-close tick time not verified)")
    for n,z in spy.get('sma',{}).items():lines.append(f"- SMA {n}: {z['value']}; close distance: ${z['distance_dollars']} / {z['distance_pct']}%")
    lines+=['','## Overnight futures (new session from 18:00 ET)','| Symbol | Quote | Quote time | Displayed change | Displayed change % | Inferred quote reference | New-session low | New-session high | Change vs 16:59 close % |','|---|---:|---|---:|---:|---:|---:|---:|---:|']
    for sym,f in futures.items():
        v=f['quote'];o=f.get('overnight') or {};lines.append(f"| {sym} | {v.get('price')} | {v.get('timestamp')} | {v.get('displayed_change')} | {v.get('displayed_change_pct')} | {v.get('implied_quote_reference')} | {o.get('low')} | {o.get('high')} | {o.get('change_vs_completed_daily_close_pct')} |")
    lines.append('Futures quote references are inferred from Yahoo displayed change, not exchange-certified settlements; minute-range bars may be delayed or partial.')
    text='\n'.join(lines)+'\n';Path(a.summary).write_text(text);payload=json.dumps(clean(result),indent=2,allow_nan=False);Path(a.output).write_text(payload+'\n');print(payload);print(text,file=sys.stderr)
    if spy.get('status')!='ok' or options.get('status')!='ok':return 2
    return 0
if __name__=='__main__':sys.exit(main())
