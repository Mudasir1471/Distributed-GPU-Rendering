# Custom Distributed Task Offloading & Remote GPU Rendering System

**CSC-334: Parallel and Distributed Computing**

A weak client laptop sends a video to a remote worker PC (NVIDIA GPU). The worker
transcodes it with **FFmpeg + NVENC (`h264_nvenc`)**, streams live progress back, and
returns the verified result. Pure Python sockets, no web framework needed.

```
CLIENT LAPTOP                                  WORKER PC (NVIDIA GPU)
 CustomTkinter GUI                              server.py daemon
   └─ network.py ── TCP :5050 (CAT6 / Wi-Fi) ──►  listener ─► job queue ─► ffmpeg h264_nvenc
        ▲   PING/HELLO · upload+SHA256 · PROGRESS/LOG/HEARTBEAT · result+SHA256   │
        └──────────────────────────────────────────────────────────────────────────┘
```

## Repository layout

```
client/   app.py (GUI) · network.py (engine) · benchmark.py · common.py · requirements.txt
server/   server.py (daemon) · common.py · requirements.txt
docs/     REPORT_TEMPLATE.md
```
`common.py` is an identical copy in both folders (protocol + ffmpeg command builder) so each
folder can be copied to its own computer and run standalone.

---

## 1. Setup

### Server (the PC with the NVIDIA GPU)
1. Install the latest **NVIDIA driver** (check with `nvidia-smi` in a terminal).
2. Install **Python 3.9+** (tick *"Add to PATH"* on Windows).
3. Install **FFmpeg** with NVENC (any recent "full" build):
   - Windows: `winget install Gyan.FFmpeg` (or download from gyan.dev and add `bin/` to PATH). Open a *new* terminal and check `ffmpeg -version`.
   - Linux: `sudo apt install ffmpeg`
4. Copy the `server/` folder to this PC. No `pip install` needed.

### Client (the weak laptop)
1. Install **Python 3.9+**.
2. Install **FFmpeg** (only needed for `benchmark.py`'s local baseline; the GUI itself does not need it).
3. In the `client/` folder:
   ```bash
   pip install -r requirements.txt
   ```

---

## 2. Network configuration (direct cable or Wi-Fi)

Connect both PCs with a **CAT6 Ethernet cable** (no router needed on modern NICs), then set static IPs:

| Machine | IP address | Subnet mask | Gateway |
|---|---|---|---|
| Server | `192.168.1.1` | `255.255.255.0` | leave empty |
| Client | `192.168.1.2` | `255.255.255.0` | leave empty |

**Windows:** `Win+R` → `ncpa.cpl` → right-click the **Ethernet** adapter → Properties →
*Internet Protocol Version 4 (TCP/IPv4)* → Properties → *Use the following IP address* → enter the values above.

**Linux:** `sudo ip addr add 192.168.1.1/24 dev eth0` (use your interface name from `ip a`).

**Wi-Fi alternative:** put both PCs on the same Wi-Fi / phone hotspot and use the server's real IP
(`ipconfig` on Windows / `ip a` on Linux) in the client instead of 192.168.1.1.

**Firewall (Windows server):** allow the port once, in an *Administrator* PowerShell:
```powershell
New-NetFirewallRule -DisplayName "GPU Offload" -Direction Inbound -Protocol TCP -LocalPort 5050 -Action Allow
```
(or click *Allow access* when Windows asks on first run of `server.py`).

**Verify:** from the client run `ping 192.168.1.1`. Replies = cable and IPs are fine.

> Testing on a single PC? Use `127.0.0.1` as the server IP.

---

## 3. Running

**Step 1: start the daemon on the server PC**
```bash
cd server
python server.py --token mysecret          # token is optional but recommended
```
You should see `GPU: NVIDIA ... | NVENC working: True` and `worker listening on 0.0.0.0:5050`.
Options: `--port`, `--workdir`, `--max-size-gb`, `--require-gpu` (never fall back to CPU).

**Step 2: start the GUI on the client laptop**
```bash
cd client
python app.py
```
1. Enter **Server IP** (`192.168.1.1`) and token → **Test Connection**. A green status shows latency and the remote GPU name.
2. **Browse** an input video, choose Resolution / Bitrate / Preset.
3. **Start Render**. Watch Upload → Render (live %) → Download in the progress bar and log terminal.
4. The output is saved next to the input as `*_offloaded.mp4`.

**Step 3: benchmark (for the report)**
```bash
cd client
python benchmark.py --host 192.168.1.1 --token mysecret \
   --files small.mp4 medium.mp4 large.mp4 --resolutions 720p 1080p --repeat 2
```
Produces `results.csv` and `benchmark_report.md`. Fill in `docs/REPORT_TEMPLATE.md` with it.

---

## 4. How it works (protocol summary)

Every message = `[4-byte length][JSON]`, optionally followed by raw file bytes.

| Phase | Messages |
|---|---|
| Handshake | 5× `PING/PONG` (latency) → `HELLO` (version + token) → `HELLO_ACK` (GPU, NVENC, queue) |
| Upload | `JOB` (name, size, SHA-256, settings) → `READY_FOR_UPLOAD` → bytes → `UPLOAD_OK` / `UPLOAD_FAILED` |
| Render | `QUEUED` → `STARTED` → streaming `PROGRESS` (% / fps / speed) and `LOG`; `HEARTBEAT` every 3 s |
| Result | `DONE` (size, SHA-256, GPU stats) → bytes → client `ACK` or `RESEND` (max 3×) |

**GPU execution:** `ffmpeg -c:v h264_nvenc -preset p2/p4/p6 -b:v <bitrate> [-vf scale=-2:H]`. Progress is parsed
from `ffmpeg -progress pipe:1` against the `ffprobe` duration. GPU utilisation / VRAM / temperature
are sampled from `nvidia-smi` every second and reported to the client.

**Robustness features**
- Connect / IO timeouts and detection of a silent worker (heartbeat; 30 s limit).
- Upload retried automatically (3×, back-off) on network error or checksum mismatch.
- SHA-256 verification of both the upload and the downloaded result; resend on corruption.
- If the client disconnects or cancels, the server kills FFmpeg and deletes the temp files.
- Server: input whitelisting (resolution/bitrate/preset), safe filenames, no shell execution, size and
  disk-space limits, optional shared-secret token (constant-time compare).
- Jobs from several clients are queued and run one at a time on the GPU.

**Known limitation:** if the connection drops *after* the upload while the render is running, the job is
cancelled and the user must resubmit (no job-resume).

---

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| `connection refused` | `server.py` not running, wrong IP/port, or firewall blocks 5050 |
| `timed out` | Cable unplugged / wrong static IP / not on the same subnet. Test with `ping` |
| `NVENC working: False` | Update NVIDIA driver; use a recent FFmpeg build; server falls back to CPU unless `--require-gpu` |
| `ffmpeg not found` | Install FFmpeg and open a **new** terminal so PATH refreshes |
| `authentication failed` | Token differs between `server.py --token` and the GUI field |

---

## 6. Demo

_Add your screenshots / GIFs here:_

| GUI during render | Server console | nvidia-smi during render |
|---|---|---|
| `docs/gui.png` | `docs/server.png` | `docs/nvidia-smi.png` |

## 7. Benchmark results

_Paste the table from `benchmark_report.md` and link your full report here._
