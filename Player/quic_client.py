import asyncio
import ssl
import struct
import uuid
import pygame
from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import HandshakeCompleted, StreamDataReceived
from collections import deque

IMAGE = 'spidermanStanding.png'
IMAGE_BACKGROUND = 'background.png'
SPEED = 4
backgroundX = -290
backgroundY = -210
otherPlayerX = 0
otherPlayerY = 0

class GameClientProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_id = None
        self.players = {}
        self.connected = False
        self.image = pygame.image.load(IMAGE)
        self.rect = self.image.get_rect()
        self.rect.x = 1920/2
        self.rect.y = 1080/2
        self.input_seq = 0
        self.pending_inputs = []
        self.control_stream_id = None
        self.input_stream_id = None
        self.recv_buffer = bytearray()
        self.last_ping_time = 0
        self.ping_interval = 2.0  # Send ping every 2 seconds
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
        if self.connected and self.input_stream_id is not None:
            payload = struct.pack("!B", 5)  # msg_type 5 = ping
            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.input_stream_id, packet, end_stream=False)
            self.transmit()
            print("sent heartbeat")

    def _handle_message(self, data, stream_id):
        global otherPlayerX, otherPlayerY
        msg_type = data[0]

        if msg_type == 1:  # check if it's a world update (1 = world update from server)
            raw_id, x, y = struct.unpack("!16sff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id != self.client_id:
                if client_id not in self.players:
                    rect = self.image.get_rect()
                    self.players[client_id] = rect

                if client_id in self.players:
                    self.players[client_id].x = int(x)-(otherPlayerX/2)
                    self.players[client_id].y = int(y)-(otherPlayerY/2)

        elif msg_type == 0:  # message after handshake
            raw_id = struct.unpack("!16s", data[1:])[0]
            client_id = uuid.UUID(bytes=raw_id)
            self.control_stream_id = stream_id
            self.client_id = client_id
            self.players[client_id] = self.rect
            self.players[client_id].x = 1920/2
            self.players[client_id].y = 1080/2

        elif msg_type == 3:  # a player disconnected
            raw_id = struct.unpack("!16s", data[1:])[0]
            client_id = uuid.UUID(bytes=raw_id)
            self.players.pop(client_id)

        elif msg_type == 2:  # players already online
            raw_id, x, y = struct.unpack("!16sff", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id not in self.players:
                rect = self.image.get_rect()
                self.players[client_id] = rect
                self.players[client_id].x = int(x)-(otherPlayerX)
                self.players[client_id].y = int(y)-(otherPlayerY)
                otherPlayerX = otherPlayerX*2
                otherPlayerY = otherPlayerY*2


        elif msg_type == 4:  # local movement update
            raw_id, x, y, last_seq = struct.unpack("!16sffH", data[1:])
            client_id = uuid.UUID(bytes=raw_id)
            if client_id == self.client_id:
                self.players[self.client_id].x = 1920/2
                self.players[self.client_id].y = 1080/2

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
        for rect in self.players.values():
            screen.blit(self.image, rect)

    def send_disconnect(self):
        if self.control_stream_id is not None:
            self.connected = False
            payload = struct.pack("!BB", 0, 0)
            packet = struct.pack("!H", len(payload)) + payload
            self._quic.send_stream_data(self.control_stream_id, packet, end_stream=True)
            self.transmit()

    def _prediction(self, intent):
        global backgroundX, backgroundY
        global otherPlayerX, otherPlayerY
        if self.client_id not in self.players:
            return

        if intent == 1:
            backgroundY += SPEED

        elif intent == 3:
            backgroundY -= SPEED

        elif intent == 2:
            backgroundX += SPEED

        elif intent == 4:
            backgroundX -= SPEED


        self.move_other_players(intent)


    def move_other_players(self, intent):
        global otherPlayerX, otherPlayerY
        for player in self.players.values():
            if player != self.client_id:
                if intent == 1:
                    player.y += SPEED
                    otherPlayerY -= SPEED
                elif intent == 3:
                    player.y -= SPEED
                    otherPlayerY += SPEED
                elif intent == 2:
                    player.x += SPEED
                    otherPlayerX -= SPEED
                elif intent == 4:
                    player.x -= SPEED
                    otherPlayerX += SPEED

    def display_fps(self, screen, clock):
        fnt = pygame.font.SysFont("Arial", 26)
        text_to_show = fnt.render(str(int(clock.get_fps())), 0, pygame.Color("red"))
        screen.blit(text_to_show, (0, 0))


async def game_loop(client: GameClientProtocol):
    pygame.init()

    width, height = 1920, 1080
    screen = pygame.display.set_mode((width, height))
    clock = pygame.time.Clock()
    pygame.display.set_caption("MMO Game")
    backgroundIMG = pygame.image.load(IMAGE_BACKGROUND)
    running = True
    last_heartbeat = 0
    heartbeat_interval = 2.0

    last_input_time = 0
    input_cooldown = 1 / 30

    while running:
        current_time = pygame.time.get_ticks() / 1000.0

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

        client.process_pending_messages()

        if current_time - last_input_time >= input_cooldown:
            keys = pygame.key.get_pressed()
            intent = 0

            if keys[pygame.K_UP]:
                intent = 1
            elif keys[pygame.K_LEFT]:
                intent = 2
            elif keys[pygame.K_DOWN]:
                intent = 3
            elif keys[pygame.K_RIGHT]:
                intent = 4

            if intent != 0:
                client.send_intent(intent)  # send intent
                last_input_time = current_time
                print("sent intent {}".format(intent))

        if current_time - last_heartbeat >= heartbeat_interval:
            client.send_heartbeat()
            last_heartbeat = current_time

        screen.blit(backgroundIMG, (backgroundX, backgroundY))
        client.draw(screen)
        client.display_fps(screen, clock)
        pygame.display.flip()
        await asyncio.sleep(1 / 120)
        clock.tick(120)

    client.send_disconnect()
    await asyncio.sleep(0.1)
    pygame.quit()


async def main():
    configuration = QuicConfiguration(
        is_client=True,
        alpn_protocols=["mmo"]  # Set label as mmo
    )
    # For self-signed certs → disable verification (LAN only!)
    configuration.verify_mode = ssl.CERT_REQUIRED
    configuration.load_verify_locations("ca.cert.pem")
    try:
        async with connect(
                "127.0.0.1",
                4433,  # let os choose a free port
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
        import sys

        sys.exit(0)