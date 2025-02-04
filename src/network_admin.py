#!/usr/bin/env python3
# ex:ts=4:sw=4:sts=4:et
# -*- tab-width: 4; c-basic-offset: 4; indent-tabs-mode: nil -*-
###############################################################################
#
# Copyright 2020 NVIDIA Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
###############################################################################

import os
import sys
import argparse
import subprocess
import shutil
import yaml
import json
import glob
import time
import re
import errno
from ipaddress import ip_address, IPv4Address

__author__ = "Vladimir Sokolovsky <vlad@nvidia.com>"
__version__ = "1.0"

prog = os.path.basename(sys.argv[0]) 
os.environ['PATH'] = '/opt/mellanox/iproute2/sbin:/usr/sbin:/usr/bin:/sbin:/bin'

MLXREG = '/usr/bin/mlxreg'
SUPPORTED_OPERATIONS=["ipconfig", "mtuconfig", "gwconfig", "dnsconfig", "domainconfig", "roceconfig"]
SUPPORTED_ACTIONS=["set", "show"]
cloud_init_config = "/var/lib/cloud/seed/nocloud-net/network-config"
netplan_config = "/etc/netplan/60-mlnx.yaml"
use_netplan = 1

if use_netplan:
    network_config = netplan_config
else:
    network_config = cloud_init_config

network_config_orig = network_config + ".orig"
network_config_backup = network_config + ".bak"

resolv_conf = "/etc/resolv.conf"
resolv_conf_orig = "/etc/resolv.conf.orig"
verbose = 0

class BFCONFIG:
    def __init__ (self, args):
        self.device = args.device
        self.port = args.port
        self.op = args.op
        self.action = args.action
        self.verbose = args.verbose

        self.devices = []
        if self.port:
            self.pci_devices = self.__get_pci_device__()
            if self.pci_devices:
                self.pci_device = self.pci_devices[0]
            if not self.device:
                self.devices = self.__get_device__()
                self.device = self.devices[0]
                self.roce_devices = self.__get_roce_device__()
                self.roce_device = self.roce_devices[0]

        if self.op in ["ipconfig", "mtuconfig", "gwconfig"]:
            try:
                with open(network_config, 'r') as stream:
                    self.data = yaml.safe_load(stream)
            except Exception as e:
                bf_log ("ERR: Failed to load configuration file {}. Exception: {}".format(network_config, e))
                return None

            self.ipv4_prefix = args.ipv4_prefix
            self.ipv6_prefix = args.ipv6_prefix
            self.dhcp4 = False
            self.dhcp6 = False
            self.ipv4_addr = None
            self.ipv6_addr = None
    #        self.bootproto = None

            if args.ipv4_addr:
                if args.ipv4_addr == "dhcp":
                    self.dhcp4 = True
    #                self.bootproto = "dhcp"
                else:
                    self.ipv4_addr = args.ipv4_addr
    #                self.bootproto = "static"

            if args.ipv6_addr:
                if args.ipv6_addr == "dhcp":
                    self.dhcp6 = True
    #                self.bootproto = "dhcp"
                else:
                    self.ipv6_addr = args.ipv6_addr
    #                self.bootproto = "static"

    #        if args.bootproto:
    #            self.bootproto = args.bootproto

            self.ipv4_gateway = args.ipv4_gateway
            self.ipv6_gateway = args.ipv6_gateway
            self.network = args.network or '0.0.0.0'
            self.network_prefix = args.network_prefix or '0'
            self.metric = args.metric
            self.mtu = args.mtu
    #        self.nmcontrolled = args.nmcontrolled
    #        self.vlan = args.vlan
    #        self.onboot = args.onboot

        if self.op in ["dnsconfig", "domainconfig"]:
            self.clean_domain = False
            self.ipv4_nameservers = []
            self.ipv6_nameservers = []
            self.searchdomains = []
            self.nameservers = []
            self.domains = []

            if self.op == "dnsconfig" and self.action == "set":
                if args.ipv4_nameservers:
                    for ipv4_nameserver in args.ipv4_nameservers:
                        if ',' in ipv4_nameserver[0]:
                            self.ipv4_nameservers.extend(ipv4_nameserver[0].split(','))
                        else:
                            self.ipv4_nameservers.append(ipv4_nameserver[0])

                if args.ipv6_nameservers:
                    for ipv6_nameserver in args.ipv6_nameservers:
                        if ',' in ipv6_nameserver[0]:
                            self.ipv6_nameservers.extend(ipv6_nameserver[0].split(','))
                        else:
                            self.ipv6_nameservers.append(ipv6_nameserver[0])

            if self.op == "domainconfig" and self.action == "set":
                self.clean_domain = True
                if args.domains != [['']]:
                    self.clean_domain = False
                    for domain in args.domains:
                        if ',' in domain[0]:
                            self.domains.extend(domain[0].split(','))
                        else:
                            if domain[0]:
                                self.domains.append(domain[0])

            try:
                # Read current configuration
                with open(resolv_conf, 'r') as stream:
                    for line in stream:
                        line = line.strip()
                        if line.startswith("search"):
                            self.searchdomains = line.split(' ')[1:]
                        elif line.startswith("nameserver"):
                            self.nameservers.append(line.split(' ')[1])
            except Exception as e:
                bf_log ("ERR: Failed to read configuration file {}. Exception: {}".format(resolv_conf, e))
                return None

        if self.op in ["roceconfig"]:
            self.type = args.type
            self.trust = args.trust
            self.ecn = []
            self.cable_len = args.cable_len
            self.dscp2prio = args.dscp2prio
            self.prio_tc = []
            self.pfc = []
            self.prio2buffer = []
            self.ratelimit = []
            self.buffer_size = []

            if args.ecn:
                for ecn in args.ecn:
                    if ',' in ecn[0]:
                        self.ecn.extend(ecn[0].split(','))
                    else:
                        self.ecn.append(ecn[0])

            if args.prio_tc:
                for prio_tc in args.prio_tc:
                    if ',' in prio_tc[0]:
                        self.prio_tc.extend(prio_tc[0].split(','))
                    else:
                        self.prio_tc.append(prio_tc[0])

            if args.pfc:
                for pfc in args.pfc:
                    if ',' in pfc[0]:
                        self.pfc.extend(pfc[0].split(','))
                    else:
                        self.pfc.append(pfc[0])

            if args.prio2buffer:
                for prio2buffer in args.prio2buffer:
                    if ',' in prio2buffer[0]:
                        self.prio2buffer.extend(prio2buffer[0].split(','))
                    else:
                        self.prio2buffer.append(prio2buffer[0])

            if args.ratelimit:
                for ratelimit in args.ratelimit:
                    if ',' in ratelimit[0]:
                        self.ratelimit.extend(ratelimit[0].split(','))
                    else:
                        self.ratelimit.append(ratelimit[0])

            if args.buffer_size:
                for buffer_size in args.buffer_size:
                    if ',' in buffer_size[0]:
                        self.buffer_size.extend(buffer_size[0].split(','))
                    else:
                        self.buffer_size.append(buffer_size[0])


    def __get_pci_device__(self):
        """
        Get network device assosiated with the port
        """
        devices = []
        try:
            if self.port:
                cmd = "readlink -f /sys/class/infiniband/mlx5_{}/device".format(self.port)
            else:
                cmd = "readlink -f /sys/class/infiniband/mlx5_*"
            rc, output = get_status_output(cmd)
            for line in output.split('\n'):
                if line:
                    devices.append(line.split('/')[-1])
        except Exception as e:
            bf_log ("ERR: Port {} does not exist. Exception: {}".format(self.port, e))
            return None

        return devices

    def __get_device__(self):
        """
        Get network device assosiated with the port
        """
        devices = []
        try:
            if self.port:
                # Map port 0 to port 2 and port 1 to port 3 to get SF netdevice p0m0 and p1m0
                cmd = "/bin/ls -d /sys/class/net/*/device/infiniband/mlx5_{}".format(str(int(self.port) + 2))
            else:
                cmd = "/bin/ls -d /sys/class/net/*/device/infiniband/mlx5_*"
            rc, output = get_status_output(cmd)
            for line in output.split('\n'):
                if line:
                    devices.append(line.split('/')[4])
        except Exception as e:
            bf_log ("ERR: Port {} does not exist. Exception: {}".format(self.port, e))
            return None

        return devices

    def __get_roce_device__(self):
        """
        Get network device assosiated with the port
        """
        devices = []
        try:
            cmd = "/bin/ls -d /sys/class/net/*/smart_nic/pf"
            rc, output = get_status_output(cmd)
            line = output.split('\n')[int(self.port)]
            if line:
                devices.append(line.split('/')[4])
        except Exception as e:
            bf_log ("ERR: Port {} does not exist. Exception: {}".format(self.port, e))
            return None

        return devices

    def show(self):
        """
        Show configurations
        """
        result = {}
        result["op"] = self.op
        result["action"] = self.action
        result["status"] = 0
        result["output"] = ""

        if self.op in ["ipconfig", "mtuconfig", "gwconfig"]:
            data = {}
            if use_netplan:
                data = self.data['network']
            else:
                data = self.data

        if self.op == 'ipconfig':
            ipv4_addr=""
            ipv4_prefix=""
            ipv6_addr=""
            ipv6_prefix=""

            if self.device in data['ethernets']:
                if 'addresses' in data['ethernets'][self.device]:
                    for addr in data['ethernets'][self.device]['addresses']:
                        ip, prefix = addr.split('/')
                        if validIPAddress(ip) == 'IPv4':
                            ipv4_addr, ipv4_prefix = ip, prefix
                        if validIPAddress(ip) == 'IPv6':
                            ipv6_addr, ipv6_prefix = ip, prefix
                if 'dhcp4' in data['ethernets'][self.device]:
                    ipv4_addr = "dhcp4"
                if 'dhcp6' in data['ethernets'][self.device]:
                    ipv6_addr = "dhcp6"

                result["output"] = "ipv4_addr={}/ipv4_prefix={}/ipv6_addr={}/ipv6_prefix={}".format(ipv4_addr, ipv4_prefix, ipv6_addr, ipv6_prefix)

        elif self.op == 'mtuconfig':
            if self.device in data['ethernets']:
                if 'mtu' in data['ethernets'][self.device]:
                    result["output"] = "mtu={}".format(data['ethernets'][self.device]['mtu'])

            if 'mtu' not in result:
                cmd = "cat /sys/class/net/{}/mtu".format(self.device)
                rc, mtu_output = get_status_output(cmd, verbose)
                if rc:
                    bf_log ("ERR: Failed to get MTU for {} interface. RC={}".format(self.device, rc))
                    result["status"] = rc
                    result["output"] = "ERR: Failed to get MTU for {} interface. RC={}".format(self.device, rc)
                    return result
                result["output"] = "mtu={}".format(mtu_output.strip())

        elif self.op == 'gwconfig':
            ipv4_gateway = ""
            ipv6_gateway = ""
            if self.device in data['ethernets']:
                if 'routes' in data['ethernets'][self.device]:
                    result["routes"] = data['ethernets'][self.device]['routes']
                if 'gateway4' in data['ethernets'][self.device]:
                    ipv4_gateway = data['ethernets'][self.device]['gateway4']
                if 'gateway6' in data['ethernets'][self.device]:
                    ipv6_gateway = data['ethernets'][self.device]['gateway6']
            result["output"] = "ipv4_gateway={}/ipv6_gateway={}".format(ipv4_gateway, ipv6_gateway)

        elif self.op == 'dnsconfig':
            ipv4_nameservers = []
            ipv6_nameservers = []
            for nameserver in self.nameservers:
                if validIPAddress(nameserver) == 'IPv4':
                    ipv4_nameservers.append(nameserver)
                if validIPAddress(nameserver) == 'IPv6':
                    ipv6_nameservers.append(nameserver)
            result["output"] = "ipv4_nameservers={}/ipv6_nameservers={}".format(','.join(ipv4_nameservers), ','.join(ipv6_nameservers))

        elif self.op == 'domainconfig':
            result["output"] = "domains={}".format(','.join(self.searchdomains))

        elif self.op == 'roceconfig':
            trust = ""
            cable_len = ""
            prio_tc = ""
            ecn = []
            pfc = ""
            prio2buffer = ""
            buffer_size = ""
            dscp2prio = ""
            ratelimit = ""
            roce_accl = []

            cmd = "bash -c 'mlnx_qos -i {}'".format(self.roce_device)
            rc, mlnx_qos_output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to run mlnx_qos. RC={}\nOutput:\n{}".format(rc, mlnx_qos_output))
                result["status"] = rc
                result["output"] = "ERR: Failed to run mlnx_qos. RC={}\nOutput:\n{}".format(rc, mlnx_qos_output)
                return result

            in_dscp2prio = 0
            dscp2prio_map = {}
            in_pfc_configuration = 0

            for i in range(8):
                dscp2prio_map[i] = ''

            for line in mlnx_qos_output.split('\n'):
                if 'Priority trust state:' in line:
                    trust = line.split(' ')[-1]
                elif 'Cable len:' in line:
                    cable_len = line.split(' ')[-1]
                elif 'Receive buffer size' in line:
                    buffer_size = line.split(':')[-1][1:-1]
                elif 'PFC configuration:' in line:
                    in_pfc_configuration = 1
                elif 'tc:' in line:
                    in_pfc_configuration = 0
                    info = re.search(r'tc:(.*?)ratelimit:(.*?)tsa:(.*?)$', line)
                    prio_tc = info.group(1).strip()
                    ratelimit = info.group(2).strip().rstrip(',')
                elif in_pfc_configuration:
                    if 'enabled' in line:
                        pfc = ','.join(line.split())
                        pfc = ','.join(pfc.split(',')[1:])
                    elif 'buffer' in line:
                        prio2buffer = ','.join(line.split())
                        prio2buffer = ','.join(prio2buffer.split(',')[1:])
                elif 'dscp2prio mapping:' in line:
                    in_dscp2prio = 1
                elif 'default priority:' in line:
                    in_dscp2prio = 0
                elif in_dscp2prio:
                    prio = int(line.split(':')[1][0])
                    dscp2prio_map[prio] += str(''.join(line.split(':')[2:]))

            for i in range(8):
                if len(dscp2prio_map[i]):
                    dscp2prio += '{}'.format('{' + dscp2prio_map[i][:-1] + '},')
                else:
                    dscp2prio += '{}'.format('{},')

            dscp2prio = dscp2prio[:-1]

            cmd = "bash -c 'mlxreg -d {} --get --reg_name ROCE_ACCL'".format(self.pci_device)
            rc, mlxreg_output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to run mlxreg. RC={}\nOutput:\n{}".format(rc, mlxreg_output))
                result["status"] = rc
                result["output"] = "ERR: Failed to run mlxreg. RC={}\nOutput:\n{}".format(rc, mlxreg_output)
                return result

            for line in mlxreg_output.split('\n'):
                if 'roce' in line:
                    reg_name = line.split('|')[0].strip()
                    reg_data = line.split('|')[1].strip()
                    roce_accl.append("{}={}".format(reg_name, reg_data))

            for i in range(8):
                cmd = 'bash -c "cat /sys/class/net/{device}/ecn/roce_np/enable/{prio} 2> /dev/null"'.format(ecn=ecn, device=self.roce_device, prio=i)
                rc, ecn_output = get_status_output(cmd, verbose)
                if rc:
                    result["status"] = rc
                    result["output"] = "ERR: Failed to read ECN. RC={}\nOutput:\n{}".format(rc, ecn_output)
                    bf_log ("ERR: Failed to get ECN. RC={}\nOutput:\n{}".format(rc, ecn_output))
                    return result

                ecn.append(ecn_output.strip())

            result["output"] = "trust={trust}/prio_tc={prio_tc}/ecn={ecn}/pfc={pfc}/cable_len={cable_len}/prio2buffer={prio2buffer}/buffer_size={buffer_size}/dscp2prio={dscp2prio}/ratelimit={ratelimit}/roce_accl={roce_accl}".format(trust=trust,prio_tc=prio_tc,ecn=','.join(ecn),pfc=pfc,cable_len=cable_len,prio2buffer=prio2buffer,buffer_size=buffer_size,dscp2prio=dscp2prio,ratelimit=ratelimit,roce_accl=','.join(roce_accl))

        return result


    def set_network_config(self):
        """
        Set configuration to be used by cloud-init
        """
        rc = 0
        cmd = None
        addr = None
        prefix = None
        dev = self.device
        data = {}

        if use_netplan:
            data = self.data["network"]
            res_data = self.data
        else:
            data = self.data

        conf = data["ethernets"]

        if self.op == "ipconfig":
            conf[dev]['addresses'] = []
            conf[dev]['dhcp4'] = None
            conf[dev]['dhcp6'] = None
        elif self.op == "gwconfig":
            conf[dev]['routes'] = []
            conf[dev]['gateway4'] = None
            conf[dev]['gateway6'] = None
        elif self.op == "mtuconfig":
            conf[dev]['mtu'] = None

        if data["ethernets"][dev]:
            if self.op in ["ipconfig", "gwconfig"]:
                if 'mtu' in data["ethernets"][dev]:
                    conf[dev]['mtu'] = data["ethernets"][dev]['mtu']
            if self.op in ["ipconfig", "mtuconfig"]:
                if 'routes' in data["ethernets"][dev]:
                    conf[dev]['routes'] = data["ethernets"][dev]['routes']
                if 'gateway4' in data["ethernets"][dev]:
                    conf[dev]['gateway4'] = data["ethernets"][dev]['gateway4']
                if 'gateway6' in data["ethernets"][dev]:
                    conf[dev]['gateway6'] = data["ethernets"][dev]['gateway6']
            if self.op in ["mtuconfig", "gwconfig"]:
                if 'addresses' in data["ethernets"][dev]:
                    conf[dev]['addresses'] = data["ethernets"][dev]['addresses']
                if 'dhcp4' in data["ethernets"][dev]:
                    conf[dev]['dhcp4'] = data["ethernets"][dev]['dhcp4']
                if 'dhcp6' in data["ethernets"][dev]:
                    conf[dev]['dhcp6'] = data["ethernets"][dev]['dhcp6']

        # Set configuration parameters
        if self.op == "ipconfig":
            if self.dhcp4:
                conf[dev]['dhcp4'] = "true"
            elif self.ipv4_addr:
                conf[dev]['addresses'].append("{}/{}".format(self.ipv4_addr, self.ipv4_prefix))

            if self.dhcp6:
                conf[dev]['dhcp6'] = "true"
            elif self.ipv6_addr:
                conf[dev]['addresses'].append("{}/{}".format(self.ipv6_addr, self.ipv6_prefix))

        if self.op == "mtuconfig":
            if self.mtu:
                conf[dev]['mtu'] = self.mtu

        if self.op == "gwconfig":
            if self.ipv4_gateway:
                if self.metric:
                    conf[dev]['routes'].append([{'metric': self.metric, 'to': "{}/{}".format(self.network, self.network_prefix), 'via': self.ipv4_gateway}])
                # elif self.network != '0.0.0.0' and self.network_prefix != '0':
                #     conf[dev]['routes'].append([{'to': "{}/{}".format(self.network, self.network_prefix), 'via': self.ipv4_gateway}])
                else:
                    conf[dev]['gateway4'] = self.ipv4_gateway

            if self.ipv6_gateway:
                if self.metric:
                    conf[dev]['routes'].append([{'metric': self.metric, 'to': "{}/{}".format(self.network, self.network_prefix), 'via': self.ipv6_gateway}])
                # elif self.network and self.network_prefix:
                #     conf[dev]['routes'].append([{'to': "{}/{}".format(self.network, self.network_prefix), 'via': self.ipv6_gateway}])
                else:
                    conf[dev]['gateway6'] = self.ipv6_gateway

        # Cleanup empty spaces
        if self.op == "ipconfig":
            if not len(conf[dev]['addresses']):
                del conf[dev]['addresses']
            if not conf[dev]['dhcp4']:
                del conf[dev]['dhcp4']
            if not conf[dev]['dhcp6']:
                del conf[dev]['dhcp6']
        elif self.op == "gwconfig":
            if not len(conf[dev]['routes']):
                del conf[dev]['routes']
            if not conf[dev]['gateway4']:
                del conf[dev]['gateway4']
            if not conf[dev]['gateway6']:
                del conf[dev]['gateway6']
        elif self.op == "mtuconfig":
            if not conf[dev]['mtu']:
                del conf[dev]['mtu']

        if len(conf[dev]):
            if use_netplan:
                res_data["network"]["ethernets"][dev] = conf[dev]
                data = res_data
            else:
                data["ethernets"][dev] = conf[dev]
        else:
            if use_netplan:
                if dev in res_data["network"]["ethernets"]:
                    del res_data["network"]["ethernets"][dev]
                    data = res_data
            else:
                if dev in data["ethernets"]:
                    del data["ethernets"][dev]

        try:
            with open(network_config, 'w') as stream:
                output = yaml.dump(data, stream, sort_keys=False)
        except:
            bf_log ("ERR: Failed to write into configuration file {}".format(network_config))
            return 1

        return rc

    def set_resolv_conf(self):
        # DNS configuration
        """
        Update /etc/resolv.conf directly
        """

        try:
            with open(resolv_conf, 'w') as stream:
                if not self.clean_domain:
                    if self.domains:
                        stream.write("search {}\n".format(' '.join(str(domain) for domain in self.domains)))
                    else:
                        if self.searchdomains:
                            stream.write("search {}\n".format(' '.join(str(domain) for domain in self.searchdomains)))

                if self.ipv4_nameservers or self.ipv6_nameservers:
                    if self.ipv4_nameservers:
                        for nameserver in self.ipv4_nameservers:
                            if nameserver:
                                stream.write("nameserver {}\n".format(str(nameserver)))
                    if self.ipv6_nameservers:
                        for nameserver in self.ipv6_nameservers:
                            if nameserver:
                                stream.write("nameserver {}\n".format(str(nameserver)))
                else:
                    if self.nameservers:
                        for nameserver in self.nameservers:
                            if nameserver.strip():
                                stream.write("nameserver {}\n".format(str(nameserver)))

        except Exception as e:
            bf_log ("ERR: Failed to write to the configuration file {}. Exception: {}".format(resolv_conf, e))
            return 1


    def apply_config(self):
        if use_netplan:
            cmd = "bash -c 'netplan apply'"
        else:
            cmd = "bash -c 'cloud-init clean; cloud-init init; netplan apply'"
        rc, output = get_status_output(cmd, verbose)
        if rc:
            bf_log ("ERR: Failed to apply configuration. RC={}\nOutput:\n{}".format(rc, output))

        return rc

    def ip_config(self):
        """
        Construct and apply ip command like:
        ip address add dev tmfifo_net0 192.168.100.1/24"
        """
        rc = 0
        cmd = None

        if self.ipv4_addr:
            cmd = "ip address add dev {}".format(self.device)
            if self.ipv4_prefix:
                cmd += " {}/{}".format(self.ipv4_addr, self.ipv4_prefix)
            else:
                cmd += " {}".format(self.ipv4_addr)
            rc, output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to configure IP address for {} interface. RC={}".format(self.device, rc))
                return rc

        if self.ipv6_addr:
            cmd = "ip address add dev {}".format(self.device)
            if self.ipv6_prefix:
                cmd += " {}/{}".format(self.ipv6_addr, self.ipv6_prefix)
            else:
                cmd += " {}".format(self.ipv6_addr)
            rc, output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to configure IP address for {} interface. RC={}".format(self.device, rc))
                return rc

        # Set routing
        if self.network or self.ipv4_gateway or self.ipv6_gateway:
            if self.network:
                if self.ipv4_gateway:
                    cmd = "ip route add {}/{} via {}".format(self.network, self.network_prefix, self.ipv4_gateway)
                elif self.ipv6_gateway:
                    cmd = "ip route add {}/{} via {}".format(self.network, self.network_prefix, self.ipv6_gateway)
                else:
                    cmd = "ip route add {}/{} via {}".format(self.network, self.network_prefix, self.device)
            else:
                if self.ipv4_gateway:
                    cmd = "ip route add default gw {}".format(self.ipv4_gateway)
                elif self.ipv6_gateway:
                    cmd = "ip route add default gw {}".format(self.ipv6_gateway)

        if self.metric:
            cmd += " metric {}".format(self.metric)

        rc, output = get_status_output(cmd, verbose)
        if rc:
            bf_log ("ERR: Failed to configure gateway for {} interface. RC={}".format(self.device, rc))
            return rc

        if self.mtu:
            cmd = "ip link set {} mtu {}".format(self.device, self.mtu)
            rc, output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to configure MTU for {} interface. RC={}".format(self.device, rc))
                return rc

        return rc

    def set_roce_config(self):
        """
        ROCE configuration
        """

        mlnx_qos_params = ""

        if self.ecn:
            i = 0
            for ecn in self.ecn:
                cmd = 'bash -c "echo {ecn} > /sys/class/net/{device}/ecn/roce_np/enable/{prio} || true; \
                                echo {ecn} > /sys/class/net/{device}/ecn/roce_rp/enable/{prio} || true"'.format(ecn=ecn, device=self.roce_device, prio=i)
                rc, ecn_output = get_status_output(cmd, verbose)
                if rc:
                    bf_log ("ERR: Failed to set ECN. RC={}\nOutput:\n{}".format(rc, ecn_output))
                    return 1
                i += 1

        if self.type:
            if self.type == "lossy":
                cmd = 'bash -c "mlxreg -d {} --yes --reg_name ROCE_ACCL --set \"roce_adp_retrans_en=0x1,roce_tx_window_en=0x1,roce_slow_restart_en=0x1\""'.format(self.pci_device)
            else:
                cmd = 'bash -c "mlxreg -d {} --yes --reg_name ROCE_ACCL --set \"roce_adp_retrans_en=0x0,roce_tx_window_en=0x0,roce_slow_restart_en=0x0\""'.format(self.pci_device)
            rc, type_output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to run mlxreg. RC={}\nOutput:\n{}".format(rc, type_output))
                return 1

        if self.trust:
            mlnx_qos_params += " --trust {}".format(self.trust)

        if self.cable_len:
            mlnx_qos_params += " --cable_len {}".format(self.cable_len)

        if self.dscp2prio:
            mlnx_qos_params += " --dscp2prio {}".format(self.dscp2prio)

        if self.prio_tc:
            mlnx_qos_params += " --prio_tc {}".format(','.join(self.prio_tc))

        if self.pfc:
            mlnx_qos_params += " --pfc {}".format(','.join(self.pfc))

        if self.prio2buffer:
            mlnx_qos_params += " --prio2buffer {}".format(','.join(self.prio2buffer))

        if self.ratelimit:
            mlnx_qos_params += " --ratelimit {}".format(','.join(self.ratelimit))

        if self.buffer_size:
            mlnx_qos_params += " --buffer_size {}".format(','.join(self.buffer_size))

        if mlnx_qos_params:
            cmd = "bash -c 'mlnx_qos -i {} {}'".format(self.roce_device, mlnx_qos_params)
            rc, mlnx_qos_output = get_status_output(cmd, verbose)
            if rc:
                bf_log ("ERR: Failed to run mlnx_qos. RC={}\nOutput:\n{}".format(rc, mlnx_qos_output))
                return 1

def version():
    """Display program version information."""
    print(prog + ' ' + __version__)


def get_status_output(cmd, verbose=False):
    rc, output = (0, '')

    if verbose:
        print("Running command:", cmd)

    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT,
                                         shell=True, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        rc, output = (e.returncode, e.output.strip())

    if rc and verbose:
        print("Running {} failed (error[{}])".format(cmd, rc))

    if verbose:
        print("Output:\n", output)

    return rc, output


def bf_log(msg, level=verbose):
    if level:
        print(msg)
    cmd = "logger -t {} -i '{}'".format(prog, msg)
    get_status_output(cmd, False)
    return 0


def verify_args(args):
    rc = 0
    msg = ""
    if (args.op not in SUPPORTED_OPERATIONS):
        msg = "ERROR: Operation {} is not supported".format(args.op)
        rc = 1
    if (args.action not in SUPPORTED_ACTIONS):
        msg = "ERROR: Action {} is not supported".format(args.action)
        rc = 1
    if args.op not in ["dnsconfig", "domainconfig"] and not args.port:
        msg = "ERROR: Port number have to be provided. Use '--port'"
        rc = 1

    return rc, msg


def validIPAddress(IP: str) -> str:
    try:
        return "IPv4" if type(ip_address(IP)) is IPv4Address else "IPv6"
    except ValueError:
        return "Invalid"


def netmask_to_prefix(netmask):
    """
    Convert NETMASK to PREFIX
    """
    return(sum([ bin(int(bits)).count("1") for bits in netmask.split(".") ]))


def main():

    global verbose
    rc = 0
    result = {"status": 0, "output": ""}

    if os.geteuid() != 0:
        sys.exit('root privileges are required to run this script!')

    parser = argparse.ArgumentParser(description='Configure network interfaces')
#    parser.add_argument('--permanent', action='store_true', help="Keep network configuration permanent", default=True)
    parser.add_argument('--op', required='--version' not in sys.argv, choices=SUPPORTED_OPERATIONS, help="Operation")
    parser.add_argument('--device', help="Network device name")
    parser.add_argument('--action', required='--version' not in sys.argv, choices=SUPPORTED_ACTIONS, help="Action")
    parser.add_argument('--get_devices', action='store_true', help="Print network interface bound to the provided port", default=False)
    parser.add_argument('--port', required='--get-devices' in sys.argv, choices=['0', '1'], help="HCA port 0|1")
    parser.add_argument('--ipv4_addr', help="IPv4 address")
    parser.add_argument('--ipv4_prefix', help="Network prefix for IPv4 address.")
    parser.add_argument('--ipv6_addr', help="IPv6 address")
    parser.add_argument('--ipv6_prefix', help="Network prefix for IPv6 address.")
    parser.add_argument('--network', help="Subnet network")
    parser.add_argument('--network_prefix', help="PREFIX to use with route add", default=0)
    parser.add_argument('--ipv4_gateway', help="IPv4 gateway address")
    parser.add_argument('--ipv6_gateway', help="IPv6 gateway address")
    parser.add_argument('--metric', help="Metric for the default route using ipv4_gateway")
#    parser.add_argument('--bootproto', help="BOOTPROTO=none|static|bootp|dhcp")
    parser.add_argument('--mtu', help="Default MTU for this device")
#    parser.add_argument('--nmcontrolled', help="NMCONTROLLED=yes|no")
    parser.add_argument('--ipv4_nameservers', action='append', nargs='+', help="DNS server IP. Use multiple times for the list of DNS servers")
    parser.add_argument('--ipv6_nameservers', action='append', nargs='+', help="DNS server IP. Use multiple times for the list of DNS servers")
    parser.add_argument('--domains', action='append', nargs='+', help="Search domain name. Use multiple times for the list of domains")
    parser.add_argument('--type', choices=['lossy', 'lossless'], help="RoCE type")
    parser.add_argument('--trust', choices=['pcp', 'dscp'], help="RoCE trust")
    parser.add_argument('--ecn', action='append', nargs='+', help="enable/disable ECN for priority. Use multiple times")
    parser.add_argument('--dscp2prio', help="RoCE set/del a (dscp,prio) mapping. e.g: 'del,30,2'.")
    parser.add_argument('--prio_tc', action='append', nargs='+', help="RoCE priority to traffic class mapping. Use multiple times")
    parser.add_argument('--pfc', action='append', nargs='+', help="RoCE priority to traffic class. Use multiple times")
    parser.add_argument('--cable_len',  help="Len for buffer's xoff and xon thresholds calculation")
    parser.add_argument('--prio2buffer', action='append', nargs='+', help="Priority to receive buffer. Use multiple times")
    parser.add_argument('--ratelimit', action='append', nargs='+', help="Rate limit per traffic class (in Gbps). Use multiple times")
    parser.add_argument('--buffer_size', action='append', nargs='+', help="Receive buffer size. Use multiple times")
    parser.add_argument('--roce_accl', action='append', nargs='+', help="field=value advanced accelerations. Use multiple times")
    parser.add_argument('--show',  help="Show parameter value")
#    parser.add_argument('--vlan', help="VLAN=yes|no", default='no')
#    parser.add_argument('--onboot', help="ONBOOT 'yes' or 'no'", default='yes')
    parser.add_argument('--verbose', action='store_true', help="Print verbose information", default=False)
    parser.add_argument('--version', action='store_true', help='Display program version information and exit')


    args = parser.parse_args()
    if args.version:
        version()
        sys.exit(rc)

    verbose = args.verbose
    if verbose:
        print(args)

    rc, msg = verify_args(args)
    if rc:
        result["output"] = msg
        result["status"] = rc
        bf_log(result["output"], rc)
        sys.exit(rc)

    bfconfig = BFCONFIG(args)

    if args.get_devices:
        print (bfconfig.devices)
        sys.exit(0)

    if args.action == 'show':
        result = bfconfig.show()
        print(json.dumps(result, indent=None))
        sys.exit(result['status'])

    # TBD:
    # Add restore factory default parameter
    # nmcontrolled
    # ipv4_nameservers
    # search
    # vlan
    # RoCE
    # Exit if restricted host

    if not os.path.exists(network_config):
        result["output"] = "ERROR: network configuration file {} does not exist".format(network_config)
        result["status"] = 1
        bf_log(result["output"], 1)
        sys.exit(1)

    if not os.path.exists(network_config_orig):
        shutil.copy2(network_config, network_config_orig)

    shutil.copy2(network_config, network_config_backup)

    if not os.path.exists(resolv_conf_orig):
        shutil.copy2(resolv_conf, resolv_conf_orig)

    if args.verbose:
        print ("Operation: ", args.op)

    if args.op in ["ipconfig", "mtuconfig", "gwconfig"]:
        rc = bfconfig.set_network_config()
        if rc:
            sys.exit(rc)

        rc = bfconfig.apply_config()
        if rc:
            bf_log("Reverting configuration")
            shutil.copy2(network_config, network_config + ".bad")
            shutil.copy2(network_config_backup, network_config)
            rc1 = bfconfig.apply_config()
            if rc1:
                bf_log("Restoring factory default configuration")
                shutil.copy2(network_config_orig, network_config)
                rc2 = bfconfig.apply_config()
                if rc2:
                    bf_log("ERR: Failed to restore factory default configuration")

    if args.op in ["dnsconfig", "domainconfig"]:
        rc = bfconfig.set_resolv_conf()
        if rc:
            sys.exit(rc)

    if args.op in ["roceconfig"]:
        if not os.path.exists(MLXREG):
            bf_log("ERR: mlxreg tool does not exist. Cannot show/set RoCE configuration")
            sys.exit(1)

        rc = bfconfig.set_roce_config()
        if rc:
            sys.exit(rc)

    sys.exit(rc)


if __name__ == '__main__':
        main()
