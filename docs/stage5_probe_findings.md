# Stage 5 probe findings: historical bars + market depth

Generated: 2026-08-23T19:33:40.617318+00:00
Tool: scripts/probe_ibkr_history.py (read-only, clientId=3).
Answers venue_semantics.md Q4 (depth) and Q7 (historical bars).

## reqMktDepthExchanges (FORECASTX)
- FORECASTX secType=FOP aggGroup=2147483647
- FORECASTX secType=OPT aggGroup=2147483647

## Per-contract probes

### GTM conId=830396294 (GTM_1226_0.5_YES)
- TRADES: headTimeStamp=2026-05-07 21:19:10 | 1M/1day -> 0 daily bars, first=None, last=None
- MIDPOINT: headTimeStamp=2026-01-17 06:33:42 | 1M/1day -> 0 daily bars, first=None, last=None
- BID_ASK: headTimeStamp=2026-01-17 06:33:42 | 1M/1day -> 0 daily bars, first=None, last=None
- BID_ASK 2D/1hour -> 48 bars
- depth: bid_levels=1 ask_levels=1 top_bids=[(0.86, 10.0)]

### RGDP conId=712857395 (RGDP_Q430_1.8_YES)
- TRADES: headTimeStamp=[] | 1M/1day -> 0 daily bars, first=None, last=None
- MIDPOINT: headTimeStamp=2026-01-17 06:34:34 | 1M/1day -> 0 daily bars, first=None, last=None
- BID_ASK: headTimeStamp=2026-01-17 06:34:34 | 1M/1day -> 0 daily bars, first=None, last=None
- BID_ASK 2D/1hour -> 0 bars
- depth: bid_levels=0 ask_levels=0 top_bids=[]

### UNR conId=753739783 (UNR_1226_3.2_YES)
- TRADES: headTimeStamp=2026-04-22 02:04:57 | 1M/1day -> 0 daily bars, first=None, last=None
- MIDPOINT: headTimeStamp=2026-01-17 06:34:42 | 1M/1day -> 0 daily bars, first=None, last=None
- BID_ASK: headTimeStamp=2026-01-17 06:34:42 | 1M/1day -> 0 daily bars, first=None, last=None
- BID_ASK 2D/1hour -> 48 bars
- depth: bid_levels=1 ask_levels=1 top_bids=[(0.92, 10.0)]

## API errors/warnings observed
- reqId=327 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=329 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=331 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=331 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=335 code=162: Historical Market Data Service error message:Query failed
- reqId=336 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=338 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=340 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=340 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=341 code=162: Historical Market Data Service error message:HMDS query returned no data: RGDP_Q430_1.8_YES@FORECASTX Bid
- reqId=345 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=347 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=349 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'
- reqId=349 code=162: Historical Market Data Service error message:No data of type EODChart is available for the exchange 'FORECASTX' and the security type 'Option' and '1 m' and '1 day'