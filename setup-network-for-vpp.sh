#!/bin/bash
set -e

#######################################
# Default values
#######################################
DPDK_KMODS_DIR="/tmp/dpdk-kmods"
VETH_HOST="vpp1host"
VETH_PEER="vpp1out"
VETH_HOST_IP="172.16.1.4/24"
CREATE_VETH=false
DRIVER_MODE=""  # empty = auto-detect, "uio" or "vfio" for manual override

#######################################
# Script setup
#######################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIND_SCRIPT="${SCRIPT_DIR}/dpdk-bind-and-record.py"

# Track if we've made changes for cleanup on failure
DRIVER_BOUND=false
VETH_CREATED=false

cleanup() {
  local exit_code=$?
  if [ $exit_code -ne 0 ]; then
    # Only run cleanup if we've actually made changes
    if [ "$VETH_CREATED" = true ] || [ "$DRIVER_BOUND" = true ]; then
      echo "Error occurred (exit code: $exit_code). Cleaning up..." >&2
      if [ "$VETH_CREATED" = true ]; then
        echo "Removing veth pair..." >&2
        sudo ip link del "${VETH_PEER}" 2>/dev/null || true
      fi
      if [ "$DRIVER_BOUND" = true ]; then
        echo "Attempting to restore original driver..." >&2
        sudo "${BIND_SCRIPT}" -u --force 2>/dev/null || true
      fi
    fi
  fi
  exit $exit_code
}
trap cleanup EXIT

#######################################
# Usage
#######################################
usage() {
  cat <<EOF >&2
Usage: $0 [OPTIONS] <interface-name>

Bind a network interface to a DPDK-compatible driver for use with VPP.

Arguments:
  <interface-name>    Network interface to bind (e.g., eth1, enp0s3)

Options:
  -m, --mode <MODE>   Driver mode: 'uio' or 'vfio' (default: auto-detect)
  -v, --veth          Create a veth pair for host communication (default: off)
  --veth-ip <IP/MASK> IP address for veth host interface (default: ${VETH_HOST_IP})
  -h, --help          Show this help message

Examples:
  $0 eth1                         # Bind eth1 with auto-detected driver
  $0 -m vfio eth1                 # Bind eth1 using VFIO driver
  $0 -m uio eth1                  # Bind eth1 using UIO driver
  $0 --veth eth1                  # Bind eth1 and create veth pair
  $0 --veth --veth-ip 10.0.0.1/24 eth1  # Bind eth1 with custom veth IP
EOF
  exit 1
}

#######################################
# Argument parsing
#######################################
INTERFACE=""

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--mode)
      if [ -z "$2" ] || [[ "$2" == -* ]]; then
        echo "Error: --mode requires an argument (uio or vfio)" >&2
        exit 1
      fi
      if [[ "$2" != "uio" && "$2" != "vfio" ]]; then
        echo "Error: --mode must be 'uio' or 'vfio', got '$2'" >&2
        exit 1
      fi
      DRIVER_MODE="$2"
      shift 2
      ;;
    -v|--veth)
      CREATE_VETH=true
      shift
      ;;
    --veth-ip)
      if [ -z "$2" ] || [[ "$2" == -* ]]; then
        echo "Error: --veth-ip requires an IP/MASK argument" >&2
        exit 1
      fi
      VETH_HOST_IP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "Error: Unknown option: $1" >&2
      usage
      ;;
    *)
      if [ -n "$INTERFACE" ]; then
        echo "Error: Multiple interfaces specified" >&2
        usage
      fi
      INTERFACE="$1"
      shift
      ;;
  esac
done

if [ -z "$INTERFACE" ]; then
  echo "Error: No interface specified" >&2
  usage
fi

# Validate interface exists
if ! ip link show "$INTERFACE" >/dev/null 2>&1; then
  echo "Error: Interface '$INTERFACE' does not exist." >&2
  echo "Available interfaces:" >&2
  ip -br link show | awk '{print "  " $1}' >&2
  exit 1
fi

# Check if bind script exists
if [ ! -x "$BIND_SCRIPT" ]; then
  echo "Error: Bind script not found or not executable: $BIND_SCRIPT" >&2
  exit 1
fi

#######################################
# Detect virtualization environment (only if mode not specified)
#######################################
if [ -z "$DRIVER_MODE" ]; then
  if VIRT_ENV=$(systemd-detect-virt 2>/dev/null); then
    echo "Detected virtualization environment: $VIRT_ENV"
  else
    VIRT_ENV="none"
    echo "Could not detect virtualization environment. Assuming bare-metal."
  fi
fi

#######################################
# Helper functions
#######################################
module_is_loaded() {
  lsmod | grep -q "^$1[[:space:]]"
}

load_vfio() {
  echo "Loading VFIO drivers..."

  if module_is_loaded "vfio"; then
    echo "  vfio module already loaded"
  else
    sudo modprobe vfio
  fi

  # Enable noiommu mode (required for environments without IOMMU)
  sudo sh -c "echo 1 > /sys/module/vfio/parameters/enable_unsafe_noiommu_mode"

  if module_is_loaded "vfio_pci"; then
    echo "  vfio-pci module already loaded"
  else
    sudo modprobe vfio-pci
  fi

  echo "Binding $INTERFACE to vfio-pci..."
  sudo "${BIND_SCRIPT}" -d vfio-pci -i "$INTERFACE" -b --noiommu-mode
  DRIVER_BOUND=true
}

load_uio() {
  echo "Loading UIO drivers..."

  if module_is_loaded "uio"; then
    echo "  uio module already loaded"
  else
    sudo modprobe uio
  fi

  # Check if igb_uio is already loaded
  if module_is_loaded "igb_uio"; then
    echo "  igb_uio module already loaded"
  else
    # Clone dpdk-kmods if not present
    if [ ! -d "$DPDK_KMODS_DIR" ]; then
      echo "dpdk-kmods directory not found. Cloning..."
      git clone http://dpdk.org/git/dpdk-kmods "$DPDK_KMODS_DIR"
    fi

    # Build igb_uio module
    echo "Building igb_uio module..."
    make -C "/lib/modules/$(uname -r)/build" M="$DPDK_KMODS_DIR/linux/igb_uio" modules

    # Load the module
    echo "Loading igb_uio module..."
    sudo insmod "$DPDK_KMODS_DIR/linux/igb_uio/igb_uio.ko" wc_activate=1
  fi

  echo "Binding $INTERFACE to igb_uio..."
  sudo "${BIND_SCRIPT}" -d igb_uio -i "$INTERFACE" -b
  DRIVER_BOUND=true
}

#######################################
# Main: Load appropriate driver
#######################################
if [ -n "$DRIVER_MODE" ]; then
  # Manual override specified
  echo "Using manually specified driver mode: $DRIVER_MODE"
  if [ "$DRIVER_MODE" = "vfio" ]; then
    load_vfio
  else
    load_uio
  fi
else
  # Auto-detect based on virtualization environment
  case "$VIRT_ENV" in
    qemu|kvm)
      load_vfio
      ;;
    amazon)
      load_uio
      ;;
    microsoft)
      echo "Error: Microsoft Hyper-V virtualization is not supported for DPDK." >&2
      echo "DPDK requires direct hardware access which is not available in Hyper-V." >&2
      exit 1
      ;;
    none)
      echo "Bare-metal environment detected. Using VFIO (preferred for security)."
      load_vfio
      ;;
    *)
      echo "Unrecognized virtualization environment: $VIRT_ENV"
      echo "Defaulting to UIO driver."
      load_uio
      ;;
  esac
fi

#######################################
# Optional: Create veth pair
#######################################
if [ "$CREATE_VETH" = true ]; then
  echo ""
  echo "Setting up veth pair for host communication..."

  if ip link show "${VETH_HOST}" >/dev/null 2>&1; then
    echo "  veth pair already exists (${VETH_HOST} <-> ${VETH_PEER})"
  else
    echo "  Creating veth pair ${VETH_HOST} <-> ${VETH_PEER}..."
    sudo ip link add name "${VETH_PEER}" type veth peer name "${VETH_HOST}"
    VETH_CREATED=true
    sudo ip link set dev "${VETH_PEER}" up
    sudo ip link set dev "${VETH_HOST}" up
    sudo ip addr add "${VETH_HOST_IP}" dev "${VETH_HOST}"
    echo "  Assigned ${VETH_HOST_IP} to ${VETH_HOST}"
  fi
fi

#######################################
# Summary
#######################################
echo ""
echo "Setup complete!"
echo "  Interface: $INTERFACE bound to DPDK driver"
if [ "$CREATE_VETH" = true ]; then
  echo "  Veth pair: ${VETH_HOST} (${VETH_HOST_IP}) <-> ${VETH_PEER}"
fi
