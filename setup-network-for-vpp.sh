#!/bin/bash
set -e

if [ $# -ne 1 ]; then
  echo "Usage: $0 <interface-name>"
  exit 1
fi

INTERFACE="$1"

# Detect virtualization environment
VIRT_ENV=$(systemd-detect-virt)
echo "Detected virtualization environment: $VIRT_ENV"

load_vfio() {
  echo "Loading VFIO drivers..."
  sudo modprobe vfio
  sudo sh -c "echo 1 > /sys/module/vfio/parameters/enable_unsafe_noiommu_mode"
  sudo modprobe vfio-pci
  sudo ./dpdk-bind-and-record.py -d vfio-pci -i "$INTERFACE" -b
}

load_uio() {
  echo "Loading UIO drivers..."
  sudo modprobe uio
  DPDK_KMODS_DIR=~/dpdk-kmods
  if [ ! -d "$DPDK_KMODS_DIR" ]; then
    echo "dpdk-kmods directory not found. Cloning..."
    git clone git://dpdk.org/dpdk-kmods "$DPDK_KMODS_DIR"
    if [ $? -ne 0 ]; then
      echo "Error: Failed to clone dpdk-kmods repository." >&2
      exit 1
    fi
  fi
  make -C "$DPDK_KMODS_DIR/linux/igb_uio"
  if [ $? -ne 0 ]; then
    echo "Error: Failed to build igb_uio driver." >&2
    exit 1
  fi
  sudo insmod "$DPDK_KMODS_DIR/linux/igb_uio/igb_uio.ko" wc_activate=1
  sudo ./dpdk-bind-and-record.py -d igb_uio -i "$INTERFACE" -b
}

case "$VIRT_ENV" in
qemu)
  load_vfio
  ;;
amazon)
  load_uio
  ;;
microsoft)
  echo "Microsoft virtualization detected. No driver loaded."
  ;;
*)
  echo "Unrecognized or bare-metal environment. Defaulting to VFIO."
  load_vfio
  ;;
esac

# Common setup for all environments
VETH_HOST="vpp1host"
VETH_PEER="vpp1out"
if ip link show "${VETH_HOST}" >/dev/null 2>&1; then
  echo "Interface $VETH_HOST exists."
else
  echo "Creating veth pair $VETH_HOST <-> $VETH_PEER..."
  sudo ip link add name "${VETH_PEER}" type veth peer name $VETH_HOST
  sudo ip link set dev $VETH_PEER up
  sudo ip link set dev $VETH_HOST up
  sudo ip addr add 172.16.1.4/24 dev $VETH_HOST
fi
