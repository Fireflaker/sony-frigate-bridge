#!/usr/bin/env python3
"""
Sony Camera Liveview Bridge for Frigate Integration

Key improvement: Accepts direct camera control port reachability as proof
of valid connection, avoiding flaky SSID detection in NetworkManager.
"""

import socket
import logging
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import requests
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CameraControl:
    """Sony camera JSON-RPC control interface."""
    
    def __init__(self, address: str, port: int):
        self.address = address
        self.port = port
        self.base_url = f"http://{address}:{port}/sony/camera"
    
    def call_method(self, method: str, params: list = None):
        """Call Sony JSON-RPC method."""
        payload = {
            "method": method,
            "params": params or [],
            "id": 1,
            "version": "1.0"
        }
        try:
            resp = requests.post(self.base_url, json=payload, timeout=5)
            result = resp.json()
            if "error" in result:
                raise RuntimeError(f"Sony API error: {result['error']}")
            return result.get("result", [])
        except Exception as e:
            logger.error(f"Failed to call {method}: {e}")
            raise
    
    def start_liveview(self):
        """Start camera liveview and return stream URL."""
        result = self.call_method("startLiveview", [{"liveviewSize": "L"}])
        if result:
            return result[0]
        raise RuntimeError("Failed to start liveview")


class SonyBridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler for Sony bridge endpoints."""
    
    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/api/status":
            self.send_status_json()
        elif self.path == "/frame.jpg":
            self.send_frame()
        elif self.path == "/stream":
            self.send_stream()
        else:
            self.send_error(404)
    
    def do_POST(self):
        """Handle POST requests."""
        if self.path == "/api/start_liveview":
            self.start_liveview()
        elif self.path == "/api/stop_liveview":
            self.stop_liveview()
        else:
            self.send_error(404)
    
    def send_status_json(self):
        """Send bridge status as JSON."""
        status = {
            "ok": True,
            "streamingEnabled": hasattr(self.server, 'stream_url') and self.server.stream_url is not None,
            "liveviewUrl": getattr(self.server, 'stream_url', None),
            "frameCount": getattr(self.server, 'frame_count', 0),
            "lastCameraError": getattr(self.server, 'last_error', None),
        }
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())
    
    def send_frame(self):
        """Send current frame as JPEG."""
        if not hasattr(self.server, 'current_frame') or self.server.current_frame is None:
            self.send_error(503, "No frame available")
            return
        
        self.send_response(200)
        self.send_header("Content-type", "image/jpeg")
        self.send_header("Content-length", len(self.server.current_frame))
        self.end_headers()
        self.wfile.write(self.server.current_frame)
    
    def send_stream(self):
        """Proxy camera stream to client."""
        if not hasattr(self.server, 'stream_url') or self.server.stream_url is None:
            self.send_error(503, "Stream not started")
            return
        
        try:
            resp = requests.get(self.server.stream_url, stream=True, timeout=30)
            self.send_response(200)
            self.send_header("Content-type", resp.headers.get("Content-type", "video/mp2t"))
            self.end_headers()
            
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
        except Exception as e:
            logger.error(f"Stream error: {e}")
            self.send_error(500)
    
    def start_liveview(self):
        """Start camera liveview stream."""
        try:
            self.server.stream_url = self.server.camera.start_liveview()
            self.server.last_error = None
            response = {"ok": True, "result": [{"id": 1, "result": [self.server.stream_url]}]}
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            self.server.last_error = str(e)
            logger.error(f"Start liveview failed: {e}")
            self.send_error(500, str(e))
    
    def stop_liveview(self):
        """Stop camera liveview stream."""
        self.server.stream_url = None
        response = {"ok": True}
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())
    
    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


class SonyBridgeServer:
    """Main bridge server."""
    
    def __init__(self, camera_addr: str, camera_port: int, wifi_iface: str, 
                 listen_addr: str = "0.0.0.0", listen_port: int = 8765):
        self.camera = CameraControl(camera_addr, camera_port)
        self.wifi_iface = wifi_iface
        self.listen_addr = listen_addr
        self.listen_port = listen_port
        self.server = None
    
    def _camera_control_port_reachable(self, timeout: float = 2.5) -> bool:
        """Check if camera control port is reachable via TCP."""
        try:
            with socket.create_connection(
                (self.camera.address, self.camera.port),
                timeout=timeout
            ):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False
    
    def ensure_wifi_connected(self):
        """Ensure Wi-Fi connection to camera."""
        logger.info(f"Checking camera reachability at {self.camera.address}:{self.camera.port}")
        
        if self._camera_control_port_reachable():
            logger.info("✓ Camera control port is reachable")
            return True
        
        logger.error("✗ Camera control port not reachable")
        return False
    
    def run(self):
        """Start the bridge server."""
        logger.info(f"Starting Sony bridge on {self.listen_addr}:{self.listen_port}")
        
        if not self.ensure_wifi_connected():
            logger.error("Cannot reach camera, exiting")
            return
        
        self.server = HTTPServer((self.listen_addr, self.listen_port), SonyBridgeHandler)
        self.server.camera = self.camera
        self.server.stream_url = None
        self.server.frame_count = 0
        self.server.last_error = None
        self.server.current_frame = None
        
        logger.info("Bridge ready. Endpoints:")
        logger.info(f"  /api/status       - Bridge status (JSON)")
        logger.info(f"  /api/start_liveview - Start camera stream (POST)")
        logger.info(f"  /api/stop_liveview  - Stop camera stream (POST)")
        logger.info(f"  /stream           - Camera liveview stream")
        logger.info(f"  /frame.jpg        - Current JPEG frame")
        
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            self.server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Sony Camera Liveview Bridge")
    parser.add_argument("--address", default="192.168.122.1", help="Camera address")
    parser.add_argument("--camera-port", type=int, default=10000, help="Camera JSON-RPC port")
    parser.add_argument("--wifi-interface", default="wlan0", help="Wi-Fi interface name")
    parser.add_argument("--listen", default="0.0.0.0", help="Listen address")
    parser.add_argument("--port", type=int, default=8765, help="Listen port")
    
    args = parser.parse_args()
    
    bridge = SonyBridgeServer(
        camera_addr=args.address,
        camera_port=args.camera_port,
        wifi_iface=args.wifi_interface,
        listen_addr=args.listen,
        listen_port=args.port
    )
    bridge.run()


if __name__ == "__main__":
    main()
