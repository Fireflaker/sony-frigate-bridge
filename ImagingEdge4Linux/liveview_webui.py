#!/bin/python3

import argparse
import json
import os
import re
import socket
import tempfile
import subprocess
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from xml.dom import minidom

import requests


class SonyCameraClient:
    def __init__(self, address: str, port: int):
        self.address = address
        self.port = port
        self.base = f"http://{address}:{port}/sony/camera"

    def call_service(self, service: str, method: str, params=None, version="1.0", timeout=8):
        if params is None:
            params = []

        base = f"http://{self.address}:{self.port}/sony/{service}"
        response = requests.post(
            base,
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
    def __init__(self, camera: SonyCameraClient, wifi_interface: str = "auto", wifi_password: str | None = None):
        self.camera = camera
        self.lock = threading.RLock()
        self.liveview_url = None
        self.streaming_enabled = False
        self.movie_recording = False
        self.latest_frame = None
        self.latest_frame_ts = None
        self.frame_count = 0
        self.last_camera_error = None
        self.grabber_thread = None
        self.stop_grabber = threading.Event()
        self.stills_thread = None
        self.stop_stills = threading.Event()
        self.wifi_interface = wifi_interface
        self.wifi_password = wifi_password
        self.last_wifi_check = 0.0
        self.wifi_check_interval_seconds = 3.0
        self.stills_enabled = False
        self.stills_interval_ms = 500
        self.stills_frame_count = 0
        self.source_mode = "liveview"
        self.exposure_mode = ""
        self.movie_quality = ""
        self.movie_file_format = ""
        self.battery_level = None
        self.camera_status = ""
        self.focus_status = ""
        self.latest_image_jpeg = None
        self.latest_image_url = None
        self.latest_image_ts = None
        self.soap_port = 64321
        self.transfer_active = False
        self.transfer_last_error = None
        self.transfer_items = []
        self.transfer_bundle_path = None
        self.transfer_bundle_name = None
        self.transfer_bundle_ts = None
        self.transfer_bundle_building = False
        self.transfer_bundle_thread = None
        self.platform_is_windows = os.name == "nt"

    def _has_recent_camera_activity(self, now: float | None = None, max_age_seconds: float = 8.0):
        current = time.time() if now is None else now
        for ts in (self.latest_frame_ts, self.latest_image_ts):
            if ts and (current - float(ts) <= max_age_seconds):
                return True
        return False

    def _format_transfer_mode_hint(self, raw_error: str, operation: str):
        text = str(raw_error or "").strip()
        low = text.lower()
        if "404" in low and ("xpushlist" in low or "contentdirectory" in low or "/upnp/control/" in low):
            return {
                "error": [
                    10046,
                    (
                        f"{operation} unavailable: camera transfer SOAP service returned 404. "
                        "On camera, open Menu -> Network -> Send to Smartphone, keep that screen active, "
                        "then retry transfer/list."
                    ),
                ],
                "detail": text,
            }
        return {"error": [10047, f"{operation} failed: {text or 'unknown error'}"]}

    def _run(self, cmd: str, timeout: int = 8):
        try:
            p = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, f"Command timed out after {timeout}s: {cmd}"

    def _safe_filename(self, text: str):
        s = re.sub(r'[\\/:*?"<>|]+', '_', str(text or '').strip())
        return s or 'unnamed'

    def _normalize_ssid(self, ssid: str):
        # nmcli -t escapes separators like ':' as '\:'.
        # Convert escaped separators back to literal SSID characters.
        return re.sub(r'\\([:\\\\])', r'\1', str(ssid or ''))

    def _cleanup_transfer_bundle(self):
        if self.transfer_bundle_path and os.path.isfile(self.transfer_bundle_path):
            try:
                os.remove(self.transfer_bundle_path)
            except OSError:
                pass
        self.transfer_bundle_path = None
        self.transfer_bundle_name = None
        self.transfer_bundle_ts = None
        self.transfer_bundle_building = False
        self.transfer_bundle_thread = None

    def _extract_camera_ssids(self, text: str):
        matches = []
        seen = set()
        for line in (text or "").splitlines():
            for match in re.finditer(r"DIRECT-[^\r\n]+", line, re.IGNORECASE):
                ssid = self._normalize_ssid(match.group(0).strip().strip('"'))
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    matches.append(ssid)
        return matches

    def _resolve_wifi_interface(self):
        iface = (self.wifi_interface or "").strip()
        if iface and iface.lower() != "auto":
            return iface

        if self.platform_is_windows:
            _, out = self._run("netsh wlan show interfaces")
            name_m = re.search(r"^\s*Name\s*:\s*(.+)$", out, re.MULTILINE)
            return name_m.group(1).strip() if name_m else None

        rc, out = self._run("nmcli -t -f DEVICE,TYPE device status")
        if rc != 0:
            return None

        for line in out.splitlines():
            parts = line.strip().split(":")
            if len(parts) >= 2 and parts[0] and parts[1] == "wifi":
                return parts[0]
        return None

    def _connected_ssid_windows(self, iface: str):
        _, out = self._run(f'netsh wlan show interfaces interface="{iface}"')
        state_m = re.search(r"^\s*State\s*:\s*(.+)$", out, re.MULTILINE)
        ssid_m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
        state = (state_m.group(1).strip().lower() if state_m else "")
        ssid = (ssid_m.group(1).strip() if ssid_m else None)
        if state == "connected" and ssid and ssid.upper().startswith("DIRECT-"):
            return ssid
        return None

    def _connected_ssid_linux(self, iface: str):
        rc, out = self._run(f'nmcli -t -g GENERAL.STATE,GENERAL.CONNECTION device show "{iface}"')
        if rc != 0:
            return None

        state = ""
        connection = None
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) >= 1:
            state = lines[0]
        if len(lines) >= 2:
            connection = lines[1]
        if state.startswith("100") and connection and connection.upper().startswith("DIRECT-"):
            return connection
        return None

    def _connected_ssid(self, iface: str):
        if self.platform_is_windows:
            return self._connected_ssid_windows(iface)
        return self._connected_ssid_linux(iface)

    def _camera_control_port_reachable(self, timeout: float = 2.5):
        try:
            with socket.create_connection((self.camera.address, self.camera.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _find_camera_ssid_windows(self, iface: str, max_wait=6):
        end = time.time() + max_wait
        while time.time() < end:
            _, txt = self._run(f'netsh wlan show networks mode=bssid interface="{iface}"')
            matches = self._extract_camera_ssids(txt)
            if matches:
                return matches[0]
            time.sleep(1)
        return None

    def _find_camera_ssid_linux(self, iface: str, max_wait=6):
        end = time.time() + max_wait
        while time.time() < end:
            rc, txt = self._run(f'nmcli -t -f SSID device wifi list ifname "{iface}" --rescan yes', timeout=15)
            if rc == 0:
                matches = self._extract_camera_ssids(txt)
                if matches:
                    return matches[0]
            time.sleep(1)
        return None

    def _find_camera_ssid(self, iface: str, max_wait=6):
        if self.platform_is_windows:
            return self._find_camera_ssid_windows(iface, max_wait=max_wait)
        return self._find_camera_ssid_linux(iface, max_wait=max_wait)

    def _connect_windows(self, iface: str, target_ssid: str):
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
            xml_path = os.path.join(tempfile.gettempdir(), "sony-wlan-profile.xml")
            with open(xml_path, "w", encoding="ascii") as f:
                f.write(xml)
            self._run(f'netsh wlan add profile filename="{xml_path}" interface="{iface}" user=current')

        return self._run(
            f'netsh wlan connect name="{target_ssid}" ssid="{target_ssid}" interface="{iface}"'
        )

    def _connect_linux(self, iface: str, target_ssid: str):
        self._run("nmcli radio wifi on")
        password_arg = f' password "{self.wifi_password}"' if self.wifi_password else ""
        return self._run(
            f'nmcli device wifi connect "{target_ssid}" ifname "{iface}"{password_arg}',
            timeout=20,
        )

    def _connect_to_camera_ssid(self, iface: str, target_ssid: str):
        if self.platform_is_windows:
            return self._connect_windows(iface, target_ssid)
        return self._connect_linux(iface, target_ssid)

    def ensure_wifi_direct_connected(self):
        now = time.time()
        if now - self.last_wifi_check < self.wifi_check_interval_seconds:
            return True

        self.last_wifi_check = now

        iface = self._resolve_wifi_interface()
        if not iface:
            self.last_camera_error = "No usable Wi-Fi interface found"
            return False

        # Never disconnect first. Keep existing Wi-Fi Direct if already up.
        connected = self._connected_ssid(iface)
        if connected:
            self.last_camera_error = None
            return True

        if self._camera_control_port_reachable():
            self.last_camera_error = None
            return True

        # If preview frames are still arriving, allow control calls to continue.
        # On some Windows adapter states, netsh SSID reporting can lag briefly.
        if self._has_recent_camera_activity(now=now):
            self.last_camera_error = None
            return True

        target_ssid = self._find_camera_ssid(iface)
        if not target_ssid:
            self.last_camera_error = f"Camera SSID not found on {iface}"
            return False

        _, connect_out = self._connect_to_camera_ssid(iface, target_ssid)

        # Allow association to settle; "request completed successfully" can still
        # need a few extra seconds before netsh reports connected state.
        connected_after = None
        for _ in range(8):
            time.sleep(1)
            connected_after = self._connected_ssid(iface)
            if connected_after:
                self.last_camera_error = None
                return True

        # Secondary fallback: if frames are still flowing, do not hard-fail control.
        if self._has_recent_camera_activity():
            self.last_camera_error = None
            return True

        self.last_camera_error = f"Wi-Fi Direct connect failed on {iface}: {connect_out.strip()}"
        return False

    def _ensure_grabber_thread(self):
        if self.grabber_thread and self.grabber_thread.is_alive():
            return

        self.stop_grabber.clear()
        self.grabber_thread = threading.Thread(target=self._grabber_loop, daemon=True)
        self.grabber_thread.start()

    def _ensure_stills_thread(self):
        if self.stills_thread and self.stills_thread.is_alive():
            return

        self.stop_stills.clear()
        self.stills_thread = threading.Thread(target=self._stills_loop, daemon=True)
        self.stills_thread.start()

    def _find_first_http_url(self, node):
        if isinstance(node, str):
            s = node.strip()
            if s.startswith("http://") or s.startswith("https://"):
                return s
            return None

        if isinstance(node, (list, tuple)):
            for item in node:
                url = self._find_first_http_url(item)
                if url:
                    return url

        if isinstance(node, dict):
            for value in node.values():
                url = self._find_first_http_url(value)
                if url:
                    return url

        return None

    def _collect_http_image_urls(self, node):
        found = []

        def walk(x):
            if isinstance(x, str):
                s = x.strip()
                if s.startswith("http://") or s.startswith("https://"):
                    low = s.lower()
                    if ("postview" in low) or ("image" in low) or low.endswith(".jpg") or low.endswith(".jpeg"):
                        found.append(s)
                return
            if isinstance(x, dict):
                for v in x.values():
                    walk(v)
                return
            if isinstance(x, list):
                for v in x:
                    walk(v)

        walk(node)
        # preserve order, unique
        uniq = []
        seen = set()
        for u in found:
            if u not in seen:
                uniq.append(u)
                seen.add(u)
        return uniq

    def _download_image_bytes(self, url: str):
        with requests.get(url, timeout=(8, 20)) as resp:
            resp.raise_for_status()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            data = resp.content
            if "image/jpeg" in ctype or data.startswith(b"\xff\xd8"):
                return data
            raise RuntimeError(f"URL did not return JPEG: {ctype}")

    def _call_avcontent(self, method: str, params=None, timeout=6):
        return self.camera.call_service("avContent", method, params=params or [], timeout=timeout)

    def _extract_sources(self, node):
        sources = []

        def walk(x):
            if isinstance(x, dict):
                if "source" in x and isinstance(x["source"], str):
                    sources.append(x["source"])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(node)
        uniq = []
        seen = set()
        for s in sources:
            if s not in seen:
                uniq.append(s)
                seen.add(s)
        return uniq

    def _try_avcontent_latest_urls(self):
        urls = []
        schemes = ["storage"]
        try:
            r = self._call_avcontent("getSchemeList", [], timeout=4)
            if not self.camera.has_error(r):
                found = self._collect_http_image_urls(r.get("result", []))
                if found:
                    urls.extend(found)
        except Exception:
            pass

        sources = []
        for sc in schemes:
            for p in ([{"scheme": sc}], [sc]):
                try:
                    r = self._call_avcontent("getSourceList", p, timeout=4)
                    if self.camera.has_error(r):
                        continue
                    sources.extend(self._extract_sources(r.get("result", [])))
                except Exception:
                    pass

        if not sources:
            sources = ["storage:memoryCard1", "storage:memoryCard1?path=DCIM"]

        content_param_variants = [
            lambda src: [{"uri": src, "stIdx": 0, "cnt": 1, "view": "date", "sort": "descending", "type": ["still"]}],
            lambda src: [{"uri": src, "stIdx": 0, "cnt": 1}],
            lambda src: [{"uri": src}],
        ]

        for src in sources:
            for mk in content_param_variants:
                try:
                    r = self._call_avcontent("getContentList", mk(src), timeout=6)
                    if self.camera.has_error(r):
                        continue
                    urls.extend(self._collect_http_image_urls(r.get("result", [])))
                    if urls:
                        return urls
                except Exception:
                    pass

        return urls

    def _soap_browse(self, object_id: str, starting_index: int = 0, requested_count: int = 200):
        soap = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            f'<ObjectID>{object_id}</ObjectID>'
            '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
            '<Filter>*</Filter>'
            f'<StartingIndex>{starting_index}</StartingIndex>'
            f'<RequestedCount>{requested_count}</RequestedCount>'
            '<SortCriteria></SortCriteria>'
            '</u:Browse>'
            '</s:Body>'
            '</s:Envelope>'
        )

        r = requests.post(
            f"http://{self.camera.address}:{self.soap_port}/upnp/control/ContentDirectory",
            headers={
                'SOAPACTION': '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
                'Content-Type': 'text/xml; charset="utf-8"',
            },
            data=soap,
            timeout=8,
        )
        r.raise_for_status()

        dom = minidom.parseString(r.text)
        result_nodes = dom.getElementsByTagName('Result')
        if not result_nodes:
            return None
        inner = result_nodes[0].firstChild.nodeValue if result_nodes[0].firstChild else ''
        if not inner:
            return None
        return minidom.parseString(inner)

    def _soap_browse_with_counts(self, object_id: str, starting_index: int = 0, requested_count: int = 200):
        soap = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            f'<ObjectID>{object_id}</ObjectID>'
            '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
            '<Filter>*</Filter>'
            f'<StartingIndex>{starting_index}</StartingIndex>'
            f'<RequestedCount>{requested_count}</RequestedCount>'
            '<SortCriteria></SortCriteria>'
            '</u:Browse>'
            '</s:Body>'
            '</s:Envelope>'
        )

        r = requests.post(
            f"http://{self.camera.address}:{self.soap_port}/upnp/control/ContentDirectory",
            headers={
                'SOAPACTION': '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"',
                'Content-Type': 'text/xml; charset="utf-8"',
            },
            data=soap,
            timeout=8,
        )
        r.raise_for_status()

        dom = minidom.parseString(r.text)
        result_nodes = dom.getElementsByTagName('Result')
        if not result_nodes:
            return None, 0, 0

        inner = result_nodes[0].firstChild.nodeValue if result_nodes[0].firstChild else ''
        didl = minidom.parseString(inner) if inner else None

        number_returned = 0
        total_matches = 0
        nr_el = dom.getElementsByTagName("NumberReturned")
        tm_el = dom.getElementsByTagName("TotalMatches")
        if nr_el and nr_el[0].firstChild:
            number_returned = int(nr_el[0].firstChild.nodeValue)
        if tm_el and tm_el[0].firstChild:
            total_matches = int(tm_el[0].firstChild.nodeValue)

        return didl, number_returned, total_matches

    def _soap_call(self, service: str, action: str, body_xml: str, timeout=8):
        r = requests.post(
            f"http://{self.camera.address}:{self.soap_port}/upnp/control/{service}",
            headers={
                "SOAPACTION": f'"{action}"',
                "Content-Type": 'text/xml; charset="utf-8"',
            },
            data=body_xml,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.text

    def _soap_transfer_start(self):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:X_TransferStart xmlns:u="urn:schemas-sony-com:service:XPushList:1"></u:X_TransferStart>'
            '</s:Body>'
            '</s:Envelope>'
        )
        self._soap_call(
            "XPushList",
            "urn:schemas-sony-com:service:XPushList:1#X_TransferStart",
            body,
            timeout=8,
        )

    def _soap_transfer_end(self):
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:X_TransferEnd xmlns:u="urn:schemas-sony-com:service:XPushList:1">'
            '<ErrCode>0</ErrCode>'
            '</u:X_TransferEnd>'
            '</s:Body>'
            '</s:Envelope>'
        )
        self._soap_call(
            "XPushList",
            "urn:schemas-sony-com:service:XPushList:1#X_TransferEnd",
            body,
            timeout=8,
        )

    def _safe_text(self, node, tag_name: str):
        els = node.getElementsByTagName(tag_name)
        if not els:
            return ""
        n = els[0]
        if n.firstChild:
            return str(n.firstChild.nodeValue or "")
        return ""

    def _best_res_url(self, item_node):
        best_url = None
        best_size = -1
        best_protocol = ""
        for res in item_node.getElementsByTagName("res"):
            if not res.firstChild:
                continue
            u = str(res.firstChild.nodeValue or "").strip()
            if not u:
                continue
            size = -1
            if res.hasAttribute("size"):
                try:
                    size = int(res.getAttribute("size"))
                except Exception:
                    size = -1
            protocol = res.getAttribute("protocolInfo") if res.hasAttribute("protocolInfo") else ""
            if size > best_size:
                best_size = size
                best_url = u
                best_protocol = protocol
        if not best_url:
            all_res = item_node.getElementsByTagName("res")
            if all_res:
                r0 = all_res[-1]
                if r0.firstChild:
                    best_url = str(r0.firstChild.nodeValue or "").strip()
                    best_protocol = r0.getAttribute("protocolInfo") if r0.hasAttribute("protocolInfo") else ""
        return best_url, best_size, best_protocol

    def _browse_collect_items(self, object_id: str, container_path: str, out_items: list, max_items: int = 500):
        start_idx = 0
        while len(out_items) < max_items:
            didl, number_returned, total_matches = self._soap_browse_with_counts(
                object_id,
                starting_index=start_idx,
                requested_count=200,
            )
            if didl is None:
                return

            containers = didl.getElementsByTagName("container")
            for c in containers:
                if len(out_items) >= max_items:
                    break
                cid = c.getAttribute("id") if c.hasAttribute("id") else ""
                if not cid:
                    continue
                cname = self._safe_text(c, "dc:title") or self._safe_text(c, "title") or cid
                sub_path = f"{container_path}/{cname}" if container_path else cname
                self._browse_collect_items(cid, sub_path, out_items, max_items=max_items)

            items = didl.getElementsByTagName("item")
            for item in items:
                if len(out_items) >= max_items:
                    break
                title = self._safe_text(item, "dc:title") or self._safe_text(item, "title") or "unnamed"
                url, size, protocol = self._best_res_url(item)
                if not url:
                    continue
                out_items.append(
                    {
                        "container": container_path,
                        "title": title,
                        "url": url,
                        "size": size,
                        "protocolInfo": protocol,
                    }
                )

            if number_returned <= 0:
                break
            start_idx += number_returned
            if start_idx >= total_matches:
                break

    def start_file_transfer_mode(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10040, self.last_camera_error or "Camera Wi-Fi not connected"]}

            # Force stop preview stream before entering transfer mode.
            try:
                if self.streaming_enabled:
                    self.camera.call("stopLiveview", timeout=3)
            except Exception:
                pass
            self.streaming_enabled = False
            self.liveview_url = None
            self.source_mode = "transfer"

            try:
                self._soap_transfer_start()
                self.transfer_active = True
                self.transfer_last_error = None
                return True, {"result": ["transfer-mode-started"]}
            except Exception as exc:
                self.transfer_active = False
                self.transfer_last_error = str(exc)
                return False, self._format_transfer_mode_hint(str(exc), "Transfer start")

    def stop_file_transfer_mode(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10042, self.last_camera_error or "Camera Wi-Fi not connected"]}
            try:
                self._soap_transfer_end()
                self.transfer_active = False
                self.source_mode = "liveview"
                self.transfer_last_error = None
                return True, {"result": ["transfer-mode-stopped"]}
            except Exception as exc:
                self.transfer_last_error = str(exc)
                return False, self._format_transfer_mode_hint(str(exc), "Transfer stop")

    def list_transfer_files(self, max_items: int = 500):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10044, self.last_camera_error or "Camera Wi-Fi not connected"]}

            roots = ["PushRoot", "PhotoRoot"]
            all_items = []
            errors = []

            for root in roots:
                try:
                    self._browse_collect_items(root, root, all_items, max_items=max_items)
                    if all_items:
                        break
                except Exception as exc:
                    errors.append(f"{root}: {exc}")

            self.transfer_items = all_items
            if not all_items:
                self.transfer_last_error = "; ".join(errors) if errors else "No items found"
                if "404" in self.transfer_last_error and "/upnp/control/" in self.transfer_last_error:
                    return False, self._format_transfer_mode_hint(self.transfer_last_error, "Transfer list")
                return False, {"error": [10045, self.transfer_last_error]}

            self.transfer_last_error = None
            return True, {
                "count": len(all_items),
                "items": [
                    {
                        "id": idx,
                        "container": item.get("container", ""),
                        "title": item.get("title", ""),
                        "size": item.get("size", -1),
                        "url": item.get("url", ""),
                        "downloadPath": f"/transfer/download/{idx}",
                    }
                    for idx, item in enumerate(all_items)
                ],
            }

    def get_transfer_item_by_id(self, idx: int):
        with self.lock:
            if idx < 0 or idx >= len(self.transfer_items):
                return None
            return self.transfer_items[idx]

    def build_transfer_bundle(self, max_items: int = 500):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10048, self.last_camera_error or "Camera Wi-Fi not connected"]}

            if self.transfer_bundle_building:
                return False, {"error": [10051, "Transfer bundle is already being built"]}

            if not self.transfer_items:
                ok, result = self.list_transfer_files(max_items=max_items)
                if not ok:
                    return False, result

            items = list(self.transfer_items[:max_items])
            if not items:
                return False, {"error": [10049, "No transfer items available to bundle"]}

            self._cleanup_transfer_bundle()
            self.transfer_bundle_building = True
            self.transfer_last_error = None

        worker_items = list(items)

        def worker():
            bundle_ts = time.strftime("%Y%m%d-%H%M%S")
            bundle_name = f"sony-transfer-{bundle_ts}.zip"
            fd, bundle_path = tempfile.mkstemp(prefix="sony-transfer-", suffix=".zip")
            os.close(fd)

            try:
                with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_STORED) as archive:
                    used_names = set()
                    for item in worker_items:
                        title = self._safe_filename(item.get("title", "unnamed"))
                        container = item.get("container", "") or "root"
                        container = container.replace("PhotoRoot/", "").replace("PushRoot/", "")
                        container = "/".join(self._safe_filename(part) for part in container.split("/") if part)
                        arcname = f"{container}/{title}" if container else title
                        if arcname in used_names:
                            stem, ext = os.path.splitext(arcname)
                            idx = 2
                            while f"{stem}-{idx}{ext}" in used_names:
                                idx += 1
                            arcname = f"{stem}-{idx}{ext}"
                        used_names.add(arcname)

                        with requests.get(item.get("url", ""), stream=True, timeout=(8, 120)) as resp:
                            resp.raise_for_status()
                            with archive.open(arcname, mode="w") as out_file:
                                for chunk in resp.iter_content(chunk_size=262144):
                                    if chunk:
                                        out_file.write(chunk)

                with self.lock:
                    self.transfer_bundle_path = bundle_path
                    self.transfer_bundle_name = bundle_name
                    self.transfer_bundle_ts = int(time.time())
                    self.transfer_bundle_building = False
                    self.transfer_last_error = None
            except Exception as exc:
                try:
                    os.remove(bundle_path)
                except OSError:
                    pass
                with self.lock:
                    self.transfer_bundle_building = False
                    self.transfer_last_error = str(exc)

        with self.lock:
            self.transfer_bundle_thread = threading.Thread(target=worker, daemon=True)
            self.transfer_bundle_thread.start()

        return True, {
            "building": True,
            "bundlePath": "/transfer/download_all.zip",
            "count": len(items),
        }

    def get_transfer_bundle_info(self):
        with self.lock:
            if not self.transfer_bundle_path or not os.path.isfile(self.transfer_bundle_path):
                return None
            return {
                "path": self.transfer_bundle_path,
                "name": self.transfer_bundle_name or "sony-transfer.zip",
                "timestamp": self.transfer_bundle_ts,
            }

    def _best_item_url_from_didl(self, didl_dom):
        if not didl_dom:
            return None
        items = didl_dom.getElementsByTagName('item')
        if not items:
            return None

        item = items[0]
        best_url = None
        best_size = -1
        for res in item.getElementsByTagName('res'):
            if not res.firstChild:
                continue
            u = res.firstChild.nodeValue
            size = -1
            if 'size' in res.attributes:
                try:
                    size = int(res.attributes['size'].value)
                except Exception:
                    size = -1
            if size > best_size:
                best_size = size
                best_url = u

        if not best_url:
            for res in item.getElementsByTagName('res'):
                if res.firstChild:
                    best_url = res.firstChild.nodeValue
                    break

        return best_url

    def _try_soap_latest_urls(self):
        # Re-use logic from schorschii/ImagingEdge4Linux SOAP reverse engineering.
        roots = ["PhotoRoot", "PushRoot"]

        for root in roots:
            try:
                didl = self._soap_browse(root, 0, 200)
            except Exception:
                continue

            if didl is None:
                continue

            # If root directly contains items, use first as newest candidate.
            u = self._best_item_url_from_didl(didl)
            if u:
                return [u]

            # Otherwise dive into first few containers and try to get first item.
            containers = didl.getElementsByTagName('container')
            for c in containers[:8]:
                cid = c.getAttribute('id') if c.hasAttribute('id') else None
                if not cid:
                    continue
                try:
                    didl2 = self._soap_browse(cid, 0, 200)
                except Exception:
                    continue
                u2 = self._best_item_url_from_didl(didl2)
                if u2:
                    return [u2]

        return []

    def _get_avcontent_method_names(self):
        try:
            r = self._call_avcontent("getMethodTypes", [""], timeout=4)
            if self.camera.has_error(r):
                return []
            rows = r.get("results", [])
            names = []
            for row in rows:
                if isinstance(row, list) and len(row) > 0 and isinstance(row[0], str):
                    names.append(row[0])
            return names
        except Exception:
            return []

    def fetch_latest_image(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10031, self.last_camera_error or "Camera Wi-Fi not connected"]}

            urls = []
            available = self._get_available_api_set()

            # 0) Best path for "just taken" image in some Sony API modes.
            if "awaitTakePicture" in available:
                try:
                    r = self.camera.call("awaitTakePicture", timeout=12)
                    if not self.camera.has_error(r):
                        urls.extend(self._collect_http_image_urls(r.get("result", [])))
                except Exception:
                    pass

            # 1) Try event payload first (can expose recent postview URL after a shot)
            try:
                ev = self.camera.call("getEvent", [False], timeout=4)
                if not self.camera.has_error(ev):
                    urls.extend(self._collect_http_image_urls(ev.get("result", [])))
            except Exception:
                pass

            # 2) Fallback to actTakePicture if camera mode allows it
            if not urls:
                if "actTakePicture" in available:
                    try:
                        r = self.camera.call("actTakePicture", timeout=8)
                        if not self.camera.has_error(r):
                            urls.extend(self._collect_http_image_urls(r.get("result", [])))
                    except Exception:
                        pass

            # 3) Fallback to avContent (latest saved still on card)
            if not urls:
                try:
                    urls.extend(self._try_avcontent_latest_urls())
                except Exception:
                    pass

            # 4) Fallback to SOAP ContentDirectory (ImagingEdge4Linux path)
            if not urls:
                try:
                    urls.extend(self._try_soap_latest_urls())
                except Exception:
                    pass

            if not urls:
                cam_methods = sorted(list(self._get_available_api_set()))
                av_methods = self._get_avcontent_method_names()
                return False, {
                    "error": [10032, "No image URL available from camera methods in current mode"],
                    "cameraMethods": cam_methods,
                    "avContentMethods": av_methods,
                }

            last_err = None
            for u in urls:
                try:
                    data = self._download_image_bytes(u)
                    self.latest_image_jpeg = data
                    self.latest_image_url = u
                    self.latest_image_ts = int(time.time())
                    self.last_camera_error = None
                    return True, {
                        "url": u,
                        "bytes": len(data),
                        "timestamp": self.latest_image_ts,
                    }
                except Exception as exc:
                    last_err = str(exc)

            return False, {"error": [10033, f"Failed to download image from camera URLs: {last_err or 'unknown'}"]}

    def _grab_compressed_still(self):
        # Preview-only mode: do not trigger actTakePicture.
        raise RuntimeError("HQ still mode disabled (preview-only liveview mode)")

    def _stills_loop(self):
        while not self.stop_stills.is_set():
            with self.lock:
                enabled = self.stills_enabled
                interval_ms = max(150, int(self.stills_interval_ms))

            if not enabled:
                self.stop_stills.wait(0.2)
                continue

            t0 = time.time()
            try:
                frame = self._grab_compressed_still()
                with self.lock:
                    if not self.stills_enabled:
                        continue
                    self.latest_frame = frame
                    self.frame_count += 1
                    self.stills_frame_count += 1
                    self.last_camera_error = None
            except Exception as exc:
                with self.lock:
                    self.last_camera_error = f"HQ still grab failed: {exc}"

            elapsed_ms = int((time.time() - t0) * 1000)
            sleep_ms = max(0, interval_ms - elapsed_ms)
            self.stop_stills.wait(sleep_ms / 1000.0)

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
                            self.latest_frame_ts = time.time()
                            self.frame_count += 1
                            self.last_camera_error = None
            except Exception:
                with self.lock:
                    self.last_camera_error = "Liveview stream unavailable"
                self.stop_grabber.wait(0.5)

    def start_liveview(self):
        with self.lock:
            self.stills_enabled = False
            self.source_mode = "liveview"
            self.transfer_active = False
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

    def start_stills_stream(self):
        with self.lock:
            # Keep behavior explicit to avoid accidental shutter triggers.
            self.stills_enabled = False
            self.source_mode = "liveview"
            self.last_camera_error = "HQ still mode disabled: preview-only liveview mode"
            return False, {"error": [10019, self.last_camera_error]}

    def stop_stills_stream(self):
        with self.lock:
            self.stills_enabled = False
            self.last_camera_error = None
            return True, {"result": ["stills-stream-stopped"]}

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

    def _get_available_api_set(self):
        try:
            r = self.camera.call("getAvailableApiList", timeout=3)
            if self.camera.has_error(r):
                return set()
            return set(r.get("result", [[]])[0])
        except Exception:
            return set()

    def _find_first_by_keys(self, node, keys):
        if isinstance(node, dict):
            for k in keys:
                if k in node:
                    return node[k]
            for v in node.values():
                found = self._find_first_by_keys(v, keys)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = self._find_first_by_keys(item, keys)
                if found is not None:
                    return found
        return None

    def half_press_start(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10020, self.last_camera_error or "Camera Wi-Fi not connected"]}

            available = self._get_available_api_set()
            if available and "actHalfPressShutter" not in available:
                return False, {"error": [10021, "actHalfPressShutter not supported"]}

            try:
                r = self.camera.call("actHalfPressShutter", timeout=3)
                if self.camera.has_error(r):
                    return False, r
                self.last_camera_error = None
                return True, r
            except Exception as exc:
                return False, {"error": [10022, str(exc)]}

    def half_press_stop(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10023, self.last_camera_error or "Camera Wi-Fi not connected"]}

            available = self._get_available_api_set()
            if available and "cancelHalfPressShutter" not in available:
                return False, {"error": [10024, "cancelHalfPressShutter not supported"]}

            try:
                r = self.camera.call("cancelHalfPressShutter", timeout=3)
                if self.camera.has_error(r):
                    return False, r
                self.last_camera_error = None
                return True, r
            except Exception as exc:
                return False, {"error": [10025, str(exc)]}

    def shutter_click(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10026, self.last_camera_error or "Camera Wi-Fi not connected"]}

            available = self._get_available_api_set()
            if "actTakePicture" not in available:
                return False, {"error": [10027, "actTakePicture not supported by current camera API mode"]}

            try:
                r = self.camera.call("actTakePicture", timeout=8)
                if self.camera.has_error(r):
                    err = r.get("error", [])
                    if isinstance(err, list) and len(err) > 0 and int(err[0]) == 40400:
                        return False, {"error": [10030, "actTakePicture exists but is unavailable in current camera mode"]}
                    return False, r
                self.last_camera_error = None
                return True, r
            except Exception as exc:
                return False, {"error": [10028, str(exc)]}

    def camera_info(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10029, self.last_camera_error or "Camera Wi-Fi not connected"]}

            available = self._get_available_api_set()
            info = {
                "availableApiCount": len(available),
                "batteryLevel": self.battery_level,
                "cameraStatus": self.camera_status,
                "focusStatus": self.focus_status,
            }

            scalar_getters = [
                ("getShootMode", "shootMode"),
                ("getExposureMode", "exposureMode"),
                ("getIsoSpeedRate", "isoSpeedRate"),
                ("getShutterSpeed", "shutterSpeed"),
                ("getFNumber", "fNumber"),
                ("getWhiteBalance", "whiteBalance"),
                ("getExposureCompensation", "exposureCompensation"),
                ("getMovieQuality", "movieQuality"),
                ("getMovieFileFormat", "movieFileFormat"),
            ]

            for method, key in scalar_getters:
                if available and method not in available:
                    continue
                try:
                    r = self.camera.call(method, timeout=3)
                    if self.camera.has_error(r):
                        continue
                    result = r.get("result", [])
                    info[key] = result[0] if isinstance(result, list) and len(result) > 0 else result
                except Exception:
                    pass

            if (not available) or ("getEvent" in available):
                try:
                    ev = self.camera.call("getEvent", [False], timeout=4)
                    if not self.camera.has_error(ev):
                        ev_root = ev.get("result", [])
                        b = self._find_first_by_keys(ev_root, ["batteryLevel", "batteryLevelPercent"])
                        c = self._find_first_by_keys(ev_root, ["cameraStatus"])
                        f = self._find_first_by_keys(ev_root, ["focusStatus", "focusState"])
                        if b is not None:
                            self.battery_level = b
                        if c is not None:
                            self.camera_status = str(c)
                        if f is not None:
                            self.focus_status = str(f)
                except Exception:
                    pass

            info["batteryLevel"] = self.battery_level
            info["cameraStatus"] = self.camera_status
            info["focusStatus"] = self.focus_status
            return True, info

    def set_stills_interval_ms(self, interval_ms: int):
        with self.lock:
            self.stills_interval_ms = max(150, min(5000, int(interval_ms)))
            return self.stills_interval_ms

    def _normalize_candidates(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            c = value.get("candidate")
            if isinstance(c, list):
                return c
            if isinstance(c, str):
                return [c]
            # some methods return {key: value}
            flattened = []
            for v in value.values():
                if isinstance(v, list):
                    flattened.extend(v)
                elif isinstance(v, str):
                    flattened.append(v)
            return flattened
        return []

    def get_setting_candidates(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10018, self.last_camera_error or "Camera Wi-Fi not connected"]}

            candidates = {}
            for method, key in [
                ("getAvailableExposureMode", "exposureMode"),
                ("getAvailableMovieQuality", "movieQuality"),
                ("getAvailableMovieFileFormat", "movieFileFormat"),
            ]:
                try:
                    r = self.camera.call(method, timeout=3)
                    if self.camera.has_error(r):
                        candidates[key] = []
                    else:
                        raw = r.get("result", [[]])
                        first = raw[0] if isinstance(raw, list) and len(raw) > 0 else raw
                        candidates[key] = self._normalize_candidates(first)
                except Exception:
                    candidates[key] = []

            return True, candidates

    def apply_key_settings(self, exposure_mode=None, movie_quality=None, movie_file_format=None):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return False, {"error": [10014, self.last_camera_error or "Camera Wi-Fi not connected"]}

            try:
                # best effort to ensure camera accepts settings
                self.camera.call("startRecMode", timeout=2)
            except Exception:
                pass

            applied = {}
            skipped = {}

            try:
                apis_result = self.camera.call("getAvailableApiList", timeout=3)
                available = set(apis_result.get("result", [[]])[0]) if isinstance(apis_result, dict) else set()
            except Exception:
                available = set()

            if exposure_mode:
                if available and "setExposureMode" not in available:
                    skipped["exposureMode"] = "setExposureMode not supported"
                else:
                    try:
                        r = self.camera.call("setExposureMode", [str(exposure_mode)], timeout=3)
                        if self.camera.has_error(r):
                            skipped["exposureMode"] = str(r.get("error"))
                        else:
                            self.exposure_mode = str(exposure_mode)
                            applied["exposureMode"] = self.exposure_mode
                    except Exception as exc:
                        skipped["exposureMode"] = str(exc)

            if movie_quality:
                if available and "setMovieQuality" not in available:
                    skipped["movieQuality"] = "setMovieQuality not supported"
                else:
                    try:
                        r = self.camera.call("setMovieQuality", [str(movie_quality)], timeout=3)
                        if self.camera.has_error(r):
                            skipped["movieQuality"] = str(r.get("error"))
                        else:
                            self.movie_quality = str(movie_quality)
                            applied["movieQuality"] = self.movie_quality
                    except Exception as exc:
                        skipped["movieQuality"] = str(exc)

            if movie_file_format:
                if available and "setMovieFileFormat" not in available:
                    skipped["movieFileFormat"] = "setMovieFileFormat not supported"
                else:
                    try:
                        r = self.camera.call("setMovieFileFormat", [str(movie_file_format)], timeout=3)
                        if self.camera.has_error(r):
                            skipped["movieFileFormat"] = str(r.get("error"))
                        else:
                            self.movie_file_format = str(movie_file_format)
                            applied["movieFileFormat"] = self.movie_file_format
                    except Exception as exc:
                        skipped["movieFileFormat"] = str(exc)

            self.last_camera_error = None
            ok = len(applied) > 0 or (exposure_mode is None and movie_quality is None and movie_file_format is None)
            return ok, {"applied": applied, "skipped": skipped}

    def health(self):
        with self.lock:
            if not self.ensure_wifi_direct_connected():
                return {
                    "ok": False,
                    "error": self.last_camera_error or "Camera Wi-Fi not connected",
                    "liveviewUrl": self.liveview_url,
                    "streamingEnabled": self.streaming_enabled,
                    "stillsEnabled": self.stills_enabled,
                    "sourceMode": self.source_mode,
                    "stillsIntervalMs": self.stills_interval_ms,
                    "exposureMode": self.exposure_mode,
                    "movieQuality": self.movie_quality,
                    "movieFileFormat": self.movie_file_format,
                    "frameCount": self.frame_count,
                    "stillsFrameCount": self.stills_frame_count,
                    "transferActive": self.transfer_active,
                    "transferItemCount": len(self.transfer_items),
                    "transferLastError": self.transfer_last_error,
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
                    "stillsEnabled": self.stills_enabled,
                    "sourceMode": self.source_mode,
                    "stillsIntervalMs": self.stills_interval_ms,
                    "exposureMode": self.exposure_mode,
                    "movieQuality": self.movie_quality,
                    "movieFileFormat": self.movie_file_format,
                    "frameCount": self.frame_count,
                    "stillsFrameCount": self.stills_frame_count,
                    "transferActive": self.transfer_active,
                    "transferItemCount": len(self.transfer_items),
                    "transferLastError": self.transfer_last_error,
                }
            return {
                "ok": True,
                "versions": versions,
                "apis": apis,
                "liveviewUrl": self.liveview_url,
                "streamingEnabled": self.streaming_enabled,
                "stillsEnabled": self.stills_enabled,
                "sourceMode": self.source_mode,
                "stillsIntervalMs": self.stills_interval_ms,
                "exposureMode": self.exposure_mode,
                "movieQuality": self.movie_quality,
                "movieFileFormat": self.movie_file_format,
                "frameCount": self.frame_count,
                "stillsFrameCount": self.stills_frame_count,
                "transferActive": self.transfer_active,
                "transferItemCount": len(self.transfer_items),
                "transferLastError": self.transfer_last_error,
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
    server_version = "SonyLiveviewWebUI/2.0"

    @property
    def state(self) -> AppState:
        return self.server.state

    def _write_body(self, body: bytes):
        try:
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False

    def _send_json(self, payload, code=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False
        return self._write_body(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False
        return self._write_body(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ["/", "/debug"]:
            self._send_html(INDEX_HTML)
            return

        if parsed.path == "/api/status":
            self._send_json(
                {
                    "liveviewUrl": self.state.liveview_url,
                    "streamingEnabled": self.state.streaming_enabled,
                    "stillsEnabled": self.state.stills_enabled,
                    "sourceMode": self.state.source_mode,
                    "stillsIntervalMs": self.state.stills_interval_ms,
                    "exposureMode": self.state.exposure_mode,
                    "movieQuality": self.state.movie_quality,
                    "movieFileFormat": self.state.movie_file_format,
                    "batteryLevel": self.state.battery_level,
                    "cameraStatus": self.state.camera_status,
                    "focusStatus": self.state.focus_status,
                    "latestImageTs": self.state.latest_image_ts,
                    "latestImageBytes": (len(self.state.latest_image_jpeg) if self.state.latest_image_jpeg else 0),
                    "transferActive": self.state.transfer_active,
                    "transferItemCount": len(self.state.transfer_items),
                    "transferLastError": self.state.transfer_last_error,
                    "transferBundleReady": bool(self.state.transfer_bundle_path and os.path.isfile(self.state.transfer_bundle_path)),
                    "transferBundleName": self.state.transfer_bundle_name,
                    "transferBundleTs": self.state.transfer_bundle_ts,
                    "transferBundleBuilding": self.state.transfer_bundle_building,
                    "stillsFrameCount": self.state.stills_frame_count,
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

        if parsed.path == "/latest_image.jpg":
            self._send_latest_captured_image()
            return

        if parsed.path.startswith("/transfer/download/"):
            self._send_transfer_file(parsed.path)
            return

        if parsed.path == "/transfer/download_all.zip":
            self._send_transfer_bundle()
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

            if parsed.path == "/api/start_stills":
                ok, result = self.state.start_stills_stream()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/stop_stills":
                ok, result = self.state.stop_stills_stream()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/set_stills_interval":
                raw_len = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(raw_len) if raw_len > 0 else b""
                payload = json.loads(body.decode("utf-8")) if body else {}
                ms = int(payload.get("ms", self.state.stills_interval_ms))
                applied = self.state.set_stills_interval_ms(ms)
                self._send_json({"ok": True, "result": {"stillsIntervalMs": applied}})
                return

            if parsed.path == "/api/apply_settings":
                raw_len = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(raw_len) if raw_len > 0 else b""
                payload = json.loads(body.decode("utf-8")) if body else {}
                ok, result = self.state.apply_key_settings(
                    exposure_mode=payload.get("exposureMode"),
                    movie_quality=payload.get("movieQuality"),
                    movie_file_format=payload.get("movieFileFormat"),
                )
                self._send_json({"ok": ok, "result": result})
                return

            if parsed.path == "/api/setting_candidates":
                ok, result = self.state.get_setting_candidates()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/camera_info":
                ok, result = self.state.camera_info()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/half_press_start":
                ok, result = self.state.half_press_start()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/half_press_stop":
                ok, result = self.state.half_press_stop()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/shutter_click":
                ok, result = self.state.shutter_click()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/fetch_latest_image":
                ok, result = self.state.fetch_latest_image()
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

            if parsed.path == "/api/transfer/start":
                ok, result = self.state.start_file_transfer_mode()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/transfer/stop":
                ok, result = self.state.stop_file_transfer_mode()
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/transfer/list":
                raw_len = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(raw_len) if raw_len > 0 else b""
                payload = json.loads(body.decode("utf-8")) if body else {}
                limit = int(payload.get("limit", 500))
                limit = max(1, min(2000, limit))
                ok, result = self.state.list_transfer_files(max_items=limit)
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
                return

            if parsed.path == "/api/transfer/build_bundle":
                raw_len = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(raw_len) if raw_len > 0 else b""
                payload = json.loads(body.decode("utf-8")) if body else {}
                limit = int(payload.get("limit", 500))
                limit = max(1, min(2000, limit))
                ok, result = self.state.build_transfer_bundle(max_items=limit)
                self._send_json({"ok": ok, "result": result}, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
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
                active = self.state.streaming_enabled or self.state.stills_enabled
                need_start = (not self.state.liveview_url) or (not active)
                source_mode = self.state.source_mode

            if need_start:
                if source_mode == "stills":
                    ok, result = self.state.start_stills_stream()
                else:
                    ok, result = self.state.start_liveview()
                if not ok:
                    self._send_json({"ok": False, "error": "Could not start stream", "result": result}, HTTPStatus.SERVICE_UNAVAILABLE)
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
                    if not (self.state.streaming_enabled or self.state.stills_enabled):
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
            enabled = self.state.streaming_enabled or self.state.stills_enabled
            source_mode = self.state.source_mode

        if not frame and not enabled:
            if source_mode == "stills":
                self.state.start_stills_stream()
            else:
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

    def _send_latest_captured_image(self):
        with self.state.lock:
            frame = self.state.latest_image_jpeg

        if not frame:
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"No captured image loaded yet")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _send_transfer_file(self, path: str):
        try:
            idx_s = path.split("/")[-1]
            idx = int(idx_s)
        except Exception:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid transfer file id")
            return

        item = self.state.get_transfer_item_by_id(idx)
        if not item:
            self.send_error(HTTPStatus.NOT_FOUND, "Transfer file id not found")
            return

        src_url = item.get("url")
        if not src_url:
            self.send_error(HTTPStatus.NOT_FOUND, "No source URL for this item")
            return

        try:
            with requests.get(src_url, stream=True, timeout=(8, 60)) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type") or "application/octet-stream"
                content_length = resp.headers.get("Content-Length")
                filename = item.get("title") or f"transfer-{idx}"

                try:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", content_type)
                    if content_length:
                        self.send_header("Content-Length", content_length)
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return

                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        if not self._write_body(chunk):
                            return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def _send_transfer_bundle(self):
        info = self.state.get_transfer_bundle_info()
        if not info:
            self.send_error(HTTPStatus.NOT_FOUND, "Transfer bundle not built yet")
            return

        try:
            size = os.path.getsize(info["path"])
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{info["name"]}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return

            with open(info["path"], "rb") as fh:
                while True:
                    chunk = fh.read(262144)
                    if not chunk:
                        break
                    if not self._write_body(chunk):
                        return
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Sony Camera Control Portal</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0f1115;
            --panel: #171a21;
            --panel-2: #1d2230;
            --text: #eef2ff;
            --muted: #a5afc4;
            --border: #2a3141;
            --blue: #3478f6;
            --blue-2: #1f5fd2;
            --amber: #f59e0b;
            --red: #ef4444;
            --green: #22c55e;
            --chip: #222938;
            --shadow: 0 10px 30px rgba(0,0,0,.28);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            padding: 22px;
            font-family: Segoe UI, Arial, sans-serif;
            background: linear-gradient(180deg, #0d1017 0%, #121620 100%);
            color: var(--text);
        }
        h1, h2, h3, p { margin: 0; }
        a { color: #8bb8ff; }
        .shell { max-width: 1500px; margin: 0 auto; }
        .hero, .panel {
            background: rgba(23,26,33,.96);
            border: 1px solid var(--border);
            border-radius: 18px;
            box-shadow: var(--shadow);
        }
        .hero { padding: 20px; margin-bottom: 16px; }
        .hero-top {
            display: flex;
            gap: 16px;
            align-items: flex-start;
            justify-content: space-between;
            flex-wrap: wrap;
        }
        .hero h1 { font-size: 28px; margin-bottom: 8px; }
        .sub { color: var(--muted); max-width: 900px; line-height: 1.5; }
        .chip-row, .stats-row, .button-row, .field-row, .mode-grid, .preview-grid {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        .chip-row { margin-top: 14px; }
        .chip {
            background: var(--chip);
            border: 1px solid #31384a;
            color: var(--text);
            padding: 7px 10px;
            border-radius: 999px;
            font-size: 12px;
        }
        .chip.good { border-color: rgba(34,197,94,.4); color: #9ff0b5; }
        .chip.warn { border-color: rgba(245,158,11,.4); color: #ffd287; }
        .chip.bad { border-color: rgba(239,68,68,.45); color: #ffaaaa; }
        .stats-row { margin-top: 16px; }
        .stat {
            min-width: 140px;
            padding: 12px 14px;
            border-radius: 14px;
            background: var(--panel-2);
            border: 1px solid #2f3750;
        }
        .stat .k { color: var(--muted); font-size: 12px; display: block; margin-bottom: 4px; }
        .stat .v { font-size: 18px; font-weight: 700; }
        .layout { display: grid; grid-template-columns: 1.15fr .85fr; gap: 16px; }
        .stack { display: grid; gap: 16px; }
        .panel { padding: 16px; }
        .panel h2 { font-size: 18px; margin-bottom: 6px; }
        .panel p.help { color: var(--muted); font-size: 13px; margin-bottom: 14px; line-height: 1.45; }
        .button-row { margin-bottom: 12px; }
        button {
            border: 0;
            border-radius: 12px;
            padding: 10px 14px;
            cursor: pointer;
            font-weight: 600;
            background: #eef2ff;
            color: #101522;
            transition: transform .05s ease, filter .15s ease;
        }
        button:hover { filter: brightness(.96); }
        button:active { transform: translateY(1px); }
        button.primary { background: var(--blue); color: white; }
        button.primary:hover { background: var(--blue-2); }
        button.warn { background: var(--amber); color: #16120b; }
        button.danger { background: var(--red); color: white; }
        button.ghost { background: #242b38; color: #eef2ff; border: 1px solid #343d51; }
        button:disabled { opacity: .6; cursor: default; }
        label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; min-width: 160px; }
        input, select {
            min-height: 40px;
            background: #111622;
            color: var(--text);
            border: 1px solid #31384a;
            border-radius: 10px;
            padding: 8px 10px;
        }
        .mode-grid { align-items: stretch; }
        .mode-card {
            flex: 1 1 380px;
            border: 1px solid #30384b;
            border-radius: 16px;
            background: linear-gradient(180deg, rgba(29,34,48,.9), rgba(21,25,34,.9));
            padding: 16px;
        }
        .mode-card h3 { margin-bottom: 6px; }
        .mode-card .caption { color: var(--muted); font-size: 13px; margin-bottom: 14px; line-height: 1.45; }
        #statusBar {
            margin-top: 12px;
            padding: 12px 14px;
            border: 1px solid #33405a;
            border-radius: 12px;
            background: #111827;
            color: #d9e4ff;
            min-height: 46px;
        }
        details {
            border: 1px solid #2f3750;
            border-radius: 12px;
            background: #121826;
            padding: 10px 12px;
            margin-top: 12px;
        }
        details summary { cursor: pointer; color: #c9d7ff; font-weight: 600; }
        #diag, #caminfo { color: var(--muted); font-size: 12px; line-height: 1.5; margin-top: 8px; }
        #streamWrap {
            background: #05070c;
            border: 1px solid #2d3447;
            border-radius: 16px;
            overflow: hidden;
        }
        #stream, #latestImage {
            width: 100%;
            display: block;
            background: #05070c;
            min-height: 240px;
            object-fit: contain;
        }
        .preview-grid { align-items: end; margin-top: 10px; }
        .meta {
            color: var(--muted);
            font-size: 12px;
            margin-top: 10px;
            line-height: 1.45;
            word-break: break-word;
        }
        .transfer-list {
            max-height: 420px;
            overflow: auto;
            margin-top: 10px;
            border: 1px solid #2c3446;
            border-radius: 12px;
            background: #0f1520;
            padding: 8px;
        }
        .file-row {
            display: grid;
            grid-template-columns: minmax(180px, 260px) 1fr auto;
            gap: 10px;
            align-items: center;
            padding: 8px 10px;
            border-bottom: 1px solid rgba(48,56,75,.7);
            font-size: 13px;
        }
        .file-row:last-child { border-bottom: 0; }
        .file-row .path { color: var(--muted); word-break: break-word; }
        .file-row .size { color: #c7d4f8; white-space: nowrap; }
        .empty { color: var(--muted); padding: 12px; }
        @media (max-width: 1120px) { .layout { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="shell">
        <section class="hero">
            <div class="hero-top">
                <div>
                    <h1>Sony Camera Control Portal</h1>
                    <p class="sub">Use one of two human-selected camera workflows: <strong>Preview Stream Mode</strong> for liveview and controls, or <strong>Transfer Mode</strong> while the camera shows <em>Send to Smartphone / Sharing</em> for browsing and bulk download of saved files.</p>
                    <div class="chip-row">
                        <span id="modeChip" class="chip">Mode unknown</span>
                        <span id="streamChip" class="chip">Preview idle</span>
                        <span id="transferChip" class="chip">Transfer idle</span>
                        <span id="recordChip" class="chip">Recording idle</span>
                    </div>
                </div>
            </div>
            <div class="stats-row">
                <div class="stat"><span class="k">Frames cached</span><span class="v" id="framesStat">0</span></div>
                <div class="stat"><span class="k">Backend FPS</span><span class="v" id="fpsStat">0.0</span></div>
                <div class="stat"><span class="k">Transfer files</span><span class="v" id="filesStat">0</span></div>
                <div class="stat"><span class="k">Battery</span><span class="v" id="batteryStat">n/a</span></div>
            </div>
            <div id="statusBar">Loading portal status…</div>
            <details>
                <summary>Technical details</summary>
                <div id="diag">Diagnostics: waiting…</div>
                <div id="caminfo">Camera info: waiting…</div>
            </details>
        </section>

        <div class="layout">
            <div class="stack">
                <section class="panel">
                    <h2>Mode Banks</h2>
                    <p class="help">These controls are grouped by the two camera states you switch on the camera itself. Enter the matching camera menu first, then use the buttons in that bank.</p>
                    <div class="mode-grid">
                        <div class="mode-card">
                            <h3>Preview Stream Mode</h3>
                            <div class="caption">Camera should be in <strong>Ctrl w/ Smartphone</strong>. Use this bank for live preview, exposure settings, half-press tests, shutter, and recording.</div>
                            <div class="button-row">
                                <button class="primary" onclick="switchToPreviewMode()">Start Preview Mode</button>
                                <button class="ghost" onclick="stopLiveview()">Stop Preview</button>
                                <button class="ghost" onclick="refreshStatus()">Refresh Status</button>
                                <button class="ghost" onclick="refreshCameraInfo()">Refresh Camera Info</button>
                            </div>
                            <div class="button-row">
                                <button onclick="halfPressStart()">Half-Press On</button>
                                <button onclick="halfPressStop()">Half-Press Off</button>
                                <button class="warn" onclick="shutterClick()">Shutter Click</button>
                                <button class="warn" onclick="startMovie()">Start Recording</button>
                                <button class="danger" onclick="stopMovie()">Stop Recording</button>
                                <button class="ghost" onclick="takeSnapshot()">Snapshot</button>
                            </div>
                        </div>

                        <div class="mode-card">
                            <h3>Transfer Mode</h3>
                            <div class="caption">Camera should show <strong>Send to Smartphone / Sharing</strong>. Use this bank to list saved files and create one bulk ZIP download of the current listing.</div>
                            <div class="button-row">
                                <button class="warn" onclick="switchToTransferMode()">Enter Transfer Mode</button>
                                <button class="ghost" onclick="listTransferFiles()">Refresh File List</button>
                                <button class="primary" onclick="buildTransferBundle()">Download All as ZIP</button>
                                <button class="ghost" onclick="stopTransferMode()">End Transfer Mode</button>
                            </div>
                            <div class="meta" id="transferMeta">Transfer mode inactive.</div>
                            <div class="meta" id="bundleMeta">No bulk bundle built yet.</div>
                        </div>
                    </div>
                </section>

                <section class="panel">
                    <h2>Live Preview</h2>
                    <p class="help">This feed is only expected to work in Preview Stream Mode.</p>
                    <div id="streamWrap"><img id="stream" alt="Live stream" /></div>
                    <div class="preview-grid">
                        <label>Preview transport
                            <select id="mode">
                                <option value="mjpeg" selected>MJPEG continuous (/stream)</option>
                                <option value="poll">JPEG polling (/frame.jpg)</option>
                            </select>
                        </label>
                        <label>Poll interval (ms)
                            <input id="pollMs" type="number" min="60" max="2000" step="10" value="150" />
                        </label>
                        <label>Display width
                            <select id="displayWidth">
                                <option value="100" selected>Auto (100%)</option>
                                <option value="75">75%</option>
                                <option value="50">50%</option>
                            </select>
                        </label>
                        <button class="ghost" onclick="applyPreviewSettings()">Apply Preview Settings</button>
                    </div>
                </section>

                <section class="panel">
                    <h2>Transfer Files</h2>
                    <p class="help">The list below reflects the latest items returned by the camera in Transfer Mode. Individual links are proxied through this local bridge.</p>
                    <div class="transfer-list" id="transferList"><div class="empty">No files listed yet.</div></div>
                </section>
            </div>

            <div class="stack">
                <section class="panel">
                    <h2>Camera Settings</h2>
                    <p class="help">These settings apply only when the Sony camera exposes them in Preview Stream Mode.</p>
                    <div class="field-row">
                        <label>Exposure mode
                            <select id="exposureMode"></select>
                        </label>
                        <label>Movie quality
                            <select id="movieQuality"></select>
                        </label>
                        <label>Movie file format
                            <select id="movieFileFormat"></select>
                        </label>
                    </div>
                    <div class="button-row" style="margin-top: 12px;">
                        <button class="ghost" onclick="applyCameraSettings()">Apply Camera Settings</button>
                        <button class="ghost" onclick="applyStillsInterval()">Apply HQ Interval</button>
                    </div>
                    <div class="meta">HQ still interval remains disabled for preview mode safety.</div>
                    <div class="field-row" style="margin-top: 12px;">
                        <label>HQ still interval (disabled)
                            <input id="stillsMs" type="number" min="150" max="5000" step="10" value="500" />
                        </label>
                    </div>
                </section>

                <section class="panel">
                    <h2>Latest Captured Image</h2>
                    <p class="help">Best-effort fetch of the latest image metadata or postview URL when the camera mode supports it.</p>
                    <div class="button-row">
                        <button class="primary" onclick="fetchLatestImage()">Refresh HQ Image</button>
                    </div>
                    <div class="meta" id="latestImageMeta">No HQ image loaded yet.</div>
                    <div id="streamWrap" style="margin-top:10px;"><img id="latestImage" alt="Latest captured image" /></div>
                </section>
            </div>
        </div>
    </div>

    <script>
        const statusEl = document.getElementById('statusBar');
        const diagEl = document.getElementById('diag');
        const camInfoEl = document.getElementById('caminfo');
        const modeChipEl = document.getElementById('modeChip');
        const streamChipEl = document.getElementById('streamChip');
        const transferChipEl = document.getElementById('transferChip');
        const recordChipEl = document.getElementById('recordChip');
        const framesStatEl = document.getElementById('framesStat');
        const fpsStatEl = document.getElementById('fpsStat');
        const filesStatEl = document.getElementById('filesStat');
        const batteryStatEl = document.getElementById('batteryStat');
        const latestImageEl = document.getElementById('latestImage');
        const latestImageMetaEl = document.getElementById('latestImageMeta');
        const transferMetaEl = document.getElementById('transferMeta');
        const bundleMetaEl = document.getElementById('bundleMeta');
        const transferListEl = document.getElementById('transferList');
        const streamEl = document.getElementById('stream');
        const exposureModeEl = document.getElementById('exposureMode');
        const movieQualityEl = document.getElementById('movieQuality');
        const movieFileFormatEl = document.getElementById('movieFileFormat');
        const modeEl = document.getElementById('mode');
        const pollMsEl = document.getElementById('pollMs');
        const stillsMsEl = document.getElementById('stillsMs');
        const displayWidthEl = document.getElementById('displayWidth');

        let pollTimer = null;
        let statusTimer = null;
        let lastFrameCount = 0;
        let lastStatusTs = Date.now();

        async function post(url) {
            const res = await fetch(url, { method: 'POST' });
            return await res.json();
        }

        async function postJson(url, payload) {
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload || {}),
            });
            return await res.json();
        }

        function setStatus(text, tone='') {
            statusEl.textContent = text;
            statusEl.style.borderColor = tone === 'bad' ? 'rgba(239,68,68,.45)' : tone === 'good' ? 'rgba(34,197,94,.4)' : '#33405a';
        }

        function setChip(el, text, tone='') {
            el.textContent = text;
            el.className = 'chip' + (tone ? ' ' + tone : '');
        }

        function esc(s) {
            return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function fmtBytes(n) {
            const value = Number(n || 0);
            if (!value) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let idx = 0;
            let current = value;
            while (current >= 1024 && idx < units.length - 1) {
                current /= 1024;
                idx += 1;
            }
            return `${current.toFixed(current >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`;
        }

        function refillSelect(el, values) {
            const previous = el.value;
            el.innerHTML = '';
            const list = Array.isArray(values) ? values : [];
            if (list.length === 0) {
                const opt = document.createElement('option');
                opt.value = '';
                opt.textContent = '(not available)';
                el.appendChild(opt);
                return;
            }
            list.forEach(v => {
                const opt = document.createElement('option');
                opt.value = String(v);
                opt.textContent = String(v);
                el.appendChild(opt);
            });
            if (previous && list.includes(previous)) el.value = previous;
        }

        async function loadSettingCandidates() {
            const r = await post('/api/setting_candidates');
            if (!r.ok) return;
            refillSelect(exposureModeEl, r.result.exposureMode || []);
            refillSelect(movieQualityEl, r.result.movieQuality || []);
            refillSelect(movieFileFormatEl, r.result.movieFileFormat || []);
        }

        async function applyCameraSettings() {
            const payload = {
                exposureMode: exposureModeEl.value,
                movieQuality: movieQualityEl.value,
                movieFileFormat: movieFileFormatEl.value,
            };
            const r = await postJson('/api/apply_settings', payload);
            setStatus(r.ok ? 'Camera settings applied.' : ('Apply settings failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshStatus();
        }

        async function refreshStatus() {
            try {
                const res = await fetch('/api/status');
                if (!res.ok) {
                    setStatus('Status endpoint unavailable: HTTP ' + res.status, 'bad');
                    return;
                }
                const s = await res.json();
                const now = Date.now();
                const dt = Math.max((now - lastStatusTs) / 1000, 0.001);
                const fps = ((s.frameCount - lastFrameCount) / dt).toFixed(1);
                lastStatusTs = now;
                lastFrameCount = s.frameCount;

                const modeText = s.transferActive ? 'Transfer mode active' : (s.streamingEnabled ? 'Preview mode active' : 'Idle / waiting for camera mode');
                const modeTone = s.transferActive ? 'warn' : (s.streamingEnabled ? 'good' : '');
                setChip(modeChipEl, modeText, modeTone);
                setChip(streamChipEl, s.streamingEnabled ? 'Preview streaming' : 'Preview stopped', s.streamingEnabled ? 'good' : '');
                setChip(transferChipEl, s.transferItemCount > 0 ? `Transfer files ready (${s.transferItemCount})` : 'Transfer list empty', s.transferItemCount > 0 ? 'warn' : '');
                setChip(recordChipEl, s.movieRecording ? 'Recording now' : 'Recording idle', s.movieRecording ? 'bad' : '');

                framesStatEl.textContent = String(s.frameCount || 0);
                fpsStatEl.textContent = String(fps);
                filesStatEl.textContent = String(s.transferItemCount || 0);
                batteryStatEl.textContent = s.batteryLevel ?? 'n/a';

                setStatus(`${modeText}. Preview=${s.streamingEnabled}, transferFiles=${s.transferItemCount || 0}, recording=${s.movieRecording}, source=${s.sourceMode || 'n/a'}.`, s.lastCameraError ? 'bad' : (s.streamingEnabled || s.transferItemCount > 0 ? 'good' : ''));
                diagEl.textContent = `backend_fps≈${fps}, liveviewUrl=${s.liveviewUrl || 'n/a'}, sourceMode=${s.sourceMode || 'n/a'}, stillsIntervalMs=${s.stillsIntervalMs || 'n/a'}, exposureMode=${s.exposureMode || 'n/a'}, movieQuality=${s.movieQuality || 'n/a'}, movieFileFormat=${s.movieFileFormat || 'n/a'}, battery=${s.batteryLevel ?? 'n/a'}, focus=${s.focusStatus || 'n/a'}, cameraStatus=${s.cameraStatus || 'n/a'}, transferActive=${s.transferActive || false}, transferItems=${s.transferItemCount || 0}, transferBundleBuilding=${s.transferBundleBuilding || false}, transferBundleReady=${s.transferBundleReady || false}, transferError=${s.transferLastError || 'none'}, lastCameraError=${s.lastCameraError || 'none'}`;
                transferMetaEl.textContent = `Files listed: ${s.transferItemCount || 0}. Transfer error: ${s.transferLastError || 'none'}`;
                bundleMetaEl.textContent = s.transferBundleBuilding
                    ? 'Building ZIP from listed transfer files…'
                    : (s.transferBundleReady ? `ZIP ready: ${s.transferBundleName || 'sony-transfer.zip'}` : 'No bulk bundle built yet.');

                if (s.latestImageTs) {
                    latestImageMetaEl.textContent = `HQ image cached at ${s.latestImageTs}, size ${fmtBytes(s.latestImageBytes || 0)}.`;
                }
                if (s.stillsIntervalMs) stillsMsEl.value = s.stillsIntervalMs;
                if (s.exposureMode) exposureModeEl.value = s.exposureMode;
                if (s.movieQuality) movieQualityEl.value = s.movieQuality;
                if (s.movieFileFormat) movieFileFormatEl.value = s.movieFileFormat;
            } catch (e) {
                setStatus('Cannot reach backend: ' + e, 'bad');
            }
        }

        async function refreshCameraInfo() {
            const r = await post('/api/camera_info');
            if (!r.ok) {
                camInfoEl.textContent = 'Camera info error: ' + JSON.stringify(r.result || r.error);
                return;
            }
            const i = r.result || {};
            camInfoEl.textContent = `status=${i.cameraStatus || 'n/a'}, focus=${i.focusStatus || 'n/a'}, battery=${i.batteryLevel ?? 'n/a'}, shootMode=${i.shootMode || 'n/a'}, exposure=${i.exposureMode || 'n/a'}, iso=${i.isoSpeedRate || 'n/a'}, shutter=${i.shutterSpeed || 'n/a'}, fNumber=${i.fNumber || 'n/a'}`;
            if (i.exposureMode) exposureModeEl.value = i.exposureMode;
            if (i.movieQuality) movieQualityEl.value = i.movieQuality;
            if (i.movieFileFormat) movieFileFormatEl.value = i.movieFileFormat;
        }

        async function applyStillsInterval() {
            const ms = Math.max(150, Number(stillsMsEl.value || 500));
            const r = await postJson('/api/set_stills_interval', { ms });
            setStatus(r.ok ? `HQ interval stored (${r.result.stillsIntervalMs} ms), though HQ still mode stays disabled.` : ('Failed to set HQ interval: ' + JSON.stringify(r.error || r.result)), r.ok ? '' : 'bad');
            await refreshStatus();
        }

        async function halfPressStart() {
            const r = await post('/api/half_press_start');
            setStatus(r.ok ? 'Half-press engaged.' : ('Half-press start failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshCameraInfo();
            await refreshStatus();
        }

        async function halfPressStop() {
            const r = await post('/api/half_press_stop');
            setStatus(r.ok ? 'Half-press released.' : ('Half-press stop failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshCameraInfo();
            await refreshStatus();
        }

        async function shutterClick() {
            const r = await post('/api/shutter_click');
            setStatus(r.ok ? 'Shutter triggered.' : ('Shutter failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshCameraInfo();
            await refreshStatus();
        }

        async function startMovie() {
            const r = await post('/api/start_movie');
            setStatus(r.ok ? 'Camera recording started.' : ('Start recording failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshStatus();
        }

        async function stopMovie() {
            const r = await post('/api/stop_movie');
            setStatus(r.ok ? 'Camera recording stopped.' : ('Stop recording failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshStatus();
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
            }
            setStatus(r.ok ? 'Preview mode started.' : ('Start preview failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshStatus();
        }

        async function stopLiveview() {
            const r = await post('/api/stop_liveview');
            stopPreview();
            setStatus(r.ok ? 'Preview mode stopped.' : ('Stop preview returned: ' + JSON.stringify(r.result || r.error)), r.ok ? '' : 'bad');
            await refreshStatus();
        }

        async function switchToPreviewMode() {
            await stopTransferMode(false);
            await startLiveview();
            await refreshCameraInfo();
        }

        async function fetchLatestImage() {
            const r = await post('/api/fetch_latest_image');
            if (r.ok) {
                latestImageEl.src = '/latest_image.jpg?t=' + Date.now();
                latestImageMetaEl.textContent = `HQ image cached, size ${fmtBytes(r.result.bytes || 0)}, ts=${r.result.timestamp || 'n/a'}.`;
            }
            setStatus(r.ok ? 'Latest image fetched.' : ('Fetch latest image failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            await refreshStatus();
        }

        async function startTransferMode() {
            const r = await post('/api/transfer/start');
            setStatus(r.ok ? 'Transfer mode requested.' : ('Transfer mode start failed: ' + JSON.stringify(r.result || r.error)), r.ok ? 'good' : 'bad');
            return r;
        }

        async function switchToTransferMode() {
            await stopLiveview();
            await startTransferMode();
            await listTransferFiles();
            await refreshStatus();
        }

        async function stopTransferMode(showStatus = true) {
            const r = await post('/api/transfer/stop');
            if (showStatus) {
                setStatus(r.ok ? 'Transfer mode ended.' : ('Transfer mode stop failed: ' + JSON.stringify(r.result || r.error)), r.ok ? '' : 'bad');
            }
            await refreshStatus();
            return r;
        }

        async function listTransferFiles() {
            transferMetaEl.textContent = 'Loading transfer file list…';
            const r = await postJson('/api/transfer/list', { limit: 500 });
            if (!r.ok) {
                transferListEl.innerHTML = '<div class="empty">Transfer list failed.</div>';
                transferMetaEl.textContent = 'List failed: ' + JSON.stringify(r.result || r.error);
                setStatus('Transfer list failed.', 'bad');
                await refreshStatus();
                return;
            }

            const items = (r.result && r.result.items) ? r.result.items : [];
            transferMetaEl.textContent = `Loaded ${items.length} file(s) from the camera.`;
            if (items.length === 0) {
                transferListEl.innerHTML = '<div class="empty">No files found.</div>';
            } else {
                transferListEl.innerHTML = items.map(it => `
                    <div class="file-row">
                        <div><a href="${it.downloadPath}" target="_blank">${esc(it.title || ('item-' + it.id))}</a></div>
                        <div class="path">${esc(it.container || '')}</div>
                        <div class="size">${fmtBytes(it.size || 0)}</div>
                    </div>
                `).join('');
            }
            setStatus('Transfer list updated.', 'good');
            await refreshStatus();
        }

        async function buildTransferBundle() {
            bundleMetaEl.textContent = 'Building ZIP from currently listed files… this can take a while for videos.';
            setStatus('Building transfer ZIP…', '');
            const r = await postJson('/api/transfer/build_bundle', { limit: 500 });
            if (!r.ok) {
                bundleMetaEl.textContent = 'ZIP build failed: ' + JSON.stringify(r.result || r.error);
                setStatus('Bulk ZIP build failed.', 'bad');
                await refreshStatus();
                return;
            }
            bundleMetaEl.textContent = `ZIP build started for ${r.result.count || 0} item(s). Waiting for completion…`;
            setStatus('Bulk ZIP build started.', 'good');
            const ready = await waitForBundleReady(180);
            if (ready) {
                window.location.href = (r.result.bundlePath || '/transfer/download_all.zip') + '?t=' + Date.now();
            }
        }

        async function waitForBundleReady(maxSeconds) {
            const deadline = Date.now() + (maxSeconds * 1000);
            while (Date.now() < deadline) {
                await refreshStatus();
                if (bundleMetaEl.textContent.includes('ZIP ready:')) {
                    setStatus('Bulk ZIP built successfully.', 'good');
                    return true;
                }
                if (bundleMetaEl.textContent.includes('failed')) {
                    setStatus('Bulk ZIP build failed.', 'bad');
                    return false;
                }
                await new Promise(resolve => setTimeout(resolve, 1500));
            }
            setStatus('Bulk ZIP build timed out waiting for completion.', 'bad');
            return false;
        }

        loadSettingCandidates();
        refreshStatus();
        refreshCameraInfo();
        statusTimer = setInterval(refreshStatus, 2500);
    </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Watch Sony camera liveview in browser with recording controls")
    parser.add_argument("--address", default="192.168.122.1", help="Camera IP address")
    parser.add_argument("--camera-port", type=int, default=10000, help="Sony JSON API port")
    parser.add_argument("--wifi-interface", default="auto", help="Wi-Fi adapter used for camera Wi-Fi Direct, or 'auto' to detect it")
    parser.add_argument("--wifi-password", default=None, help="Camera Wi-Fi Direct password (optional, for auto-connect)")
    parser.add_argument("--listen", default="127.0.0.1", help="Web UI bind address")
    parser.add_argument("--port", type=int, default=8765, help="Web UI port")
    parser.add_argument("--stills-interval-ms", type=int, default=500, help="HQ still capture interval in milliseconds")
    args = parser.parse_args()

    camera = SonyCameraClient(args.address, args.camera_port)
    state = AppState(camera, wifi_interface=args.wifi_interface, wifi_password=args.wifi_password)
    state.stills_interval_ms = max(150, min(5000, int(args.stills_interval_ms)))

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
