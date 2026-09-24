"""Minimal MQTT 3.1.1 broker and client (dependency-free) for the IoT layer of the lab.

Supported: CONNECT/CONNACK, PUBLISH QoS 0 and 1 (PUBACK), SUBSCRIBE/SUBACK with
+ and # wildcards, UNSUBSCRIBE, PINGREQ/PINGRESP, DISCONNECT, retained messages.
Not supported: QoS 2, persistent sessions, TLS (deliberately: the lab's broker
is the kind of cleartext IoT service 0xPlant's discovery flags with OXP-010).
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("plant.mqtt")

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 8, 9, 10, 11, 12, 13, 14


def _encode_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + _encode_len(len(body)) + body


async def read_packet(reader: asyncio.StreamReader) -> Tuple[int, int, bytes]:
    first = await reader.readexactly(1)
    ptype, flags = first[0] >> 4, first[0] & 0x0F
    mult, length = 1, 0
    for _ in range(4):
        b = (await reader.readexactly(1))[0]
        length += (b & 0x7F) * mult
        mult *= 128
        if not b & 0x80:
            break
    else:
        raise ValueError("malformed remaining length")
    if length > 1_000_000:
        raise ValueError("packet too large")
    return ptype, flags, await reader.readexactly(length) if length else b""


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return True
        if i >= len(t) or (seg != "+" and seg != t[i]):
            return False
    return len(p) == len(t)


def encode_publish(topic: str, payload: bytes, qos: int = 0, retain: bool = False, pid: int = 0) -> bytes:
    body = _str(topic) + (struct.pack(">H", pid) if qos else b"") + payload
    return packet(PUBLISH, (qos << 1) | (1 if retain else 0), body)


class Session:
    def __init__(self, client_id: str, writer: asyncio.StreamWriter, peer: str):
        self.client_id = client_id
        self.writer = writer
        self.peer = peer
        self.subscriptions: Set[str] = set()
        self.connected_at = time.time()
        self.published = 0


class MQTTBroker:
    def __init__(self, host: str = "0.0.0.0", port: int = 1883, on_publish: Optional[Callable[[str, str, bytes, str], None]] = None):
        self.host, self.port = host, port
        self.sessions: Dict[str, Session] = {}
        self.retained: Dict[str, bytes] = {}
        self.on_publish = on_publish
        self._server: Optional[asyncio.AbstractServer] = None
        self.messages = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("MQTT broker listening on %s:%d (cleartext, no authentication - as found on many plant floors)", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for s in list(self.sessions.values()):
                s.writer.close()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = str((writer.get_extra_info("peername") or ("?",))[0])
        session: Optional[Session] = None
        try:
            ptype, _flags, body = await asyncio.wait_for(read_packet(reader), 10)
            if ptype != CONNECT:
                return
            pos = 0
            plen = struct.unpack(">H", body[pos:pos + 2])[0]
            pos += 2 + plen                     # protocol name
            pos += 1                            # level
            conn_flags = body[pos]
            pos += 3                            # flags + keepalive
            cid_len = struct.unpack(">H", body[pos:pos + 2])[0]
            client_id = body[pos + 2:pos + 2 + cid_len].decode("utf-8", "replace") or f"anon-{peer}"
            _ = conn_flags
            session = Session(client_id, writer, peer)
            self.sessions[client_id] = session
            writer.write(packet(CONNACK, 0, b"\x00\x00"))
            await writer.drain()
            while True:
                ptype, flags, body = await asyncio.wait_for(read_packet(reader), 300)
                if ptype == PUBLISH:
                    qos = (flags >> 1) & 3
                    tlen = struct.unpack(">H", body[:2])[0]
                    topic = body[2:2 + tlen].decode("utf-8", "replace")
                    pos = 2 + tlen
                    pid = 0
                    if qos:
                        pid = struct.unpack(">H", body[pos:pos + 2])[0]
                        pos += 2
                    payload = body[pos:]
                    session.published += 1
                    self.messages += 1
                    if flags & 1:
                        self.retained[topic] = payload
                    if self.on_publish:
                        self.on_publish(client_id, topic, payload, peer)
                    await self._fanout(topic, payload)
                    if qos == 1:
                        writer.write(packet(PUBACK, 0, struct.pack(">H", pid)))
                        await writer.drain()
                elif ptype == SUBSCRIBE:
                    pid = struct.unpack(">H", body[:2])[0]
                    pos, codes = 2, bytearray()
                    while pos < len(body):
                        tlen = struct.unpack(">H", body[pos:pos + 2])[0]
                        pattern = body[pos + 2:pos + 2 + tlen].decode("utf-8", "replace")
                        pos += 3 + tlen
                        session.subscriptions.add(pattern)
                        codes.append(0)
                        for topic, payload in list(self.retained.items()):
                            if topic_matches(pattern, topic):
                                writer.write(encode_publish(topic, payload, retain=True))
                    writer.write(packet(SUBACK, 0, struct.pack(">H", pid) + bytes(codes)))
                    await writer.drain()
                elif ptype == UNSUBSCRIBE:
                    pid = struct.unpack(">H", body[:2])[0]
                    pos = 2
                    while pos < len(body):
                        tlen = struct.unpack(">H", body[pos:pos + 2])[0]
                        session.subscriptions.discard(body[pos + 2:pos + 2 + tlen].decode("utf-8", "replace"))
                        pos += 2 + tlen
                    writer.write(packet(UNSUBACK, 0, struct.pack(">H", pid)))
                    await writer.drain()
                elif ptype == PINGREQ:
                    writer.write(packet(PINGRESP, 0, b""))
                    await writer.drain()
                elif ptype == DISCONNECT:
                    break
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, ValueError, struct.error):
            pass
        finally:
            if session and self.sessions.get(session.client_id) is session:
                del self.sessions[session.client_id]
            writer.close()

    async def _fanout(self, topic: str, payload: bytes) -> None:
        for s in list(self.sessions.values()):
            if any(topic_matches(p, topic) for p in s.subscriptions):
                try:
                    s.writer.write(encode_publish(topic, payload))
                    await s.writer.drain()
                except (ConnectionError, RuntimeError):
                    pass


class MQTTClient:
    def __init__(self, host: str, port: int = 1883, client_id: str = "client", timeout: float = 5.0):
        self.host, self.port, self.client_id, self.timeout = host, port, client_id, timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pid = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        body = _str("MQTT") + bytes([4, 0x02]) + struct.pack(">H", 60) + _str(self.client_id)
        self._writer.write(packet(CONNECT, 0, body))
        await self._writer.drain()
        ptype, _f, body = await asyncio.wait_for(read_packet(self._reader), self.timeout)
        if ptype != CONNACK or body[1] != 0:
            raise ConnectionError("broker refused connection")

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.write(packet(DISCONNECT, 0, b""))
                await self._writer.drain()
            except (ConnectionError, RuntimeError):
                pass
            self._writer.close()
        self._reader = self._writer = None

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        async with self._lock:
            assert self._writer
            self._pid = (self._pid % 65535) + 1
            self._writer.write(encode_publish(topic, payload, qos, retain, self._pid))
            await self._writer.drain()

    async def subscribe(self, *patterns: str) -> None:
        async with self._lock:
            assert self._writer
            self._pid = (self._pid % 65535) + 1
            body = struct.pack(">H", self._pid) + b"".join(_str(p) + b"\x00" for p in patterns)
            self._writer.write(packet(SUBSCRIBE, 2, body))
            await self._writer.drain()

    async def messages(self):
        """Async generator yielding (topic, payload, retained) for incoming PUBLISH packets."""
        assert self._reader
        while True:
            ptype, flags, body = await read_packet(self._reader)
            if ptype == PUBLISH:
                tlen = struct.unpack(">H", body[:2])[0]
                topic = body[2:2 + tlen].decode("utf-8", "replace")
                pos = 2 + tlen
                qos = (flags >> 1) & 3
                if qos:
                    pid = struct.unpack(">H", body[pos:pos + 2])[0]
                    pos += 2
                    if self._writer:
                        self._writer.write(packet(PUBACK, 0, struct.pack(">H", pid)))
                yield topic, body[pos:], bool(flags & 1)
            elif ptype == PINGREQ and self._writer:
                self._writer.write(packet(PINGRESP, 0, b""))
