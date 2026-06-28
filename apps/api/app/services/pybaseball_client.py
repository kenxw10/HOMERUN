from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import importlib
from typing import Any


class PybaseballSourceError(RuntimeError):
    def __init__(self, message: str, *, function_name: str, error: BaseException) -> None:
        super().__init__(message)
        self.function_name = function_name
        self.error = error

    def to_detail(self) -> dict[str, object]:
        return {
            "source": "pybaseball_public_stats_v1",
            "function": self.function_name,
            "error_type": self.error.__class__.__name__,
            "message": str(self.error),
        }


@dataclass(frozen=True)
class PybaseballResult:
    function: str
    rows: list[dict[str, Any]]
    row_count: int
    columns: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "function": self.function,
            "rows": self.rows,
            "row_count": self.row_count,
            "columns": self.columns,
        }


def import_status() -> dict[str, object]:
    try:
        module = importlib.import_module("pybaseball")
    except Exception as exc:
        return {
            "available": False,
            "version": None,
            "module_path": None,
            "import_error": {
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            },
        }
    return {
        "available": True,
        "version": getattr(module, "__version__", None),
        "module_path": getattr(module, "__file__", None),
        "import_error": None,
    }


def _load_pybaseball():
    try:
        return importlib.import_module("pybaseball")
    except Exception as exc:
        raise PybaseballSourceError("pybaseball import failed.", function_name="import_pybaseball", error=exc) from exc


def _records_from_frame(frame: object) -> PybaseballResult:
    if isinstance(frame, PybaseballResult):
        return frame
    if isinstance(frame, dict) and isinstance(frame.get("rows"), list):
        rows = [row for row in frame["rows"] if isinstance(row, dict)]
        columns = frame.get("columns")
        return PybaseballResult(
            function=str(frame.get("function") or "pybaseball"),
            rows=rows,
            row_count=int(frame.get("row_count") or len(rows)),
            columns=[str(column) for column in columns] if isinstance(columns, list) else _columns_from_rows(rows),
        )
    if isinstance(frame, list):
        rows = [row for row in frame if isinstance(row, dict)]
        return PybaseballResult(function="pybaseball", rows=rows, row_count=len(rows), columns=_columns_from_rows(rows))

    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            rows = to_dict("records")
        except TypeError:
            rows = to_dict(orient="records")
        if not isinstance(rows, list):
            rows = []
        typed_rows = [row for row in rows if isinstance(row, dict)]
        columns = getattr(frame, "columns", [])
        return PybaseballResult(
            function="pybaseball",
            rows=typed_rows,
            row_count=len(typed_rows),
            columns=[str(column) for column in columns],
        )

    return PybaseballResult(function="pybaseball", rows=[], row_count=0, columns=[])


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            name = str(key)
            if name not in seen:
                columns.append(name)
                seen.add(name)
    return columns


def _call(function_name: str, *args: object, **kwargs: object) -> dict[str, object]:
    module = _load_pybaseball()
    function = getattr(module, function_name, None)
    if not callable(function):
        error = AttributeError(f"pybaseball.{function_name} is not available")
        raise PybaseballSourceError("pybaseball function unavailable.", function_name=function_name, error=error)
    try:
        frame = function(*args, **kwargs)
    except TypeError:
        try:
            frame = function(*args)
        except Exception as exc:
            raise PybaseballSourceError("pybaseball call failed.", function_name=function_name, error=exc) from exc
    except Exception as exc:
        raise PybaseballSourceError("pybaseball call failed.", function_name=function_name, error=exc) from exc

    result = _records_from_frame(frame)
    return PybaseballResult(
        function=function_name,
        rows=result.rows,
        row_count=result.row_count,
        columns=result.columns,
    ).to_dict()


def get_batting_stats(season: int) -> dict[str, object]:
    return _call("batting_stats", season, qual=0)


def get_pitching_stats(season: int) -> dict[str, object]:
    return _call("pitching_stats", season, qual=0)


def get_recent_batting_range(start_date: date, end_date: date) -> dict[str, object]:
    return _call("batting_stats_range", start_date.isoformat(), end_date.isoformat())


def get_recent_pitching_range(start_date: date, end_date: date) -> dict[str, object]:
    return _call("pitching_stats_range", start_date.isoformat(), end_date.isoformat())


def get_statcast_range(start_date: date, end_date: date) -> dict[str, object]:
    return _call("statcast", start_date.isoformat(), end_date.isoformat())


def get_pitcher_statcast_range(player_id: str, start_date: date, end_date: date) -> dict[str, object]:
    return _call("statcast_pitcher", start_date.isoformat(), end_date.isoformat(), player_id)
