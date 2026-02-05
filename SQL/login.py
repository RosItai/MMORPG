import pygame
import sys
import sqlite3
import random

pygame.init()

# -------- Settings --------
WIDTH, HEIGHT = 760, 460
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Login / Sign Up (UI + basic check)")

FONT = pygame.font.SysFont(None, 32)
FONT_BIG = pygame.font.SysFont(None, 44)
FONT_SMALL = pygame.font.SysFont(None, 24)

BG = (20, 22, 28)
PANEL = (30, 34, 44)
TEXT = (230, 230, 230)
MUTED = (160, 160, 160)
BOX = (45, 50, 65)
BOX_ACTIVE = (80, 130, 255)

BTN = (55, 140, 90)
BTN_HOVER = (75, 170, 110)
BTN2 = (170, 70, 70)
BTN2_HOVER = (200, 90, 90)
BTN3 = (80, 110, 180)
BTN3_HOVER = (105, 135, 210)

clock = pygame.time.Clock()

# -------- Helpers --------
def draw_text(surface, txt, pos, color=TEXT, font=FONT):
    surface.blit(font.render(txt, True, color), pos)

class InputBox:
    def __init__(self, x, y, w, h, label, is_password=False):
        self.rect = pygame.Rect(x, y, w, h)
        self.label = label
        self.text = ""
        self.active = False
        self.is_password = is_password

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            self.active = self.rect.collidepoint(event.pos)

        if event.type == pygame.KEYDOWN and self.active:
            if event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.unicode.isprintable():
                self.text += event.unicode

    def draw(self, surface):
        draw_text(surface, self.label, (self.rect.x, self.rect.y - 26), MUTED, FONT_SMALL)

        pygame.draw.rect(surface, BOX, self.rect, border_radius=8)
        border_color = BOX_ACTIVE if self.active else (70, 75, 90)
        pygame.draw.rect(surface, border_color, self.rect, 2, border_radius=8)

        shown = "*" * len(self.text) if self.is_password else self.text
        txt_surf = FONT.render(shown, True, TEXT)
        surface.blit(txt_surf, (self.rect.x + 12,
                                self.rect.y + (self.rect.height - txt_surf.get_height()) // 2))

class Button:
    def __init__(self, x, y, w, h, text, base_color, hover_color):
        self.rect = pygame.Rect(x, y, w, h)
        self.text = text
        self.base = base_color
        self.hover = hover_color

    def draw(self, surface, mouse_pos):
        color = self.hover if self.rect.collidepoint(mouse_pos) else self.base
        pygame.draw.rect(surface, color, self.rect, border_radius=10)
        pygame.draw.rect(surface, (0, 0, 0), self.rect, 2, border_radius=10)

        txt = FONT.render(self.text, True, (255, 255, 255))
        surface.blit(txt,
                     (self.rect.centerx - txt.get_width() // 2,
                      self.rect.centery - txt.get_height() // 2))

    def clicked(self, event):
        return (event.type == pygame.MOUSEBUTTONDOWN and
                event.button == 1 and
                self.rect.collidepoint(event.pos))

def checkInput(username, password):
    if "'" not in username and '"' not in username and "'" not in password and '"' not in password:
        return True
    return False

def login(username, password):
    message1 = "invalid input"
    if checkInput(username, password):
        exists = False
        conn = sqlite3.connect('cyber.db')
        c = conn.cursor()
        c.execute("SELECT username,password FROM login")
        rows = c.fetchall()
        print(rows)
        for row in rows:
            if username == row[0]:
                if password == row[1]:
                    message1 = "hello welcome back"
                    exists = True
                    break
                else:
                    message1 = "wrong password"
                    exists = True
                    break
            else:
                exists = False
        if not exists:
            message1 = "username does not exist"

        conn.commit()
    return message1

def signup(username, password):
    exists = False
    message1 = "invalid input"
    if checkInput(username, password):
        conn = sqlite3.connect('cyber.db')
        c = conn.cursor()
        c.execute("SELECT username,password FROM login")
        rows = c.fetchall()
        print(rows)
        for row in rows:
            if username == row[0]:
                    message1 = "username already exists"
                    exists = True
                    break
            else:
                exists = False
        if not exists:
            id = random.randint(1, 100)
            c.execute("INSERT INTO login(player_id, username, password) VALUES (?, ?, ?)", (id, username, password))
            message1 = "signup successful"
            # add to players
            x = 0
            y = 0
            c.execute("INSERT INTO players VALUES (?,?,?)", (id, x, y))

        # get results
        conn.commit()
    return message1


# -------- Layout --------
panel_rect = pygame.Rect(90, 60, 580, 330)

# --- Login screen ---
login_user = InputBox(190, 165, 380, 44, "Username")
login_pass = InputBox(190, 245, 380, 44, "Password", is_password=True)

btn_login = Button(190, 310, 180, 48, "Login", BTN, BTN_HOVER)
btn_to_signup = Button(390, 310, 180, 48, "Create account", BTN3, BTN3_HOVER)
btn_quit = Button(590, 310, 80, 48, "Quit", BTN2, BTN2_HOVER)

# --- Sign Up screen ---
signup_user = InputBox(190, 155, 380, 44, "New username")
signup_pass = InputBox(190, 225, 380, 44, "New password", is_password=True)
signup_pass2 = InputBox(190, 295, 380, 44, "Confirm password", is_password=True)

btn_signup = Button(190, 365, 180, 48, "Sign up", BTN, BTN_HOVER)
btn_back_login = Button(390, 365, 180, 48, "Back to login", BTN3, BTN3_HOVER)

# -------- State --------
MODE_LOGIN = "login"
MODE_SIGNUP = "signup"
mode = MODE_LOGIN

message = ""   # <---- הודעות למשתמש

# -------- Main Loop --------
while True:
    mouse_pos = pygame.mouse.get_pos()

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            pygame.quit()
            sys.exit()

        if btn_quit.clicked(event):
            pygame.quit()
            sys.exit()

        if mode == MODE_LOGIN:
            login_user.handle_event(event)
            login_pass.handle_event(event)

            if btn_login.clicked(event):
                username = login_user.text.strip()
                password = login_pass.text.strip()

                if not username or not password:
                    message = "Please fill both username and password."
                else:
                    message = login(username, password)


            if btn_to_signup.clicked(event):
                mode = MODE_SIGNUP
                message = ""

        else:  # SIGN UP
            signup_user.handle_event(event)
            signup_pass.handle_event(event)
            signup_pass2.handle_event(event)

            if btn_signup.clicked(event):
                u = signup_user.text.strip()
                p1 = signup_pass.text.strip()
                p2 = signup_pass2.text.strip()

                if not u or not p1 or not p2:
                    message = "Please fill all fields."
                elif p1 != p2:
                    message = "Passwords do not match."
                else:
                    message = signup(u, p1)

            if btn_back_login.clicked(event):
                mode = MODE_LOGIN
                message = ""

    # -------- Draw --------
    screen.fill(BG)
    pygame.draw.rect(screen, PANEL, panel_rect, border_radius=16)
    pygame.draw.rect(screen, (0, 0, 0), panel_rect, 2, border_radius=16)

    if mode == MODE_LOGIN:
        draw_text(screen, "Login", (panel_rect.x + 20, panel_rect.y + 16), TEXT, FONT_BIG)
        draw_text(screen, "Enter your credentials",
                  (panel_rect.x + 20, panel_rect.y + 56), MUTED, FONT_SMALL)

        login_user.draw(screen)
        login_pass.draw(screen)

        btn_login.draw(screen, mouse_pos)
        btn_to_signup.draw(screen, mouse_pos)
        btn_quit.draw(screen, mouse_pos)

        if message:
            draw_text(screen, message, (190, 370), (255, 220, 120), FONT_SMALL)

    else:
        draw_text(screen, "Sign Up", (panel_rect.x + 20, panel_rect.y + 16), TEXT, FONT_BIG)
        draw_text(screen, "Create a new account",
                  (panel_rect.x + 20, panel_rect.y + 56), MUTED, FONT_SMALL)

        signup_user.draw(screen)
        signup_pass.draw(screen)
        signup_pass2.draw(screen)

        btn_signup.draw(screen, mouse_pos)
        btn_back_login.draw(screen, mouse_pos)
        btn_quit.draw(screen, mouse_pos)

        if message:
            draw_text(screen, message, (190, 350), (255, 220, 120), FONT_SMALL)

    pygame.display.flip()
    clock.tick(60)
