# =============================================================================
# EnerSense — simulation/run.py
# Desktop simulation entry point.
# Loads the MicroPython shim, then runs the firmware's main.py directly.
#
# Usage (from repo root):
#   pip install paho-mqtt
#   python simulation/run.py
#   python simulation/run.py --load 1200    # start with 1200W load
#   python simulation/run.py --anomaly      # inject an anomaly after 30s
#
# While running, type commands in the terminal:
#   load <watts>   — change simulated appliance load  (e.g. "load 800")
#   anomaly        — spike load to 2800W for 5 seconds
#   relay          — print current relay state
#   quit           — stop simulation
# =============================================================================

import sys
import os
import argparse
import threading
import time

# ---------------------------------------------------------------------------
# Path setup — must happen before any firmware imports
# ---------------------------------------------------------------------------

REPO_ROOT    = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
FIRMWARE_DIR = os.path.join(REPO_ROOT, 'firmware')
SIM_DIR      = os.path.join(REPO_ROOT, 'simulation')

sys.path.insert(0, SIM_DIR)
sys.path.insert(0, FIRMWARE_DIR)
sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Load the shim BEFORE importing anything from firmware
# ---------------------------------------------------------------------------

import micropython_shim as _shim   # registers all mock modules into sys.modules

# Now we can safely reference the shared sim state the shim exposes
sim_state = _shim._sim_state


# ---------------------------------------------------------------------------
# Interactive command thread
# ---------------------------------------------------------------------------

def command_loop():
    """
    Runs in a background thread.
    Reads commands from stdin and updates sim_state accordingly.
    """
    print("\n[Sim] Commands: load <W>  |  anomaly  |  relay  |  quit\n")

    while True:
        try:
            line = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        parts = line.split()
        cmd   = parts[0]

        if cmd == 'load' and len(parts) == 2:
            try:
                w = float(parts[1])
                sim_state['load_w'] = w
                print(f"[Sim] Load set to {w} W")
            except ValueError:
                print("[Sim] Usage: load <watts>  e.g. load 800")

        elif cmd == 'anomaly':
            print("[Sim] Injecting anomaly spike (2800 W for 5s)...")
            original = sim_state['load_w']
            sim_state['load_w'] = 2800.0
            time.sleep(5)
            sim_state['load_w'] = original
            print(f"[Sim] Load restored to {original} W")

        elif cmd == 'relay':
            # Relay state is tracked inside relay.py — we just print a hint
            print("[Sim] Check the [GPIO26] log lines above for relay state.")

        elif cmd in ('quit', 'exit', 'q'):
            print("[Sim] Stopping...")
            os._exit(0)

        else:
            print(f"[Sim] Unknown command: '{line}'")
            print("[Sim] Commands: load <W>  |  anomaly  |  relay  |  quit")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='EnerSense desktop simulation')
    parser.add_argument('--load',    type=float, default=500.0,
                        help='Initial simulated load in Watts (default: 500)')
    parser.add_argument('--anomaly', action='store_true',
                        help='Automatically inject anomaly after 30 seconds')
    parser.add_argument('--noise',   type=float, default=20.0,
                        help='ADC noise level in counts (default: 20)')
    args = parser.parse_args()

    # Apply initial simulation parameters
    sim_state['load_w'] = args.load
    sim_state['noise']  = args.noise

    print("=" * 55)
    print("  EnerSense — Desktop Simulation")
    print(f"  Initial load : {args.load} W")
    print(f"  ADC noise    : {args.noise} counts")
    print("=" * 55)

    # Start interactive command thread (daemon so it dies with main thread)
    cmd_thread = threading.Thread(target=command_loop, daemon=True)
    cmd_thread.start()

    # Optional: auto-inject anomaly after 30s
    if args.anomaly:
        def delayed_anomaly():
            time.sleep(30)
            print("\n[Sim] AUTO ANOMALY — spiking to 2800 W for 5s")
            sim_state['load_w'] = 2800.0
            time.sleep(5)
            sim_state['load_w'] = args.load
            print(f"[Sim] Load restored to {args.load} W")
        threading.Thread(target=delayed_anomaly, daemon=True).start()

    # Run the firmware main loop directly
    # main.py's __name__ guard won't fire so we call run() explicitly
    import main as firmware_main
    try:
        firmware_main.run()
    except KeyboardInterrupt:
        print("\n[Sim] Stopped by user.")


if __name__ == '__main__':
    main()
