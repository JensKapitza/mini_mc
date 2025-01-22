#!/usr/bin/env python3

import os
import curses
import time
import paramiko
import hashlib

###############################################################################
# CONFIGURATION
###############################################################################
REMOTE_HOST = ""
REMOTE_PORT = 22
REMOTE_USER = ""
REMOTE_PASS = ""

INITIAL_REMOTE_PATH = "/"
INITIAL_LOCAL_PATH = "."

# Wieviel MB bei "Partial Compare" gelesen werden (lokal & remote)
PARTIAL_MB = 5

# Grenze, ab der eine Datei als "groß" gilt und wir erst prüfen,
# ob sie (identisch) auf der Gegenseite existiert (in MB).
LARGE_FILE_MB = 10

###############################################################################
# LOG / DEBUG COMMANDS
###############################################################################
CMD_LOG_MAX = 5
cmd_log = []

def log_command(cmd: str):
    global cmd_log
    cmd_log.append(cmd)
    if len(cmd_log) > CMD_LOG_MAX:
        cmd_log.pop(0)

###############################################################################
# LOKALE CHECKSUM: PARTIAL ODER FULL
###############################################################################
def compute_local_checksum(path, algo='md5', full=True, partial_mb=PARTIAL_MB, chunk_size=65536):
    """
    Berechnet die Checksum einer lokalen Datei.
    - full=True => ganze Datei
    - full=False => nur die ersten partial_mb MB
    """
    if not os.path.isfile(path):
        return None

    h = hashlib.new(algo)
    max_bytes = None if full else (partial_mb * 1024 * 1024)
    try:
        with open(path, 'rb') as f:
            bytes_read = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                bytes_read += len(chunk)
                if max_bytes is not None and bytes_read >= max_bytes:
                    break
        return h.hexdigest()
    except OSError:
        return None

###############################################################################
# REMOTE CHECKSUM: PARTIAL ODER FULL MIT SSH-BEFEHL
###############################################################################
def compute_remote_checksum(ssh, remote_path, algo='md5', full=True, partial_mb=PARTIAL_MB):
    """ Berechnet die Checksum einer Remote-Datei per SSH-Befehl (kein Download). """
    if not remote_file_exists(ssh, remote_path):
        return None

    command_map = {
        'md5': 'md5sum',
        'sha256': 'sha256sum',
        'sha1': 'sha1sum',
    }
    sum_cmd = command_map.get(algo, 'md5sum')

    if full:
        cmd = f"{sum_cmd} '{remote_path}'"
    else:
        # Teilweises Lesen via dd
        cmd = f"dd if='{remote_path}' bs=1M count={partial_mb} 2>/dev/null | {sum_cmd}"

    log_command(cmd)

    try:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out_data = stdout.read().decode('utf-8', errors='ignore').strip()
        err_data = stderr.read().decode('utf-8', errors='ignore').strip()
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            return None

        # md5sum-Ausgabe: "abcdef1234...  dateiname"
        parts = out_data.split()
        if len(parts) > 0:
            return parts[0]
        else:
            return None
    except Exception:
        return None

def remote_file_exists(ssh, remote_path):
    """Prüft, ob remote_path eine reguläre Datei ist."""
    check_cmd = f"[ -f '{remote_path}' ]"
    log_command(check_cmd)
    stdin, stdout, stderr = ssh.exec_command(check_cmd)
    exit_status = stdout.channel.recv_exit_status()
    return (exit_status == 0)

def remote_file_size(ssh, remote_path):
    """Gibt die Größe einer Remote-Datei in Bytes zurück oder None bei Fehler."""
    # z.B. `stat -c %s /pfad/zur/datei`
    cmd = f"stat -c %s '{remote_path}'"
    log_command(cmd)
    try:
        stdin, stdout, stderr = ssh.exec_command(cmd)
        out_data = stdout.read().decode('utf-8', errors='ignore').strip()
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0 and out_data.isdigit():
            return int(out_data)
        else:
            return None
    except Exception:
        return None

###############################################################################
# LISTEN (LOKAL + REMOTE)
###############################################################################
def list_local_dir(path):
    try:
        items = []
        for entry in os.scandir(path):
            items.append((entry.name, entry.is_dir()))
        # Sortierung: erst Verzeichnisse, dann Dateien
        items.sort(key=lambda x: (not x[1], x[0].lower()))
        if path not in ('/', ''):
            items.insert(0, ('..', True))
        return items
    except OSError:
        return []

def list_remote_dir(sftp, path):
    try:
        items = []
        for x in sftp.listdir_attr(path):
            fname = x.filename
            is_dir = bool(x.st_mode & 0o040000)
            items.append((fname, is_dir))
        items.sort(key=lambda x: (not x[1], x[0].lower()))
        if path not in ('/', ''):
            items.insert(0, ('..', True))
        return items
    except IOError:
        return []

###############################################################################
# SCROLLBARE PANEL DARSTELLUNG
###############################################################################
def draw_panel(stdscr, y, x, height, width,
               title, entries, selected_idx, scroll_offset, current_path):
    """Zeichnet das Panel (Rahmen + Inhalt) mit Scrollfunktion."""
    stdscr.attron(curses.color_pair(1))
    try:
        curses.rectangle(stdscr, y, x, y+height-1, x+width-1)
    except:
        pass
    stdscr.attroff(curses.color_pair(1))

    stdscr.addstr(y, x+2, f"[ {title} ]")

    max_path_len = width - 4
    display_path = current_path
    if len(display_path) > max_path_len:
        display_path = "..." + display_path[-(max_path_len-3):]
    stdscr.addstr(y+1, x+2, display_path)

    inner_height = height - 3
    inner_width = width - 2
    start_y = y+2
    start_x = x+1

    visible_entries = entries[scroll_offset:scroll_offset+inner_height]

    for i, (fname, is_dir) in enumerate(visible_entries):
        actual_index = scroll_offset + i
        highlight = (actual_index == selected_idx)
        text = fname + ("/" if is_dir else "")
        text = text[:inner_width-1]
        if highlight:
            stdscr.attron(curses.A_REVERSE)
        stdscr.addstr(start_y+i, start_x, text.ljust(inner_width-1))
        if highlight:
            stdscr.attroff(curses.A_REVERSE)

###############################################################################
# WECHSEL IM DATEISYSTEM (LOKAL / REMOTE)
###############################################################################
def change_directory_local(current_path, target):
    if target == "..":
        return os.path.dirname(os.path.abspath(current_path))
    else:
        newpath = os.path.join(current_path, target)
        if os.path.isdir(newpath):
            return os.path.abspath(newpath)
    return current_path

def change_directory_remote(sftp, current_path, target):
    if target == "..":
        if current_path == "/":
            return "/"
        return os.path.dirname(current_path.rstrip("/"))
    else:
        if current_path == "/":
            newpath = "/" + target
        else:
            newpath = current_path.rstrip("/") + "/" + target

        try:
            attr = sftp.stat(newpath)
            if attr.st_mode & 0o040000:
                return newpath
        except IOError:
            pass
    return current_path

###############################################################################
# SMART COPY (LOKAL -> REMOTE)
###############################################################################
def smart_copy_local_to_remote(sftp, ssh, local_path, remote_path):
    """
    Kopiert 'local_path' (Datei oder Ordner) rekursiv zum 'remote_path'.
    - Falls local_path ein Ordner ist, wird remote_path als Ordner angelegt und alle Inhalte rekursiv kopiert.
    - Bei großen Dateien (> LARGE_FILE_MB) prüfen wir erst, ob remote schon eine identische Datei existiert.
      -> partial compare, wenn gleich -> full compare
      -> Wenn identisch, kein Kopieren
    - Kleinere Dateien werden direkt kopiert.
    """
    if os.path.isdir(local_path):
        # Ordner anlegen (falls nicht existiert)
        try:
            sftp.mkdir(remote_path)
        except IOError:
            pass  # Ordner existiert vermutlich schon

        for entry in os.scandir(local_path):
            src_child = os.path.join(local_path, entry.name)
            # remote_path ggf. / weglassen, wenn / am Ende
            if remote_path == "/":
                dst_child = "/" + entry.name
            else:
                dst_child = remote_path.rstrip('/') + "/" + entry.name

            # Rekursiver Aufruf
            smart_copy_local_to_remote(sftp, ssh, src_child, dst_child)
    else:
        # Datei
        size_local = os.path.getsize(local_path)
        # Prüfen, ob remote bereits existiert
        if remote_file_exists(ssh, remote_path):
            # Falls groß => partial compare
            if size_local > LARGE_FILE_MB * 1024 * 1024:
                # partial compare
                csum_loc_part = compute_local_checksum(local_path, full=False)
                csum_rem_part = compute_remote_checksum(ssh, remote_path, full=False)
                if csum_loc_part and csum_rem_part and csum_loc_part == csum_rem_part:
                    # jetzt full compare
                    csum_loc_full = compute_local_checksum(local_path, full=True)
                    csum_rem_full = compute_remote_checksum(ssh, remote_path, full=True)
                    if csum_loc_full == csum_rem_full:
                        # identisch -> nicht kopieren
                        log_command(f"SKIP identical: {local_path} -> {remote_path}")
                        return
        else:
            # Existiert remote nicht -> kein Compare nötig
            pass

        # Datei kopieren
        cmd = f"PUT {local_path} -> {remote_path}"
        log_command(cmd)
        try:
            sftp.put(local_path, remote_path)
        except Exception as e:
            log_command(f"Fehler: {e}")

###############################################################################
# SMART COPY (REMOTE -> LOKAL)
###############################################################################
def smart_copy_remote_to_local(sftp, ssh, remote_path, local_path):
    """
    Kopiert 'remote_path' (Datei oder Ordner) rekursiv zum 'local_path'.
    Analog zur Funktion oben, nur umgekehrt.
    """
    # Prüfen, ob remote_path ein Verzeichnis ist
    # Trick: sftp.stat(remote_path).st_mode => wir schauen, ob es S_IFDIR ist
    try:
        attr = sftp.stat(remote_path)
        is_dir = bool(attr.st_mode & 0o040000)
    except IOError:
        return  # existiert nicht?

    if is_dir:
        # Ordner lokal anlegen
        if not os.path.exists(local_path):
            try:
                os.mkdir(local_path)
            except OSError:
                pass

        # Inhalte auslesen
        try:
            entries = sftp.listdir_attr(remote_path)
            for e in entries:
                rname = e.filename
                remote_child = remote_path.rstrip('/') + "/" + rname if remote_path != "/" else "/" + rname
                local_child = os.path.join(local_path, rname)
                smart_copy_remote_to_local(sftp, ssh, remote_child, local_child)
        except IOError:
            pass
    else:
        # Datei
        # Prüfen, ob lokal existiert
        if os.path.exists(local_path):
            size_remote = remote_file_size(ssh, remote_path)
            if size_remote and size_remote > LARGE_FILE_MB * 1024 * 1024:
                # partial compare
                csum_rem_part = compute_remote_checksum(ssh, remote_path, full=False)
                csum_loc_part = compute_local_checksum(local_path, full=False)
                if csum_rem_part and csum_loc_part and csum_rem_part == csum_loc_part:
                    # full compare
                    csum_rem_full = compute_remote_checksum(ssh, remote_path, full=True)
                    csum_loc_full = compute_local_checksum(local_path, full=True)
                    if csum_rem_full == csum_loc_full:
                        # identisch
                        log_command(f"SKIP identical: {remote_path} -> {local_path}")
                        return

        cmd = f"GET {remote_path} -> {local_path}"
        log_command(cmd)
        try:
            sftp.get(remote_path, local_path)
        except Exception as e:
            log_command(f"Fehler: {e}")

###############################################################################
# COPY / MOVE / DELETE
###############################################################################
def copy_file_local_to_remote(sftp, ssh, local_path, remote_path):
    """Einfache Hülle, die `smart_copy_local_to_remote` aufruft."""
    smart_copy_local_to_remote(sftp, ssh, local_path, remote_path)

def copy_file_remote_to_local(sftp, ssh, remote_path, local_path):
    smart_copy_remote_to_local(sftp, ssh, remote_path, local_path)

def move_file_local_to_remote(sftp, ssh, local_path, remote_path):
    """Erst kopieren, dann lokal löschen."""
    smart_copy_local_to_remote(sftp, ssh, local_path, remote_path)
    try:
        if os.path.isfile(local_path):
            os.remove(local_path)
            log_command(f"RM LOCAL {local_path}")
        else:
            # ggf. rekursive Löschung falls Ordner
            import shutil
            shutil.rmtree(local_path)
            log_command(f"RM LOCAL DIR {local_path}")
    except Exception as e:
        log_command(f"Fehler bei rm local: {e}")

def move_file_remote_to_local(sftp, ssh, remote_path, local_path):
    """Erst kopieren, dann remote löschen."""
    smart_copy_remote_to_local(sftp, ssh, remote_path, local_path)
    # Falls es ein Verzeichnis war, müssen wir rekursiv löschen
    try:
        # Test: Ordner oder Datei?
        attr = sftp.stat(remote_path)
        is_dir = bool(attr.st_mode & 0o040000)
        if is_dir:
            # rekursiv auf Remote löschen
            remove_remote_dir_recursive(sftp, remote_path)
            log_command(f"RM REMOTE DIR {remote_path}")
        else:
            sftp.remove(remote_path)
            log_command(f"RM REMOTE {remote_path}")
    except Exception as e:
        log_command(f"Fehler bei rm remote: {e}")

def remove_remote_dir_recursive(sftp, path):
    """Löscht ein Verzeichnis auf dem Remote-System rekursiv."""
    try:
        for item in sftp.listdir_attr(path):
            fname = item.filename
            fullp = path.rstrip('/') + '/' + fname if path != '/' else '/' + fname
            if bool(item.st_mode & 0o040000):
                # Ordner -> rekursiv
                remove_remote_dir_recursive(sftp, fullp)
            else:
                sftp.remove(fullp)
        sftp.rmdir(path)
    except:
        pass

def delete_local_file_or_dir(path):
    """Löscht eine lokale Datei oder Ordner rekursiv."""
    cmd = f"DEL LOCAL {path}"
    log_command(cmd)
    if os.path.isdir(path):
        import shutil
        try:
            shutil.rmtree(path)
        except Exception as e:
            log_command(f"Fehler: {e}")
    else:
        try:
            os.remove(path)
        except Exception as e:
            log_command(f"Fehler: {e}")

def delete_remote_file_or_dir(sftp, path, ssh):
    """Löscht eine remote-Datei oder Ordner rekursiv."""
    # Prüfen, ob Ordner
    cmd = f"DEL REMOTE {path}"
    log_command(cmd)
    try:
        attr = sftp.stat(path)
        is_dir = bool(attr.st_mode & 0o040000)
        if is_dir:
            remove_remote_dir_recursive(sftp, path)
        else:
            sftp.remove(path)
    except Exception as e:
        log_command(f"Fehler: {e}")

###############################################################################
# VERGLEICH: TEILWEISE (PARTIAL) ODER VOLL (FULL)
###############################################################################
def compare_directories(local_path, remote_path, ssh, sftp, algo='md5', full=False):
    """
    Vergleicht top-level Dateien in local_path und remote_path.
    Gibt (only_local, only_remote, different, same) zurück (Mengen von Dateinamen).
    """
    local_entries = list_local_dir(local_path)
    remote_entries = list_remote_dir(sftp, remote_path)

    local_files = [n for (n, d) in local_entries if not d and n != '..']
    remote_files = [n for (n, d) in remote_entries if not d and n != '..']

    set_local = set(local_files)
    set_remote = set(remote_files)

    common = set_local.intersection(set_remote)
    only_local = set_local - common
    only_remote = set_remote - common

    different = set()
    same = set()

    for fname in common:
        local_fp = os.path.join(local_path, fname)
        remote_fp = remote_path.rstrip('/') + '/' + fname if remote_path != '/' else '/' + fname
        csum_loc = compute_local_checksum(local_fp, algo=algo, full=full)
        csum_rem = compute_remote_checksum(ssh, remote_fp, algo=algo, full=full)
        if csum_loc is None or csum_rem is None:
            different.add(fname)
        else:
            if csum_loc == csum_rem:
                same.add(fname)
            else:
                different.add(fname)

    return only_local, only_remote, different, same

###############################################################################
# EINFACHE BESTÄTIGUNGS-ABFRAGE IM CURSES-DIALOG
###############################################################################
def confirm_dialog(stdscr, question):
    rows, cols = stdscr.getmaxyx()
    win_height = 5
    win_width = len(question) + 10
    if win_width > cols:
        win_width = cols - 2
    start_y = (rows - win_height) // 2
    start_x = (cols - win_width) // 2

    win = curses.newwin(win_height, win_width, start_y, start_x)
    win.box()
    win.addstr(1, 2, question[:win_width-4])
    win.addstr(3, 2, "[y]es / [n]o")

    win.refresh()

    while True:
        c = win.getch()
        if c in (ord('y'), ord('Y')):
            return True
        elif c in (ord('n'), ord('N'), 27):  # ESC
            return False

###############################################################################
# MAIN LOOP
###############################################################################
def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(REMOTE_HOST, port=REMOTE_PORT, username=REMOTE_USER, password=REMOTE_PASS)
    sftp = ssh.open_sftp()

    local_path = os.path.abspath(INITIAL_LOCAL_PATH)
    remote_path = INITIAL_REMOTE_PATH

    # Auswahl + Scroll
    local_sel = 0
    remote_sel = 0
    local_scroll = 0
    remote_scroll = 0

    focus_panel = 0  # 0=local, 1=remote
    diff_info = None

    while True:
        stdscr.clear()
        rows, cols = stdscr.getmaxyx()

        # Oben: Info-Leiste mit Uhrzeit und Hilfe
        now_str = time.strftime("%H:%M:%S")
        commands_str = ("[c:copy  v:move  x:del  p:partial  f:full  q:quit] "
                        "[ENTER:cd .. TAB:switch]")
        info_line = f"{now_str} | {commands_str}"
        stdscr.addstr(0, 0, info_line[:cols-1])

        # Panels eine Zeile nach unten
        panel_width = cols // 2
        panel_height = rows - 1  # 1 Zeile für Info
        # => Wir nutzen y=1 als Start, damit Zeile 0 frei bleibt
        local_entries = list_local_dir(local_path)
        remote_entries = list_remote_dir(sftp, remote_path)
        local_count = len(local_entries)
        remote_count = len(remote_entries)

        visible_area = panel_height - 3

        if local_sel >= local_count:
            local_sel = max(0, local_count - 1)
        if remote_sel >= remote_count:
            remote_sel = max(0, remote_count - 1)

        # Scroll Korrektur local
        if local_sel < local_scroll:
            local_scroll = local_sel
        elif local_sel >= local_scroll + visible_area:
            local_scroll = local_sel - visible_area + 1

        # Scroll Korrektur remote
        if remote_sel < remote_scroll:
            remote_scroll = remote_sel
        elif remote_sel >= remote_scroll + visible_area:
            remote_scroll = remote_sel - visible_area + 1

        # Zeichne Panels
        draw_panel(
            stdscr,
            y=1, x=0,
            height=panel_height, width=panel_width,
            title="LOCAL",
            entries=local_entries,
            selected_idx=local_sel if focus_panel == 0 else -1,
            scroll_offset=local_scroll,
            current_path=local_path
        )
        draw_panel(
            stdscr,
            y=1, x=panel_width,
            height=panel_height, width=panel_width,
            title="REMOTE",
            entries=remote_entries,
            selected_idx=remote_sel if focus_panel == 1 else -1,
            scroll_offset=remote_scroll,
            current_path=remote_path
        )

        # Vorletzte Zeile: Log
        if len(cmd_log) > 0:
            log_text = " | ".join(cmd_log[-2:])
            stdscr.addstr(rows-2, 0, log_text[:cols-1])

        # Letzte Zeile: Diff-Info (falls vorhanden)
        if diff_info is not None:
            (only_l, only_r, diff_p, same_p) = diff_info
            line = (
                f"Nur lokal: {sorted(list(only_l))} | "
                f"Nur remote: {sorted(list(only_r))} | "
                f"Verschieden: {sorted(list(diff_p))} | "
                f"Gleich: {sorted(list(same_p))}"
            )
            stdscr.addstr(rows-1, 0, line[:cols-1])

        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_F10, ord('q')):
            break

        # Steuerung
        if focus_panel == 0:  # LOCAL
            if key == curses.KEY_UP:
                if local_sel > 0:
                    local_sel -= 1
            elif key == curses.KEY_DOWN:
                if local_sel < local_count - 1:
                    local_sel += 1
            elif key in (curses.KEY_PPAGE,):
                local_sel = max(0, local_sel - visible_area)
            elif key in (curses.KEY_NPAGE,):
                local_sel = min(local_count - 1, local_sel + visible_area)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):
                focus_panel = 1
            elif key in (curses.KEY_ENTER, 10, 13):
                if local_count > 0:
                    name, is_dir = local_entries[local_sel]
                    if is_dir:
                        local_path = change_directory_local(local_path, name)
                        local_sel = 0
                        local_scroll = 0
            elif key == ord('c'):
                # Copy local->remote (Datei oder Ordner)
                if local_count > 0:
                    name, is_dir = local_entries[local_sel]
                    src = os.path.join(local_path, name)
                    if remote_path == "/":
                        dst = "/" + name
                    else:
                        dst = remote_path.rstrip('/') + "/" + name
                    copy_file_local_to_remote(sftp, ssh, src, dst)
            elif key == ord('v'):
                # Move local->remote
                if local_count > 0:
                    name, is_dir = local_entries[local_sel]
                    src = os.path.join(local_path, name)
                    if remote_path == "/":
                        dst = "/" + name
                    else:
                        dst = remote_path.rstrip('/') + "/" + name
                    move_file_local_to_remote(sftp, ssh, src, dst)
            elif key == ord('x'):
                # Delete local
                if local_count > 0:
                    name, is_dir = local_entries[local_sel]
                    src = os.path.join(local_path, name)
                    question = f"Lokal löschen: {name}? (y/n)"
                    if confirm_dialog(stdscr, question):
                        delete_local_file_or_dir(src)
            elif key == ord('p'):
                diff_info = compare_directories(local_path, remote_path, ssh, sftp, algo='md5', full=False)
            elif key == ord('f'):
                diff_info = compare_directories(local_path, remote_path, ssh, sftp, algo='md5', full=True)

        else:  # REMOTE
            if key == curses.KEY_UP:
                if remote_sel > 0:
                    remote_sel -= 1
            elif key == curses.KEY_DOWN:
                if remote_sel < remote_count - 1:
                    remote_sel += 1
            elif key in (curses.KEY_PPAGE,):
                remote_sel = max(0, remote_sel - visible_area)
            elif key in (curses.KEY_NPAGE,):
                remote_sel = min(remote_count - 1, remote_sel + visible_area)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, 9):
                focus_panel = 0
            elif key in (curses.KEY_ENTER, 10, 13):
                if remote_count > 0:
                    name, is_dir = remote_entries[remote_sel]
                    if is_dir:
                        remote_path = change_directory_remote(sftp, remote_path, name)
                        remote_sel = 0
                        remote_scroll = 0
            elif key == ord('c'):
                # Copy remote->local
                if remote_count > 0:
                    name, is_dir = remote_entries[remote_sel]
                    if remote_path == "/":
                        src = "/" + name
                    else:
                        src = remote_path.rstrip('/') + "/" + name
                    dst = os.path.join(local_path, name)
                    copy_file_remote_to_local(sftp, ssh, src, dst)
            elif key == ord('v'):
                # Move remote->local
                if remote_count > 0:
                    name, is_dir = remote_entries[remote_sel]
                    if remote_path == "/":
                        src = "/" + name
                    else:
                        src = remote_path.rstrip('/') + "/" + name
                    dst = os.path.join(local_path, name)
                    move_file_remote_to_local(sftp, ssh, src, dst)
            elif key == ord('x'):
                # Delete remote
                if remote_count > 0:
                    name, is_dir = remote_entries[remote_sel]
                    if remote_path == "/":
                        src = "/" + name
                    else:
                        src = remote_path.rstrip('/') + "/" + name
                    question = f"Remote löschen: {name}? (y/n)"
                    if confirm_dialog(stdscr, question):
                        delete_remote_file_or_dir(sftp, src, ssh)
            elif key == ord('p'):
                diff_info = compare_directories(local_path, remote_path, ssh, sftp, algo='md5', full=False)
            elif key == ord('f'):
                diff_info = compare_directories(local_path, remote_path, ssh, sftp, algo='md5', full=True)

    # Ende
    sftp.close()
    ssh.close()

###############################################################################
# START
###############################################################################
if __name__ == "__main__":
    curses.wrapper(main)
