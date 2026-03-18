#!/bin/bash
# Health check script for Sony bridge and Frigate integration

BRIDGE_URL="${1:-http://localhost:8765}"
FRIGATE_HOST="${2:-http://10.0.0.101:5000}"

echo "=== Sony Bridge & Frigate Health Check ==="
echo "Bridge: $BRIDGE_URL"
echo "Frigate: $FRIGATE_HOST"
echo ""

# Check bridge status
echo "[1] Bridge Status"
if bridge_status=$(curl -s "${BRIDGE_URL}/api/status"); then
  if echo "$bridge_status" | grep -q '"ok": true'; then
    echo "  ✓ Bridge API responding"
    
    streaming=$(echo "$bridge_status" | grep -o '"streamingEnabled": [^,}]*' | cut -d: -f2)
    frame_count=$(echo "$bridge_status" | grep -o '"frameCount": [0-9]*' | cut -d: -f2)
    
    echo "  - Streaming: $streaming"
    echo "  - Frame Count: $frame_count"
    
    if echo "$bridge_status" | grep -q '"lastCameraError": null'; then
      echo "  - Camera Error: None"
    else
      error=$(echo "$bridge_status" | grep -o '"lastCameraError": "[^"]*"' | cut -d'"' -f4)
      echo "  - Camera Error: $error"
    fi
  else
    echo "  ✗ Bridge API error"
    echo "$bridge_status" | head -3
  fi
else
  echo "  ✗ Cannot reach bridge at $BRIDGE_URL"
fi

echo ""

# Check JPEG stream
echo "[2] Frame Capture"
if curl -s -m 5 "${BRIDGE_URL}/frame.jpg" -o /tmp/sony_frame.jpg 2>/dev/null; then
  size=$(stat -f%z /tmp/sony_frame.jpg 2>/dev/null || stat -c%s /tmp/sony_frame.jpg 2>/dev/null)
  magic=$(xxd -p -l 2 /tmp/sony_frame.jpg 2>/dev/null)
  
  if [ "$magic" = "ffd8" ]; then
    echo "  ✓ Valid JPEG frame"
    echo "  - Size: $size bytes"
  else
    echo "  ⚠ Frame is not JPEG (magic: $magic)"
  fi
else
  echo "  ✗ Cannot fetch frame"
fi

echo ""

# Check Frigate
echo "[3] Frigate Status"
if frigate_status=$(curl -s "${FRIGATE_HOST}/api/config"); then
  if echo "$frigate_status" | grep -q '"cameras"'; then
    echo "  ✓ Frigate API responding"
    
    camera_count=$(echo "$frigate_status" | grep -o '"cameras": {' | wc -l)
    echo "  - Cameras configured: $camera_count"
  else
    echo "  ✗ Frigate API malformed"
  fi
else
  echo "  ✗ Cannot reach Frigate at $FRIGATE_HOST"
fi

echo ""

# Check connectivity
echo "[4] Network Connectivity"
if timeout 2 bash -c "echo >/dev/tcp/192.168.122.1/10000" 2>/dev/null; then
  echo "  ✓ Camera (192.168.122.1:10000) reachable"
else
  echo "  ✗ Camera not reachable"
fi

echo ""
echo "=== End Health Check ==="
