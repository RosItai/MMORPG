import json
import math
import socket
import time
import uuid
import asyncio
import struct
import redis.asyncio as redis
from aioquic.asyncio import serve, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import HandshakeCompleted, StreamDataReceived

# ===========================
# GLOBALS
# ===========================

CONNECTED_CLIENTS = set()

SPEED = 3
SPRINT_SPEED = 6
CROUCH_SPEED = 1

UP = 1 << 0
LEFT = 1 << 1
DOWN = 1 << 2
RIGHT = 1 << 3
SPRINT = 1 << 4
CROUCH = 1 << 5

DIR_MASK = UP | LEFT | DOWN | RIGHT

MAP_WIDTH = 2500
MAP_HEIGHT = 1500
PLAYER_WIDTH = 37
PLAYER_HEIGHT = 56

redis_client: redis.Redis | None = None
redis_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

# ===========================
# REDIS HELPERS
# ===========================

async def try_enqueue_redis(snapshot):
    try:
        redis_queue.put_nowait(snapshot)
    except asyncio.QueueFull:
        # drop write
        pass


def redis_player_key(client_id: uuid.UUID) -> str:
    return f"player:{client_id}"


async def redis_load_player_state(client_id: uuid.UUID):
    key = redis_player_key(client_id)
    data = await redis_client.hgetall(key)

    if not data:
        return None

    return {
        "x": float(data[b"x"]),
        "y": float(data[b"y"]),
        "last_seq": int(data[b"last_seq"]),
        "last_heartbeat": float(data[b"last_heartbeat"]),
    }


async def redis_writer():
    while True:
        snapshot = await redis_queue.get()

        try:
            key = redis_player_key(snapshot["client_id"])
            await redis_client.hset(
                key,
                mapping={
                    "x": snapshot["x"],
                    "y": snapshot["y"],
                    "last_seq": snapshot["last_seq"],
                    "last_heartbeat": snapshot["last_heartbeat"],
                }
            )
        except Exception as e:
            print("redis write failed:", e)


# ===========================
# QUIC GAME SERVER
# ===========================


class GameServerProtocol(QuicConnectionProtocol):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.x = MAP_WIDTH//2 - 18
        self.y = MAP_HEIGHT//2 - 28

        self.client_id: uuid.UUID | None = None
        self.last_seq = 0

        self.control_stream_id = None
        self.state_stream_id = None

        self.recv_buffer = bytearray()

        self.last_heartbeat = time.time()
        self.heartbeat_timeout = 5.0

    # ===========================
    # QUIC EVENTS
    # ===========================

    def quic_event_received(self, event): # This is the only function QUIC calls.

        if isinstance(event, HandshakeCompleted):
            asyncio.create_task(self.safe_handle_handshake())
            print("Hand shake complete")

        elif isinstance(event, StreamDataReceived):
            self.recv_buffer.extend(event.data)
            self.process_recv_buffer()

    # ===========================
    # HANDSHAKE
    # ===========================

    async def handle_handshake(self):
        print("Client connected")

        self.client_id = uuid.uuid4()

        self.control_stream_id = self._quic.get_next_available_stream_id(False)
        self.state_stream_id = self._quic.get_next_available_stream_id(True)

        # Authoritative load
        state = await redis_load_player_state(self.client_id)

        if state:
            self.x = state["x"]
            self.y = state["y"]
            self.last_seq = state["last_seq"]
            self.last_heartbeat = state["last_heartbeat"]
        else:
            # First time players
            self.x = MAP_WIDTH//2 - 18
            self.y = MAP_HEIGHT//2 - 28
            self.last_seq = 0
            self.last_heartbeat = time.time()

            snapshot = ({
                "client_id": self.client_id,
                "x": self.x,
                "y": self.y,
                "last_seq": self.last_seq,
                "last_heartbeat": self.last_heartbeat
            })

            await try_enqueue_redis(snapshot)

        CONNECTED_CLIENTS.add(self)

        payload = struct.pack("!B16sff", 0, self.client_id.bytes, self.x, self.y)
        packet = struct.pack("!H", len(payload)) + payload
        self._quic.send_stream_data(self.control_stream_id, packet, end_stream=False)
        self.transmit()

        await asyncio.sleep(0.5)

        self.broadcast_new_connection()

        self.broadcast_online_clients()

    async def safe_handle_handshake(self):
        try:
            await self.handle_handshake()
        except Exception as e:
            print("handshake failed:", e)
            try:
                self._quic.close()
            except:
                pass

    # ===========================
    # MESSAGE HANDLING
    # ===========================

    def process_recv_buffer(self):
        while True:
            if len(self.recv_buffer) < 2:
                return  # Not enough for length

            msg_len = struct.unpack("!H", self.recv_buffer[:2])[0]

            if len(self.recv_buffer) < 2 + msg_len:
                return  # Wait for full message

            payload = self.recv_buffer[2:2 + msg_len]
            del self.recv_buffer[:2 + msg_len]

            self.handle_message(payload)

    def handle_message(self, data):
        msg_type = data[0]  # We use binary protocol. The first byte is the message type

        if msg_type == 1:  # If msg type is 1 (aka I chose it to be intent movement)
            intent, seq = struct.unpack("!BH", data[1:])  # Unpack the data as a structure
            if intent & DIR_MASK == 0:
                return
            self.last_seq = seq
            self.change_pos(intent)

            snapshot = ({
                "client_id": self.client_id,
                "x": self.x,
                "y": self.y,
                "last_seq": self.last_seq,
                "last_heartbeat": self.last_heartbeat
            })

            asyncio.create_task(try_enqueue_redis(snapshot))

            # Send authoritative state back
            self.broadcast_world_state()
            self.send_self_movement()

        elif msg_type == 0:
            typee = struct.unpack("!B", data[1:])
            typee = int(typee[0])
            if typee == 0:
                self.connection_loss()

        elif msg_type == 5:
            self.last_heartbeat = time.time()
            snapshot = ({
                "client_id": self.client_id,
                "x": self.x,
                "y": self.y,
                "last_seq": self.last_seq,
                "last_heartbeat": self.last_heartbeat
            })

            asyncio.create_task(try_enqueue_redis(snapshot))

            payload = struct.pack("!B", 6) # msg type 6 = pong
            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.control_stream_id, packet, end_stream=False)
            self.transmit()

    # ===========================
    # MOVEMENT & COLLISIONS
    # ===========================

    def change_pos(self, intent):
        dx = dy = 0

        if intent & SPRINT and not intent & CROUCH:
            if intent & UP:
                dy -= SPRINT_SPEED
            if intent & DOWN:
                dy += SPRINT_SPEED
            if intent & LEFT:
                dx -= SPRINT_SPEED
            if intent & RIGHT:
                dx += SPRINT_SPEED
        elif intent & CROUCH and not intent & SPRINT:
            if intent & UP:
                dy -= CROUCH_SPEED
            if intent & DOWN:
                dy += CROUCH_SPEED
            if intent & LEFT:
                dx -= CROUCH_SPEED
            if intent & RIGHT:
                dx += CROUCH_SPEED
        else:
            if intent & UP:
                dy -= SPEED
            if intent & DOWN:
                dy += SPEED
            if intent & LEFT:
                dx -= SPEED
            if intent & RIGHT:
                dx += SPEED

        if dx != 0 and dy != 0:
            scale = 1 / math.sqrt(2)
            dx *= scale
            dy *= scale

        if dx != 0 or dy != 0:
            self.collisions(dx, dy)

    def collisions(self, dx, dy):
        allow_x = True
        allow_y = True

        for client in list(CONNECTED_CLIENTS):
            if client is self:
                continue

            # --- Check overlap ---
            overlap_x = abs(self.x - client.x) < PLAYER_WIDTH
            overlap_y = abs(self.y - client.y) < PLAYER_HEIGHT

            # --- If overlapping: only allow moving AWAY ---
            if overlap_x and overlap_y:
                if dx != 0 and (self.x - client.x) * dx < 0:
                    allow_x = False
                if dy != 0 and (self.y - client.y) * dy < 0:
                    allow_y = False
                continue

            # --- Normal collision ---
            if dx != 0:
                test_x = self.x + dx
                if abs(test_x - client.x) < PLAYER_WIDTH and abs(self.y - client.y) < PLAYER_HEIGHT:
                    allow_x = False

            if dy != 0:
                test_y = self.y + dy
                if abs(self.x - client.x) < PLAYER_WIDTH and abs(test_y - client.y) < PLAYER_HEIGHT:
                    allow_y = False

        # --- Apply movement ONCE ---
        if allow_x:
            self.x += dx
        if allow_y:
            self.y += dy

        # --- Clamp to map ---
        self.x = max(0, min(self.x, MAP_WIDTH - PLAYER_WIDTH))
        self.y = max(0, min(self.y, MAP_HEIGHT - PLAYER_HEIGHT))

    # ===========================
    # CONNECTION LOSS
    # ===========================

    def connection_loss(self):
        if self in CONNECTED_CLIENTS:
            CONNECTED_CLIENTS.remove(self)

        if self.client_id:
            asyncio.create_task(
                redis_client.expire(redis_player_key(self.client_id),30) # Expire after 30 seconds
            )

        print(f"Client {self.client_id} disconnected")

        for client in list(CONNECTED_CLIENTS):
                payload = struct.pack ("!B16s",3,self.client_id.bytes)
                packet = struct.pack("!H", len(payload)) + payload
                client._quic.send_stream_data(client.state_stream_id, packet, end_stream=False)
                client.transmit()

    def connection_lost(self, exc):
        self.connection_loss()

    # ===========================
    # BROADCASTS
    # ===========================

    def broadcast_world_state(self):
        for client in list(CONNECTED_CLIENTS):
            if client is not self:
                payload = struct.pack(
                    "!B16sff",           # means transfer one byte 16 bytes and two floats
                    1,                    # msg_type = world update
                    self.client_id.bytes,     # who moved in bytes format
                    self.x,
                    self.y
                )

                packet = struct.pack("!H", len(payload)) + payload
                client._quic.send_stream_data(client.state_stream_id, packet, end_stream=False)
                client.transmit()

    def broadcast_online_clients(self):
        for client in list(CONNECTED_CLIENTS):
            payload = struct.pack(
                "!B16sff",
                2,                    # msg_type = online members
                client.client_id.bytes,
                client.x,
                client.y
            )

            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.state_stream_id, packet, end_stream=False)
            self.transmit()

    def broadcast_new_connection(self):
        for client in list(CONNECTED_CLIENTS):
            payload = struct.pack(
                "!B16sff",
                2,
                self.client_id.bytes,
                self.x,
                self.y
            )

            packet = struct.pack("!H", len(payload)) + payload
            client._quic.send_stream_data(client.state_stream_id, packet, end_stream=False)
            client.transmit()

    def send_self_movement(self):
        payload = struct.pack(
            "!B16sffH",
            4,                  #local movement update
            self.client_id.bytes,
            self.x,
            self.y,
            self.last_seq
        )

        packet = struct.pack("!H", len(payload)) + payload
        self._quic.send_stream_data(self.control_stream_id, packet, end_stream=False)
        self.transmit()


# ===========================
# BACKGROUND TASKS
# ===========================


async def broadcast_server():
    await asyncio.sleep(0.5) # allow server socket to bind
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)  # allow the socket to send on broadcast

    message = json.dumps(
        {
            "service": "mm0Rgb-!#sErv-7",
            "host": "game-server.local",
            "port": 4433
        }
    ).encode() # create a small json message

    while True:
        sock.sendto(message, ("255.255.255.255", 37020)) # broadcast on port 37020
        await asyncio.sleep(4) # broadcast every 4 seconds


async def check_heartbeats():
    """Background task to detect dead connections"""
    while True:
        await asyncio.sleep(2)
        current_time = time.time()

        for client in list(CONNECTED_CLIENTS):
            if current_time - client.last_heartbeat > client.heartbeat_timeout:
                print(f"Client {client.client_id} timed out (no heartbeat)")
                client.connection_loss()
                try:
                    client._quic.close()
                except:
                    print("cant close connection")
                    pass


async def start_server():
    global redis_client

    redis_client = redis.Redis(host="localhost", port=6379, decode_responses=False)

    # Quic settings
    config = QuicConfiguration(
        is_client=False,  # This is not a client this is a server.
        alpn_protocols=["mmo"]  # ALPN = Aplication Layer Protocol Negotiation.
        # This means after encryption starts, it asks what kind of protocol are you using?
        # And I say mmo (its like a handshake label, there is no such protocol as mmo).
    )
    config.load_cert_chain(certfile="server.cert.pem", keyfile="server.key.pem")
    # The certificate contains my public key and the server identity info.
    # The certificate proves who you are and the private key proves you own it.

    asyncio.create_task(redis_writer())
    asyncio.create_task(check_heartbeats())

    await serve(  # Pause the whole function until this is done (until server is fully started)
        "0.0.0.0",  # Anyone wanting to connect can connect
        4433,  # The server is on port 4433
        configuration=config,  # Set the configuration (rules of the connection)
        create_protocol=GameServerProtocol  # For each client connection, create a new GameServerProtocol objet
    )

    try:
        await asyncio.Future()  # Run this forever
    except asyncio.CancelledError:
        print()


async def main():
    server_task = asyncio.create_task(start_server())
    broadcast_task = asyncio.create_task(broadcast_server())
    try:
        await asyncio.gather(server_task, broadcast_task)
    except asyncio.CancelledError:
        print()


if __name__ == "__main__":
    print("server is running...")
    asyncio.run(main())