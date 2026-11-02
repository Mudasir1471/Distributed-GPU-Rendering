#!/usr/bin/env python3
"""
Remote GPU Worker Daemon  (runs on the machine with the NVIDIA GPU)

    python server.py                      # listen on 0.0.0.0:5050
    python server.py --token mysecret     # require a shared secret
    python server.py --require-gpu        # refuse to run without NVENC

Pipeline:  socket listener -> per-client handler thread -> job queue
           -> single GPU worker thread -> ffmpeg (h264_nvenc) -> stream back
"""
import argparse
import collections
import hmac
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from common import (PORT, PROTOCOL_VERSION, ConnectionClosed, ProtocolError,
                    build_ffmpeg_cmd, find_ffmpeg, nvenc_works, recv_file,
                    recv_msg, send_file, send_msg, sha256_file,
                    validate_settings)

log = logging.getLogger("worker")

HEARTBEAT_SECS = 3        # server pings client while a job is running
IO_TIMEOUT = 30           # max silence tolerated on a socket
PROGRESS_KEYS = {"frame", "fps", "stream_0_0_q", "bitrate", "total_size",
                 "out_time_us", "out_time_ms", "out_time", "dup_frames",
                 "drop_frames", "speed", "progress"}


# ----------------------------------------------------------------- helpers
class Job:
    def __init__(self, workdir, filename, settings, size):
        self.id = uuid.uuid4().hex[:8]
        self.dir = os.path.join(workdir, self.id)
        ext = os.path.splitext(filename)[1].lower()
        ext = ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".bin"
        self.in_path = os.path.join(self.dir, "input" + ext)
        self.out_path = os.path.join(self.dir, "output.mp4")
        self.settings = settings
        self.size = size
        self.outbox = queue.Queue()        # worker -> handler messages
        self.cancelled = threading.Event()
        self.proc = None


def detect_gpu():
    smi = shutil.which("nvidia-smi")
    if not smi:
        return "none"
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return out.splitlines()[0] if out else "unknown"
    except Exception:
        return "unknown"


def probe_duration(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


class GpuMonitor(threading.Thread):
    """Samples nvidia-smi once per second while a job is running."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.samples = []

    def run(self):
        smi = shutil.which("nvidia-smi")
        if not smi:
            return
        while not self.stop_evt.wait(1.0):
            try:
                out = subprocess.run(
                    [smi, "--query-gpu=utilization.gpu,memory.used,temperature.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3).stdout.strip()
                util, mem, temp = [float(x) for x in out.splitlines()[0].split(",")]
                self.samples.append((util, mem, temp))
            except Exception:
                pass

    def summary(self):
        self.stop_evt.set()
        if not self.samples:
            return {}
        n = len(self.samples)
        return {"gpu_util_avg": round(sum(s[0] for s in self.samples) / n, 1),
                "gpu_util_max": max(s[0] for s in self.samples),
                "gpu_mem_max_mb": max(s[1] for s in self.samples),
                "gpu_temp_max_c": max(s[2] for s in self.samples)}


# ------------------------------------------------------------- GPU worker
def run_job(job, caps, cfg):
    use_nvenc = caps["nvenc"]
    if not use_nvenc and cfg.require_gpu:
        raise RuntimeError("NVENC not available on this worker")
    encoder = "h264_nvenc" if use_nvenc else "libx264 (CPU fallback)"
    duration = probe_duration(job.in_path)
    cmd = build_ffmpeg_cmd(caps["ffmpeg"], job.in_path, job.out_path,
                           job.settings, use_nvenc)
    log.info("job %s: %s", job.id, " ".join(cmd))
    job.outbox.put({"type": "STARTED", "encoder": encoder, "gpu": caps["gpu"],
                    "duration": duration})

    monitor = GpuMonitor()
    monitor.start()
    t0 = time.perf_counter()
    job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
    tail = collections.deque(maxlen=15)
    stats, logged = {}, 0
    for raw in job.proc.stdout:
        if job.cancelled.is_set():
            job.proc.kill()
            break
        line = raw.strip()
        if not line:
            continue
        key, _, val = line.partition("=")
        if key in PROGRESS_KEYS:
            stats[key] = val
            if key == "progress":
                us = stats.get("out_time_us") or stats.get("out_time_ms") or ""
                pct = None
                if duration and us.lstrip("-").isdigit():
                    pct = max(0.0, min(99.9, int(us) / 1e6 / duration * 100))
                if val == "end":
                    pct = 100.0
                job.outbox.put({"type": "PROGRESS", "percent": pct,
                                "fps": stats.get("fps"), "speed": stats.get("speed"),
                                "frame": stats.get("frame")})
        else:
            tail.append(line)
            if logged < 200:
                logged += 1
                job.outbox.put({"type": "LOG", "line": line})
    rc = job.proc.wait()
    encode_s = time.perf_counter() - t0
    gpu_stats = monitor.summary()
    if job.cancelled.is_set():
        return
    if rc != 0:
        raise RuntimeError(f"ffmpeg exited with code {rc}: {' | '.join(list(tail)[-3:])}")
    job.outbox.put({"type": "DONE", "sha256": sha256_file(job.out_path),
                    "stats": {"encode_seconds": round(encode_s, 3),
                              "encoder": encoder, **gpu_stats}})


def worker_loop(job_queue, caps, cfg):
    while True:
        job = job_queue.get()
        if job.cancelled.is_set():
            continue
        try:
            run_job(job, caps, cfg)
        except Exception as e:  # report ANY failure to the waiting client
            log.exception("job %s failed", job.id)
            job.outbox.put({"type": "ERROR", "message": str(e)})


# ------------------------------------------------------- per-client handler
def send_result(conn, job, done_msg):
    """Send rendered file; resend (max 3x) if the client's checksum fails."""
    size = os.path.getsize(job.out_path)
    conn.settimeout(60)
    for attempt in range(1, 4):
        kind = "DONE" if attempt == 1 else "RESULT_RESEND"
        send_msg(conn, {**done_msg, "type": kind, "size": size})
        send_file(conn, job.out_path, size)
        reply = recv_msg(conn)
        if reply.get("type") == "ACK":
            return True
        log.warning("job %s: client reported corrupt download (attempt %d)",
                    job.id, attempt)
    return False


def handle_job(conn, msg, cfg, caps, job_queue):
    try:
        settings = validate_settings(msg.get("settings", {}))
        size = int(msg["size"])
        sha = str(msg["sha256"]).lower()
        filename = os.path.basename(str(msg.get("filename", "input.mp4")))
    except (KeyError, ValueError, TypeError) as e:
        send_msg(conn, {"type": "ERROR", "message": f"bad job request: {e}"})
        return
    if not 0 < size <= cfg.max_size:
        send_msg(conn, {"type": "ERROR", "message": "file size not accepted"})
        return
    if shutil.disk_usage(cfg.workdir).free < size * 3:
        send_msg(conn, {"type": "ERROR", "message": "worker is out of disk space"})
        return

    job = Job(cfg.workdir, filename, settings, size)
    os.makedirs(job.dir)
    try:
        send_msg(conn, {"type": "READY_FOR_UPLOAD", "job_id": job.id})
        log.info("job %s: receiving %s (%.1f MB)", job.id, filename, size / 1e6)
        got = recv_file(conn, job.in_path, size)
        if got != sha:
            log.warning("job %s: upload checksum mismatch", job.id)
            send_msg(conn, {"type": "UPLOAD_FAILED", "reason": "checksum mismatch"})
            return
        send_msg(conn, {"type": "UPLOAD_OK"})
        job_queue.put(job)
        send_msg(conn, {"type": "QUEUED", "position": job_queue.qsize()})

        while True:
            try:
                m = job.outbox.get(timeout=HEARTBEAT_SECS)
            except queue.Empty:
                send_msg(conn, {"type": "HEARTBEAT"})   # also detects dead client
                continue
            if m["type"] == "DONE":
                ok = send_result(conn, job, m)
                log.info("job %s: result delivered=%s", job.id, ok)
                return
            send_msg(conn, m)
            if m["type"] == "ERROR":
                return
    except (OSError, ProtocolError) as e:
        log.warning("job %s: client lost (%s) -> cancelling", job.id, e)
    finally:
        job.cancelled.set()
        if job.proc and job.proc.poll() is None:
            job.proc.kill()
        shutil.rmtree(job.dir, ignore_errors=True)


def handle_client(conn, addr, cfg, caps, job_queue):
    log.info("client connected: %s:%d", *addr)
    conn.settimeout(IO_TIMEOUT)
    authed = False
    try:
        while True:
            msg = recv_msg(conn)
            kind = msg.get("type")
            if kind == "PING":
                send_msg(conn, {"type": "PONG", "t": msg.get("t")})
            elif kind == "HELLO":
                if msg.get("version") != PROTOCOL_VERSION:
                    send_msg(conn, {"type": "ERROR", "message": "protocol version mismatch"})
                    return
                if cfg.token and not hmac.compare_digest(str(msg.get("token", "")), cfg.token):
                    send_msg(conn, {"type": "ERROR", "message": "authentication failed"})
                    return
                authed = True
                send_msg(conn, {"type": "HELLO_ACK", "gpu": caps["gpu"],
                                "nvenc": caps["nvenc"], "queue_length": job_queue.qsize()})
            elif kind == "JOB":
                if not authed:
                    send_msg(conn, {"type": "ERROR", "message": "send HELLO first"})
                    return
                handle_job(conn, msg, cfg, caps, job_queue)
                return
            elif kind == "BYE":
                return
            else:
                send_msg(conn, {"type": "ERROR", "message": f"unknown message '{kind}'"})
    except (OSError, ProtocolError) as e:
        log.info("client %s:%d dropped: %s", addr[0], addr[1], e)
    finally:
        conn.close()


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Remote GPU worker daemon")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--token", default="", help="shared secret clients must send")
    ap.add_argument("--workdir", default=os.path.join(tempfile.gettempdir(), "gpu_offload_jobs"))
    ap.add_argument("--max-size-gb", type=float, default=20)
    ap.add_argument("--require-gpu", action="store_true",
                    help="fail jobs instead of falling back to CPU x264")
    cfg = ap.parse_args()
    cfg.max_size = int(cfg.max_size_gb * 1024 ** 3)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit("ffmpeg not found in PATH - install it first (see README)")
    os.makedirs(cfg.workdir, exist_ok=True)
    caps = {"ffmpeg": ffmpeg, "gpu": detect_gpu(), "nvenc": nvenc_works(ffmpeg)}
    log.info("GPU: %s | NVENC working: %s", caps["gpu"], caps["nvenc"])
    if not caps["nvenc"]:
        log.warning("NVENC unavailable -> %s",
                    "jobs will FAIL (--require-gpu)" if cfg.require_gpu
                    else "falling back to CPU libx264 (for testing only)")

    job_queue = queue.Queue()
    threading.Thread(target=worker_loop, args=(job_queue, caps, cfg), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((cfg.host, cfg.port))
    srv.listen(8)
    log.info("worker listening on %s:%d  (Ctrl+C to stop)", cfg.host, cfg.port)
    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            threading.Thread(target=handle_client,
                             args=(conn, addr, cfg, caps, job_queue),
                             daemon=True).start()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
