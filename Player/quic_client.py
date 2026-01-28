import asyncio
import json
import math
import socket
import ssl
import struct
import time
import uuid
import pygame
import sys
from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import HandshakeCompleted, StreamDataReceived
from collections import deque


IMAGE = 'men-stands.png'
IMAGE_BK = "Background.png"
SPEED = 3
WIDTH = 1200
HEIGHT = 700
SERVER_TIMEOUT = 6.0
PING_INTERVAL = 2.0
UP = 1 << 0
LEFT = 1 << 1
DOWN = 1 << 2
RIGHT = 1 << 3
MAP_WIDTH = 2500
MAP_HEIGHT = 1500
CAM_BORDER_X = WIDTH // 4   # 25% of the left/right
CAM_BORDER_Y = HEIGHT // 4  # 25% of the top/bottom
PLAYER_WIDTH = 37
PLAYER_HEIGHT = 56


class Player:
    def __init__(self):
        super().__init__()
        self.x = WIDTH//2 - 18
        self.y = HEIGHT//2 - 28


class GameClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_id = None
        self.player = Player()
        self.connected = False
        self.players = {}
        self.image_bk = pygame.image.load(IMAGE_BK)
        self.image = pygame.image.load(IMAGE)
        self.rect = self.image.get_rect()
        self.rect.x = WIDTH//2 - 18
        self.rect.y = HEIGHT//2 - 28
        self.input_seq = 0
        self.pending_inputs = []
        self.control_stream_id = None
        self.input_stream_id = None
        self.recv_buffer = bytearray()
        self.last_server_activity = time.monotonic()
        self.last_ping_sent = 0.0
        self.message_queue = deque()

    def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            print("connected to server")
            self.connected = True
            self.input_stream_id = self._quic.get_next_available_stream_id(True)

        elif isinstance(event, StreamDataReceived):
            self.recv_buffer.extend(event.data)
            self._process_buffer(event)

    def _process_buffer(self, event):
        """process all complete messages in the buffer"""
        while True:
            if len(self.recv_buffer) < 2:
                return  # Not enough for length

            msg_len = struct.unpack("!H", self.recv_buffer[:2])[0]

            if len(self.recv_buffer) < 2 + msg_len:
                return  # Wait for full message

            payload = self.recv_buffer[2:2 + msg_len]
            del self.recv_buffer[:2 + msg_len]

            self.message_queue.append((payload, event.stream_id))

    def process_pending_messages(self):
        """call this from the game loop to precess network messages"""
        while self.message_queue:
            payload, stream_id = self.message_queue.popleft()
            self._handle_message(payload, stream_id)

    def send_heartbeat(self):
        if not self.connected or self.input_stream_id is None:
            return

        payload = struct.pack("!B", 5)  # msg_type 5 = ping
        packet = struct.pack("!H", len(payload)) + payload
        self._quic.send_stream_data(self.input_stream_id, packet, end_stream=False)
        self.transmit()
        print("sent heartbeat")


    def _handle_message(self, data, stream_id):
        self.last_server_activity = time.monotonic()
        msg_type = data[0]

        if msg_type == 1:  # check if it's a world update (1 = world update from server)
            raw_id, x, y = struct.unpack("!16sff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id != self.client_id:
                if client_id not in self.players:
                    player = Player()
                    rect = self.image.get_rect()
                    self.players[client_id] = [player, rect]

                if client_id in self.players:
                    self.players[client_id][0].x = int(x)
                    self.players[client_id][0].y = int(y)

        elif msg_type == 0:  # message after handshake
            raw_id, x, y = struct.unpack("!16sff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            self.control_stream_id = stream_id
            self.client_id = client_id
            self.players[client_id] = [self.player, self.rect]
            self.players[client_id][0].x = int(x)
            self.players[client_id][0].y = int(y)

        elif msg_type == 3:  # a player disconnected
            raw_id = struct.unpack("!16s", data[1:])[0]
            client_id = uuid.UUID(bytes=raw_id)
            self.players.pop(client_id, None)

        elif msg_type == 2:  # players already online
            raw_id, x, y = struct.unpack("!16sff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id != self.client_id:
                if client_id not in self.players:
                    player = Player()
                    rect = self.image.get_rect()
                    self.players[client_id] = [player, rect]
                    self.players[client_id][0].x = int(x)
                    self.players[client_id][0].y = int(y)
                    self.players[client_id][1].x = self.players[self.client_id][0].x - int(x)
                    self.players[client_id][1].y = self.players[self.client_id][0].y - int(y)

        elif msg_type == 4:  # local movement update
            raw_id, x, y, last_seq = struct.unpack("!16sffH", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id == self.client_id:
                self.players[self.client_id][0].x = int(x)
                self.players[self.client_id][0].y = int(y)

                self.pending_inputs = [
                    (seq, intent)
                    for (seq, intent) in self.pending_inputs
                    if seq > last_seq
                ]
                # these lines discard the old intents already confirmed by the server
                # and keeps the ones that have not yet been confirmed by the server

                for _, intent in self.pending_inputs:
                    self._prediction(intent)

        elif msg_type == 6:
            print("connection alive")

    def send_intent(self, intent):
        if self.client_id not in self.players:
            return

        if not self.connected or self.input_stream_id is None:
            return

        self.input_seq += 1
        self.pending_inputs.append((self.input_seq, intent))

        self._prediction(intent)
        payload = struct.pack("!BBH", 1, intent, self.input_seq)  # H stands for unsigned short
        packet = struct.pack("!H", len(payload)) + payload
        self._quic.send_stream_data(self.input_stream_id, packet, end_stream=False)
        self.transmit()

    def draw(self, screen):
        if self.client_id not in self.players:
            return

        local_player = self.players[self.client_id][0]

        # draw background using camera offset
        cam_x = local_player.x - WIDTH // 2
        cam_y = local_player.y - HEIGHT // 2

        cam_x = max(0, min(cam_x, MAP_WIDTH - WIDTH))
        cam_y = max(0, min(cam_y, MAP_HEIGHT - HEIGHT))

        bg_x = -cam_x
        bg_y = -cam_y

        screen.blit(self.image_bk, (bg_x, bg_y))

        for pid, (player, _) in self.players.items():
            if pid != self.client_id:
                screen_x = player.x - cam_x
                screen_y = player.y - cam_y
                screen.blit(self.image, (screen_x, screen_y))

        item = self.players[self.client_id]
        screen_x = item[0].x - cam_x
        screen_y = item[0].y - cam_y
        screen.blit(self.image, (screen_x, screen_y))

    def send_disconnect(self):
        if self.client_id not in self.players:
            return

        if self.control_stream_id is not None:
            self.connected = False
            payload = struct.pack("!BB", 0, 0)
            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.control_stream_id, packet, end_stream=True)
            self.transmit()

    def _prediction(self, intent):
        if self.client_id not in self.players:
            return

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
            scale = 1/math.sqrt(2)
            dx *= scale
            dy *= scale

        self.collisions(dy, dx)

    def collisions(self, dy, dx):
        # ---- Separate axis collisions ----
        local_player = self.players[self.client_id][0]
        new_x = local_player.x + dx
        new_y = local_player.y + dy

        for pid, (client, _) in self.players.items():
            if pid != self.client_id:
                continue
            overlap_x = abs(new_x - client.x) < PLAYER_WIDTH
            overlap_y = abs(new_y - client.y) < PLAYER_HEIGHT

            # Check if overlapping currently
            currently_overlap_x = abs(local_player.x - client.x) < PLAYER_WIDTH
            currently_overlap_y = abs(local_player.y - client.y) < PLAYER_HEIGHT

            if currently_overlap_x and currently_overlap_y:
                # Only allow movement that increases distance
                if dx > 0 and local_player.x < client.x:  # moving right into them? block
                    new_x = local_player.x
                if dx < 0 and local_player.x > client.x:  # moving left into them? block
                    new_x = local_player.x
                if dy > 0 and local_player.y < client.y:  # moving down into them? block
                    new_y = local_player.y
                if dy < 0 and local_player.y > client.y:  # moving up into them? block
                    new_y = local_player.y
            else:
                # Normal collision handling: prevent entering other players
                if overlap_x and overlap_y:
                    # Axis-separated collision
                    if abs(dx) > abs(dy):
                        new_x = local_player.x
                    else:
                        new_y = local_player.y

        # Clamp to map boundaries
        new_x = max(0, min(new_x, MAP_WIDTH - PLAYER_WIDTH))
        new_y = max(0, min(new_y, MAP_HEIGHT - PLAYER_HEIGHT))

        # Apply movement
        self.players[self.client_id][0].x = new_x
        self.players[self.client_id][0].y = new_y


async def display_fps(screen, clock):
    fnt = pygame.font.SysFont("Italian", 20)
    text_to_show = fnt.render(str(int(clock.get_fps())), 0, pygame.Color("Green"))
    screen.blit(text_to_show, (0, 0))


async def game_loop(client: GameClientProtocol):
    pygame.init()

    width, height = 1200, 700
    screen = pygame.display.set_mode((width, height))
    clock = pygame.time.Clock()
    pygame.display.set_caption("MMO Game")

    running = True

    last_input_time = 0
    input_cooldown = 1 / 30

    while running:
        current_time = pygame.time.get_ticks() / 1000.0
        now = time.monotonic()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

        client.process_pending_messages()

        if current_time - last_input_time >= input_cooldown:
            keys = pygame.key.get_pressed()
            intent = 0

            if keys[pygame.K_UP]:
                intent |= UP

            if keys[pygame.K_LEFT]:
                intent |= LEFT

            if keys[pygame.K_DOWN]:
                intent |= DOWN

            if keys[pygame.K_RIGHT]:
                intent |= RIGHT


            if intent != 0:
                client.send_intent(intent)  # send intent
                last_input_time = current_time
                print("sent intent {}".format(intent))

        if now -client.last_ping_sent >= PING_INTERVAL:
            client.send_heartbeat()
            client.last_ping_sent = now

        if now - client.last_server_activity > SERVER_TIMEOUT:
            client.connected = False
            pygame.quit()
            sys.exit(0)

        screen.fill((255, 255, 255))
        client.draw(screen)
        await display_fps(screen, clock)
        pygame.display.flip()
        clock.tick(240)
        await asyncio.sleep(0)

    client.send_disconnect()
    await asyncio.sleep(0.1)
    pygame.quit()


async def discover_server(timeout=5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 37020))
    sock.settimeout(timeout)

    try:
        data, addr = sock.recvfrom(1024)
        info = json.loads(data.decode())
        if info["service"] == "mmo-server":
            return addr[0], info["port"], info["host"]
    except socket.timeout:
        return None


async def main():
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=["mmo"]  # Set label as mmo
    )
    # For self-signed certs → disable verification (LAN only!)
    configuration.verify_mode = ssl.CERT_REQUIRED
    configuration.load_verify_locations("ca.cert.pem")

    result = await discover_server()
    if result is None:
        print("No Server Found")
        return

    server_ip, port, server_host = result

    configuration.server_name = server_host

    try:
        async with connect(
            server_ip,
            port,               # let os choose a free port
            configuration=configuration,
            create_protocol=GameClientProtocol,
            stream_handler=None  # Optional: skips auto stream handling since we are custom
        ) as client:
            print("Client connecting...")

            while not client.connected:
                await asyncio.sleep(0.01)

            await game_loop(client)
    except Exception as e:
        print(e)
    finally:
        pygame.quit()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:

        sys.exit(0)