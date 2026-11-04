"""Dedicated DEMO guest boundary, enforced in the host's network namespace."""
import ipaddress
import re

BRIDGE = 'demo-br0'
TAP = 'demo-tap0'
GATEWAY = '172.31.250.1'
GUEST = '172.31.250.2'
TABLE = 'demo_vm_guard'


def render(uplink, public_ip):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', uplink):
        raise ValueError('Invalid uplink interface')
    addr = ipaddress.IPv4Address(public_ip)
    if not addr.is_global:
        raise ValueError('Expected observed public IPv4')
    return f'''table inet {TABLE} {{
    set nonpublic_v4 {{
        type ipv4_addr; flags interval;
        elements = {{ 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10,
            127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12,
            192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16,
            198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24,
            224.0.0.0/4, 240.0.0.0/4 }}
    }}
    set home_public_v4 {{ type ipv4_addr; elements = {{ {addr} }}; }}
    counter blocked_host {{ }}
    counter blocked_private {{ }}
    counter blocked_other {{ }}
    counter blocked_inbound {{ }}
    chain input {{
        type filter hook input priority -50; policy accept;
        iifname "{BRIDGE}" meta nfproto ipv6 counter name blocked_host drop
        iifname "{BRIDGE}" ip saddr != {GUEST} counter name blocked_host drop
        iifname "{BRIDGE}" ct state established,related accept
        iifname "{BRIDGE}" ip daddr {GATEWAY} udp dport 53 accept
        iifname "{BRIDGE}" ip daddr {GATEWAY} tcp dport 53 accept
        iifname "{BRIDGE}" counter name blocked_host drop
    }}
    chain forward {{
        type filter hook forward priority -50; policy drop;
        iifname "{BRIDGE}" meta nfproto ipv6 counter name blocked_other drop
        iifname "{BRIDGE}" ip saddr != {GUEST} counter name blocked_other drop
        iifname "{BRIDGE}" ip daddr @nonpublic_v4 counter name blocked_private drop
        iifname "{BRIDGE}" ip daddr @home_public_v4 counter name blocked_private drop
        # Ubuntu chrony authenticates public NTP via NTS-KE (TCP 4460).
        # Private/home/host destinations above remain blocked on every port.
        iifname "{BRIDGE}" oifname "{uplink}" tcp dport {{ 80, 443, 465, 587, 4460 }} accept
        iifname "{BRIDGE}" oifname "{uplink}" udp dport 123 accept
        oifname "{BRIDGE}" ip daddr {GUEST} ct state established,related accept
        oifname "{BRIDGE}" counter name blocked_inbound drop
        counter name blocked_other drop
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr {GUEST} oifname "{uplink}" masquerade
    }}
}}
'''
