# =============================================================================
# EnerSense — mqtt_client.py
# Manages Wi-Fi connection, MQTT broker connection, telemetry publishing,
# relay command subscription, and offline buffering on disconnection.
#
# Design notes:
#   - Uses umqtt.simple (built into MicroPython firmware for ESP32).
#   - On Wi-Fi or broker loss, readings are pushed into a circular buffer
#     (max BUFFER_MAX_READINGS entries). On reconnect the buffer is flushed
#     before new readings are published, preserving data continuity.
#   - Relay commands arrive as retained MQTT messages on the relay/cmd topic.
#     The callback sets a flag that main.py polls — no blocking inside the ISR.
# =============================================================================

import network
import utime
import ujson
from umqtt.simple import MQTTClient
import config


# ---------------------------------------------------------------------------
# Wi-Fi helpers
# ---------------------------------------------------------------------------

def wifi_connect(timeout_s=30):
    """
    Connect to Wi-Fi. Blocks until connected or timeout_s seconds elapse.

    Returns:
        True  if connected successfully.
        False if timed out.
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        return True

    print(f"[WiFi] Connecting to '{config.WIFI_SSID}'", end="")
    wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)

    deadline = utime.time() + timeout_s
    while not wlan.isconnected():
        if utime.time() > deadline:
            print(" TIMEOUT")
            return False
        print(".", end="")
        utime.sleep(1)

    print(f" OK  —  IP: {wlan.ifconfig()[0]}")
    return True


def wifi_is_up():
    """Return True if Wi-Fi interface is connected."""
    wlan = network.WLAN(network.STA_IF)
    return wlan.active() and wlan.isconnected()


# ---------------------------------------------------------------------------
# Circular offline buffer
# ---------------------------------------------------------------------------

class RingBuffer:
    """
    Fixed-size circular buffer for storing telemetry dicts while offline.
    Oldest entries are overwritten when the buffer is full.
    """

    def __init__(self, capacity):
        self._buf  = [None] * capacity
        self._head = 0   # next write position
        self._size = 0
        self._cap  = capacity

    def push(self, item):
        self._buf[self._head] = item
        self._head = (self._head + 1) % self._cap
        if self._size < self._cap:
            self._size += 1

    def flush(self):
        """Yield all buffered items in insertion order, then clear."""
        if self._size == 0:
            return
        start = (self._head - self._size) % self._cap
        for i in range(self._size):
            yield self._buf[(start + i) % self._cap]
        self._head = 0
        self._size = 0

    @property
    def size(self):
        return self._size

    def is_empty(self):
        return self._size == 0


# ---------------------------------------------------------------------------
# MQTT client wrapper
# ---------------------------------------------------------------------------

class EnerSenseMQTT:
    """
    Wraps umqtt.simple.MQTTClient with:
      - Auto-reconnect logic
      - Offline ring buffer
      - Relay command subscription + callback flag
      - JSON serialisation for telemetry and aggregate payloads

    Usage:
        mqtt = EnerSenseMQTT()
        mqtt.connect()                        # call once at startup
        mqtt.publish_telemetry(telemetry_dict)
        mqtt.check_messages()                 # call in main loop to receive relay cmds
        if mqtt.relay_command is not None:
            handle(mqtt.relay_command)
            mqtt.relay_command = None
    """

    def __init__(self):
        self._client       = None
        self._connected    = False
        self._buffer       = RingBuffer(config.BUFFER_MAX_READINGS)

        # Set by the MQTT subscription callback; polled by main.py
        self.relay_command = None   # 'ON' | 'OFF' | None

        # Resolved topic strings (computed once)
        self._t_telemetry  = config.topic(config.TOPIC_TELEMETRY)
        self._t_aggregate  = config.topic(config.TOPIC_AGGREGATE)
        self._t_relay_cmd  = config.topic(config.TOPIC_RELAY_CMD)
        self._t_relay_sts  = config.topic(config.TOPIC_RELAY_STS)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self):
        """
        Attempt to connect to the MQTT broker.
        Returns True on success, False on failure.
        Subscribes to the relay command topic on first connect.
        """
        if not wifi_is_up():
            print("[MQTT] No Wi-Fi — skipping broker connect")
            return False

        client_id = f"{config.NODE_ID}_{config.APPLIANCE_ID}"
        try:
            self._client = MQTTClient(
                client_id   = client_id,
                server      = config.MQTT_BROKER,
                port        = config.MQTT_PORT,
                user        = config.MQTT_USER,
                password    = config.MQTT_PASSWORD,
                keepalive   = config.MQTT_KEEPALIVE,
            )
            self._client.set_callback(self._on_message)
            self._client.connect()
            self._client.subscribe(self._t_relay_cmd, qos=1)
            self._connected = True
            print(f"[MQTT] Connected to {config.MQTT_BROKER}:{config.MQTT_PORT}")
            self._flush_buffer()
            return True

        except Exception as e:
            print(f"[MQTT] Connect failed: {e}")
            self._connected = False
            return False

    def ensure_connected(self):
        """
        Call at the top of each main loop iteration.
        Attempts reconnect if Wi-Fi or broker connection was lost.
        Returns True if connected (or just reconnected), False otherwise.
        """
        if self._connected:
            return True

        print(f"[MQTT] Reconnecting in {config.RECONNECT_DELAY_S}s...")
        utime.sleep(config.RECONNECT_DELAY_S)

        if not wifi_is_up():
            wifi_connect()

        return self.connect()

    def disconnect(self):
        """Gracefully disconnect from the broker."""
        if self._client and self._connected:
            try:
                self._client.disconnect()
            except Exception:
                pass
        self._connected = False
        self._client    = None

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_telemetry(self, data: dict):
        """
        Serialise data as JSON and publish to the telemetry topic (QoS 1).
        If not connected, the reading is buffered for later retransmission.

        Args:
            data : telemetry dict from CurrentSensor.combine()
        """
        payload = ujson.dumps(data)

        if not self._connected:
            self._buffer.push(payload)
            print(f"[MQTT] Offline — buffered ({self._buffer.size} queued)")
            return

        try:
            self._client.publish(self._t_telemetry, payload, qos=1)
        except Exception as e:
            print(f"[MQTT] Publish error: {e} — buffering reading")
            self._connected = False
            self._buffer.push(payload)

    def publish_aggregate(self, data: dict):
        """
        Publish an hourly aggregate payload (QoS 1).

        Expected keys: total_kwh, avg_w, max_w, min_w, cost_tnd,
                       window_start_ms, window_end_ms
        """
        if not self._connected:
            return   # aggregates are less critical; drop if offline

        payload = ujson.dumps(data)
        try:
            self._client.publish(self._t_aggregate, payload, qos=1)
            print(f"[MQTT] Aggregate published: {data['total_kwh']} kWh")
        except Exception as e:
            print(f"[MQTT] Aggregate publish error: {e}")
            self._connected = False

    def publish_relay_status(self, state: bool):
        """
        Publish the current relay state as a retained message.

        Args:
            state : True = ON, False = OFF
        """
        if not self._connected:
            return
        payload = b"ON" if state else b"OFF"
        try:
            self._client.publish(self._t_relay_sts, payload, retain=True, qos=1)
        except Exception as e:
            print(f"[MQTT] Relay status publish error: {e}")
            self._connected = False

    # ------------------------------------------------------------------
    # Receiving (relay commands)
    # ------------------------------------------------------------------

    def check_messages(self):
        """
        Non-blocking check for incoming MQTT messages.
        Must be called regularly from the main loop.
        Sets self.relay_command if a valid relay command is received.
        """
        if not self._connected:
            return
        try:
            self._client.check_msg()
        except Exception as e:
            print(f"[MQTT] check_msg error: {e}")
            self._connected = False

    def _on_message(self, topic, msg):
        """
        Internal MQTT message callback (called by umqtt on check_msg()).
        Only handles relay command topic; ignores everything else.
        """
        topic_str = topic.decode() if isinstance(topic, bytes) else topic
        msg_str   = msg.decode().strip().upper() if isinstance(msg, bytes) else msg.strip().upper()

        if topic_str == self._t_relay_cmd:
            if msg_str in ("ON", "OFF"):
                print(f"[MQTT] Relay command received: {msg_str}")
                self.relay_command = msg_str
            else:
                print(f"[MQTT] Unknown relay command: {msg_str}")

    # ------------------------------------------------------------------
    # Buffer flush
    # ------------------------------------------------------------------

    def _flush_buffer(self):
        """
        Publish all buffered readings after a successful reconnect.
        Called automatically by connect().
        """
        if self._buffer.is_empty():
            return

        print(f"[MQTT] Flushing {self._buffer.size} buffered readings...")
        flushed = 0
        for payload in self._buffer.flush():
            try:
                self._client.publish(self._t_telemetry, payload, qos=1)
                flushed += 1
            except Exception as e:
                print(f"[MQTT] Flush error at reading {flushed}: {e}")
                self._connected = False
                break

        print(f"[MQTT] Flushed {flushed} readings.")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def is_connected(self):
        return self._connected

    @property
    def buffer_size(self):
        return self._buffer.size