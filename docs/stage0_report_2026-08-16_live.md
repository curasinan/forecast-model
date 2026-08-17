# Stage 0 entitlement report

Generated: 2026-08-16T12:40:59.586431+00:00
Tool: scripts/verify_ibkr_entitlements.py

Stage 0: IBKR ForecastEx entitlement verification
Started: 2026-08-16T11:01:30.587092+00:00
Connected: 127.0.0.1:4002 clientId=3

## 1. Session and account facts

Managed accounts: ['U0000000']
Server version: 178
Account modes (by prefix): {'U0000000': 'live'}
  U0000000  AccountType = INDIVIDUAL 
  U0000000  AccruedCash = 0.00 USD
  U0000000  AvailableFunds = 20.88 USD
  U0000000  NetLiquidation = 20.88 USD
  U0000000  TotalCashValue = 20.88 USD

NOTE: the servicing IBKR legal entity is not exposed via the API.
Confirm it in Client Portal (Settings -> Account Settings) and
record it in docs/venue_semantics.md. Turkey-resident accounts are
typically held by a non-US entity; product availability follows the
entity, which is exactly what the probes below measure.

## 2. Contract discovery (multi-probe, nothing hardcoded)

Probe A: reqContractDetails wildcards per (exchange, secType)
Probe B: reqMatchingSymbols on seed terms
Probe C: reqScannerParameters scan for prediction-market codes
  scanner instrument codes matching prediction markets: none
Probe D: derivative chains for 64 underlyings
  ACD: 70 derivative contracts
  CAGDP: 72 derivative contracts
  CAREC: 8 derivative contracts
  CAUNR: 0 derivative contracts
  CPI: 0 derivative contracts
  CPIC: 0 derivative contracts
  CPIGE: 0 derivative contracts
  CPIIN: 0 derivative contracts
  CPIJP: 0 derivative contracts
  CPISP: 0 derivative contracts
  CPIY: 0 derivative contracts
  DISSA: 0 derivative contracts
  DISSN: 0 derivative contracts
  EZGDP: 0 derivative contracts
  EZR: 0 derivative contracts
  EZUNR: 0 derivative contracts
  FF: 0 derivative contracts
  GCE: 0 derivative contracts
  GDP: 0 derivative contracts
  GDPAU: 54 derivative contracts
  GDPGE: 36 derivative contracts
  GDPJP: 90 derivative contracts
  GDQCA: 104 derivative contracts
  GDQFL: 112 derivative contracts
  GDQGA: 88 derivative contracts
  GDQIL: 120 derivative contracts
  GDQNJ: 120 derivative contracts
  GDQNY: 104 derivative contracts
  GDQOH: 112 derivative contracts
  GDQPA: 112 derivative contracts
  GDQTX: 120 derivative contracts
  GDQWA: 88 derivative contracts
  GT: 122 derivative contracts
  GTM: 122 derivative contracts
  GTTA: 38 derivative contracts
  GTTM: 26 derivative contracts
  HCAB: 14 derivative contracts
  HCAT4: 8 derivative contracts
  HKGDP: 178 derivative contracts
  HLF: 30 derivative contracts
  HLFRE: 0 derivative contracts
  INFLM: 0 derivative contracts
  MACD: 0 derivative contracts
  MHCMA: 0 derivative contracts
  RCATL: 0 derivative contracts
  RCBOS: 0 derivative contracts
  RCCHI: 0 derivative contracts
  RCDAL: 0 derivative contracts
  RCDET: 0 derivative contracts
  RCLAX: 0 derivative contracts
  RCMWE: 0 derivative contracts
  RCNET: 0 derivative contracts
  RCNYC: 0 derivative contracts
  RCPAC: 0 derivative contracts
  RCSAL: 0 derivative contracts
  RCSEA: 16 derivative contracts
  RCSTH: 148 derivative contracts
  RCWDC: 0 derivative contracts
  RCWST: 0 derivative contracts
  RGDP: 234 derivative contracts
  SGGDP: 204 derivative contracts
  UNR: 122 derivative contracts
  USCE: 220 derivative contracts
  USR: 8 derivative contracts

Responding (exchange, secType) combinations: NONE
Distinct contracts discovered (lower bound): 2964
  of which tradable (non-IND): 2900

## 3. Raw contract specifications (first 3+, resolved via reqContractDetails)

Contract conId=573031111:
    conId = 573031111
    symbol = 'GDP'
    localSymbol = 'GDP'
    secType = 'IND'
    exchange = 'FORECASTX'
    currency = 'USD'
    minTick = 0.001
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALLOC,BASKET,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,LMT,NONALGO,OCA,SCALE,SCALERST,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'US Gross Domestic Product'
    timeZoneId = 'US/Central'

Contract conId=573031117:
    conId = 573031117
    symbol = 'UNR'
    localSymbol = 'UNR'
    secType = 'IND'
    exchange = 'FORECASTX'
    currency = 'USD'
    minTick = 0.001
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALLOC,BASKET,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,LMT,NONALGO,OCA,SCALE,SCALERST,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'US Unemployment Rate'
    timeZoneId = 'US/Central'

Contract conId=573031126:
    conId = 573031126
    symbol = 'CPI'
    localSymbol = 'CPI'
    secType = 'IND'
    exchange = 'FORECASTX'
    currency = 'USD'
    minTick = 0.01
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALLOC,BASKET,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,LMT,NONALGO,OCA,SCALE,SCALERST,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'US Consumer Price Index'
    timeZoneId = 'US/Central'

Contract conId=658663572:
    conId = 658663572
    symbol = 'FF'
    localSymbol = 'FF.X'
    secType = 'IND'
    exchange = 'FORECASTX'
    currency = 'USD'
    minTick = 0.001
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALLOC,BASKET,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,LMT,NONALGO,OCA,SCALE,SCALERST,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'US Fed Funds Target Rate'
    timeZoneId = 'US/Central'

Contract conId=712856682:
    conId = 712856682
    symbol = 'CPIY'
    localSymbol = 'CPIY'
    secType = 'IND'
    exchange = 'FORECASTX'
    currency = 'USD'
    minTick = 0.01
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALLOC,BASKET,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,LMT,NONALGO,OCA,SCALE,SCALERST,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'US Consumer Price Index Yearly'
    timeZoneId = 'US/Central'

... plus 2959 more (full list in the JSON snapshot)

## 4. Yes/No structure and price-sum check

Tradable contracts per (symbol, expiry, strike) group -- distinct sizes: [2]
  (2 => Yes and No are separate conIds; 1 => one contract, two sides)
Distinct 'right' values across tradable contracts: ['C', 'P']
Sample group ('ACD', '20270105', 432.0): conIds [712856754, 712856759]
  conId=712856754: right='C' strike=432.0 localSymbol='ACD_1226_432.0_YES' tradingClass='ACD'
  conId=712856759: right='P' strike=432.0 localSymbol='ACD_1226_432.0_NO' tradingClass='ACD'
  quote conId=712856754: bid=0.03 ask=0.06 last=nan close=0.04
  quote conId=712856759: bid=0.94 ask=0.97 last=nan close=0.96
  Sum of the two ASK prices: 1.0300 (spec expects ~1.01 on ForecastEx; verify)
  Sum of the two BID prices: 0.9700

## 5. Order-type probes (whatIf only -- nothing is transmitted)

Venue-declared orderTypes for conId=712856754: 'ACTIVETIM,AD,ADJUST,ALERT,ALGO,AVGCOST,BASKET,COND,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,IOC,LIT,LMT,MIT,MKT,MTL,NGCOMB,NONALGO,OCA,PEGBENCH,SCALE,SCALERST,SNAPMID,SNAPMKT,SNAPREL,STP,STPLMT,TRAIL,TRAILLIT,TRAILLMT,TRAILMIT,WHATIF'
  LMT: REJECTED/UNKNOWN  (10349: Order TIF was set to DAY based on order preset.)
     If this says read-only: uncheck 'Read-Only API' in the
     Gateway/TWS API settings and rerun. whatIf never transmits.
  MKT: REJECTED/UNKNOWN  (10349: Order TIF was set to DAY based on order preset.)
     If this says read-only: uncheck 'Read-Only API' in the
     Gateway/TWS API settings and rerun. whatIf never transmits.
  LMT+PriceCondition: REJECTED/UNKNOWN  (10349: Order TIF was set to DAY based on order preset.)
     If this says read-only: uncheck 'Read-Only API' in the
     Gateway/TWS API settings and rerun. whatIf never transmits.

## 6. Market depth availability

  FORECASTX  secType=FOP listingExch= aggGroup=2147483647
  FORECASTX  secType=OPT listingExch= aggGroup=2147483647

## 7. Verdict

PASS: 2964 contracts discovered (2900 tradable); order probes accepted: []

Snapshot written: data\ibkr_universe_live.json

## Appendix: API errors/warnings observed

- reqId=102 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=103 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=104 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=105 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=106 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=107 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=108 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=109 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=110 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=111 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=112 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=113 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=114 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=115 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=195 code=200: No security definition has been found for the request
- reqId=196 code=200: No security definition has been found for the request
- reqId=197 code=200: No security definition has been found for the request
- reqId=199 code=200: No security definition has been found for the request
- reqId=201 code=200: No security definition has been found for the request
- reqId=203 code=200: No security definition has been found for the request
- reqId=205 code=200: No security definition has been found for the request
- reqId=207 code=200: No security definition has been found for the request
- reqId=209 code=200: No security definition has been found for the request
- reqId=211 code=200: No security definition has been found for the request
- reqId=213 code=200: No security definition has been found for the request
- reqId=215 code=200: No security definition has been found for the request
- reqId=217 code=200: No security definition has been found for the request
- reqId=219 code=200: No security definition has been found for the request
- reqId=221 code=200: No security definition has been found for the request
- reqId=223 code=200: No security definition has been found for the request
- reqId=224 code=200: No security definition has been found for the request
- reqId=225 code=200: No security definition has been found for the request
- reqId=247 code=200: No security definition has been found for the request
- reqId=248 code=200: No security definition has been found for the request
- reqId=250 code=200: No security definition has been found for the request
- reqId=252 code=200: No security definition has been found for the request
- reqId=254 code=200: No security definition has been found for the request
- reqId=256 code=200: No security definition has been found for the request
- reqId=258 code=200: No security definition has been found for the request
- reqId=260 code=200: No security definition has been found for the request
- reqId=262 code=200: No security definition has been found for the request
- reqId=264 code=200: No security definition has been found for the request
- reqId=266 code=200: No security definition has been found for the request
- reqId=268 code=200: No security definition has been found for the request
- reqId=270 code=200: No security definition has been found for the request
- reqId=272 code=200: No security definition has been found for the request
- reqId=273 code=200: No security definition has been found for the request
- reqId=274 code=200: No security definition has been found for the request
- reqId=275 code=200: No security definition has been found for the request
- reqId=276 code=200: No security definition has been found for the request
- reqId=280 code=200: No security definition has been found for the request
- reqId=282 code=200: No security definition has been found for the request
- reqId=293 code=10349: Order TIF was set to DAY based on order preset.
- reqId=294 code=10349: Order TIF was set to DAY based on order preset.
- reqId=295 code=10349: Order TIF was set to DAY based on order preset.