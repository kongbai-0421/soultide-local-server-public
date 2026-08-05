"""Minimal offline DNS server for the Soul Tide local stack."""

import ipaddress
import logging
import os
import socket
import struct
from logging.handlers import RotatingFileHandler


ROOT = os.environ.get("SOULTIDE_ROOT", os.path.dirname(os.path.abspath(__file__)))
LOCAL_IP = os.environ.get("SOULTIDE_SERVER_IP", "192.168.1.136")
TTL = 60
DNS_PORT = int(os.environ.get("SOULTIDE_DNS_PORT", "53"))
LOCAL_HOSTS = {
    "login-onigao-1.iqigame.com",
    "cdn-onigao-1.iqigame.com",
    "cdn-sdk.iqigame.com",
    "usdk.iqigame.com",
    "sdk.9game.cn",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DNS] %(message)s",
    handlers=[
        RotatingFileHandler(
            ROOT + r"\dns_server.log",
            maxBytes=2 * 1024 * 1024,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("dns_server")


def parse_question(packet):
    offset = 12
    labels = []
    while True:
        if offset >= len(packet):
            raise ValueError("truncated qname")
        length = packet[offset]
        offset += 1
        if length == 0:
            break
        if length & 0xC0:
            raise ValueError("compressed question is unsupported")
        end = offset + length
        if end > len(packet):
            raise ValueError("truncated label")
        labels.append(packet[offset:end].decode("ascii").lower())
        offset = end
    if offset + 4 > len(packet):
        raise ValueError("truncated question fields")
    qtype, qclass = struct.unpack_from(">HH", packet, offset)
    return ".".join(labels), qtype, qclass, offset + 4


def response_header(packet, flags, answers):
    return packet[:2] + struct.pack(">HHHHH", flags, 1, answers, 0, 0)


def build_response(packet):
    name, qtype, qclass, question_end = parse_question(packet)
    question = packet[12:question_end]

    if name not in LOCAL_HOSTS:
        log.info("%s type=%d -> NXDOMAIN", name, qtype)
        return response_header(packet, 0x8183, 0) + question

    if qclass != 1 or qtype != 1:
        log.info("%s type=%d -> NOERROR/empty", name, qtype)
        return response_header(packet, 0x8180, 0) + question

    address = ipaddress.IPv4Address(LOCAL_IP).packed
    answer = (
        b"\xc0\x0c"
        + struct.pack(">HHIH", 1, 1, TTL, len(address))
        + address
    )
    log.info("%s -> %s", name, LOCAL_IP)
    return response_header(packet, 0x8180, 1) + question + answer


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", DNS_PORT))
    log.info("DNS server running on 0.0.0.0:%d (unknown names: NXDOMAIN)", DNS_PORT)
    while True:
        packet, address = sock.recvfrom(4096)
        try:
            sock.sendto(build_response(packet), address)
        except Exception as exc:
            log.warning("bad query from %s: %s", address, exc)


if __name__ == "__main__":
    main()
