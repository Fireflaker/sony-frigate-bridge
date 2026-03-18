# USB & PCIe Passthrough Guide for TrueNAS/Incus

## Hardware Tested

| Device | USB/PCIe | Vendor:Product | Status |
|--------|----------|----------------|--------|
| Realtek RTL8812AU | USB | 0bda:8812 | ✅ Working |
| Intel AX200 | PCIe | 8086:2723 | ❌ Not available |

## Realtek RTL8812AU USB Passthrough

### Prerequisites
- TrueNAS with Incus VMs
- Realtek USB Wi-Fi adapter physically connected to TrueNAS host
- Incus VM already created (e.g., `sonybridge`)

### Steps

#### 1. Identify USB Device

On TrueNAS host:
```bash
lsusb | grep -i realtek
# Output: Bus 001 Device 005: ID 0bda:8812 Realtek Semiconductor Corp.
```

Or:
```bash
incus query /1.0/devices | jq '.[] | select(.type=="usb")'
```

#### 2. Configure Passthrough

Add USB device to VM:
```bash
incus config device add sonybridge wifi usb \
  vendorid=0bda \
  productid=8812 \
  required=false
```

**Parameters:**
- `vendorid`: Vendor ID (0bda for Realtek)
- `productid`: Product ID (8812 for RTL8812AU)
- `required=false`: Don't fail VM boot if device missing

#### 3. Verify in VM

Inside the VM:
```bash
lsusb
# Should show: Realtek ... RTL8812AU

lspci -k
# Alternative view if enumerated as network device
```

Check for wireless interface:
```bash
ip link show
# Look for wlx* interface (e.g., wlx00c0caa60b48)

iwconfig
# Should show the wireless adapter
```

#### 4. Connect to Wi-Fi

```bash
# List available networks
nmcli device wifi list --rescan yes

# Connect to camera
nmcli device wifi connect "DIRECT-n6E1:ILCE-6400" \
  password "your_camera_wifi_password" \
  ifname wlx00c0caa60b48

# Verify connection
ip addr show wlx00c0caa60b48
# Should show 192.168.122.x address
```

## Intel AX200 PCIe Passthrough (Not Available)

### Why It Failed

**Discovery Process:**
```bash
# On TrueNAS host:
lspci | grep Intel | grep Wireless
# Output: 00:14.3 Network controller: Intel Corporation Wi-Fi 6 AX200

lspci -n | grep 8086:2723
# Output: 00:14.3 0280: 8086:2723 (rev 1a)
```

**Attempted Passthrough:**
```bash
incus config device add sonybridge ax200 pci \
  pcislot="00:14.3" \
  required=false
```

**Result:**
```
Error: Failed to hot-plug device 'ax200': device not found
```

### Root Causes
1. **IOMMU Groups**: On this system, the AX200 is in a shared IOMMU group with other devices, making isolated passthrough impossible.
2. **Firmware Constraints**: TrueNAS may have disabled individual device passthrough for this slot.
3. **Driver Binding**: The device remains bound to host driver (iwlwifi), preventing VM access.

### Verification Commands

Check IOMMU groups:
```bash
find /sys/kernel/iommu_groups/ -type l | grep 8086:2723
# If shared with other devices, passthrough is not safe
```

Check device binding:
```bash
lspci -vvv -s 00:14.3 | grep -i driver
# If "Driver: iwlwifi" (host-bound), VM cannot claim it

ls -la /sys/bus/pci/drivers/iwlwifi/ | grep 0000:00:14.3
# Presence confirms host binding
```

### Alternatives
1. ✅ **Use USB Wi-Fi adapter** (Realtek, TP-Link, etc.)
2. ⚠️ **Unbind host driver** (risky, can break host networking)
3. ❌ **PCI bridge isolation** (not available in this TrueNAS version)

## General Passthrough Guidelines

### Safe USB Passthrough Checklist

- [ ] Device is USB (not root hub or controller)
- [ ] Device vendor/product IDs correctly identified
- [ ] Tested on host before passthrough
- [ ] `required=false` set (graceful degradation if removed)
- [ ] VM has sufficient access permissions
- [ ] Guest OS drivers available

### PCIe Passthrough Checklist

- [ ] IOMMU enabled in BIOS
- [ ] Device in isolated IOMMU group (via `iommu_groups/`)
- [ ] Host driver can be safely unbound
- [ ] Guest OS supports device drivers
- [ ] VM has direct DMA access needs
- [ ] Performance gains justify isolation complexity

## Troubleshooting

### Device Not Appearing in VM
```bash
# Rescan VM devices
incus config device list sonybridge

# Check hot-plug status
dmesg | tail -20  # In VM

# Verify host export
incus query /1.0/devices/sonybridge
```

### Wi-Fi Connection Failures
```bash
# Check adapter status inside VM
rfkill list
# If "Soft blocked: yes", unblock:
rfkill unblock wifi

# Monitor connection attempts
journalctl -u NetworkManager -f  # In VM
```

### IOMMU Error
```bash
# Check if IOMMU is actually enabled
grep IOMMU /proc/cmdline

# View group assignments
for group in $(find /sys/kernel/iommu_groups -type d -name groups | sort -V); do
  echo "IOMMU group ${group##*/}:"
  for device in $group/devices/*/; do
    echo -n $'\t'
    lspci -nns ${device##*/}
  done
done
```

## Performance Notes

- **USB 3.0**: Typical USB Wi-Fi adapters over USB 3.0 see minimal passthrough overhead
- **PCIe**: Direct passthrough offers near-native performance if possible
- **Latency**: Expect 5-20% latency increase vs. native passthrough
