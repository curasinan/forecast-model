# Third-party notices

The code in this repository is released under the MIT License (see
[`LICENSE`](LICENSE)). No third-party source code is bundled with it. The
packages it installs and the data services it talks to keep their own
licences and terms, listed here for convenience. Each project's own licence
text is the authoritative one.

## Python packages

Licences are the ones each package declares in its metadata.

### Core dependencies

| Package | Licence |
|---|---|
| [pydantic](https://github.com/pydantic/pydantic) | MIT |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | MIT |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause |
| [requests](https://github.com/psf/requests) | Apache-2.0 |
| [feedparser](https://github.com/kurtmckee/feedparser) | BSD-2-Clause |
| [nltk](https://www.nltk.org/) | Apache-2.0 |
| [scikit-learn](https://scikit-learn.org) | BSD-3-Clause |
| [xgboost](https://github.com/dmlc/xgboost) | Apache-2.0 |
| [lightgbm](https://github.com/microsoft/LightGBM) | MIT |
| [numpy](https://numpy.org) | BSD-3-Clause (with bundled 0BSD, MIT, Zlib and CC0-1.0 parts) |
| [pandas](https://pandas.pydata.org) | BSD-3-Clause |
| [scipy](https://scipy.org/) | BSD-3-Clause |
| [joblib](https://joblib.readthedocs.io) | BSD-3-Clause |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | MIT |
| [rich](https://github.com/Textualize/rich) | MIT |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause |
| [PyYAML](https://pyyaml.org/) | MIT |
| [pyarrow](https://arrow.apache.org/) | Apache-2.0 |

### Optional extras

| Extra | Package | Licence |
|---|---|---|
| `dev` | [pytest](https://docs.pytest.org/) | MIT |
| `dev` | [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) | Apache-2.0 |
| `dev` | [ruff](https://docs.astral.sh/ruff) | MIT |
| `web` | [fastapi](https://github.com/fastapi/fastapi) | MIT |
| `web` | [uvicorn](https://uvicorn.dev/) | BSD-3-Clause |
| `web` | [Jinja2](https://github.com/pallets/jinja/) | BSD-3-Clause |
| `trading` | [py-clob-client](https://github.com/Polymarket/py-clob-client) | MIT |
| `ibkr` | [ib_async](https://github.com/ib-api-reloaded/ib_async) | BSD-2-Clause |
| `climate` | [cdsapi](https://github.com/ecmwf/cdsapi) | Apache-2.0 |
| `climate` | [xarray](https://xarray.dev/) | Apache-2.0 |
| `climate` | [netCDF4](https://github.com/Unidata/netcdf4-python) | MIT |
| `mcp` | [mcp](https://modelcontextprotocol.io) | MIT |

`ib_async` connects to Interactive Brokers' TWS or IB Gateway. That software
is proprietary, is not part of this repository, and is governed by IBKR's own
agreements.

The standalone `bist_brief/` tool is not installed by `pyproject.toml`. It
imports [yfinance](https://github.com/ranaroussi/yfinance) (Apache-2.0),
[matplotlib](https://matplotlib.org/) (Matplotlib licence, PSF-based) and
[Pillow](https://python-pillow.github.io/) (MIT-CMU).

## Data sources

The code fetches data from the services below. The raw data is not stored in
this repository; reports, result tables and test fixtures derived from it
are. Anyone running the collectors is bound by each provider's terms.

| Source | Used for | Terms |
|---|---|---|
| [NOAA NCEI](https://www.ncei.noaa.gov/) and the US National Weather Service | Global temperature anomaly labels, daily climate reports | US Government work, public domain in the United States; cite NOAA NCEI |
| [Copernicus Climate Change Service](https://cds.climate.copernicus.eu/) (C3S, operated by ECMWF) | ERA5 reanalysis and SEAS5 seasonal forecasts | The licence shown on each dataset's Climate Data Store page; attribution required (below) |
| [Open-Meteo](https://open-meteo.com/) | Ensemble and previous-run weather forecasts | Data under CC BY 4.0; the free API is for non-commercial use, commercial use needs a subscription or a self-hosted instance (`weather/open_meteo.py` enforces this) |
| [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/), Iowa State University | ASOS/METAR station observations | IEM's usage terms; acknowledge IEM |
| Weather Underground / The Weather Company | Daily high and low labels for some markets | Proprietary; their terms of use apply |
| [Metaculus](https://www.metaculus.com/) | Community forecasts | Metaculus API terms |
| [Polymarket](https://polymarket.com/) | Market metadata, prices and order books | Polymarket terms of service |
| [GDELT Project](https://www.gdeltproject.org/) | News volume | Open for use with citation of the GDELT Project |
| [NewsAPI.org](https://newsapi.org/), GitHub API | Optional evidence collectors | Each service's terms |
| Interactive Brokers / [ForecastEx](https://forecastex.com/) | Contract listings, delayed quotes, paper orders | IBKR customer and market-data agreements |
| Yahoo Finance, through yfinance | `bist_brief/` only | Yahoo's terms of use |

### Copernicus attribution

Model results and reports in this repository (for example
`docs/gtm_model_report.md` and `docs/gtm_bridge_robustness.md`) were
generated using Copernicus Climate Change Service information (2026).
Neither the European Commission nor ECMWF is responsible for any use that
may be made of the Copernicus information or data it contains.

### Open-Meteo attribution

Weather forecast data by [Open-Meteo.com](https://open-meteo.com/), licensed
under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
