**DISCLAIMER: This mod and companion VOIP client were created with the assistance of Antigravity Orchestrator (Neon/Argon). We are not affiliated with the xrRazom team or its author in any way — this was built primarily for our own co-op sessions and immersion. Other modders, as well as the xrRazom developer himself, are completely free to take, modify, or integrate any part of this into their own mods (including bundling it into xrRazom by default if desired).**

**PLATFORM STATUS: Built for both Linux and Windows, but so far only tested on Linux (CachyOS). Windows support is implemented via `sounddevice` and `Start_VOIP_Windows.bat` — bug reports and feedback from Windows testers are very welcome!**

---

# xrRazom VOIP — 3D Proximity & Walkie-Talkie VOIP for S.T.A.L.K.E.R. Anomaly / G.A.M.M.A. Co-op

A standalone, open-source **3D proximity + walkie-talkie VOIP** mod and companion voice client for **S.T.A.L.K.E.R. Anomaly 1.5.3** and **G.A.M.M.A.** running **xrRazom Co-op**.

- **Standalone:** Does not overwrite any base Anomaly, G.A.M.M.A., or xrRazom Co-op files.
- **Works with or without G.A.M.M.A.:** Requires only S.T.A.L.K.E.R. Anomaly 1.5.3 + xrRazom Co-op. **Mod Configuration Menu (MCM)** is optional (falls back cleanly to defaults if MCM is not installed).

---

## Features

1. **Directional 3D Proximity Voice (Hands-Free / Open Mic)**
   - Voice-activated (VAD) with an adjustable noise gate — no Push-to-Talk needed when talking nearby.
   - Real-time 3D stereo panning and subtle Interaural Time Delay (ITD) derived from the player's camera orientation (`cam_pos`, `cam_dir`, `cam_top`).
   - Smooth distance rolloff and high-frequency air absorption (configurable max proximity range, default 50 m).
   - **Wall & Terrain Occlusion:** Casts a static `ray_pick` (`rqtStatic`) between players to muffle voices behind solid walls, buildings, and terrain.

2. **Walkie-Talkie Radio Voice (Hold `Z` — Rebindable in MCM)**
   - By default, all players always have a built-in walkie-talkie (`always_has_radio = true`). You can optionally require a walkie-talkie item in inventory or slot 9 via MCM.
   - Classic walkie-talkie DSP: 380–2750 Hz bandpass filter, soft overdrive/compression, continuous radio static hiss, crisp **PTT key-up chirp ("pling")**, and **squelch tail** on release.
   - Works across the entire Zone regardless of distance or current level.
   - **Simultaneous Proximity + Radio ("Double Audio"):** When a nearby teammate transmits on radio (`Z`), you hear both their directional 3D voice and the slightly delayed radio transmission at the same time.
   - **Works seamlessly on Linux/Wayland:** Because in-game PTT is captured inside S.T.A.L.K.E.R. via Lua and exported to `voip_telemetry.json`, the VOIP app does **not** need global unfocused-window hotkey permissions.

---

## Requirements & Installing Dependencies

The companion VOIP client (`voip_client/xr_razom_voip_client.py`) requires **Python 3.10+** with Tkinter (`tk`) and the following Python packages (`voip_client/requirements.txt`):

```text
numpy>=1.24.0
scipy>=1.10.0
sounddevice>=0.4.6
```

> **Linux audio note:** On Linux, the client uses `libpulse-simple.so.0` (provided by PipeWire-Pulse or PulseAudio) by default for low-latency audio, and falls back to `sounddevice` (PortAudio) if PulseSimple is unavailable.

### Arch Linux / CachyOS / EndeavourOS / Manjaro *(Tested on CachyOS)*

Install via `pacman` (recommended — uses system packages):
```bash
sudo pacman -S --needed python python-numpy python-scipy tk libpulse pipewire-pulse
```

If you also want the `sounddevice` (PortAudio) fallback installed in a virtual environment (or via `pip`):
```bash
sudo pacman -S --needed portaudio
python3 -m venv voip_client/.venv
source voip_client/.venv/bin/activate
pip install "numpy>=1.24.0" "scipy>=1.10.0" "sounddevice>=0.4.6"
```

### Ubuntu / Debian / Linux Mint / Pop!_OS
```bash
sudo apt update
sudo apt install -y python3 python3-numpy python3-scipy python3-tk libpulse0
```
*(Optional PortAudio / `sounddevice` fallback:)*
```bash
sudo apt install -y libportaudio2 python3-sounddevice
```

### Fedora / Nobara
```bash
sudo dnf install -y python3 python3-numpy python3-scipy python3-tkinter pulseaudio-libs
```
*(Optional PortAudio / `sounddevice` fallback:)*
```bash
sudo dnf install -y portaudio
python3 -m pip install --user "sounddevice>=0.4.6"
```

### Generic Linux (`venv` + `pip`)
```bash
python3 -m venv voip_client/.venv
source voip_client/.venv/bin/activate
pip install -r voip_client/requirements.txt
```

### Windows 10 / 11 *(Untested — Feedback Welcome)*
1. Install **Python 3.10+** from [python.org/downloads](https://www.python.org/downloads/).
   - During setup, check **"Add python.exe to PATH"**.
   - Keep **"tcl/tk and IDLE"** checked (required for the GUI window).
2. Double-click **`voip_client\Start_VOIP_Windows.bat`** — it will automatically run `pip install -r requirements.txt` (`numpy>=1.24.0`, `scipy>=1.10.0`, `sounddevice>=0.4.6`) if needed and launch the client.
3. Or install manually in PowerShell / Command Prompt:
```powershell
python -m pip install --upgrade pip
python -m pip install -r voip_client\requirements.txt
python voip_client\xr_razom_voip_client.py
```
*(The Windows `sounddevice` wheel bundles PortAudio automatically, so no separate C libraries are required.)*

---

## Installation

### 1. Install the In-Game Telemetry Mod

#### Option A: G.A.M.M.A. / Mod Organizer 2 (Recommended)
1. Download/extract the mod folder (`xrRazom_VOIP_Telemetry`).
2. Either install the release archive via Mod Organizer 2's **"Install a new mod from an archive"** button, copy `xrRazom_VOIP_Telemetry` into `GAMMA/mods/`, or run on Linux:
```bash
./Install_Mod_in_GAMMA.sh /path/to/GAMMA/mods
```
3. In **Mod Organizer 2**, press **F5** (Refresh), enable **`xrRazom_VOIP_Telemetry`**, and place it **below `xrRazom Co-op`** in your load order.

#### Option B: Vanilla S.T.A.L.K.E.R. Anomaly 1.5.3 + xrRazom Co-op (No G.A.M.M.A.)
- Copy the contents of `gamedata/` into your `Anomaly/gamedata/` folder (or install via Mod Organizer 2).

> **G.A.M.M.A. Keybind Note:** By default, G.A.M.M.A.'s **Quick Action Wheel** also uses **`Z`**. To avoid opening the wheel while keying the walkie-talkie, rebind either **MCM -> xrRazom VOIP -> Walkie-Talkie PTT Key** or the Quick Action Wheel keybind in MCM.

### 2. Launch the Companion VOIP Client

Keep the `voip_client/` folder extracted outside MO2 (e.g., on your Desktop or next to your game folder):

- **Linux:**
```bash
./voip_client/Start_VOIP_Linux.sh
```
- **Windows:**
  Double-click `voip_client\Start_VOIP_Windows.bat`.

In section **1. STALKER Telemetry**, verify that the path points to your **`Anomaly/appdata/voip_telemetry.json`** (click **Browse...** if it isn't auto-detected; the file is generated as soon as you load into a level, and your selected path/IP/port are remembered in `voip_config.json`). Once in-game, the status line will change to:
`Active: <YourName> | Level: <level_name> | Nearby peers: <n> | VOIP peers: <n>`

---

## Networking & Firewall Ports

- **xrRazom Co-op** uses UDP port **`27015`** by default.
- **xrRazom VOIP** uses a **separate** UDP port (**`27016`** by default) so it never conflicts with the game socket. Any free UDP port works as long as all players use the same port.
- **Host:** Select **Host (Server + Client)**, allow UDP port **`27016`** through your firewall (and port-forward if playing over the internet instead of LAN/VPN like WireGuard/Tailscale/ZeroTier), and click **Start VOIP**.
  - Example on Linux (`ufw`):
  ```bash
  sudo ufw allow 27016/udp
  ```
  - Example on Windows (PowerShell as Administrator):
  ```powershell
  New-NetFirewallRule -DisplayName "xrRazom VOIP UDP 27016" -Direction Inbound -Protocol UDP -LocalPort 27016 -Action Allow
  ```
- **Peers:** Select **Peer (Connect to Host IP)**, enter the Host's LAN/VPN/public IP and port **`27016`**, and click **Start VOIP**.

---

## Audio Self-Test (Loopback)

You can test 3D panning, distance attenuation, wall occlusion, and the walkie-talkie DSP **even without S.T.A.L.K.E.R. running**:
1. Start the VOIP client (Host mode on `127.0.0.1` is fine) and enable **Enable Loopback Self-Test**.
2. Adjust **Test Distance**, **Direction (-90° Left / +90° Right)**, and **Simulate wall occlusion**.
3. Hold **`Z`** (while the VOIP window is focused), hold the **Hold for Walkie-Talkie** button, or check **Lock Walkie-Talkie PTT On** to preview the PTT chirp, radio static, squelch tail, and double-audio effect.

---

## Telemetry Schema (`$app_data_root$/voip_telemetry.json`)

Written at ~20 Hz (50 ms) and flushed immediately on PTT press/release:

```json
{
  "timestamp": 1050,
  "name": "Player",
  "is_host": true,
  "in_session": true,
  "last_ip": "10.8.0.1:27015",
  "level": "l01_escape",
  "pos": {"x": 10.0, "y": 2.0, "z": 10.0},
  "dir": {"x": 0.0, "y": 0.0, "z": 1.0},
  "top": {"x": 0.0, "y": 1.0, "z": 0.0},
  "has_radio": true,
  "radio_tx": false,
  "peers": [
    {
      "id": 101,
      "name": "Teammate",
      "pos": {"x": 15.0, "y": 1.62, "z": 10.0},
      "occluded": false
    }
  ]
}
```
