import curses
import random
import time

def main(stdscr):
    # Curses-Grundeinstellungen
    curses.curs_set(0)          # Unsichtbarer Cursor
    stdscr.nodelay(True)        # getch() blockiert nicht
    stdscr.keypad(True)         # Ermöglicht das Abfangen von Sondertasten
    curses.start_color()        # Farben initialisieren (wenn möglich)

    # Farben definieren
    curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)  # Für Schlange
    curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)    # Für Futter

    # Spielfeldmaße ermitteln
    sh, sw = stdscr.getmaxyx()

    # Anfangsposition der Schlange (in der Mitte)
    start_y = sh // 2
    start_x = sw // 2

    # Schlange als Liste von (y, x)-Koordinaten (Kopf = snake[0])
    snake = [
        (start_y, start_x),
        (start_y, start_x - 1),
        (start_y, start_x - 2)
    ]

    # Anfangsrichtung: nach rechts
    direction = curses.KEY_RIGHT
    
    # Erstes Futter irgendwo zufällig
    food = (random.randint(0, sh - 1), random.randint(0, sw - 1))

    # Zählt, wie oft die Schlange schon gefressen hat
    food_eaten_count = 0

    # Spielgeschwindigkeit (Sekunden-Pause pro Frame)
    speed = 0.2  # Für 3-Jährige etwas langsamer

    while True:
        stdscr.clear()

        # Tastenabfrage (nicht blockierend)
        key = stdscr.getch()
        if key != -1:
            # Nur ändern, wenn es eine der Pfeiltasten ist
            if key in [curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT]:
                # Keine Gegen-Richtung-Blockade (da kein Game Over),
                # aber man könnte es optional einbauen, um "Zurückfahren" zu verhindern
                direction = key

        # Aktuelle Kopfposition
        head_y, head_x = snake[0]

        # Neue Kopfposition basierend auf Richtung
        if direction == curses.KEY_UP:
            head_y -= 1
        elif direction == curses.KEY_DOWN:
            head_y += 1
        elif direction == curses.KEY_LEFT:
            head_x -= 1
        elif direction == curses.KEY_RIGHT:
            head_x += 1

        # Unendliche Welt: Wenn Kopf aus Rand raus, am anderen Ende wieder erscheinen
        if head_y < 0:
            head_y = sh - 1
        elif head_y >= sh:
            head_y = 0
        if head_x < 0:
            head_x = sw - 1
        elif head_x >= sw:
            head_x = 0

        new_head = (head_y, head_x)

        # Kopf an den Anfang der Schlange setzen
        snake.insert(0, new_head)

        # Prüfung, ob die Schlange das Futter frisst
        if new_head == food:
            food_eaten_count += 1

            # Neues Futter platzieren
            food = (random.randint(0, sh - 1), random.randint(0, sw - 1))

            # Nur jeden zweiten Fress-Vorgang wächst die Schlange tatsächlich
            if food_eaten_count % 2 != 0:
                # Bei ungeraden Zähler (1,3,5...) wird der Schwanz entfernt, 
                # das heißt, die Schlange wächst NICHT.
                snake.pop()
        else:
            # Kein Futter gefressen => Schwanz entfernen (Länge bleibt gleich)
            snake.pop()

        # Futter zeichnen
        stdscr.attron(curses.color_pair(2))
        stdscr.addch(food[0], food[1], 'F')
        stdscr.attroff(curses.color_pair(2))

        # Schlange zeichnen
        stdscr.attron(curses.color_pair(1))
        for y, x in snake:
            stdscr.addch(y, x, '#')
        stdscr.attroff(curses.color_pair(1))

        stdscr.refresh()
        time.sleep(speed)

if __name__ == "__main__":
    curses.wrapper(main)
