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
MAP_PATH = "new_map.txt"

SPEED = 3
SPRINT_SPEED = 6
CROUCH_SPEED = 1

WIDTH = 1200
HEIGHT = 700

SERVER_TIMEOUT = 6.0
PING_INTERVAL = 2.0

UP = 1 << 0
LEFT = 1 << 1
DOWN = 1 << 2
RIGHT = 1 << 3
SPRINT = 1 << 4
CROUCH = 1 << 5

DIR_MASK = UP | LEFT | DOWN | RIGHT

MAP_WIDTH = 1920 * 40 # 76800 pixels
MAP_HEIGHT = 1080 * 40 # 43200 pixels
MAP_HALF_WIDTH = MAP_WIDTH // 2
MAP_HALF_HEIGHT = MAP_HEIGHT // 2

PLAYER_WIDTH = 37
PLAYER_HEIGHT = 56

HP_BAR_WIDTH = 40
HP_BAR_HEIGHT = 6
HP_BAR_OFFSET_Y = 10

TILE_SIZE =40
TILE_DEFS = {
    '.': ("ground.png", True),

    '#': ("lava.png", False),

    '←': ("grnd_lava_left.png", True),
    '→': ("grnd_lava_right.png", True),
    '↑': ("grnd_lava_up.png", True),
    '↓': ("grnd_lava_down.png", True),

    '↖': ("grnd_lava_up_left.png", True),
    '↗': ("grnd_lava_up_right.png", True),
    '↘': ("grnd_lava_down_right.png", True),
    '↙': ("grnd_lava__left_down.png", True),

    '⇦': ("grnd_lava_up_right_down.png", True),
    '⇨': ("grnd_lava_up_left_down.png", True),
    '⇧': ("grnd_lava_left_down_right.png", True),
    '⇩': ("grnd_lava_left_up_right.png", True),
}
TILE_DICT = {}
HALF_TILE = TILE_SIZE // 2

LAVA_DAMAGE = 2.5
LAVA_INTERVAL = 0.5

SEQ_BITS = 16
SEQ_MAX = 1 << SEQ_BITS
SEQ_HALF = SEQ_MAX >> 1


class Player:
    def __init__(self):
        super().__init__()
        self.x = -PLAYER_WIDTH // 2
        self.y = -PLAYER_HEIGHT // 2
        self.hp = 100

class GameClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_id = None
        self.connected = False

        self.player = Player()
        self.players = {}

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

        self.initialized = False

        self.last_lava_check = time.monotonic()
        self.local_damage_seq = 0
        self.last_server_damage_seq = 0
        self.pending_damage = []

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


    def _handle_message(self, data, stream_id):
        self.last_server_activity = time.monotonic()
        msg_type = data[0]

        if msg_type == 1:  # check if it's a world update (1 = world update from server)
            raw_id, x, y, hp = struct.unpack("!16sfff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id != self.client_id:
                if client_id not in self.players:
                    player = Player()
                    rect = self.image.get_rect()
                    self.players[client_id] = [player, rect]

                if client_id in self.players:
                    self.players[client_id][0].x = x
                    self.players[client_id][0].y = y
                    self.players[client_id][0].hp = hp

        elif msg_type == 0:  # message after handshake
            raw_id, x, y, hp = struct.unpack("!16sfff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            self.control_stream_id = stream_id
            self.client_id = client_id
            self.players[client_id] = [self.player, self.rect]
            self.players[client_id][0].x = x
            self.players[client_id][0].y = y
            self.players[client_id][0].hp = hp
            self.initialized = True

        elif msg_type == 3:  # a player disconnected
            raw_id = struct.unpack("!16s", data[1:])[0]
            client_id = uuid.UUID(bytes=raw_id)
            self.players.pop(client_id, None)

        elif msg_type == 2:  # players already online
            raw_id, x, y, hp = struct.unpack("!16sfff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id != self.client_id:
                if client_id not in self.players:
                    player = Player()
                    rect = self.image.get_rect()
                    self.players[client_id] = [player, rect]
                    self.players[client_id][0].hp = hp
                    self.players[client_id][0].x = x
                    self.players[client_id][0].y = y
                    self.players[client_id][1].x = self.players[self.client_id][0].x - int(x)
                    self.players[client_id][1].y = self.players[self.client_id][0].y - int(y)

        elif msg_type == 4:  # local movement update
            raw_id, x, y, last_seq = struct.unpack("!16sffH", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id == self.client_id:
                self.players[self.client_id][0].x = x
                self.players[self.client_id][0].y = y

                self.pending_inputs = [
                    (seq, intent)
                    for (seq, intent) in self.pending_inputs
                    if seq_newer(seq, last_seq)
                ]
                # these lines discard the old intents already confirmed by the server
                # and keeps the ones that have not yet been confirmed by the server

                for _, intent in self.pending_inputs:
                    if intent & DIR_MASK:
                        self._prediction(intent)

        elif msg_type == 6:
            pass

        elif msg_type == 7: # local hp change
            raw_id, hp, server_seq = struct.unpack("!16sfH", data[1:])
            cid = uuid.UUID(bytes=raw_id)
            if cid != self.client_id:
                return

            # authoritative snap
            self.last_server_damage_seq = server_seq
            self.player.hp = hp

            # if server healed us (respawn), clear predictions
            if hp == 100:
                self.pending_damage.clear()
                self.local_damage_seq = server_seq

            # discard confirmed predictions
            self.pending_damage = [
                seq for seq in self.pending_damage if seq_newer(seq, server_seq)
            ]

            # reapply unconfirmed predicted damage
            for _ in self.pending_damage:
                self.player.hp -= LAVA_DAMAGE

        elif msg_type == 8:
            raw_id, hp, server_seq = struct.unpack("!16sfH", data[1:])
            cid = uuid.UUID(bytes=raw_id)
            if cid != self.client_id:
                self.players[cid][0].hp = hp

    def send_intent(self, intent):
        if not self.initialized:
            return

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
        if not self.initialized:
            return

        if self.client_id not in self.players:
            return

        local_player = self.players[self.client_id][0]

        # draw background using camera offset
        cam_x = local_player.x - (WIDTH // 2)
        cam_y = local_player.y - (HEIGHT // 2)

        cam_x = max(-MAP_HALF_WIDTH, min(cam_x, MAP_HALF_WIDTH - WIDTH))
        cam_y = max(-MAP_HALF_HEIGHT, min(cam_y, MAP_HALF_HEIGHT - HEIGHT))

        world_left = cam_x
        world_right = cam_x + WIDTH
        world_top = cam_y
        world_bottom = cam_y + HEIGHT

        left = int(((world_left + MAP_HALF_WIDTH) // TILE_SIZE)) - 1
        right = int(((world_right + MAP_HALF_WIDTH) // TILE_SIZE)) + 1
        top = int(((world_top + MAP_HALF_HEIGHT) // TILE_SIZE)) - 1
        bottom = int(((world_bottom + MAP_HALF_HEIGHT) // TILE_SIZE)) + 1

        left = max(left, 0)
        right = min(right, 1919)
        top = max(top, 0)
        bottom = min(bottom, 1079)

        for ty in range(top, bottom + 1):
            for tx in range(left, right + 1):

                tile = TILE_DICT.get((tx, ty))
                if tile is None:
                    continue

                image, world_x, world_y, _ = tile

                screen_x = world_x - cam_x
                screen_y = world_y - cam_y

                screen.blit(image,(screen_x, screen_y))

        max_hp = 100

        for pid, (player, _) in self.players.items():
            if pid != self.client_id:
                screen_x = player.x - cam_x
                screen_y = player.y - cam_y
                screen.blit(self.image, (screen_x, screen_y))


        for pid, (player, _) in self.players.items():
            if pid == self.client_id:
                continue

            ratio = max(0, player.hp) / max_hp

            screen_x = player.x - cam_x
            screen_y = player.y - cam_y

            bar_x = screen_x + PLAYER_WIDTH // 2 - HP_BAR_WIDTH // 2
            bar_y = screen_y - HP_BAR_OFFSET_Y

            pygame.draw.rect(
                screen,
                (255, 0, 0),
                (bar_x, bar_y, HP_BAR_WIDTH, HP_BAR_HEIGHT)
            )
            pygame.draw.rect(
                screen,
                (0, 255, 0),
                (bar_x, bar_y, HP_BAR_WIDTH * ratio, HP_BAR_HEIGHT)
            )

        item = self.players[self.client_id]
        screen_x = item[0].x - cam_x
        screen_y = item[0].y - cam_y
        screen.blit(self.image, (screen_x, screen_y))

        ratio = max(0, self.player.hp) / max_hp
        pygame.draw.rect(screen, (255, 0, 0), (20, 40, 200, 10))
        pygame.draw.rect(screen, (0, 255, 0), (20, 40, 200 * ratio, 10))

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
            scale = 1/math.sqrt(2)
            dx *= scale
            dy *= scale

        if dx != 0 or dy != 0:
            self.collisions(dx, dy)

    def collisions(self, dx, dy):
        # ---- Separate axis collisions ----
        local_player = self.players[self.client_id][0]

        allow_x = True
        allow_y = True

        for pid, (client, _) in self.players.items():
            if pid == self.client_id:
                continue

            overlap_x = abs(local_player.x - client.x) < PLAYER_WIDTH
            overlap_y = abs(local_player.y - client.y) < PLAYER_HEIGHT

            if overlap_x and overlap_y:
                if dx != 0 and (local_player.x - client.x) * dx < 0:
                    allow_x = False
                if dy != 0 and (local_player.y - client.y) * dy < 0:
                    allow_y = False
                continue

            if dx != 0:
                test_x = local_player.x + dx
                if abs(test_x - client.x) < PLAYER_WIDTH and abs(local_player.y - client.y) < PLAYER_HEIGHT:
                    allow_x = False

            if dy != 0:
                test_y = local_player.y + dy
                if abs(local_player.x - client.x) < PLAYER_WIDTH and abs(test_y - client.y) < PLAYER_HEIGHT:
                    allow_y = False

        if allow_x:
            local_player.x += dx
        if allow_y:
            local_player.y += dy

        new_x = max(
            -MAP_HALF_WIDTH,
            min(local_player.x, MAP_HALF_WIDTH - PLAYER_WIDTH)
        )
        new_y = max(
            -MAP_HALF_HEIGHT,
            min(local_player.y, MAP_HALF_HEIGHT - PLAYER_HEIGHT)
        )

        # Apply movement
        self.players[self.client_id][0].x = new_x
        self.players[self.client_id][0].y = new_y

    def convert_images(self):
        self.image = self.image.convert_alpha()
        for ch, (img, walkable) in TILE_DEFS.items():
            img = pygame.image.load(img)
            TILE_DEFS[ch] = (img.convert(), walkable)

    def is_in_lava(self):
        tx = int((self.player.x + MAP_HALF_WIDTH) // TILE_SIZE)
        ty = int((self.player.y + (PLAYER_HEIGHT - 15) + MAP_HALF_HEIGHT) // TILE_SIZE)
        return not TILE_DICT.get((tx, ty), True)

    def predict_lava_if_needed(self):
        # only predict if:
        # 1. we are standing in lava
        # 2. we have NO unconfirmed damage predicted
        if not self.is_in_lava():
            return

        if self.pending_damage:
            return  # already predicted one, wait for server

        # predict exactly ONE future server damage
        self.local_damage_seq = (self.local_damage_seq + 1) & 0xFFFF
        self.pending_damage.append(self.local_damage_seq)
        self.player.hp -= LAVA_DAMAGE


def seq_newer(a, b):
    return ((a - b) & (SEQ_MAX - 1)) < SEQ_HALF


async def load_tile_map(path: str):
    tile_dict = {}

    with open(path, "r", encoding="utf-8") as f:
        for ty, line in enumerate(f):
            for tx, ch in enumerate(line.strip("\n")):
                if ch not in TILE_DEFS:
                    continue

                image, walkable = TILE_DEFS[ch]

                world_x = tx * TILE_SIZE - MAP_HALF_WIDTH
                world_y = ty * TILE_SIZE - MAP_HALF_HEIGHT

                tile_dict[(tx, ty)] = (image, world_x, world_y, walkable)

    return tile_dict


async def display_fps(screen, clock):
    fnt = pygame.font.SysFont("Italian", 20)
    text_to_show = fnt.render(str(int(clock.get_fps())), 0, pygame.Color("Green"))
    screen.blit(text_to_show, (0, 0))


async def game_loop(client: GameClientProtocol):
    global TILE_DICT
    pygame.init()

    width, height = 1200, 700
    screen = pygame.display.set_mode((width, height))
    clock = pygame.time.Clock()
    pygame.display.set_caption("MMO Game")

    client.convert_images()
    TILE_DICT = await load_tile_map(MAP_PATH)

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
        client.predict_lava_if_needed()

        if current_time - last_input_time >= input_cooldown:
            keys = pygame.key.get_pressed()
            mods = pygame.key.get_mods()
            intent = 0

            if keys[pygame.K_UP]:
                intent |= UP

            if keys[pygame.K_LEFT]:
                intent |= LEFT

            if keys[pygame.K_DOWN]:
                intent |= DOWN

            if keys[pygame.K_RIGHT]:
                intent |= RIGHT

            if mods & pygame.KMOD_CTRL:
                intent |= SPRINT

            if mods & pygame.KMOD_SHIFT:
                intent |= CROUCH

            if intent & DIR_MASK: # Only check the direction bits (if at least one is on, continue)
                client.send_intent(intent)  # send intent
                last_input_time = current_time

        if now - client.last_ping_sent >= PING_INTERVAL:
            if client.initialized:
                client.send_heartbeat()
                client.last_ping_sent = now

        if now - client.last_server_activity > SERVER_TIMEOUT:
            client.connected = False
            print("exiting")
            pygame.quit()
            sys.exit(0)

        client.draw(screen)
        await display_fps(screen, clock)
        pygame.display.flip()
        clock.tick(60)
        await asyncio.sleep(0)

    print("disconnecting...")
    client.send_disconnect()
    await asyncio.sleep(0.1)
    pygame.quit()


async def discover_server(timeout=5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 37020))
    sock.settimeout(timeout)

    try:
        data, addr = sock.recvfrom(1024)
        info = json.loads(data.decode())
        if info["service"] == "mm0Rgb-!#sErv-7":
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
        print("exception occurred", repr(e))
    finally:
        pygame.quit()


if __name__ == "__main__":
    try:
        print("running")
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        sys.exit(0)