# =============================================================================
# EnerSense — ml/anomaly.py
# Tier-2 statistical anomaly detector using a rolling Z-score baseline.
#
# How it works:
#   For each (appliance, hour-of-day) slot, a rolling window of the last
#   WINDOW_HOURS hours of observations is maintained. When a new reading
#   arrives, its Z-score against the slot's mean and std is computed.
#   If the Z-score exceeds ZSCORE_THRESHOLD (default 3.0), the reading
#   is flagged as anomalous.
#
#   This approach:
#     - Requires no training phase (starts working from the first reading)
#     - Adapts to the user's actual usage patterns over time
#     - Is fully explainable (Z-score is a familiar statistical concept)
#     - Handles multi-appliance scenarios cleanly via per-slot baselines
#
# Usage (standalone):
#   from anomaly import AnomalyDetector
#   detector = AnomalyDetector()
#   result   = detector.check(appliance_id='appliance_01', hour=14, P_W=2800.0)
#   if result['anomaly']:
#       print(result['reason'])
#
# Usage (with InfluxDB for persistent baseline):
#   detector = AnomalyDetector(use_influx=True)
# =============================================================================

import math
import json
import os
from collections import defaultdict, deque

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ZSCORE_THRESHOLD = 3.0     # flag if |Z| exceeds this
WINDOW_HOURS     = 168     # 7-day rolling baseline per slot
MIN_SAMPLES      = 5       # minimum observations before Z-score is applied
                            # (below this, only static threshold fires)
STATIC_THRESHOLD_W = 2500  # hard ceiling regardless of baseline

BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'baseline.json')


# ---------------------------------------------------------------------------
# Rolling statistics helper
# ---------------------------------------------------------------------------

class RollingStats:
    """
    Maintains a fixed-size deque of float observations and exposes
    mean and standard deviation on demand.
    """

    def __init__(self, maxlen):
        self._data = deque(maxlen=maxlen)

    def push(self, value: float):
        self._data.append(float(value))

    @property
    def n(self):
        return len(self._data)

    @property
    def mean(self):
        if not self._data:
            return 0.0
        return sum(self._data) / len(self._data)

    @property
    def std(self):
        if len(self._data) < 2:
            return 0.0
        m = self.mean
        variance = sum((x - m) ** 2 for x in self._data) / len(self._data)
        return math.sqrt(variance)

    def zscore(self, value: float) -> float:
        s = self.std
        if s < 1e-6:
            return 0.0
        return (value - self.mean) / s

    def to_list(self):
        return list(self._data)

    def from_list(self, data, maxlen):
        self._data = deque(data[-maxlen:], maxlen=maxlen)


# ---------------------------------------------------------------------------
# Anomaly detector
# ---------------------------------------------------------------------------

class AnomalyDetector:
    """
    Per-slot (appliance × hour-of-day) rolling Z-score anomaly detector.

    Slots are keyed as "{appliance_id}:{hour_of_day}" (e.g. "appliance_01:14").
    Each slot maintains its own RollingStats instance.
    """

    def __init__(self,
                 zscore_threshold=ZSCORE_THRESHOLD,
                 window_hours=WINDOW_HOURS,
                 min_samples=MIN_SAMPLES,
                 static_threshold_w=STATIC_THRESHOLD_W):

        self.zscore_threshold    = zscore_threshold
        self.window_hours        = window_hours
        self.min_samples         = min_samples
        self.static_threshold_w  = static_threshold_w

        # slot_key → RollingStats
        self._baselines = defaultdict(lambda: RollingStats(window_hours))

        # Load persisted baseline if it exists
        self.load_baseline()

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def check(self, appliance_id: str, hour: int, P_W: float) -> dict:
        """
        Evaluate a single power reading against the rolling baseline.

        Args:
            appliance_id : identifier string (matches config.APPLIANCE_ID)
            hour         : hour of day (0–23)
            P_W          : instantaneous power reading in Watts

        Returns:
            {
                'anomaly'   : bool,
                'reason'    : str,       # human-readable explanation
                'zscore'    : float,     # Z-score of this reading (0 if < min_samples)
                'mean_w'    : float,     # baseline mean for this slot
                'std_w'     : float,     # baseline std for this slot
                'n_samples' : int,       # number of observations in this slot
            }
        """
        key   = f"{appliance_id}:{hour}"
        stats = self._baselines[key]

        # --- Tier 1: static hard threshold (always active) ---
        if P_W > self.static_threshold_w:
            self._update_baseline(key, P_W)
            return self._result(
                anomaly=True,
                reason=f"Static threshold exceeded: {P_W:.1f} W > {self.static_threshold_w} W",
                zscore=None,
                stats=stats
            )

        # --- Tier 2: Z-score (only once enough data is collected) ---
        if stats.n >= self.min_samples:
            z = stats.zscore(P_W)
            self._update_baseline(key, P_W)

            if abs(z) > self.zscore_threshold:
                direction = "above" if z > 0 else "below"
                return self._result(
                    anomaly=True,
                    reason=(f"Z-score anomaly ({direction} baseline): "
                            f"Z={z:.2f}, P={P_W:.1f} W, "
                            f"baseline mean={stats.mean:.1f} W ± {stats.std:.1f} W"),
                    zscore=z,
                    stats=stats
                )

            return self._result(anomaly=False, reason='Normal', zscore=z, stats=stats)

        # --- Not enough data yet — update baseline, no anomaly ---
        self._update_baseline(key, P_W)
        return self._result(
            anomaly=False,
            reason=f"Baseline building ({stats.n}/{self.min_samples} samples)",
            zscore=0.0,
            stats=stats
        )

    def update(self, appliance_id: str, hour: int, P_W: float):
        """
        Update the baseline without performing an anomaly check.
        Use during a dedicated data collection phase.
        """
        key = f"{appliance_id}:{hour}"
        self._update_baseline(key, P_W)

    def slot_summary(self, appliance_id: str) -> dict:
        """
        Return a summary of all 24 hourly slots for a given appliance.
        Useful for debugging and dashboard display.
        """
        summary = {}
        for h in range(24):
            key   = f"{appliance_id}:{h}"
            stats = self._baselines[key]
            summary[f"{h:02d}:00"] = {
                'mean_w':   round(stats.mean, 2),
                'std_w':    round(stats.std, 2),
                'n':        stats.n,
            }
        return summary

    # ------------------------------------------------------------------
    # Baseline persistence
    # ------------------------------------------------------------------

    def save_baseline(self):
        """
        Serialise all slot baselines to a JSON file.
        Call periodically (e.g. every hour) to survive restarts.
        """
        data = {}
        for key, stats in self._baselines.items():
            data[key] = {
                'observations': stats.to_list(),
                'window_hours': self.window_hours,
            }
        with open(BASELINE_PATH, 'w') as f:
            json.dump(data, f)

    def load_baseline(self):
        """Load persisted baseline from JSON if it exists."""
        if not os.path.exists(BASELINE_PATH):
            return
        try:
            with open(BASELINE_PATH) as f:
                data = json.load(f)
            for key, slot in data.items():
                stats = RollingStats(self.window_hours)
                stats.from_list(slot['observations'], self.window_hours)
                self._baselines[key] = stats
            print(f"[Anomaly] Loaded baseline: {len(data)} slots from {BASELINE_PATH}")
        except Exception as e:
            print(f"[Anomaly] Could not load baseline: {e} — starting fresh")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_baseline(self, key, P_W):
        self._baselines[key].push(P_W)

    @staticmethod
    def _result(anomaly, reason, zscore, stats):
        return {
            'anomaly'   : anomaly,
            'reason'    : reason,
            'zscore'    : round(zscore, 4) if zscore is not None else None,
            'mean_w'    : round(stats.mean, 2),
            'std_w'     : round(stats.std, 2),
            'n_samples' : stats.n,
        }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("EnerSense anomaly detector — standalone test\n")

    detector = AnomalyDetector(min_samples=3)

    # Feed normal readings for slot appliance_01, hour 14
    normal = [950, 980, 960, 970, 940, 955]
    print("Feeding normal baseline readings (hour=14):")
    for w in normal:
        r = detector.check('appliance_01', 14, float(w))
        print(f"  P={w}W  anomaly={r['anomaly']}  reason={r['reason']}")

    print()

    # Feed anomalous reading
    test_cases = [960, 2900, 200, 955]
    print("Test readings:")
    for w in test_cases:
        r = detector.check('appliance_01', 14, float(w))
        flag = '*** ANOMALY ***' if r['anomaly'] else ''
        z_str = f"{r['zscore']:>6.2f}" if r['zscore'] is not None else "  N/A "
        print(f"  P={w:>6.1f}W  Z={z_str}  {r['reason'][:60]}  {flag}")