# Stage 0 entitlement report

Generated: 2026-09-28T13:33:06.427833+00:00
Tool: scripts/verify_ibkr_entitlements.py

Stage 0: IBKR ForecastEx entitlement verification
Started: 2026-09-28T12:03:30.497016+00:00
Connected: 127.0.0.1:4002 clientId=3

## 1. Session and account facts

Managed accounts: ['DU0000000']
Server version: 178
Account modes (by prefix): {'DU0000000': 'paper'}
  DU0000000  AccountType = INDIVIDUAL 
  DU0000000  AccruedCash = 2125.22 USD
  DU0000000  AvailableFunds = 1002238.08 USD
  DU0000000  NetLiquidation = 1004363.30 USD
  DU0000000  TotalCashValue = 1002238.08 USD

NOTE: the servicing IBKR legal entity is not exposed via the API.
Confirm it in Client Portal (Settings -> Account Settings) and
record it in docs/venue_semantics.md. Non-US-resident accounts are
typically held by a non-US entity; product availability follows the
entity, which is exactly what the probes below measure.

## 2. Contract discovery (multi-probe, nothing hardcoded)

Probe A: reqContractDetails wildcards per (exchange, secType)
Probe B: reqMatchingSymbols on seed terms
Probe C: reqScannerParameters scan for prediction-market codes
  scanner instrument codes matching prediction markets: none
Probe D: derivative chains for 67 underlyings
  ACD: 72 derivative contracts
  CAGDP: 66 derivative contracts
  CAREC: 8 derivative contracts
  CAUNR: 56 derivative contracts
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
  EZR: 8 derivative contracts
  EZUNR: 64 derivative contracts
  FF: 86 derivative contracts
  FFRANGE: 0 derivative contracts
  GCE: 0 derivative contracts
  GDP: 0 derivative contracts
  GDPAU: 54 derivative contracts
  GDPGE: 36 derivative contracts
  GDPJP: 72 derivative contracts
  GDQCA: 106 derivative contracts
  GDQFL: 112 derivative contracts
  GDQGA: 96 derivative contracts
  GDQIL: 120 derivative contracts
  GDQNJ: 120 derivative contracts
  GDQNY: 108 derivative contracts
  GDQOH: 114 derivative contracts
  GDQPA: 114 derivative contracts
  GDQTX: 120 derivative contracts
  GDQWA: 92 derivative contracts
  GT: 122 derivative contracts
  GTM: 98 derivative contracts
  GTTA: 38 derivative contracts
  GTTM: 26 derivative contracts
  HCAB: 14 derivative contracts
  HCAT4: 8 derivative contracts
  HKGDP: 168 derivative contracts
  HLF: 32 derivative contracts
  HLFRE: 0 derivative contracts
  INFLM: 0 derivative contracts
  MACD: 0 derivative contracts
  MHCMA: 24 derivative contracts
  RCATL: 48 derivative contracts
  RCBOS: 52 derivative contracts
  RCCHI: 140 derivative contracts
  RCDAL: 24 derivative contracts
  RCDET: 42 derivative contracts
  RCLAX: 122 derivative contracts
  RCMWE: 108 derivative contracts
  RCNET: 100 derivative contracts
  RCPAC: 0 derivative contracts
  RCSAL: 0 derivative contracts
  RCSEA: 16 derivative contracts
  RCSTH: 108 derivative contracts
  RCWDC: 0 derivative contracts
  RCWST: 0 derivative contracts
  RGDP: 236 derivative contracts
  SGGDP: 186 derivative contracts
  UNR: 104 derivative contracts
  USCE: 220 derivative contracts
  USR: 12 derivative contracts
  ZFFCP: 18 derivative contracts
  ZSENMUSR: 4 derivative contracts
  ZUNRUSR: 8 derivative contracts

Responding (exchange, secType) combinations: NONE
Distinct contracts discovered (lower bound): 3769
  of which tradable (non-IND): 3702

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

... plus 3764 more (full list in the JSON snapshot)

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
  LMT DAY: ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.5' warning=''
  LMT GTC (resting): ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.04999999999999716' warning=''
  MKT DAY: ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.04999999999999716' warning=''
  LMT+PriceCondition: ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.04999999999999716' warning=''

## 6. Market depth availability

  FORECASTX  secType=FOP listingExch= aggGroup=2147483647
  FORECASTX  secType=OPT listingExch= aggGroup=2147483647

## 7. Verdict

PASS: 3769 contracts discovered (3702 tradable); order probes accepted: ['LMT DAY', 'LMT GTC (resting)', 'MKT DAY', 'LMT+PriceCondition']

Snapshot written: data\ibkr_universe_live.json

## Appendix: API errors/warnings observed

- reqId=203 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=204 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=205 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=206 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=207 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=208 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=209 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=210 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=211 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=212 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=213 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=214 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=215 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=216 code=321: Error validating request.-'bF' : cause - The symbol or the local-symbol or the security id must be entered
- reqId=300 code=200: No security definition has been found for the request
- reqId=301 code=200: No security definition has been found for the request
- reqId=303 code=200: No security definition has been found for the request
- reqId=305 code=200: No security definition has been found for the request
- reqId=307 code=200: No security definition has been found for the request
- reqId=309 code=200: No security definition has been found for the request
- reqId=311 code=200: No security definition has been found for the request
- reqId=313 code=200: No security definition has been found for the request
- reqId=315 code=200: No security definition has been found for the request
- reqId=317 code=200: No security definition has been found for the request
- reqId=319 code=200: No security definition has been found for the request
- reqId=323 code=200: No security definition has been found for the request
- reqId=324 code=200: No security definition has been found for the request
- reqId=326 code=200: No security definition has been found for the request
- reqId=327 code=200: No security definition has been found for the request
- reqId=328 code=200: No security definition has been found for the request
- reqId=350 code=200: No security definition has been found for the request
- reqId=351 code=200: No security definition has been found for the request
- reqId=353 code=200: No security definition has been found for the request
- reqId=355 code=200: No security definition has been found for the request
- reqId=365 code=200: No security definition has been found for the request
- reqId=366 code=200: No security definition has been found for the request
- reqId=367 code=200: No security definition has been found for the request
- reqId=368 code=200: No security definition has been found for the request
- reqId=372 code=200: No security definition has been found for the request
- reqId=374 code=200: No security definition has been found for the request