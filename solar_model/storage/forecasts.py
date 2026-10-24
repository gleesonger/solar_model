from __future__ import annotations
from datetime import datetime
from typing import Any
from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from solar_model.config import SolarArrayConfig
from solar_model.time_utils import utc_instant

from solar_model.storage.schema import DatabaseMetadata, ForecastLoadSample, ForecastSolarSample, PV_ARRAY_IDS

class ForecastStorage:

    def has_load_forecast(self, target_day: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(ForecastLoadSample.target_day).where(
                ForecastLoadSample.target_day == target_day,
            ).limit(1)) is not None

    def save_daily_load_forecast(self, target_day: str, collected_at: str, values: list[dict]) -> int:
        # Serialize the check and entire profile write. A restart/concurrent
        # collector must never overwrite or partially extend a frozen day.
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            table = ForecastLoadSample.__table__
            if connection.scalar(select(table.c.target_day).where(table.c.target_day == target_day).limit(1)) is not None:
                connection.rollback()
                return 0
            connection.execute(table.insert(), [dict(target_day=target_day, collected_at_utc=collected_at, **row) for row in values])
            connection.commit()
            return len(values)

    def claim_forecast_collection(self, now: datetime, interval_seconds: int) -> tuple[bool, datetime | None]:
        """Reserve a due external scan, retaining its attempt time across restarts."""
        now = utc_instant(now)
        key = "forecast_last_attempt_utc"
        metadata = DatabaseMetadata.__table__
        with self.engine.begin() as connection:
            # Serialize the due check/reservation, then release before HTTP work.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            attempted = connection.scalar(select(metadata.c.value).where(metadata.c.key == key))
            # Existing installations have snapshots but no attempt marker yet.
            saved = connection.scalar(select(ForecastSolarSample.collected_at_utc)
                                      .order_by(ForecastSolarSample.collected_at_utc.desc()).limit(1))
            previous = [utc_instant(datetime.fromisoformat(value)) for value in (attempted, saved) if value is not None]
            last = max(previous) if previous else None
            if last is not None and (now - last).total_seconds() < interval_seconds:
                return False, last
            statement = sqlite_insert(metadata).values(key=key, value=now.isoformat())
            connection.execute(statement.on_conflict_do_update(index_elements=[metadata.c.key], set_={"value": now.isoformat()}))
        return True, last

    def save_forecast(
        self,
        array: SolarArrayConfig,
        payload: dict,
        collected: tuple[str, str],
    ) -> int:
        result = payload.get("result", {})
        watts = result.get("watts", {})
        watt_hours = result.get("watt_hours", {})
        daily = result.get("watt_hours_day", {})
        prefix = f"panel_{array.panel_id}"
        fields = (f"{prefix}_raw_watts", f"{prefix}_raw_watt_hours", f"{prefix}_raw_watt_hours_day")
        if any(field not in ForecastSolarSample.__table__.c for field in fields):
            raise ValueError(
                f"forecast panel ID {array.panel_id!r} is not declared in ForecastSolarSample"
            )

        rows = 0
        with self.sessions.begin() as session:
            for forecast_time in sorted(set(watts) | set(watt_hours)):
                values: dict[str, Any] = {
                    "collected_at_utc": collected[0],
                    "forecast_time": forecast_time,
                    fields[0]: watts.get(forecast_time),
                    fields[1]: watt_hours.get(forecast_time),
                    fields[2]: daily.get(forecast_time[:10]),
                }
                row = session.scalar(select(ForecastSolarSample).where(
                    ForecastSolarSample.collected_at_utc == collected[0],
                    ForecastSolarSample.forecast_time == forecast_time,
                ))
                if row is None:
                    session.add(ForecastSolarSample(**values))
                else:
                    for name, value in values.items():
                        setattr(row, name, value)
                rows += 1
        return rows

    @staticmethod
    def _pending_adjusted_condition():
        """Return rows with at least one raw panel still lacking adjustment."""
        return or_(*(
            getattr(ForecastSolarSample, f"panel_{panel_id}_raw_watts").is_not(None)
            & getattr(ForecastSolarSample, f"panel_{panel_id}_adj_watts").is_(None)
            for panel_id in PV_ARRAY_IDS
        ))

    def pending_adjusted_forecasts(
        self,
        limit: int | None = None,
        collected_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw forecast rows which have not yet been adjusted.

        The returned dictionaries intentionally include all raw and adjusted
        columns.  This is the persistence boundary used by the adjustment
        model and its resumable backfill: a row stops being pending once every
        panel with a raw power forecast has an adjusted power forecast.
        """
        statement = select(ForecastSolarSample).where(self._pending_adjusted_condition())
        if collected_at is not None:
            statement = statement.where(ForecastSolarSample.collected_at_utc == collected_at)
        statement = statement.order_by(
            ForecastSolarSample.collected_at_utc,
            ForecastSolarSample.forecast_time,
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.sessions() as session:
            rows = session.scalars(statement).all()
        columns = tuple(ForecastSolarSample.__table__.columns.keys())
        return [{column: getattr(row, column) for column in columns} for row in rows]

    def pending_adjusted_forecast_snapshots(self, limit: int) -> list[str]:
        """Return a bounded, oldest-first set of incomplete snapshots."""
        if limit < 1:
            raise ValueError("limit must be at least one snapshot")
        statement = (
            select(ForecastSolarSample.collected_at_utc)
            .where(self._pending_adjusted_condition())
            .distinct()
            .order_by(ForecastSolarSample.collected_at_utc)
            .limit(limit)
        )
        with self.sessions() as session:
            return list(session.scalars(statement))

    def save_adjusted_forecast(
        self,
        collected_at: str,
        forecast_time: str,
        values: dict[str, float | None],
    ) -> None:
        """Persist adjusted values for one existing forecast snapshot row.

        ``values`` may contain only ``panel_n_adj_*`` fields.  The explicit
        restriction prevents an adjustment/backfill job from ever changing the
        immutable raw API response.
        """
        self.save_adjusted_forecasts(collected_at, {forecast_time: values})

    def save_adjusted_forecasts(
        self,
        collected_at: str,
        values_by_forecast_time: dict[str, dict[str, float | None]],
    ) -> None:
        """Atomically save all adjusted rows belonging to one snapshot."""
        if not values_by_forecast_time:
            return
        allowed = {
            column.name for column in ForecastSolarSample.__table__.columns
            if "_adj_" in column.name
        }
        for values in values_by_forecast_time.values():
            unexpected = set(values) - allowed
            if unexpected:
                raise ValueError(f"only adjusted forecast fields may be saved: {sorted(unexpected)!r}")
        with self.sessions.begin() as session:
            for forecast_time, values in values_by_forecast_time.items():
                row = session.scalar(select(ForecastSolarSample).where(
                    ForecastSolarSample.collected_at_utc == collected_at,
                    ForecastSolarSample.forecast_time == forecast_time,
                ))
                if row is None:
                    raise ValueError("cannot save adjusted forecast for a missing raw forecast row")
                for name, value in values.items():
                    setattr(row, name, value)

    def save_adjusted_forecasts_bulk(
        self,
        values_by_snapshot: dict[str, dict[str, dict[str, float | None]]],
    ) -> None:
        """Save several provider snapshots in one transaction.

        This is used only by the historical rebuild.  It deliberately keeps
        the same field validation as the normal single-snapshot writer.
        """
        allowed = {
            column.name for column in ForecastSolarSample.__table__.columns
            if "_adj_" in column.name
        }
        with self.sessions.begin() as session:
            for collected_at, values_by_forecast_time in values_by_snapshot.items():
                if not values_by_forecast_time:
                    continue
                for values in values_by_forecast_time.values():
                    unexpected = set(values) - allowed
                    if unexpected:
                        raise ValueError(f"only adjusted forecast fields may be saved: {sorted(unexpected)!r}")
                rows = session.scalars(select(ForecastSolarSample).where(
                    ForecastSolarSample.collected_at_utc == collected_at,
                )).all()
                rows_by_time = {row.forecast_time: row for row in rows}
                for forecast_time, values in values_by_forecast_time.items():
                    row = rows_by_time.get(forecast_time)
                    if row is None:
                        raise ValueError("cannot save adjusted forecast for a missing raw forecast row")
                    for name, value in values.items():
                        setattr(row, name, value)
