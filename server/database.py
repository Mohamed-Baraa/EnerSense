# =============================================================================
# EnerSense — server/database.py
# SQLite database setup and helper functions.
# Replaces InfluxDB for local simulation and development.
#
# Schema:
#   telemetry       — raw 1Hz readings from ESP32
#   aggregate       — hourly summaries
#   anomaly_events  — flagged anomaly readings
#
# Usage:
#   python server/database.py          # initialises the DB and prints schema
#   from server.database import DB     # import in other scripts
# =============================================================================

import sqlite3
import os
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'enersense.db')


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,        -- Unix timestamp ms
    node        TEXT    NOT NULL,
    appliance   TEXT    NOT NULL,
    I_rms       REAL    DEFAULT 0,
    V_rms       REAL    DEFAULT 0,
    P_W         REAL    DEFAULT 0,
    E_Wh        REAL    DEFAULT 0,
    pf          REAL    DEFAULT 1,
    relay       TEXT    DEFAULT 'OFF',
    anomaly     INTEGER DEFAULT 0        -- 0 = normal, 1 = anomaly
);

CREATE TABLE IF NOT EXISTS aggregate (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    node            TEXT    NOT NULL,
    appliance       TEXT    NOT NULL,
    total_kwh       REAL    DEFAULT 0,
    avg_w           REAL    DEFAULT 0,
    max_w           REAL    DEFAULT 0,
    min_w           REAL    DEFAULT 0,
    cost_tnd        REAL    DEFAULT 0,
    samples         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    node        TEXT    NOT NULL,
    appliance   TEXT    NOT NULL,
    P_W         REAL    DEFAULT 0,
    I_rms       REAL    DEFAULT 0,
    message     TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_telemetry_ts        ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_appliance ON telemetry(appliance);
CREATE INDEX IF NOT EXISTS idx_aggregate_ts        ON aggregate(ts);
CREATE INDEX IF NOT EXISTS idx_anomaly_ts          ON anomaly_events(ts);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_conn():
    """Return a SQLite connection with row_factory set for dict-like access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables and indexes if they don't exist yet.
    If the database file is corrupted, delete it and start fresh."""
    import sqlite3 as _sqlite3
    if os.path.exists(DB_PATH):
        try:
            test = _sqlite3.connect(DB_PATH)
            test.execute("SELECT name FROM sqlite_master LIMIT 1")
            test.close()
        except _sqlite3.DatabaseError:
            print(f"[DB] Corrupted database detected — deleting and recreating.")
            try:
                test.close()
            except Exception:
                pass
            os.remove(DB_PATH)
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"[DB] Initialised → {DB_PATH}")


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def insert_telemetry(data: dict):
    """
    Insert one telemetry reading.

    Expected keys: ts, node, appliance, I_rms, V_rms, P_W, E_Wh, pf,
                   relay, anomaly
    """
    conn = get_conn()
    conn.execute("""
        INSERT INTO telemetry
            (ts, node, appliance, I_rms, V_rms, P_W, E_Wh, pf, relay, anomaly)
        VALUES
            (:ts, :node, :appliance, :I_rms, :V_rms, :P_W, :E_Wh, :pf, :relay, :anomaly)
    """, {
        'ts':        data.get('ts_ms', int(time.time() * 1000)),
        'node':      data.get('node',      'node_01'),
        'appliance': data.get('appliance', 'appliance_01'),
        'I_rms':     data.get('I_rms',     0.0),
        'V_rms':     data.get('V_rms',     0.0),
        'P_W':       data.get('P_W',       0.0),
        'E_Wh':      data.get('E_Wh',      0.0),
        'pf':        data.get('pf',        1.0),
        'relay':     data.get('relay',     'OFF'),
        'anomaly':   1 if data.get('anomaly') else 0,
    })
    conn.commit()
    conn.close()


def insert_aggregate(data: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO aggregate
            (ts, node, appliance, total_kwh, avg_w, max_w, min_w, cost_tnd, samples)
        VALUES
            (:ts, :node, :appliance, :total_kwh, :avg_w, :max_w, :min_w, :cost_tnd, :samples)
    """, {
        'ts':        int(time.time() * 1000),
        'node':      data.get('node',      'node_01'),
        'appliance': data.get('appliance', 'appliance_01'),
        'total_kwh': data.get('total_kwh', 0.0),
        'avg_w':     data.get('avg_w',     0.0),
        'max_w':     data.get('max_w',     0.0),
        'min_w':     data.get('min_w',     0.0),
        'cost_tnd':  data.get('cost_tnd',  0.0),
        'samples':   data.get('samples',   0),
    })
    conn.commit()
    conn.close()


def insert_anomaly(data: dict, message: str = ''):
    conn = get_conn()
    conn.execute("""
        INSERT INTO anomaly_events (ts, node, appliance, P_W, I_rms, message)
        VALUES (:ts, :node, :appliance, :P_W, :I_rms, :message)
    """, {
        'ts':        int(time.time() * 1000),
        'node':      data.get('node',      'node_01'),
        'appliance': data.get('appliance', 'appliance_01'),
        'P_W':       data.get('P_W',       0.0),
        'I_rms':     data.get('I_rms',     0.0),
        'message':   message,
    })
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Query helpers (used by train.py)
# ---------------------------------------------------------------------------

def query_hourly(node='node_01', appliance='appliance_01', limit=720):
    """
    Return hourly averaged P_W and last E_Wh grouped by hour.
    Used by train.py to build the feature matrix.

    Returns a list of dicts: [{timestamp_h, P_W, E_Wh}, ...]
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            (ts / 3600000) * 3600000  AS timestamp_h,
            AVG(P_W)                  AS P_W,
            MAX(E_Wh)                 AS E_Wh
        FROM telemetry
        WHERE node = ? AND appliance = ?
        GROUP BY timestamp_h
        ORDER BY timestamp_h ASC
        LIMIT ?
    """, (node, appliance, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_recent_telemetry(node='node_01', appliance='appliance_01', limit=60):
    """Return the last N telemetry rows as a list of dicts."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM telemetry
        WHERE node = ? AND appliance = ?
        ORDER BY ts DESC
        LIMIT ?
    """, (node, appliance, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_anomalies(limit=20):
    """Return the most recent anomaly events."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM anomaly_events
        ORDER BY ts DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# DB write server — called by Node-RED via HTTP
# ---------------------------------------------------------------------------

def run_write_server(host='127.0.0.1', port=5051):
    """
    Lightweight Flask server that Node-RED POSTs data to.
    Replaces the node-red-contrib-influxdb palette entirely —
    no extra Node-RED package needed.

    Endpoints:
        POST /write/telemetry   → insert_telemetry(payload)
        POST /write/aggregate   → insert_aggregate(payload)
        POST /write/anomaly     → insert_anomaly(payload)
        GET  /read/recent       → last 60 telemetry rows (for dashboard history)
        GET  /read/anomalies    → last 20 anomaly events
    """
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("[DB] ERROR: Flask not installed. Run: pip install flask")
        return

    app = Flask(__name__)

    @app.route('/write/telemetry', methods=['POST'])
    def write_telemetry():
        data = request.get_json(silent=True) or {}
        insert_telemetry(data)
        return jsonify({'status': 'ok'})

    @app.route('/write/aggregate', methods=['POST'])
    def write_aggregate():
        data = request.get_json(silent=True) or {}
        insert_aggregate(data)
        return jsonify({'status': 'ok'})

    @app.route('/write/anomaly', methods=['POST'])
    def write_anomaly():
        data    = request.get_json(silent=True) or {}
        message = data.get('message', '')
        insert_anomaly(data, message)
        return jsonify({'status': 'ok'})

    @app.route('/read/recent', methods=['GET'])
    def read_recent():
        node      = request.args.get('node',      'node_01')
        appliance = request.args.get('appliance', 'appliance_01')
        limit     = int(request.args.get('limit', 60))
        return jsonify(query_recent_telemetry(node, appliance, limit))

    @app.route('/read/anomalies', methods=['GET'])
    def read_anomalies():
        return jsonify(query_anomalies())

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({'status': 'ok', 'db': DB_PATH})

    print(f"[DB] Write server on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    init_db()

    if '--server' in sys.argv:
        run_write_server()
    else:
        # Print schema summary
        conn = get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        print(f"[DB] Tables: {[t[0] for t in tables]}")
        conn.close()