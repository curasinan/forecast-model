# Climate contract resolution-oracle research (full reports)

Verbatim research reports, 2026-08-18. Sources are the venue's own
T&C PDFs at data.forecastex.com/regulatory/ plus the ForecastEx LLC
rulebook (Feb 17, 2026 version) and CFTC filings. The distilled,
load-bearing facts live in docs/venue_semantics.md; this file is the
evidence trail.



---

## Report 1

I have everything needed. Compiling the structured report.

# ForecastEx CARBON family — official settlement/resolution rules

**Method note:** All four contract Terms & Conditions ("T&C") PDFs were retrieved from the venue's own regulatory document store (`data.forecastex.com/regulatory/…`), the same domain ForecastEx uses for Notices to Members. Rulebook text is from ForecastEx LLC's CFTC rulebook filing (Version Date: February 17, 2026). Embedded hyperlinks ("here" links) were extracted from the PDF link annotations to identify the exact underlying data URLs — these are load-bearing because the visible text does not always name the exact series.

---

## Common terms (all four products, identical language)

- Binary $1.00/$0.00 settlement: "The Settlement Value of each Contract is $1.00 or $0.00" — [USCE T&C](https://data.forecastex.com/regulatory/USCETermsandConditions.pdf) (same in all four).
- All four state: "This contract is not subject to early Resolution." (each T&C, Instructions section).
- Delay rule: "ForecastEx will delay the Resolution Time of the Contract until such data is released." (each T&C, Changes in Resolution Time ¶1).
- Settlement: "Settlement of the Contract will occur no later than the day following Expiration Time." (each T&C).
- Rulebook definition (65): "The time when the Outcome of a Forecast Market is determined" (Resolution Time) — [ForecastEx Rulebook, CFTC filing](https://www.cftc.gov/filings/orgrules/rules02022638613.pdf).
- Revision/vintage handling (rulebook, no per-product revision rule exists):
  - Rule 415(c): a "potential error in the data retrieved from the Source Agency" can trigger Event Review before Settlement; the Change Control Oversight Board resolves by unanimous consent, else the Event Review Committee decides and its "determinations … are final."
  - Rule 603(e): "Settlement is irrevocable and unconditional." → **The vintage that counts is the value as published at Resolution Time (first release); later source-agency revisions cannot reopen a settled contract.** This is a synthesis of the two rules — the rulebook has no explicit "which vintage" sentence.
  - Rule 413 (Contract Modifications): if an underlying "is unavailable or undergoes significant modifications," ForecastEx may "designate a new Source Agency and Underlying."

**Strike semantics (all four):** Sample questions use strict inequality — "greater than" (ACD/MACD) or "exceed" (USCE/GCE). Corroborated by a live listed question: "Will Atmospheric Carbon Dioxide be greater than 426.1ppm in 2024?" — [IBKR Campus](https://www.interactivebrokers.com/campus/trading-lessons/practical-use-of-forecast-contracts/). So **YES at strike X pays iff value > X; an exact tie (value = X) resolves NO** — the tie consequence is my inference from the strict wording; no explicit tie rule exists in the T&Cs or rulebook. API C/P (YES/NO) pairs map to the "Yes"/"No" Positions in Payout Criteria.

---

## ACD — Atmospheric Carbon Dioxide (annual)

1. **Underlying series:** NOAA Global Monitoring Laboratory, *Trends in Atmospheric Carbon Dioxide – Mauna Loa*, **annual mean** CO2 at Mauna Loa Observatory. The T&C's "Underlying" hyperlink points to **`https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.txt`** — NOAA's annual-mean file (extracted from the PDF's link annotation; the visible text says only "Trends in Atmospheric Carbon Dioxide – Mauna Loa"). Units per NOAA file header: "mole fraction in dry air, micromol/mol, abbreviated as ppm" — [NOAA GML file](https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.txt). Recent values: 2023 = 421.08, 2024 = 424.61, 2025 = 427.35 ppm. This is the NOAA/GML series, **not** the separate Scripps Keeling-curve series (NOAA uses Scripps only for its uncertainty estimate). No baseline/reference period applies (absolute concentration, single station; Maunakea substituted Nov 2022–Jul 2023 during the eruption outage per NOAA's own notes).
2. **Timing:** "published by the National Oceanic and Atmospheric Administration on the fifth day of January" at 8:00 AM CT (T&C Instructions). Resolution Time: "8:00 AM CT when National Atmospheric and Oceanic Administration publishes the annual" report — [ACD T&C](https://data.forecastex.com/regulatory/ACDTermsandConditions.pdf). So a contract referencing year Y resolves ~Jan 5 of Y+1 — consistent with the API's January expiries 2027–2036 (Jan-2027 expiry ⇒ calendar-2026 annual mean; this mapping is derived, not quoted). Vintage: the annual mean as first posted; NOAA itself warns "data presented for the last year are subject to change" — settlement uses the January first-release under Rule 603(e).
3. **Strikes:** "Will Atmospheric Carbon Dioxide be greater than [number]ppm in [year]?" — strict >; strikes to 0.1 ppm are used in practice (426.1 example); NOAA reports to 0.01 ppm.
4. **Not verified:** whether ForecastEx would use NOAA's revised value if NOAA revises between Jan 5 publication and the 4:00 PM-next-day settlement window (no rule text found); T&C never uses the words "annual mean" in prose — identification rests on the embedded URL.

## MACD — Monthly Atmospheric Carbon Dioxide

1. **Underlying series:** NOAA GML, *Trends in Atmospheric Carbon Dioxide – Mauna Loa*, **monthly** report. T&C's embedded link: **`https://gml.noaa.gov/ccgg/trends/`** (the Trends landing page, not a specific data file). Units: ppm (same NOAA series as ACD).
2. **Timing:** "published … on the fifth day of each month by 8:00 am CT" (T&C Instructions); Resolution Time "8:00 am CT when [NOAA] publishes the monthly Trends in Atmospheric Carbon Dioxide report" — [MACD T&C](https://data.forecastex.com/regulatory/MACDTermsandConditions.pdf). So month M resolves ~5th of M+1.
3. **Strikes:** "Will Monthly Atmospheric Carbon Dioxide be greater than [number] ppm in [month] [year]?" — strict >.
4. **Not verified — material ambiguity:** NOAA's monthly trends page shows both the raw monthly mean and a **de-seasonalized** monthly value. The T&C does not say which column settles the contract, and the link is to the landing page rather than `co2_mm_mlo.txt`. The natural reading is the headline (non-deseasonalized) monthly mean, but this is inference. No live chain exists to cross-check question text. NOAA's most-recent-year monthly values are also preliminary ("subject to change").

## USCE — United States Carbon Dioxide Emissions (annual)

1. **Underlying series:** U.S. Energy Information Administration (EIA), *Annual U.S. Energy-Related Carbon Dioxide Emissions Report*. Exact cell: "found in Table 1, labelled 'Total U.S. energy-related CO2 emissions by sector,'" in the "row labelled 'Total.'" — [USCE T&C](https://data.forecastex.com/regulatory/USCETermsandConditions.pdf). Embedded link: **`https://www.eia.gov/environment/emissions/carbon/`** (EIA's "U.S. Energy-Related Carbon Dioxide Emissions" page). Units: million metric tons of CO2 (sample question: "million metric tons"). Note this is **energy-related** CO2 only (EIA accounting), not EPA's economy-wide GHG Inventory.
2. **Timing:** "published by the US Energy Information Administration ('EIA') annually in April" (T&C); Resolution "4:00 PM CT on the day EIA publishes" the report. EIA's report covering year Y historically appears the following year (current page edition: "U.S. Energy-Related Carbon Dioxide Emissions, 2025" — [EIA](https://www.eia.gov/environment/emissions/carbon/)), so a [Year Y] contract resolves ~April Y+1. **Vintage:** the figure in the report as published that day; EIA's later statistical revisions do not reopen settlement (Rule 603(e)).
3. **Strikes:** "Will US Carbon Dioxide emissions exceed [number] million metric tons in [Year]?" — strict > ("exceed").
4. **Not verified:** the T&C does not state which *edition* of Table 1 maps to which contract year in so many words (derived from the question's "[Year]" + annual April publication); whether the SEDS-based re-benchmarking EIA applies in later editions could ever feed a Rule 415 review.

## GCE — Global Carbon Dioxide Emissions (annual; no live chain)

1. **Underlying series:** **Energy Institute**, *Statistical Review of World Energy* — **not** EIA, not Global Carbon Project. Exact cell: section "Carbon," table "'Carbon Dioxide Emissions From Energy,'" row "'Total World.'" — [GCE T&C](https://data.forecastex.com/regulatory/GCETermsandConditions.pdf). Embedded link: **`https://www.energyinst.org/statistical-review`**. Units: million metric tons ("million metrics tons" [sic] in sample question); scope is CO2 **from energy** per EI's methodology.
2. **Timing:** "published annually, for the previous year, typically in June" (T&C Instructions); Resolution "4:00 PM CT on the day the Energy Institute publishes" the review. Distinctive: **Last Trading Time is "11:59:59 PM CT on December 31st of the referenced year"** — trading halts ~6 months before resolution (unlike ACD/USCE/MACD, where last trading = resolution time).
3. **Strikes:** "Will global carbon dioxide emissions exceed [##,###] million metrics tons in [Year]?" — strict >.
4. **Not verified:** no live chain existed to confirm listed strikes/expiries; whether EI's table row is CO2-only or includes CO2e variants in future editions (EI publishes both; the T&C names the "From Energy" table specifically).

---

## Disagreements / gaps found

- **No third-party source disagreements found**, but note the T&C-vs-rulebook numbering quirk: the T&Cs cite "Rule 413" for early resolution, while the Feb-2026 rulebook's Rule 413 is titled "Contract Modifications" (early-resolution power sits in Rule 413(b)); Rule 414 is "Accelerated Settlement."
- `forecastex.com/contract-specifications/` returned 404; the authoritative documents live at `data.forecastex.com/regulatory/{CODE}TermsandConditions.pdf`. I could not locate a public index page listing ACD/MACD/GCE, so I cannot rule out that a newer T&C version supersedes these files (the MACD PDF metadata shows creation date 2026-07-30, i.e., current).
- Nothing in any document defines behavior for an **exact strike tie**; strict-inequality reading is from question wording only.
- Local copies of all four T&C PDFs and the rulebook are kept outside the repository.

Sources:
- [ACD Contract Terms and Conditions](https://data.forecastex.com/regulatory/ACDTermsandConditions.pdf)
- [MACD Contract Terms and Conditions](https://data.forecastex.com/regulatory/MACDTermsandConditions.pdf)
- [USCE Contract Terms and Conditions](https://data.forecastex.com/regulatory/USCETermsandConditions.pdf)
- [GCE Contract Terms and Conditions](https://data.forecastex.com/regulatory/GCETermsandConditions.pdf)
- [ForecastEx LLC Rulebook, Version Feb 17 2026 (CFTC filing)](https://www.cftc.gov/filings/orgrules/rules02022638613.pdf)
- [NOAA GML Mauna Loa annual mean CO2 file](https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_annmean_mlo.txt)
- [NOAA GML Trends in Atmospheric CO2](https://gml.noaa.gov/ccgg/trends/)
- [EIA U.S. Energy-Related CO2 Emissions](https://www.eia.gov/environment/emissions/carbon/)
- [Energy Institute Statistical Review of World Energy](https://www.energyinst.org/statistical-review)
- [IBKR Campus — Practical Use of Forecast Contracts](https://www.interactivebrokers.com/campus/trading-lessons/practical-use-of-forecast-contracts/)


---

## Report 2

All load-bearing sources are now verified from the venue's own rule text. Here is the structured report.

# ForecastEx Global Temperature Family — Official Settlement Rules

**Primary sources (venue's own T&C PDFs, all live on data.forecastex.com):**
- GT: https://data.forecastex.com/regulatory/GTTermsandConditions.pdf
- GTM: https://data.forecastex.com/regulatory/GTMTermsandConditions.pdf
- GTTA: https://data.forecastex.com/regulatory/GTTATermsandConditions.pdf
- GTTM: https://data.forecastex.com/regulatory/GTTMTermsandConditions.pdf
- Rulebook (v. Feb 17, 2026): https://data.forecastex.com/regulatory/ForecastEx_LLC_Rulebook.pdf
- NOAA underlying (hyperlinks extracted from the T&C PDFs' link annotations): release schedule https://www.ncei.noaa.gov/access/monitoring/dyk/monthly-releases ; GT report https://www.ncei.noaa.gov/access/monitoring/monthly-report/global/202313 ; GTM/GTTA/GTTM tool https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/global/time-series/globe/tavg/land_ocean/1/0/1850-2025

Local copies of the four T&C PDFs, the rulebook and two CFTC filings are kept outside the repository.

## ANSWER TO THE AUDIT'S CRITICAL QUESTION

**The dataset is NOAA (NCEI), not NASA GISTEMP, not ERA5, not Berkeley Earth.** All four contracts name "Source Agency: NOAA" and hyperlink NCEI pages. **The baseline differs by sub-family:**
- **GT and GTM strikes are anomalies vs NOAA's 20th-century average (1901–2000).** GT sample question: "greater than [Celsius # C]([Fahrenheit # F]) above the 20th century average" (GT T&C). Your observed GT strikes 1.0–1.7 °C are consistent with this base (NOAA 2024 annual ≈ +1.29 °C on it), NOT with a pre-industrial base.
- **GTTA and GTTM strikes are anomalies vs pre-industrial (1850–1900), Paris-Agreement style,** computed as NOAA's 1901–2000-based anomaly plus a venue offset: "anomalies above the 1850–1900 average" and "this offset is currently estimated at 0.16°C" (GTTA T&C). The T&C itself states: "NOAA, however, expresses temperature anomalies relative to the 1901–2000 average" (GTTA T&C). The offset "may be updated in the future if NOAA modifies its methodology" (GTTA T&C).

NOAA-side confirmation of base and units, from the Climate at a Glance JSON metadata (https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/global/time-series/globe/tavg/land_ocean/1/0/1850-2025/data.json): title "Global Land and Ocean Average Temperature Departures", units "Degrees Celsius", base period "1901-2000"; values published to two decimals (e.g. 2025-01 = +1.31).

---

## GT — Global Temperature (annual)

1. **Underlying series:** NOAA NCEI **annual Global Climate Report**, "Global Temperatures" section ("report" → "global climate report" → year → "annual"). Global land+ocean average temperature anomaly vs 1901–2000, in °C. "The underlying is reported in Celsius" and "only the Celsius value is used for the purposes of determining resolution" (GT T&C). The °F shown in the question is convenience only.
2. **Timing:** annual value "in the first half of the month of January at 10:00 AM CT" (GT T&C) — i.e., the January-following-year release; Resolution Time = Last Trading Time = Expiration Time = that publication moment (matches your Jan 2027–2036 expiries for years 2026–2035). Delays by NOAA delay resolution (public notice posted). Revisions: no vintage rule beyond "ForecastEx will use the data from the most recent up to date model" (GT T&C) if NOAA changes methodology. "This contract is not subject to early Resolution." (GT T&C). "Settlement of the Contract will occur no later than the day following Expiration Time" (GT T&C).
3. **Strike semantics:** YES iff annual anomaly is **strictly greater than** strike ("be greater than"); binary $1.00/$0.00 payout. No explicit equal-to-strike rule (see Unverified).

## GTM — Monthly Avg Global Temperature

1. **Underlying series:** NOAA NCEI **Climate at a Glance, Global Time Series** tool: "Time Scale should be set to 1month" , start year "1850", "the surface should be set to 'land and ocean'" (GTM T&C). Anomaly in °C vs 1901–2000 (per NOAA metadata above); Celsius value only.
2. **Timing:** "published … in the first half of the month at 10:00 am CT" (GTM T&C) — month M data resolves at the M+1 release per the NCEI release calendar; Resolution = Last Trading = Expiration at publication; settlement no later than next day. Not subject to early resolution. **Vintage:** the T&C has no explicit "initial vs revised" clause, but because resolution occurs at the moment NOAA first publishes the reference month, the **first-published value operationally governs**; the only revision-related clause is the methodology-change/"most recent up to date model" language (see Unverified).
3. **Strike semantics:** YES iff monthly anomaly **strictly greater than** strike; same binary payout.

## GTTA — Annual Temperature Threshold ("by [year]")

1. **Underlying series:** same Climate at a Glance global land+ocean series, "the time scale is 12-Month with the designated month being December" (calendar-year average), start 1850, end "the most recent available" — then **adjusted to the 1850–1900 pre-industrial base via the ~0.16 °C offset** described above. Sample question: "greater than [#] degrees Celsius above preindustrial levels by [year]?" (GTTA T&C).
2. **Timing:** checked at each annual NOAA release (first half of January, 10:00 AM CT); "This contract is subject to early Resolution." (GTTA T&C) — YES can lock in years before expiry under Rule 413(b). **Vintage:** because each check re-plots the full 1850–present table, **the current (revised) vintage of all past years counts**, not original prints.
3. **Strike semantics:** cumulative threshold — "resolve to Yes when any year prior to or including" (GTTA T&C) the listed year "has a value higher than the threshold value" — strictly higher; NO only if no qualifying year has occurred by the final release.

## GTTM — Monthly Temperature Threshold ("any month by end of [year]")

1. **Underlying series:** same tool, "the month is set to All Months" (GTTM T&C), land+ocean, start 1850; same pre-industrial conversion with the ~0.16 °C offset.
2. **Timing:** checkable at every monthly release (first half of each month, 10:00 AM CT); subject to early resolution; Resolution Time is "when NOAA publishes its Average Global Temperature data for any month" exceeding the threshold. Current-vintage table governs, as with GTTA.
3. **Strike semantics:** YES iff **any single month** is strictly greater than the pre-industrial-based threshold on or before the final release for the listed year. **Documented internal inconsistency:** the GTTM Underlying paragraph says "resolve to Yes when any year prior to or including" — the word "year" appears to be carried over from GTTA and conflicts with the sample question's "for any month"; the Sample Question and Resolution Time both say monthly. Report this as a venue drafting discrepancy, not resolved by any other text in the document.

## Cross-cutting rulebook provisions (Rulebook, Feb 17, 2026 version)

- **Rule 413(a):** if the source degrades, "the Exchange retains the authority to designate a new Source Agency" — replacement must be "objective and verifiable".
- **Rule 413(b):** early resolution when outcome determined before Resolution Time (applies to GTTA/GTTM only).
- **Rule 414:** if the question cannot be answered (e.g., dataset discontinued), ForecastEx may accelerate settlement and pay out at "the most recent last prices" — a real tail risk for 2031–2046 expiries.
- **Rule 415:** event review can delay settlement; "The determinations by the Event Review Committee are final."

## NOT VERIFIED — explicit gaps

1. **Tie at exactly the strike:** no T&C or rulebook text addresses anomaly == strike. Plain reading of "greater than"/"higher than" implies equality resolves NO, but this is my reading, not venue text. NOAA publishes to 0.01 °C and strikes sit on hundredths, so ties are possible.
2. **Dataset build name:** no venue document names "NOAAGlobalTemp." That the CaaG global series is NOAAGlobalTemp (v6, hence the 1850 start) is a high-confidence inference from NOAA documentation conventions, not verified verbatim here.
3. **GTM first-print rule:** "first published value governs" is an operational consequence of the resolution mechanics, not a verbatim clause; the T&C never uses the word "revision."
4. **The exact current offset value (0.16 °C) is venue-estimated** and explicitly mutable; I could not verify how/when ForecastEx recomputes it or whether a notice accompanies a change.
5. **Original CFTC 40.2 self-certifications for GT/GTM/GTTA/GTTM specifically:** not located (the Jan 2025 filing found is USTM/US Monthly Temperature: https://www.cftc.gov/sites/default/files/filings/ptc/25/01/ptc01012512998.pdf ; the Apr 2026 filing is SHNOR/Hormuz: https://www.cftc.gov/sites/default/files/filings/ptc/26/04/ptc040826884.pdf). The venue-hosted T&Cs above are the operative current terms.
6. **Same-day cash settlement timing (1:00 PM CT if resolved before noon, Rule 603(b)(3))** appears in the SHNOR filing's T&C; the GT-family T&Cs only say "no later than the day following Expiration Time." I did not read Rule 603(b)(3) itself in the rulebook.
7. The annual Global Climate Report page is dynamic and would not render for text extraction; its baseline/units were instead confirmed via the CaaG data.json metadata quoted above.

Sources: [GT T&C](https://data.forecastex.com/regulatory/GTTermsandConditions.pdf), [GTM T&C](https://data.forecastex.com/regulatory/GTMTermsandConditions.pdf), [GTTA T&C](https://data.forecastex.com/regulatory/GTTATermsandConditions.pdf), [GTTM T&C](https://data.forecastex.com/regulatory/GTTMTermsandConditions.pdf), [ForecastEx Rulebook](https://data.forecastex.com/regulatory/ForecastEx_LLC_Rulebook.pdf), [NOAA CaaG data.json](https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/global/time-series/globe/tavg/land_ocean/1/0/1850-2025/data.json), [NCEI release schedule](https://www.ncei.noaa.gov/access/monitoring/dyk/monthly-releases), [CFTC USTM filing](https://www.cftc.gov/sites/default/files/filings/ptc/25/01/ptc01012512998.pdf), [CFTC SHNOR filing](https://www.cftc.gov/sites/default/files/filings/ptc/26/04/ptc040826884.pdf)


---

## Report 3

All verification complete. I have the venue's own T&C PDFs, the rulebook, and the live IBKR ForecastTrader rules endpoint that links each listed product to its governing document. Final report:

# ForecastEx Hurricane Contract Family — Official Settlement Rules

**Governing documents** (venue's own, hosted at `data.forecastex.com/regulatory/`, confirmed as the `market_rules_link` served by IBKR's own `tws.proxy/public/forecasttrader/contract/rules` endpoint per live conid):

| API symbol | Live market name | Governing T&C PDF |
|---|---|---|
| HCAB | Hurricane Count Atlantic Basin | https://data.forecastex.com/regulatory/HCTermsandConditions.pdf |
| HLF | Hurricane Landfall | https://data.forecastex.com/regulatory/HLFTermsandConditions.pdf |
| HCAT4 | Category 4 Hurricane in the US | https://data.forecastex.com/regulatory/HCATTermsandConditions.pdf |
| MHCMA | Atlantic Major Hurricanes (monthly) | https://data.forecastex.com/regulatory/MHCTermsandConditions.pdf |
| TROPA (sibling) | Atlantic Tropical Storms | https://data.forecastex.com/regulatory/TROPTermsandConditions.pdf |
| (sibling, no chain seen) | Regional Hurricane Landfall | https://data.forecastex.com/regulatory/HLFRETermsandConditions.pdf |
| LHL (sibling) | Live Hurricane Location (highest gust) | https://data.forecastex.com/regulatory/LHLTermsandConditions.pdf |

Rulebook: https://data.forecastex.com/regulatory/ForecastEx_LLC_Rulebook.pdf (also filed with CFTC, e.g. https://www.cftc.gov/filings/orgrules/rules02022638613.pdf).

---

## Answer to the critical cross-cutting question: whose count settles

**NOAA/NHC operational advisories — NOT post-season HURDAT2 reanalysis.** Every hurricane T&C names Source Agency "NOAA" and instructs resolution from the NHC Tropical Cyclone Archive **public advisories** ("Under the 'Public Advisory' column, use the storm's advisory history" — HC T&C). HURDAT2 / best-track reanalysis is mentioned nowhere in any of these documents. TROP makes the vintage explicit: "Revisions to the Underlying published after Contract expiration will not affect Contract resolution" (TROP T&C). HC/HLF/HCAT/MHC lack that exact sentence, but the advisory-based methodology plus Rulebook Rule 603(e) ("Settlement is irrevocable and unconditional") and Rule 415 (pre-settlement Event Review for "a potential error in the data") mean post-settlement reanalysis cannot change outcomes. Data errors caught **before** settlement go to the Change Control Oversight Board / Event Review Committee, whose determinations "are final and not subject to review."

---

## 1. HCAB — Hurricane Count Atlantic Basin (product family "HC")

- **Event question**: "Will at least [# of hurricanes] hurricane(s) form in the [Region] in [Year]?" (HC T&C). Product code = `HC` + two-letter region; "the Product code would be HCAB" for Atlantic Basin. Regions: Atlantic, East Pacific, Central Pacific — "The regions are based on NOAA's reporting."
- **Underlying series**: count of storms "classified as Category 1 or higher (sustained winds of at least 64 knots)" that form in the region during the **contract year**, determined from NOAA's NHC advisory archive (basin tab per year → per-storm Public Advisory history). Units: integer count of hurricanes. IBKR's rules endpoint additionally points `data_and_resolution_link` at https://www.ncei.noaa.gov/access/monitoring/monthly-report/tropical-cyclones (NCEI monthly tropical cyclone report) while the T&C describes the NHC advisory archive — minor pointer disagreement, T&C governs.
- **Window**: calendar/contract year, **not** the Jun 1–Nov 30 official season. Resolution Time: "December 31, [year] at 11:59:59 PM CT."
- **Strike semantics**: **inclusive** — "at least [#]"; live strike labels are "At Least 1/3/5/7/9…" So YES at strike X ⟺ count ≥ X.
- **Timing/revisions**: subject to **early resolution** (Rule 413(b)) once count ≥ X is reached. "Settlement of the Contract will occur no later than the day following Expiration Time"; Rulebook 603(b)(3): daily settlement at 13:00 CT.
- **Definition nuance**: "hurricane" = Cat 1+ at any point per advisories; named storms below 64 kt do **not** count (those are the separate TROP product, ≥34 kt / 39 mph).

## 2. HLF — Hurricane Landfall

- **Event question**: "Will there be a Major Hurricane Landfall within [Region] in [year]?" — **binary per region, not a count**. **Correction to the API-side anchor**: the OPT "strikes" encode **regions, not counts**. Live Dec'26 chain (15 strikes): Florida, Texas, North Carolina, Louisiana, Harris TX, Broward FL, Palm Beach FL, Miami-Dade FL, Hillsborough FL, Lee FL, The Bahamas, Cuba, Mexico, Dominican Republic, Honduras. So HLF is **not US-only** — foreign countries are listed regions.
- **What qualifies**: "Category 3 or higher (sustained winds of at least 96 knots)" **at the time of landfall**; "A storm must be a Category 3 or higher hurricane at the time of landfall."
- **Landfall definition**: "the intersection of the hurricane's center of circulation (eye) with land"; explicitly, "If the hurricane's eye remains offshore… this does not constitute landfall."
- **Geographic tolerance (load-bearing)**: YES if landfall is within the region **"or within 50 miles (straight-line distance) of any border"** of the region — e.g., a Georgia landfall 40 mi from the Florida line settles "Florida" YES.
- **No-advisory fallback**: "determined by interpolating between the final advisory issued before landfall and the first advisory issued after landfall."
- **Window/timing**: contract year; Resolution "December 31, [year] at 11:59:59 PM CT"; early resolution allowed; settle ≤ 1 day after expiry.

## 3. HCAT4 — Category 4 Hurricane in the US (family "HCAT")

- **Event question**: "Will any Category [#] hurricane make landfall in the United States by [date]?" Product code `HCAT[one digit]`. Live strikes are **dates** ("By Sep 30, 2026", etc.; 4 strikes, expiries spanning 2026–2027 per your discovery).
- **Exact-category trap (load-bearing)**: "Hurricanes of a higher or lower category do not qualify." A Cat 5 US landfall does **not** resolve HCAT4 YES. Category is Saffir-Simpson **at the time its surface center made landfall** (IBKR description: Cat 4 = "maximum sustained winds of 130-156 mph"). Multiple US landfalls: "any landfall meeting the specified category qualifies."
- **Timing semantics**: "The timing of the landfall, rather than the publication time of the advisory" determines whether it beat the date. Early resolution: "ForecastEx may resolve the contract based on the first official NHC advisory" confirming a qualifying landfall.
- **Resolution Time**: "11:59 PM CT the last day of the period listed in the Contract."
- **NOT verified**: "United States" is nowhere defined (continental vs. Puerto Rico/territories). The T&C is silent; do not assume either way.

## 4. MHCMA — Atlantic Major Hurricanes, monthly (family "MHC")

- **Decode**: product code is `MHC[M/Y][one character region code]` → MHCMA = **M**onthly + **A**tlantic. It is *not* "Miami." Yearly variant would be MHCY_.
- **Event question**: "Will the number of major hurricanes in [region] exceed [##] in [date]?" Live Aug'26 strikes: "Above 0", "Above 1", "Above 2".
- **Strike semantics**: **strict** — "exceed [##]" / "Above N" ⟹ YES ⟺ count > N (contrast HCAB's inclusive "At Least"). Same strict "Above N" applies to TROP.
- **Underlying**: hurricanes attaining "Category 3 or higher intensity (maximum sustained winds of at least 96 knots (111 mph))" in the region during the contract period, per NHC records. **Monthly attribution rule**: "Each hurricane is counted only once, at the time it first attains Category 3" — a storm first reaching Cat 3 Aug 31 and peaking in Sept counts for August only.
- **Disagreement found**: IBKR's live `contract/rules` description for MHCMA says Cat 3+ is "maximum sustained winds of at least 64 knots (74 mph)" — internally wrong (64 kt is Cat 1). The governing T&C says 96 kt. Venue T&C controls.
- **Timing**: Resolution "11:59 PM CT the last day of the period"; early resolution allowed; settle ≤ 1 day later.

## Family-wide settlement mechanics

- Binary $1.00/$0.00 payout per Yes/No pair; min tick $0.01; position accountability 250,000 contracts.
- Rulebook 603(b)(3): settlement runs "at 13:00 CST each day" for contracts resolved before 12:00 CST; resolution after noon settles next day's cycle.
- Rule 413(a): if the Source Agency becomes unreliable/unavailable, ForecastEx may "designate a new Source Agency and Underlying" (posted to its website).
- Rule 415: pre-settlement Event Review; committee determinations "are final."
- Contracts pay a monthly incentive coupon (currently 3.13% APY per IBKR).

## Not verified / open items

1. **No hurricane-specific CFTC 40.2 product-submission PDF found** on cftc.gov via search (found sports/Success and TSA filings, plus rulebook filings). The data.forecastex.com T&Cs are the venue's operative public documents; I could not locate the original hurricane certification filing text to diff against.
2. **HCAT "United States" scope** (territories?) — undefined in all sources examined.
3. **Cross-year/cross-basin edge cases** for HCAB (storm forming Dec 31, or crossing E-Pac→Atlantic) — T&C ties both formation and Cat-1 attainment to "during the contract year" and to the basin tab of the NHC archive, but gives no explicit crossover rule.
4. **Explicit revision-vintage clause exists only in TROP**; for HC/HLF/HCAT/MHC the operational-advisory basis is implied by methodology + irrevocable settlement, not stated as a vintage rule.
5. **HLFRE** (county-list regions, no 50-mile buffer, resolves off NOAA "Daily Storm Advisory Reports") is a distinct sibling product — I saw no live chain for it; the 10 named county-defined regions are in its T&C.
6. Live HCAB chain shows `last_trade_time` ≈ Dec 31 12:59 PM CT while the T&C Resolution Time is 11:59:59 PM CT — plausibly an IBKR display/trading-halt artifact; not reconciled.

Sources: [HC T&C](https://data.forecastex.com/regulatory/HCTermsandConditions.pdf), [HLF T&C](https://data.forecastex.com/regulatory/HLFTermsandConditions.pdf), [HCAT T&C](https://data.forecastex.com/regulatory/HCATTermsandConditions.pdf), [MHC T&C](https://data.forecastex.com/regulatory/MHCTermsandConditions.pdf), [TROP T&C](https://data.forecastex.com/regulatory/TROPTermsandConditions.pdf), [HLFRE T&C](https://data.forecastex.com/regulatory/HLFRETermsandConditions.pdf), [LHL T&C](https://data.forecastex.com/regulatory/LHLTermsandConditions.pdf), [ForecastEx Rulebook](https://data.forecastex.com/regulatory/ForecastEx_LLC_Rulebook.pdf), [CFTC rulebook filing](https://www.cftc.gov/filings/orgrules/rules02022638613.pdf), [CFTC 40.2 Success filing (for filing-format reference)](https://www.cftc.gov/sites/default/files/filings/ptc/25/09/ptc09162530506.pdf), IBKR live rules endpoint `https://www.interactivebrokers.com/tws.proxy/public/forecasttrader/contract/rules?conid={conid}` (queried 2026-08-18 for conids 871016074, 871016195, 902537007, 902537764, 902537950).
