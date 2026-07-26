"""BIST 100 analytics engine: trajectories, volatility, volume."""
import numpy as np, pandas as pd, yfinance as yf, datetime as dt

BENCH = "XU100.IS"
TICKERS = """AEFES AGHOL AKBNK AKCNS AKFGY AKFYE AKSA AKSEN ALARK ALBRK ALFAS ANSGR ARCLK ASELS ASTOR
BERA BIENY BIMAS BINHO BRSAN BRYAT BUCIM CANTE CCOLA CIMSA CWENE DOAS DOHOL ECILC ECZYT EGEEN EKGYO
ENERY ENJSA ENKAI EREGL EUPWR EUREN FROTO GARAN GESAN GLYHO GUBRF GWIND HALKB HEKTS ISCTR
ISDMR ISGYO ISMEN IZENR IZMDC KARSN KAYSE KCAER KCHOL KLSER KMPUR KONTR KONYA KRDMD
KZBGY MAVI MGROS MIATK MPARK ODAS OTKAR OYAKC PENTA PETKM PGSUS QUAGR REEDR SAHOL SASA SDTTR SISE
SKBNK SMRTG SOKM TAVHL TCELL THYAO TKFEN TOASO TSKB TTKOM TTRAK TUKAS TUPRS ULKER VAKBN VESBE
VESTL YEOTK YKBNK YYLGD ZOREN GEREL""".split()

SECTOR = {
 "AKBNK":"Bankacılık","GARAN":"Bankacılık","ISCTR":"Bankacılık","YKBNK":"Bankacılık","VAKBN":"Bankacılık",
 "HALKB":"Bankacılık","ALBRK":"Bankacılık","SKBNK":"Bankacılık","TSKB":"Bankacılık","ISMEN":"Aracı Kurum",
 "KCHOL":"Holding","SAHOL":"Holding","AGHOL":"Holding","DOHOL":"Holding","ALARK":"Holding","GLYHO":"Holding",
 "BRYAT":"Holding","ECILC":"Holding","BERA":"Holding",
 "EREGL":"Demir-Çelik","ISDMR":"Demir-Çelik","KRDMD":"Demir-Çelik","IZMDC":"Demir-Çelik","KCAER":"Demir-Çelik",
 "SISE":"Cam-Kimya","PETKM":"Cam-Kimya","SASA":"Cam-Kimya","GUBRF":"Cam-Kimya","AKSA":"Cam-Kimya","HEKTS":"Cam-Kimya",
 "TUPRS":"Enerji-Rafineri","ENJSA":"Enerji","AKSEN":"Enerji","ZOREN":"Enerji","ODAS":"Enerji","AKFYE":"Enerji",
 "ENERY":"Enerji","GWIND":"Enerji","SMRTG":"Enerji","EUPWR":"Enerji","IZENR":"Enerji","CWENE":"Enerji","EUREN":"Enerji",
 "THYAO":"Ulaştırma","PGSUS":"Ulaştırma","TAVHL":"Ulaştırma",
 "FROTO":"Otomotiv","TOASO":"Otomotiv","DOAS":"Otomotiv","OTKAR":"Otomotiv","KARSN":"Otomotiv","TTRAK":"Otomotiv",
 "BIMAS":"Perakende","MGROS":"Perakende","SOKM":"Perakende","MAVI":"Perakende","ULKER":"Gıda","AEFES":"Gıda",
 "CCOLA":"Gıda","TUKAS":"Gıda","KONYA":"Gıda","YYLGD":"Gıda",
 "ASELS":"Savunma-Tekno","SDTTR":"Savunma-Tekno","ASTOR":"Sanayi","GESAN":"Sanayi","GEREL":"Sanayi",
 "KONTR":"Sanayi","ALFAS":"Sanayi","gg":"",
 "TCELL":"Telekom","TTKOM":"Telekom","MIATK":"Teknoloji","PENTA":"Teknoloji","REEDR":"Teknoloji",
 "EKGYO":"GYO","ISGYO":"GYO","AKFGY":"GYO","KZBGY":"GYO","BIENY":"GYO","QUAGR":"Çimento",
 "AKCNS":"Çimento","CIMSA":"Çimento","OYAKC":"Çimento","BUCIM":"Çimento","KLSER":"Çimento",
 "ARCLK":"Dayanıklı Tük.","VESTL":"Dayanıklı Tük.","VESBE":"Dayanıklı Tük.","EGEEN":"Sanayi",
 "MPARK":"Sağlık","ANSGR":"Sigorta","ENKAI":"İnşaat","TKFEN":"İnşaat","BRSAN":"Sanayi","KMPUR":"Kimya",
 "CANTE":"Enerji","KAYSE":"Sanayi","BINHO":"Holding","YEOTK":"Sanayi","ECZYT":"Holding",
}

def fetch(period="1y"):
    syms = [t + ".IS" for t in TICKERS] + [BENCH]
    raw = yf.download(syms, period=period, interval="1d",
                      progress=False, auto_adjust=False, threads=True)
    px, vol = raw["Close"], raw["Volume"]
    hi, lo = raw["High"], raw["Low"]
    keep = [c for c in px.columns if px[c].notna().sum() > 200]
    return px[keep], vol[keep], hi[keep], lo[keep]

def _ret(s, n):
    s = s.dropna()
    if len(s) <= n: return np.nan
    return (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100

def _ytd(s):
    s = s.dropna()
    y = s[s.index.year == s.index[-1].year]
    if len(y) < 2: return np.nan
    return (s.iloc[-1] / y.iloc[0] - 1) * 100

def classify_trend(s):
    s = s.dropna()
    if len(s) < 60: return "Yetersiz veri", 0
    p = s.iloc[-1]
    sma20, sma50 = s.rolling(20).mean().iloc[-1], s.rolling(50).mean().iloc[-1]
    sma200 = s.rolling(200).mean().iloc[-1] if len(s) >= 200 else np.nan
    # swing structure: last 10d vs prior 10d
    hh = s.iloc[-10:].max() > s.iloc[-20:-10].max()
    ll = s.iloc[-10:].min() < s.iloc[-20:-10].min()
    score = 0
    if p > sma20: score += 1
    if p > sma50: score += 1
    if not np.isnan(sma200) and p > sma200: score += 1
    if sma20 > sma50: score += 1
    if hh and not ll: score += 1
    if ll and not hh: score -= 1
    if score >= 4: lab = "Güçlü yükseliş"
    elif score >= 2: lab = "Yükseliş"
    elif score <= -1 or score == 0: lab = "Düşüş"
    else: lab = "Yatay/Sıkışma"
    return lab, score

def levels(s, h, l):
    s, h, l = s.dropna(), h.dropna(), l.dropna()
    out = {}
    out["sup20"] = l.iloc[-21:-1].min() if len(l) > 21 else np.nan
    out["res20"] = h.iloc[-21:-1].max() if len(h) > 21 else np.nan
    out["sup60"] = l.iloc[-61:-1].min() if len(l) > 61 else np.nan
    out["res60"] = h.iloc[-61:-1].max() if len(h) > 61 else np.nan
    p = s.iloc[-1]
    out["to_res20"] = (out["res20"] / p - 1) * 100
    out["to_sup20"] = (out["sup20"] / p - 1) * 100
    # ATR14
    tr = pd.concat([h - l, (h - s.shift()).abs(), (l - s.shift()).abs()], axis=1).max(axis=1)
    out["atr_pct"] = (tr.rolling(14).mean().iloc[-1] / p) * 100
    return out

def build(px, vol, hi, lo):
    b = px[BENCH].dropna()
    bench = {n: _ret(b, n) for n in (1, 5, 20, 60)}
    bench["ytd"] = _ytd(b)
    rows = []
    for c in px.columns:
        if c == BENCH: continue
        s, v = px[c].dropna(), vol[c].dropna()
        if len(s) < 60: continue
        sym = c.replace(".IS", "")
        lr = np.log(s / s.shift()).dropna()
        v20 = lr.iloc[-20:].std() * np.sqrt(252) * 100
        v60 = lr.iloc[-60:].std() * np.sqrt(252) * 100
        d = {"sym": sym, "sector": SECTOR.get(sym, "Diğer"), "close": s.iloc[-1],
             "r1": _ret(s,1), "r5": _ret(s,5), "r20": _ret(s,20), "r60": _ret(s,60), "ytd": _ytd(s),
             "vol20": v20, "vol60": v60, "vol_regime": v20 / v60 if v60 else np.nan,
             "advol20": v.iloc[-21:-1].mean(), "relvol": v.iloc[-1] / v.iloc[-21:-1].mean() if v.iloc[-21:-1].mean() else np.nan,
             "tl_vol": s.iloc[-1] * v.iloc[-1]}
        d["trend"], d["tscore"] = classify_trend(s)
        d.update(levels(s, hi[c], lo[c]))
        d["rs5"], d["rs20"], d["rs60"] = d["r5"]-bench[5], d["r20"]-bench[20], d["r60"]-bench[60]
        d["rs_ytd"] = d["ytd"] - bench["ytd"]
        d["rs_slope"] = d["rs5"] - (d["rs20"] / 4)   # 5d excess vs avg weekly excess over 20d
        sig = lr.iloc[-60:].std() * 100
        d["zmove"] = d["r1"] / sig if sig else np.nan
        rows.append(d)
    df = pd.DataFrame(rows).set_index("sym")
    return df, bench, b

def screen(df):
    f = {}
    f["hacim_patlaması"] = df[df.relvol >= 2.0].sort_values("relvol", ascending=False)
    f["volatilite_genişlemesi"] = df[(df.vol_regime >= 1.4) & (df.vol20 >= 30)].sort_values("vol_regime", ascending=False)
    f["direnç_kırılımı"] = df[(df.close >= df.res20) & (df.r1 > 0)].sort_values("r1", ascending=False)
    f["destek_kaybı"] = df[(df.close <= df.sup20) & (df.r1 < 0)].sort_values("r1")
    f["aşırı_hareket"] = df[df.zmove.abs() >= 2.5].reindex(df.zmove.abs().sort_values(ascending=False).index).dropna(how="all")
    f["güçlü_RS"] = df[(df.rs20 >= 5) & (df.tscore >= 3)].sort_values("rs20", ascending=False)
    f["zayıf_RS"] = df[(df.rs20 <= -5) & (df.tscore <= 1)].sort_values("rs20")
    return f
