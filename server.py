"""Oracle MCP Server.

Exposes Oracle database operations as MCP tools via streamable HTTP.
Connection pools are managed internally — the LLM never calls connect/disconnect.
"""

from __future__ import annotations

import inspect
import os
import re
from typing import Any, Dict, Optional

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from oracle_pool import OraclePoolManager

# ── Constants ────────────────────────────────────────────────────────────────

_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_MAX_ROWS_DEFAULT = 100

# ── Init ─────────────────────────────────────────────────────────────────────

_config_path = os.environ.get(
    "ORACLE_MCP_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
)

pool_manager = OraclePoolManager(_config_path)
_server_cfg = pool_manager.server_config
_query_timeout = _server_cfg.get("query_timeout", 30)

mcp = FastMCP("Oracle MCP Server")

# ── Formatting ───────────────────────────────────────────────────────────────


def _format_rows(cursor, sql: str, connection_name: str) -> str:
    """Format cursor results as a text table with headers."""
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    lines = [
        f"Query: {sql}",
        f"Connection: {connection_name}",
        f"Rows: {len(rows)}",
        "",
    ]

    if not rows:
        lines.append("(no rows returned)")
        return "\n".join(lines)

    col_widths = [len(c) for c in columns]
    str_rows = []
    for row in rows:
        str_row = [str(v) if v is not None else "NULL" for v in row]
        str_rows.append(str_row)
        for i, val in enumerate(str_row):
            col_widths[i] = max(col_widths[i], len(val))

    header = "  ".join(c.ljust(w) for c, w in zip(columns, col_widths))
    separator = "  ".join("─" * w for w in col_widths)
    lines.append(header)
    lines.append(separator)

    for str_row in str_rows:
        line = "  ".join(v.ljust(w) for v, w in zip(str_row, col_widths))
        lines.append(line)

    return "\n".join(lines)


# ── SQL validation ───────────────────────────────────────────────────────────


def _validate_read_only(sql: str) -> None:
    """Reject DML/DDL statements."""
    if _WRITE_KEYWORDS.search(sql):
        raise ToolError(
            "Write operations are not allowed. This server is read-only. "
            "Rejected: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, MERGE, GRANT, REVOKE."
        )


def _auto_limit(sql: str) -> str:
    """Append FETCH FIRST N ROWS ONLY if no limit clause is present."""
    if not re.search(r"FETCH\s+FIRST\s+\d+\s+ROWS", sql, re.IGNORECASE):
        sql = sql.rstrip().rstrip(";")
        sql += f" FETCH FIRST {_MAX_ROWS_DEFAULT} ROWS ONLY"
    return sql


# ── Core tools ───────────────────────────────────────────────────────────────


@mcp.tool
def list_connections() -> str:
    """List all configured Oracle database connections with their usernames and DSN."""
    lines = ["Available connections:", ""]
    for name, cfg in pool_manager.connections_config.items():
        lines.append(f"  {name}: {cfg['username']} @ {cfg['dsn']}")
    return "\n".join(lines)


@mcp.tool
def query(connection_name: str, sql: str) -> str:
    """Execute a read-only SQL query against an Oracle database.

    Returns formatted results with column headers. Write operations
    (INSERT, UPDATE, DELETE, etc.) are rejected.

    Args:
        connection_name: Name of the connection (see list_connections).
        sql: SQL SELECT statement to execute.
    """
    try:
        _validate_read_only(sql)
        sql = _auto_limit(sql)
        with pool_manager.get_connection(connection_name) as conn:
            conn.call_timeout = _query_timeout * 1000
            cursor = conn.cursor()
            cursor.execute(sql)
            return _format_rows(cursor, sql, connection_name)
    except ToolError:
        raise
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


# ── Introspection tools ─────────────────────────────────────────────────────


@mcp.tool
def list_tables(connection_name: str) -> str:
    """List all tables owned by the user on the specified connection.

    Args:
        connection_name: Name of the connection (see list_connections).
    """
    sql = "SELECT TABLE_NAME FROM USER_TABLES ORDER BY TABLE_NAME"
    try:
        with pool_manager.get_connection(connection_name) as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            return _format_rows(cursor, sql, connection_name)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool
def describe_table(connection_name: str, table_name: str) -> str:
    """Describe columns of a table: name, data type, nullable, and comments.

    Args:
        connection_name: Name of the connection (see list_connections).
        table_name: Name of the table to describe (case-insensitive).
    """
    sql = (
        "SELECT c.COLUMN_NAME, c.DATA_TYPE, c.NULLABLE, cc.COMMENTS "
        "FROM USER_TAB_COLUMNS c "
        "LEFT JOIN USER_COL_COMMENTS cc "
        "ON c.TABLE_NAME = cc.TABLE_NAME AND c.COLUMN_NAME = cc.COLUMN_NAME "
        "WHERE c.TABLE_NAME = :tname "
        "ORDER BY c.COLUMN_ID"
    )
    display_sql = f"DESCRIBE {table_name.upper()}"
    try:
        with pool_manager.get_connection(connection_name) as conn:
            cursor = conn.cursor()
            cursor.execute(sql, {"tname": table_name.upper()})
            return _format_rows(cursor, display_sql, connection_name)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


# ── Config-defined helpers ───────────────────────────────────────────────────


def _register_helpers() -> None:
    """Read helpers from config and register each as an MCP tool."""
    for name, cfg in pool_manager.helpers_config.items():
        _register_one_helper(name, cfg)


def _register_one_helper(name: str, cfg: Dict[str, Any]) -> None:
    """Register a single config-defined helper as an MCP tool."""
    sql_template = cfg["sql"].strip()
    description = cfg.get("description", f"Helper query: {name}")
    params_cfg = cfg.get("params", {})

    # Determine which params are required and which have defaults
    default_values = {}
    param_types = {}
    for pname, pcfg in params_cfg.items():
        ptype = pcfg.get("type", "str")
        param_types[pname] = ptype
        if "default" in pcfg:
            default_values[pname] = pcfg["default"]

    # Build a function with a proper signature so FastMCP can introspect it
    func_params = []
    for pname in params_cfg:
        ptype = param_types[pname]
        annotation = int if ptype == "int" else str
        if pname in default_values:
            p = inspect.Parameter(
                pname,
                inspect.Parameter.KEYWORD_ONLY,
                default=default_values[pname],
                annotation=annotation,
            )
        else:
            p = inspect.Parameter(
                pname,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
            )
        func_params.append(p)

    sig = inspect.Signature(func_params, return_annotation=str)

    def _make_helper_fn(tmpl: str, ptypes: Dict[str, str]) -> Any:
        def helper_fn(**kwargs: Any) -> str:
            conn_name = kwargs.pop("connection_name", None)
            if not conn_name:
                return "Error: connection_name is required."

            # Build final SQL: substitute {int_params} directly, keep :bind_vars
            final_sql = tmpl
            bind_vars = {}
            for pname, pvalue in kwargs.items():
                brace_placeholder = "{" + pname + "}"
                if brace_placeholder in final_sql:
                    # Integer substitution (validated)
                    try:
                        int_val = int(pvalue)
                    except (ValueError, TypeError):
                        return f"Error: parameter '{pname}' must be an integer, got '{pvalue}'."
                    final_sql = final_sql.replace(brace_placeholder, str(int_val))
                else:
                    # Oracle bind variable — already in SQL as :pname
                    bind_vars[pname] = pvalue

            try:
                with pool_manager.get_connection(conn_name) as conn:
                    conn.call_timeout = _query_timeout * 1000
                    cursor = conn.cursor()
                    if bind_vars:
                        cursor.execute(final_sql, bind_vars)
                    else:
                        cursor.execute(final_sql)
                    return _format_rows(cursor, final_sql, conn_name)
            except ValueError as e:
                return f"Error: {e}"
            except Exception as e:
                return f"Error: {e}"

        return helper_fn

    fn = _make_helper_fn(sql_template, param_types)
    fn.__name__ = name
    fn.__doc__ = description
    fn.__signature__ = sig
    fn.__annotations__ = {p.name: p.annotation for p in func_params}
    fn.__annotations__["return"] = str

    mcp.tool(name=name, description=description)(fn)


_register_helpers()

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    host = _server_cfg.get("host", "0.0.0.0")
    port = _server_cfg.get("port", 8100)
    mcp.run(transport="http", host=host, port=port)
