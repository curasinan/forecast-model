"""The GTM benchmarks: the seasonal and nowcast protocols, their summaries and win rules."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from climate.dataset import build_frame_from_inputs
from climate.emos import (
    expanding_window_predictions,
    expanding_window_scores,
    summarise_scores,
)
from climate.nn.availability import genuine_first_print_months, seas5_available_on
from climate.nn.baselines import (
    bridge_nowcast,
    emos_baseline,
    persistence,
    raw_ensemble,
    trend_climatology,
)
from climate.nn.features import SEASONAL_FEATURES
from climate.nn.leakage import LeakageError, assert_no_leakage
from climate.nn.nowcast import CHECKPOINTS, NOWCAST_FEATURES, WIN_CHECKPOINTS
from climate.nn.nowcast_model import GtmNowcastModel
from climate.nn.nowcast_windows import NowcastWindow, daily_coverage, nowcast_windows
from climate.nn.scoring import pit_histograms, summarise, summary_columns
from climate.nn.seasonal_model import VALIDATION_YEARS, GtmNnModel, usable_target_years
from climate.nn.windows import Window, strict_windows

PROTOCOLS: tuple[str, ...] = ("strict", "frozen")
STRICT_MODELS: tuple[str, ...] = ("nn", "emos", "raw", "trend")
FROZEN_MODELS: tuple[str, ...] = ("emos", "trend", "clim")
LEADS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
MIN_TRAIN_YEARS: int = VALIDATION_YEARS + 1
WIN_MIN_LEADS: int = 4
COVER50_BAND: tuple[float, float] = (0.35, 0.65)
COVER90_BAND: tuple[float, float] = (0.75, 0.98)

ROW_COLUMNS = ["test_year", "lead_month", "target_month", "decision_utc", "y_C"]
SEASONAL_REFERENCES = {"skill_vs_trend": "trend", "skill_vs_emos": "emos"}
SUMMARY_COLUMNS = summary_columns("lead_month", SEASONAL_REFERENCES)
SEASONAL_AVAILABILITY = ["avail_seas5_utc"]

NOWCAST_MODELS: tuple[str, ...] = ("nn", "bridge", "persistence")
NOWCAST_ROW_COLUMNS = ["test_year", "target_month", "checkpoint", "decision_utc", "y_C"]
NOWCAST_REFERENCES = {"skill_vs_bridge": "bridge"}
NOWCAST_SUMMARY_COLUMNS = summary_columns("checkpoint", NOWCAST_REFERENCES)
NOWCAST_AVAILABILITY = ["avail_era5_utc", "avail_seas_utc", "avail_prev_label_utc"]
FIRST_PRINT_MIN_ROWS: int = 3


@dataclass
class SeasonalRun:
    """Options of one seasonal run; a frozen run ignores models and scores emos, trend, clim."""

    protocol: str
    models: tuple[str, ...]
    first_test_year: int
    last_test_year: int
    seeds: tuple[int, ...]
    hidden: int
    device: str


@dataclass
class NowcastRun:
    """Options of one nowcast run."""

    models: tuple[str, ...]
    first_test_year: int
    last_test_year: int
    seeds: tuple[int, ...]
    hidden: int
    device: str


def check_models(
    models: tuple[str, ...], allowed: tuple[str, ...] = STRICT_MODELS
) -> tuple[str, ...]:
    """The models of a run; ValueError for none, an unknown name or a name listed twice."""
    if len(models) == 0:
        raise ValueError("models must name at least one model")
    for name in models:
        if name not in allowed:
            raise ValueError(f"unknown model {name!r}; choose from {', '.join(allowed)}")
        if models.count(name) > 1:
            raise ValueError(f"model {name!r} is listed twice")
    return models


def run_seasonal(
    inputs: tuple[pd.DataFrame, pd.Series, pd.Series],
    ledger: pd.DataFrame | None,
    cfg: SeasonalRun,
    *,
    model_dir: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(rows, summary per model and lead, extras); the last window's net is saved to model_dir."""
    if cfg.protocol == "frozen":
        return _run_frozen(inputs, cfg)
    if cfg.protocol == "strict":
        return _run_strict(inputs, ledger, cfg, model_dir)
    raise ValueError(f"unknown protocol {cfg.protocol!r}; choose from {', '.join(PROTOCOLS)}")


def seasonal_win(summary) -> tuple[bool | None, str]:
    """(True, "") when nn wins, (False, why), (None, why) when unpaired; a table may be records."""
    nn, emos = _by_key(summary, "nn", "lead_month"), _by_key(summary, "emos", "lead_month")
    if nn.empty or emos.empty:
        return False, "the summary needs both nn and emos rows"
    unpaired = _unpaired(nn, emos, LEADS, "emos", "rows at lead ")
    if unpaired:
        return None, unpaired
    beats = sum(
        1
        for lead in LEADS
        if lead in nn.index and lead in emos.index and nn.at[lead, "crps"] <= emos.at[lead, "crps"]
    )
    if beats < WIN_MIN_LEADS:
        return False, f"nn beats emos at {beats} of {len(LEADS)} leads"
    for lead in LEADS:
        for name, (low, high) in (("cover50", COVER50_BAND), ("cover90", COVER90_BAND)):
            value = nn.at[lead, name] if lead in nn.index else float("nan")
            if not low <= value <= high:
                return False, f"{name} at lead {lead}"
    return True, ""


def run_nowcast(
    daily_K: pd.Series,
    noaa_C: pd.Series,
    seas5: pd.DataFrame,
    ledger: pd.DataFrame | None,
    cfg: NowcastRun,
    *,
    bridge_through: str | None = None,
    model_dir: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(rows, summary per model and checkpoint, extras); the last window's net goes to model_dir."""
    models = check_models(tuple(cfg.models), NOWCAST_MODELS)
    windows = nowcast_windows(
        daily_K,
        noaa_C,
        seas5,
        ledger,
        first_test_year=cfg.first_test_year,
        last_test_year=cfg.last_test_year,
        bridge_through=bridge_through,
    )
    for window in windows:
        _assert_clean(window, NOWCAST_AVAILABILITY)
    skipped, blocks, last_net, last_window = [], [], None, None
    for window in windows:
        year = window.test_year
        if len(window.test) == 0 or _short_of_years(window, NOWCAST_FEATURES):
            skipped.append(year)
            _progress(year, window, "skipped")
            continue
        block, net = _predict_nowcast_window(window, models, cfg)
        blocks.append(block)
        if net is not None:
            last_net, last_window = net, window
        _progress(year, window, "scored")
    if model_dir is not None and last_net is not None:
        last_net.save(model_dir)
    rows = (
        pd.concat(blocks, ignore_index=True) if blocks else _empty_rows(models, NOWCAST_ROW_COLUMNS)
    )
    genuine = genuine_first_print_months(ledger) if ledger is not None else set()
    protocol = "strict" if bridge_through is None else "frozen"
    saved = None if last_net is None else _saved_model("nowcast", last_window, protocol)
    extras = {
        "protocol": protocol,
        "kind": "nowcast",
        "bridge_through": bridge_through,
        "excluded_rows": int(sum(window.excluded for window in windows)),
        "dropped_gap_rows": int(sum(window.dropped_gap_rows for window in windows)),
        "dropped_gap_rows_train": int(sum(window.dropped_gap_rows_train for window in windows)),
        "daily_coverage": daily_coverage(daily_K),
        "skipped_windows": skipped,
        "pit_hist": pit_histograms(rows, models),
        "saved_model": saved,
        "first_print_summary": _nowcast_summary(rows[rows["target_month"].isin(genuine)], models),
    }
    return rows, _nowcast_summary(rows, models), extras


def nowcast_win(summary, first_print_summary) -> tuple[bool | None, str]:
    """(True, "") when nn wins on both tables, (False, why), (None, why) unpaired; or records."""
    nn, bridge = _by_key(summary, "nn", "checkpoint"), _by_key(summary, "bridge", "checkpoint")
    if nn.empty or bridge.empty:
        return False, "the summary needs both nn and bridge rows"
    unpaired = _unpaired(nn, bridge, WIN_CHECKPOINTS, "bridge", "rows at ")
    if unpaired:
        return None, unpaired
    for checkpoint in WIN_CHECKPOINTS:
        if not _beats(nn, bridge, checkpoint):
            return False, f"nn does not beat bridge at {checkpoint}"
    first_nn = _by_key(first_print_summary, "nn", "checkpoint")
    first_bridge = _by_key(first_print_summary, "bridge", "checkpoint")
    read = [
        checkpoint
        for checkpoint in WIN_CHECKPOINTS
        if min(_rows_at(first_nn, checkpoint), _rows_at(first_bridge, checkpoint))
        >= FIRST_PRINT_MIN_ROWS
    ]
    unpaired = _unpaired(first_nn, first_bridge, read, "bridge", "first print rows at ")
    if unpaired:
        return None, unpaired
    for checkpoint in read:
        if not _beats(first_nn, first_bridge, checkpoint):
            return False, f"first print table disagrees at {checkpoint}"
    return True, ""


# strict protocol


def _run_strict(inputs, ledger, cfg: SeasonalRun, model_dir: Path | None):
    models = check_models(tuple(cfg.models))
    seas5, era5_K, noaa_C = inputs
    windows = strict_windows(
        seas5,
        era5_K,
        noaa_C,
        first_test_year=cfg.first_test_year,
        last_test_year=cfg.last_test_year,
        ledger=ledger,
    )
    for window in windows:
        _assert_clean(window, SEASONAL_AVAILABILITY)
    made = {window.test_year: window for window in windows}
    skipped, blocks, last_net, last_window = [], [], None, None
    for year in range(cfg.first_test_year, cfg.last_test_year + 1):
        window = made.get(year)
        if window is None or len(window.test) == 0 or _short_of_years(window, SEASONAL_FEATURES):
            skipped.append(year)
            _progress(year, window, "skipped")
            continue
        block, net = _predict_window(window, models, cfg)
        blocks.append(block)
        if net is not None:
            last_net, last_window = net, window
        _progress(year, window, "scored")
    if model_dir is not None and last_net is not None:
        last_net.save(model_dir)
    rows = pd.concat(blocks, ignore_index=True) if blocks else _empty_rows(models)
    saved = None if last_net is None else _saved_model("seasonal", last_window, "strict")
    extras = {
        "protocol": "strict",
        "excluded_rows": int(sum(window.excluded for window in windows)),
        "skipped_windows": skipped,
        "pit_hist": pit_histograms(rows, models),
        "saved_model": saved,
    }
    summary = summarise(
        rows, models, key="lead_month", key_values=LEADS, references=SEASONAL_REFERENCES
    )
    return rows, summary, extras


def _saved_model(kind: str, window, protocol: str) -> dict:
    """Where the saved network came from: its protocol, test year, labels seen and its bridge."""
    return {
        "kind": kind,
        "protocol": protocol,
        "test_year": window.test_year,
        "labels_through": f"{window.test_year - 1}-12",
        "bridge": window.bridge.params,
    }


def _progress(year: int, window, outcome: str) -> None:
    """One stderr line per test year; a year without a window has no rows."""
    n_train = 0 if window is None else len(window.train)
    n_test = 0 if window is None else len(window.test)
    line = f"test year {year}: n_train={n_train}, n_test={n_test}, {outcome}"
    print(line, file=sys.stderr, flush=True)


def _assert_clean(window: Window | NowcastWindow, availability_cols: list[str]) -> None:
    """LeakageError, naming the test year, when a test row used an input not yet public."""
    try:
        assert_no_leakage(window.test, "decision_utc", availability_cols)
    except LeakageError as exc:
        raise LeakageError(f"test year {window.test_year}: {exc}") from exc


def _training_years(train: pd.DataFrame, features: list[str] = SEASONAL_FEATURES) -> int:
    """Distinct target years among the training rows the network can use."""
    return len(usable_target_years(train, features))


def _short_of_years(window: Window | NowcastWindow, features: list[str]) -> bool:
    return _training_years(window.train, features) < MIN_TRAIN_YEARS


def _predict_window(window: Window, models, cfg: SeasonalRun):
    """The window's test rows with (mu, sigma) of every model, and the fitted network if any."""
    block = window.test[["lead_month", "target_month", "decision_utc", "y_C"]].copy()
    block.attrs.clear()  # the frame's bridge stays out of the rows, parquet cannot hold it
    block.insert(0, "test_year", window.test_year)
    net = None
    for name in models:
        if name == "nn":
            net = GtmNnModel(cfg.seeds, cfg.hidden, cfg.device).fit(window.train)
            prediction = net.predict(window.test)
            mu, sigma = prediction["mu"].to_numpy(), prediction["sigma"].to_numpy()
        else:
            mu, sigma = _baseline(name, window.train, window.test)
        block[f"mu_{name}"], block[f"sigma_{name}"] = mu, sigma
    return block, net


def _baseline(name: str, train: pd.DataFrame, test: pd.DataFrame):
    if name == "raw":
        return raw_ensemble(test)
    if name == "emos":
        return emos_baseline(train, test, trend=True)
    return trend_climatology(train, test)


def _empty_rows(models, columns: list[str] = ROW_COLUMNS) -> pd.DataFrame:
    return pd.DataFrame(columns=[*columns, *_prediction_columns(models)])


def _prediction_columns(models) -> list[str]:
    return [f"{kind}_{name}" for name in models for kind in ("mu", "sigma")]


# frozen protocol


def _run_frozen(inputs, cfg: SeasonalRun):
    frame = build_frame_from_inputs(*inputs)
    first, last = cfg.first_test_year, cfg.last_test_year
    # the model report's own scoring, next to the per-row predictions it is built from
    by_lead = summarise_scores(expanding_window_scores(frame, first, last, trend=True))
    predictions = expanding_window_predictions(frame, first, last, trend=True)
    rows = _frozen_rows(frame, predictions)
    scored = set(rows["test_year"].unique().tolist())
    extras = {
        "protocol": "frozen",
        "excluded_rows": 0,
        "skipped_windows": [y for y in range(first, last + 1) if y not in scored],
        "pit_hist": pit_histograms(rows, FROZEN_MODELS),
        "saved_model": None,
    }
    return rows, _frozen_summary(by_lead), extras


def _frozen_rows(frame: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    """The expanding-window predictions in the layout of the strict rows."""
    source = frame.loc[predictions["row"].to_numpy()]
    months = source["init_date"].dt.strftime("%Y-%m")
    decided = {month: seas5_available_on(month) for month in months.unique()}
    rows = pd.DataFrame(
        {
            "test_year": predictions["test_year"].to_numpy(),
            "lead_month": predictions["lead_month"].to_numpy(),
            "target_month": source["target_month"].to_numpy(),
            "decision_utc": pd.to_datetime(months.map(decided).to_numpy(), utc=True),
            "y_C": predictions["y_C"].to_numpy(),
        }
    )
    for name in FROZEN_MODELS:
        rows[f"mu_{name}"] = predictions[f"mu_{name}"].to_numpy()
        rows[f"sigma_{name}"] = predictions[f"sigma_{name}"].to_numpy()
    return rows


def _frozen_summary(by_lead: pd.DataFrame) -> pd.DataFrame:
    """The report's per-lead means as (model, lead) rows; it tabulates coverage for emos only."""
    records = []
    for name in FROZEN_MODELS:
        for line in by_lead.itertuples(index=False):
            crps = getattr(line, f"crps_{name}")
            records.append(
                {
                    "model": name,
                    "lead_month": int(line.lead_month),
                    "n": int(line.n),
                    "crps": float(crps),
                    "skill_vs_trend": _ratio_skill(crps, line.crps_trend),
                    "skill_vs_emos": _ratio_skill(crps, line.crps_emos),
                    "cover50": float(line.cover50_emos) if name == "emos" else float("nan"),
                    "cover90": float(line.cover90_emos) if name == "emos" else float("nan"),
                }
            )
    return pd.DataFrame(records, columns=SUMMARY_COLUMNS)


def _ratio_skill(crps: float, reference: float) -> float:
    """1 - crps / reference as summarise_scores computes it."""
    return 1.0 - crps / reference if reference > 0 else float("nan")


# nowcast protocol


def _predict_nowcast_window(window: NowcastWindow, models, cfg: NowcastRun):
    """The window's test rows with (mu, sigma) of every model, and the fitted network if any."""
    block = window.test[["target_month", "checkpoint", "decision_utc", "y_C"]].copy()
    block.insert(0, "test_year", window.test_year)
    net = None
    for name in models:
        if name == "nn":
            net = GtmNowcastModel(cfg.seeds, cfg.hidden, cfg.device).fit(window.train)
            prediction = net.predict(window.test)
            mu, sigma = prediction["mu"].to_numpy(), prediction["sigma"].to_numpy()
        elif name == "bridge":
            mu, sigma = bridge_nowcast(window.train, window.test, bridge=window.bridge)
        else:
            mu, sigma = persistence(window.train, window.test)
        block[f"mu_{name}"], block[f"sigma_{name}"] = mu, sigma
    return block, net


def _nowcast_summary(rows: pd.DataFrame, models) -> pd.DataFrame:
    return summarise(
        rows, models, key="checkpoint", key_values=CHECKPOINTS, references=NOWCAST_REFERENCES
    )


def _by_key(summary, model: str, key: str) -> pd.DataFrame:
    """The model's summary rows indexed by the key, scores as numbers; empty without any."""
    frame = pd.DataFrame(summary)
    if "model" not in frame:
        return pd.DataFrame()
    part = frame[frame["model"] == model].set_index(key)
    numbers = {
        column: pd.to_numeric(part[column], errors="coerce")
        for column in ("crps", "cover50", "cover90")
        if column in part
    }
    return part.assign(**numbers, n=pd.to_numeric(part["n"], errors="coerce").fillna(0))


def _rows_at(by_key: pd.DataFrame, key) -> int:
    return int(by_key.at[key, "n"]) if key in by_key.index else 0


def _unpaired(nn: pd.DataFrame, other: pd.DataFrame, keys, name: str, where: str) -> str:
    """Why nn and the other model are not scored on as many rows at every key; '' when they are."""
    for key in keys:
        n_nn, n_other = _rows_at(nn, key), _rows_at(other, key)
        if n_nn != n_other:
            return f"unpaired {where}{key}: nn has {n_nn}, {name} {n_other}"
    return ""


def _beats(nn: pd.DataFrame, bridge: pd.DataFrame, checkpoint: str) -> bool:
    """Whether nn has the lower CRPS at the checkpoint; a tie or a missing score does not beat."""
    if checkpoint not in nn.index or checkpoint not in bridge.index:
        return False
    return bool(nn.at[checkpoint, "crps"] < bridge.at[checkpoint, "crps"])
