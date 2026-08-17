# Stage 0 entitlement report

Generated: 2026-08-16T13:10:31.221581+00:00
Tool: scripts/verify_ibkr_entitlements.py

Stage 0: IBKR ForecastEx entitlement verification
Started: 2026-08-16T13:10:07.811838+00:00
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

Follow-up mode: chains only, for ['FF', 'CPI', 'CPIY', 'INFLM', 'EZR', 'GCE', 'RCNYC', 'DISSA']
  FF: secdef reports NO option chain (likely nothing listed now)
  CPI: secdef reports NO option chain (likely nothing listed now)
  CPIY: secdef reports NO option chain (likely nothing listed now)
  INFLM: secdef reports NO option chain (likely nothing listed now)
  EZR: secdef reports NO option chain (likely nothing listed now)
  GCE: secdef reports NO option chain (likely nothing listed now)
  RCNYC: secdef reports NO option chain (likely nothing listed now)
  DISSA: secdef reports NO option chain (likely nothing listed now)

Distinct contracts discovered (lower bound): 8
  of which tradable (non-IND): 0

## 3. Raw contract specifications (first 3+, resolved via reqContractDetails)

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

Contract conId=712856754:
    conId = 712856754
    symbol = 'ACD'
    localSymbol = 'ACD_1226_432.0_YES'
    secType = 'OPT'
    exchange = 'FORECASTX'
    currency = 'USD'
    tradingClass = 'ACD'
    multiplier = '1'
    lastTradeDateOrContractMonth = '20270105'
    right = 'C'
    strike = 432.0
    marketName = 'ACD'
    minTick = 0.01
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALGO,AVGCOST,BASKET,COND,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,IOC,LIT,LMT,MIT,MKT,MTL,NGCOMB,NONALGO,OCA,PEGBENCH,SCALE,SCALERST,SNAPMID,SNAPMKT,SNAPREL,STP,STPLMT,TRAIL,TRAILLIT,TRAILLMT,TRAILMIT,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'Atmospheric Carbon Dioxide'
    contractMonth = '202612'
    timeZoneId = 'US/Central'
    underConId = 712856705

Contract conId=732764729:
    conId = 732764729
    symbol = 'GCE'
    localSymbol = 'GCE'
    secType = 'IND'
    exchange = 'FORECASTX'
    currency = 'USD'
    minTick = 0.01
    orderTypes = 'ACTIVETIM,AD,ADJUST,ALERT,ALLOC,BASKET,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,LMT,NONALGO,OCA,SCALE,SCALERST,WHATIF'
    validExchanges = 'FORECASTX'
    longName = 'Global Carbon Dioxide Emissions'
    timeZoneId = 'US/Central'

... plus 4 more (full list in the JSON snapshot)

## 4. Yes/No structure and price-sum check

Tradable contracts per (symbol, expiry, strike) group -- distinct sizes: [1]
  (2 => Yes and No are separate conIds; 1 => one contract, two sides)
Distinct 'right' values across tradable contracts: ['C']
Sample group ('ACD', '20270105', 432.0): conIds [712856754]
  conId=712856754: right='C' strike=432.0 localSymbol='ACD_1226_432.0_YES' tradingClass='ACD'
  quote conId=712856754: bid=0.03 ask=0.06 last=nan close=0.04

## 5. Order-type probes (whatIf only -- nothing is transmitted)

Venue-declared orderTypes for conId=712856754: 'ACTIVETIM,AD,ADJUST,ALERT,ALGO,AVGCOST,BASKET,COND,CONDORDER,DAY,DEACT,DEACTDIS,DEACTEOD,GAT,GTC,GTD,GTT,HID,IOC,LIT,LMT,MIT,MKT,MTL,NGCOMB,NONALGO,OCA,PEGBENCH,SCALE,SCALERST,SNAPMID,SNAPMKT,SNAPREL,STP,STPLMT,TRAIL,TRAILLIT,TRAILLMT,TRAILMIT,WHATIF'
  LMT DAY: ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.05' warning=''
  LMT GTC (resting): ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.05' warning=''
  MKT DAY: ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.05' warning=''
  LMT+PriceCondition: ACCEPTED  commission=0.01 minCommission=1.7976931348623157e+308 maxCommission=1.7976931348623157e+308 commissionCurrency='USD' initMarginChange='0.05' warning=''

## 6. Market depth availability

  FORECASTX  secType=FOP listingExch= aggGroup=2147483647
  FORECASTX  secType=OPT listingExch= aggGroup=2147483647

## 7. Verdict

PASS: 9 contracts discovered (1 tradable); order probes accepted: ['LMT DAY', 'LMT GTC (resting)', 'MKT DAY', 'LMT+PriceCondition']

Snapshot written: data\ibkr_universe_live_followup.json

## Appendix: API errors/warnings observed
