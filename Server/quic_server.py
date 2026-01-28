import json
import math
import random
import socket
import time
import uuid
import asyncio
import pygame
import struct
from aioquic.asyncio import serve, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import HandshakeCompleted, StreamDataReceived


CONNECTED_CLIENTS = set()
SPEED = 3
WIDTH = 1200
HEIGHT = 700
UP = 1 << 0
LEFT = 1 << 1
DOWN = 1 << 2
RIGHT = 1 << 3
MAP_WIDTH = 2500
MAP_HEIGHT = 1500
PLAYER_WIDTH = 37
PLAYER_HEIGHT = 56


class GameServerProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.x = WIDTH//2 - 18
        self.y = HEIGHT//2 - 28
        self.client_id = None
        self.last_seq = 0
        self.control_stream_id = None
        self.state_stream_id = None
        self.recv_buffer = bytearray()
        self.last_heartbeat = time.time()
        self.heartbeat_timeout = 5.0
        # Each client connection has:
        # - Its own protocol object
        # - Its own player state

    def quic_event_received(self, event): # This is the only function QUIC calls.
        # Handshake
        if isinstance(event, HandshakeCompleted): # If an event is detected, check if it`s a handshake completion
            print("Client connected")
            self.client_id = uuid.uuid4()
            self.control_stream_id = self._quic.get_next_available_stream_id(False)
            self.state_stream_id = self._quic.get_next_available_stream_id(True)
            self.broadcast_new_connection()
            CONNECTED_CLIENTS.add(self)
            # Client is now authenticated & encrypted.
            # Each connection performs a handshake to connect, once the event is completed
            payload = struct.pack("!B16sff", 0, self.client_id.bytes, self.x, self.y)
            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.control_stream_id, packet, end_stream=False)
            self.transmit()

            self.broadcast_online_clients()

        # Receiving game data
        elif isinstance(event, StreamDataReceived): # If the event detected is a stream of data, receive it
            self.recv_buffer.extend(event.data)

            while True:
                if len(self.recv_buffer) < 2:
                    return # Not enough for length

                msg_len = struct.unpack("!H", self.recv_buffer[:2])[0]

                if len(self.recv_buffer) < 2 + msg_len:
                    return # Wait for full message

                payload = self.recv_buffer[2:2 + msg_len]
                del self.recv_buffer[:2 + msg_len]

                self.handle_message(payload)

    def handle_message(self, data):
        msg_type = data[0]  # We use binary protocol. The first byte is the message type

        if msg_type == 1:  # If msg type is 1 (aka I chose it to be intent movement)
            intent, seq = struct.unpack("!BH", data[1:])  # Unpack the data as a structure
            self.last_seq = seq
            print("Received intent {}".format(str(intent)))
            self.change_pos(intent)
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
            payload = struct.pack("!B", 6) # msg type 6 = pong
            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.control_stream_id, packet, end_stream=False)
            self.transmit()

    def change_pos(self, intent):
        dx = dy = 0

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

        self.collisions(dx, dy)

    def collisions(self, dx, dy):
        # ---- Separate axis collisions ----
        new_x = self.x + dx
        new_y = self.y + dy

        for client in CONNECTED_CLIENTS:
            if client is self:
                continue
            overlap_x = abs(new_x - client.x) < PLAYER_WIDTH
            overlap_y = abs(new_y - client.y) < PLAYER_HEIGHT

            # Check if overlapping currently
            currently_overlap_x = abs(self.x - client.x) < PLAYER_WIDTH
            currently_overlap_y = abs(self.y - client.y) < PLAYER_HEIGHT

            if currently_overlap_x and currently_overlap_y:
                # Only allow movement that increases distance
                if dx > 0 and self.x < client.x:  # moving right into them? block
                    new_x = self.x
                if dx < 0 and self.x > client.x:  # moving left into them? block
                    new_x = self.x
                if dy > 0 and self.y < client.y:  # moving down into them? block
                    new_y = self.y
                if dy < 0 and self.y > client.y:  # moving up into them? block
                    new_y = self.y
            else:
                # Normal collision handling: prevent entering other players
                if overlap_x and overlap_y:
                    # Axis-separated collision
                    if abs(dx) > abs(dy):
                        new_x = self.x
                    else:
                        new_y = self.y

        # Clamp to map boundaries
        new_x = max(0, min(new_x, MAP_WIDTH - PLAYER_WIDTH))
        new_y = max(0, min(new_y, MAP_HEIGHT - PLAYER_HEIGHT))

        # Apply movement
        self.x = new_x
        self.y = new_y

    def connection_loss(self):
        if self in list(CONNECTED_CLIENTS):
            CONNECTED_CLIENTS.remove(self)

        print(f"Client {self.client_id} disconnected")
        for client in list(CONNECTED_CLIENTS):
                payload = struct.pack (
                    "!B16s",
                    3,
                    self.client_id.bytes
                )

                packet = struct.pack("!H", len(payload)) + payload
                client._quic.send_stream_data(client.state_stream_id, packet, end_stream=False)
                client.transmit()

    def connection_lost(self, exc):
        self.connection_loss()

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


async def broadcast_server():
    await asyncio.sleep(0.5) # allow server socket to bind
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)  # allow the socket to send on broadcast
    message = json.dumps(
        {
            "service": "mmo-server",
            "host": "game-server.local",
            "port": 4433
        }
    ).encode() # create a small json message

    while True:
        sock.sendto(message, ("255.255.255.255", 37020)) # broadcast on port 37020
        print("sent Broadcast")
        await asyncio.sleep(4) # broadcast every 4 seconds


async def check_heartbeats():
    """Background task to detect dead connections"""
    while True:
        await asyncio.sleep(2)
        current_time = time.time()
        dead_clients = []

        for client in list(CONNECTED_CLIENTS):
            if current_time - client.last_heartbeat > client.heartbeat_timeout:
                dead_clients.append(client)

        for client in dead_clients:
            print(f"Client {client.client_id} timed out (no heartbeat)")
            client.connection_loss()
            try:
                client._quic.close()
            except:
                print("could not close quic connection")
                pass


async def start_server():
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
    asyncio.run(main())