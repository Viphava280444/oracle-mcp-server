"""Oracle connection pool manager.

Loads connection definitions from a YAML config file and creates
oracledb connection pools lazily on first use. Pools are reused
across tool calls for low latency.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator

import oracledb
import yaml


class OraclePoolManager:
    """Manages named oracledb connection pools from a YAML config."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        with open(config_path) as f:
            self._config = yaml.safe_load(f)

        lib_dir = self._config.get("oracle_client_lib")
        if lib_dir:
            oracledb.init_oracle_client(lib_dir=lib_dir)

        self._pools: Dict[str, oracledb.ConnectionPool] = {}
        self._conn_configs: Dict[str, Dict[str, Any]] = self._config.get("connections", {})

    @property
    def server_config(self) -> Dict[str, Any]:
        """Return the server section of the config."""
        return self._config.get("server", {})

    @property
    def connections_config(self) -> Dict[str, Dict[str, Any]]:
        """Return a copy of all connection configs."""
        return dict(self._conn_configs)

    @property
    def helpers_config(self) -> Dict[str, Dict[str, Any]]:
        """Return the helpers section of the config."""
        return self._config.get("helpers", {})

    def _read_password(self, password_file: str) -> str:
        return Path(password_file).read_text().strip()

    def _get_or_create_pool(self, name: str) -> oracledb.ConnectionPool:
        if name not in self._conn_configs:
            available = ", ".join(sorted(self._conn_configs.keys()))
            raise ValueError(f"Connection '{name}' not found. Available: {available}")

        if name not in self._pools:
            cfg = self._conn_configs[name]
            password = self._read_password(cfg["password_file"])
            self._pools[name] = oracledb.create_pool(
                user=cfg["username"],
                password=password,
                dsn=cfg["dsn"],
                min=cfg.get("pool_min", 1),
                max=cfg.get("pool_max", 5),
                increment=1,
            )

        return self._pools[name]

    @contextmanager
    def get_connection(self, name: str) -> Generator[oracledb.Connection, None, None]:
        """Acquire a connection from the named pool. Auto-released on exit."""
        pool = self._get_or_create_pool(name)
        conn = pool.acquire()
        try:
            yield conn
        finally:
            pool.release(conn)

    def close_all(self) -> None:
        """Close all connection pools."""
        for pool in self._pools.values():
            try:
                pool.close(force=True)
            except Exception:
                pass
        self._pools.clear()
