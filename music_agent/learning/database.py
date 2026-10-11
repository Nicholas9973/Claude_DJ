"""The SQLite file that memory and playlists share, plus generic browsing and editing.

Each feature (``Memory``, ``PlaylistStore``) adds its own tables with
``add_schema``, and can register checks with ``add_check`` that every GUI edit
must pass (on top of the tables' own CHECK constraints). This class only knows
how to run SQL safely from several threads, and how to show and edit any of the
tables in the GUI.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

TIME_COLUMNS = ("ts", "created", "updated")  # hold ISO times like "2026-10-08T18:30:00"
RowCheck = Callable[[dict[str, Any]], None]   # raises ValueError if a row is invalid


class Database:
    """One SQLite connection, shared and thread-safe.

    Args:
        path: Database file, or ``":memory:"`` for tests.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")  # so deleting a playlist deletes its tracks
        self._checks: dict[str, list[RowCheck]] = {}

    def add_schema(self, sql: str) -> None:
        """Create tables and indexes (statements should use ``IF NOT EXISTS``)."""
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def add_check(self, table: str, check: RowCheck) -> None:
        """Register a rule that rows edited or added in the GUI must follow.

        Args:
            table: Table the rule is for.
            check: Called with the row as it would be saved; raises ValueError
                to refuse the change (nothing is saved then).
        """
        self._checks.setdefault(table, []).append(check)

    def ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add a column to an existing table if it's missing (a tiny migration).

        Args:
            table: Table name (ours, never user input).
            column: Column to add.
            decl: Its SQL type, e.g. ``"TEXT"``.
        """
        if column not in self.columns(table):
            with self._lock:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                self._conn.commit()

    def insert(self, table: str, row: dict[str, Any]) -> int:
        """Insert one row (table and column names are ours, never user input); return its id."""
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
            self._conn.commit()
            return cur.lastrowid

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        """Run a SELECT and return all rows."""
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def execute(self, sql: str, params: Iterable = ()) -> int:
        """Run one UPDATE/DELETE and commit.

        Returns:
            Number of rows changed.
        """
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.rowcount

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several statements as one unit: all are saved, or none if an error is raised."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ browsing and editing (GUI)
    def tables(self) -> list[dict[str, Any]]:
        """List the tables with their row counts."""
        names = [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return [{"name": n, "rows": self.query(f"SELECT COUNT(*) AS n FROM {n}")[0]["n"]} for n in names]

    def columns(self, table: str) -> list[str]:
        """Column names of a table, in order (empty if there's no such table)."""
        return [r["name"] for r in self.query(f"PRAGMA table_info({table})")]

    def column_info(self, table: str) -> list[dict[str, Any]]:
        """Describe a table's columns, so the GUI can pick a fitting editor for each.

        Returns:
            ``{"name", "type", "required", "choices"}`` per column, in order.
            ``choices`` lists the allowed values when the table has a
            ``CHECK (column IN (...))`` constraint, else None.

        Raises:
            KeyError: Unknown table.
        """
        self._check_table(table)
        sql = self.query("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))[0]["sql"]
        choices = _check_choices(sql or "")
        return [{"name": r["name"], "type": (r["type"] or "").upper(), "required": bool(r["notnull"]),
                 "choices": choices.get(r["name"])}
                for r in self.query(f"PRAGMA table_info({table})")]

    def rows(self, table: str, limit: int = 50, offset: int = 0, search: str | None = None) -> dict[str, Any]:
        """Read one page of a table, newest first.

        Args:
            table: Table name; must be one listed by ``tables()``.
            limit: Rows per page, clamped to 1-500.
            offset: Rows to skip.
            search: Only rows where some column contains this text (ignoring case).

        Returns:
            ``{"table", "columns", "info", "rows", "total"}``; rows are lists in
            column order, ``info`` is ``column_info()``, and ``total`` counts
            the matching rows.

        Raises:
            KeyError: Unknown table.
        """
        info = self.column_info(table)
        limit = max(1, min(int(limit), 500))
        where, params = "", []
        if search and search.strip():
            # Column names come from the table itself, never from the request.
            where = "WHERE " + " OR ".join(f"CAST({c['name']} AS TEXT) LIKE ?" for c in info)
            params = [f"%{search.strip()}%"] * len(info)
        with self._lock:
            cur = self._conn.execute(f"SELECT * FROM {table} {where} ORDER BY rowid DESC LIMIT ? OFFSET ?",
                                     (*params, limit, max(0, int(offset))))
            columns = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchall()]
            total = self._conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
        return {"table": table, "columns": columns, "info": info, "rows": rows, "total": total}

    def update_row(self, table: str, row_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """Change some columns of one row, found by its ``id``.

        The table's CHECK constraints and the checks registered with
        ``add_check`` apply, so e.g. a feedback ``sentiment`` of 5, or a ``ts``
        that isn't a date, is refused and nothing changes.

        Args:
            table: Table name.
            row_id: The row's ``id``.
            values: ``{column: new value}``; ``id`` can't be changed.

        Returns:
            The row after the change.

        Raises:
            KeyError: Unknown table or row.
            ValueError: Unknown column, ``id``, nothing to change, or a check failed.
            sqlite3.IntegrityError: The new values break a constraint.
        """
        self._check_table(table)
        allowed = set(self.columns(table)) - {"id"}
        bad = [c for c in values if c not in allowed]
        if bad or not values:
            raise ValueError(f"Can't edit {', '.join(bad) or 'nothing'}; editable columns: {', '.join(sorted(allowed))}")
        sets = ", ".join(f"{c} = ?" for c in values)  # names checked against the table above
        with self.transaction() as conn:
            if not conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id]).rowcount:
                raise KeyError(row_id)
            row = dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())
            self._validate(table, row)  # raising here rolls the change back
        return row

    def insert_row(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        """Add a row by hand (the GUI's "Add row"), with the same checks as ``update_row``.

        Time columns (``ts``, ``created``, ``updated``) left empty get the current time.

        Args:
            table: Table name.
            values: ``{column: value}``; ``id`` is assigned automatically.

        Returns:
            The new row.

        Raises:
            KeyError: Unknown table.
            ValueError: Unknown column, or a check failed.
            sqlite3.IntegrityError: A required column is missing or a constraint is broken.
        """
        self._check_table(table)
        columns = self.columns(table)
        bad = [c for c in values if c not in columns or c == "id"]
        if bad:
            raise ValueError(f"Can't set {', '.join(bad)}; columns: {', '.join(c for c in columns if c != 'id')}")
        row = {k: v for k, v in values.items() if v is not None}
        now = datetime.now().isoformat(timespec="seconds")
        row.update({c: now for c in TIME_COLUMNS if c in columns and row.get(c) in (None, "")})
        if row:
            sql = f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})"
        else:
            sql = f"INSERT INTO {table} DEFAULT VALUES"
        with self.transaction() as conn:
            new_id = conn.execute(sql, tuple(row.values())).lastrowid
            saved = dict(conn.execute(f"SELECT * FROM {table} WHERE rowid = ?", (new_id,)).fetchone())
            self._validate(table, saved)  # raising here rolls the insert back
        return saved

    def delete_row(self, table: str, row_id: int) -> bool:
        """Delete one row by its ``id``.

        Returns:
            True if a row was deleted.

        Raises:
            KeyError: Unknown table.
        """
        self._check_table(table)
        return self.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,)) > 0

    def _validate(self, table: str, row: dict[str, Any]) -> None:
        """Check time columns hold real times, then run the table's registered checks."""
        for column in TIME_COLUMNS:
            value = row.get(column)
            if value is not None:
                try:
                    datetime.fromisoformat(str(value))
                except ValueError:
                    raise ValueError(f"{column} must be a date and time like 2026-10-08T18:30:00, "
                                     f"not '{value}'.") from None
        for check in self._checks.get(table, []):
            check(row)

    def _check_table(self, table: str) -> None:
        """Raise KeyError unless ``table`` is one of ours (also keeps names out of SQL injection)."""
        if table not in {t["name"] for t in self.tables()}:
            raise KeyError(table)


def _check_choices(create_sql: str) -> dict[str, list[Any]]:
    """Find ``CHECK (column IN (a, b, ...))`` constraints in a CREATE TABLE statement.

    Returns:
        ``{column: [allowed values]}``; numbers stay numbers, quoted text loses its quotes.
    """
    found = {}
    pattern = r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)\s*\)"
    for column, values in re.findall(pattern, create_sql, re.IGNORECASE):
        found[column] = [_sql_literal(v.strip()) for v in values.split(",") if v.strip()]
    return found


def _sql_literal(text: str) -> Any:
    """``'track'`` -> ``"track"``, ``-1`` -> ``-1``."""
    if text[:1] in ("'", '"'):
        return text[1:-1]
    try:
        return int(text)
    except ValueError:
        return float(text)
