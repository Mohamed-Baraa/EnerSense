# EnerSense
**Smart Energy Monitoring and Optimization System**

An IoT-based system that measures real-time energy consumption of household
appliances, visualises usage data on a local web dashboard, detects abnormal
consumption patterns, and optionally controls appliances via relay.

Built with ESP32 (MicroPython), MQTT, SQLite, Node-RED, and scikit-learn.
Runs entirely locally — no cloud, no subscription, no internet required.

---

## Repository Structure

```
EnerSense/
├── .gitignore
├── README.md
├── requirements.txt
│
├── firmware/
│   ├── config.py            # All constants: pins, broker IP, calibration, thresholds
│   ├── sensor.py            # ACS712 + ZMPT101B RMS computation
│   ├── mqtt_client.py       # Wi-Fi, MQTT, offline ring buffer, reconnect logic
│   ├── relay.py             # Relay GPIO control with safety timing lock
│   └── main.py              # Main acquisition and publish loop
│
├── simulation/
│   ├── micropython_shim.py  # Mocks MicroPython modules for desktop execution
│   ├── run.py               # Desktop simulation entry point
│   ├── diagram.json         # Wokwi circuit (for future reference)
│   └── wokwi.toml           # Wokwi project config (for future reference)
│
├── server/
│   ├── mosquitto.conf       # Local Mosquitto broker configuration
│   ├── database.py          # SQLite schema + Flask write server (:5051)
│   └── flows.json           # Node-RED flow: MQTT -> SQLite -> dashboard + ML
│
└── ml/
    ├── train.py             # Ridge regression training (hourly prediction)
    ├── anomaly.py           # Z-score statistical anomaly detector
    └── predict.py           # Flask inference server (:5050)
```

---

## Hardware (per node)

| Component        | Role                          | Cost (approx.) |
|------------------|-------------------------------|----------------|
| ESP32 DevKit V1  | Microcontroller + Wi-Fi       | ~5 USD         |
| ACS712-20A       | Non-invasive current sensor   | ~1.5 USD       |
| ZMPT101B         | AC voltage sensor (optional)  | ~2 USD         |
| 5V relay module  | Appliance on/off control      | ~1 USD         |
| Resistors, PCB   | Voltage divider, wiring       | ~2 USD         |
| **Total**        |                               | **~11 USD**    |

---

## Software Stack

| Layer     | Technology                  |
|-----------|-----------------------------|
| Firmware  | MicroPython on ESP32        |
| Transport | MQTT (Mosquitto broker)     |
| Storage   | SQLite (built into Python)  |
| Dashboard | Node-RED UI                 |
| ML        | Python, scikit-learn, Flask |

---

## Prerequisites

### Install once

**Python 3.11+**
Download from https://python.org. During install check "Add Python to PATH".

**Node.js + Node-RED**
Download Node.js from https://nodejs.org, then:
```powershell
npm install -g --unsafe-perm node-red
```

**Mosquitto MQTT Broker**
Download the Windows installer from https://mosquitto.org/download/

**Python dependencies**
```powershell
pip install -r requirements.txt
pip install paho-mqtt
```

**Node-RED palettes**
Start Node-RED once (`node-red`), open http://localhost:1880,
then go to Menu -> Manage Palette -> Install and install:
- `node-red-dashboard`

That is it. No InfluxDB, no extra database software needed.

---

## Running the Full Stack (Windows)

Open five separate PowerShell terminals and run each command below.
Order matters — start them top to bottom.

---

### Terminal 1 — MQTT Broker

First time only — create the password file:
```powershell
cd server
& "C:\Program Files\mosquitto\mosquitto_passwd.exe" -c passwd enersense
# enter: enersense123 when prompted
```

Then start the broker (always run from inside server\ folder):
```powershell
cd server
& "C:\Program Files\mosquitto\mosquitto.exe" -c mosquitto.conf -v
```

You should see:
```
mosquitto version 2.x.x starting
Opening ipv4 listen socket on port 1883
```

---

### Terminal 2 — SQLite Write Server

```powershell
python server\database.py --server
```

You should see:
```
[DB] Initialised -> server/enersense.db
[DB] Write server on http://127.0.0.1:5051
```

---

### Terminal 3 — ML Inference Server

First time only — train the model:
```powershell
python ml\train.py --synthetic
```

Then start the inference server:
```powershell
python ml\predict.py
```

You should see:
```
[Predict] Model loaded from ml/model.joblib
[Predict] EnerSense inference server starting on http://0.0.0.0:5050
```

---

### Terminal 4 — Node-RED Dashboard

```powershell
node-red
```

Then open http://localhost:1880 in your browser:
1. Click the menu in the top right corner
2. Click Import
3. Paste the contents of server/flows.json (or click "select file")
4. Click Import, then click Deploy (red button top right)

Dashboard is available at: http://localhost:1880/ui

---

### Terminal 5 — Firmware Simulation

```powershell
python simulation\run.py --load 800
```

You should see the firmware boot sequence followed by live readings:
```
==================================================
  EnerSense — Smart Energy Monitor
  Node: node_01  |  Appliance: appliance_01
==================================================
[WiFi] Simulated connection to 'YourSSID' — OK
[Boot] Calibrating sensors...
[Boot] Calibration done.
[MQTT] Connected to 127.0.0.1:1883

[Main] Entering main loop...

I=3.478A  V=230.0V  P=800.1W  E=0.0002Wh  Relay=OFF
I=3.479A  V=230.1V  P=800.4W  E=0.0004Wh  Relay=OFF
```

Open http://localhost:1880/ui — the power gauge and chart should be
updating live.

---

## Simulation Commands

While Terminal 5 is running, type commands directly into it:

| Command      | Effect                           |
|--------------|----------------------------------|
| `load 1200`  | Set simulated load to 1200 W     |
| `load 50`    | Set to near-standby load         |
| `anomaly`    | Spike to 2800 W for 5 seconds    |
| `relay`      | Print current relay state        |
| `quit`       | Stop the simulation              |

Try typing `anomaly` and watch the alert panel on the dashboard fire.

---

## MQTT Topic Structure

| Topic                                       | Direction      | Content      |
|---------------------------------------------|----------------|--------------|
| `enersense/{node}/{appliance}/telemetry`    | ESP32 -> broker | JSON, 1 Hz  |
| `enersense/{node}/{appliance}/aggregate`    | ESP32 -> broker | JSON, hourly|
| `enersense/{node}/{appliance}/relay/cmd`    | broker -> ESP32 | ON / OFF    |
| `enersense/{node}/{appliance}/relay/status` | ESP32 -> broker | ON / OFF    |

### Telemetry payload example

```json
{
  "I_rms":     4.23,
  "V_rms":     228.5,
  "P_W":       966.4,
  "E_Wh":      0.268,
  "pf":        0.99,
  "anomaly":   false,
  "relay":     "OFF",
  "node":      "node_01",
  "appliance": "appliance_01",
  "ts_ms":     1718000000000
}
```

---

## Verifying MQTT Traffic

Open a sixth terminal to watch all messages live:
```powershell
& "C:\Program Files\mosquitto\mosquitto_sub.exe" -h localhost -u enersense -P enersense123 -t "enersense/#" -v
```

---

## Deploying to Real Hardware (ESP32)

When you have the physical hardware assembled:

**1. Edit firmware\config.py:**
```python
WIFI_SSID     = "YourActualSSID"
WIFI_PASSWORD = "YourActualPassword"
MQTT_BROKER   = "192.168.x.x"   # your machine's LAN IP (not 127.0.0.1)
```

**2. Install mpremote:**
```powershell
pip install mpremote
```

**3. Flash all firmware files:**
```powershell
mpremote connect COM3 cp firmware/config.py      :config.py
mpremote connect COM3 cp firmware/sensor.py      :sensor.py
mpremote connect COM3 cp firmware/mqtt_client.py :mqtt_client.py
mpremote connect COM3 cp firmware/relay.py       :relay.py
mpremote connect COM3 cp firmware/main.py        :main.py
```
Replace COM3 with your actual port (check Device Manager -> Ports).

**4. Monitor serial output:**
```powershell
mpremote connect COM3 repl
```

---

## Status LED (GPIO2 / onboard LED)

| Pattern          | Meaning                  |
|------------------|--------------------------|
| Off              | Booting                  |
| Rapid blink (6x) | Wi-Fi connection failed  |
| 3 slow blinks    | Wi-Fi connected          |
| Solid on         | MQTT broker connected    |
| Off (after solid)| MQTT broker lost         |
| Fast blink (10x) | Unhandled crash -> reboot|

---

## Team

- Mohamed Baraa Ben Mahoud
- Yassine Ben Abdallah
- Moemen Ghozzi

**Supervisor:** Faouzi MOUSSA
**Institution:** Faculty of Sciences of Tunis — Department of Computer Sciences
**Academic Year:** 2025/2026
