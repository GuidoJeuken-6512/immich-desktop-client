# Plan: Immich Desktop Client als Tray-Hintergrund-App (wie OneDrive/Nextcloud)

## Context

Die App läuft aktuell nur als normales Vordergrund-Fenster: Sync muss per Knopfdruck gestartet werden, und Schließen des Fensters beendet den gesamten Prozess inkl. laufendem Sync/Watchdog-Observer. Der Nutzer möchte stattdessen ein Verhalten wie bei OneDrive/Nextcloud: App startet mit Windows, läuft dauerhaft im System-Tray, synchronisiert automatisch im Hintergrund, und das Fenster ist nur eine optionale Ansicht (Schließen = Minimieren ins Tray, nicht Beenden).

Die nötigen Abhängigkeiten (`pystray`, `Pillow`) sind im `.venv` bereits installiert, werden aber von keinem Code aktuell genutzt. Die Installer-Konfiguration legt bereits eine Autostart-Verknüpfung im Windows-Startup-Ordner an — sie startet aktuell nur die normale GUI ohne Auto-Sync.

Alle Änderungen betreffen ausschließlich `src/main.py`, `immich-desktop-client.spec` und `resources/installer-script.iss`. `src/immich.py` bleibt unverändert (stabile Sync-Engine, bereits passend entkoppelt über Callbacks).

## Änderungen im Detail

### 1. `src/main.py`

**Neue Imports:** `pystray`, `PIL.Image`, `socket`.

**Single-Instance-Lock + Bring-to-Front-Signal** (neue Modul-Funktionen, nach den Pfad-Konstanten ~Zeile 23):
- Fester lokaler TCP-Port (z.B. `47834`) als Mutex-Ersatz — kein neues schweres Abhängigkeit (kein `pywin32` nötig).
- `acquire_single_instance_lock()`: bindet den Port. Erfolg → erste Instanz, Socket-Referenz muss für die Prozesslaufzeit gehalten werden (nicht schließen/GC). Fehlschlag (`OSError`) → es läuft bereits eine Instanz.
- Da der Nutzer **"Fenster nach vorne holen"** gewählt hat: der lock-haltende Prozess startet zusätzlich einen leichten Listener-Thread auf einem zweiten festen Port (oder reicht ein kurzes "SHOW"-Kommando über denselben Socket via `listen()/accept()`), der eingehende Verbindungen entgegennimmt und per `self.after(0, self._show_window)` das Fenster nach vorne holt. Eine zweite Instanz, die den Lock nicht bekommt, verbindet sich kurz zu diesem Port, sendet ein Signal und beendet sich sofort (`sys.exit(0)`).
- `is_background_launch()`: prüft `"--background" in sys.argv[1:]`.

**`MainWindow.__init__` (Zeilen 169-197):**
- Neues Flag `self._quitting = False`, `self._hide_notice_shown = False`.
- Nach `_build_ui()`: `_build_tray_icon()` aufrufen, Icon-Thread starten (`threading.Thread(target=self.tray_icon.run, daemon=True).start()`).
- Wenn `config_data` vorhanden ist: Sync automatisch starten (`self.after(200, self.start_sync)`), analog zum bestehenden Muster für `open_config_window` bei fehlender Config (Zeile 195).
- Wenn `is_background_launch()`: Fenster sofort per `self.withdraw()` verbergen, bevor es sichtbar aufblitzt. **Ausnahme laut Nutzerentscheidung:** beim allerersten Start (keine Config vorhanden) bleibt das Fenster offen, damit der Nutzer die Einstellungen ausfüllen kann/den ersten Sync-Fortschritt sieht — `withdraw()` nur anwenden, wenn `config_data is not None`.

**Neue Methode `_build_tray_icon(self)`:**
- Lädt `Image.open(str(ICON_PATH))` (Pillow kann `.ico` direkt öffnen, kein neues Asset nötig).
- Menü (deutsche Labels, passend zur bestehenden UI):
  - "Öffnen" (Default-Aktion bei Linksklick) → zeigt Fenster
  - dynamischer Eintrag "Synchronisierung pausieren" / "fortsetzen" → toggelt `start_sync`/`cancel_sync`
  - "Einstellungen" → zeigt Fenster + öffnet `ConfigWindow`
  - "Alle Uploads löschen" → zeigt Fenster + bestehender Lösch-Dialog
  - Separator
  - "Beenden" → `quit_app()`
- `self.tray_icon = pystray.Icon("immich-desktop-client", icon_image, "Immich Desktop Client", menu)`

**Tray-Callbacks** (laufen auf pystray-Thread, müssen wie der Rest der Codebase per `self.after(0, ...)` zurück ins Tk-Thread dispatchen — exakt das bestehende Muster aus `_run_sync`/`_run_delete_all`):
- `_tray_show_window` → `self._show_window()` (neu: `deiconify()`, `lift()`, `focus_force()`)
- `_tray_toggle_sync` → `cancel_sync` bzw. `start_sync` je nach `self.sync_active`
- `_tray_open_settings`, `_tray_delete_all_uploads` → Fenster zeigen + bestehende Methode aufrufen
- `_tray_quit` → `quit_app()`

**`on_close()` (Zeilen 405-411) — Verhalten ändern:**
- Statt Teardown: nur `self.withdraw()`. Sync/Observer laufen unverändert weiter.
- Einmalig (gesteuert über `self._hide_notice_shown`) eine Tray-Benachrichtigung zeigen (`self.tray_icon.notify(...)`), dass die App im Hintergrund weiterläuft.

**Neue Methode `quit_app(self)`** (übernimmt den bisherigen Teardown-Code aus `on_close`):
- `self._quitting = True`
- `cancel_event.set()`, Observer stoppen + joinen (Timeout 2s)
- `sys.stdout` zurücksetzen
- `self.tray_icon.stop()` (pystray-Loop sauber beenden)
- `self.destroy()`

**Entry Point (Zeilen 414-415):**
```python
if __name__ == "__main__":
    lock_socket = acquire_single_instance_lock()
    if lock_socket is None:
        signal_existing_instance_to_show()  # kurzes Connect+Send an Show-Port
        sys.exit(0)
    MainWindow(lock_socket).mainloop()
```

### 2. `immich-desktop-client.spec`

- Nach Implementierung einmal bauen und die exe testen. `pyinstaller-hooks-contrib` deckt `pystray`/`PIL` i.d.R. ab, aber **empirisch prüfen** (Build + Start, auf `ImportError`/`ModuleNotFoundError` achten — typischerweise `pystray._win32`). Falls nötig, zu `hiddenimports` ergänzen (aktuell `['dbm.sqlite3', 'sqlite3']`).
- Keine neuen `datas` nötig — `icon.ico` ist bereits gebündelt und wird für das Tray-Icon wiederverwendet.

### 3. `resources/installer-script.iss`

- Nur die `{userstartup}`-Zeile (aktuell Zeile 58) ändern:
  ```
  Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--background"
  ```
- Start-Menü- (Zeile 56) und Desktop-Verknüpfung (Zeile 57) bleiben unverändert — Start im Vordergrund, Single-Instance-Logik holt im Tray-Fall ein laufendes Fenster nach vorne.

## Out of Scope (bewusst nicht Teil dieser Änderung)

- Unterschiedliche Tray-Icon-Grafiken je Status (Pause/Sync/Fehler) — ein statisches Icon für alle Zustände.
- Sonderbehandlung von Windows-Logoff/Shutdown-Events.
- Mehrinstanz-Unterstützung mit unterschiedlichen Configs.
- Code-Signing.
- Weitere Toast-Benachrichtigungen (Sync abgeschlossen, Fehler, neue Uploads) — nur der einmalige "läuft im Hintergrund weiter"-Hinweis.

## Risiken / Annahmen, die früh verifiziert werden sollten

1. **pystray-Thread + tkinter-Mainloop unter PyInstaller:** Sollte auf Windows (win32-Backend) unproblematisch sein, da nur macOS/Cocoa einen Hauptthread-Zwang hat — aber im gefrorenen Build empirisch testen, nicht nur mit `python src/main.py`.
2. **Hidden Imports für pystray/PIL:** siehe Spec-Abschnitt oben — Build-and-run-Check einplanen.
3. **Socket-basierter Single-Instance-Lock:** einfache, abhängigkeitsfreie Lösung; Randfall: schneller Neustart nach Absturz kann durch TIME_WAIT kurzzeitig fälschlich "läuft bereits" melden — beim Testen auf Absturz+Sofort-Neustart prüfen.

## Verifikation

1. `python src/main.py` lokal starten — manuelles Testen aller Tray-Menüpunkte, Fenster-Schließen→Tray, "Beenden"→Prozess tatsächlich weg (Task-Manager prüfen).
2. Zweite Instanz parallel starten (`python src/main.py`) während die erste läuft → bestehendes Fenster muss nach vorne kommen, keine zweite Tray-Ikone.
3. `python src/main.py --background` mit vorhandener Config → kein Fenster sichtbar, Tray-Icon erscheint, Sync läuft automatisch an (Log/Server prüfen).
4. `python src/main.py --background` ganz ohne Config (frischer Client) → Fenster bleibt sichtbar (Ausnahme), Einstellungsdialog öffnet sich wie bisher.
5. Mit PyInstaller neu bauen (`.venv/Scripts/python.exe -m PyInstaller immich-desktop-client.spec --noconfirm`), exe direkt starten, auf fehlende Module achten, ggf. `hiddenimports` ergänzen und neu bauen.
6. Installer neu bauen (`ISCC.exe resources/installer-script.iss`), installieren, Windows abmelden/anmelden oder Startup-Verknüpfung manuell ausführen → App muss lautlos im Tray mit aktivem Sync erscheinen.

## Status: Umgesetzt

Alle Punkte oben sind implementiert (`src/main.py`, `immich-desktop-client.spec` unverändert nötig, `resources/installer-script.iss`). Empirisch geprüft: `pystray`/`PIL` benötigen auf Windows keine zusätzlichen `hiddenimports` (nur Linux/macOS-Backend-Importe fehlen, was unkritisch ist). Single-Instance-Lock und Tray-Icon im gefrorenen Build (`dist/immich-desktop-client.exe`) erfolgreich getestet.
