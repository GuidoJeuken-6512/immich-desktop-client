import mimetypes
import queue
import socket
import sys
import threading
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pystray
import yaml
from PIL import Image
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from immich import Immich

if getattr(sys, 'frozen', False):
    EXE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, '_MEIPASS', EXE_DIR))
else:
    EXE_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = EXE_DIR

CONFIG_PATH = EXE_DIR / 'config.yaml'
ICON_PATH = BUNDLE_DIR / 'resources' / 'icon.ico'
VERSION_PATH = BUNDLE_DIR / 'VERSION'

try:
    APP_VERSION = VERSION_PATH.read_text().strip()
except FileNotFoundError:
    APP_VERSION = "dev"

SINGLE_INSTANCE_PORT = 47834
BACKGROUND_FLAG = "--background"


def is_background_launch():
    return BACKGROUND_FLAG in sys.argv[1:]


def acquire_single_instance_lock():
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock_socket.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        lock_socket.close()
        return None
    lock_socket.listen(1)
    return lock_socket


def signal_existing_instance_to_show():
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=1) as sock:
            sock.sendall(b"SHOW")
    except OSError:
        pass


AUTOSTART_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "ImmichDesktopClient"


def is_autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    if not getattr(sys, 'frozen', False):
        print("Autostart-Umschaltung wird im Entwicklungsmodus ignoriert.")
        return
    command = f'"{sys.executable}" {BACKGROUND_FLAG}'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
            except FileNotFoundError:
                pass


def get_extensions_for_type():
    mimetypes.init()
    temp = []
    for ext in mimetypes.types_map:
        if mimetypes.types_map[ext].split('/')[0] == "video" or mimetypes.types_map[ext].split('/')[0] == "image":
            temp.append(ext)
    for ext in mimetypes.common_types:
        if mimetypes.common_types[ext].split('/')[0] == "video" or mimetypes.common_types[ext].split('/')[0] == "image":
            temp.append(ext)

    return tuple(temp)


class QueueWriter:
    def __init__(self, log_queue):
        self.log_queue = log_queue

    def write(self, text):
        if text.strip():
            self.log_queue.put(text)

    def flush(self):
        pass


class SyncEventHandler(FileSystemEventHandler):
    def __init__(self, api, media_file_extensions):
        self.api = api
        self.media_file_extensions = media_file_extensions

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(self.media_file_extensions):
            print(f"File {event.src_path} has been created!")
            self.api.created(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(self.media_file_extensions):
            print(f"File {event.src_path} has been deleted!")
            self.api.delete(event.src_path)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(self.media_file_extensions):
            print(f"File {event.src_path} has been modified!")
            self.api.modify(event.src_path)


class ConfigWindow(tk.Toplevel):
    def __init__(self, master, on_saved, existing_config=None):
        super().__init__(master)
        self.title("Einstellungen")
        self.resizable(False, False)
        self.on_saved = on_saved

        api_cfg = (existing_config or {}).get("api") or {}
        watchdog_cfg = (existing_config or {}).get("watchdog") or {}
        self.directories = list(watchdog_cfg.get("directories") or [])

        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text="Server-URL:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value=api_cfg.get("url", ""))
        ttk.Entry(frm, textvariable=self.url_var, width=50).grid(row=0, column=1, columnspan=2, sticky="we", pady=2)

        ttk.Label(frm, text="API-Key:").grid(row=1, column=0, sticky="w")
        self.key_var = tk.StringVar(value=api_cfg.get("key", ""))
        ttk.Entry(frm, textvariable=self.key_var, width=50, show="*").grid(row=1, column=1, columnspan=2, sticky="we", pady=2)

        ttk.Label(frm, text="Album (optional):").grid(row=2, column=0, sticky="w")
        self.album_var = tk.StringVar(value=api_cfg.get("album") or "")
        self.album_entry = ttk.Entry(frm, textvariable=self.album_var, width=50)
        self.album_entry.grid(row=2, column=1, columnspan=2, sticky="we", pady=2)

        self.album_by_year_var = tk.BooleanVar(value=bool(api_cfg.get("album_by_year", False)))
        ttk.Checkbutton(
            frm, text="Statt festem Album automatisch ein Album pro Erstellungsjahr anlegen",
            variable=self.album_by_year_var, command=self._update_album_entry_state,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(5, 5))

        self.recursive_var = tk.BooleanVar(value=bool(watchdog_cfg.get("recursive", False)))
        ttk.Checkbutton(
            frm, text="Unterordner beim Start mit hochladen (rekursiv)", variable=self.recursive_var
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 5))

        self.delete_remote_var = tk.BooleanVar(value=bool(api_cfg.get("delete_remote_on_local_delete", False)))
        ttk.Checkbutton(
            frm, text="Beim Löschen einer lokalen Datei auch das zugehörige Asset auf dem Server löschen",
            variable=self.delete_remote_var,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 5))

        autostart_default = True if existing_config is None else is_autostart_enabled()
        self.autostart_var = tk.BooleanVar(value=autostart_default)
        ttk.Checkbutton(
            frm, text="Mit Windows starten (im Hintergrund)", variable=self.autostart_var,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 5))

        ttk.Label(frm, text="Überwachte Verzeichnisse:").grid(row=7, column=0, sticky="w")
        self.dir_listbox = tk.Listbox(frm, width=60, height=6, selectmode=tk.EXTENDED)
        self.dir_listbox.grid(row=8, column=0, columnspan=3, sticky="we")
        for directory in self.directories:
            self.dir_listbox.insert(tk.END, directory)

        dir_btn_frame = ttk.Frame(frm)
        dir_btn_frame.grid(row=9, column=0, columnspan=3, sticky="we", pady=(5, 10))
        ttk.Button(dir_btn_frame, text="Ordner hinzufügen", command=self.add_directory).pack(side="left")
        ttk.Button(dir_btn_frame, text="Entfernen", command=self.remove_directories).pack(side="left", padx=5)

        action_frame = ttk.Frame(frm)
        action_frame.grid(row=10, column=0, columnspan=3, sticky="e")
        ttk.Button(action_frame, text="Speichern", command=self.save).pack(side="right")
        ttk.Button(action_frame, text="Abbrechen", command=self.destroy).pack(side="right", padx=5)

        self.transient(master)
        self.grab_set()
        self._update_album_entry_state()

    def _update_album_entry_state(self):
        self.album_entry.configure(state="disabled" if self.album_by_year_var.get() else "normal")

    def add_directory(self):
        directory = filedialog.askdirectory(title="Verzeichnis auswählen", parent=self)
        if directory and directory not in self.directories:
            self.directories.append(directory)
            self.dir_listbox.insert(tk.END, directory)

    def remove_directories(self):
        for index in reversed(self.dir_listbox.curselection()):
            del self.directories[index]
            self.dir_listbox.delete(index)

    def save(self):
        url = self.url_var.get().strip()
        key = self.key_var.get().strip()
        if not url or not key:
            messagebox.showerror("Fehler", "Server-URL und API-Key sind erforderlich.", parent=self)
            return
        if not self.directories:
            messagebox.showerror("Fehler", "Mindestens ein zu überwachendes Verzeichnis ist erforderlich.", parent=self)
            return

        config = {
            "api": {
                "key": key,
                "url": url,
                "album": self.album_var.get().strip() or None,
                "album_by_year": self.album_by_year_var.get(),
                "delete_remote_on_local_delete": self.delete_remote_var.get(),
            },
            "watchdog": {
                "recursive": self.recursive_var.get(),
                "directories": self.directories,
            },
        }
        with open(CONFIG_PATH, "wt") as file:
            yaml.safe_dump(config, file, sort_keys=False, allow_unicode=True)

        set_autostart(self.autostart_var.get())

        self.on_saved(config)
        self.destroy()


class MainWindow(tk.Tk):
    def __init__(self, lock_socket=None):
        super().__init__()
        self.title(f"Immich Desktop Client v{APP_VERSION}")
        self.geometry("640x420")
        try:
            self.iconbitmap(str(ICON_PATH))
        except tk.TclError:
            pass

        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.sync_thread = None
        self.observer = None
        self.sync_active = False
        self.cancel_event = threading.Event()
        self._quitting = False
        self._hide_notice_shown = False
        self._lock_socket = lock_socket
        self.tray_icon = None

        self._orig_stdout = sys.stdout
        sys.stdout = QueueWriter(self.log_queue)

        self.config_data = self._load_config()
        self._build_ui()
        self._build_tray_icon()
        self.after(150, self._poll_log_queue)
        self.after(150, self._poll_progress_queue)

        if self._lock_socket:
            threading.Thread(target=self._listen_for_show_signal, daemon=True).start()

        if self.config_data is None:
            self.status_var.set("Keine Konfiguration gefunden – bitte Einstellungen ausfüllen.")
            self.after(200, self.open_config_window)
        else:
            self.status_var.set("Synchronisierung wird gestartet …")
            self.after(200, self.start_sync)
            if is_background_launch():
                self.withdraw()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Unmap>", self._on_minimize)

    def _on_minimize(self, event):
        if self.state() == "iconic":
            self._hide_to_tray()

    def _build_tray_icon(self):
        try:
            icon_image = Image.open(str(ICON_PATH))
        except Exception as exc:
            print(f"Tray-Icon konnte nicht geladen werden: {exc}")
            return

        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", self._tray_show_window, default=True),
            pystray.MenuItem(
                lambda item: "Synchronisierung pausieren" if self.sync_active else "Synchronisierung fortsetzen",
                self._tray_toggle_sync,
            ),
            pystray.MenuItem("Einstellungen", self._tray_open_settings),
            pystray.MenuItem("Alle Uploads löschen", self._tray_delete_all_uploads),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden", self._tray_quit),
        )
        self.tray_icon = pystray.Icon("immich-desktop-client", icon_image, "Immich Desktop Client", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _listen_for_show_signal(self):
        while True:
            try:
                conn, _ = self._lock_socket.accept()
            except OSError:
                return
            with conn:
                conn.recv(16)
            self.after(0, self._show_window)

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_show_window(self, icon=None, item=None):
        self.after(0, self._show_window)

    def _tray_toggle_sync(self, icon=None, item=None):
        self.after(0, self.cancel_sync if self.sync_active else self.start_sync)

    def _tray_open_settings(self, icon=None, item=None):
        self.after(0, self._show_window)
        self.after(0, self.open_config_window)

    def _tray_delete_all_uploads(self, icon=None, item=None):
        self.after(0, self._show_window)
        self.after(0, self.delete_all_uploads)

    def _tray_quit(self, icon=None, item=None):
        self.after(0, self.quit_app)

    @staticmethod
    def _load_config():
        if not CONFIG_PATH.exists():
            return None
        with open(CONFIG_PATH, "rt") as file:
            return yaml.safe_load(file)

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=5)
        toolbar.pack(side="top", fill="x")

        self.start_button = ttk.Button(toolbar, text="Start Sync", command=self.start_sync)
        self.start_button.pack(side="left")

        ttk.Button(toolbar, text="Einstellungen", command=self.open_config_window).pack(side="left", padx=5)

        self.delete_button = ttk.Button(toolbar, text="Alle Uploads löschen", command=self.delete_all_uploads)
        self.delete_button.pack(side="left", padx=5)

        self.status_var = tk.StringVar(value="Bereit" if self.config_data else "")
        ttk.Label(self, textvariable=self.status_var, padding=(10, 0)).pack(side="top", anchor="w")

        progress_frame = ttk.Frame(self, padding=(10, 0))
        progress_frame.pack(side="top", fill="x")
        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress_bar.pack(side="top", fill="x")
        self.progress_var = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=self.progress_var).pack(side="top", anchor="w")

        log_frame = ttk.Frame(self, padding=10)
        log_frame.pack(side="top", fill="both", expand=True)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def open_config_window(self):
        ConfigWindow(self, on_saved=self.on_config_saved, existing_config=self.config_data)

    def on_config_saved(self, config):
        self.config_data = config
        self.status_var.set("Konfiguration gespeichert. Bereit zum Start.")

    def append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text if text.endswith("\n") else text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                self.append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    def _poll_progress_queue(self):
        try:
            while True:
                current, total = self.progress_queue.get_nowait()
                self._update_progress(current, total)
        except queue.Empty:
            pass
        self.after(150, self._poll_progress_queue)

    def _update_progress(self, current, total):
        if total == 0:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=1, value=0)
            self.progress_var.set("Keine neuen Dateien zum Hochladen")
            return

        if str(self.progress_bar["mode"]) != "determinate" or int(self.progress_bar["maximum"]) != total:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=total, value=current)
        else:
            self.progress_bar.configure(value=current)
        self.progress_var.set(f"{current} / {total} Dateien hochgeladen")

    def start_sync(self):
        if self.config_data is None:
            messagebox.showwarning("Keine Konfiguration", "Bitte zuerst die Einstellungen ausfüllen.")
            self.open_config_window()
            return

        if self.sync_active:
            return

        self.sync_active = True
        self.cancel_event.clear()
        self.start_button.configure(text="Abbrechen", command=self.cancel_sync)
        self.status_var.set("Scanne Verzeichnisse …")
        self.progress_var.set("Scanne Verzeichnisse …")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(10)
        self.sync_thread = threading.Thread(target=self._run_sync, daemon=True)
        self.sync_thread.start()

    def cancel_sync(self):
        if not self.sync_active:
            return

        self.cancel_event.set()
        self.start_button.configure(state="disabled", text="Wird beendet …")
        self.status_var.set("Synchronisierung wird beendet …")
        if self.observer:
            self.observer.stop()
        threading.Thread(target=self._finish_cancel, daemon=True).start()

    def _finish_cancel(self):
        if self.observer:
            self.observer.join(timeout=5)
        if self.sync_thread:
            self.sync_thread.join(timeout=5)
        self.after(0, self._reset_start_button)

    def _reset_start_button(self):
        self.observer = None
        self.sync_active = False
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.progress_var.set("")
        self.start_button.configure(state="normal", text="Start Sync", command=self.start_sync)
        self.status_var.set("Bereit")

    def _report_progress(self, current, total):
        self.progress_queue.put((current, total))

    def delete_all_uploads(self):
        if self.config_data is None:
            messagebox.showwarning("Keine Konfiguration", "Bitte zuerst die Einstellungen ausfüllen.")
            return
        if self.sync_active:
            messagebox.showwarning("Synchronisierung aktiv", "Bitte zuerst die Synchronisierung beenden.")
            return

        confirmed = messagebox.askyesno(
            "Alle Uploads löschen?",
            "Dies löscht ALLE über diesen Client hochgeladenen Assets unwiderruflich vom Immich-Server "
            "und leert den lokalen Cache, damit der nächste Sync wieder bei null beginnt.\n\n"
            "Diese Aktion kann nicht rückgängig gemacht werden. Wirklich fortfahren?",
            icon="warning",
        )
        if not confirmed:
            return

        self.delete_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.status_var.set("Lösche alle Uploads …")
        threading.Thread(target=self._run_delete_all, daemon=True).start()

    def _run_delete_all(self):
        try:
            config = self.config_data
            api = Immich(config["api"]["url"], config["api"]["key"], album_by_year=True)
            api.delete_all_uploads()
            self.after(0, lambda: self.status_var.set("Alle Uploads gelöscht. Bereit für einen frischen Sync."))
        except Exception as exc:
            print(f"Fehler beim Löschen: {exc}")
            self.after(0, lambda: self.status_var.set("Fehler beim Löschen – siehe Log"))
        finally:
            self.after(0, lambda: self.delete_button.configure(state="normal"))
            self.after(0, lambda: self.start_button.configure(state="normal"))

    def _run_sync(self):
        try:
            config = self.config_data
            media_file_extensions = get_extensions_for_type()
            immich_host = config["api"]["url"]
            album_name = config["api"].get("album")
            album_by_year = config["api"].get("album_by_year", False)
            delete_remote_on_local_delete = config["api"].get("delete_remote_on_local_delete", False)
            api_key = config["api"]["key"]
            directories_to_watch = config["watchdog"]["directories"]
            recursive_scan = config["watchdog"].get("recursive", False)

            api = Immich(immich_host, api_key, album_name, album_by_year=album_by_year,
                         delete_remote_on_local_delete=delete_remote_on_local_delete)
            api.test_connection()
            api.print_shelve()
            api.upload_all_images(
                directories_to_watch, media_file_extensions, recursive_scan,
                progress_callback=self._report_progress,
                should_cancel=self.cancel_event.is_set,
            )

            if self.cancel_event.is_set():
                print("Synchronisierung abgebrochen.")
                self.after(0, self._reset_start_button)
                return

            self.observer = Observer()
            handler = SyncEventHandler(api, media_file_extensions)
            for directory in directories_to_watch:
                self.observer.schedule(handler, directory, recursive=True)
                print("watching directory: " + directory)
            self.observer.start()

            print("Synchronisierung gestartet, neue Dateien werden automatisch hochgeladen.")
            self.after(0, lambda: self.status_var.set("Synchronisierung aktiv"))
        except Exception as exc:
            print(f"Fehler: {exc}")
            self.after(0, lambda: self.status_var.set("Fehler – siehe Log"))
            self.after(0, self._reset_start_button)

    def on_close(self):
        self._hide_to_tray()

    def _hide_to_tray(self):
        self.withdraw()
        if not self._hide_notice_shown:
            self._hide_notice_shown = True
            if self.tray_icon:
                try:
                    self.tray_icon.notify(
                        "Immich Desktop Client läuft im Hintergrund weiter.",
                        "Immich Desktop Client",
                    )
                except Exception as exc:
                    print(f"Tray-Benachrichtigung fehlgeschlagen: {exc}")

    def quit_app(self):
        self._quitting = True
        self.cancel_event.set()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2)
        sys.stdout = self._orig_stdout
        if self.tray_icon:
            self.tray_icon.stop()
        if self._lock_socket:
            self._lock_socket.close()
        self.destroy()


if __name__ == "__main__":
    instance_lock = acquire_single_instance_lock()
    if instance_lock is None:
        signal_existing_instance_to_show()
        sys.exit(0)
    MainWindow(instance_lock).mainloop()
