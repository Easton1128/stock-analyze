#!/usr/bin/env python3
"""Fetch public market snapshots for stock-analyze-v4.1.

Sources:
- A-share: Eastmoney public quote endpoints (breadth/turnover/limit-up-down/mainline proxy)
- Northbound: Eastmoney public capital-flow endpoint when available
- US: Yahoo public chart endpoint for S&P 500 / VIX; breadth is best-effort from index/market data

Output is data/market-auto.json with the exact UI schema expected by V4.1:
{generated_at, markets:{A:{latest,runs}, US:{latest,runs}}}
"""
import argparse, json, os, re, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT=os.path.dirname(os.path.abspath(__file__))
OUT=os.path.join(ROOT,'data','market-auto.json')
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36'
HEAD={'User-Agent':UA,'Referer':'https://quote.eastmoney.com/'}
SINA_HEAD={'User-Agent':UA,'Referer':'https://finance.sina.com.cn/'}


def get_json(url, params=None, timeout=15, headers=None):
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params)
    req=Request(url, headers=headers or HEAD)
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8-sig'))


def num(v, default=0.0):
    try:
        if v is None or v=='-' or v=='': return default
        return float(v)
    except Exception: return default


def clamp(v,a,b): return max(a,min(b,v))


def sina_stocks():
    # [第1步：备用数据源] 新浪财经公开的沪深A股行情列表接口，在东财被限流/彻底失败时作为兜底。
    # 这是另一个独立的公开接口、独立的host，跟东财被限流互不关联，两边同时被限流的概率很低。
    rows=[]; page=1; pz=3000
    url='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeDataSimple'
    while True:
        data=get_json(url,{'page':page,'num':pz,'sort':'symbol','asc':1,'node':'hs_a','symbol':'','_s_r_a':'init'},headers=SINA_HEAD)
        if not data: break
        rows.extend(data)
        if len(data) < pz: break
        page+=1
        if page>5: break
        time.sleep(.2)
    return rows


def fetch_a_universe():
    # [第1步：备用数据源] 统一入口：优先东财(带重试)，东财彻底失败时自动切到新浪，
    # 两个都失败才真正报错。返回统一格式 (标准化后的个股列表, 数据源名称)，
    # 屏蔽掉两个数据源字段名不同(东财是f2/f3/f6，新浪是trade/changepercent/amount)的差异，
    # 让 a_snapshot() 不用关心具体是哪个源提供的数据。
    try:
        rows=eastmoney_stocks_with_retry(min_rows=3000)
        norm=[{'price':num(r.get('f2')),'pct_chg':num(r.get('f3')),'amount':num(r.get('f6'))} for r in rows if num(r.get('f2'),0)>0]
        return norm,'eastmoney-public'
    except Exception as e_east:
        try:
            rows=sina_stocks()
            norm=[]
            for r in rows:
                try:
                    price=num(r.get('trade'))
                    if price<=0: continue
                    norm.append({'price':price,'pct_chg':num(r.get('changepercent')),'amount':num(r.get('amount'))})
                except Exception:
                    continue
            if len(norm) < 3000:
                raise RuntimeError(f'Sina A-share quote only returned {len(norm)} usable rows (expected 3000+)')
            return norm,'sina-public-fallback'
        except Exception as e_sina:
            raise RuntimeError(f'both sources failed — eastmoney: {e_east}; sina fallback: {e_sina}')


def eastmoney_stocks_with_retry(min_rows=3000, attempts=3, backoff=(4,10)):
    # [新增] 东方财富对来自数据中心IP（比如GitHub Actions的服务器）的请求有时会限流/降级返回，
    # 表现为第一页就提前吐回一个远小于正常数量的结果（这次实测是100条，正常5000+）。
    # 这种情况通常是短暂的，加几次重试、每次间隔几秒，很大概率下一次就恢复正常了。
    last_err=None
    for i in range(attempts):
        try:
            rows=eastmoney_stocks()
            if len(rows) >= min_rows:
                return rows
            last_err=f'第{i+1}/{attempts}次尝试只返回了{len(rows)}条（期望3000+以上）'
        except Exception as e:
            last_err=str(e)
        if i < attempts-1:
            time.sleep(backoff[min(i,len(backoff)-1)])
    raise RuntimeError(f'Eastmoney A-share quote kept returning too few rows after {attempts} attempts ({last_err}); likely throttled by the data source for this network, treating as failure rather than committing a skewed score')


def eastmoney_stocks():
    rows=[]; page=1; pz=500
    fs='m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
    fields='f2,f3,f5,f6,f8,f12,f13,f14,f15,f16,f17,f18'
    while True:
        data=get_json('https://push2.eastmoney.com/api/qt/clist/get',{
            'pn':page,'pz':pz,'po':1,'np':1,'ut':'bd1d9ddb04089700cf9c27f6f7426281',
            'fltt':2,'invt':2,'fid':'f3','fs':fs,'fields':fields
        })
        block=(data or {}).get('data') or {}
        diff=block.get('diff') or []
        if not diff: break
        rows.extend(diff)
        if len(rows)>=int(block.get('total') or 0) or len(diff)<pz: break
        page+=1
        if page>30: break
        time.sleep(.15)
    return rows


def northbound_flow():
    # Eastmoney capital-flow endpoint; API shape can change, so fail soft.
    urls=[
      'https://push2.eastmoney.com/api/qt/kamt/get',
      'https://push2his.eastmoney.com/api/qt/kamt/get'
    ]
    params={'fields1':'f1,f2,f3,f4','fields2':'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65'}
    for u in urls:
        try:
            d=get_json(u,params)
            x=(d or {}).get('data') or {}
            # Common fields: hsgt/s2n/s2n_total etc. Search numeric leaves for likely net-flow keys.
            for key in ('hk2sh','hk2sz','north','northbound','s2n','s2n_total','hsgt'):
                if key in x:
                    obj=x[key]
                    if isinstance(obj,dict):
                        for k in ('net','netInflow','f2','f52','f53'):
                            if k in obj: return num(obj[k])
                    if isinstance(obj,(int,float,str)): return num(obj)
        except Exception:
            pass
    return None


def board_fund_flow():
    # [新增] 东方财富行业板块资金流排行（公开接口，和northbound_flow同一个host）：
    # fs=m:90+t:2 是"行业板块"分类，按 f62（主力净流入-净额）降序取前几名。
    # 接口结构和northbound_flow一样存在变化风险，这里同样"失败就返回空列表"，
    # 不影响A股主评分（评分四项：量能/涨跌家数/涨跌停/封板率跟主线板块无关），
    # 只影响"今日主线板块"这个展示性字段。
    try:
        params={'pn':'1','pz':'5','po':'1','np':'1','fltt':'2','invt':'2','fid':'f62',
                'fs':'m:90+t:2','fields':'f12,f14,f3,f62'}
        d=get_json('https://push2.eastmoney.com/api/qt/clist/get', params, headers=HEAD)
        rows=((d or {}).get('data') or {}).get('diff') or []
        names=[]
        for r in rows:
            n=r.get('f14')
            if n: names.append(n)
        return names[:3]
    except Exception:
        return []


def mainline_streak(hist_runs, today_mainline, today_date):
    # [新增] 判断"今天这个主线板块，已经连续排第一多少个交易日"，用来推断所处阶段：
    # 1-2天=启动期（刚冒头，观察是否延续）；3-5天=主升期（趋势确立，跟随度较高）；
    # 6天以上=退潮观察期（持续时间已经偏长，提示注意资金是否开始撤离，而不是继续追高）。
    # 判定方式：从本地已积累的历史往回看，只要某天记录的mainline字段和今天一致就继续累计，
    # 中断（换了别的板块，或那天没有主线数据）就停止往回数。
    if not today_mainline:
        return {'mainlineStartDate': today_date, 'mainlineDays': 0, 'mainlineStage': ''}
    rank={'T+3h':3,'T+2.5h':2.5,'T+2h':2}
    by_date={}
    for r in (hist_runs or []):
        d=r.get('date')
        if not d or d==today_date: continue  # 排除"今天"自己，历史只看今天之前
        if d not in by_date or rank.get(r.get('slot'),0)>=rank.get(by_date[d].get('slot'),0):
            by_date[d]=r
    dates=sorted(by_date.keys())
    streak_start=today_date
    days=1  # 算上"今天"自己
    for d in reversed(dates):
        if by_date[d].get('mainline')==today_mainline:
            days+=1
            streak_start=d
        else:
            break
    stage='启动期' if days<=2 else ('主升期' if days<=5 else '退潮观察期')
    return {'mainlineStartDate': streak_start, 'mainlineDays': days, 'mainlineStage': stage}


def rolling_baseline(hist_runs, field, days=5):
    # [方案B新增] 用已经存好的历史快照，取最近N个交易日（每天只取一条最可信的：T+3h优先）的
    # 某个原始字段做算术平均，当作"正常水平"的基准——不需要额外接口，用的是同一份market-auto.json
    # 自己积累下来的历史。前几天数据不够时返回 None，调用方需要自行处理"暂时没有基准"的情况。
    rank={'T+3h':3,'T+2.5h':2.5,'T+2h':2}
    by_date={}
    for r in (hist_runs or []):
        d=r.get('date')
        if not d: continue
        if d not in by_date or rank.get(r.get('slot'),0)>=rank.get(by_date[d].get('slot'),0):
            by_date[d]=r
    dates=sorted(by_date.keys())[-days:]
    vals=[num(by_date[d].get(field),None) for d in dates]
    vals=[v for v in vals if v]
    return sum(vals)/len(vals) if vals else None


def a_snapshot(now, hist_runs=None):
    valid,source_used=fetch_a_universe()
    up=sum(r['pct_chg']>0 for r in valid)
    down=sum(r['pct_chg']<0 for r in valid)
    turnover=sum(r['amount'] for r in valid)/1e8 # 亿元
    # Broad limit-up/down proxy. Exact ST/IPO rules vary; preserve raw counts as a transparent proxy.
    limitup=sum(r['pct_chg']>=9.8 for r in valid)
    limitdown=sum(r['pct_chg']<=-9.8 for r in valid)
    # Mainline proxy: strongest turnover/price sectors are not directly available from the stock list;
    # leave sectors empty rather than inventing classifications.
    # [方案B修复] 基准不再等于当天成交额自己（那样比值永远是1，分数恒定）。
    # 改用本地已经积累的最近5个交易日成交额均值；数据不够5天时，暂时退回"等于自己"
    # （比值=1，即中性分数），跟老版本行为一致，等自动化跑满5天后会自动切换成真实基准。
    baseline=rolling_baseline(hist_runs, 'turnover', days=5) or turnover
    north=northbound_flow()
    sectors=board_fund_flow()
    mainline=sectors[0] if sectors else ''
    streak=mainline_streak(hist_runs, mainline, now.strftime('%Y-%m-%d'))
    up_ratio=up/(up+down) if up+down else .5
    # [方案B] 量能：真实基准可用后即为真实动态分值，权重从20分提到30分。
    turnover_score=clamp((turnover/max(baseline,1)-.7)/.8*30,0,30)
    breadth_score=clamp((up_ratio-.3)/.4*25,0,25)
    limit_score=clamp(((limitup-limitdown)+20)/100*25,0,25)
    seal_score=clamp((limitup/max(limitup+1,1)-.3)/.6*20,0,20)
    # [方案B] 主力资金净流入(inflow)和连板梯队(ladder)目前没有零成本的真实数据源，
    # 不再打包进0-100总分（避免用假分数拉低整体评分可信度）——继续算出来仅作参考展示，
    # 不计入 score。想要更真实的分数，仍然可以在这两项手工核实后走手工录入路径。
    inflow_score_ref=0
    ladder_score_ref=0
    score=round(clamp(turnover_score+breadth_score+limit_score+seal_score,0,100))
    return {
      'source':source_used,'autoGenerated':True,'date':now.strftime('%Y-%m-%d'),
      'slot':slot_for(now,'A'),'captured_at':now.isoformat(timespec='seconds'),
      'formula_version':2,
      'score':score,'turnoverScore':round(turnover_score),'breadthScore':round(breadth_score),
      'limitScore':round(limit_score),'sealScore':round(seal_score),
      'inflowScoreRef':inflow_score_ref,'ladderScoreRef':ladder_score_ref,
      'turnover':round(turnover,2),'baseline':round(baseline,2),'inflow':0,'up':up,'down':down,
      'limitup':limitup,'limitdown':limitdown,'broken':0,'northbound':north,
      'mainline':mainline,'sectors':sectors,
      'mainlineStartDate':streak['mainlineStartDate'],'mainlineDays':streak['mainlineDays'],'mainlineStage':streak['mainlineStage']
    }


US_SECTOR_ETFS=[('XLK','科技'),('XLF','金融'),('XLE','能源'),('XLV','医疗保健'),('XLY','非必需消费'),
                ('XLP','必需消费'),('XLI','工业'),('XLB','原材料'),('XLU','公用事业'),('XLRE','房地产'),('XLC','通信服务')]


def yahoo_daily_change(symbol):
    # 取5天窗口而不是1天，是为了能拿到"昨收"来算涨跌幅——range=1d只有当天一根K线，算不出涨跌幅。
    d=get_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',{'range':'5d','interval':'1d'},headers={'User-Agent':UA})
    res=((d or {}).get('chart') or {}).get('result') or []
    if not res: return None
    q=res[0].get('indicators',{}).get('quote',[{}])[0]
    closes=[x for x in q.get('close',[]) if x is not None]
    if len(closes) < 2: return None
    prev,last=closes[-2],closes[-1]
    if not prev: return None
    return (last-prev)/prev*100


def us_sector_flow():
    # [第2步：美股主线板块] 美股没有像东财那样公开的全市场行业资金流接口，
    # 用SPDR 11个行业ETF(标普500官方行业分类的代表性ETF)当日涨跌幅做代理指标——
    # 涨幅最高的行业视为"今日主线"，这是行业内常见的简化替代做法（用价格表现代替资金流）。
    # 单个ETF抓取失败不影响其它；能比较的数量太少(<5个)时，视为数据不足，返回空列表而不是编造结果。
    perf=[]
    for sym,name in US_SECTOR_ETFS:
        try:
            chg=yahoo_daily_change(sym)
            if chg is not None: perf.append((name,chg))
        except Exception:
            pass
        time.sleep(.15)
    if len(perf) < 5:
        return []
    perf.sort(key=lambda x:x[1], reverse=True)
    return [p[0] for p in perf[:3]]


def yahoo_chart(symbol, now):
    d=get_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',{'range':'1d','interval':'1d','events':'div,splits'},headers={'User-Agent':UA})
    res=((d or {}).get('chart') or {}).get('result') or []
    if not res: return None
    q=res[0].get('indicators',{}).get('quote',[{}])[0]
    close=[x for x in q.get('close',[]) if x is not None]
    vol=[x for x in q.get('volume',[]) if x is not None]
    return {'close':close[-1] if close else None,'volume':vol[-1] if vol else None}


def us_snapshot(now, hist_runs=None):
    # Public Yahoo endpoints provide robust index/VIX snapshots without credentials.
    sp=yahoo_chart('^GSPC',now) or {}
    vx=yahoo_chart('^VIX',now) or {}
    # [P2新增] 如果标普和VIX两个端点都拿不到收盘价，说明Yahoo接口本身失败了；
    # 之前的写法会静默退回默认值（vix=20中性值、turnover=0），产生一条"看起来正常"但其实是假数据的记录。
    if sp.get('close') is None and vx.get('close') is None:
        raise RuntimeError('Yahoo chart endpoint returned no usable data for ^GSPC/^VIX')
    vix=num(vx.get('close'),20)
    # Without a stable public full-US breadth endpoint, do not fabricate up/down/new-high/new-low.
    up=down=0; newhigh=newlow=0
    turnover=num(sp.get('volume'))
    # [方案B修复] 同A股：不再用"基准=自己"（比值恒为1），改用最近5个交易日标普成交量均值。
    baseline=rolling_baseline(hist_runs, 'turnover', days=5) or turnover or 1
    ratio=turnover/baseline if baseline else 1
    # [方案B] capex/breadth/highlow/credit/curve 目前没有零成本的真实数据源，不再计入0-100总分，
    # 只保留 turnover（现在基准是真实的）和 vix（本来就是真实的）两项，按原权重比例放大到100分：
    # 原 turnover:vix = 15:20，等比放大后 40:60。
    turnover_score=clamp((ratio-.7)/.8*40,0,40)
    vix_score=clamp((30-vix)/18*60,0,60)
    capex=0; credit=4; curve=0  # 仅作参考展示，不再计入总分
    score=round(clamp(turnover_score+vix_score,0,100))
    # [第2步：美股主线板块] 跟A股用同一套"连续几天排第一"的阶段判定逻辑(mainline_streak)。
    sectors=us_sector_flow()
    mainline=sectors[0] if sectors else ''
    streak=mainline_streak(hist_runs, mainline, now.strftime('%Y-%m-%d'))
    return {
      'source':'yahoo-public','autoGenerated':True,'date':now.strftime('%Y-%m-%d'),'slot':slot_for(now,'US'),'captured_at':now.isoformat(timespec='seconds'),
      'formula_version':2,
      'score':score,'turnoverScore':round(turnover_score),'vixScore':round(vix_score),
      'capexScoreRef':0,'breadthScoreRef':0,'highlowScoreRef':0,'creditScoreRef':0,'curveScoreRef':0,
      'turnover':turnover,'baseline':round(baseline,2) if baseline else baseline,'capex':capex,'vix':vix,'up':up,'down':down,'newhigh':newhigh,'newlow':newlow,'credit':credit,'curve':curve,
      'mainline':mainline,'sectors':sectors,
      'mainlineStartDate':streak['mainlineStartDate'],'mainlineDays':streak['mainlineDays'],'mainlineStage':streak['mainlineStage']
    }


def slot_for(dt, market):
    # Market-local slots: A-share close 15:00 Asia/Shanghai; US close 16:00 America/New_York.
    h=dt.hour + dt.minute/60
    if market=='A':
        if h<17.25: return 'T+2h'
        if h<17.75: return 'T+2.5h'
        return 'T+3h'
    if h<18.25: return 'T+2h'
    if h<18.75: return 'T+2.5h'
    return 'T+3h'


def update_market(existing, market, run):
    bucket=existing.setdefault('markets',{}).setdefault(market,{'latest':None,'runs':[]})
    runs=bucket.get('runs') or []
    runs=[x for x in runs if not (x.get('date')==run.get('date') and x.get('slot')==run.get('slot'))]
    runs.append(run)
    runs.sort(key=lambda x:x.get('captured_at',''))
    # Retain latest 20 distinct trading dates, as the UI expects a compact history window.
    keep_dates=[]
    for x in reversed(runs):
        d=x.get('date')
        if d and d not in keep_dates: keep_dates.append(d)
        if len(keep_dates)>=20: break
    keep=set(keep_dates)
    runs=[x for x in runs if x.get('date') in keep]
    bucket['runs']=runs
    bucket['latest']=runs[-1] if runs else None


def main():
    parser=argparse.ArgumentParser(description='Fetch one market snapshot')
    parser.add_argument('--market', choices=['A','US','ALL'], default='ALL')
    args=parser.parse_args()
    now_utc=datetime.now(timezone.utc)
    now_a=now_utc.astimezone(ZoneInfo('Asia/Shanghai'))
    now_us=now_utc.astimezone(ZoneInfo('America/New_York'))
    os.makedirs(os.path.dirname(OUT),exist_ok=True)
    try:
        with open(OUT,'r',encoding='utf-8') as f: data=json.load(f)
    except Exception:
        data={'schema_version':1,'generated_at':None,'markets':{'A':{'latest':None,'runs':[]},'US':{'latest':None,'runs':[]}}}
    data['schema_version']=1
    data['generated_at']=now_utc.isoformat(timespec='seconds')
    # [P2修复] fetch_errors 按市场分别保存，不再是一个被整体覆盖的共享数组。
    # 原因：A股和美股是两个独立的 GitHub Actions job、分别调用本脚本（--market A / --market US），
    # 旧写法每次都用 data['fetch_errors']=本次的errors 整体覆盖，导致后运行的那个市场
    # 哪怕自己完全成功（errors=[]），也会把另一个市场刚记录下的失败信息一起抹掉，
    # 前端因此完全看不到"某个市场其实抓取失败了"这个事实。
    prev_errors = data.get('fetch_errors')
    if not isinstance(prev_errors, dict):
        prev_errors = {}  # 兼容旧schema（数组形式）：直接丢弃，从这次运行开始改用按市场记录
    errors_by_market = {}
    if args.market in ('A','ALL'):
        try:
            update_market(data,'A',a_snapshot(now_a, data['markets']['A'].get('runs')))
            errors_by_market['A']=[]
        except Exception as e:
            errors_by_market['A']=['A: '+str(e)]
    if args.market in ('US','ALL'):
        try:
            update_market(data,'US',us_snapshot(now_us, data['markets']['US'].get('runs')))
            errors_by_market['US']=[]
        except Exception as e:
            errors_by_market['US']=['US: '+str(e)]
    merged_errors = dict(prev_errors)
    merged_errors.update(errors_by_market)
    data['fetch_errors']=merged_errors
    all_errors = [e for lst in errors_by_market.values() for e in lst]
    tmp=OUT+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False,indent=2)
    os.replace(tmp,OUT)
    print(json.dumps({'output':OUT,'generated_at':data['generated_at'],'errors':all_errors,'fetch_errors':merged_errors,'A_runs':len(data['markets']['A']['runs']),'US_runs':len(data['markets']['US']['runs'])},ensure_ascii=False))
    return 0 if not all_errors else 2

if __name__=='__main__': sys.exit(main())
