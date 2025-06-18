#!/bin/bash
set -e

cd ~/vpp/build-root/ || {
  echo "ERROR: Failed to enter vpp/build-root"
  exit 1
}

# Check for existence of any .deb files
if ! ls *.deb >/dev/null 2>&1; then
  echo "ERROR: No .deb files found in $(pwd). Make sure the build completed successfully."
  echo "make -C vpp pkg-deb"
  cd -
  exit 1
fi

# Extract version from vpp_*.deb
ver=$(basename vpp_*.deb | sed -E 's/^vpp_(.*)\.deb$/\1/' | head -n 1)

if [[ -z "$ver" ]]; then
  echo "ERROR: Failed to determine VPP version from vpp_*.deb"
  exit 1
fi

echo "Installing VPP packages with version: $ver"

sudo dpkg -i ./libvppinfra_${ver}.deb
sudo dpkg -i ./libvppinfra-dev_${ver}.deb
sudo dpkg -i ./vpp_${ver}.deb
sudo dpkg -i ./vpp-dev_${ver}.deb
sudo dpkg -i ./vpp-crypto-engines_${ver}.deb
sudo dpkg -i ./vpp-plugin-core_${ver}.deb
sudo dpkg -i ./vpp-plugin-devtools_${ver}.deb
sudo dpkg -i ./vpp-plugin-dpdk_${ver}.deb
