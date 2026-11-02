"""
Client-side networking engine (no GUI code in here, so the benchmark script
and the GUI both reuse it).

    c = OffloadClient("192.168.1.1", token="x", on_log=print, on_progress=cb)
    info = c.check_worker()                    # ping + handshake
    metrics = c.run_job(in_path, out_path, {"resolution": "720p", ...})
"""
import os
import socket
import time

from common import (PORT, PROTOCOL_VERSION, ProtocolError, recv_file, recv_msg,
                    send_file, send_msg, sha256_file)


class OffloadError(Exception):
    """A user-presentable failure."""


class _Retryable(Exception):
    pass


def explain(exc):
    """Turn low-level socket errors into helpful messages."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused - is server.py running and is the firewall port open?"
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "timed out - check cable / IP address / firewall"
    if isinstance(exc, socket.gaierror):
        return "invalid host name or IP address"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return "connection reset by the worker"
    return str(exc) or exc.__class__.__name__


class OffloadClient:
    def __init__(self, host, port=PORT, token="", on_log=print, on_progress=None,
                 connect_timeout=5, io_timeout=30, max_retries=3):
        self.host, self.port, self.token = host, int(port), token
        self.log = on_log
        self.progress = on_progress or (lambda *a: None)
        self.connect_timeout, self.io_timeout = connect_timeout, io_timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------ plumbing
    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        s.settimeout(self.io_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return s

    def _hello(self, sock):
        send_msg(sock, {"type": "HELLO", "version": PROTOCOL_VERSION, "token": self.token})
        r = recv_msg(sock)
        if r.get("type") == "ERROR":
            raise OffloadError(f"worker rejected handshake: {r.get('message')}")
        if r.get("type") != "HELLO_ACK":
            raise OffloadError(f"unexpected handshake reply: {r}")
        return r

    def _throttled(self, stage, label_fn):
        """Progress callback that fires at most ~10x per second."""
        t0, last = time.perf_counter(), [0.0]

        def cb(done, total):
            now = time.perf_counter()
            if done < total and now - last[0] < 0.1:
                return
            last[0] = now
            mbps = done / max(now - t0, 1e-6) / 1e6
            self.progress(stage, done / total * 100, f"{label_fn(done, total)}  ({mbps:.1f} MB/s)")
        return cb

    # --------------------------------------------------- handshake / health
    def check_worker(self, pings=5):
        """Latency check (N pings) + authenticated handshake. Returns a dict."""
        self.log(f"Connecting to {self.host}:{self.port} ...")
        try:
            s = self._connect()
        except OSError as e:
            raise OffloadError(f"Cannot reach worker: {explain(e)}")
        try:
            rtts = []
            for i in range(pings):
                t = time.perf_counter()
                send_msg(s, {"type": "PING", "t": t})
                r = recv_msg(s)
                if r.get("type") != "PONG":
                    raise OffloadError("worker answered ping incorrectly")
                rtts.append((time.perf_counter() - t) * 1000)
            hello = self._hello(s)
            send_msg(s, {"type": "BYE"})
        except (OSError, ProtocolError) as e:
            raise OffloadError(f"Handshake failed: {explain(e)}")
        finally:
            s.close()
        info = {"latency_avg_ms": sum(rtts) / len(rtts), "latency_min_ms": min(rtts),
                "latency_max_ms": max(rtts), "gpu": hello.get("gpu"),
                "nvenc": hello.get("nvenc"), "queue_length": hello.get("queue_length")}
        self.log(f"Worker OK | latency avg {info['latency_avg_ms']:.2f} ms "
                 f"(min {info['latency_min_ms']:.2f} / max {info['latency_max_ms']:.2f}) | "
                 f"GPU: {info['gpu']} | NVENC: {info['nvenc']} | queue: {info['queue_length']}")
        return info

    # -------------------------------------------------------------- the job
    def run_job(self, in_path, out_path, settings, cancel=None):
        def check_cancel():
            if cancel is not None and cancel.is_set():
                raise OffloadError("Cancelled by user")

        size = os.path.getsize(in_path)
        self.log(f"Computing SHA-256 of {os.path.basename(in_path)} ({size / 1e6:.1f} MB) ...")
        sha = sha256_file(in_path)
        t_total = time.perf_counter()
        sock = None

        # ---- upload, with automatic retry on network error / bad checksum
        for attempt in range(1, self.max_retries + 1):
            try:
                check_cancel()
                sock = self._connect()
                self._hello(sock)
                send_msg(sock, {"type": "JOB", "filename": os.path.basename(in_path),
                                "size": size, "sha256": sha, "settings": settings})
                r = recv_msg(sock)
                if r.get("type") == "ERROR":
                    raise OffloadError(r.get("message", "worker refused job"))
                if r.get("type") != "READY_FOR_UPLOAD":
                    raise _Retryable(f"unexpected reply {r.get('type')}")
                self.log(f"Uploading (attempt {attempt}/{self.max_retries}) ...")
                t_up = time.perf_counter()
                up_cb = self._throttled("upload", lambda d, t: f"Uploading {d / 1e6:.0f}/{t / 1e6:.0f} MB")

                def guarded_cb(d, t):
                    check_cancel()
                    up_cb(d, t)
                send_file(sock, in_path, size, guarded_cb)
                r = recv_msg(sock)
                if r.get("type") == "UPLOAD_OK":
                    upload_s = time.perf_counter() - t_up
                    self.log(f"Upload verified by worker (checksum OK) in {upload_s:.2f}s")
                    break
                raise _Retryable(f"worker says upload failed: {r.get('reason', r)}")
            except (OSError, ProtocolError, _Retryable) as e:
                if sock:
                    sock.close()
                    sock = None
                self.log(f"Upload problem: {explain(e)}")
                if attempt == self.max_retries:
                    raise OffloadError(f"Upload failed after {attempt} attempts: {explain(e)}")
                wait = 2 * attempt
                self.log(f"Retrying in {wait}s ...")
                time.sleep(wait)
            except OffloadError:
                if sock:
                    sock.close()
                raise

        # ---- wait for render, streaming progress
        t_wait = time.perf_counter()
        try:
            while True:
                check_cancel()
                try:
                    m = recv_msg(sock)
                except socket.timeout:
                    raise OffloadError(f"Worker silent for {self.io_timeout}s - "
                                       "connection considered dead")
                kind = m.get("type")
                if kind == "QUEUED":
                    self.log(f"Job queued on worker (jobs ahead: {max(m.get('position', 1) - 1, 0)})")
                elif kind == "STARTED":
                    self.log(f"Render started with encoder: {m.get('encoder')} on GPU: {m.get('gpu')}")
                elif kind == "PROGRESS":
                    pct = m.get("percent")
                    detail = f"Rendering  fps={m.get('fps')}  speed={m.get('speed')}"
                    self.progress("encode", pct if pct is not None else -1, detail)
                elif kind == "LOG":
                    self.log(f"[worker] {m.get('line')}")
                elif kind == "HEARTBEAT":
                    pass
                elif kind == "ERROR":
                    raise OffloadError(f"Worker error: {m.get('message')}")
                elif kind == "DONE":
                    break
                else:
                    self.log(f"(ignoring unknown message {kind})")
            render_wait_s = time.perf_counter() - t_wait

            # ---- download + integrity check (up to 3 tries)
            tmp = out_path + ".part"
            t_dl = time.perf_counter()
            dl_cb = self._throttled("download", lambda d, t: f"Downloading {d / 1e6:.0f}/{t / 1e6:.0f} MB")
            for attempt in range(1, 4):
                got = recv_file(sock, tmp, m["size"], dl_cb)
                if got == m["sha256"]:
                    send_msg(sock, {"type": "ACK"})
                    break
                self.log(f"Downloaded file is corrupt (attempt {attempt}/3) - requesting resend")
                send_msg(sock, {"type": "RESEND"})
                if attempt == 3:
                    raise OffloadError("Result file failed checksum 3 times")
                m = recv_msg(sock)
            os.replace(tmp, out_path)
            download_s = time.perf_counter() - t_dl
            self.log(f"Download verified (checksum OK) in {download_s:.2f}s -> {out_path}")
        except OffloadError:
            raise
        except socket.timeout:
            raise OffloadError("Timed out while receiving data from worker")
        except (OSError, ProtocolError) as e:
            raise OffloadError(f"Connection lost during render/download: {explain(e)}")
        finally:
            if sock:
                sock.close()

        total_s = time.perf_counter() - t_total
        stats = m.get("stats", {})
        encode_s = stats.get("encode_seconds", render_wait_s)
        return {"input_bytes": size, "output_bytes": m["size"], "upload_s": upload_s,
                "encode_s": encode_s, "download_s": download_s, "total_s": total_s,
                "overhead_s": total_s - encode_s, "server_stats": stats}
