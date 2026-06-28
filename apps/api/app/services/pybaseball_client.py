from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import importlib
import math
import numbers
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


def _is_missing_scalar(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float):
        return not math.isfinite(value)
    try:
        comparison = value != value
        if bool(comparison):
            return True
    except Exception:
        pass
    return str(value).strip().lower() in {"<na>", "nan", "nat"}


def _json_safe_value(value: object) -> object:
    if _is_missing_scalar(value):
        return None
    if isinstance(value, bool | str):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe_value(child) for child in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe_value(item())
        except Exception:
            pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except Exception:
            pass
    return str(value)


def _json_safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{str(key): _json_safe_value(value) for key, value in row.items()} for row in rows]


def _records_from_frame(frame: object) -> PybaseballResult:
    if isinstance(frame, PybaseballResult):
        return PybaseballResult(
            function=frame.function,
            rows=_json_safe_rows(frame.rows),
            row_count=frame.row_count,
            columns=frame.columns,
        )
    if isinstance(frame, dict) and isinstance(frame.get("rows"), list):
        rows = _json_safe_rows([row for row in frame["rows"] if isinstance(row, dict)])
        columns = frame.get("columns")
        return PybaseballResult(
            function=str(frame.get("function") or "pybaseball"),
            rows=rows,
            row_count=int(frame.get("row_count") or len(rows)),
            columns=[str(column) for column in columns] if isinstance(columns, list) else _columns_from_rows(rows),
        )
    if isinstance(frame, list):
        rows = _json_safe_rows([row for row in frame if isinstance(row, dict)])
        return PybaseballResult(function="pybaseball", rows=rows, row_count=len(rows), columns=_columns_from_rows(rows))

    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            rows = to_dict("records")
        except TypeError:
            rows = to_dict(orient="records")
        if not isinstance(rows, list):
            rows = []
        typed_rows = _json_safe_rows([row for row in rows if isinstance(row, dict)])
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
