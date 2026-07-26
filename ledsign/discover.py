"""
discover.py — find DayStar / DBStar LED signs on the local network by multicast.

A sign advertises nothing until probed. This mirrors the discovery handshake used
by the vendor application so a host can find a sign and learn its control endpoint
WITHOUT knowing the sign's IP or subnet ahead of time.

Sequence (see docs/protocol-daystar.md §6):

    probe   : UDP -> 224.0.0.1:6007   12-byte frame, command 0x1002
                                   (+ ASCII "Is anybody there?")
    reply   : sign announces on group 224.5.6.8:6007  (ASCII, contains the sign's
             IP and identity)
    connect : TCP <sign-ip>:6006   (hand off to DayStarSign)

Multicast is link-local: it does NOT cross routers, VPNs, or Tailscale. You must
run this from a host on the sign's own L2 segment. If you can `ping` the sign but
this finds nothing, you are almost certainly on a routed path rather than the
same wire — be on the panel's physical LAN (or a host bridged to it).

The probe layout is CONFIRMED (recovered from the vendor application). The reply
payload is ASCII and carries at least the sign's IP; its exact field order is
PARTIAL — parsed best-effort here. A single capture on-LAN would pin it.
"""
from __future__ import annotations
import socket, struct, sys, time, argparse, re

PROBE_GROUP = "224.0.0.1"      # where the probe is sent (all-hosts)
ANSWER_GROUP = "224.5.6.8"     # group the sign announces itself on
PORT = 6007                    # UDP discovery port (send and receive)
CONTROL_PORT = 6006            # TCP control channel on the sign
MAGIC = 0x73                   # 's'  (0x74 't' on the newer protocol variant)
CMD_DISCOVER = 0x1002          # command word (0x1003 is a variant)
ALT_GROUP = "239.255.19.56"    # alternate/legacy group referenced by the app


def build_probe(cmd: int = CMD_DISCOVER, ver=(0, 0, 0), magic: int = MAGIC) -> bytes:
    """12-byte probe frame: [magic][verMin][verMid][verMaj][cmd u32 LE][param u32 LE].

    The version bytes are a *minimum* firmware gate; (0,0,0) is accepted in
    practice. `0x1002` little-endian on the wire is `02 10 00 00`.
    """
    return (bytes([magic, ver[0] & 0xff, ver[1] & 0xff, ver[2] & 0xff])
            + struct.pack("<I", cmd) + struct.pack("<I", 0))


def parse_reply(data: bytes, src_ip: str) -> dict:
    """Best-effort parse of the ASCII announcement. [PARTIAL] — exact field order
    unverified; confirm against one on-LAN capture."""
    txt = data.decode("ascii", "replace")
    ips = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", txt)
    nums = re.findall(r"\b\d{4,7}\b", txt)          # display numbers, e.g. 920248
    sign_ip = ips[0] if ips else src_ip
    return {
        "from": src_ip,
        "sign_ip": sign_ip,
        "display_no?": next((n for n in nums if len(n) >= 5), None),
        "control_endpoint": f"{sign_ip}:{CONTROL_PORT}",
        "ascii": txt.strip(),
        "hex": data.hex(),
    }


def discover(secs: float = 5.0, iface: str | None = None) -> list[dict]:
    """Send the probe and collect replies for `secs` seconds.

    `iface` is a local interface IP to send/join on; useful when the host has
    several interfaces and only one is on the sign's segment.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    s.bind(("", PORT))
    # join the announcement group so we hear the sign's reply
    mreq = struct.pack("=4s4s", socket.inet_aton(ANSWER_GROUP),
                       socket.inet_aton(iface or "0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    if iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    # don't hear our own probe echoed back (we are an implicit member of 224.0.0.1)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    except OSError:
        pass

    probes = {build_probe(), b"Is anybody there?"}   # both variants
    for p in probes:
        s.sendto(p, (PROBE_GROUP, PORT))

    found, seen = [], set()
    s.settimeout(0.5)
    t_end = time.monotonic() + secs
    while time.monotonic() < t_end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        if addr[0] in seen or not data or data in probes:
            continue
        seen.add(addr[0])
        found.append(parse_reply(data, addr[0]))
    s.close()
    return found


def main(argv):
    ap = argparse.ArgumentParser(description="Discover DayStar/DBStar LED signs on the LAN")
    ap.add_argument("--secs", type=float, default=5.0, help="seconds to listen")
    ap.add_argument("--iface", default=None, help="local interface IP to send/join on")
    a = ap.parse_args(argv)
    print(f"probe -> {PROBE_GROUP}:{PORT}  (frame {build_probe().hex()} + \"Is anybody there?\")")
    print(f"listening on {ANSWER_GROUP}:{PORT} for {a.secs:g}s ...\n")
    signs = discover(a.secs, a.iface)
    if not signs:
        print("no signs found (are you on the sign's L2 segment? multicast does not cross "
              "routers/VPNs — try --iface <your-lan-ip>)")
        return 1
    for i, sg in enumerate(signs, 1):
        print(f"[{i}] sign at {sg['sign_ip']}  ->  connect {sg['control_endpoint']}")
        if sg["display_no?"]:
            print(f"     display#: {sg['display_no?']}")
        print(f"     ascii : {sg['ascii'][:200]}")
        print(f"     hex   : {sg['hex'][:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
