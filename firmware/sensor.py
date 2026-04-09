# =============================================================================
# EnerSense — sensor.py
# Handles ADC sampling and RMS computation for the ACS712 current sensor
# and the optional ZMPT101B voltage sensor.
#
# Design notes:
#   - RMS is computed over a fixed window of CYCLES_PER_READ full mains cycles
#     (default: 5 cycles at 50 Hz = 100 ms window, 500 samples at 5 kHz).
#   - The DC offset (idle midpoint of the sensor output) is subtracted from
#     every sample before squaring. This is critical for AC RMS accuracy.
#   - On Wokwi the ACS712 is simulated with a potentiometer driving the ADC
#     pin. The RMS math is identical — only the input signal differs.
# =============================================================================

from machine import ADC, Pin
import utime
import math
import config


class CurrentSensor:
    """
    ACS712 current sensor driver.

    Usage:
        cs = CurrentSensor()
        reading = cs.read()
        print(reading['I_rms'], reading['P_W'], reading['E_Wh'])
    """

    def __init__(self):
        self._adc = ADC(Pin(config.PIN_CURRENT))
        # ESP32 MicroPython: attn(ADC.ATTN_11DB) → full 0–3.3 V input range
        self._adc.atten(ADC.ATTN_11DB)
        self._adc.width(ADC.WIDTH_12BIT)

        # Running energy accumulator (Wh)
        self._energy_wh   = 0.0
        self._last_ts_ms  = utime.ticks_ms()

        # Calibrated DC offset in ADC counts (updated by auto-calibrate)
        self._offset = config.ADC_OFFSET

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def auto_calibrate(self, samples=200):
        """
        Sample the ADC with no load connected and store the mean as the
        DC offset. Call once at startup before the first read().
        """
        total = 0
        for _ in range(samples):
            total += self._adc.read()
            utime.sleep_us(200)
        self._offset = total // samples
        return self._offset

    def read(self):
        """
        Acquire one RMS window, compute electrical quantities, accumulate
        energy, and return a result dict.

        Returns:
            {
                'I_rms' : float,   # RMS current  (A)
                'P_W'   : float,   # Active power  (W)  — uses V from VoltageSensor
                                   # or NOMINAL_VOLTAGE if sensor disabled
                'E_Wh'  : float,   # Cumulative energy since boot (Wh)
                'ts_ms' : int,     # Timestamp of this reading (ms since boot)
            }
        """
        counts_rms = self._sample_rms(
            self._adc,
            config.SAMPLES_PER_READ,
            self._offset
        )
        I_rms = self._counts_to_amps(counts_rms)

        now_ms   = utime.ticks_ms()
        delta_s  = utime.ticks_diff(now_ms, self._last_ts_ms) / 1000.0
        self._last_ts_ms = now_ms

        # Power and energy are filled in by main.py once V_rms is known.
        # We return I_rms here; main.py calls combine() to get the full dict.
        return {
            'I_rms' : round(I_rms, 4),
            'ts_ms' : now_ms,
            '_delta_s': delta_s,   # internal, used by combine()
        }

    def combine(self, current_reading, V_rms):
        """
        Given a current reading dict (from read()) and a V_rms value,
        compute power, accumulate energy, and return the full telemetry dict.

        Args:
            current_reading : dict returned by read()
            V_rms           : float, RMS voltage in Volts

        Returns:
            Full telemetry dict ready for JSON serialisation.
        """
        I_rms   = current_reading['I_rms']
        delta_s = current_reading['_delta_s']

        P_W  = round(I_rms * V_rms, 2)          # apparent power (W)
        dWh  = P_W * (delta_s / 3600.0)
        self._energy_wh += dWh

        return {
            'I_rms' : I_rms,
            'V_rms' : round(V_rms, 2),
            'P_W'   : P_W,
            'E_Wh'  : round(self._energy_wh, 4),
            'pf'    : 1.0,          # assumed unity; update if reactive power is needed
            'ts_ms' : current_reading['ts_ms'],
        }

    def reset_energy(self):
        """Reset the cumulative energy counter (e.g. at the start of a new day)."""
        self._energy_wh = 0.0

    @property
    def energy_wh(self):
        return self._energy_wh

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sample_rms(adc, n_samples, offset):
        """
        Sample the ADC n_samples times and return the RMS value in ADC counts
        (with DC offset removed).

        The inter-sample delay is set to achieve approximately SAMPLE_FREQ_HZ.
        At 5 kHz: period = 200 µs.
        """
        delay_us = 1_000_000 // config.SAMPLE_FREQ_HZ   # 200 µs at 5 kHz
        sum_sq   = 0
        for _ in range(n_samples):
            val    = adc.read() - offset
            sum_sq += val * val
            utime.sleep_us(delay_us)
        mean_sq = sum_sq / n_samples
        return math.sqrt(mean_sq)

    @staticmethod
    def _counts_to_amps(counts_rms):
        """
        Convert RMS ADC counts to RMS current in Amperes.

        Formula:
            V_rms_at_pin = counts_rms * (ADC_VREF / ADC_MAX)
            I_rms        = V_rms_at_pin / ACS712_SENSITIVITY
        """
        v_rms_pin = counts_rms * (config.ADC_VREF / config.ADC_MAX)
        return v_rms_pin / config.ACS712_SENSITIVITY


class VoltageSensor:
    """
    ZMPT101B voltage sensor driver.

    If config.VOLTAGE_SENSOR_ENABLED is False, read() always returns
    config.NOMINAL_VOLTAGE without touching the ADC.
    """

    def __init__(self):
        if config.VOLTAGE_SENSOR_ENABLED:
            self._adc = ADC(Pin(config.PIN_VOLTAGE))
            self._adc.atten(ADC.ATTN_11DB)
            self._adc.width(ADC.WIDTH_12BIT)
        else:
            self._adc = None

        self._offset = config.ADC_OFFSET

    def auto_calibrate(self, samples=200):
        """Same DC-offset calibration as CurrentSensor."""
        if self._adc is None:
            return self._offset
        total = 0
        for _ in range(samples):
            total += self._adc.read()
            utime.sleep_us(200)
        self._offset = total // samples
        return self._offset

    def read(self):
        """
        Returns RMS voltage in Volts.
        Falls back to NOMINAL_VOLTAGE if sensor is disabled.
        """
        if self._adc is None:
            return config.NOMINAL_VOLTAGE

        counts_rms = CurrentSensor._sample_rms(
            self._adc,
            config.SAMPLES_PER_READ,
            self._offset
        )
        return self._counts_to_volts(counts_rms)

    @staticmethod
    def _counts_to_volts(counts_rms):
        """
        Convert RMS ADC counts to RMS mains voltage in Volts.

        ZMPT101B_SCALE is an empirical factor determined by comparing the
        output to a calibrated reference voltmeter.
        """
        v_rms_pin = counts_rms * (config.ADC_VREF / config.ADC_MAX)
        return v_rms_pin * config.ZMPT101B_SCALE


# =============================================================================
# Standalone test — runs only when this file is executed directly (not imported)
# Useful for serial monitor verification on Wokwi or real hardware.
# =============================================================================
if __name__ == "__main__":
    print("EnerSense sensor.py — standalone test")
    print(f"Sampling {config.SAMPLES_PER_READ} samples per window "
          f"({config.CYCLES_PER_READ} cycles @ {config.MAINS_FREQ_HZ} Hz)")

    cs = CurrentSensor()
    vs = VoltageSensor()

    print("Auto-calibrating DC offsets...")
    i_offset = cs.auto_calibrate()
    v_offset = vs.auto_calibrate()
    print(f"  Current sensor offset : {i_offset} counts")
    print(f"  Voltage sensor offset : {v_offset} counts")

    print("\nStarting continuous readings (Ctrl+C to stop):\n")
    print(f"{'I_rms (A)':>12} {'V_rms (V)':>12} {'P_W (W)':>10} {'E_Wh':>12}")
    print("-" * 50)

    while True:
        cr = cs.read()
        V  = vs.read()
        telemetry = cs.combine(cr, V)
        print(
            f"{telemetry['I_rms']:>12.4f} "
            f"{telemetry['V_rms']:>12.2f} "
            f"{telemetry['P_W']:>10.2f} "
            f"{telemetry['E_Wh']:>12.4f}"
        )
        utime.sleep(1)