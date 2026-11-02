# Performance Evaluation Report – Distributed GPU Task Offloading
**Course:** CSC-334 Parallel and Distributed Computing  **Name / Roll No:** ________

## 1. Test Setup
| | Client (laptop) | Worker (server) |
|---|---|---|
| CPU | | |
| RAM | | |
| GPU / VRAM | | |
| OS | | |
| Network link | Direct CAT6 / Wi-Fi, link speed ____ Mbps (check in adapter status) | |

Software: Python ___, FFmpeg ___, NVIDIA driver ___.  Static IPs: server 192.168.1.1, client 192.168.1.2.

## 2. Method
Same input files and encode settings were rendered (a) locally with `libx264` / local encoder and
(b) remotely with `h264_nvenc` via the offloading system. `client/benchmark.py` measured
upload, render, download and total time, repeated ___ times and averaged.

## 3. Results
*(paste the table from `benchmark_report.md` here)*

## 4. Metrics (formulas)
* **Speedup (total)** = T_local / (T_upload + T_render + T_download)
* **Speedup (compute only)** = T_local / T_render_remote
* **Network overhead** = T_total - T_render_remote (upload + download + protocol time); also as % of remote total
* **Transfer throughput** = file size / transfer time (MB/s) - compare with the link's theoretical limit (gigabit CAT6 ~ 110 MB/s)
* **Resource utilisation:** worker GPU util / VRAM / temperature (nvidia-smi sampling, reported by the server); client CPU % during local render.

## 5. Analysis
* Which file sizes / resolutions benefited most? Where is the break-even point (small files may not be worth transferring)?
* How large was the network overhead relative to render time? Direct cable vs Wi-Fi?
* Did the client stay free during offloading (thermal throttling / OOM avoided)?
* Limitations (e.g. whole-file transfer, single GPU queue) and improvements (resumable transfer, multiple workers).

## 6. Screenshots
Insert: GUI during render, server console, nvidia-smi during render, benchmark output.

## 7. Conclusion
