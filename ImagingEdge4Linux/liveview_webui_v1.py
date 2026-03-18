#!/bin/python3

import argparse
import json
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests


class SonyCameraClient:
    def __init__(self, address: str, port: int):
        self.base = f"http://{address}:{port}/sony/camera"

    def call(self, method: str, params=None, version="1.0", timeout=8):
        if params is None:
            params = []

        response = requests.post(
            self.base,
            json={
                "method": method,
                "params": params,
                "id": 1,
                "version": version,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def has_error(self, result):
        return isinstance(result, dict) and "error" in result


class AppState:
    def __init__(self, camera: SonyCameraClient, wifi_interface: str = "WLAN 2", wifi_password: str | None = None):
        self.camera = camera
        self.lock = threading.RLock()
        self.liveview_url = None
        self.streaming_enabled = False
        self.movie_recording = False
        self.latest_frame = None
        self.frame_count = 0
        self.last_camera_error = None
        self.grabber_thread = None
        self.stop_grabber = threading.Event()
        self.wifi_interface = wifi_interface
        self.wifi_password = wifi_password
        self.last_wifi_check = 0.0
        self.wifi_check_interval_seconds = 3.0

    def _run(self, cmd: str):
        p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def _connected_ssid(self, iface: str):
        _, out = self._run(f'netsh wlan show interfaces interface="{iface}"')
        state_m = re.search(r"^\s*State\s*:\s*(.+)$", out, re.MULTILINE)
        ssid_m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
        state = (state_m.group(1).strip().lower() if state_m else "")
        ssid = (ssid_m.group(1).strip() if ssid_m else None)
        if state == "connected" and ssid and ssid.upper().startswith("DIRECT-"):
            return ssid
        return None

    def _find_camera_ssid(self, iface: str, max_wait=6):
        end = time.time() + max_wait
        while time.time() < end:
            _, txt = self._run(f'netsh wlan show networks mode=bssid interface="{iface}"')
            m = re.search(r"DIRECT-[^\r\n]*ILCE-6400", txt)
            if m:
                return m.group(0)
            time.sleep(1)
        return None

    def ensure_wifi_direct_connected(self):
        now = time.time()
        if now - self.last_wifi_check < self.wifi_check_interval_seconds:
            return True

        self.last_wifi_check = now

        # Never disconnect first. Keep existing Wi-Fi Direct if already up.
        connected = self._connected_ssid(self.wifi_interface)
        if connected:
            return True

        target_ssid = self._find_camera_ssid(self.wifi_interface)
        if not target_ssid:
            self.last_camera_error = f"Camera SSID not found on {self.wifi_interface}"
            return False

        if self.wifi_password:
            xml = f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{target_ssid}</name>
  <SSIDConfig><SSID><name>{target_ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption><sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{self.wifi_password}</keyMaterial></sharedKey></security></MSM>
</WLANProfile>
'''
            xml_path = "e:\\Co2Root\\sony-wlan-profile.xml"
            with open(xml_path, "w", encoding="ascii") as f:
                f.write(xml)
            self._run(f'netsh wlan add profile filename="{xml_path}" interface="{self.wifi_interface}" user=current')

        _, connect_out = self._run(
            f'netsh wlan connect name="{target_ssid}" ssid="{target_ssid}" interface="{self.wifi_interface}"'
        )
        time.sleep(2)
        connected_after = self._connected_ssid(self.wifi_interface)
        if connected_after:
            self.last_camera_error = None
            return True

        self.last_camera_error = f"Wi-Fi Direct connect failed on {self.wifi_interface}: {connect_out.strip()}"
        return False

    def _ensure_grabber_thread(self):
        if self.grabber_thread and self.grabber_thread.is_alive():
            return

        self.stop_grabber.clear()
        self.grabber_thread = threading.Thread(target=self._grabber_loop, daemon=True)
        self.grabber_thread.start()

    def _grabber_loop(self):
        while not self.stop_grabber.is_set():
            with self.lock:
                enabled = self.streaming_enabled
                url = self.liveview_url

            if not enabled or not url:
                self.stop_grabber.wait(0.2)
                continue

            try:
                with requests.get(url, stream=True, timeout=(8, 20)) as resp:
                    resp.raise_for_status()
                    for frame in extract_jpeg_frames(resp.iter_content(chunk_size=32768)):
                        if self.stop_grabber.is_set():
                            break

                        with self.lock:
                            if not self.streaming_enabled:
                                break

                            self.latest_frame = frame
                            self.frame_count += 1
                            self.last_camera_error = None
            except Exception:
                with self.lock:
                    self.last_camera_error = "Liveview stream unavailable"
                self.stop_grabber.wait(0.5)

    def start_liveview(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                self.streaming_enabled = False
                return False, {"error": [10010, self.last_camera_error or "Camera Wi-Fi not connected"]}

            try:
                # harmless if camera is already in recording mode or does not need it
                self.camera.call("startRecMode", timeout=2)
            except Exception:
                pass

            try:
                result = self.camera.call("startLiveview", timeout=4)
            except Exception as exc:
                self.last_camera_error = str(exc)
                self.streaming_enabled = False
                return False, {"error": [10000, str(exc)]}

            if self.camera.has_error(result):
                self.last_camera_error = str(result.get("error"))
                return False, result

            self.liveview_url = result.get("result", [None])[0]
            self.streaming_enabled = True
            self.last_camera_error = None
            self._ensure_grabber_thread()
            return bool(self.liveview_url), result

    def stop_liveview(self):
        with self.lock:
            self.ensure_wifi_direct_connected()
            try:
                result = self.camera.call("stopLiveview", timeout=4)
            except Exception as exc:
                self.last_camera_error = str(exc)
                self.streaming_enabled = False
                return False, {"error": [10001, str(exc)]}

            if self.camera.has_error(result):
                self.last_camera_error = str(result.get("error"))
                return False, result

            self.streaming_enabled = False
            self.latest_frame = None
            self.last_camera_error = None
            return True, result

    def start_movie_rec(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10011, self.last_camera_error or "Camera Wi-Fi not connected"]}

            try:
                result = self.camera.call("startMovieRec", timeout=4)
            except Exception as exc:
                self.last_camera_error = str(exc)
                return False, {"error": [10002, str(exc)]}

            if self.camera.has_error(result):
                self.last_camera_error = str(result.get("error"))
                return False, result

            self.movie_recording = True
            self.last_camera_error = None
            return True, result

    def stop_movie_rec(self):
        with self.lock:
            self.ensure_wifi_direct_connected()
            try:
                result = self.camera.call("stopMovieRec", timeout=4)
            except Exception as exc:
                self.last_camera_error = str(exc)
                return False, {"error": [10003, str(exc)]}

            if self.camera.has_error(result):
                self.last_camera_error = str(result.get("error"))
                return False, result

            self.movie_recording = False
            self.last_camera_error = None
            return True, result

    def available_api_list(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return {"error": [10012, self.last_camera_error or "Camera Wi-Fi not connected"]}
            return self.camera.call("getAvailableApiList")

    def health(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return {
                    "ok": False,
                    "error": self.last_camera_error or "Camera Wi-Fi not connected",
                    "liveviewUrl": self.liveview_url,
                    "streamingEnabled": self.streaming_enabled,
                    "frameCount": self.frame_count,
                }
            try:
                versions = self.camera.call("getVersions", timeout=2)
                apis = self.camera.call("getAvailableApiList", timeout=2)
                self.last_camera_error = None
            except Exception as exc:
                self.last_camera_error = str(exc)
                return {
                    "ok": False,
                    "error": str(exc),
                    "liveviewUrl": self.liveview_url,
                    "streamingEnabled": self.streaming_enabled,
                    "frameCount": self.frame_count,
                }
            return {
                "ok": True,
                "versions": versions,
                "apis": apis,
                "liveviewUrl": self.liveview_url,
                "streamingEnabled": self.streaming_enabled,
                "frameCount": self.frame_count,
            }


def extract_jpeg_frames(raw_iter, chunk_size=32768):
    """
    Convert Sony liveview stream payload into plain JPEG frames by scanning for
    JPEG start/end markers.
    """
    buffer = bytearray()

    for chunk in raw_iter:
        if not chunk:
            continue

        buffer.extend(chunk)

        while True:
            soi = buffer.find(b"\xff\xd8")
            if soi == -1:
                # keep buffer bounded
                if len(buffer) > 4 * 1024 * 1024:
                    del buffer[:-1024]
                break

            eoi = buffer.find(b"\xff\xd9", soi + 2)
            if eoi == -1:
                # keep bytes from SOI onward while waiting for frame end
                if soi > 0:
                    del buffer[:soi]
                break

            frame = bytes(buffer[soi : eoi + 2])
            del buffer[: eoi + 2]
            yield frame


class LiveviewHandler(BaseHTTPRequestHandler):
    server_version = "SonyLiveviewWebUI/1.0"

    @property
    def state(self) -> AppState:
        return self.server.state

    def _send_json(self, payload, code=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ["/", "/debug"]:
            self._send_html(INDEX_HTML)
            return

        if parsed.path == "/api/status":
            with self.state.lock:
                self._send_json(
                    {
                        "liveviewUrl": self.state.liveview_url,
                        "streamingEnabled": self.state.streaming_enabled,
                        "movieRecording": self.state.movie_recording,
                        "frameCount": self.state.frame_count,
                        "lastCameraError": self.state.last_camera_error,
                    }
                )
            return

        if parsed.path == "/api/health":
            try:
                health = self.state.health()
                ok = bool(health.get("ok", True))
                self._send_json({"ok": ok, "result": health}, HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if parsed.path == "/frame.jpg":
            self._send_latest_frame()
            return

        if parsed.path == "/stream":
            self._stream_liveview()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/start_liveview":
                ok, result = self.state.start_liveview()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/stop_liveview":
                ok, result = self.state.stop_liveview()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/start_movie":
                ok, result = self.state.start_movie_rec()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/stop_movie":
                ok, result = self.state.stop_movie_rec()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/apis":
                result = self.state.available_api_list()
                self._send_json({"ok": True, "result": result})
                return

            # Frigate ONVIF probe can hit this path when "Probe camera" is selected.
            if parsed.path.endswith("/onvif/device_service"):
                self._send_json(
                    {
                        "ok": False,
                        "error": "ONVIF not supported by this Sony bridge. Use manual stream URL: http://<bridge-ip>:8770/stream",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _stream_liveview(self):
        try:
            with self.state.lock:
                need_start = (not self.state.liveview_url) or (not self.state.streaming_enabled)

            if need_start:
                ok, result = self.state.start_liveview()
                if not ok:
                    self._send_json({"ok": False, "error": "Could not start liveview", "result": result}, HTTPStatus.SERVICE_UNAVAILABLE)
                    return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        try:
            # Stream cached frames from the background grabber to avoid opening
            # a second direct Sony liveview stream per client.
            last_sent = None
            while True:
                with self.state.lock:
                    if not self.state.streaming_enabled:
                        break
                    frame = self.state.latest_frame

                if not frame:
                    time.sleep(0.05)
                    continue

                if frame is last_sent:
                    time.sleep(0.02)
                    continue

                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                last_sent = frame
        except (BrokenPipeError, ConnectionResetError):
            # browser disconnected
            pass
        except Exception:
            pass

    def _send_latest_frame(self):
        with self.state.lock:
            frame = self.state.latest_frame
            enabled = self.state.streaming_enabled

        if not frame and not enabled:
            self.state.start_liveview()

        if not frame:
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"No frame yet")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)


INDEX_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Sony Liveview Debug Panel</title>
  <style>
        body { font-family: Segoe UI, Arial, sans-serif; margin: 16px; background: #111; color: #f1f1f1; }
        .toolbar { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
        .panel { background: #181818; border: 1px solid #2a2a2a; border-radius: 10px; padding: 10px; margin-bottom: 12px; }
        .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        label { color: #b8b8b8; font-size: 13px; }
        input, select { background: #202020; color: #f1f1f1; border: 1px solid #333; border-radius: 6px; padding: 6px 8px; }
        button { padding: 9px 13px; border: 0; border-radius: 8px; cursor: pointer; }
    button.primary { background: #1f6feb; color: white; }
    button.warn { background: #f59e0b; color: #111; }
    button.danger { background: #ef4444; color: white; }
        #status { margin: 6px 0; color: #b4b4b4; }
        #diag { margin: 4px 0 8px; color: #9ac7ff; font-size: 13px; }
        #streamWrap { background: #000; border: 1px solid #333; border-radius: 10px; overflow: hidden; display: inline-block; }
        #stream { width: min(100%, 1280px); display: block; background: #000; }
    code { color: #8bd3ff; }
        .muted { color: #8f8f8f; font-size: 12px; }
  </style>
</head>
<body>
    <h2>Sony Liveview Debug Panel</h2>

    <div class=\"panel\">
        <div class=\"toolbar\">
            <button class=\"primary\" onclick=\"startLiveview()\">Start Liveview</button>
            <button onclick=\"stopLiveview()\">Stop Liveview</button>
            <button class=\"warn\" onclick=\"startMovie()\">Start Recording</button>
            <button class=\"danger\" onclick=\"stopMovie()\">Stop Recording</button>
            <button onclick=\"refreshStatus()\">Refresh Status</button>
        </div>

        <div class=\"row\">
            <label>Preview Mode
                <select id=\"mode\">
                    <option value=\"mjpeg\" selected>MJPEG continuous (/stream, best preview quality)</option>
                    <option value=\"poll\">JPEG polling (/frame.jpg)</option>
                </select>
            </label>

            <label>Poll interval (ms)
                <input id=\"pollMs\" type=\"number\" min=\"60\" max=\"2000\" step=\"10\" value=\"150\" />
            </label>

            <label>Display width
                <select id=\"displayWidth\">
                    <option value=\"100\" selected>Auto (100%)</option>
                    <option value=\"75\">75%</option>
                    <option value=\"50\">50%</option>
                </select>
            </label>

            <button onclick=\"applyPreviewSettings()\">Apply Preview Settings</button>
            <button onclick=\"takeSnapshot()\">Snapshot</button>
        </div>
        <div class=\"muted\">Tip: For quality comparison, use MJPEG continuous mode first.</div>
  </div>

  <div id=\"status\">Status: initializing...</div>
    <div id=\"diag\">Diagnostics: waiting...</div>
    <div id=\"streamWrap\"><img id=\"stream\" alt=\"Live stream\" /></div>

  <script>
    const statusEl = document.getElementById('status');
        const diagEl = document.getElementById('diag');
    const streamEl = document.getElementById('stream');
        const modeEl = document.getElementById('mode');
        const pollMsEl = document.getElementById('pollMs');
        const displayWidthEl = document.getElementById('displayWidth');

        let pollTimer = null;
        let statusTimer = null;
        let lastFrameCount = 0;
        let lastStatusTs = Date.now();

    async function post(url) {
      const res = await fetch(url, { method: 'POST' });
      return await res.json();
    }

    function setStatus(text) {
      statusEl.textContent = 'Status: ' + text;
    }

        async function refreshStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    setStatus('status endpoint not ready: HTTP ' + res.status);
                    return;
                }
                const s = await res.json();

                const now = Date.now();
                const dt = Math.max((now - lastStatusTs) / 1000, 0.001);
                const fps = ((s.frameCount - lastFrameCount) / dt).toFixed(1);
                lastStatusTs = now;
                lastFrameCount = s.frameCount;

                setStatus(`streaming=${s.streamingEnabled}, recording=${s.movieRecording}, frames=${s.frameCount}, mode=${modeEl.value}, liveviewUrl=${s.liveviewUrl || 'n/a'}`);
                diagEl.textContent = `Diagnostics: backend_fps≈${fps}, lastCameraError=${s.lastCameraError || 'none'}`;
            } catch (e) {
                setStatus('cannot reach backend: ' + e);
            }
        }

        function startPollingFrames() {
            stopPollingFrames();
            const ms = Math.max(60, Number(pollMsEl.value || 150));
            pollTimer = setInterval(() => {
                streamEl.src = '/frame.jpg?t=' + Date.now();
            }, ms);
        }

        function stopPollingFrames() {
            if (pollTimer) {
                clearInterval(pollTimer);
                pollTimer = null;
            }
        }

        function startMjpegPreview() {
            stopPollingFrames();
            streamEl.src = '/stream?t=' + Date.now();
        }

        function stopPreview() {
            stopPollingFrames();
            streamEl.src = '';
        }

        function applyPreviewSettings() {
            const pct = Number(displayWidthEl.value || 100);
            streamEl.style.width = pct + '%';

            if (modeEl.value === 'poll') {
                startPollingFrames();
            } else {
                startMjpegPreview();
            }
        }

        function takeSnapshot() {
            const a = document.createElement('a');
            a.href = '/frame.jpg?t=' + Date.now();
            a.download = 'sony-liveview-snapshot.jpg';
            a.click();
    }

    async function startLiveview() {
      const r = await post('/api/start_liveview');
      if (r.ok) {
                applyPreviewSettings();
        setStatus('liveview started');
      } else {
        setStatus('failed to start liveview: ' + JSON.stringify(r.result || r.error));
      }
      await refreshStatus();
    }

    async function stopLiveview() {
      await post('/api/stop_liveview');
            stopPreview();
      setStatus('liveview stopped');
      await refreshStatus();
    }

    async function startMovie() {
      const r = await post('/api/start_movie');
      setStatus(r.ok ? 'camera recording started' : ('start recording failed: ' + JSON.stringify(r.result || r.error)));
      await refreshStatus();
    }

    async function stopMovie() {
      const r = await post('/api/stop_movie');
      setStatus(r.ok ? 'camera recording stopped' : ('stop recording failed: ' + JSON.stringify(r.result || r.error)));
      await refreshStatus();
    }

        refreshStatus();
        statusTimer = setInterval(refreshStatus, 2000);
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Watch Sony camera liveview in browser with recording controls")
    parser.add_argument("--address", default="192.168.122.1", help="Camera IP address")
    parser.add_argument("--camera-port", type=int, default=10000, help="Sony JSON API port")
    parser.add_argument("--wifi-interface", default="WLAN 2", help="Wi-Fi adapter used for camera Wi-Fi Direct")
    parser.add_argument("--wifi-password", default=None, help="Camera Wi-Fi Direct password (optional, for auto-connect)")
    parser.add_argument("--listen", default="127.0.0.1", help="Web UI bind address")
    parser.add_argument("--port", type=int, default=8765, help="Web UI port")
    args = parser.parse_args()

    camera = SonyCameraClient(args.address, args.camera_port)
    state = AppState(camera, wifi_interface=args.wifi_interface, wifi_password=args.wifi_password)

    httpd = ThreadingHTTPServer((args.listen, args.port), LiveviewHandler)
    httpd.state = state

    print(f"Web UI: http://{args.listen}:{args.port}")
    print(f"Camera API: http://{args.address}:{args.camera_port}/sony/camera")
    print("Put camera in Ctrl w/ Smartphone mode, then open the Web UI.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
