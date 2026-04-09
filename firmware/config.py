# =============================================================================
# EnerSense — config.py
# Central configuration file. Edit this before deploying to a new node.
# =============================================================================

# --- Node identity -----------------------------------------------------------
NODE_ID       = "node_01"
APPLIANCE_ID  = "appliance_01"   # change per monitored appliance

# --- Wi-Fi -------------------------------------------------------------------
WIFI_SSID     = "YourSSID"
WIFI_PASSWORD = "YourPassword"

# --- MQTT broker -------------------------------------------------------------
#MQTT_BROKER   = "192.168.1.100"  # local broker IP (your laptop/RPi)
MQTT_BROKER   = "127.0.0.1"     # use for local hosting
MQTT_PORT     = 1884
MQTT_USER     = "enersense"
MQTT_PASSWORD = "enersense123"
MQTT_KEEPALIVE = 60

# MQTT topic templates  (formatted at runtime with NODE_ID / APPLIANCE_ID)
TOPIC_TELEMETRY = "enersense/{node}/{appliance}/telemetry"
TOPIC_AGGREGATE = "enersense/{node}/{appliance}/aggregate"
TOPIC_RELAY_CMD = "enersense/{node}/{appliance}/relay/cmd"
TOPIC_RELAY_STS = "enersense/{node}/{appliance}/relay/status"

def topic(template):
    """Return a fully resolved topic string."""
    return template.format(node=NODE_ID, appliance=APPLIANCE_ID)

# --- Hardware pins -----------------------------------------------------------
PIN_CURRENT   = 34   # ADC1_CH6  — ACS712 analog output
PIN_VOLTAGE   = 35   # ADC1_CH7  — ZMPT101B analog output (optional)
PIN_RELAY     = 26   # GPIO26    — relay control

# Set to False if no voltage sensor is connected.
# When False, nominal mains voltage is assumed for power calculation.
VOLTAGE_SENSOR_ENABLED = True

# --- ADC / sampling ----------------------------------------------------------
ADC_BITS         = 12           # ESP32 ADC resolution
ADC_MAX          = 4095         # 2^12 - 1
ADC_VREF         = 3.3          # ESP32 ADC reference voltage (V)
ADC_OFFSET       = 2048         # DC midpoint (0 A / 0 V maps here) in counts
                                 # Calibrate empirically if readings drift.

SAMPLE_FREQ_HZ   = 5000         # ADC sampling frequency
MAINS_FREQ_HZ    = 50           # 50 Hz mains (Tunisia / Europe)
CYCLES_PER_READ  = 5            # number of full mains cycles per RMS window
SAMPLES_PER_READ = (SAMPLE_FREQ_HZ // MAINS_FREQ_HZ) * CYCLES_PER_READ
                                 # = 500 samples per measurement window

# --- Sensor calibration ------------------------------------------------------
# ACS712-20A: sensitivity = 100 mV/A → 0.1 V/A
# With 3.3 V ADC and 12-bit resolution: 1 count = 3.3/4095 V ≈ 0.000806 V
# current_A = counts_rms * (ADC_VREF / ADC_MAX) / ACS712_SENSITIVITY
ACS712_SENSITIVITY = 0.100      # V/A  (use 0.185 for 5A variant, 0.066 for 30A)

# ZMPT101B: calibration factor (adjust by comparing to reference voltmeter)
ZMPT101B_SCALE     = 234.5      # empirical scale to convert counts_rms → V_rms

# Nominal mains voltage assumed when voltage sensor is disabled
NOMINAL_VOLTAGE    = 230.0      # V (Tunisian grid: 230 V / 50 Hz)

# --- Anomaly detection -------------------------------------------------------
ANOMALY_STATIC_THRESHOLD_W  = 2500.0   # flag if instantaneous power exceeds this
ANOMALY_ZSCORE_THRESHOLD    = 3.0      # flag if Z-score exceeds this
ANOMALY_WINDOW_HOURS        = 168      # 7-day rolling baseline (hours)

# --- Relay safety ------------------------------------------------------------
RELAY_MIN_ON_TIME_S  = 5    # minimum seconds relay stays ON before it can switch OFF
RELAY_MIN_OFF_TIME_S = 5    # minimum seconds relay stays OFF before it can switch ON

# --- Timing ------------------------------------------------------------------
TELEMETRY_INTERVAL_S  = 1    # publish telemetry every N seconds
AGGREGATE_INTERVAL_S  = 3600 # publish hourly aggregate every N seconds
RECONNECT_DELAY_S     = 5    # seconds between Wi-Fi / MQTT reconnect attempts
BUFFER_MAX_READINGS   = 100  # max offline readings to buffer in RAM

# --- Cost estimation ---------------------------------------------------------
TARIFF_TND_PER_KWH = 0.180   # Tunisian electricity tariff (TND/kWh, tranches 1-2)