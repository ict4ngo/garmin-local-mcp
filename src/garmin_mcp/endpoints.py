"""Endpoint registry: fetch + parse for every Garmin Connect endpoint we sync.

Parse functions are pure (no I/O), tolerate missing keys (absent data becomes
None, never an exception), and return [] to mean "the API had nothing for this
day". The sync engine snapshots raw payloads verbatim before parsing, so a
parser bug never loses data - `reparse` re-runs these functions offline.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .samples import Sample

# A parse result: (table name, row dict). Rows may be partial - the sync
# engine upserts them with db.upsert_partial so two endpoints can each
# contribute columns to the same row (e.g. sleep's skin-temp deviation joins
# usersummary's vitals in daily_wellness regardless of fetch order).
ParsedRow = tuple[str, dict]

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "daily_wellness": ("date",),
    "sleep": ("date",),
    "hrv": ("date",),
    "training_status": ("date",),
    "activities": ("activity_id",),
}


@dataclass(frozen=True)
class Endpoint:
    name: str
    fetch: Callable[[Any, str], Any]  # (client, date) -> raw payload
    parse: Callable[[Any, str], list[ParsedRow]]  # (payload, date) -> rows


@dataclass(frozen=True)
class SampleEndpoint:
    name: str
    fetch: Callable[[Any, str], Any]
    parse: Callable[[Any, str], list[Sample]]


def _get(obj: Any, *path: str) -> Any:
    """Walk nested dicts; None as soon as anything is missing or not a dict."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _int(v: Any) -> int | None:
    return None if v is None else round(v)


def _minutes(seconds: Any) -> int | None:
    return None if seconds is None else round(seconds / 60)


def _lower(v: Any) -> str | None:
    return v.lower() if isinstance(v, str) else None


def _round2(v: Any) -> float | None:
    return None if v is None else round(v, 2)


def _iso_local(epoch_ms: Any) -> str | None:
    """Garmin '...TimestampLocal' epoch-ms values are already wall-clock local."""
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _has_data(row: dict, ignore: tuple[str, ...] = ("date",)) -> bool:
    return any(v is not None for k, v in row.items() if k not in ignore)


# --- usersummary -> daily_wellness ----------------------------------------


def fetch_usersummary(client: Any, date: str) -> Any:
    return client.get_stats(date)


def parse_usersummary(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict):
        return []
    row = {
        "date": date,
        "resting_hr": payload.get("restingHeartRate"),
        "min_hr": payload.get("minHeartRate"),
        "max_hr": payload.get("maxHeartRate"),
        "steps": payload.get("totalSteps"),
        "distance_m": payload.get("totalDistanceMeters"),
        "floors_up": _int(payload.get("floorsAscended")),
        "stress_avg": payload.get("averageStressLevel"),
        "stress_max": payload.get("maxStressLevel"),
        "body_battery_high": payload.get("bodyBatteryHighestValue"),
        "body_battery_low": payload.get("bodyBatteryLowestValue"),
        "calories_total": _int(payload.get("totalKilocalories")),
        "calories_active": _int(payload.get("activeKilocalories")),
        "spo2_avg": payload.get("averageSpo2"),
        "respiration_avg": payload.get("avgWakingRespirationValue"),
        "intensity_min_moderate": payload.get("moderateIntensityMinutes"),
        "intensity_min_vigorous": payload.get("vigorousIntensityMinutes"),
    }
    if not _has_data(row):
        return []
    row["source"] = "api"
    return [("daily_wellness", row)]


# --- sleep -> sleep (+ skin temp into daily_wellness) ----------------------


def fetch_sleep(client: Any, date: str) -> Any:
    return client.get_sleep_data(date)


def parse_sleep(payload: Any, date: str) -> list[ParsedRow]:
    # The full payload is ~230KB of per-minute arrays; only summary fields are
    # parsed here - the raw snapshot keeps everything.
    if not isinstance(payload, dict):
        return []
    dto = payload.get("dailySleepDTO") or {}
    row = {
        "date": date,
        "score": _get(dto, "sleepScores", "overall", "value"),
        "duration_min": None
        if dto.get("sleepTimeSeconds") is None
        else round(dto["sleepTimeSeconds"] / 60, 1),
        "deep_min": _minutes(dto.get("deepSleepSeconds")),
        "light_min": _minutes(dto.get("lightSleepSeconds")),
        "rem_min": _minutes(dto.get("remSleepSeconds")),
        "awake_min": _minutes(dto.get("awakeSleepSeconds")),
        "start_ts": _iso_local(dto.get("sleepStartTimestampLocal")),
        "end_ts": _iso_local(dto.get("sleepEndTimestampLocal")),
        "avg_spo2": dto.get("averageSpO2Value"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_stress": _int(dto.get("avgSleepStress")),
        "restless_moments": payload.get("restlessMomentsCount"),
        "nap_min": _minutes(dto.get("napTimeSeconds")),
        "quality_flags": None,
    }
    if not _has_data(row):
        return []
    row["source"] = "api"
    rows: list[ParsedRow] = [("sleep", row)]
    skin_temp = payload.get("avgSkinTempDeviationC")
    if skin_temp is not None:
        # Partial row: contributes one column to daily_wellness without
        # clobbering whatever usersummary wrote (or will write).
        rows.append(("daily_wellness", {"date": date, "skin_temp_dev_c": skin_temp}))
    return rows


def _as_timestamp(value: Any, *, gmt: bool = False) -> datetime | None:
    """Decode an upstream timestamp without inventing a timezone or time point."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        # Garmin chart arrays use Unix epoch milliseconds.
        seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # A wall-clock timestamp alone is not enough to identify an instant.
        return parsed.replace(tzinfo=UTC) if gmt else None
    return parsed.astimezone(UTC)


def _timestamp_local(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("localTimestamp", "timestampLocal", "startTimestampLocal"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return None


def _sample_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _source_records(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _parse_series(
    payload: Any,
    date: str,
    endpoint: str,
    metric: str,
    unit: str | None,
    array_keys: tuple[str, ...],
    value_key: str = "value",
    sentinels: tuple[float, ...] = (),
) -> list[Sample]:
    parsed: list[Sample] = []
    for record in _source_records(payload):
        source_date = record.get("calendarDate") or record.get("date") or date
        if not isinstance(source_date, str):
            source_date = date
        for array_key in array_keys:
            values = record.get(array_key)
            if not isinstance(values, list):
                continue
            for index, item in enumerate(values):
                timestamp = sample_value = None
                local_timestamp = _timestamp_local(item)
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    timestamp, sample_value = item[0], item[1]
                    is_gmt = False
                elif isinstance(item, dict):
                    timestamp_key = next(
                        (
                            key
                            for key in (
                                "timestamp",
                                "timestampGMT",
                                "startGMT",
                                "startTimestampGMT",
                                "timestampLocal",
                                "localTimestamp",
                                "startTimestampLocal",
                            )
                            if item.get(key) is not None
                        ),
                        None,
                    )
                    timestamp = item.get(timestamp_key) if timestamp_key else None
                    is_gmt = timestamp_key in ("timestampGMT", "startGMT", "startTimestampGMT")
                    sample_value = item.get(value_key)
                    if sample_value is None and value_key != "activityLevel":
                        sample_value = item.get("activityLevel")
                else:
                    continue
                observed_at = _as_timestamp(timestamp, gmt=is_gmt)
                value = _sample_value(sample_value)
                if observed_at is None or value is None or value in sentinels:
                    continue
                parsed.append(
                    Sample(
                        source_endpoint=endpoint,
                        source_id=source_date,
                        metric=metric,
                        observed_at=observed_at,
                        source_date=source_date,
                        sample_index=index,
                        value=value,
                        unit=unit,
                        local_timestamp=local_timestamp,
                    )
                )
    return parsed


def parse_stress_samples(payload: Any, date: str) -> list[Sample]:
    return _parse_series(
        payload, date, "stress", "stress_level", "stress_level", ("stressValuesArray",),
        sentinels=(-1.0,)
    )


def parse_body_battery_samples(payload: Any, date: str) -> list[Sample]:
    return _parse_series(
        payload, date, "body_battery", "body_battery", "body_battery",
        ("bodyBatteryValuesArray",)
    )


def parse_heart_rate_samples(payload: Any, date: str) -> list[Sample]:
    return _parse_series(
        payload, date, "heart_rates", "heart_rate", "bpm", ("heartRateValues",)
    )


def parse_spo2_samples(payload: Any, date: str) -> list[Sample]:
    return _parse_series(
        payload, date, "spo2", "spo2", "percent", ("spO2ValuesArray", "spo2ValuesArray")
    )


def parse_respiration_samples(payload: Any, date: str) -> list[Sample]:
    return _parse_series(
        payload, date, "respiration", "respiration", "breaths_per_minute",
        ("respirationValuesArray",)
    )


def parse_daily_movement_samples(payload: Any, date: str) -> list[Sample]:
    return _parse_series(
        payload, date, "daily_movement", "movement", None, ("movementValuesArray",),
        value_key="activityLevel"
    )


def parse_training_readiness_samples(payload: Any, date: str) -> list[Sample]:
    parsed: list[Sample] = []
    for index, item in enumerate(payload if isinstance(payload, list) else [payload]):
        if not isinstance(item, dict):
            continue
        source_date = item.get("calendarDate") or item.get("date") or date
        timestamp = item.get("timestamp")
        observed_at = _as_timestamp(timestamp)
        value = _sample_value(item.get("score"))
        if observed_at is None or value is None:
            continue
        local_timestamp = item.get("timestampLocal")
        parsed.append(
            Sample(
                source_endpoint="training_readiness",
                source_id=str(source_date),
                metric="training_readiness",
                observed_at=observed_at,
                source_date=str(source_date),
                sample_index=index,
                value=value,
                unit="score",
                local_timestamp=local_timestamp if isinstance(local_timestamp, str) else None,
            )
        )
    return parsed


def parse_sleep_samples(payload: Any, date: str) -> list[Sample]:
    mappings = (
        ("sleepHeartRate", "sleep_heart_rate", "bpm", "value"),
        ("hrvData", "sleep_hrv", "ms", "value"),
        ("sleepStress", "sleep_stress", "stress_level", "value"),
        ("sleepLevels", "sleep_stage", "stage", "activityLevel"),
        ("sleepMovement", "sleep_movement", "level", "activityLevel"),
        ("sleepRestlessMoments", "sleep_restless", "count", "value"),
    )
    if not isinstance(payload, dict):
        return []
    parsed = []
    dto = payload.get("dailySleepDTO")
    source_date = dto.get("calendarDate", date) if isinstance(dto, dict) else date
    for key, metric, unit, value_key in mappings:
        parsed.extend(
            _parse_series(
                {"calendarDate": source_date, key: payload.get(key)}, date, "sleep", metric, unit,
                (key,), value_key=value_key
            )
        )
    return parsed


def parse_activity_detail_samples(activity_id: int | str, payload: Any, date: str) -> list[Sample]:
    if not isinstance(payload, dict):
        return []
    from garminconnect import parse_activity_detail_metrics

    mappings = {
        "directHeartRate": ("heart_rate", "bpm"),
        "directSpeed": ("speed", "m/s"),
        "directPower": ("power", "watts"),
        "directCadence": ("cadence", "rpm"),
        "directElevation": ("elevation", "m"),
    }
    parsed: list[Sample] = []
    for index, values in enumerate(parse_activity_detail_metrics(payload)):
        timestamp = values.get("directTimestamp")
        observed_at = _as_timestamp(timestamp)
        if observed_at is None:
            continue
        for source_metric, (metric, unit) in mappings.items():
            value = _sample_value(values.get(source_metric))
            if value is None:
                continue
            parsed.append(
                Sample(
                    source_endpoint="activity_details",
                    source_id=str(activity_id),
                    metric=metric,
                    observed_at=observed_at,
                    source_date=date,
                    sample_index=index,
                    value=value,
                    unit=unit,
                    local_timestamp=None,
                )
            )
    return parsed


def fetch_stress(client: Any, date: str) -> Any:
    return client.get_stress_data(date)


def fetch_body_battery(client: Any, date: str) -> Any:
    return client.get_body_battery(date, date)


def fetch_heart_rates(client: Any, date: str) -> Any:
    return client.get_heart_rates(date)


def fetch_spo2(client: Any, date: str) -> Any:
    return client.get_spo2_data(date)


def fetch_respiration(client: Any, date: str) -> Any:
    return client.get_respiration_data(date)


def fetch_daily_movement(client: Any, date: str) -> Any:
    return client.get_daily_movement(date)


def fetch_training_readiness(client: Any, date: str) -> Any:
    return client.get_training_readiness(date)


SAMPLE_ENDPOINTS: dict[str, SampleEndpoint] = {
    endpoint.name: endpoint
    for endpoint in (
        SampleEndpoint("stress", fetch_stress, parse_stress_samples),
        SampleEndpoint("body_battery", fetch_body_battery, parse_body_battery_samples),
        SampleEndpoint("heart_rates", fetch_heart_rates, parse_heart_rate_samples),
        SampleEndpoint("spo2", fetch_spo2, parse_spo2_samples),
        SampleEndpoint("respiration", fetch_respiration, parse_respiration_samples),
        SampleEndpoint("daily_movement", fetch_daily_movement, parse_daily_movement_samples),
        SampleEndpoint(
            "training_readiness", fetch_training_readiness, parse_training_readiness_samples
        ),
    )
}


# --- hrv -> hrv -------------------------------------------------------------


def fetch_hrv(client: Any, date: str) -> Any:
    return client.get_hrv_data(date)


def parse_hrv(payload: Any, date: str) -> list[ParsedRow]:
    summary = _get(payload, "hrvSummary")
    if not isinstance(summary, dict):
        return []  # endpoint returns None (or no summary) on days without HRV
    row = {
        "date": date,
        "last_night_avg": summary.get("lastNightAvg"),
        "weekly_avg": summary.get("weeklyAvg"),
        "high_5min": summary.get("lastNight5MinHigh"),
        "status": _lower(summary.get("status")),
        "baseline_low": _get(summary, "baseline", "balancedLow"),
        "baseline_high": _get(summary, "baseline", "balancedUpper"),
    }
    if not _has_data(row):
        return []
    row["source"] = "api"
    return [("hrv", row)]


# --- training_status -> training_status -------------------------------------


def fetch_training_status(client: Any, date: str) -> Any:
    return client.get_training_status(date)


def parse_training_status(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict):
        return []
    status = acute_load = load_ratio = None
    device_map = _get(payload, "mostRecentTrainingStatus", "latestTrainingStatusData")
    if isinstance(device_map, dict) and device_map:
        device = device_map[next(iter(device_map))]  # keyed by deviceId; take the first
        status = _lower(_get(device, "trainingStatusFeedbackPhrase"))
        acute_load = _get(device, "acuteTrainingLoadDTO", "dailyTrainingLoadAcute")
        load_ratio = _get(device, "acuteTrainingLoadDTO", "dailyAcuteChronicWorkloadRatio")
    row = {
        "date": date,
        "status": status,
        "vo2max": _get(payload, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue"),
        "acute_load": acute_load,
        "load_ratio": load_ratio,
    }
    if not _has_data(row):
        return []
    return [("training_status", row)]


# --- fitnessage -> training_status (partial) --------------------------------


def fetch_fitnessage(client: Any, date: str) -> Any:
    return client.get_fitnessage_data(date)


def parse_fitnessage(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, dict):
        return []
    # Partial row: contributes two columns to training_status without touching
    # what the training_status endpoint wrote (or will write) - the same
    # mechanism sleep uses for skin_temp_dev_c in daily_wellness. Component
    # breakdowns (rhr, bmi, vigorous minutes) stay in the raw snapshot.
    row = {
        "date": date,
        "fitness_age": _round2(payload.get("fitnessAge")),
        "achievable_fitness_age": _round2(payload.get("achievableFitnessAge")),
    }
    if not _has_data(row):
        return []
    return [("training_status", row)]


# --- activities -> activities ------------------------------------------------


def fetch_activities(client: Any, date: str) -> Any:
    return client.get_activities_by_date(date, date)


def activity_date(item: dict, fallback: str | None = None) -> str | None:
    start = item.get("startTimeLocal")
    return start[:10] if isinstance(start, str) else fallback


def parse_activities(payload: Any, date: str) -> list[ParsedRow]:
    if not isinstance(payload, list):
        return []
    rows: list[ParsedRow] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("activityId") is None:
            continue
        activity_id = item["activityId"]
        start = item.get("startTimeLocal")
        duration = item.get("duration")
        distance = item.get("distance")
        pace = duration / (distance / 1000) if duration is not None and distance else None
        rows.append(
            (
                "activities",
                {
                    "activity_id": activity_id,
                    "date": activity_date(item, fallback=date or None),
                    "start_ts": start.replace(" ", "T") if isinstance(start, str) else None,
                    "name": item.get("activityName"),
                    "type": _get(item, "activityType", "typeKey"),
                    "duration_s": duration,
                    "distance_m": distance,
                    "elevation_gain_m": item.get("elevationGain"),
                    "avg_hr": _int(item.get("averageHR")),
                    "max_hr": _int(item.get("maxHR")),
                    "calories": _int(item.get("calories")),
                    "avg_pace_s_per_km": pace,
                    "training_load": item.get("activityTrainingLoad"),
                    "aerobic_te": item.get("aerobicTrainingEffect"),
                    "anaerobic_te": item.get("anaerobicTrainingEffect"),
                    "raw_path": f"activities/{activity_id}.json",
                },
            )
        )
    return rows


# body_battery and rhr_day endpoints are deliberately absent: usersummary
# already carries their useful fields (body battery high/low, resting HR).
ENDPOINTS: dict[str, Endpoint] = {
    e.name: e
    for e in (
        Endpoint("usersummary", fetch_usersummary, parse_usersummary),
        Endpoint("sleep", fetch_sleep, parse_sleep),
        Endpoint("hrv", fetch_hrv, parse_hrv),
        Endpoint("training_status", fetch_training_status, parse_training_status),
        Endpoint("fitnessage", fetch_fitnessage, parse_fitnessage),
        Endpoint("activities", fetch_activities, parse_activities),
    )
}
