# =============================================================================
# EnerSense — relay.py
# Controls the relay module via a single GPIO pin.
#
# Design notes:
#   - The relay is active-HIGH (most 5 V relay modules with optocoupler).
#     If yours is active-LOW, flip the logic in _apply().
#   - A minimum on/off time lock (from config) prevents rapid switching,
#     which can damage both the relay contacts and the connected appliance.
#   - State is tracked internally so main.py can query it without re-reading
#     the GPIO pin.
# =============================================================================

from machine import Pin
import utime
import config


class Relay:
    """
    Single-channel relay controller with safety timing lock.

    Usage:
        relay = Relay()
        relay.turn_on()
        relay.turn_off()
        relay.set(True)          # True = ON, False = OFF
        relay.toggle()
        print(relay.is_on)       # True / False
        print(relay.state_str)   # 'ON' / 'OFF'
    """

    def __init__(self):
        self._pin       = Pin(config.PIN_RELAY, Pin.OUT)
        self._state     = False          # False = OFF
        self._last_change_ms = utime.ticks_ms() - 60_000   # allow immediate first switch

        # Apply initial OFF state
        self._apply(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def turn_on(self):
        """Switch relay ON if the minimum OFF time has elapsed."""
        self._switch(True)

    def turn_off(self):
        """Switch relay OFF if the minimum ON time has elapsed."""
        self._switch(False)

    def set(self, state: bool):
        """Set relay to a specific state. True = ON, False = OFF."""
        self._switch(state)

    def toggle(self):
        """Toggle relay to the opposite state."""
        self._switch(not self._state)

    def force_off(self):
        """
        Immediately cut power regardless of timing lock.
        Use only for safety-critical shutdowns (e.g. overcurrent detection).
        """
        self._apply(False)
        print("[Relay] FORCED OFF (safety override)")

    @property
    def is_on(self):
        return self._state

    @property
    def is_off(self):
        return not self._state

    @property
    def state_str(self):
        return "ON" if self._state else "OFF"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _switch(self, target: bool):
        """
        Switch to target state, respecting the minimum on/off timing lock.
        Logs a warning and does nothing if the lock has not expired.
        """
        if target == self._state:
            return   # already in requested state, nothing to do

        now_ms   = utime.ticks_ms()
        elapsed_s = utime.ticks_diff(now_ms, self._last_change_ms) / 1000.0

        if self._state:
            # Currently ON — check minimum ON time before allowing OFF
            min_s = config.RELAY_MIN_ON_TIME_S
            label = "ON"
        else:
            # Currently OFF — check minimum OFF time before allowing ON
            min_s = config.RELAY_MIN_OFF_TIME_S
            label = "OFF"

        if elapsed_s < min_s:
            remaining = min_s - elapsed_s
            print(f"[Relay] Switch blocked — minimum {label} time not elapsed "
                  f"({remaining:.1f}s remaining)")
            return

        self._apply(target)

    def _apply(self, state: bool):
        """Directly drive the GPIO pin and update internal state."""
        self._pin.value(1 if state else 0)   # active-HIGH; flip if active-LOW
        self._state          = state
        self._last_change_ms = utime.ticks_ms()
        print(f"[Relay] -> {self.state_str}")