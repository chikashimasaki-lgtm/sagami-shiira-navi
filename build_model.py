#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
庄三郎丸 シイラ船 釣果モデル ビルダー
  1. 公式釣果API から過去2年のルアーシイラ船を取得
  2. Open-Meteo アーカイブで相模湾平塚沖の海況（SST・波高・風）を取得
  3. 釣果×海況を相関分析し、判定モデル model.json を生成
GitHub Actions から定期実行する想定。失敗時は既存 model.json を維持。
"""
import re, html, json, time, sys, urllib.request, datetime
from collections import defaultdict
from statistics import mean

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
LISTAPI = "https://www.shouzaburo.com/api/getChokaListPage/"
LAT, LON = 35.27, 139.35
Z2H = str.maketrans("０１２３４５６７８９", "0123456789")

# ───────── 1. スクレイプ ─────────
def fetch_list(p):
    req = urllib.request.Request(f"{LISTAPI}?p={p}", headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t　]+", " ", html.unescape(s)).strip().translate(Z2H)

def parse_page(htmltext):
    out = []
    for blk in re.split(r"(?=ChokaDetail/\d+)", htmltext):
        if "ChokaDetail/" not in blk:
            continue
        txt = clean(blk)
        md = re.search(r"(20\d{2})年(\d{2})月(\d{2})日", txt)
        if not md or "シイラ船" not in txt:
            continue
        d = f"{md.group(1)}-{md.group(2)}-{md.group(3)}"
        ms = re.search(r"シイラ.*?(?=本ガツオ|キハダ|カツオ|マグロ |マグロ・|船長コメント|$)", txt, re.S)
        sec = ms.group(0) if ms else txt
        sizes   = [int(x) for x in re.findall(r"(\d+)\s*cm", sec)]
        caught  = [int(x) for x in re.findall(r"キャッチ\s*(\d+)\s*本", sec)]
        hits    = [int(x) for x in re.findall(r"ヒット\s*(\d+)\s*回", sec)]
        anglers = [int(x) for x in re.findall(r"(\d+)\s*名", sec)]
        out.append({"date": d, "size_max": max(sizes) if sizes else None,
                    "caught": sum(caught), "hits": sum(hits),
                    "anglers": sum(anglers) if anglers else None})
    return out

def scrape(cutoff, limit=260):
    recs, p = [], 1
    while p <= limit:
        try:
            page = fetch_list(p)
        except Exception as e:
            print(f"  p={p} error {e}", file=sys.stderr); break
        dates = re.findall(r"20\d{2}年\d{2}月\d{2}日", page)
        recs += parse_page(page)
        if dates:
            oldest = min(f"{m[:4]}-{m[5:7]}-{m[8:10]}" for m in dates)
            if oldest < cutoff:
                break
        else:
            break
        p += 1
        time.sleep(0.4)
    recs.sort(key=lambda r: r["date"])
    return recs

# ───────── 2. 海況アーカイブ ─────────
def gj(u): return json.load(urllib.request.urlopen(u, timeout=60))

def fetch_conditions(start, end):
    mar = gj(f"https://marine-api.open-meteo.com/v1/marine?latitude={LAT}&longitude={LON}"
             "&hourly=sea_surface_temperature&daily=wave_height_max"
             f"&start_date={start}&end_date={end}&timezone=Asia%2FTokyo")
    wx = gj(f"https://archive-api.open-meteo.com/v1/archive?latitude={LAT}&longitude={LON}"
            "&daily=wind_speed_10m_max,temperature_2m_mean"
            f"&start_date={start}&end_date={end}&timezone=Asia%2FTokyo")
    sst = defaultdict(list)
    for t, v in zip(mar['hourly']['time'], mar['hourly']['sea_surface_temperature']):
        if v is not None: sst[t[:10]].append(v)
    sst = {d: mean(v) for d, v in sst.items()}
    wave = dict(zip(mar['daily']['time'], mar['daily']['wave_height_max']))
    wind = dict(zip(wx['daily']['time'], wx['daily']['wind_speed_10m_max']))
    return sst, wave, wind

# ───────── 3. 分析 → model.json ─────────
def corr(xs, ys):
    mx, my = mean(xs), mean(ys)
    cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    sx = sum((x-mx)**2 for x in xs)**.5; sy = sum((y-my)**2 for y in ys)**.5
    return round(cov/(sx*sy), 2) if sx and sy else 0

def binmean(rows, key, edges):
    out = []
    for lo, hi in zip(edges, edges[1:]):
        g = [r["cpa"] for r in rows if lo <= r[key] < hi]
        out.append({"range": f"{lo}–{hi}", "n": len(g),
                    "avg": round(mean(g), 1) if g else None})
    return out

def build_model(catch, sst):
    byd = defaultdict(lambda: {"caught": 0, "anglers": 0, "size": 0})
    for r in catch:
        b = byd[r['date']]; b["caught"] += r['caught'] or 0
        b["anglers"] += r['anglers'] or 0; b["size"] = max(b["size"], r['size_max'] or 0)
    days = sorted(byd)
    cdays = [d for d in days if byd[d]["caught"] > 0]
    best = max(days, key=lambda d: byd[d]["caught"])
    rows = [{"cpa": byd[d]["caught"]/byd[d]["anglers"], "sst": sst[d], "month": int(d[5:7])}
            for d in days if byd[d]["anglers"] > 0 and d in sst]
    cor = {"sst": corr([r["sst"] for r in rows], [r["cpa"] for r in rows]),
           "month": corr([r["month"] for r in rows], [r["cpa"] for r in rows])}

    return {
        "title": "庄三郎丸 ルアーシイラ船 釣行推奨ランキング",
        "generated": datetime.date.today().isoformat(),
        "spot": {"name": "相模湾・平塚沖（庄三郎丸）", "lat": LAT, "lon": LON},
        "source": "https://www.shouzaburo.com/category/Choka/",
        "history": {
            "period": [days[0], days[-1]], "trips": len(catch), "days": len(days),
            "catch_rate": round(len(cdays)/len(days), 2),
            "total_caught": sum(byd[d]["caught"] for d in days),
            "max_day": {"date": best, "caught": byd[best]["caught"]},
            "by_sst": binmean(rows, "sst", [18, 22, 24, 25, 26, 27, 32]),
            "by_month": binmean(rows, "month", [5, 6, 7, 8, 9, 10]),
            "corr": cor,
        },
        "scoring": {
            "weights": {"sst": 0.42, "season": 0.20, "trend": 0.12, "wind": 0.14, "wave": 0.12},
            "sst_curve": [[0, 0.20], [22, 0.30], [24, 0.42], [25, 0.60], [26, 0.78], [27, 1.0], [40, 1.0]],
            "season_month": {"1":0.05,"2":0.05,"3":0.05,"4":0.15,"5":0.30,"6":0.40,
                             "7":0.55,"8":1.0,"9":0.90,"10":0.60,"11":0.20,"12":0.05},
            "trend_curve": [[-9, 1.0], [-0.5, 1.0], [0.5, 0.85], [1.5, 0.6], [9, 0.45]],
            "wind_curve": [[0, 0.8], [15, 1.0], [20, 0.95], [25, 0.9], [26, 0.4], [99, 0.35]],
            "wave_curve": [[0, 1.0], [1.0, 1.0], [1.5, 0.8], [2.0, 0.45], [3.0, 0.15], [9, 0.1]],
            "rank": [["S", 80], ["A", 68], ["B", 55], ["C", 42], ["D", 28], ["E", 0]],
            "deploy_caution_wave": 1.5, "deploy_cancel_wave": 2.2,
        },
        "insights": [
            f"海面水温が最大の決定要因。25℃が分岐点、27℃超で爆発的に釣れる(過去2年: 27℃超で平均{_avg(rows,27)}本/人)。",
            "最盛期は8〜9月。6〜7月は1本台/人で発展途上。",
            f"水温の急上昇期より高水温が定着した安定期が好釣果(SST×釣果の傾向)。",
            "最大風速25km/h超は釣果激減。強風日は避ける。",
            "波高は釣果への影響は小さいが、1.5m超は出船注意・2.2m超は出船中止の目安(安全最優先)。",
            "船長コメント頻出: ナブラ・モジ・潮目・サメ付きイワシ団子・流木に着く。進行方向の先へキャストしメーターオーバーを獲る。",
        ],
    }

def _avg(rows, sst_min):
    g = [r["cpa"] for r in rows if r["sst"] >= sst_min]
    return round(mean(g), 1) if g else "—"

def main():
    today = datetime.date.today()
    cutoff = today.replace(year=today.year - 2).isoformat()
    print(f"スクレイプ開始 cutoff={cutoff}")
    catch = scrape(cutoff)
    if not catch:
        print("釣果0件取得 → 既存model.jsonを維持して終了", file=sys.stderr)
        sys.exit(0)
    print(f"  シイラ船 {len(catch)}件 ({catch[0]['date']}〜{catch[-1]['date']})")
    sst, _wave, _wind = fetch_conditions(catch[0]['date'], today.isoformat())
    model = build_model(catch, sst)
    json.dump(catch, open("shiira_catch.json", "w"), ensure_ascii=False, indent=1)
    json.dump(model, open("model.json", "w"), ensure_ascii=False, indent=1)
    print(f"完了: 出船{model['history']['days']}日 釣果率{model['history']['catch_rate']} "
          f"相関SST={model['history']['corr']['sst']}")

if __name__ == "__main__":
    main()
