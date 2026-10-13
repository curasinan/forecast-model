"""Generates dashboard + email digest. Usage: python3 report.py [preopen|postclose]"""
import sys, os, json, base64, io, datetime as dt, zoneinfo
import numpy as np, pandas as pd
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image
import engine, dashboard

MODE = sys.argv[1] if len(sys.argv) > 1 else "auto"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/bist/out"
os.makedirs(OUT, exist_ok=True)
IST = zoneinfo.ZoneInfo("Europe/Istanbul")
now = dt.datetime.now(IST)
TR_D = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
TR_M = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
datestr = f"{now.day} {TR_M[now.month-1]} {now.year} {TR_D[now.weekday()]}"

OPEN_H, CLOSE_H = 10.0, 18.1
hhmm = now.hour + now.minute / 60
weekday = now.weekday() < 5
if not weekday:                       state = "kapali_haftasonu"
elif hhmm < OPEN_H:                   state = "acilis_oncesi"
elif hhmm < CLOSE_H:                  state = "seans_ici"
else:                                 state = "kapanis_sonrasi"

REQ = MODE
if MODE == "auto":
    MODE = {"acilis_oncesi": "preopen", "seans_ici": "intraday",
            "kapanis_sonrasi": "postclose", "kapali_haftasonu": "postclose"}[state]
MISMATCH = None
if REQ == "preopen" and state != "acilis_oncesi":
    MISMATCH = (f"Bu bülten açılış öncesi olarak planlandı ancak {now:%H:%M} itibarıyla piyasa durumu: "
                f"{state.replace('_',' ')}. İleriye dönük yorumlar geçerliliğini yitirmiş olabilir.")
    MODE = "intraday" if state == "seans_ici" else "postclose"
if REQ == "postclose" and state == "seans_ici":
    MISMATCH = (f"Bu bülten kapanış sonrası olarak planlandı ancak seans halen açık ({now:%H:%M}). "
                f"Veriler gün içi son bara aittir, kapanış kesinleşmemiştir.")
    MODE = "intraday"

px, vol, hi, lo = engine.fetch()
df, bench, bseries = engine.build(px, vol, hi, lo)
last_bar = bseries.index[-1].strftime("%d.%m.%Y")
sig = engine.screen(df)

# ---------- chart ----------
def chart():
    sec = df.groupby("sector").agg(r1=("r1","mean"), n=("r1","size"))
    sec = sec[sec.n >= 2].sort_values("r1")
    fig, ax = plt.subplots(figsize=(7.4, max(3.4, .3*len(sec))), dpi=100)
    col = ["#2e8b57" if v >= 0 else "#c0392b" for v in sec.r1]
    ax.barh(sec.index, sec.r1, color=col)
    for i,(v,n) in enumerate(zip(sec.r1, sec.n)):
        ax.text(v+(.03 if v>=0 else -.03), i, f"{v:+.2f}%", va="center",
                ha="left" if v>=0 else "right", fontsize=8)
    ax.axvline(0, color="#555", lw=.8)
    ax.set_title(f"Sektör ortalama günlük değişim — {last_bar}", fontsize=11, fontweight="bold")
    ax.grid(axis="x", ls=":", alpha=.4); ax.tick_params(labelsize=8)
    m = max(abs(sec.r1.min()), abs(sec.r1.max())) * 1.25 or 1
    ax.set_xlim(-m, m); plt.tight_layout()
    buf = io.BytesIO(); plt.savefig(buf, format="png", bbox_inches="tight"); plt.close()
    buf.seek(0)
    im = Image.open(buf).convert("RGB").quantize(colors=64, method=2)
    o = io.BytesIO(); im.save(o, format="PNG", optimize=True)
    return base64.b64encode(o.getvalue()).decode()

try: CHART = chart()
except Exception as e: CHART = None; print("chart failed:", e)

# ---------- helpers ----------
def sp(v, d=2, suf="%"):
    if v is None or (isinstance(v,float) and np.isnan(v)): return "<span>veri yok</span>"
    c = "#2e8b57" if v > 0 else ("#c0392b" if v < 0 else "#555")
    return f'<span style="color:{c};font-weight:600">{v:+.{d}f}{suf}</span>'

def chips(d, fmt, n=10):
    if d.empty: return '<i style="color:#888">sinyal yok</i>'
    return " ".join(f'<span style="background:#f0f3f7;border:1px solid #d9e0e8;border-radius:5px;'
        f'padding:2px 7px;margin:2px;display:inline-block;font-size:12.5px">'
        f'<b>{s}</b> {fmt(r)}</span>' for s, r in d.head(n).iterrows())

def tbl(d, cols, hdrs):
    h = "".join(f'<th style="text-align:right;padding:5px 8px;background:#f0f3f7;'
                f'font-size:11.5px;color:#445">{x}</th>' for x in hdrs)
    rows = ""
    for s, r in d.iterrows():
        cells = f'<td style="padding:5px 8px;text-align:left"><b>{s}</b></td>'
        for c, dd, isp in cols:
            v = r[c]
            cells += (f'<td style="padding:5px 8px;text-align:right">'
                      f'{sp(v,dd) if isp else (f"{v:,.2f}" if isinstance(v,(int,float,np.floating)) else v)}</td>')
        rows += f'<tr style="border-bottom:1px solid #eef1f5">{cells}</tr>'
    return (f'<table style="border-collapse:collapse;width:100%;font-size:12.5px">'
            f'<tr><th style="text-align:left;padding:5px 8px;background:#f0f3f7;font-size:11.5px;color:#445">Hisse</th>'
            f'{h}</tr>{rows}</table>')

up = df.nlargest(8, "r1"); dn = df.nsmallest(8, "r1")
volup = df.nlargest(8, "vol20"); hot = df.nlargest(8, "relvol")
tlv = df.nlargest(10, "tl_vol")
adv, dec = int((df.r1 > 0).sum()), int((df.r1 < 0).sum())
breadth = adv / max(adv + dec, 1) * 100
sec_agg = df.groupby("sector").agg(n=("r1","size"), r1=("r1","mean"), r20=("r20","mean"),
    ytd=("ytd","mean"), vol20=("vol20","mean"), relvol=("relvol","mean"))
sec_agg = sec_agg[sec_agg.n >= 2].sort_values("r1", ascending=False)

PRE = MODE == "preopen"
title = {"preopen": "Açılış Öncesi", "intraday": "Seans İçi",
         "postclose": "Kapanış Sonrası"}[MODE]
icon = {"preopen": "🌅", "intraday": "🕐", "postclose": "🌆"}[MODE]
WARN = (f'<div style="background:#fff4e5;border:1px solid #ffb547;border-left:4px solid #ff9800;'
        f'border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:13px">'
        f'<b>⚠️ Zamanlama uyarısı</b><br>{MISMATCH}</div>') if MISMATCH else ""

# ---------- narrative ----------
def narrative():
    b1, b20, byt = bench[1], bench[20], bench["ytd"]
    t = []
    dirn = "yükselişle" if b1 > 0 else "düşüşle"
    t.append(f"XU100 son seansı <b>{bseries.iloc[-1]:,.2f}</b> ({sp(b1)}) ile {dirn} tamamladı. "
             f"Piyasa genişliği {adv} yükselen / {dec} düşen (%{breadth:.0f} pozitif). "
             f"20 günlük endeks getirisi {sp(b20)}, yılbaşından bu yana {sp(byt)}.")
    hi_s, lo_s = sec_agg.index[0], sec_agg.index[-1]
    t.append(f"Sektör bazında <b>{hi_s}</b> ({sp(sec_agg.r1.iloc[0])}) en güçlü, "
             f"<b>{lo_s}</b> ({sp(sec_agg.r1.iloc[-1])}) en zayıf grup oldu. "
             f"20 günlük bazda liderlik <b>{sec_agg.r20.idxmax()}</b> ({sp(sec_agg.r20.max())}) tarafında.")
    nh, nv = len(sig["hacim_patlaması"]), len(sig["volatilite_genişlemesi"])
    if nh or nv:
        t.append(f"Tarama {nh} isimde hacim patlaması (≥2x ortalama), {nv} isimde volatilite rejimi "
                 f"genişlemesi işaretledi. Kırılım/destek kaybı sinyalleri: "
                 f"{len(sig['direnç_kırılımı'])} yukarı, {len(sig['destek_kaybı'])} aşağı.")
    if PRE:
        t.append("Aşağıdaki seviyeler bir önceki kapanışa göre hesaplanmıştır; açılışta gap "
                 "durumunda destek/direnç mesafeleri yeniden değerlendirilmelidir.")
    return "".join(f"<p style='margin:9px 0'>{x}</p>" for x in t)

img = (f'<p><img src="data:image/png;base64,{CHART}" alt="Sektör değişim" '
       f'style="max-width:100%;height:auto;border:1px solid #e3e8ee;border-radius:6px"/></p>') if CHART else ""

html = f"""<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:760px;color:#1a1f26;line-height:1.55">
<h2 style="margin-bottom:2px">{icon} BIST 100 Bülteni — {title}</h2>
<div style="color:#6b7684;font-size:12.5px;margin-bottom:16px">{datestr} · Veri: {last_bar} · 100 hisse · Üretim: {now:%H:%M} TSİ</div>
{WARN}

<table style="border-collapse:collapse;margin-bottom:14px">
<tr><td style="padding:4px 14px 4px 0;color:#6b7684;font-size:12.5px">XU100</td>
<td style="padding:4px 0"><b style="font-size:16px">{bseries.iloc[-1]:,.2f}</b></td></tr>
<tr><td style="padding:4px 14px 4px 0;color:#6b7684;font-size:12.5px">1G / 5G / 20G</td>
<td style="padding:4px 0">{sp(bench[1])} &nbsp; {sp(bench[5])} &nbsp; {sp(bench[20])}</td></tr>
<tr><td style="padding:4px 14px 4px 0;color:#6b7684;font-size:12.5px">Yılbaşından</td>
<td style="padding:4px 0">{sp(bench['ytd'])}</td></tr>
<tr><td style="padding:4px 14px 4px 0;color:#6b7684;font-size:12.5px">Genişlik</td>
<td style="padding:4px 0"><b>{adv}</b> yükselen / <b>{dec}</b> düşen (%{breadth:.0f})</td></tr>
</table>

<h3 style="font-size:14.5px;color:#2c3e50;border-bottom:1px solid #e3e8ee;padding-bottom:5px">Değerlendirme</h3>
{narrative()}
{img}

<h3 style="font-size:14.5px;color:#2c3e50;border-bottom:1px solid #e3e8ee;padding-bottom:5px">🎯 Tarama Sinyalleri</h3>
<p style="margin:8px 0"><b style="font-size:12.5px">🔊 Hacim patlaması (≥2x 20g ort.)</b><br>
{chips(sig['hacim_patlaması'], lambda r: f"{r.relvol:.1f}x · {r.r1:+.1f}%")}</p>
<p style="margin:8px 0"><b style="font-size:12.5px">⚡ Volatilite genişlemesi (rejim ≥1.4, vol ≥30%)</b><br>
{chips(sig['volatilite_genişlemesi'], lambda r: f"{r.vol20:.0f}% · rejim {r.vol_regime:.2f}")}</p>
<p style="margin:8px 0"><b style="font-size:12.5px">🚀 20 günlük direnç kırılımı</b><br>
{chips(sig['direnç_kırılımı'], lambda r: f"{r.r1:+.1f}% · {r.close:.2f}")}</p>
<p style="margin:8px 0"><b style="font-size:12.5px">🔻 20 günlük destek kaybı</b><br>
{chips(sig['destek_kaybı'], lambda r: f"{r.r1:+.1f}% · {r.close:.2f}")}</p>
<p style="margin:8px 0"><b style="font-size:12.5px">📊 Aşırı hareket (|z| ≥ 2.5σ)</b><br>
{chips(sig['aşırı_hareket'], lambda r: f"{r.r1:+.1f}% (z={r.zmove:.1f})")}</p>
<p style="margin:8px 0"><b style="font-size:12.5px">💪 Güçlü relatif güç</b><br>
{chips(sig['güçlü_RS'], lambda r: f"RS {r.rs20:+.1f} · {r.trend}")}</p>
<p style="margin:8px 0"><b style="font-size:12.5px">🩸 Zayıf relatif güç</b><br>
{chips(sig['zayıf_RS'], lambda r: f"RS {r.rs20:+.1f} · {r.trend}")}</p>

<h3 style="font-size:14.5px;color:#2c3e50;border-bottom:1px solid #e3e8ee;padding-bottom:5px">📈 Günün Hareketleri</h3>
<div style="display:flex;gap:16px;flex-wrap:wrap">
<div style="flex:1;min-width:300px"><b style="font-size:12.5px;color:#2e8b57">Yükselenler</b>
{tbl(up, [("close",2,False),("r1",2,True),("r20",1,True),("relvol",2,False)], ["Kapanış","1G","20G","BgHcm"])}</div>
<div style="flex:1;min-width:300px"><b style="font-size:12.5px;color:#c0392b">Düşenler</b>
{tbl(dn, [("close",2,False),("r1",2,True),("r20",1,True),("relvol",2,False)], ["Kapanış","1G","20G","BgHcm"])}</div>
</div>

<h3 style="font-size:14.5px;color:#2c3e50;border-bottom:1px solid #e3e8ee;padding-bottom:5px">🌪️ Volatilite &amp; Hacim</h3>
<b style="font-size:12.5px">En volatil 8 isim (20g yıllıklandırılmış)</b>
{tbl(volup, [("vol20",1,False),("vol_regime",2,False),("r20",1,True),("relvol",2,False)], ["Vol%","Rejim","20G","BgHcm"])}
<br><b style="font-size:12.5px">En yüksek TL hacmi</b>
{tbl(tlv.assign(mn=lambda x: x.tl_vol/1e6), [("mn",1,False),("relvol",2,False),("r1",2,True),("vol20",1,False)], ["Hacim(mn TL)","BgHcm","1G","Vol%"])}

<h3 style="font-size:14.5px;color:#2c3e50;border-bottom:1px solid #e3e8ee;padding-bottom:5px">🏭 Sektör Görünümü</h3>
{tbl(sec_agg, [("n",0,False),("r1",2,True),("r20",1,True),("ytd",1,True),("vol20",1,False),("relvol",2,False)],
     ["Adet","1G","20G","YBB","Vol%","BgHcm"])}

<h3 style="font-size:14.5px;color:#2c3e50;border-bottom:1px solid #e3e8ee;padding-bottom:5px">🧭 Takip Listesi</h3>
{tbl(df.reindex([s for s in ["KCHOL","SAHOL","EREGL","SISE","GEREL"] if s in df.index]),
     [("close",2,False),("r1",2,True),("r20",1,True),("ytd",1,True),("rs20",1,True),
      ("vol20",1,False),("relvol",2,False),("sup20",2,False),("res20",2,False)],
     ["Kapanış","1G","20G","YBB","RS20","Vol%","BgHcm","Dest20","Dir20"])}

<p style="font-size:12px;color:#6b7684;margin-top:20px;border-top:1px solid #e3e8ee;padding-top:10px">
<b>Yöntem:</b> Tüm fiyat, hacim ve türev istatistikler Yahoo Finance günlük OHLCV verisinden hesaplanmıştır
(yfinance). Vol% = 20 günlük yıllıklandırılmış gerçekleşen volatilite. Rejim = 20g vol ÷ 60g vol.
BgHcm = son seans hacmi ÷ 20 gün ortalama hacim. RS20 = 20 günlük XU100'e göre aşırı getiri (puan).
z = günlük getirinin 60 günlük standart sapmaya oranı. Detaylı ve sıralanabilir tablo için
<b>bist_panel.html</b> dosyasına bakınız.<br><br>
<i>Yatırım tavsiyesi değildir.</i></p></div>"""

# ---------- plaintext ----------
def pl(d, f, n=10):
    return ", ".join(f"{s} {f(r)}" for s, r in d.head(n).iterrows()) or "yok"
text = f"""BIST 100 BÜLTENİ — {title.upper()} — {datestr}""" + (f"\n\n!! ZAMANLAMA UYARISI: {MISMATCH}" if MISMATCH else "") + f"""
Veri: {last_bar} kapanışı | 100 hisse

XU100: {bseries.iloc[-1]:,.2f} | 1G {bench[1]:+.2f}% | 5G {bench[5]:+.2f}% | 20G {bench[20]:+.2f}% | YBB {bench['ytd']:+.2f}%
Genişlik: {adv} yükselen / {dec} düşen (%{breadth:.0f} pozitif)

TARAMA SİNYALLERİ
Hacim patlaması (>=2x): {pl(sig['hacim_patlaması'], lambda r: f"{r.relvol:.1f}x")}
Volatilite genişlemesi: {pl(sig['volatilite_genişlemesi'], lambda r: f"{r.vol20:.0f}%/rejim{r.vol_regime:.2f}")}
Direnç kırılımı: {pl(sig['direnç_kırılımı'], lambda r: f"{r.r1:+.1f}%")}
Destek kaybı: {pl(sig['destek_kaybı'], lambda r: f"{r.r1:+.1f}%")}
Aşırı hareket: {pl(sig['aşırı_hareket'], lambda r: f"{r.r1:+.1f}%(z{r.zmove:.1f})")}
Güçlü RS: {pl(sig['güçlü_RS'], lambda r: f"{r.rs20:+.1f}")}
Zayıf RS: {pl(sig['zayıf_RS'], lambda r: f"{r.rs20:+.1f}")}

YÜKSELENLER: {pl(up, lambda r: f"{r.r1:+.2f}%")}
DÜŞENLER: {pl(dn, lambda r: f"{r.r1:+.2f}%")}
EN VOLATİL: {pl(volup, lambda r: f"{r.vol20:.0f}%")}
EN YÜKSEK TL HACMİ: {pl(tlv, lambda r: f"{r.tl_vol/1e6:.0f}mn")}

SEKTÖRLER (1G ort.): {", ".join(f"{i} {r.r1:+.2f}%" for i, r in sec_agg.iterrows())}

TAKİP LİSTESİ
""" + "\n".join(
    f"{s}: {r.close:.2f} TL | 1G {r.r1:+.2f}% | 20G {r.r20:+.1f}% | YBB {r.ytd:+.1f}% | "
    f"RS20 {r.rs20:+.1f} | Vol {r.vol20:.0f}% | BgHcm {r.relvol:.2f}x | Dest {r.sup20:.2f} / Dir {r.res20:.2f}"
    for s, r in df.reindex([x for x in ["KCHOL","SAHOL","EREGL","SISE","GEREL"] if x in df.index]).iterrows()
) + """

Yöntem: Yahoo Finance günlük OHLCV (yfinance). Detaylı tablo: bist_panel.html
Yatırım tavsiyesi değildir."""

# ---------- write ----------
dash = dashboard.build_html(df, bench, bseries, MODE, now.strftime("%d.%m.%Y %H:%M"))
open(f"{OUT}/bist_panel.html", "w", encoding="utf-8").write(dash)
open(f"{OUT}/email.html", "w", encoding="utf-8").write(html)
open(f"{OUT}/email.txt", "w", encoding="utf-8").write(text)
df.round(3).to_csv(f"{OUT}/bist_data.csv", encoding="utf-8-sig")
json.dump({"subject": f"BIST 100 Bülteni ({title}) — {datestr}", "mode": MODE,
           "requested_mode": REQ, "market_state": state, "mismatch": MISMATCH,
           "last_bar": last_bar, "xu100": float(bseries.iloc[-1]), "r1": float(bench[1]),
           "adv": adv, "dec": dec, "html_bytes": len(html),
           "signals": {k: list(v.index[:12]) for k, v in sig.items()}},
          open(f"{OUT}/meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("OK", MODE, "| state", state, "| mismatch", bool(MISMATCH), "| email", len(html), "| dash", len(dash), "| chart", bool(CHART))
