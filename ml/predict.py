# =============================================================================
# EnerSense — ml/predict.py
# Flask inference server — exposes the trained model and anomaly detector
# as HTTP endpoints consumed by Node-RED.
#
# Endpoints:
#   POST /predict    → next-hour kWh prediction
#   POST /anomaly    → Z-score anomaly check + baseline update
#   GET  /health     → server status + model info
#   GET  /baseline/<appliance_id>  → 24-slot baseline summary
#
# Usage:
#   python ml/predict.py              # default: localhost:5050
#   python ml/predict.py --port 5050
#
# Node-RED integration:
#   Use an http request node to POST to http://localhost:5050/predict
#   with a JSON body built from the last 3 hourly aggregates.
# =============================================================================

import argparse
import json
import math
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Lazy imports — fail gracefully with clear messages
# ---------------------------------------------------------------------------

try:
    from flask import Flask, request, jsonify
except ImportError:
    print("ERROR: Flask not installed. Run: pip install flask")
    sys.exit(1)

try:
    import joblib
    import numpy as np
except ImportError:
    print("ERROR: joblib/numpy not installed. Run: pip install scikit-learn numpy")
    sys.exit(1)

from anomaly import AnomalyDetector

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH   = os.path.join(SCRIPT_DIR, 'model.joblib')
METRICS_PATH = os.path.join(SCRIPT_DIR, 'metrics.json')

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Load model at startup (fail loudly if not trained yet)
pipeline = None
metrics  = {}

def load_model():
    global pipeline, metrics

    if not os.path.exists(MODEL_PATH):
        print(f"[Predict] WARNING: model not found at {MODEL_PATH}")
        print("[Predict] Run 'python ml/train.py --synthetic' to train a model first.")
        return False

    pipeline = joblib.load(MODEL_PATH)
    print(f"[Predict] Model loaded from {MODEL_PATH}")

    if os.path.exists(METRICS_PATH):
        with open(METRICS_PATH) as f:
            metrics = json.load(f)
        print(f"[Predict] Metrics — MAE: {metrics.get('mae')} kWh  "
              f"RMSE: {metrics.get('rmse')} kWh  R²: {metrics.get('r2')}")

    return True


# Global anomaly detector (shared across requests, stateful)
detector = AnomalyDetector()


# ---------------------------------------------------------------------------
# Feature builder (mirrors train.py logic)
# ---------------------------------------------------------------------------

def build_features(hour: int, dow: int,
                   lag_1h: float, lag_2h: float, lag_3h: float) -> np.ndarray:
    """
    Build the feature vector for a single prediction request.
    Must match the feature order used in train.py exactly.
    """
    hour_sin = math.sin(2 * math.pi * hour / 24)
    hour_cos = math.cos(2 * math.pi * hour / 24)
    day_sin  = math.sin(2 * math.pi * dow  / 7)
    day_cos  = math.cos(2 * math.pi * dow  / 7)

    return np.array([[hour_sin, hour_cos, day_sin, day_cos,
                      lag_1h, lag_2h, lag_3h]])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    """
    Returns server status and model availability.

    Example response:
    {
        "status": "ok",
        "model_loaded": true,
        "metrics": { "mae": 0.031, "rmse": 0.045, "r2": 0.87 },
        "baseline_slots": 42
    }
    """
    return jsonify({
        'status':        'ok',
        'model_loaded':  pipeline is not None,
        'metrics':       metrics,
        'baseline_slots': len(detector._baselines),
        'timestamp':     datetime.now().isoformat(),
    })


@app.route('/predict', methods=['POST'])
def predict():
    """
    Predict next-hour energy consumption.

    Request body (JSON):
    {
        "lag_1h": 0.95,    // kWh consumed in the previous hour
        "lag_2h": 0.88,    // kWh consumed 2 hours ago
        "lag_3h": 0.76,    // kWh consumed 3 hours ago
        "hour": 14,        // current hour (0-23), optional (auto from server time)
        "dow": 2           // day of week (0=Mon), optional (auto from server time)
    }

    Response (JSON):
    {
        "predicted_kwh": 0.93,
        "predicted_w":   930.0,
        "hour": 14,
        "features": { "lag_1h": 0.95, ... },
        "model_mae_kwh": 0.031
    }
    """
    if pipeline is None:
        return jsonify({'error': 'Model not loaded. Run train.py first.'}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid or missing JSON body'}), 400

    # Extract required lag features
    try:
        lag_1h = float(data['lag_1h'])
        lag_2h = float(data['lag_2h'])
        lag_3h = float(data['lag_3h'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'Missing or invalid lag feature: {e}'}), 400

    # Time features — use provided values or derive from server clock
    now  = datetime.now()
    hour = int(data.get('hour', now.hour))
    dow  = int(data.get('dow',  now.weekday()))

    # Validate ranges
    if not (0 <= hour <= 23):
        return jsonify({'error': f'hour must be 0–23, got {hour}'}), 400
    if not (0 <= dow <= 6):
        return jsonify({'error': f'dow must be 0–6, got {dow}'}), 400

    # Build feature vector and predict
    X = build_features(hour, dow, lag_1h, lag_2h, lag_3h)
    predicted_kwh = float(pipeline.predict(X)[0])
    predicted_kwh = max(predicted_kwh, 0.0)   # clamp to non-negative

    return jsonify({
        'predicted_kwh' : round(predicted_kwh, 4),
        'predicted_w'   : round(predicted_kwh * 1000, 2),
        'hour'          : hour,
        'dow'           : dow,
        'features'      : {
            'lag_1h': lag_1h,
            'lag_2h': lag_2h,
            'lag_3h': lag_3h,
        },
        'model_mae_kwh' : metrics.get('mae', None),
    })


@app.route('/anomaly', methods=['POST'])
def anomaly_check():
    """
    Check a power reading against the rolling Z-score baseline.
    Also updates the baseline with the new reading.

    Request body (JSON):
    {
        "appliance_id": "appliance_01",
        "P_W": 966.4,
        "hour": 14       // optional, auto from server time
    }

    Response (JSON):
    {
        "anomaly": false,
        "reason": "Normal",
        "zscore": 0.23,
        "mean_w": 950.0,
        "std_w": 45.2,
        "n_samples": 87
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid or missing JSON body'}), 400

    try:
        appliance_id = str(data.get('appliance_id', 'appliance_01'))
        P_W          = float(data['P_W'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'Missing or invalid field: {e}'}), 400

    now  = datetime.now()
    hour = int(data.get('hour', now.hour))

    result = detector.check(appliance_id=appliance_id, hour=hour, P_W=P_W)

    # Persist baseline periodically (every 100 requests via a simple counter)
    app._anomaly_counter = getattr(app, '_anomaly_counter', 0) + 1
    if app._anomaly_counter % 100 == 0:
        detector.save_baseline()

    return jsonify(result)


@app.route('/baseline/<appliance_id>', methods=['GET'])
def baseline_summary(appliance_id):
    """
    Return the 24-slot hourly baseline summary for a given appliance.
    Useful for dashboard display and debugging.

    Response: { "00:00": {"mean_w": 80.0, "std_w": 5.2, "n": 42}, ... }
    """
    summary = detector.slot_summary(appliance_id)
    return jsonify(summary)


@app.route('/baseline/save', methods=['POST'])
def save_baseline():
    """Force-save the anomaly detector baseline to disk."""
    detector.save_baseline()
    return jsonify({'status': 'saved', 'path': str(detector.__class__.__name__)})


# ---------------------------------------------------------------------------
# Forecast helper — 6-hour rolling prediction
# ---------------------------------------------------------------------------

@app.route('/forecast', methods=['POST'])
def forecast():
    """
    Generate a 6-hour rolling forecast starting from now.
    Each step uses the previous step's prediction as the next lag value.

    Request body (JSON):
    {
        "lag_1h": 0.95,
        "lag_2h": 0.88,
        "lag_3h": 0.76
    }

    Response:
    {
        "forecast": [
            {"hour": 15, "predicted_kwh": 0.93, "predicted_w": 930},
            ...
        ],
        "total_kwh": 5.42
    }
    """
    if pipeline is None:
        return jsonify({'error': 'Model not loaded.'}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON body'}), 400

    try:
        lag_1 = float(data['lag_1h'])
        lag_2 = float(data['lag_2h'])
        lag_3 = float(data['lag_3h'])
    except (KeyError, ValueError) as e:
        return jsonify({'error': f'Missing lag feature: {e}'}), 400

    now      = datetime.now()
    forecast = []
    total    = 0.0

    for step in range(6):
        hour = (now.hour + step + 1) % 24
        dow  = (now.weekday() + (now.hour + step + 1) // 24) % 7

        X   = build_features(hour, dow, lag_1, lag_2, lag_3)
        kwh = max(float(pipeline.predict(X)[0]), 0.0)

        forecast.append({
            'hour'          : hour,
            'predicted_kwh' : round(kwh, 4),
            'predicted_w'   : round(kwh * 1000, 2),
        })
        total += kwh

        # Shift lags: most recent prediction becomes lag_1 for next step
        lag_3, lag_2, lag_1 = lag_2, lag_1, kwh

    return jsonify({
        'forecast'   : forecast,
        'total_kwh'  : round(total, 4),
        'start_hour' : (now.hour + 1) % 24,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='EnerSense inference server')
    parser.add_argument('--host', type=str, default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5050)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    load_model()

    print(f"\n[Predict] EnerSense inference server starting on "
          f"http://{args.host}:{args.port}")
    print("[Predict] Endpoints:")
    print("  GET  /health")
    print("  POST /predict")
    print("  POST /anomaly")
    print("  POST /forecast")
    print(f"  GET  /baseline/<appliance_id>\n")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()