#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
# Copyright(c) 2010-2014 Intel Corporation
#

import sys
import os
import platform
import subprocess
import argparse
import netifaces
import json

from glob import glob
from os.path import exists, basename
from os.path import join as path_join
from pprint import pprint

file_name_for_saved_data = "dpdk-bind-and-record.json"
network_class = {'Class': '02', 'Vendor': None, 'Device': None,
                 'SVendor': None, 'SDevice': None}

network_devices = [network_class]

# global dict ethernet devices present. Dictionary indexed by PCI address.
# Each device within this is itself a dictionary of device properties
devices = {}
# global dict for the selected device
device = {}
# list of supported DPDK drivers
dpdk_drivers = ["igb_uio", "vfio-pci", "uio_pci_generic"]
# list of currently loaded kernel modules
loaded_modules = None

# command-line arg flags
b_flag = None
info_flag = False
args_dev = []
driver = "igb_uio"
force_flag = False
noiommu_flag = False

# check if a specific kernel module is loaded
def module_is_loaded(module):
    global loaded_modules

    if module == 'vfio_pci':
        module = 'vfio-pci'

    if loaded_modules:
        return module in loaded_modules

    # Get list of sysfs modules (both built-in and dynamically loaded)
    sysfs_path = '/sys/module/'

    # Get the list of directories in sysfs_path
    sysfs_mods = [m for m in os.listdir(sysfs_path)
                  if os.path.isdir(os.path.join(sysfs_path, m))]

    # special case for vfio_pci (module is named vfio-pci,
    # but its .ko is named vfio_pci)
    sysfs_mods = [a if a != 'vfio_pci' else 'vfio-pci' for a in sysfs_mods]

    loaded_modules = sysfs_mods

    # add built-in modules as loaded
    release = platform.uname().release
    filename = os.path.join("/lib/modules/", release, "modules.builtin")
    if os.path.exists(filename):
        try:
            with open(filename) as f:
                loaded_modules += [os.path.splitext(os.path.basename(mod))[0] for mod in f]
        except IOError:
            print("Warning: cannot read list of built-in kernel modules")

    return module in loaded_modules

def check_dpdk_modules():
    '''Checks that at least one DPDK driver is loaded'''
    global dpdk_drivers

    # list of supported modules
    mods = [{"Name": driver, "Found": False} for driver in dpdk_drivers]

    # Check all modules to see if they are loaded
    for mod in mods:
        if module_is_loaded(mod["Name"]):
            mod["Found"] = True

    # check if we have at least one loaded module
    if True not in [mod["Found"] for mod in mods] and b_flag is not None:
        print("Warning: no supported DPDK kernel modules are loaded", file=sys.stderr)

    # change DPDK driver list to only contain drivers that are loaded
    dpdk_drivers = [mod["Name"] for mod in mods if mod["Found"]]


def has_iommu():
    """Check if IOMMU is enabled on system"""
    iommu_path = "/sys/class/iommu"
    return exists(iommu_path) and len(os.listdir(iommu_path)) > 0


def check_noiommu_mode():
    """Check and enable the noiommu mode for VFIO drivers"""
    global noiommu_flag
    filename = "/sys/module/vfio/parameters/enable_unsafe_noiommu_mode"

    try:
        with open(filename, "r") as f:
            value = f.read(1)
            if value in ("1", "y", "Y"):
                return  # Already enabled
    except OSError as err:
        sys.exit(f"Error: failed to check unsafe noiommu mode - Cannot open {filename}: {err}")

    if not noiommu_flag:
        sys.exit("Error: IOMMU support is disabled, use --noiommu-mode for binding in noiommu mode")

    try:
        with open(filename, "w") as f:
            f.write("1")
    except OSError as err:
        sys.exit(f"Error: failed to enable unsafe noiommu mode - Cannot open {filename}: {err}")
    print("Warning: enabling unsafe no IOMMU mode for VFIO drivers")


def verify_driver_loaded(driver_name):
    """Verify that the specified driver is actually loaded"""
    module_name = driver_name.replace('-', '_')
    if not module_is_loaded(driver_name):
        sys.exit(f"Error: Driver '{driver_name}' is not loaded.")
    return True


def verify_binding(dev_id, expected_driver):
    """Verify that a device is actually bound to the expected driver"""
    tmp = get_pci_device_details(dev_id, True)
    if "Driver_str" in tmp and tmp["Driver_str"] == expected_driver:
        return True
    return False

def has_driver(dev_id):
    '''return true if a device is assigned to a driver. False otherwise'''
    return ("Driver_str" in devices[dev_id]) and (devices[dev_id]["Driver_str"] != "")

def get_pci_device_details(dev_id, probe_lspci):
    '''This function gets additional details for a PCI device'''
    device = {}

    if probe_lspci:
        extra_info = subprocess.check_output(["lspci", "-vmmks", dev_id]).splitlines()
        # parse lspci details
        for line in extra_info:
            if not line:
                continue
            name, value = line.decode("utf8").split("\t", 1)
            name = name.strip(":") + "_str"
            device[name] = value
    # check for a unix interface name
    device["Interface"] = ""
    for base, dirs, _ in os.walk("/sys/bus/pci/devices/%s/" % dev_id):
        if "net" in dirs:
            device["Interface"] = \
                ",".join(os.listdir(os.path.join(base, "net")))
            break
    # check if a port is used for ssh connection
    device["Ssh_if"] = False
    device["Active"] = ""

    return device

def build_dict_of_all_devices(devices_type):
    '''This function populates the "devices" dictionary. The keys used are
    the pci addresses (domain:bus:slot.func). The values are themselves
    dictionaries - one for each NIC.'''
    global devices
    global dpdk_drivers

    # first loop through and read details for all devices
    # request machine readable format, with numeric IDs and String
    dev = {}
    dev_lines = subprocess.check_output(["lspci", "-Dvmmnnk"]).splitlines()
    for dev_line in dev_lines:
        if not dev_line:
            if device_type_match(dev, devices_type):
                # Replace "Driver" with "Driver_str" to have consistency of
                # of dictionary key names
                if "Driver" in dev.keys():
                    dev["Driver_str"] = dev.pop("Driver")
                if "Module" in dev.keys():
                    dev["Module_str"] = dev.pop("Module")
                # use dict to make copy of dev
                devices[dev["Slot"]] = dict(dev)
            # Clear previous device's data
            dev = {}
        else:
            name, value = dev_line.decode("utf8").split("\t", 1)
            value_list = value.rsplit(' ', 1)
            if value_list:
                # String stored in <name>_str
                dev[name.rstrip(":") + '_str'] = value_list[0]
            # Numeric IDs
            dev[name.rstrip(":")] = value_list[len(value_list) - 1] \
                .rstrip("]").lstrip("[")

    if devices_type == network_devices:
        # Find the default route interface - this is the critical one to protect
        default_if = None
        try:
            # Get the default route interface
            default_route = subprocess.check_output(
                ["ip", "-o", "route", "show", "default"],
                stderr=subprocess.DEVNULL
            ).decode().strip()
            # Parse "default via X.X.X.X dev <interface> ..."
            if default_route:
                parts = default_route.split()
                for i, part in enumerate(parts):
                    if part == "dev" and i + 1 < len(parts):
                        default_if = parts[i + 1]
                        break
        except subprocess.CalledProcessError:
            pass  # No default route

        # Count total network interfaces (excluding lo)
        try:
            all_interfaces = subprocess.check_output(
                ["ip", "-o", "link", "show"],
                stderr=subprocess.DEVNULL
            ).decode().strip().splitlines()
            # Filter out loopback and count real interfaces
            real_interfaces = [
                line.split(":")[1].strip().split("@")[0]
                for line in all_interfaces
                if "loopback" not in line.lower() and ": lo:" not in line
            ]
            single_interface = len(real_interfaces) == 1
        except subprocess.CalledProcessError:
            single_interface = False
            real_interfaces = []

    # based on the basic info, get extended text details
    for d in devices.keys():
        if not device_type_match(devices[d], devices_type):
            continue

        # get additional info and add it to existing data
        devices[d] = devices[d].copy()
        # No need to probe lspci
        devices[d].update(get_pci_device_details(d, False).items())

        if devices_type == network_devices:
            iface_names = devices[d]["Interface"].split(",")
            # Only protect the interface if:
            # 1. It's the default route interface, OR
            # 2. It's the only network interface on the system
            is_default = default_if and default_if in iface_names
            is_only_interface = single_interface and any(
                iface in real_interfaces for iface in iface_names
            )
            if is_default or is_only_interface:
                devices[d]["Ssh_if"] = True
                devices[d]["Active"] = "*Active*"
                if is_default:
                    devices[d]["Active"] = "*Default Route*"
                elif is_only_interface:
                    devices[d]["Active"] = "*Only Interface*"

        # add igb_uio to list of supporting modules if needed
        if "Module_str" in devices[d]:
            for driver in dpdk_drivers:
                if driver not in devices[d]["Module_str"]:
                    devices[d]["Module_str"] = \
                        devices[d]["Module_str"] + ",%s" % driver
        else:
            devices[d]["Module_str"] = ",".join(dpdk_drivers)

        # make sure the driver and module strings do not have any duplicates
        if has_driver(d):
            modules = devices[d]["Module_str"].split(",")
            if devices[d]["Driver_str"] in modules:
                modules.remove(devices[d]["Driver_str"])
                devices[d]["Module_str"] = ",".join(modules)

def device_type_match(dev, devices_type):
    for i in range(len(devices_type)):
        param_count = len(
            [x for x in devices_type[i].values() if x is not None])
        match_count = 0
        if dev["Class"][0:2] == devices_type[i]["Class"]:
            match_count = match_count + 1
            for key in devices_type[i].keys():
                if key != 'Class' and devices_type[i][key]:
                    value_list = devices_type[i][key].split(',')
                    for value in value_list:
                        if value.strip(' ') == dev[key]:
                            match_count = match_count + 1
            # count must be the number of non None parameters to match
            if match_count == param_count:
                return True
    return False

def pci_from_dev_name(dev_name):
    '''Take a device "name" - a string passed in by user to identify a NIC
    device, and determine the device id - i.e. the domain:bus:slot.func - for
    it, which can then be used to index into the devices array'''

    # check if it's already a suitable index
    if dev_name in devices:
        return dev_name
    # check if it's an index just missing the domain part
    if "0000:" + dev_name in devices:
        return "0000:" + dev_name

    # check if it's an interface name, e.g. eth1
    for d in devices.keys():
        if dev_name in devices[d]["Interface"].split(","):
            return devices[d]["Slot"]
    # if nothing else matches - error
    raise ValueError("Unknown device: %s. "
                     "Please specify device in \"bus:slot.func\" format" % dev_name)

def parse_args():
    '''Parses the command-line arguments given by the user and takes the
    appropriate action for each'''
    global b_flag
    global info_flag
    global args_dev
    global driver
    global force_flag
    global noiommu_flag

    parser = argparse.ArgumentParser(
        description='Utility to bind and unbind devices from Linux kernel',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
---------

To display device info for eth1 but not bind:
        %(prog)s --info eth1

To bind eth1 from the current driver and move to use vfio-pci
        %(prog)s -d vfio-pci --bind eth1

To bind with force (override SSH interface protection):
        %(prog)s -d vfio-pci --bind --force eth1

""")

    parser.add_argument(
        '-i',
        '--info',
        action='store_true',
        help="Print the device info")
    bind_group = parser.add_mutually_exclusive_group()
    bind_group.add_argument(
        '-b',
        '--bind',
        action='store_true',
        help="Select the driver to use or \"none\" to unbind the device")
    bind_group.add_argument(
        '-u',
        '--unbind',
        action='store_true',
        help="Unbind a device (equivalent to \"-b none\")")
    parser.add_argument(
        '-d',
        '--driver',
        type=str,
        default='igb_uio',
        help="Set the driver to use: vfio-pci, igb_uio (default)")
    parser.add_argument(
        '--force',
        action='store_true',
        help="""
Override restriction on binding devices in use by Linux.
WARNING: This can lead to loss of network connection and should be used with caution.
""")
    parser.add_argument(
        '--noiommu-mode',
        action='store_true',
        help="If IOMMU is not available, enable no IOMMU mode for VFIO drivers")
    parser.add_argument(
        'devices',
        metavar='DEVICE',
        nargs='*',
        help="""
Device specified by interface name.
""")
    opt = parser.parse_args()

    if opt.info:
        info_flag = True
    if opt.bind or opt.unbind:
        b_flag = opt.bind
    if opt.force:
        force_flag = True
    if opt.noiommu_mode:
        noiommu_flag = True
    args_dev = opt.devices
    driver = opt.driver

    if (b_flag is None) and (not info_flag):
        print("Error: No action specified for devices. "
              "Please give a --bind, --ubind or --info option",
              file=sys.stderr)
        parser.print_usage()
        sys.exit(1)

    if (b_flag or info_flag) and not args_dev:
        print("Error: No devices specified.", file=sys.stderr)
        parser.print_usage()
        sys.exit(1)

    if (b_flag or info_flag) and (len(args_dev) != 1):
        print("Error: Only one device may be specified.", file=sys.stderr)
        parser.print_usage()
        sys.exit(1)
    if args_dev:
        args_dev = args_dev[0]

def check_device():
    '''Make sure the selected device is actually one of the system interfaces.
    '''
    if not args_dev in netifaces.interfaces():
        print("Error: %s is not a valid network interface." % (args_dev), file=sys.stderr)
        print("       Valid interfaces are:"+str(netifaces.interfaces()))
        sys.exit(1)
    pass

def extract_device_details():
    '''Creats a disctionary called 'device' which holds all the interesting details about
    the selected device.
    '''
    dev_pci = pci_from_dev_name(args_dev)
    ifaddrs = netifaces.ifaddresses(args_dev)
    device["device"] = args_dev
    device["pci"] = devices[dev_pci]["Slot_str"]
    device["driver"] = devices[dev_pci]["Driver_str"]
    device["mac"] = ifaddrs.get(netifaces.AF_LINK, [{}])[0].get("addr", "")
    if netifaces.AF_INET in ifaddrs:
        IPv4 = ifaddrs[netifaces.AF_INET][0]
        device["ipv4"] = IPv4.get("addr", "")
        device["netmask"] = IPv4.get("netmask", "")
    else:
        device["ipv4"] = ""
        device["netmask"] = ""

    # Get the default gateway if this interface has one
    device["gateway"] = ""
    try:
        default_route = subprocess.check_output(
            ["ip", "-o", "route", "show", "default", "dev", args_dev],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        # Parse "default via X.X.X.X dev <interface> ..."
        if default_route:
            parts = default_route.split()
            for i, part in enumerate(parts):
                if part == "via" and i + 1 < len(parts):
                    device["gateway"] = parts[i + 1]
                    break
    except subprocess.CalledProcessError:
        pass  # No default route on this interface

def show_status():
    '''Shows the details for the selected device'''
    print("Device  : "+device["device"])
    print("PCI     : "+device["pci"])
    print("Driver  : "+device["driver"])
    print("MAC     : "+device["mac"])
    print("IP      : "+device["ipv4"])
    print("Netmask : "+device["netmask"])
    print("Gateway : "+device.get("gateway", ""))

def save_device_details():
    '''Writes device details to json file'''
    global device
    with open(file_name_for_saved_data, 'w', encoding='utf-8') as f:
        json.dump(device, f, ensure_ascii=False, indent=4)

def read_device_details_from_file():
    '''Reads device details from json file'''
    global device
    try:
        with open(file_name_for_saved_data) as f:
            device = json.load(f)
    except FileNotFoundError:
        sys.exit("ERROR: File '"+file_name_for_saved_data+" not found. Can't auto unbind.")

def unbind_one(dev_id, force):
    '''Unbind the device identified by "dev_id" from its current driver'''
    dev = devices[dev_id]
    if not has_driver(dev_id):
        print("Notice: %s %s %s is not currently managed by any driver" %
              (dev["Slot"], dev["Device_str"], dev["Interface"]), file=sys.stderr)
        return

    # prevent us disconnecting ourselves
    if dev["Ssh_if"] and not force:
        print("Warning: interface %s is %s. "
              "Skipping unbind. Use --force to override." %
              (dev_id, dev.get("Active", "active")), file=sys.stderr)
        return

    print("Info: unbinding %s from device %s" % (dev["Driver_str"], dev_id))

    # write to /sys to unbind
    filename = "/sys/bus/pci/drivers/%s/unbind" % dev["Driver_str"]
    try:
        f = open(filename, "a")
    except OSError as err:
        sys.exit("Error: unbind failed for %s - Cannot open %s: %s" %
                 (dev_id, filename, err))
    try:
        f.write(dev_id)
        f.close()
    except OSError as err:
        sys.exit("Error: unbind failed for %s - Cannot write to %s: %s" %
                 (dev_id, filename, err))

def bind_one(dev_id, driver, force) -> bool:
    '''Bind the device given by "dev_id" to the driver "driver". If the device
    is already bound to a different driver, it will be unbound first'''

    dev = devices[dev_id]
    saved_driver = None  # used to rollback any unbind in case of failure

    # Check driver is loaded before attempting to bind
    if not module_is_loaded(driver.replace('-', '_')):
        print(f"Error: Driver '{driver}' is not loaded.", file=sys.stderr)
        return False

    # Check for IOMMU support when binding to vfio-pci
    if driver == "vfio-pci" and not has_iommu():
        check_noiommu_mode()

    # prevent disconnection of our ssh session
    if dev["Ssh_if"] and not force:
        print("Warning: interface %s is %s. "
              "Not modifying. Use --force to override." %
              (dev_id, dev.get("Active", "active")), file=sys.stderr)
        return False

    # unbind any existing drivers we don't want
    if has_driver(dev_id):
        if dev["Driver_str"] == driver:
            print("Notice: %s already bound to driver %s, skipping" %
                  (dev_id, driver), file=sys.stderr)
            return False
        saved_driver = dev["Driver_str"]
        unbind_one(dev_id, force)
        dev["Driver_str"] = ""  # clear driver string

    print("Info: binding device %s to driver %s" % (dev_id, driver))

    # For kernels >= 3.15 driver_override can be used to specify the driver
    # for a device rather than relying on the driver to provide a positive
    # match of the device.  The existing process of looking up
    # the vendor and device ID, adding them to the driver new_id,
    # will erroneously bind other devices too which has the additional burden
    # of unbinding those devices
    if driver in dpdk_drivers:
        filename = "/sys/bus/pci/devices/%s/driver_override" % dev_id
        if exists(filename):
            try:
                f = open(filename, "w")
            except OSError as err:
                print("Error[1]: bind failed for %s - Cannot open %s: %s"
                      % (dev_id, filename, err), file=sys.stderr)
                return False
            try:
                f.write("%s" % driver)
                f.close()
            except OSError as err:
                print("Error: bind failed for %s - Cannot write driver %s to "
                      "PCI ID: %s" % (dev_id, driver, err), file=sys.stderr)
                return False
        # For kernels < 3.15 use new_id to add PCI id's to the driver
        else:
            filename = "/sys/bus/pci/drivers/%s/new_id" % driver
            try:
                f = open(filename, "w")
            except OSError as err:
                print("Error[2]: bind failed for %s - Cannot open %s: %s"
                      % (dev_id, filename, err), file=sys.stderr)
                return False
            try:
                # Convert Device and Vendor Id to int to write to new_id
                f.write("%04x %04x" % (int(dev["Vendor"], 16),
                                       int(dev["Device"], 16)))
                f.close()
            except OSError as err:
                print("Error: bind failed for %s - Cannot write new PCI ID to "
                      "driver %s: %s" % (dev_id, driver, err), file=sys.stderr)
                return False

    # do the bind by writing to /sys
    filename = "/sys/bus/pci/drivers/%s/bind" % driver
    try:
        f = open(filename, "a")
    except OSError as err:
        print("Error[3]: bind failed for %s - Cannot open %s: %s"
              % (dev_id, filename, err), file=sys.stderr)
        if saved_driver is not None:  # restore any previous driver
            bind_one(dev_id, saved_driver, force)
        return False
    try:
        f.write(dev_id)
        f.close()
    except OSError as err:
        # for some reason, closing dev_id after adding a new PCI ID to new_id
        # results in IOError. however, if the device was successfully bound,
        # we don't care for any errors and can safely ignore IOError
        tmp = get_pci_device_details(dev_id, True)
        if "Driver_str" in tmp and tmp["Driver_str"] == driver:
            return True  # Fixed: was returning None instead of True
        print("Error: bind failed for %s - Cannot bind to driver %s: %s"
              % (dev_id, driver, err), file=sys.stderr)
        if saved_driver is not None:  # restore any previous driver
            bind_one(dev_id, saved_driver, force)
        return False

    # For kernels > 3.15 driver_override is used to bind a device to a driver.
    # Before unbinding it, overwrite driver_override with empty string so that
    # the device can be bound to any other driver
    filename = "/sys/bus/pci/devices/%s/driver_override" % dev_id
    if exists(filename):
        try:
            f = open(filename, "w")
        except OSError as err:
            sys.exit("Error: unbind failed for %s - Cannot open %s: %s"
                     % (dev_id, filename, err))
        try:
            f.write("\00")
            f.close()
        except OSError as err:
            sys.exit("Error: unbind failed for %s - Cannot write %s: %s"
                     % (dev_id, filename, err))

    # Verify that binding actually succeeded
    if not verify_binding(dev_id, driver):
        print("Error: bind appeared to succeed but device %s is not bound to %s"
              % (dev_id, driver), file=sys.stderr)
        if saved_driver is not None:  # restore any previous driver
            bind_one(dev_id, saved_driver, force)
        return False

    return True

def validate_driver_name(driver_name):
    '''Validate that the driver name is not accidentally a device name.
    A common user error is to forget to specify the driver.'''
    try:
        pci_from_dev_name(driver_name)
        # if we've made it this far, the "driver" was a valid device string,
        # so it's probably not a valid driver name.
        sys.exit("Error: Driver '%s' does not look like a valid driver. "
                 "Did you forget to specify the driver to bind devices to?" % driver_name)
    except ValueError:
        # driver generated error - it's not a valid device ID, so all is well
        pass


def do_arg_actions():
    '''do the actual action requested by the user'''
    global b_flag
    global info_flag
    global args_dev
    global force_flag

    if info_flag:
        show_status()
    if b_flag is not None:
        if b_flag:
            # Validate that the driver is not accidentally a device name
            validate_driver_name(driver)
            save_device_details()
            if not bind_one(device["pci"], driver, force_flag):
                sys.exit("Error: Failed to bind device to driver")
        else:
            if bind_one(device["pci"], device["driver"], force_flag):
                os.remove(file_name_for_saved_data)

def main():
    '''program main function'''
    # check to make sure we have the right permissions
    if os.geteuid() != 0:
        sys.exit("You must run this script with SUDO or be root")
    # check if lspci is installed, suppress any output
    with open(os.devnull, 'w') as devnull:
        ret = subprocess.call(['which', 'lspci'],
                              stdout=devnull, stderr=devnull)
        if ret != 0:
            sys.exit("'lspci' not found - please install 'pciutils'")
    parse_args()
    check_dpdk_modules()
    build_dict_of_all_devices(network_devices)
    if ((b_flag is not None) and b_flag) or info_flag:
        check_device()
        extract_device_details()
    else:
        read_device_details_from_file()

    do_arg_actions()


if __name__ == "__main__":
    main()

