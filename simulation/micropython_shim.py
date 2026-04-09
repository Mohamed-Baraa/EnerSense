# =============================================================================
# EnerSense — simulation/micropython_shim.py
# Mocks MicroPython-specific modules so the ESP32 firmware runs unmodified
# on a standard Python 3 installation on Windows/Linux/macOS.
#
# Mocked modules:
#   machine       → ADC returns simulated AC waveform samples
#                   Pin prints GPIO state changes
#   network       → WLAN immediately reports connected
#   utime         → wraps standard time module
#   umqtt.simple  → wraps paho-mqtt (pip install paho-mqtt)
#
# Usage:
#   python simulation/run_simulation.py
#   (do NOT import this file directly — use run_simulation.py)
# =============================================================================

import sys
import os
import time
import math
import random
import threading

# ---------------------------------------------------------------------------
# Inject shim into sys.modules BEFORE firmware modules are imported
# ---------------------------------------------------------------------------

# -- utime -------------------------------------------------------------------

class _utime:
    @staticmethod
    def ticks_ms():
        return int(time.time() * 1000)

    @staticmethod
    def ticks_diff(a, b):
        return a - b

    @staticmethod
    def ticks_us():
        return int(time.time() * 1_000_000)

    @staticmethod
    def time():
        return int(time.time())

    @staticmethod
    def sleep(s):
        time.sleep(s)

    @staticmethod
    def sleep_ms(ms):
        time.sleep(ms / 1000.0)

    @staticmethod
    def sleep_us(us):
        time.sleep(us / 1_000_000.0)

sys.modules['utime'] = _utime()


# -- ujson -------------------------------------------------------------------

import json as _json

class _ujson:
    dumps = staticmethod(_json.dumps)
    loads = staticmethod(_json.loads)

sys.modules['ujson'] = _ujson()


# -- machine -----------------------------------------------------------------

# Simulated load profile — edit to test different scenarios
# Each entry: (label, current_A, voltage_V)
SIMULATED_LOADS = [
    ("Idle / standby",   0.35,  229.0),
    ("Laptop + monitor", 1.20,  228.5),
    ("Electric kettle",  9.50,  230.0),
    ("Washing machine",  7.80,  227.0),
]

# Cycle through loads every N seconds
LOAD_CYCLE_SECONDS = 20

_load_index    = 0
_load_start_ts = time.time()
_load_lock     = threading.Lock()

# ---------------------------------------------------------------------------
# _sim_state — shared control dict used by run.py to set load interactively.
# When load_w > 0 it overrides the cycling profiles.
# ---------------------------------------------------------------------------
_sim_state = {
    'load_w':      0.0,    # 0 = use cycling profiles; >0 = fixed wattage
    'voltage_rms': 230.0,  # mains voltage (V)
    'noise':       20.0,   # ADC noise amplitude in counts
}


def _get_current_load():
    global _load_index, _load_start_ts
    # If run.py has set a fixed wattage, derive I from P/V
    if _sim_state['load_w'] > 0:
        V = _sim_state['voltage_rms']
        W = _sim_state['load_w']
        I = W / V if V > 0 else 0.0
        return ('manual', I, V)
    # Otherwise cycle through preset profiles
    with _load_lock:
        elapsed = time.time() - _load_start_ts
        if elapsed >= LOAD_CYCLE_SECONDS:
            _load_index = (_load_index + 1) % len(SIMULATED_LOADS)
            _load_start_ts = time.time()
            label, I, V = SIMULATED_LOADS[_load_index]
            print(f"\n[Shim] Load changed -> {label}  ({I} A, {V} V)\n")
        return SIMULATED_LOADS[_load_index]


def _simulate_adc_sample(pin_num, offset=2048):
    """
    Return a single ADC sample simulating the ACS712 (pin 34)
    or ZMPT101B (pin 35) output for the current load profile.
    """
    _, I_rms, V_rms = _get_current_load()
    t = time.time()

    if pin_num == 34:   # current sensor
        sensitivity  = 0.100
        adc_vref     = 3.3
        adc_max      = 4095
        counts_per_V = adc_max / adc_vref
        I_peak       = I_rms * math.sqrt(2)
        amplitude    = I_peak * sensitivity * counts_per_V
        noise        = random.gauss(0, _sim_state['noise'])
        sample       = offset + amplitude * math.sin(2 * math.pi * 50 * t) + noise

    elif pin_num == 35:  # voltage sensor
        zmpt_scale   = 234.5
        adc_vref     = 3.3
        adc_max      = 4095
        counts_per_V = adc_max / adc_vref
        V_peak       = V_rms * math.sqrt(2)
        v_pin_peak   = V_peak / zmpt_scale
        amplitude    = v_pin_peak * counts_per_V
        noise        = random.gauss(0, _sim_state['noise'] * 0.5)
        sample       = offset + amplitude * math.sin(2 * math.pi * 50 * t) + noise

    else:
        sample = offset

    return max(0, min(4095, int(sample)))


class _ADC:
    ATTN_11DB   = 3
    WIDTH_12BIT = 3

    def __init__(self, pin):
        self._pin = pin.id if hasattr(pin, 'id') else int(str(pin))

    def atten(self, _): pass
    def width(self, _): pass

    def read(self):
        return _simulate_adc_sample(self._pin)


class _Pin:
    OUT = 1
    IN  = 0

    def __init__(self, num, mode=None):
        self.id   = num
        self._val = 0

    def value(self, v=None):
        if v is None:
            return self._val
        if v != self._val:
            label = {2: "Status LED", 26: "Relay"}.get(self.id, f"GPIO{self.id}")
            print(f"[Shim] {label} -> {'ON' if v else 'OFF'}")
        self._val = v

    def __str__(self):
        return str(self.id)


class _machine:
    ADC = _ADC
    Pin = _Pin

    @staticmethod
    def reset():
        print("[Shim] machine.reset() called -- exiting.")
        sys.exit(0)

sys.modules['machine'] = _machine()


# -- network -----------------------------------------------------------------
_wlan_connected = False

class _WLAN:
    STA_IF = 1

    def __init__(self, _):
        pass

    def active(self, v=None):
        return True

    def isconnected(self):
        global _wlan_connected
        return _wlan_connected

    def connect(self, ssid, password):
        global _wlan_connected
        print(f"[Shim] Wi-Fi connecting to '{ssid}'...")
        time.sleep(0.3)
 
        _wlan_connected = True
        print("[Shim] Wi-Fi connected (simulated IP: 127.0.0.1)")

    def ifconfig(self):
        return ('127.0.0.1', '255.255.255.0', '192.168.1.1', '8.8.8.8')


class _network:
    WLAN   = _WLAN
    STA_IF = 1

sys.modules['network'] = _network()


# -- umqtt.simple ------------------------------------------------------------

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:
    print("=" * 60)
    print("ERROR: paho-mqtt not installed.")
    print("Run:   pip install paho-mqtt")
    print("=" * 60)
    sys.exit(1)


class _MQTTClient:
    """
    Drop-in replacement for umqtt.simple.MQTTClient using paho-mqtt.
    Matches the umqtt API so firmware code runs unchanged.
    """

    def __init__(self, client_id, server, port=1883,
                 user=None, password=None, keepalive=60, ssl=False):
        self._server    = server
        self._port      = port
        self._callback  = None
        self._connected = False

        # Try both new and old paho-mqtt callback API versions
        try:
            self._paho = paho_mqtt.Client(
                client_id=client_id,
                protocol=paho_mqtt.MQTTv311,
                callback_api_version=paho_mqtt.CallbackAPIVersion.VERSION1
            )
        except AttributeError:
            # Older paho-mqtt versions don't have CallbackAPIVersion
            self._paho = paho_mqtt.Client(
                client_id=client_id,
                protocol=paho_mqtt.MQTTv311
            )

        if user:
            self._paho.username_pw_set(user, password)

        self._paho.on_message    = self._on_message
        self._paho.on_connect    = self._on_connect
        self._paho.on_disconnect = self._on_disconnect

    def set_callback(self, cb):
        self._callback = cb

    def connect(self):
        self._paho.connect(self._server, self._port)
        self._paho.loop_start()
        deadline = time.time() + 5
        while not self._connected and time.time() < deadline:
            time.sleep(0.05)
        if not self._connected:
            raise OSError(
                f"Could not connect to MQTT broker at {self._server}:{self._port}\n"
                "Make sure Mosquitto is running (Terminal 1)."
            )

    def disconnect(self):
        self._paho.loop_stop()
        self._paho.disconnect()
        self._connected = False

    def subscribe(self, topic, qos=0):
        if isinstance(topic, bytes):
            topic = topic.decode()
        self._paho.subscribe(topic, qos=qos)

    def publish(self, topic, msg, retain=False, qos=0):
        if isinstance(topic, bytes):
            topic = topic.decode()
        if isinstance(msg, bytes):
            msg = msg.decode()
        self._paho.publish(topic, msg, qos=qos, retain=retain)

    def check_msg(self):
        pass   # paho background thread handles this

    def ping(self):
        pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
        else:
            codes = {1: "wrong protocol", 2: "invalid ID",
                     3: "broker unavailable", 4: "bad credentials", 5: "not authorised"}
            print(f"[Shim] MQTT connect refused: {codes.get(rc, rc)}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            print(f"[Shim] MQTT unexpected disconnect (rc={rc})")

    def _on_message(self, client, userdata, msg):
        if self._callback:
            self._callback(msg.topic.encode(), msg.payload)


class _umqtt_simple:
    MQTTClient = _MQTTClient

class _umqtt:
    simple = _umqtt_simple()

sys.modules['umqtt']        = _umqtt()
sys.modules['umqtt.simple'] = _umqtt_simple()


# ---------------------------------------------------------------------------
print("[Shim] MicroPython shim loaded:")
print("       machine / network / utime / ujson / umqtt.simple -> desktop")
print(f"       {len(SIMULATED_LOADS)} load profiles, cycling every {LOAD_CYCLE_SECONDS}s\n")
