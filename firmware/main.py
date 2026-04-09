# =============================================================================
# EnerSense — main.py
# Entry point. Boots the system, runs the main acquisition + publish loop.
#
# Execution flow:
#   1. Boot: Wi-Fi connect, MQTT connect, sensor calibration
#   2. Loop (every TELEMETRY_INTERVAL_S):
#       a. Read current sensor
#       b. Read voltage sensor
#       c. Combine into telemetry dict
#       d. Check static anomaly threshold → force relay OFF if triggered
#       e. Publish telemetry via MQTT (or buffer if offline)
#       f. Check for incoming relay commands
#       g. Execute pending relay command (with timing lock)
#       h. Every AGGREGATE_INTERVAL_S: compute and publish hourly aggregate
#   3. On any unhandled exception: log, wait, reboot via machine.reset()
# =============================================================================

import utime
import ujson
import machine
from machine import Pin
import config
from sensor      import CurrentSensor, VoltageSensor
from mqtt_client import EnerSenseMQTT, wifi_connect
from relay       import Relay

# GPIO2 = onboard LED on ESP32 DevKit V1 (also drives the green status LED
# in the Wokwi diagram).  Solid ON = MQTT connected.  Fast blink = connecting.
PIN_STATUS_LED = 2


# ---------------------------------------------------------------------------
# Status LED helpers
# ---------------------------------------------------------------------------

_status_led = Pin(PIN_STATUS_LED, Pin.OUT)

def led_on():
    _status_led.value(1)

def led_off():
    _status_led.value(0)

def led_blink(times=3, period_ms=150):
    """Blink the status LED `times` times (blocking, used during boot)."""
    for _ in range(times):
        _status_led.value(1)
        utime.sleep_ms(period_ms)
        _status_led.value(0)
        utime.sleep_ms(period_ms)


# ---------------------------------------------------------------------------
# Aggregate accumulator
# ---------------------------------------------------------------------------

class HourlyAccumulator:
    """
    Accumulates telemetry readings over one hour and produces a summary dict
    for the aggregate MQTT topic.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._readings  = []
        self._start_ms  = utime.ticks_ms()

    def add(self, telemetry: dict):
        self._readings.append(telemetry['P_W'])

    def is_due(self):
        elapsed_s = utime.ticks_diff(utime.ticks_ms(), self._start_ms) / 1000.0
        return elapsed_s >= config.AGGREGATE_INTERVAL_S

    def build(self, energy_wh: float) -> dict:
        if not self._readings:
            return {}

        avg_w = sum(self._readings) / len(self._readings)
        return {
            'window_start_ms' : self._start_ms,
            'window_end_ms'   : utime.ticks_ms(),
            'total_kwh'       : round(energy_wh / 1000.0, 4),
            'avg_w'           : round(avg_w, 2),
            'max_w'           : round(max(self._readings), 2),
            'min_w'           : round(min(self._readings), 2),
            'samples'         : len(self._readings),
            'cost_tnd'        : round((energy_wh / 1000.0) * config.TARIFF_TND_PER_KWH, 4),
            'node'            : config.NODE_ID,
            'appliance'       : config.APPLIANCE_ID,
        }


# ---------------------------------------------------------------------------
# Anomaly checker
# ---------------------------------------------------------------------------

def check_anomaly(telemetry: dict, relay: Relay) -> bool:
    """
    Static threshold anomaly check (Tier 1).
    Returns True if an anomaly was detected.
    If power exceeds ANOMALY_STATIC_THRESHOLD_W, the relay is force-cut.
    """
    if telemetry['P_W'] > config.ANOMALY_STATIC_THRESHOLD_W:
        print(f"[Anomaly] ALERT — power {telemetry['P_W']} W exceeds "
              f"threshold {config.ANOMALY_STATIC_THRESHOLD_W} W")
        relay.force_off()
        return True
    return False


# ---------------------------------------------------------------------------
# Boot sequence
# ---------------------------------------------------------------------------

def boot():
    """
    Initialise all subsystems. Returns (sensor_current, sensor_voltage, mqtt, relay).
    Blocks until Wi-Fi is up. MQTT failure at boot is non-fatal — the buffer
    will absorb readings until the broker becomes reachable.
    """
    print("\n" + "=" * 50)
    print("  EnerSense — Smart Energy Monitor")
    print(f"  Node: {config.NODE_ID}  |  Appliance: {config.APPLIANCE_ID}")
    print("=" * 50 + "\n")

    led_off()

    # Wi-Fi (block until connected; retry indefinitely — blink while waiting)
    while not wifi_connect(timeout_s=30):
        print("[Boot] Wi-Fi failed, retrying in 10s...")
        led_blink(times=6, period_ms=100)   # rapid blink = no Wi-Fi
        utime.sleep(4)

    led_blink(times=3, period_ms=200)       # 3 slow blinks = Wi-Fi OK

    # Hardware
    cs    = CurrentSensor()
    vs    = VoltageSensor()
    relay = Relay()

    # Sensor calibration (no-load required)
    print("[Boot] Calibrating sensors...")
    cs.auto_calibrate()
    vs.auto_calibrate()
    print("[Boot] Calibration done.")

    # MQTT (non-blocking failure allowed)
    mqtt = EnerSenseMQTT()
    if mqtt.connect():
        led_on()                            # solid green = MQTT connected
    else:
        led_off()                           # LED off = broker unreachable

    return cs, vs, mqtt, relay


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    cs, vs, mqtt, relay = boot()

    accumulator   = HourlyAccumulator()
    last_telem_ms = utime.ticks_ms()

    print("\n[Main] Entering main loop...\n")

    while True:
        now_ms = utime.ticks_ms()

        # ── Telemetry interval ────────────────────────────────────────
        elapsed_s = utime.ticks_diff(now_ms, last_telem_ms) / 1000.0
        if elapsed_s >= config.TELEMETRY_INTERVAL_S:
            last_telem_ms = now_ms

            # 1. Read sensors
            cr  = cs.read()
            V   = vs.read()

            # 2. Build telemetry dict
            telemetry = cs.combine(cr, V)
            telemetry['node']      = config.NODE_ID
            telemetry['appliance'] = config.APPLIANCE_ID
            telemetry['relay']     = relay.state_str

            # 3. Anomaly check (Tier 1 — static threshold)
            anomaly = check_anomaly(telemetry, relay)
            telemetry['anomaly'] = anomaly

            # 4. Print to serial (useful for Wokwi serial monitor)
            print(
                f"I={telemetry['I_rms']:.3f}A  "
                f"V={telemetry['V_rms']:.1f}V  "
                f"P={telemetry['P_W']:.1f}W  "
                f"E={telemetry['E_Wh']:.4f}Wh  "
                f"Relay={telemetry['relay']}  "
                f"{'*** ANOMALY ***' if anomaly else ''}"
            )

            # 5. Ensure broker connection then publish
            was_connected = mqtt.is_connected
            mqtt.ensure_connected()

            # Update status LED if connection state changed
            if not was_connected and mqtt.is_connected:
                led_on()
            elif was_connected and not mqtt.is_connected:
                led_off()

            mqtt.publish_telemetry(telemetry)

            # 6. Accumulate for hourly aggregate
            accumulator.add(telemetry)

        # ── Aggregate interval ────────────────────────────────────────
        if accumulator.is_due():
            agg = accumulator.build(cs.energy_wh)
            if agg:
                mqtt.publish_aggregate(agg)
            accumulator.reset()

        # ── Relay command check ───────────────────────────────────────
        mqtt.check_messages()

        if mqtt.relay_command is not None:
            cmd = mqtt.relay_command
            mqtt.relay_command = None   # consume the command

            if cmd == "ON":
                relay.turn_on()
            elif cmd == "OFF":
                relay.turn_off()

            # Publish updated relay state back to broker
            mqtt.publish_relay_status(relay.is_on)

        # ── Yield (keep loop near TELEMETRY_INTERVAL_S cadence) ──────
        utime.sleep_ms(50)


# ---------------------------------------------------------------------------
# Entry point with crash guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n[Main] Stopped by user.")
    except Exception as e:
        import sys
        print(f"\n[Main] UNHANDLED EXCEPTION: {e}")
        sys.print_exception(e)
        print("[Main] Rebooting in 10s...")
        led_blink(times=10, period_ms=80)   # fast blink = crash
        utime.sleep(5)
        machine.reset()