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

# ------------------------------------------------------------------ Daily Gamma Tracker fields (Nov 12 2026)
# Data only, no bias wording. Every value is computed from data this script already fetched
# (Cboe delayed chain, Yahoo daily + 1-minute bars) or from the local archive time series.
# Anything that cannot be derived is None, never estimated from thin air.
CT=ZoneInfo('America/Chicago')
HVL_LABEL='model estimate (cumulative strike net-GEX zero-crossing), not vendor HVL'
VP_LABEL='approximation from 1-minute bars, not a tick profile'
FUT_LABEL='Yahoo delayed ~10 min'
PREV_GEX_NOTE='expiry roll and OI refresh make prior-session and current net GEX not strictly comparable'
PC_NOTE='Cboe delayed chain; volume is session-to-date, open interest is from the prior session'
# NYSE full-day closures (early closes still count as sessions). Unknown year -> None.
NYSE_HOLIDAYS={
    2026:{'2026-01-01','2026-01-19','2026-02-16','2026-04-03','2026-05-25','2026-06-19','2026-07-03','2026-09-07','2026-11-26','2026-12-25'},
    2027:{'2027-01-01','2027-01-18','2027-02-15','2027-03-26','2027-05-31','2027-06-18','2027-07-05','2027-09-06','2027-11-25','2027-12-24'},
}

def trading_days_left_ytd(d):
    """NYSE sessions from d through Dec 31 of d's year, including d when d is a session."""
    hol=NYSE_HOLIDAYS.get(d.year)
    if hol is None:return None
    n=0;x=d
    while x.year==d.year:
        if x.weekday()<5 and x.isoformat() not in hol:n+=1
        x+=dt.timedelta(days=1)
    return n

def is_nyse_session(d):
    hol=NYSE_HOLIDAYS.get(d.year)
    return None if hol is None else (d.weekday()<5 and d.isoformat() not in hol)

def gamma_split(profile):
    if not profile:return None
    c=sum(abs(z.get('call_gex') or 0.) for z in profile);p=sum(abs(z.get('put_gex') or 0.) for z in profile);t=c+p
    if t<=0:return {'call_gex_abs':c,'put_gex_abs':p,'call_share':None,'put_share':None,'dominant_side':None}
    return {'call_gex_abs':c,'put_gex_abs':p,'call_share':c/t,'put_share':p/t,'dominant_side':'call' if c>p else 'put' if p>c else 'balanced'}

def pc_ratios(totals):
    if not totals:return None
    return {'put_call_volume':totals.get('put_call_volume_ratio'),'put_call_oi':totals.get('put_call_oi_ratio'),'note':PC_NOTE}

def hvl_model(profile,spot=None):
    """Zero crossings of cumulative net GEX summed from the lowest strike upward, linearly
    interpolated between adjacent strikes. Level = crossing nearest spot (or the first one)."""
    if not profile:return {'status':'unavailable','level':None,'crossings':[],'label':HVL_LABEL}
    rows=sorted(profile,key=lambda z:z['strike']);cum=0.;prev=None;cross=[]
    for z in rows:
        cum+=float(z.get('net_gex') or 0.)
        if prev is not None:
            k0,c0=prev
            if (c0<0<=cum) or (c0>0>=cum):
                lvl=k0+(0-c0)/(cum-c0)*(z['strike']-k0) if cum!=c0 else z['strike']
                cross.append({'level':lvl,'direction':'negative_to_positive' if c0<0 else 'positive_to_negative','lower_strike':k0,'upper_strike':z['strike']})
        prev=(z['strike'],cum)
    level=None
    if cross:level=(min(cross,key=lambda c:abs(c['level']-spot)) if spot else cross[0])['level']
    return {'status':'model_estimate' if cross else 'no_zero_crossing','level':level,'crossings':cross,'label':HVL_LABEL}

def _ny_index(df):
    idx=df.index
    if getattr(idx,'tz',None) is None:idx=idx.tz_localize(UTC)
    return idx.tz_convert(NY)

def volume_profile(intra,session,bin_size=0.25,value_area=0.70):
    """Regular hours (8:30 AM-3:00 PM CT) volume profile for `session` from 1-minute bars; each
    bar's volume is placed at its typical price (H+L+C)/3 rounded to the nearest $0.25 bin."""
    base={'session':session.isoformat() if session else None,'bin_size':bin_size,'label':VP_LABEL,'poc':None,'vah':None,'val':None}
    if intra is None or getattr(intra,'empty',True) or session is None:return dict(base,status='unavailable_no_bars')
    idx=_ny_index(intra);t=idx.time
    m=(idx.date==session)&(t>=dt.time(9,30))&(t<dt.time(16,0))
    bars=intra[m]
    if bars.empty:return dict(base,status='unavailable_no_session_bars')
    bins={}
    for h,l,c,v in zip(bars.High,bars.Low,bars.Close,bars.Volume):
        if not all(map(math.isfinite,[float(h),float(l),float(c),float(v)])) or float(v)<=0:continue
        b=round(round(((float(h)+float(l)+float(c))/3)/bin_size)*bin_size,4);bins[b]=bins.get(b,0.)+float(v)
    if not bins:return dict(base,status='unavailable_zero_volume')
    lo,hi=min(bins),max(bins);n=int(round((hi-lo)/bin_size))+1
    grid=[round(lo+i*bin_size,4) for i in range(n)];vols=[bins.get(g,0.) for g in grid]
    total=sum(vols);ip=max(range(n),key=lambda i:(vols[i],-abs(i-n/2)))
    a=b=ip;acc=vols[ip]
    while acc<value_area*total and (a>0 or b<n-1):
        up=vols[b+1] if b<n-1 else -1;dn=vols[a-1] if a>0 else -1
        if up>=dn:b+=1;acc+=vols[b]
        else:a-=1;acc+=vols[a]
    return dict(base,status='ok',poc=grid[ip],vah=grid[b],val=grid[a],total_volume=total,bars_used=int(len(bars)),value_area_volume_share=acc/total if total else None)

def prev_net_gex(history_path,tracker_date):
    """45D and 0DTE net GEX from the latest post-close row dated before tracker_date."""
    out={'net_gex_45d':None,'net_gex_0dte':None,'source_timestamp_ct':None,'note':PREV_GEX_NOTE}
    p=Path(history_path) if history_path else None
    if not p or not p.exists():return dict(out,status='unavailable_no_history')
    best=None
    for line in p.read_text().splitlines():
        try:r=json.loads(line)
        except Exception:continue
        ts=str(r.get('timestamp_ct') or '')
        if r.get('run_tag')!='post-close' or ts[:10]>=tracker_date.isoformat():continue
        if best is None or ts>str(best.get('timestamp_ct')):best=r
    if not best:return dict(out,status='unavailable_no_prior_post_close')
    return dict(out,status='ok',net_gex_45d=best.get('net_gex_45d_dollars'),net_gex_0dte=best.get('net_gex_0dte_dollars'),source_timestamp_ct=best.get('timestamp_ct'),source_session=best.get('session_date'))

def overnight_futures(intra,tracker_date,cutoff_ct=dt.time(8,20),now=None):
    """ref = price at the prior 3:00 PM CT cash close; low/time since then; price at 8:20 CT."""
    out={'label':FUT_LABEL,'ref':None,'ref_time_ct':None,'high':None,'high_time_ct':None,'low':None,'low_time_ct':None,'p0820':None,'p0820_time_ct':None,'gap_dollars':None,'gap_pct':None,'pct_retraced':None,'classification':None}
    if intra is None or getattr(intra,'empty',True):return dict(out,status='unavailable_no_bars')
    idx=_ny_index(intra);df=intra.copy();df.index=idx;df=df.dropna(subset=['Close'])
    # prior cash close: last bar starting before 16:00 ET on the latest earlier date with such a bar
    prior=df[(df.index.date<tracker_date)&(df.index.time<dt.time(16,0))]
    if prior.empty:return dict(out,status='unavailable_no_prior_close_bar')
    pdate=prior.index[-1].date();pb=prior[prior.index.date==pdate]
    ref_bar=pb.iloc[-1];ref_t=pb.index[-1]
    if ref_t.time()<dt.time(15,50):return dict(out,status='unavailable_prior_close_bar_missing')
    ref=float(ref_bar.Close)
    cutoff=dt.datetime.combine(tracker_date,cutoff_ct,CT).astimezone(NY)
    end=min(cutoff,now.astimezone(NY)) if now else cutoff
    win=df[(df.index>ref_t)&(df.index<end)]
    if win.empty:return dict(out,ref=ref,ref_time_ct=ref_t.astimezone(CT).isoformat(),status='unavailable_no_overnight_bars')
    li=win.Low.astype(float).idxmin();low=float(win.Low.loc[li]);hi_i=win.High.astype(float).idxmax();last=win.iloc[-1];p=float(last.Close)
    o=dict(out,ref=ref,ref_time_ct=(ref_t+pd.Timedelta(minutes=1)).astimezone(CT).isoformat(),low=low,low_time_ct=li.astimezone(CT).isoformat(),high=float(win.High.loc[hi_i]),high_time_ct=hi_i.astimezone(CT).isoformat(),p0820=p,
           p0820_time_ct=(win.index[-1]+pd.Timedelta(minutes=1)).astimezone(CT).isoformat(),gap_dollars=p-ref,gap_pct=(p/ref-1)*100,
           status='ok' if end>=cutoff else 'partial_before_0820')
    if low<ref:o['pct_retraced']=(p-low)/(ref-low)
    o['classification']=classify_futures(ref,low,p)
    return o

def classify_futures(ref,low,p0820):
    if ref is None or low is None or p0820 is None:return None
    if (ref-low)/ref<=0.0025 or p0820>=ref:return 'flat_or_green'
    return 'dipped_recovering' if (p0820-low)/(ref-low)>=0.5 else 'dipped_not_recovering'

def regime_from_gex(net):
    if net is None:return None
    return 'neutral' if abs(net)<1e9 else 'short' if net<0 else 'long'

TRACKER_COLUMNS=['date','close','premarket_gap_pct','net_gex_45d','prev_net_gex_45d','flip_45d','hvl_45d','call_wall_45d','put_wall_45d','pc_vol','pc_oi','poc','vah','val','vix','futures_bias_class','regime','notes']

def build_gamma_tracker(options,spy,assets,ys,now,history_path,profile_session):
    D=now.astimezone(CT).date();sc=((options or {}).get('scopes') or {}) if (options or {}).get('status')=='ok' else {}
    for k,z in sc.items():
        z['gamma_split']=gamma_split(z.get('strike_profile'));z['pc_ratios']=pc_ratios(z.get('totals'))
    spot=(options or {}).get('gex_spot')
    hvl={k:hvl_model(sc[s].get('strike_profile'),spot) if s in sc else {'status':'unavailable','level':None,'crossings':[],'label':HVL_LABEL} for k,s in [('0dte','next_session'),('45d','upcoming_45d')]}
    for k,s in [('0dte','next_session'),('45d','upcoming_45d')]:
        if s in sc:sc[s]['hvl_model']=hvl[k]
    flip={k:(sc[s].get('zero_gamma_repriced_model') or {}).get('zero_gamma_level') if s in sc else None for k,s in [('0dte','next_session'),('45d','upcoming_45d')]}
    spy_intra=ys.get('SPY',(None,None,None))[2]
    vp=volume_profile(spy_intra,profile_session)
    prev=prev_net_gex(history_path,D)
    fut={s:overnight_futures(ys.get(s,(None,None,None))[2],D,now=now) for s in ['ES=F','NQ=F']}
    ref=spy.get('regular_close',spy.get('close'));cur=((spy.get('cboe_quote') or {}).get('data') or {}).get('current_price')
    gap=(float(cur)/ref-1)*100 if cur and ref else None
    s45=sc.get('upcoming_45d') or {};t45=s45.get('totals') or {}
    net45=t45.get('net_gex')
    fb=(fut['ES=F'].get('classification') or fut['NQ=F'].get('classification'))
    row={'date':D.isoformat(),'close':None,'premarket_gap_pct':gap,'net_gex_45d':net45,'prev_net_gex_45d':prev.get('net_gex_45d'),
         'flip_45d':flip['45d'],'hvl_45d':hvl['45d']['level'],'call_wall_45d':(s45.get('call_walls_by_gex') or [{}])[0].get('strike'),
         'put_wall_45d':(s45.get('put_walls_by_gex') or [{}])[0].get('strike'),'pc_vol':t45.get('put_call_volume_ratio'),'pc_oi':t45.get('put_call_oi_ratio'),
         'poc':vp.get('poc'),'vah':vp.get('vah'),'val':vp.get('val'),'vix':(assets.get('^VIX') or {}).get('close'),
         'futures_bias_class':fb,'regime':regime_from_gex(net45),'notes':None}
    return {'tracker_date':D.isoformat(),'is_nyse_session':is_nyse_session(D),'trading_days_left_ytd':trading_days_left_ytd(D),
            'premarket_gap_pct':gap,'premarket_gap_basis':{'reference_close':ref,'delayed_quote':cur},
            'gamma_split':{k:sc[s].get('gamma_split') if s in sc else None for k,s in [('0dte','next_session'),('45d','upcoming_45d')]},
            'pc_ratios':{k:sc[s].get('pc_ratios') if s in sc else None for k,s in [('0dte','next_session'),('45d','upcoming_45d'),('all','upcoming_all')]},
            'hvl_model':hvl,'zero_gamma_repriced':flip,'volume_profile':vp,'prev_net_gex':prev,'overnight_futures':fut,
            'regime_45d':row['regime'],'regime_rule':'neutral when |45D net GEX| < $1B, otherwise short or long by sign',
            'tracker_columns':TRACKER_COLUMNS,'tracker_row':row,
            'not_derivable':{}}

# ------------------------------------------------------------------ Intraday feed for the RTH watcher (Nov 12 2026, CIO D1-D5)
RVOL_SESSIONS=20      # N prior RTH sessions for slot averages
SPIKE_CAP_MULT=8.0    # each 5m bar volume capped at 8x that slot's median over the N prior sessions
TOD_RVOL_LABEL='time-of-day RVOL: cumulative RTH volume through the last completed 5m bar / average cumulative volume through the same slot over the prior 20 sessions; spike prints capped at 8x slot median'
GRID_LABEL='net GEX repriced across spot (frozen IV Black-Scholes, prior-session OI); model estimate, not observed dealer inventory'
ES_SPY_LABEL='estimate: ES level x (SPY close / ES price) at the prior 3:00 PM CT cash close; basis drift overnight not modelled'

def spy_5m_feed(df5,now,n=RVOL_SESSIONS,cap_mult=SPIKE_CAP_MULT):
    out={'label':TOD_RVOL_LABEL,'n_sessions_target':n,'spike_cap':f'{cap_mult:g}x slot median of the prior sessions','tod_rvol':None,'bar_rvol':None,
         'last_completed_bar_ct':None,'cum_volume_today':None,'avg_cum_volume_same_slot':None,'sessions_used':[],'slot_curves':[],'today_bars':[]}
    if df5 is None or getattr(df5,'empty',True):return dict(out,status='unavailable_no_5m_bars')
    idx=_ny_index(df5);df=df5.copy();df.index=idx;df=df.dropna(subset=['Close'])
    df=df[(df.index.time>=dt.time(9,30))&(df.index.time<dt.time(16,0))]
    if df.empty:return dict(out,status='unavailable_no_rth_bars')
    nowny=now.astimezone(NY);today=nowny.date()
    df=df[df.index+pd.Timedelta(minutes=5)<=nowny]  # completed bars only
    slot=lambda ts:ts.astimezone(CT).strftime('%H:%M')
    days=sorted(set(df.index.date));prior=[d for d in days if d<today][-n:]
    if not prior:return dict(out,status='unavailable_no_prior_sessions')
    per={d:{slot(ts):float(v) for ts,v in zip(df.index[df.index.date==d],df.Volume[df.index.date==d])} for d in prior}
    slots=sorted(set(k for d in prior for k in per[d]))
    med={k:float(np.median([per[d][k] for d in prior if k in per[d]])) for k in slots}
    cap=lambda k,v:min(v,cap_mult*med[k]) if med.get(k) else v
    avg={};avgcum={};cums={d:0. for d in prior}
    for k in slots:
        vals=[cap(k,per[d][k]) for d in prior if k in per[d]];avg[k]=float(np.mean(vals))
        for d in prior:
            if k in per[d]:cums[d]+=cap(k,per[d][k])
        avgcum[k]=float(np.mean([cums[d] for d in prior if k in per[d]]))
    out.update(status='ok',sessions_used=[d.isoformat() for d in prior],
               slot_curves=[{'slot_ct':k,'avg_volume':avg[k],'avg_cum_volume':avgcum[k]} for k in slots])
    tb=df[df.index.date==today]
    if tb.empty:return dict(out,status='ok_no_completed_bars_today')
    cum=0.;bars_out=[]
    for ts,r in tb.iterrows():
        k=slot(ts);v=float(r.Volume);vc=cap(k,v);cum+=vc
        bars_out.append({'t_ct':ts.astimezone(CT).strftime('%H:%M'),'o':float(r.Open),'h':float(r.High),'l':float(r.Low),'c':float(r.Close),'v':v,'v_capped':vc,'spike_capped':vc<v})
    k=slot(tb.index[-1]);last=bars_out[-1]
    out.update(today_bars=bars_out,last_completed_bar_ct=tb.index[-1].astimezone(CT).isoformat(),cum_volume_today=cum,
               avg_cum_volume_same_slot=avgcum.get(k),tod_rvol=cum/avgcum[k] if avgcum.get(k) else None,
               bar_rvol=last['v_capped']/avg[k] if avg.get(k) else None)
    return out

def gex_by_spot(rows,chain_spot,now,r,q,center=None,pct=0.02,step=1.0,live_spot=None):
    valid=[o for o in rows if o['oi']>0 and o['iv']>0 and math.isfinite(o['iv'])]
    base={'label':GRID_LABEL,'chain_spot':chain_spot,'center_spot':center or chain_spot,'live_spot':live_spot,'grid':[],'net_gex_at_live_spot':None,'net_gex_at_chain_spot':None}
    if not valid:return dict(base,status='unavailable_no_iv')
    K=np.array([o['strike'] for o in valid]);T=np.array([max((dt.datetime.fromisoformat(o['expiry_at'])-now).total_seconds(),60)/(365*86400) for o in valid])
    iv=np.array([o['iv'] for o in valid]);oi=np.array([o['oi']*(1 if o['type']=='C' else -1) for o in valid]);sq=iv*np.sqrt(T)
    def f(s):
        d1=(np.log(s/K)+(r-q+iv**2/2)*T)/sq
        return float(np.sum(np.exp(-q*T)*np.exp(-d1**2/2)/(math.sqrt(2*math.pi)*s*sq)*oi)*s*s)
    c=center or chain_spot;lo=math.floor(c*(1-pct));hi=math.ceil(c*(1+pct))
    grid=[{'spot':float(x),'net_gex':f(float(x))} for x in np.arange(lo,hi+step/2,step)]
    return dict(base,status='model_estimate',grid=grid,net_gex_at_chain_spot=f(chain_spot),net_gex_at_live_spot=f(live_spot) if live_spot else None,contracts_used=len(valid))

def es_overnight_spy_terms(es,spy_hist):
    out={'label':ES_SPY_LABEL,'ratio_spy_per_es':None,'reference_time_ct':(es or {}).get('ref_time_ct'),'es_high':None,'es_low':None,'spy_equiv_high':None,'spy_equiv_low':None,'spy_equiv_p0820':None,'spy_reference_close':None}
    if not es or es.get('ref') is None or spy_hist is None or getattr(spy_hist,'empty',True):return dict(out,status='unavailable')
    d=dt.datetime.fromisoformat(es['ref_time_ct']).date();h=spy_hist[[x==d for x in spy_hist.index.date]]
    if h.empty:return dict(out,status='unavailable_no_spy_close_for_reference_date')
    sc=float(h.Close.iloc[-1]);ratio=sc/es['ref'];cv=lambda v:v*ratio if v is not None else None
    return dict(out,status='estimate',ratio_spy_per_es=ratio,spy_reference_close=sc,es_high=es.get('high'),es_low=es.get('low'),
                high_time_ct=es.get('high_time_ct'),low_time_ct=es.get('low_time_ct'),spy_equiv_high=cv(es.get('high')),spy_equiv_low=cv(es.get('low')),spy_equiv_p0820=cv(es.get('p0820')))

def tracker_md_lines(g,fmt):
    if not isinstance(g,dict) or 'tracker_row' not in g:return ['','## Daily gamma tracker fields','- unavailable']
    r=g['tracker_row'];vp=g.get('volume_profile') or {};h=g.get('hvl_model') or {};pv=g.get('prev_net_gex') or {}
    L=['','## Daily gamma tracker fields (data only)',f"- Trading days left in {r['date'][:4]} (including today): {g.get('trading_days_left_ytd')}",
       f"- Pre-market gap vs prior close: {fmt(g.get('premarket_gap_pct'),3)}%",
       '','| Scope | Call gamma share | Put gamma share | Dominant | P/C volume | P/C OI | Model gamma flip | HVL model |','|---|---:|---:|---|---:|---:|---:|---:|']
    for k in ['0dte','45d']:
        s=(g.get('gamma_split') or {}).get(k) or {};p=(g.get('pc_ratios') or {}).get(k) or {}
        L.append(f"| {k.upper()} | {fmt(s.get('call_share'),3)} | {fmt(s.get('put_share'),3)} | {s.get('dominant_side') or 'NA'} | {fmt(p.get('put_call_volume'),3)} | {fmt(p.get('put_call_oi'),3)} | {fmt((g.get('zero_gamma_repriced') or {}).get(k),2)} | {fmt((h.get(k) or {}).get('level'),2)} |")
    L+=[f"- HVL: {HVL_LABEL}. P/C: {PC_NOTE}.",
        f"- Prior-session volume profile ({vp.get('session')}): POC {fmt(vp.get('poc'),2)}, VAH {fmt(vp.get('vah'),2)}, VAL {fmt(vp.get('val'),2)} ({VP_LABEL})",
        f"- Prior post-close net GEX: 45D {fmt(pv.get('net_gex_45d'),0)}, 0DTE {fmt(pv.get('net_gex_0dte'),0)} ({pv.get('source_timestamp_ct')}; {PREV_GEX_NOTE})",
        '','| Futures | Ref (3:00 PM CT) | Overnight low | Low time | 8:20 CT | Gap $ | Gap % | Retraced | Class |','|---|---:|---:|---|---:|---:|---:|---:|---|']
    for s,o in (g.get('overnight_futures') or {}).items():
        L.append(f"| {s} | {fmt(o.get('ref'),2)} | {fmt(o.get('low'),2)} | {o.get('low_time_ct') or 'NA'} | {fmt(o.get('p0820'),2)} | {fmt(o.get('gap_dollars'),2)} | {fmt(o.get('gap_pct'),3)} | {fmt(o.get('pct_retraced'),3)} | {o.get('classification') or 'NA'} |")
    L+=[f"- Futures: {FUT_LABEL}. Regime rule: {g.get('regime_rule')}; 45D regime: {g.get('regime_45d')}"]
    return L

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--asof');ap.add_argument('--offline',action='store_true');ap.add_argument('--data-dir',default='/workspace/spy_gamma_data');ap.add_argument('--output',default='/workspace/spy_gamma_snapshot.json');ap.add_argument('--summary',default='/workspace/spy_gamma_summary.md');ap.add_argument('--rate',type=float,default=.04);ap.add_argument('--dividend-yield',type=float,default=.012);ap.add_argument('--headlines-json',default='/workspace/spy_gamma_headlines.json');ap.add_argument('--history-jsonl',default='/workspace/horsey/history/timeseries.jsonl');a=ap.parse_args()
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
                    intra=t.history(period='5d',interval='1m',prepost=True,timeout=20);intra.to_json(ip,orient='table',date_format='iso')
                elif s=='SPY':
                    try:
                        b5=t.history(period='60d',interval='5m',prepost=False,timeout=20);b5.to_json(directory/'yf_SPY_5m.json',orient='table',date_format='iso')
                    except Exception as e:errors.append({'source':'yfinance 5m','symbol':s,'error':str(e)})
                    # regular-hours 1-minute bars for the prior-session volume profile (tracker POC/VAH/VAL)
                    try:
                        intra=t.history(period='5d',interval='1m',prepost=False,timeout=20);intra.to_json(ip,orient='table',date_format='iso')
                    except Exception as e:errors.append({'source':'yfinance intraday','symbol':s,'error':str(e)});intra=None
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
    # LIVE overlay (only when not pinned by --asof and not --offline replay): if Yahoo has a fresher
    # session row (today's partial daily bar, or a regularMarketPrice stamped today) than the completed
    # session, recompute metrics through today so close/1D%/SMA distances are live intraday. The prior
    # completed close is preserved as regular_close/completed_session. Futures keep their own logic.
    live_session=None
    today_ny=local.date();session_open=local.time()<dt.time(16,0)
    live_mode=(not a.offline) and (not a.asof or (asof>=today_ny and session_open))
    if live_mode:
        for s_,(h_,meta_,intra_) in list(ys.items()):
            if s_.endswith('=F') or h_ is None or h_.empty:continue
            try:
                hh=h_.dropna(subset=['Close']).copy();tz=hh.index.tz
                rmp=meta_.get('regularMarketPrice');rmt=meta_.get('regularMarketTime')
                qts=pd.Timestamp(rmt) if rmt not in (None,'None','') else None
                if qts is not None and qts.tzinfo is None:qts=qts.tz_localize(UTC)
                qd=qts.tz_convert(NY).date() if qts is not None else None
                if qd==today_ny and rmp and session_open:
                    if hh.index[-1].date()==qd:hh.loc[hh.index[-1],'Close']=float(rmp)
                    elif hh.index[-1].date()<qd:
                        ix=pd.Timestamp(dt.datetime.combine(qd,dt.time(0)));ix=ix.tz_localize(tz) if tz is not None else ix
                        hh.loc[ix,['Open','High','Low','Close','Volume']]=[float(rmp)]*4+[0];hh=hh.sort_index()
                if not session_open or hh.index[-1].date()!=today_ny:continue
                hc=hh[[d<today_ny for d in hh.index.date]]
                if len(hc)<2:continue
                comp=metrics(hc,hc.index[-1].date());lm=metrics(hh,today_ny)
                if lm.get('status')!='ok':continue
                lm.update({'live':True,'quote_session':today_ny.isoformat(),'intraday_quote':lm.get('close'),'quote_time':str(rmt) if rmt else None,
                           'regular_close':comp.get('close'),'completed_session':comp.get('session'),'completed':comp,
                           'volume_partial_session':True,'note_live':'close/returns/SMA distances use live delayed intraday quote vs prior completed close; volume is partial-session (volume_average_prior_20 excludes today)'})
                if s_=='SPY' and spy.get('cboe_quote'):lm['cboe_quote']=spy['cboe_quote']
                assets[s_]=lm;live_session=today_ny
            except Exception as e:errors.append({'source':'live_overlay','symbol':s_,'error':str(e)})
        spy=assets['SPY']
        if live_session and spy.get('live') and not a.asof:asof=dt.date.fromisoformat(spy['completed_session'])
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
        options_asof=live_session or asof
        if (now-ft).total_seconds()>36*3600 or ft.astimezone(NY).date()!=options_asof:
            options={'status':'stale_or_not_asof','chain_timestamp':cboe['timestamp'],'requested_session':options_asof.isoformat()}
        else:
            spot=float(cboe['data']['current_price']);dates=sorted(set(o['expiry'] for o in rows));nextday=dates[0] if dates else None
            month=options_asof.replace(day=1);fridays=[month+dt.timedelta(days=i) for i in range(31) if (month+dt.timedelta(days=i)).month==month.month and (month+dt.timedelta(days=i)).weekday()==4];opex=fridays[2]
            if opex<options_asof:
                month=(month+dt.timedelta(days=32)).replace(day=1);fridays=[month+dt.timedelta(days=i) for i in range(28) if (month+dt.timedelta(days=i)).weekday()==4];opex=fridays[2]
            groups={'upcoming_45d':[o for o in rows if o['dte']<=45],'upcoming_all':rows,'next_session':[o for o in rows if o['expiry']==nextday],'monthly_opex':[o for o in rows if o['expiry']==opex.isoformat()]}
            options={'status':'ok','chain_timestamp':cboe['timestamp'],'timestamp_timezone_assumed':'UTC for top-level Cboe cache timestamp; naive contract times are America/New_York','gex_spot':spot,'close_spot_sensitivity':spy.get('close') or cboe['data']['close'],'exclusions':excluded,'next_session_expiry':nextday,'monthly_opex_expiry':opex.isoformat(),'units':'USD delta-notional change for 1% underlying move','dealer_assumption':'long call gamma / short put gamma; OI-side proxy only, not actual dealer inventory','formula':'sign * gamma * open_interest * 100 * spot^2 * 0.01','scopes':{k:scope(v,spot,now,a.rate,a.dividend_yield) for k,v in groups.items()}}
            live_spot=spy.get('close') if spy.get('live') else float(cboe['data'].get('current_price') or spot)
            for k,v in groups.items():
                options['scopes'][k]['reference_spot']=spot;options['scopes'][k]['chain_timestamp']=cboe['timestamp']
                try:options['scopes'][k]['gex_by_spot']=gex_by_spot(v,spot,now,a.rate,a.dividend_yield,center=live_spot,live_spot=live_spot)
                except Exception as e:errors.append({'source':'gex_by_spot','scope':k,'error':str(e)})
            for z in options['scopes'].values():z['net_gex_rescaled_to_regular_close_same_gamma']=z['totals']['net_gex']*((spy.get('regular_close') or spy.get('close') or cboe['data']['close'])/spot)**2
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
    try:gamma_tracker=build_gamma_tracker(options,spy,assets,ys,now,a.history_jsonl,asof)
    except Exception as e:errors.append({'source':'gamma_tracker','error':str(e)});gamma_tracker={'status':'error','error':str(e)}
    intraday_feed={'generated_at_utc':now.isoformat()}
    try:
        p5=directory/'yf_SPY_5m.json';df5=pd.read_json(p5,orient='table') if p5.exists() else None
        intraday_feed['spy_5m']=spy_5m_feed(df5,now)
    except Exception as e:errors.append({'source':'spy_5m_feed','error':str(e)});intraday_feed['spy_5m']={'status':'error'}
    sc_=(options.get('scopes') or {}) if options.get('status')=='ok' else {}
    intraday_feed['gex_by_spot']={k:dict(sc_[k].get('gex_by_spot') or {},chain_timestamp=sc_[k].get('chain_timestamp')) for k in sc_}
    intraday_feed['volume_profile_prior_session']=(gamma_tracker or {}).get('volume_profile')
    try:intraday_feed['es_overnight_spy_terms']=es_overnight_spy_terms(((gamma_tracker or {}).get('overnight_futures') or {}).get('ES=F'),ys.get('SPY',(None,))[0])
    except Exception as e:errors.append({'source':'es_overnight_spy_terms','error':str(e)});intraday_feed['es_overnight_spy_terms']={'status':'error'}
    vx=assets.get('^VIX',{}).get('close');v3=assets.get('^VIX3M',{}).get('close');v9=assets.get('^VIX9D',{}).get('close')
    term={'vix_over_vix3m':vx/v3 if vx and v3 else None,'vix9d_minus_vix':v9-vx if vx and v9 else None,'vix_minus_vix3m':vx-v3 if vx and v3 else None,'regime':'backwardated_implied_volatility_tenors' if vx and v3 and vx>v3 else 'contango_implied_volatility_tenors' if vx and v3 else 'unavailable','note':'spot implied-volatility horizons, NOT a VIX futures curve'}
    headlines=[]
    if Path(a.headlines_json).exists():
        wanted={asof.isoformat()}
        if live_session: wanted.add(live_session.isoformat())
        headlines=[x for x in json.loads(Path(a.headlines_json).read_text()) if x.get('session') in wanted]
    result={'schema_version':'1.0','generated_at_utc':now.isoformat(),'requested_completed_session':asof.isoformat(),'live_quote_session':live_session.isoformat() if live_session else None,'spy_regular_close':spy.get('regular_close',spy.get('close')),'replay_mode':a.offline,'spy':spy,'options_gex':options,'volatility':{'term_structure':term,'cboe_crosschecks':vol},'assets':assets,'futures':futures,'gamma_tracker':gamma_tracker,'intraday_feed':intraday_feed,'verified_headlines':headlines,'headlines_status':'dated manually verified public reporting; no automatic claim of verification' if headlines else 'unavailable; supply dated verified article metadata via --headlines-json','sources':sources,'data_normalizations':adjustments,'errors':errors,'methodology':{'sma':'arithmetic mean of last N unadjusted Close observations through completed session; source quotes snapped to 0.0001 to suppress Yahoo float32 artifacts; no dividends adjustment','returns':'close-to-close; 1, 5, 20 trading observations; split-adjusted Yahoo OHLC, dividends unadjusted','volume':'session shares / mean prior 20 completed sessions, excluding today; share volume is NOT signed fund flow','gamma_flip':'reprice all unexpired gammas at trial spot using frozen IV Black-Scholes; cumulative strike crossing separately named and is not a dealer zero-gamma spot','data_limits':'public delayed data; OI typically prior session and not verified intraday; rounded source gamma; actual dealer position unknown; no level independently validates actual dealer hedging','rate_and_dividend':'explicit configurable model assumptions, NOT measured market inputs'}}
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
    lines+=tracker_md_lines(gamma_tracker,fmt)
    f5=intraday_feed.get('spy_5m') or {};eo=intraday_feed.get('es_overnight_spy_terms') or {}
    lines+=['','## Intraday feed (data only)',f"- Time-of-day RVOL: {fmt(f5.get('tod_rvol'),2)} through {f5.get('last_completed_bar_ct')}; last bar vs slot: {fmt(f5.get('bar_rvol'),2)} ({len(f5.get('sessions_used') or [])} prior sessions; spike cap {f5.get('spike_cap')})",
            f"- ES overnight high/low in SPY terms ({ES_SPY_LABEL}): high {fmt(eo.get('spy_equiv_high'),2)}, low {fmt(eo.get('spy_equiv_low'),2)}, 8:20 CT {fmt(eo.get('spy_equiv_p0820'),2)}"]
    for k,g_ in (intraday_feed.get('gex_by_spot') or {}).items():
        lines.append(f"- GEX by spot, {k}: at live spot {fmt(g_.get('live_spot'),2)} = {fmt(g_.get('net_gex_at_live_spot'),0)}; at chain spot {fmt(g_.get('chain_spot'),2)} = {fmt(g_.get('net_gex_at_chain_spot'),0)}; grid {len(g_.get('grid') or [])} points ($1 steps, +/-2%)")
    lines+=['','## Methodology and limitations']+[f'- {k}: {v}' for k,v in result['methodology'].items()]
    lines+=['','## Verified dated headlines']+[f"- [{h['title']}]({h['url']}): {h['summary']}" for h in headlines]
    lines+=['','## SPY price / moving averages']
    lines.append(f"- Regular close: {spy.get('regular_close',spy.get('close'))}; live quote: {spy.get('intraday_quote')}; day change: {spy.get('change_dollars')}; volume: {spy.get('volume')}; prior-20 mean: {spy.get('volume_average_prior_20')}; relative volume: {spy.get('volume_vs_prior_20')}")
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
