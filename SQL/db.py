import sqlite3
import random

from pyasn1_modules.rfc5280 import extension_OR_address_components

conn = sqlite3.connect('cyber.db')
c = conn.cursor()
table = """CREATE TABLE IF NOT EXISTS players(
    player_id INTEGER PRIMARY KEY , last_x INTEGER,
    last_y INTEGER health Integer)"""
table_login = """CREATE TABLE IF NOT EXISTS login(
                player_id INTEGER PRIMARY KEY, username TEXT, password TEXT, 
                FOREIGN KEY(player_id) REFERENCES players(player_id))"""
c.execute(table)
c.execute(table_login)

username = input("Enter username: ")
password = input("Enter password: ")
id = random.randint(1, 100)
exists = False


if "'" not in username and '"' not in username and"'" not in password and '"' not in password:
    username = username.lower()
    password = password.lower()

    c.execute("SELECT username,password FROM login")
    rows = c.fetchall()
    print(rows)
    for row in rows:
        if username == row[0]:
            if password == row[1]:
                print("hello welcome back")
                exists = True
                break
            else:
                print("wrong password or username already exists")
                exists = True
                break
        else:
            exists = False
    print(exists)
    if not exists:
        c.execute("INSERT INTO login(player_id, username, password) VALUES (?, ?, ?)", (id, username, password))


        #add to players
        x = 0
        y = 0
        c.execute("INSERT INTO players VALUES (?,?,?)", (id,x,y))

    #get results
    conn.commit()

    c.execute("SELECT * FROM login")
    result = c.fetchall()
    print(result)

    c.execute("SELECT * FROM players")
    result = c.fetchall()
    print(result)
else:
    print("invalid input")

#update
#c.execute("UPDATE players SET x=0 WHERE id = 1")

#delete
#c.execute("DELETE * FROM purchases WHERE id = 1")

#delete all the tables
#conn.execute("DROP TABLE login")
#conn.execute("DROP TABLE players")


c.close()
conn.close()