# Sony Camera JSON-RPC API Notes

## Discovered via Reverse Engineering

### Camera Information
- **Model**: Sony a6400
- **Protocol**: HTTP JSON-RPC 2.0 over local network
- **Default Port**: 10000
- **Authentication**: None (local network only)

### Endpoint
```
http://192.168.122.1:10000/sony/camera
```

### Key Methods

#### `startLiveview`
Starts the camera's liveview stream.

**Request:**
```json
{
  "method": "startLiveview",
  "params": [{"liveviewSize": "L"}],
  "id": 1,
  "version": "1.0"
}
```

**Response:**
```json
{
  "result": [
    "http://192.168.122.1:60152/liveviewstream?url=...token..."
  ],
  "id": 1
}
```

**Notes:**
- `liveviewSize` options: `"L"` (large, ~1920x1080), `"M"` (medium), `"S"` (small)
- Returns a single-use or short-lived tokenized URL
- Stream is MJPEG format
- Port is dynamic (60152 in this case)

#### `stopLiveview`
Stops the current liveview stream.

**Request:**
```json
{
  "method": "stopLiveview",
  "params": [],
  "id": 1,
  "version": "1.0"
}
```

#### `getAvailableApiList`
Lists all available methods on the camera.

**Request:**
```json
{
  "method": "getAvailableApiList",
  "params": [],
  "id": 1,
  "version": "1.0"
}
```

### Wi-Fi Connection

#### Ad-Hoc / Wi-Fi Direct Mode
When the Sony a6400 is in "Wi-Fi Direct" mode:
- **SSID**: `DIRECT-XXXX:ILCE-6400` (e.g., `DIRECT-n6E1:ILCE-6400`)
- **Security**: WPA2
- **Password**: Default or custom (set on camera)
- **Gateway**: Usually `192.168.122.1`

#### Detection

**Via `nmcli` (NetworkManager):**
```bash
nmcli device wifi list --rescan yes
```

May list the camera SSID, but detection can be flaky in VMs.

**Fallback (TCP Reachability):**
Instead of relying on SSID detection, verify the camera is reachable:
```bash
python3 -c "import socket; socket.create_connection(('192.168.122.1', 10000), timeout=2.5)"
```

If this succeeds, the camera is reachable regardless of SSID detection status.

### Error Handling

**NVML Error (Host-side):**
```
nvidia-container-cli: detection error: nvml error: unknown error: unknown
```
This occurs when Frigate is configured with TensorRT/NVIDIA GPU acceleration but the host lacks proper NVIDIA drivers. Solution: Use CPU-only Frigate image.

**SSID Not Found (Bridge-side):**
```
Wi-Fi Direct connect failed: Error: No network with SSID '...' found.
```
This is a false negative if the camera is actually reachable. Solution: Use TCP reachability fallback.

### Timing & Performance

- **Stream Latency**: ~200-500ms (depends on Wi-Fi signal strength)
- **Frame Rate**: 15-30 FPS (configurable, default 30)
- **Resolution**: Up to 1920x1080 (varies by mode)
- **Connection Timeout**: Recommend 5-10 seconds

### Notes for Integration

1. **Single Connection**: Camera typically supports only one liveview stream at a time. If multiple clients try to start liveview, only one will succeed.

2. **Token Expiration**: The returned liveview URL includes a token that may expire if not consumed quickly. Test immediate consumption after calling `startLiveview`.

3. **Camera Standby**: Camera may enter standby or power-save mode after inactivity. Wake it before expecting streams.

4. **Network Stability**: Wi-Fi Direct is less stable than standard Wi-Fi. Recommend bridge placement close to camera.

## References

- Official Sony API docs: https://developer.sony.com/ (limited)
- ImagingEdge source: Based on reverse-engineering Sony's desktop software
- MJPEG over HTTP is widely supported by Frigate and other RTMP servers
