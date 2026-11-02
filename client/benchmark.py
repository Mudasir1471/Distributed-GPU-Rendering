#!/usr/bin/env python3
"""
Benchmark: local rendering vs remote GPU offloading.

    python benchmark.py --host 192.168.1.1 --files small.mp4 big.mp4 \
                        --resolutions 720p 1080p --bitrate 4M --repeat 2

Outputs  results.csv  and  benchmark_report.md  (paste the table in your report).
Optional: pip install psutil   -> also records local CPU utilisation.
"""
import argparse
import csv
import os
import subprocess
import tempfile
import threading
import time

from common import PORT, build_ffmpeg_cmd, find_ffmpeg, nvenc_works
from network import OffloadClient, OffloadError

try:
    import psutil
except ImportError:
    psutil = None


def local_render(ffmpeg, in_path, out_path, settings, use_nvenc):
    """Render on THIS machine. Returns (seconds, cpu_avg%, cpu_max%)."""
    cmd = build_ffmpeg_cmd(ffmpeg, in_path, out_path, settings, use_nvenc, progress=False)
    samples, stop = [], threading.Event()

    def sampler():
        psutil.cpu_percent(None)
        while not stop.wait(0.5):
            samples.append(psutil.cpu_percent(None))
    if psutil:
        threading.Thread(target=sampler, daemon=True).start()
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    stop.set()
    if r.returncode != 0:
        raise RuntimeError(f"local ffmpeg failed: {r.stderr[-300:]}")
    avg = sum(samples) / len(samples) if samples else None
    return elapsed, avg, (max(samples) if samples else None)


def mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def fmt(v, nd=2):
    return "n/a" if v is None else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--token", default="")
    ap.add_argument("--files", nargs="+", required=True)
    ap.add_argument("--resolutions", nargs="+", default=["720p"])
    ap.add_argument("--bitrate", default="4M")
    ap.add_argument("--preset", default="Balanced")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--local-encoder", choices=["auto", "libx264", "h264_nvenc"], default="auto")
    ap.add_argument("--skip-local", action="store_true")
    a = ap.parse_args()

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("ffmpeg not found locally (needed for the local baseline)")
    if a.local_encoder == "auto":
        use_nvenc = nvenc_works(ffmpeg)
    else:
        use_nvenc = a.local_encoder == "h264_nvenc"
    local_name = "h264_nvenc" if use_nvenc else "libx264"
    print(f"Local baseline encoder: {local_name}")

    client = OffloadClient(a.host, a.port, a.token, on_log=lambda m: None)
    info = client.check_worker()
    print(f"Worker GPU: {info['gpu']} | latency {info['latency_avg_ms']:.2f} ms")

    tmp = tempfile.mkdtemp(prefix="bench_")
    rows = []
    for path in a.files:
        size_mb = os.path.getsize(path) / 1e6
        for res in a.resolutions:
            settings = {"resolution": res, "bitrate": a.bitrate, "preset": a.preset}
            print(f"\n=== {os.path.basename(path)} ({size_mb:.1f} MB) @ {res} ===")
            loc, cpu_avg, cpu_max, rem = [], [], [], []
            for i in range(a.repeat):
                if not a.skip_local:
                    t, ca, cm = local_render(ffmpeg, path, os.path.join(tmp, "local.mp4"),
                                             settings, use_nvenc)
                    loc.append(t); cpu_avg.append(ca); cpu_max.append(cm)
                    print(f"  run {i + 1}: local  {t:7.2f}s")
                try:
                    m = client.run_job(path, os.path.join(tmp, "remote.mp4"), settings)
                except OffloadError as e:
                    print(f"  run {i + 1}: remote FAILED: {e}")
                    continue
                rem.append(m)
                print(f"  run {i + 1}: remote {m['total_s']:7.2f}s "
                      f"(up {m['upload_s']:.2f} / render {m['encode_s']:.2f} / "
                      f"down {m['download_s']:.2f})")
            if not rem:
                continue
            local_s = mean(loc)
            total = mean([m["total_s"] for m in rem])
            enc = mean([m["encode_s"] for m in rem])
            rows.append({
                "file": os.path.basename(path), "size_mb": round(size_mb, 1), "resolution": res,
                "local_s": local_s, "upload_s": mean([m["upload_s"] for m in rem]),
                "remote_render_s": enc, "download_s": mean([m["download_s"] for m in rem]),
                "remote_total_s": total,
                "overhead_s": mean([m["overhead_s"] for m in rem]),
                "overhead_pct": mean([m["overhead_s"] / m["total_s"] * 100 for m in rem]),
                "speedup_total": (local_s / total) if local_s else None,
                "speedup_compute_only": (local_s / enc) if local_s else None,
                "gpu_util_avg": mean([m["server_stats"].get("gpu_util_avg") for m in rem]),
                "gpu_mem_max_mb": mean([m["server_stats"].get("gpu_mem_max_mb") for m in rem]),
                "local_cpu_avg": mean(cpu_avg), "local_cpu_max": mean(cpu_max),
            })

    if not rows:
        raise SystemExit("No successful runs - nothing to report")

    with open("results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    with open("benchmark_report.md", "w") as f:
        f.write("# Benchmark results\n\n")
        f.write(f"- Worker GPU: **{info['gpu']}**\n- Link latency (avg of 5 pings): "
                f"**{info['latency_avg_ms']:.2f} ms**\n- Local encoder: **{local_name}**\n"
                f"- Settings: bitrate {a.bitrate}, preset {a.preset}, runs per cell: {a.repeat}\n\n")
        f.write("| File | Size MB | Res | Local s | Upload s | GPU render s | Download s | "
                "Remote total s | Net overhead s (%) | Speedup (total) | Speedup (compute only) | "
                "GPU util % | Local CPU % |\n")
        f.write("|" + "---|" * 13 + "\n")
        for r in rows:
            f.write(f"| {r['file']} | {r['size_mb']} | {r['resolution']} | {fmt(r['local_s'])} | "
                    f"{fmt(r['upload_s'])} | {fmt(r['remote_render_s'])} | {fmt(r['download_s'])} | "
                    f"{fmt(r['remote_total_s'])} | {fmt(r['overhead_s'])} ({fmt(r['overhead_pct'], 1)}%) | "
                    f"**{fmt(r['speedup_total'])}x** | {fmt(r['speedup_compute_only'])}x | "
                    f"{fmt(r['gpu_util_avg'], 1)} | {fmt(r['local_cpu_avg'], 1)} |\n")
        f.write("\nSpeedup (total) = local time / (upload + render + download).  "
                "Speedup (compute only) = local time / remote render time.\n")
    print("\nSaved results.csv and benchmark_report.md")


if __name__ == "__main__":
    main()
