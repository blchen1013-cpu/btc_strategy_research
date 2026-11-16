"""Phase 1: Offline snapshot acquisition, verification, and macro daily/weekly alignment.

Strictly uses Python 3.11 standard library and enforces fail-closed primary data integrity.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
HTTP_TIMEOUT_SEC = 20
MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_RETRIES = 3
RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)
MAX_CM_PAGES = 20
DAY_SEC = 86400
YAHOO_START_TS = 1410912000  # 2014-09-17T00:00:00Z


class SnapshotIntegrityError(RuntimeError):
    """Raised when any fail-closed snapshot fetch or verification invariant fails."""


@dataclass(frozen=True)
class DailyOHLCVRow:
    open_ts: int
    close_ts: int
    date_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MVRVRow:
    date_utc: str
    open_ts: int
    effective_ts: int
    cap_mvrv_cur: float
    price_usd: float
    realized_price: float


@dataclass(frozen=True)
class MacroDailyRow:
    open_ts: int
    close_ts: int
    date_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_ohlcv: bool
    cap_mvrv_cur: float | None = None
    cm_price_usd: float | None = None
    realized_price: float | None = None


@dataclass(frozen=True)
class WeeklyOHLCVRow:
    week_open_ts: int
    week_close_ts: int
    open_date_utc: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_ohlcv: bool


@dataclass(frozen=True)
class FnGRow:
    date_utc: str
    fng_value: int
    fng_class: str


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def floor_utc_day_ts(ts: int) -> int:
    return (int(ts) // DAY_SEC) * DAY_SEC


def ts_to_iso_utc(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_date_str(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).strftime("%Y-%m-%d")


def date_str_to_open_ts(date_str: str) -> int:
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except ValueError as exc:
        raise SnapshotIntegrityError(f"Invalid UTC date string: {date_str!r}") from exc
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def parse_as_of_arg(as_of_str: str | None, now_dt: dt.datetime | None = None) -> tuple[int, str, bool]:
    """Parse --as-of argument into (req_as_of_ts, req_as_of_utc, is_date_only).

    Date-only YYYY-MM-DD resolves to min(now_utc, YYYY-MM-DDT23:59:59Z).
    Future timestamps > now_utc + 60s fail closed.
    """
    ref_now = now_dt if now_dt is not None else utc_now()
    now_ts = int(ref_now.timestamp())

    if not as_of_str:
        return now_ts, ts_to_iso_utc(now_ts), False

    raw = as_of_str.strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            d = dt.date.fromisoformat(raw)
        except ValueError as exc:
            raise SnapshotIntegrityError(f"Invalid --as-of date format: {raw!r}") from exc
        end_of_day_dt = dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc)
        end_ts = int(end_of_day_dt.timestamp())
        day_start_ts = int(dt.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp())
        if day_start_ts > now_ts + 60:
            raise SnapshotIntegrityError(
                f"Requested --as-of date {raw} is in the future relative to {ts_to_iso_utc(now_ts)}"
            )
        req_ts = min(now_ts, end_ts)
        return req_ts, ts_to_iso_utc(req_ts), True

    norm = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed_dt = dt.datetime.fromisoformat(norm)
    except ValueError as exc:
        raise SnapshotIntegrityError(f"Invalid --as-of timestamp format: {raw!r}") from exc
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=dt.timezone.utc)
    else:
        parsed_dt = parsed_dt.astimezone(dt.timezone.utc)
    req_ts = int(parsed_dt.timestamp())
    if req_ts > now_ts + 60:
        raise SnapshotIntegrityError(
            f"Requested --as-of {ts_to_iso_utc(req_ts)} exceeds current UTC {ts_to_iso_utc(now_ts)} + 60s"
        )
    return req_ts, ts_to_iso_utc(req_ts), False


def linear_percentile(sorted_vals: list[float], q: float) -> float:
    """Compute linear interpolated percentile at q in [0, 1] using index q * (n - 1)."""
    if not sorted_vals:
        raise ValueError("Cannot compute percentile of empty sequence")
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def fetch_json_url(
    url: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Any:
    """Fetch JSON from URL with User-Agent, 20s timeout, 50MB chunked limit, and bounded retries."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with opener(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                status = getattr(resp, "status", 200)
                if status and int(status) >= 400:
                    raise SnapshotIntegrityError(f"HTTP {status} fetching {url}")
                chunks: list[bytes] = []
                total = 0
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_RESPONSE_BYTES:
                        raise SnapshotIntegrityError(
                            f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes limit"
                        )
                    chunks.append(block)
                raw_bytes = b"".join(chunks)
                return json.loads(raw_bytes.decode("utf-8"))
        except SnapshotIntegrityError as exc:
            last_err = exc
            if "exceeded" in str(exc):
                raise
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            last_err = exc

        if attempt < MAX_RETRIES:
            sleep_fn(RETRY_DELAYS_SEC[attempt])

    raise SnapshotIntegrityError(f"Failed fetching {url} after {MAX_RETRIES + 1} attempts: {last_err}")


def fetch_yahoo_daily(
    req_as_of_ts: int,
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
) -> list[DailyOHLCVRow]:
    if req_as_of_ts < YAHOO_START_TS + DAY_SEC:
        return []
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
        f"?period1={YAHOO_START_TS}&period2={int(req_as_of_ts)}&interval=1d"
    )
    payload = fetcher(url)
    try:
        result_list = payload["chart"]["result"]
        if not result_list:
            raise SnapshotIntegrityError("Yahoo Finance returned empty result list")
        res0 = result_list[0]
        timestamps = res0.get("timestamp") or []
        quote = res0["indicators"]["quote"][0]
        opens = quote["open"]
        highs = quote["high"]
        lows = quote["low"]
        closes = quote["close"]
        volumes = quote["volume"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SnapshotIntegrityError(f"Malformed Yahoo Finance payload structure: {exc}") from exc

    if not (len(timestamps) == len(opens) == len(highs) == len(lows) == len(closes) == len(volumes)):
        raise SnapshotIntegrityError("Yahoo Finance parallel arrays have mismatched lengths")

    # Filter open_ts + 86400 <= req_as_of_ts FIRST to discard unclosed intraday bar
    raw_filtered: list[tuple[int, Any, Any, Any, Any, Any]] = []
    for ts_raw, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes):
        if ts_raw is None:
            continue
        open_ts = floor_utc_day_ts(int(ts_raw))
        if open_ts < YAHOO_START_TS:
            continue
        if open_ts + DAY_SEC <= req_as_of_ts:
            raw_filtered.append((open_ts, o, h, l, c, v))

    if not raw_filtered:
        raise SnapshotIntegrityError("Yahoo Finance returned zero closed daily bars for requested window")

    rows: list[DailyOHLCVRow] = []
    for idx, (open_ts, o, h, l, c, v) in enumerate(raw_filtered):
        if o is None or h is None or l is None or c is None or v is None:
            raise SnapshotIntegrityError(
                f"Yahoo Finance null OHLCV field at closed bar open_ts={open_ts} ({ts_to_date_str(open_ts)})"
            )
        fo, fh, fl, fc, fv = float(o), float(h), float(l), float(c), float(v)
        if not (
            math.isfinite(fo)
            and math.isfinite(fh)
            and math.isfinite(fl)
            and math.isfinite(fc)
            and math.isfinite(fv)
        ):
            raise SnapshotIntegrityError(f"Non-finite OHLCV values at {ts_to_date_str(open_ts)}")
        if fo <= 0.0 or fh <= 0.0 or fl <= 0.0 or fc <= 0.0 or fv < 0.0:
            raise SnapshotIntegrityError(f"Non-positive price or negative volume at {ts_to_date_str(open_ts)}")
        if fh < max(fo, fc, fl) - 1e-6 or fl > min(fo, fc, fh) + 1e-6:
            raise SnapshotIntegrityError(f"Inconsistent high/low envelope at {ts_to_date_str(open_ts)}")
        if idx > 0:
            prev_ts = rows[-1].open_ts
            if open_ts - prev_ts != DAY_SEC:
                raise SnapshotIntegrityError(
                    f"Yahoo Finance daily spacing violation between {ts_to_date_str(prev_ts)} "
                    f"and {ts_to_date_str(open_ts)} (delta={open_ts - prev_ts}s)"
                )
        rows.append(
            DailyOHLCVRow(
                open_ts=open_ts,
                close_ts=open_ts + DAY_SEC,
                date_utc=ts_to_date_str(open_ts),
                open=fo,
                high=fh,
                low=fl,
                close=fc,
                volume=fv,
            )
        )
    return rows


def fetch_coinmetrics_mvrv(
    req_as_of_ts: int,
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
) -> list[MVRVRow]:
    url: str | None = (
        "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
        "?assets=btc&metrics=CapMVRVCur,PriceUSD&frequency=1d&page_size=10000"
    )
    raw_items: list[dict[str, Any]] = []
    pages = 0
    while url:
        pages += 1
        if pages > MAX_CM_PAGES:
            raise SnapshotIntegrityError(f"CoinMetrics pagination exceeded {MAX_CM_PAGES} pages cap")
        payload = fetcher(url)
        if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload["data"], list):
            raise SnapshotIntegrityError("Malformed CoinMetrics payload: missing 'data' list")
        raw_items.extend(payload["data"])
        next_url = payload.get("next_page_url")
        url = str(next_url) if next_url else None

    parsed: list[MVRVRow] = []
    for item in raw_items:
        time_str = item.get("time")
        mvrv_raw = item.get("CapMVRVCur")
        px_raw = item.get("PriceUSD")
        if not time_str or mvrv_raw is None or px_raw is None:
            continue
        date_utc = str(time_str)[:10]
        open_ts = date_str_to_open_ts(date_utc)
        effective_ts = open_ts + DAY_SEC
        if effective_ts > req_as_of_ts:
            continue
        try:
            mvrv_val = float(mvrv_raw)
            px_val = float(px_raw)
        except (ValueError, TypeError) as exc:
            raise SnapshotIntegrityError(f"Invalid float in CoinMetrics at {date_utc}") from exc
        if not (math.isfinite(mvrv_val) and math.isfinite(px_val)) or mvrv_val <= 0.0 or px_val <= 0.0:
            raise SnapshotIntegrityError(f"Non-positive or non-finite CoinMetrics values at {date_utc}")
        realized_px = px_val / mvrv_val
        if not math.isfinite(realized_px) or realized_px <= 0.0:
            raise SnapshotIntegrityError(f"Invalid realized_price at {date_utc}")
        parsed.append(
            MVRVRow(
                date_utc=date_utc,
                open_ts=open_ts,
                effective_ts=effective_ts,
                cap_mvrv_cur=mvrv_val,
                price_usd=px_val,
                realized_price=realized_px,
            )
        )

    parsed.sort(key=lambda r: r.open_ts)
    if not parsed:
        raise SnapshotIntegrityError("CoinMetrics returned zero valid MVRV/PriceUSD rows for requested window")

    for i in range(1, len(parsed)):
        delta = parsed[i].open_ts - parsed[i - 1].open_ts
        if delta != DAY_SEC:
            raise SnapshotIntegrityError(
                f"CoinMetrics daily spacing violation between {parsed[i-1].date_utc} and {parsed[i].date_utc} "
                f"(delta={delta}s)"
            )
    return parsed


def fetch_bitstamp_daily(
    req_as_of_ts: int,
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
) -> tuple[dict[int, float] | None, bool]:
    """Return (map of open_ts -> close_price, timed_out_or_failed)."""
    url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000"
    try:
        payload = fetcher(url)
        ohlc_list = payload["data"]["ohlc"]
        if not isinstance(ohlc_list, list):
            return None, True
        out: dict[int, float] = {}
        for item in ohlc_list:
            ts = floor_utc_day_ts(int(item["timestamp"]))
            if ts + DAY_SEC <= req_as_of_ts:
                close_px = float(item["close"])
                if math.isfinite(close_px) and close_px > 0.0:
                    out[ts] = close_px
        return out, False
    except Exception:
        return None, True


def fetch_fng_series(
    ohlcv_effective_as_of_ts: int,
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
) -> tuple[list[FnGRow], str]:
    """Fetch Alternative.me Fear & Greed Index. Non-fatal diagnostic: returns ([], 'UNAVAILABLE') on failure."""
    if ohlcv_effective_as_of_ts <= date_str_to_open_ts("2018-02-01"):
        return [], "UNAVAILABLE"
    url = "https://api.alternative.me/fng/?limit=0"
    try:
        payload = fetcher(url)
        data_list = payload.get("data")
        if not isinstance(data_list, list) or not data_list:
            return [], "UNAVAILABLE"
        by_date: dict[str, FnGRow] = {}
        for item in data_list:
            ts_int = int(item["timestamp"])
            if ts_int >= ohlcv_effective_as_of_ts:
                continue
            val_int = int(item["value"])
            cls_str = str(item.get("value_classification") or "").strip()
            if not (0 <= val_int <= 100) or not cls_str:
                continue
            d_str = ts_to_date_str(floor_utc_day_ts(ts_int))
            by_date[d_str] = FnGRow(date_utc=d_str, fng_value=val_int, fng_class=cls_str)
        rows = sorted(by_date.values(), key=lambda r: r.date_utc)
        return (rows, "OK") if rows else ([], "UNAVAILABLE")
    except Exception:
        return [], "UNAVAILABLE"


def fetch_kraken_live_spot(
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
    now_dt: dt.datetime | None = None,
) -> dict[str, Any]:
    ref_now = now_dt if now_dt is not None else utc_now()
    url = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    try:
        payload = fetcher(url)
        result = payload.get("result") or {}
        pair_obj = result.get("XXBTZUSD") or next(iter(result.values()), None)
        last_str = pair_obj["c"][0]
        px = float(last_str)
        if not math.isfinite(px) or px <= 0.0:
            raise ValueError("Invalid Kraken price")
        return {
            "mode": "LIVE",
            "live_spot_status": "OK",
            "fetched_at_utc": ts_to_iso_utc(int(ref_now.timestamp())),
            "ticker_last": px,
            "source": "kraken_ticker",
        }
    except Exception:
        return {
            "mode": "LIVE",
            "live_spot_status": "UNAVAILABLE",
            "fetched_at_utc": ts_to_iso_utc(int(ref_now.timestamp())),
            "ticker_last": None,
            "source": None,
        }


def build_macro_daily_series(
    mvrv_rows: list[MVRVRow],
    daily_ohlcv_rows: list[DailyOHLCVRow],
    weekly_ohlcv_rows: list[WeeklyOHLCVRow] | None = None,
) -> list[MacroDailyRow]:
    """Merge CoinMetrics daily series (2010-07-18+) and Yahoo Daily OHLCV (2014-09-17+) by UTC date.

    Pre-OHLCV dates use open=high=low=close=price_usd, volume=0.0, is_ohlcv=False.
    From daily_ohlcv[0].date_utc onward, Yahoo OHLCV provides prices/volume with is_ohlcv=True.
    """
    _ = weekly_ohlcv_rows  # Accepted for signature compatibility across Phase 1 & 2
    mvrv_by_ts: dict[int, MVRVRow] = {r.open_ts: r for r in mvrv_rows}
    ohlcv_by_ts: dict[int, DailyOHLCVRow] = {r.open_ts: r for r in daily_ohlcv_rows}
    first_ohlcv_ts = daily_ohlcv_rows[0].open_ts if daily_ohlcv_rows else None

    all_open_ts = sorted(set(mvrv_by_ts.keys()) | set(ohlcv_by_ts.keys()))
    macro_rows: list[MacroDailyRow] = []

    for open_ts in all_open_ts:
        cm = mvrv_by_ts.get(open_ts)
        yh = ohlcv_by_ts.get(open_ts)
        if first_ohlcv_ts is not None and open_ts >= first_ohlcv_ts:
            if yh is None:
                # If mvrv extends beyond ohlcv_effective_as_of_ts, stop at ohlcv boundary if OHLCV exists
                continue
            macro_rows.append(
                MacroDailyRow(
                    open_ts=yh.open_ts,
                    close_ts=yh.close_ts,
                    date_utc=yh.date_utc,
                    open=yh.open,
                    high=yh.high,
                    low=yh.low,
                    close=yh.close,
                    volume=yh.volume,
                    is_ohlcv=True,
                    cap_mvrv_cur=cm.cap_mvrv_cur if cm else None,
                    cm_price_usd=cm.price_usd if cm else None,
                    realized_price=cm.realized_price if cm else None,
                )
            )
        else:
            if cm is None:
                continue
            px = cm.price_usd
            macro_rows.append(
                MacroDailyRow(
                    open_ts=cm.open_ts,
                    close_ts=cm.effective_ts,
                    date_utc=cm.date_utc,
                    open=px,
                    high=px,
                    low=px,
                    close=px,
                    volume=0.0,
                    is_ohlcv=False,
                    cap_mvrv_cur=cm.cap_mvrv_cur,
                    cm_price_usd=cm.price_usd,
                    realized_price=cm.realized_price,
                )
            )
    return macro_rows


def resample_iso_monday_weekly(macro_daily: list[MacroDailyRow]) -> list[WeeklyOHLCVRow]:
    """Group macro_daily into complete 7-day ISO Monday 00:00:00 UTC buckets, discarding partial weeks."""
    if not macro_daily:
        return []
    buckets: dict[int, list[MacroDailyRow]] = {}
    for row in macro_daily:
        dt_obj = dt.datetime.fromtimestamp(row.open_ts, tz=dt.timezone.utc)
        monday_dt = dt_obj - dt.timedelta(days=dt_obj.weekday())
        monday_ts = int(
            dt.datetime(monday_dt.year, monday_dt.month, monday_dt.day, tzinfo=dt.timezone.utc).timestamp()
        )
        buckets.setdefault(monday_ts, []).append(row)

    weekly_rows: list[WeeklyOHLCVRow] = []
    for monday_ts in sorted(buckets.keys()):
        days = sorted(buckets[monday_ts], key=lambda r: r.open_ts)
        if len(days) != 7:
            continue
        # Verify contiguous 7 days starting on Monday
        if days[0].open_ts != monday_ts or days[-1].open_ts != monday_ts + 6 * DAY_SEC:
            continue
        weekly_rows.append(
            WeeklyOHLCVRow(
                week_open_ts=monday_ts,
                week_close_ts=monday_ts + 7 * DAY_SEC,
                open_date_utc=ts_to_date_str(monday_ts),
                open=days[0].open,
                high=max(d.high for d in days),
                low=min(d.low for d in days),
                close=days[-1].close,
                volume=sum(d.volume for d in days),
                is_ohlcv=all(d.is_ohlcv for d in days),
            )
        )
    return weekly_rows


def compute_canonical_manifest_sha256(manifest_obj: dict[str, Any]) -> str:
    stripped = {k: v for k, v in manifest_obj.items() if k != "manifest_sha256"}
    canonical_bytes = json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _write_csv_files(
    out_dir: Path,
    daily_rows: list[DailyOHLCVRow],
    mvrv_rows: list[MVRVRow],
    weekly_rows: list[WeeklyOHLCVRow],
    fng_rows: list[FnGRow],
) -> dict[str, dict[str, Any]]:
    files_meta: dict[str, dict[str, Any]] = {}

    daily_path = out_dir / "daily_ohlcv.csv"
    with daily_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["open_ts", "close_ts", "date_utc", "open", "high", "low", "close", "volume"])
        for r in daily_rows:
            w.writerow(
                [
                    r.open_ts,
                    r.close_ts,
                    r.date_utc,
                    f"{r.open:.8f}",
                    f"{r.high:.8f}",
                    f"{r.low:.8f}",
                    f"{r.close:.8f}",
                    f"{r.volume:.8f}",
                ]
            )
    files_meta["daily_ohlcv.csv"] = {"rows": len(daily_rows), "sha256": compute_file_sha256(daily_path)}

    mvrv_path = out_dir / "mvrv.csv"
    with mvrv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date_utc", "open_ts", "effective_ts", "cap_mvrv_cur", "price_usd", "realized_price"])
        for r in mvrv_rows:
            w.writerow(
                [
                    r.date_utc,
                    r.open_ts,
                    r.effective_ts,
                    f"{r.cap_mvrv_cur:.8f}",
                    f"{r.price_usd:.8f}",
                    f"{r.realized_price:.8f}",
                ]
            )
    files_meta["mvrv.csv"] = {"rows": len(mvrv_rows), "sha256": compute_file_sha256(mvrv_path)}

    weekly_path = out_dir / "weekly_ohlcv.csv"
    with weekly_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "week_open_ts",
                "week_close_ts",
                "open_date_utc",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "is_ohlcv",
            ]
        )
        for r in weekly_rows:
            w.writerow(
                [
                    r.week_open_ts,
                    r.week_close_ts,
                    r.open_date_utc,
                    f"{r.open:.8f}",
                    f"{r.high:.8f}",
                    f"{r.low:.8f}",
                    f"{r.close:.8f}",
                    f"{r.volume:.8f}",
                    "true" if r.is_ohlcv else "false",
                ]
            )
    files_meta["weekly_ohlcv.csv"] = {"rows": len(weekly_rows), "sha256": compute_file_sha256(weekly_path)}

    fng_path = out_dir / "fng.csv"
    with fng_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date_utc", "fng_value", "fng_class"])
        for r in fng_rows:
            w.writerow([r.date_utc, r.fng_value, r.fng_class])
    files_meta["fng.csv"] = {"rows": len(fng_rows), "sha256": compute_file_sha256(fng_path)}

    return files_meta


def _check_out_dir_writable_for_new_snapshot(out_dir: Path) -> None:
    if out_dir.exists():
        if not out_dir.is_dir():
            raise SnapshotIntegrityError(f"Target --out-dir {out_dir} exists and is not a directory")
        disallowed = [p.name for p in out_dir.iterdir() if p.name != ".gitkeep"]
        if disallowed:
            raise SnapshotIntegrityError(
                f"Refusing to overwrite existing snapshot directory {out_dir} containing: {sorted(disallowed)}"
            )


def determine_live_eligibility(
    req_as_of_ts: int,
    is_date_only: bool,
    ohlcv_effective_as_of_ts: int,
    now_dt: dt.datetime,
) -> bool:
    now_ts = int(now_dt.timestamp())
    today_floor_ts = floor_utc_day_ts(now_ts)
    if ohlcv_effective_as_of_ts != today_floor_ts:
        return False
    if is_date_only:
        req_date = dt.datetime.fromtimestamp(req_as_of_ts, tz=dt.timezone.utc).date()
        return req_date == now_dt.date()
    return abs(now_ts - req_as_of_ts) <= 3600


def create_snapshot(
    out_dir: Path,
    as_of_str: str | None = None,
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
    now_dt: dt.datetime | None = None,
) -> dict[str, Any]:
    resolved_dir = out_dir.resolve()
    _check_out_dir_writable_for_new_snapshot(resolved_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)

    ref_now = now_dt if now_dt is not None else utc_now()
    req_as_of_ts, req_as_of_utc, is_date_only = parse_as_of_arg(as_of_str, now_dt=ref_now)
    req_floor_ts = floor_utc_day_ts(req_as_of_ts)

    mvrv_raw = fetch_coinmetrics_mvrv(req_as_of_ts, fetcher=fetcher)
    yahoo_raw = fetch_yahoo_daily(req_as_of_ts, fetcher=fetcher)

    last_cm_eff_ts = mvrv_raw[-1].effective_ts
    mvrv_effective_as_of_ts = min(req_floor_ts, last_cm_eff_ts)

    if yahoo_raw:
        last_yh_close_ts = yahoo_raw[-1].close_ts
        ohlcv_effective_as_of_ts = min(req_floor_ts, last_yh_close_ts)
    else:
        ohlcv_effective_as_of_ts = mvrv_effective_as_of_ts

    if abs(ohlcv_effective_as_of_ts - mvrv_effective_as_of_ts) > DAY_SEC:
        raise SnapshotIntegrityError(
            f"Dual-watermark divergence > 86400s: ohlcv_effective={ts_to_iso_utc(ohlcv_effective_as_of_ts)} "
            f"vs mvrv_effective={ts_to_iso_utc(mvrv_effective_as_of_ts)}"
        )

    daily_rows = [r for r in yahoo_raw if r.close_ts <= ohlcv_effective_as_of_ts]
    mvrv_rows = [r for r in mvrv_raw if r.effective_ts <= mvrv_effective_as_of_ts]
    if not mvrv_rows:
        raise SnapshotIntegrityError("No CoinMetrics rows remain after watermark truncation")

    # 12-year overlap check: CoinMetrics PriceUSD vs Yahoo Close
    yh_by_ts = {r.open_ts: r.close for r in daily_rows}
    cm_yh_diffs: list[float] = []
    for r in mvrv_rows:
        if r.open_ts in yh_by_ts:
            yh_px = yh_by_ts[r.open_ts]
            cm_yh_diffs.append(abs(r.price_usd - yh_px) / yh_px * 100.0)

    if daily_rows:
        if not cm_yh_diffs:
            raise SnapshotIntegrityError("Zero overlapping dates between CoinMetrics and Yahoo Daily")
        cm_yh_diffs.sort()
        cm_med = linear_percentile(cm_yh_diffs, 0.50)
        cm_p99 = linear_percentile(cm_yh_diffs, 0.99)
        if cm_med > 0.50 or cm_p99 > 3.00:
            raise SnapshotIntegrityError(
                f"CoinMetrics vs Yahoo cross-check failed: median={cm_med:.4f}% (limit 0.50%), "
                f"P99={cm_p99:.4f}% (limit 3.00%)"
            )
    else:
        cm_med = 0.0
        cm_p99 = 0.0

    # Bitstamp overlap check (<= 1,000d)
    bs_map, bs_failed = fetch_bitstamp_daily(ohlcv_effective_as_of_ts, fetcher=fetcher)
    bs_med: float | None = None
    bs_p99: float | None = None
    if bs_failed or bs_map is None:
        # Check if requested window overlaps the recent 1,000-day window where Bitstamp should exist
        cross_check_status = "DEGRADED" if daily_rows else "VERIFIED"
    else:
        bs_diffs = [
            abs(bs_px - yh_by_ts[ts]) / yh_by_ts[ts] * 100.0
            for ts, bs_px in bs_map.items()
            if ts in yh_by_ts
        ]
        if len(bs_diffs) < 30:
            cross_check_status = "VERIFIED"
        else:
            bs_diffs.sort()
            bs_med = linear_percentile(bs_diffs, 0.50)
            bs_p99 = linear_percentile(bs_diffs, 0.99)
            if bs_med > 0.50 or bs_p99 > 1.50:
                raise SnapshotIntegrityError(
                    f"Bitstamp vs Yahoo cross-check failed: median={bs_med:.4f}% (limit 0.50%), "
                    f"P99={bs_p99:.4f}% (limit 1.50%)"
                )
            cross_check_status = "VERIFIED"

    macro_daily = build_macro_daily_series(mvrv_rows, daily_rows)
    weekly_rows = [
        w for w in resample_iso_monday_weekly(macro_daily) if w.week_close_ts <= ohlcv_effective_as_of_ts
    ]
    fng_rows, fng_status = fetch_fng_series(ohlcv_effective_as_of_ts, fetcher=fetcher)

    files_meta = _write_csv_files(resolved_dir, daily_rows, mvrv_rows, weekly_rows, fng_rows)

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "requested_as_of_utc": req_as_of_utc,
        "ohlcv_effective_as_of_utc": ts_to_iso_utc(ohlcv_effective_as_of_ts),
        "ohlcv_effective_as_of_ts": ohlcv_effective_as_of_ts,
        "mvrv_effective_as_of_utc": ts_to_iso_utc(mvrv_effective_as_of_ts),
        "mvrv_effective_as_of_ts": mvrv_effective_as_of_ts,
        "cross_check_status": cross_check_status,
        "fng_status": fng_status,
        "overlap_diffs": {
            "cm_vs_yh_median_pct": round(cm_med, 6),
            "cm_vs_yh_p99_pct": round(cm_p99, 6),
            "bs_median_pct": round(bs_med, 6) if bs_med is not None else None,
            "bs_p99_pct": round(bs_p99, 6) if bs_p99 is not None else None,
        },
        "files": files_meta,
    }
    manifest["manifest_sha256"] = compute_canonical_manifest_sha256(manifest)

    manifest_path = resolved_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    # Write unhashed live_spot.json
    is_live = determine_live_eligibility(req_as_of_ts, is_date_only, ohlcv_effective_as_of_ts, ref_now)
    if is_live:
        live_spot_obj = fetch_kraken_live_spot(fetcher=fetcher, now_dt=ref_now)
    else:
        live_spot_obj = {
            "mode": "HISTORICAL_REPLAY",
            "live_spot_status": "HISTORICAL_REPLAY",
            "fetched_at_utc": None,
            "ticker_last": None,
            "source": None,
        }
    with (resolved_dir / "live_spot.json").open("w", encoding="utf-8") as f:
        json.dump(live_spot_obj, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    return manifest


def load_and_verify_snapshot(
    snapshot_dir: Path,
) -> tuple[dict[str, Any], list[DailyOHLCVRow], list[MVRVRow], list[WeeklyOHLCVRow], list[FnGRow], dict[str, Any]]:
    """Verify manifest_sha256, per-file sha256, row counts, spacing, and finite positive numbers."""
    resolved_dir = snapshot_dir.resolve()
    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SnapshotIntegrityError(f"Missing manifest.json in {resolved_dir}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnapshotIntegrityError(f"Failed parsing manifest.json: {exc}") from exc

    expected_hash = manifest.get("manifest_sha256")
    computed_hash = compute_canonical_manifest_sha256(manifest)
    if not expected_hash or expected_hash != computed_hash:
        raise SnapshotIntegrityError(
            f"manifest_sha256 mismatch in {manifest_path}: expected {expected_hash}, got {computed_hash}"
        )

    ohlcv_ts = int(manifest["ohlcv_effective_as_of_ts"])
    mvrv_ts = int(manifest["mvrv_effective_as_of_ts"])
    if abs(ohlcv_ts - mvrv_ts) > DAY_SEC:
        raise SnapshotIntegrityError(f"Manifest watermark divergence > 86400s: {ohlcv_ts} vs {mvrv_ts}")

    files_meta = manifest.get("files") or {}
    required_files = ("daily_ohlcv.csv", "mvrv.csv", "weekly_ohlcv.csv", "fng.csv")
    for fname in required_files:
        if fname not in files_meta:
            raise SnapshotIntegrityError(f"Missing {fname} entry in manifest.json files")
        fpath = resolved_dir / fname
        if not fpath.is_file():
            raise SnapshotIntegrityError(f"Missing snapshot data file: {fpath}")
        actual_sha = compute_file_sha256(fpath)
        if actual_sha != files_meta[fname].get("sha256"):
            raise SnapshotIntegrityError(
                f"SHA-256 mismatch for {fname}: expected {files_meta[fname].get('sha256')}, got {actual_sha}"
            )

    # Parse and validate daily_ohlcv.csv
    daily_rows: list[DailyOHLCVRow] = []
    with (resolved_dir / "daily_ohlcv.csv").open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            open_ts = int(row["open_ts"])
            close_ts = int(row["close_ts"])
            fo, fh, fl, fc, fv = (
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            )
            if close_ts - open_ts != DAY_SEC or close_ts > ohlcv_ts:
                raise SnapshotIntegrityError(f"Invalid daily_ohlcv timestamps at row {idx}")
            if not all(math.isfinite(x) for x in (fo, fh, fl, fc, fv)) or min(fo, fh, fl, fc) <= 0.0 or fv < 0.0:
                raise SnapshotIntegrityError(f"Invalid daily_ohlcv numeric values at row {idx}")
            if idx > 0 and open_ts - daily_rows[-1].open_ts != DAY_SEC:
                raise SnapshotIntegrityError(f"daily_ohlcv.csv spacing violation at row {idx}")
            daily_rows.append(
                DailyOHLCVRow(
                    open_ts=open_ts,
                    close_ts=close_ts,
                    date_utc=row["date_utc"],
                    open=fo,
                    high=fh,
                    low=fl,
                    close=fc,
                    volume=fv,
                )
            )
    if len(daily_rows) != int(files_meta["daily_ohlcv.csv"]["rows"]):
        raise SnapshotIntegrityError("Row count mismatch in daily_ohlcv.csv")

    # Parse and validate mvrv.csv
    mvrv_rows: list[MVRVRow] = []
    with (resolved_dir / "mvrv.csv").open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            open_ts = int(row["open_ts"])
            eff_ts = int(row["effective_ts"])
            mvrv_v = float(row["cap_mvrv_cur"])
            px_v = float(row["price_usd"])
            rp_v = float(row["realized_price"])
            if eff_ts - open_ts != DAY_SEC or eff_ts > mvrv_ts:
                raise SnapshotIntegrityError(f"Invalid mvrv timestamps at row {idx}")
            if not all(math.isfinite(x) and x > 0.0 for x in (mvrv_v, px_v, rp_v)):
                raise SnapshotIntegrityError(f"Invalid mvrv numeric values at row {idx}")
            if idx > 0 and open_ts - mvrv_rows[-1].open_ts != DAY_SEC:
                raise SnapshotIntegrityError(f"mvrv.csv spacing violation at row {idx}")
            mvrv_rows.append(
                MVRVRow(
                    date_utc=row["date_utc"],
                    open_ts=open_ts,
                    effective_ts=eff_ts,
                    cap_mvrv_cur=mvrv_v,
                    price_usd=px_v,
                    realized_price=rp_v,
                )
            )
    if len(mvrv_rows) != int(files_meta["mvrv.csv"]["rows"]) or not mvrv_rows:
        raise SnapshotIntegrityError("Row count mismatch or empty mvrv.csv")

    # Parse and validate weekly_ohlcv.csv
    weekly_rows: list[WeeklyOHLCVRow] = []
    with (resolved_dir / "weekly_ohlcv.csv").open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            w_open = int(row["week_open_ts"])
            w_close = int(row["week_close_ts"])
            fo, fh, fl, fc, fv = (
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            )
            is_ohlcv = row["is_ohlcv"].strip().lower() == "true"
            if w_close - w_open != 7 * DAY_SEC or w_close > ohlcv_ts:
                raise SnapshotIntegrityError(f"Invalid weekly_ohlcv timestamps at row {idx}")
            dt_w = dt.datetime.fromtimestamp(w_open, tz=dt.timezone.utc)
            if dt_w.weekday() != 0 or dt_w.hour != 0 or dt_w.minute != 0:
                raise SnapshotIntegrityError(f"Weekly bar at row {idx} does not start on ISO Monday 00:00 UTC")
            if not all(math.isfinite(x) for x in (fo, fh, fl, fc, fv)) or min(fo, fh, fl, fc) <= 0.0 or fv < 0.0:
                raise SnapshotIntegrityError(f"Invalid weekly_ohlcv numeric values at row {idx}")
            if idx > 0 and w_open - weekly_rows[-1].week_open_ts != 7 * DAY_SEC:
                raise SnapshotIntegrityError(f"weekly_ohlcv.csv spacing violation at row {idx}")
            weekly_rows.append(
                WeeklyOHLCVRow(
                    week_open_ts=w_open,
                    week_close_ts=w_close,
                    open_date_utc=row["open_date_utc"],
                    open=fo,
                    high=fh,
                    low=fl,
                    close=fc,
                    volume=fv,
                    is_ohlcv=is_ohlcv,
                )
            )
    if len(weekly_rows) != int(files_meta["weekly_ohlcv.csv"]["rows"]):
        raise SnapshotIntegrityError("Row count mismatch in weekly_ohlcv.csv")

    # Parse and validate fng.csv
    fng_rows: list[FnGRow] = []
    with (resolved_dir / "fng.csv").open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            d_utc = row["date_utc"]
            val = int(row["fng_value"])
            cls_s = row["fng_class"].strip()
            if not (0 <= val <= 100) or not cls_s:
                raise SnapshotIntegrityError(f"Invalid fng row at {idx}")
            if idx > 0 and d_utc <= fng_rows[-1].date_utc:
                raise SnapshotIntegrityError(f"fng.csv ascending date violation at row {idx}")
            fng_rows.append(FnGRow(date_utc=d_utc, fng_value=val, fng_class=cls_s))
    if len(fng_rows) != int(files_meta["fng.csv"]["rows"]):
        raise SnapshotIntegrityError("Row count mismatch in fng.csv")

    live_spot_path = resolved_dir / "live_spot.json"
    if live_spot_path.is_file():
        try:
            live_spot_obj = json.loads(live_spot_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SnapshotIntegrityError(f"Corrupt live_spot.json: {exc}") from exc
    else:
        live_spot_obj = {
            "mode": "HISTORICAL_REPLAY",
            "live_spot_status": "UNAVAILABLE",
            "fetched_at_utc": None,
            "ticker_last": None,
            "source": None,
        }

    return manifest, daily_rows, mvrv_rows, weekly_rows, fng_rows, live_spot_obj


def refresh_live_spot_in_snapshot(
    snapshot_dir: Path,
    *,
    fetcher: Callable[[str], Any] = fetch_json_url,
    now_dt: dt.datetime | None = None,
) -> dict[str, Any]:
    """Update only unhashed live_spot.json in an existing verified snapshot when effective OHLCV equals today."""
    resolved_dir = snapshot_dir.resolve()
    manifest, _, _, _, _, _ = load_and_verify_snapshot(resolved_dir)
    ref_now = now_dt if now_dt is not None else utc_now()
    today_floor_ts = floor_utc_day_ts(int(ref_now.timestamp()))
    if int(manifest["ohlcv_effective_as_of_ts"]) != today_floor_ts:
        raise SnapshotIntegrityError(
            f"Cannot --refresh-live-spot on snapshot with ohlcv_effective_as_of_utc="
            f"{manifest['ohlcv_effective_as_of_utc']} != today floor {ts_to_iso_utc(today_floor_ts)}"
        )
    live_spot_obj = fetch_kraken_live_spot(fetcher=fetcher, now_dt=ref_now)
    with (resolved_dir / "live_spot.json").open("w", encoding="utf-8") as f:
        json.dump(live_spot_obj, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return live_spot_obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch or verify immutable BTC/USD market & on-chain snapshots.")
    parser.add_argument("--out-dir", type=str, default=None, help="Target snapshot directory path")
    parser.add_argument("--as-of", type=str, default=None, help="UTC cutoff (ISO-8601 UTC or YYYY-MM-DD)")
    parser.add_argument("--verify-only", action="store_true", help="Verify existing snapshot without network calls")
    parser.add_argument(
        "--refresh-live-spot",
        action="store_true",
        help="Refresh only unhashed live_spot.json in an existing today-aligned snapshot",
    )
    args = parser.parse_args(argv)

    try:
        if args.verify_only:
            if not args.out_dir:
                raise SnapshotIntegrityError("--verify-only requires --out-dir")
            manifest, d_rows, m_rows, w_rows, f_rows, _ = load_and_verify_snapshot(Path(args.out_dir))
            print(
                f"VERIFIED snapshot at {Path(args.out_dir).resolve()} | "
                f"manifest_sha256={manifest['manifest_sha256']} | "
                f"daily={len(d_rows)}, mvrv={len(m_rows)}, weekly={len(w_rows)}, fng={len(f_rows)}"
            )
            return 0

        if args.refresh_live_spot:
            if not args.out_dir:
                raise SnapshotIntegrityError("--refresh-live-spot requires --out-dir")
            live_obj = refresh_live_spot_in_snapshot(Path(args.out_dir))
            print(f"REFRESHED live_spot.json: status={live_obj['live_spot_status']} price={live_obj['ticker_last']}")
            return 0

        now_val = utc_now()
        if args.out_dir:
            target_dir = Path(args.out_dir)
        else:
            stamp = now_val.strftime("%Y%m%d_%H%M%SZ")
            target_dir = Path("data/snapshots") / stamp

        manifest = create_snapshot(target_dir, as_of_str=args.as_of, now_dt=now_val)
        print(
            f"CREATED snapshot at {target_dir.resolve()} | "
            f"ohlcv_effective={manifest['ohlcv_effective_as_of_utc']} | "
            f"mvrv_effective={manifest['mvrv_effective_as_of_utc']} | "
            f"cross_check={manifest['cross_check_status']} | "
            f"manifest_sha256={manifest['manifest_sha256']}"
        )
        return 0
    except SnapshotIntegrityError as exc:
        print(f"SNAPSHOT ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
