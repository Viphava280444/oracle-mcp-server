# Oracle MCP Server

Custom MCP server for Oracle database. The LLM just calls `query` with the connection name and SQL and the MCP handles the rest. It can query all databases in parallel.

## How it works

The server uses FastMCP and oracledb (thick mode). It keeps database connections open and ready so queries are fast. Any MCP client can connect to it via `streamable_http`.

```
MCP Client → streamable_http → Oracle MCP Server (port 8100) → connection pool → Oracle DB
```

All queries are read-only — write operations (INSERT, UPDATE, DELETE, DROP, etc.) are rejected. Results are auto-limited to 100 rows unless you specify a FETCH FIRST clause.

## Tools available to the LLM

- `list_connections` — show all configured databases
- `query(connection_name, sql)` — run a read-only SQL query
- `list_tables(connection_name)` — list all tables in a database
- `describe_table(connection_name, table_name)` — show columns and types for a table

You can also add helper tools (like `get_recent_runs`, `get_express_config`) in `config.yaml` — just add SQL templates, no code changes needed.

## Prerequisites

- **Python 3.12+**
- **Oracle Instant Client** — install the basic package for your platform. On RHEL/CentOS:
  ```bash
  sudo yum install oracle-instantclient19.19-basic
  ```
  The default path is `/usr/lib/oracle/19.19/client64/lib`. Update `oracle_client_lib` in `config.yaml` if yours is different.

## Setup

```bash
git clone <repo-url>
cd oracle-mcp-server

# create venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# copy the example config and edit it with your connections
cp config.example.yaml config.yaml
```

## Config

Everything is in `config.yaml`. You can also point to a different config with the `ORACLE_MCP_CONFIG` env var.

```yaml
server:
  host: 0.0.0.0
  port: 8100
  query_timeout: 30

oracle_client_lib: /usr/lib/oracle/19.19/client64/lib

connections:
  my_database:
    username: MY_USER
    password_file: /path/to/password.txt
    dsn: hostname:port/service_name
    pool_min: 1
    pool_max: 5
```

- **password_file**: a plain text file with just the password in it. One file per unique password.
- **dsn**: Oracle connection string in `host:port/service_name` format.
- **pool_min/pool_max**: how many connections to keep in the pool.

To add a new database, just add another entry under `connections`.

## Start the server

```bash
source .venv/bin/activate
LD_LIBRARY_PATH=/usr/lib/oracle/19.19/client64/lib python server.py
```

Server starts on port 8100 by default. You should see the FastMCP banner and `Starting MCP server` in the output.

> For production, recommend running with systemd — see [Run as systemd service](#run-as-systemd-service) below.

## Connect from an MCP client

Point your MCP client to the server's URL. Example config:

```yaml
mcp_servers:
  oracle:
    transport: streamable_http
    url: http://localhost:8100/mcp
```

The client will pick up all tools automatically.

## Adding helper tools

You can add pre-built SQL queries as helper tools in `config.yaml`:

```yaml
helpers:
  get_recent_runs:
    description: "Get the N most recent runs"
    sql: |
      SELECT RUN_ID, STATUS, ACQ_ERA
      FROM RUN ORDER BY RUN_ID DESC
      FETCH FIRST {limit} ROWS ONLY
    params:
      connection_name: {required: true}
      limit: {default: 10, type: int}
```

The LLM sees these as dedicated tools (e.g. `get_recent_runs(connection_name, limit)`) so it knows exactly what to call without having to figure out the SQL.

## Run as systemd service

1. Edit `oracle-mcp.service` to match your setup — update `User`, `WorkingDirectory`, `LD_LIBRARY_PATH`, and the `ExecStart` path:

```ini
[Unit]
Description=Oracle MCP Server
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/oracle-mcp-server
Environment=PATH=/path/to/oracle-mcp-server/.venv/bin:/usr/bin:/bin
Environment=LD_LIBRARY_PATH=/usr/lib/oracle/19.19/client64/lib
ExecStart=/path/to/oracle-mcp-server/.venv/bin/python server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

2. Copy, enable, and start:

```bash
sudo cp oracle-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable oracle-mcp
sudo systemctl start oracle-mcp
```

3. Check if it's running:

```bash
sudo systemctl status oracle-mcp
```

4. View logs:

```bash
journalctl -u oracle-mcp -f
```

5. Restart or stop:

```bash
sudo systemctl restart oracle-mcp
sudo systemctl stop oracle-mcp
```

## Tests

```bash
source .venv/bin/activate
LD_LIBRARY_PATH=/usr/lib/oracle/19.19/client64/lib pytest tests/ -v
```
