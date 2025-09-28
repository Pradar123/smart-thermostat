# thermostat_supervisor_gui.py
# Egyoldalas Windows GUI a textfájlos, zónás termosztáthoz.
# - Nem indítja a controllert: a fájlokon keresztül felügyel, állít és figyel.
# - Stabil I/O: retry-olt olvasás/írás Samba/Windows lockok ellen.
# - Szép naplózás (logs/gui.log) rotációval; hibatűrő frissítés (nem omlik össze).
#
# Futtatás: python thermostat_supervisor_gui.py

import os
import json
import time
import threading
from datetime import datetime
from typing import Dict, Any, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# -------------------- Alap konstansok (tartsd szinkronban a controllerrel) --------------------
DEFAULT_NUM_ROOMS = 5
DEFAULT_LOOP_SECONDS = 2.0
DEFAULT_SETPOINT = 21.0
DEFAULT_HYSTERESIS = 0.3
DEFAULT_MIN_ON_SECONDS = 120
DEFAULT_MIN_OFF_SECONDS = 120
DEFAULT_TAU_MIN = 5.0

# Windows hálózati meghajtó alap (állítsd igény szerint)
DEFAULT_BASE_DIR = r"I:\thermostat"

OUTDOOR_BINS = [(-30, -10), (-10, 0), (0, 10), (10, 20), (20, 35), (35, 60)]

# I/O retry beállítások (Samba/Windows lockok ellen)
IO_RETRIES = 5
IO_DELAY_S = 0.2

# Log rotáció
MAX_LOG_BYTES = 2 * 1024 * 1024  # ~2MB

# -------------------- Segédfüggvények: idő, retry-os I/O --------------------

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _retry_io(fn, *args, **kwargs):
    last_exc = None
    for attempt in range(IO_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < IO_RETRIES - 1:
                time.sleep(IO_DELAY_S)
            else:
                raise last_exc

def atomic_write_text(path: str, content: str):
    tmp = path + ".tmp"
    def _write_and_replace():
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    _retry_io(_write_and_replace)

def load_json(path: str, default: Any) -> Any:
    try:
        def _load():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return _retry_io(_load)
    except Exception:
        return default

def save_json_atomic(path: str, obj: Any):
    tmp = path + ".tmp"
    def _dump_and_replace():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    _retry_io(_dump_and_replace)

def safe_read_text(path: str) -> Optional[str]:
    try:
        def _read():
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return _retry_io(_read)
    except Exception:
        return None

def safe_read_float(path: str, default: float) -> float:
    s = safe_read_text(path)
    if s is None or s == "":
        return default
    try:
        return float(s.replace(",", "."))
    except Exception:
        return default

# -------------------- Logger --------------------

class EventLogger:
    def __init__(self, log_path: str):
        self.log_path = log_path
        ensure_dir(os.path.dirname(log_path))
        self.lock = threading.Lock()

    def _rotate_if_needed(self):
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > MAX_LOG_BYTES:
                bak = self.log_path + ".1"
                try:
                    if os.path.exists(bak):
                        os.remove(bak)
                except Exception:
                    pass
                try:
                    os.replace(self.log_path, bak)
                except Exception:
                    pass
        except Exception:
            pass

    def log(self, msg: str):
        line = f"{iso_now()} | {msg}\n"
        try:
            with self.lock:
                self._rotate_if_needed()
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            try:
                # végső fallback: konzol
                print(line, end="")
            except Exception:
                pass

# -------------------- Fájlrendszer wrapper (gyökér köré) --------------------

class FS:
    def __init__(self, base_dir: str, logger: Optional[EventLogger] = None):
        self.base_dir = base_dir
        self.config_dir = os.path.join(base_dir, "config")
        self.data_dir = os.path.join(base_dir, "data")
        self.rooms_root = os.path.join(self.data_dir, "rooms")
        self.external_dir = os.path.join(self.data_dir, "external")
        self.logs_dir = os.path.join(self.data_dir, "logs")

        ensure_dir(self.config_dir)
        ensure_dir(self.data_dir)
        ensure_dir(self.rooms_root)
        ensure_dir(self.external_dir)
        ensure_dir(self.logs_dir)

        self.p_controller_cfg = os.path.join(self.config_dir, "controller_config.json")
        self.p_rooms_map = os.path.join(self.config_dir, "rooms.json")
        self.p_learning = os.path.join(self.config_dir, "learning.json")
        self.p_outdoor_temp = os.path.join(self.external_dir, "outdoor_temperature.txt")
        self.p_mode = os.path.join(self.external_dir, "mode.txt")
        self.p_status_all = os.path.join(self.data_dir, "status_all.json")
        self.p_log = os.path.join(self.logs_dir, "events.log")  # controller logja
        self.p_gui_log = os.path.join(self.logs_dir, "gui.log") # GUI log

        self.logger = logger or EventLogger(self.p_gui_log)

        # failsafe alapok
        self._ensure_controller_cfg()
        self._ensure_rooms_map()
        if safe_read_text(self.p_outdoor_temp) is None:
            atomic_write_text(self.p_outdoor_temp, "10.0")
        if safe_read_text(self.p_mode) is None:
            atomic_write_text(self.p_mode, "auto")
        if load_json(self.p_learning, None) is None:
            save_json_atomic(self.p_learning, {"rooms": {}})

        # per-room failsafe az aktuális num_rooms szerint
        cfg = load_json(self.p_controller_cfg, {})
        num_rooms = int(cfg.get("num_rooms", DEFAULT_NUM_ROOMS))
        self.ensure_rooms_exist(num_rooms)

    def _ensure_controller_cfg(self):
        cfg = load_json(self.p_controller_cfg, None)
        if cfg is None:
            cfg = {
                "num_rooms": DEFAULT_NUM_ROOMS,
                "loop_seconds": DEFAULT_LOOP_SECONDS,
                "global_hysteresis": DEFAULT_HYSTERESIS,
                "min_on_seconds": DEFAULT_MIN_ON_SECONDS,
                "min_off_seconds": DEFAULT_MIN_OFF_SECONDS,
                "tau_minutes": DEFAULT_TAU_MIN
            }
            save_json_atomic(self.p_controller_cfg, cfg)
            self.logger.log("controller_config.json létrehozva alapértékekkel.")

    def _ensure_rooms_map(self):
        m = load_json(self.p_rooms_map, None)
        if m is None:
            rooms = [{"id": f"room{i}", "name": f"Szoba {i}", "enabled": True}
                     for i in range(1, DEFAULT_NUM_ROOMS + 1)]
            m = {"rooms": rooms}
            save_json_atomic(self.p_rooms_map, m)
            self.logger.log("rooms.json létrehozva alap szobákkal.")

    def ensure_rooms_exist(self, num_rooms: int):
        # egészítsük ki a rooms.json-t és a per-szoba fájlokat
        m = load_json(self.p_rooms_map, {"rooms": []})
        existing_ids = {r["id"] for r in m.get("rooms", [])}
        changed = False
        for i in range(1, num_rooms + 1):
            rid = f"room{i}"
            if rid not in existing_ids:
                m["rooms"].append({"id": rid, "name": f"Szoba {i}", "enabled": True})
                changed = True
            # per-room mappa + alap txt-k
            rdir = os.path.join(self.rooms_root, rid)
            ensure_dir(rdir)
            paths = {
                "sensor_temperature.txt": f"{DEFAULT_SETPOINT:.2f}",
                "sensor_humidity.txt": "50.0",
                "setpoint.txt": f"{DEFAULT_SETPOINT:.2f}",
                "hysteresis.txt": f"{DEFAULT_HYSTERESIS:.2f}",
                "command_heat.txt": "0",
            }
            for fname, defval in paths.items():
                p = os.path.join(rdir, fname)
                if safe_read_text(p) is None:
                    atomic_write_text(p, defval)
            pstat = os.path.join(rdir, "status.json")
            if load_json(pstat, None) is None:
                save_json_atomic(pstat, {"init": True, "room_id": rid, "name": f"Szoba {i}"})
        if changed:
            save_json_atomic(self.p_rooms_map, m)
            self.logger.log(f"rooms.json bővítve {num_rooms} szobára.")
        # controller_config.json-ban is állítsuk be a num_rooms-ot
        cfg = load_json(self.p_controller_cfg, {})
        cfg["num_rooms"] = int(num_rooms)
        save_json_atomic(self.p_controller_cfg, cfg)

    # ---- Tanulási adatok törlése (szobánként) ----
    def reset_learning_for_room(self, room_id: str) -> bool:
        try:
            data = load_json(self.p_learning, {"rooms": {}})
            if "rooms" not in data or not isinstance(data["rooms"], dict):
                data["rooms"] = {}
            # alap szerkezet üresítve
            data["rooms"][room_id] = {
                "bins": {},
                "ema_slope_on": None,
                "ema_slope_off": None
            }
            save_json_atomic(self.p_learning, data)
            self.logger.log(f"{room_id}: learning.json tanulási adatok törölve.")
            return True
        except Exception as e:
            self.logger.log(f"{room_id}: learning reset hiba: {e}")
            return False

# -------------------- Szoba sor (UI komponens) --------------------

class RoomRow(ttk.Frame):
    def __init__(self, master, fs: FS, room: Dict[str, Any]):
        super().__init__(master)
        self.fs = fs
        self.room_id = room.get("id")
        self.name_var = tk.StringVar(value=room.get("name", self.room_id))
        self.enabled_var = tk.BooleanVar(value=bool(room.get("enabled", True)))

        # Dinamikus értékek
        self.temp_var = tk.StringVar(value="—")
        self.hum_var = tk.StringVar(value="—")
        self.cmd_var = tk.StringVar(value="OFF")
        self.overshoot_var = tk.StringVar(value="—")
        self.last_switch_var = tk.StringVar(value="—")

        # Állítható értékek
        setp_path = os.path.join(self.fs.rooms_root, self.room_id, "setpoint.txt")
        hyst_path = os.path.join(self.fs.rooms_root, self.room_id, "hysteresis.txt")
        self.setpoint_var = tk.StringVar(value=safe_read_text(setp_path) or f"{DEFAULT_SETPOINT:.2f}")
        self.hyst_var = tk.StringVar(value=safe_read_text(hyst_path) or f"{DEFAULT_HYSTERESIS:.2f}")

        # UI: rács, kompakt táblázatsor
        col = 0
        ttk.Label(self, text=self.room_id, width=8).grid(row=0, column=col, padx=4, pady=3, sticky="w"); col += 1

        ttk.Entry(self, textvariable=self.name_var, width=18).grid(row=0, column=col, padx=4, pady=3); col += 1
        ttk.Checkbutton(self, variable=self.enabled_var).grid(row=0, column=col, padx=4, pady=3); col += 1

        ttk.Label(self, textvariable=self.temp_var, width=8).grid(row=0, column=col, padx=4, pady=3); col += 1
        ttk.Label(self, textvariable=self.hum_var, width=8).grid(row=0, column=col, padx=4, pady=3); col += 1

        self.setp_entry = ttk.Entry(self, textvariable=self.setpoint_var, width=8)
        self.setp_entry.grid(row=0, column=col, padx=4, pady=3); col += 1

        self.hyst_entry = ttk.Entry(self, textvariable=self.hyst_var, width=8)
        self.hyst_entry.grid(row=0, column=col, padx=4, pady=3); col += 1

        self.cmd_label = ttk.Label(self, textvariable=self.cmd_var, width=8)
        self.cmd_label.grid(row=0, column=col, padx=4, pady=3); col += 1

        ttk.Label(self, textvariable=self.overshoot_var, width=10).grid(row=0, column=col, padx=4, pady=3); col += 1
        ttk.Label(self, textvariable=self.last_switch_var, width=19).grid(row=0, column=col, padx=4, pady=3); col += 1

        ttk.Button(self, text="Mentés", command=self.save_changes).grid(row=0, column=col, padx=4, pady=3); col += 1
        ttk.Button(self, text="Mappa", command=self.open_folder).grid(row=0, column=col, padx=4, pady=3); col += 1
        ttk.Button(self, text="Reset tanulás", command=self.reset_learning).grid(row=0, column=col, padx=4, pady=3); col += 1

        # Hover tippek
        self.setp_entry.tooltip = CreateToolTip(self.setp_entry, "Célhőmérséklet (°C) – setpoint.txt")
        self.hyst_entry.tooltip = CreateToolTip(self.hyst_entry, "Hiszterézis (°C) – hysteresis.txt")

        self.update_status_colors()

    def open_folder(self):
        rdir = os.path.join(self.fs.rooms_root, self.room_id)
        try:
            os.startfile(rdir)  # Windows
        except Exception:
            messagebox.showerror("Hiba", f"Nem sikerült megnyitni: {rdir}")

    def update_status_colors(self):
        txt = (self.cmd_var.get() or "").upper()
        fg = "#2e7d32" if txt == "ON" else "#555555"
        try:
            self.cmd_label.configure(foreground=fg)
        except Exception:
            pass

    def refresh_from_files(self):
        # status.json preferált, fallback a nyers txt-kre
        try:
            pstat = os.path.join(self.fs.rooms_root, self.room_id, "status.json")
            st = load_json(pstat, None)
            if st:
                t = st.get("temperature")
                h = st.get("humidity")
                cmd = st.get("heating_command")
                po = st.get("predicted_overshoot")
                ls = st.get("last_switch_iso")
                if isinstance(t, (int, float)):
                    self.temp_var.set(f"{t:.2f}°")
                else:
                    self.temp_var.set("—")
                if isinstance(h, (int, float)):
                    self.hum_var.set(f"{h:.0f}%")
                else:
                    self.hum_var.set("—")
                self.cmd_var.set("ON" if int(cmd or 0) == 1 else "OFF")
                self.overshoot_var.set("—" if po is None else f"{float(po):.2f}°")
                self.last_switch_var.set(ls or "—")
            else:
                # fallback
                pt = os.path.join(self.fs.rooms_root, self.room_id, "sensor_temperature.txt")
                ph = os.path.join(self.fs.rooms_root, self.room_id, "sensor_humidity.txt")
                cmdp = os.path.join(self.fs.rooms_root, self.room_id, "command_heat.txt")
                t = safe_read_float(pt, DEFAULT_SETPOINT)
                h = safe_read_float(ph, 50.0)
                c = safe_read_text(cmdp)
                self.temp_var.set(f"{t:.2f}°")
                self.hum_var.set(f"{h:.0f}%")
                self.cmd_var.set("ON" if (c or "0").strip() == "1" else "OFF")
                self.overshoot_var.set("—")
                self.last_switch_var.set("—")

            # setpoint/hyst UI frissítés (ha más írta közben)
            sp_path = os.path.join(self.fs.rooms_root, self.room_id, "setpoint.txt")
            hy_path = os.path.join(self.fs.rooms_root, self.room_id, "hysteresis.txt")
            sp_now = safe_read_text(sp_path)
            hy_now = safe_read_text(hy_path)
            if sp_now and self.setpoint_var.get() != sp_now:
                self.setpoint_var.set(sp_now)
            if hy_now and self.hyst_var.get() != hy_now:
                self.hyst_var.set(hy_now)

            self.update_status_colors()
        except Exception as e:
            # nem állunk le
            self.fs.logger.log(f"{self.room_id}: refresh error: {e}")

    def save_changes(self):
        # név + enabled -> rooms.json
        try:
            rooms = load_json(self.fs.p_rooms_map, {"rooms": []})
            changed = False
            for r in rooms.get("rooms", []):
                if r.get("id") == self.room_id:
                    new_name = self.name_var.get().strip() or self.room_id
                    new_enabled = bool(self.enabled_var.get())
                    if r.get("name") != new_name:
                        r["name"] = new_name
                        changed = True
                    if bool(r.get("enabled", True)) != new_enabled:
                        r["enabled"] = new_enabled
                        changed = True
                    break
            if changed:
                save_json_atomic(self.fs.p_rooms_map, rooms)
                self.fs.logger.log(f"{self.room_id}: rooms.json frissítve (name/enabled).")
        except Exception as e:
            self.fs.logger.log(f"{self.room_id}: rooms.json mentés hiba: {e}")
            messagebox.showerror("Hiba", f"rooms.json mentése sikertelen:\n{e}")

        # setpoint / hysteresis txt
        try:
            sp = float(self.setpoint_var.get().replace(",", "."))
            atomic_write_text(os.path.join(self.fs.rooms_root, self.room_id, "setpoint.txt"), f"{sp:.2f}")
        except Exception as e:
            self.fs.logger.log(f"{self.room_id}: setpoint mentés hiba: {e}")
            messagebox.showwarning("Figyelem", f"{self.room_id}: Setpoint formátum hibás.")
        try:
            hy = float(self.hyst_var.get().replace(",", "."))
            atomic_write_text(os.path.join(self.fs.rooms_root, self.room_id, "hysteresis.txt"), f"{hy:.2f}")
        except Exception as e:
            self.fs.logger.log(f"{self.room_id}: hysteresis mentés hiba: {e}")
            messagebox.showwarning("Figyelem", f"{self.room_id}: Hysteresis formátum hibás.")

    def reset_learning(self):
        try:
            if not messagebox.askyesno("Megerősítés",
                                       f"Biztosan törlöd a tanulási adatokat ennél a szobánál?\n({self.room_id})"):
                return
            ok = self.fs.reset_learning_for_room(self.room_id)
            if ok:
                messagebox.showinfo("OK", f"{self.room_id}: tanulási adatok törölve.")
                # felület frissítése (következő controller ciklusban az overshoot le fog esni)
                self.refresh_from_files()
            else:
                messagebox.showerror("Hiba", f"{self.room_id}: tanulási adatok törlése sikertelen. Részletek a logban.")
        except Exception as e:
            self.fs.logger.log(f"{self.room_id}: reset_learning UI hiba: {e}")
            messagebox.showerror("Hiba", f"{self.room_id}: reset_learning hiba:\n{e}")

# Egyszerű tooltip segéd
class CreateToolTip:
    def __init__(self, widget, text=''):
        self.widget = widget
        self.text = text
        self.tipwindow = None
        widget.bind("<Enter>", self.showtip)
        widget.bind("<Leave>", self.hidetip)

    def showtip(self, event=None):
        if self.tipwindow or not self.text:
            return
        try:
            x, y, cx, cy = self.widget.bbox("insert")
        except Exception:
            x = y = 0
        x = x + self.widget.winfo_rootx() + 20
        y = y + self.widget.winfo_rooty() + 20
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(1)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         relief=tk.SOLID, borderwidth=1,
                         background="#ffffe0")
        label.pack(ipadx=4, ipady=2)

    def hidetip(self, event=None):
        tw = self.tipwindow
        self.tipwindow = None
        if tw:
            tw.destroy()

# -------------------- Fő alkalmazás --------------------

class SupervisorApp(tk.Tk):
    def __init__(self, base_dir: str):
        super().__init__()
        self.title("Thermostat Supervisor")
        self.geometry("1280x720")
        self.minsize(1100, 600)

        # Alap stílus
        try:
            self.style = ttk.Style(self)
            if "vista" in self.style.theme_names():
                self.style.theme_use("vista")
        except Exception:
            pass

        # Logger + FS
        tmp_logger = EventLogger(os.path.join(base_dir, "data", "logs", "gui.log"))
        self.fs = FS(base_dir, logger=tmp_logger)
        self.logger = self.fs.logger

        self.refresh_interval_ms = 1000

        # Fejléc / útvonal
        top = ttk.Frame(self, padding=8)
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(top, text="Base dir:", width=9).pack(side=tk.LEFT)
        self.base_var = tk.StringVar(value=self.fs.base_dir)
        self.base_entry = ttk.Entry(top, textvariable=self.base_var)
        self.base_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(top, text="Módosít…", command=self.change_base).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Reload", command=self.reload_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="Log megnyitása", command=self.open_log).pack(side=tk.LEFT, padx=4)

        # Globális beállítások keret
        globalf = ttk.LabelFrame(self, text="Globális beállítások", padding=8)
        globalf.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        cfg = load_json(self.fs.p_controller_cfg, {})
        self.num_rooms_var = tk.IntVar(value=int(cfg.get("num_rooms", DEFAULT_NUM_ROOMS)))
        self.loop_var = tk.StringVar(value=str(cfg.get("loop_seconds", DEFAULT_LOOP_SECONDS)))
        self.hyst_global_var = tk.StringVar(value=str(cfg.get("global_hysteresis", DEFAULT_HYSTERESIS)))
        self.min_on_var = tk.StringVar(value=str(cfg.get("min_on_seconds", DEFAULT_MIN_ON_SECONDS)))
        self.min_off_var = tk.StringVar(value=str(cfg.get("min_off_seconds", DEFAULT_MIN_OFF_SECONDS)))
        self.tau_var = tk.StringVar(value=str(cfg.get("tau_minutes", DEFAULT_TAU_MIN)))

        # elrendezés
        row1 = ttk.Frame(globalf)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Szobák száma:").pack(side=tk.LEFT)
        ttk.Spinbox(row1, from_=1, to=50, textvariable=self.num_rooms_var, width=6).pack(side=tk.LEFT, padx=6)

        ttk.Label(row1, text="Loop (s):").pack(side=tk.LEFT, padx=(18,0))
        ttk.Entry(row1, textvariable=self.loop_var, width=8).pack(side=tk.LEFT, padx=6)

        ttk.Label(row1, text="Globális hiszterézis (°C):").pack(side=tk.LEFT, padx=(18,0))
        ttk.Entry(row1, textvariable=self.hyst_global_var, width=8).pack(side=tk.LEFT, padx=6)

        row2 = ttk.Frame(globalf)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Min ON (s):").pack(side=tk.LEFT)
        ttk.Entry(row2, textvariable=self.min_on_var, width=8).pack(side=tk.LEFT, padx=6)

        ttk.Label(row2, text="Min OFF (s):").pack(side=tk.LEFT, padx=(18,0))
        ttk.Entry(row2, textvariable=self.min_off_var, width=8).pack(side=tk.LEFT, padx=6)

        ttk.Label(row2, text="Tau (perc):").pack(side=tk.LEFT, padx=(18,0))
        ttk.Entry(row2, textvariable=self.tau_var, width=8).pack(side=tk.LEFT, padx=6)

        ttk.Button(globalf, text="Konfig mentése", command=self.save_global_config).pack(side=tk.RIGHT, padx=4)

        # Külső bemenetek (mode, outdoor)
        extf = ttk.LabelFrame(self, text="Külső bemenetek (HA/Node-RED)", padding=8)
        extf.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        self.mode_var = tk.StringVar(value=(safe_read_text(self.fs.p_mode) or "auto").lower())
        self.outdoor_var = tk.StringVar(value=safe_read_text(self.fs.p_outdoor_temp) or "10.0")

        ttk.Label(extf, text="Mód:").pack(side=tk.LEFT)
        self.mode_combo = ttk.Combobox(extf, textvariable=self.mode_var, values=["auto", "off"], width=8, state="readonly")
        self.mode_combo.pack(side=tk.LEFT, padx=6)

        ttk.Label(extf, text="Kültéri hőmérséklet (°C):").pack(side=tk.LEFT, padx=(18,0))
        ttk.Entry(extf, textvariable=self.outdoor_var, width=10).pack(side=tk.LEFT, padx=6)

        ttk.Button(extf, text="Bemenetek mentése", command=self.save_external).pack(side=tk.RIGHT, padx=4)

        # Szobák: görgethető táblázat
        roomsf = ttk.LabelFrame(self, text="Szobák", padding=4)
        roomsf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=6)

        header = ttk.Frame(roomsf)
        header.pack(fill=tk.X, padx=2)
        # +1 oszlop a "Reset tanulás" gombnak
        labels = ["ID", "Név", "Aktív", "T (°C)", "RH (%)", "Setpoint", "Hyster.", "Fűtés", "Overshoot", "Utolsó váltás", "", "", "Reset"]
        widths =  [8,   18,    6,      8,       8,        8,         8,        8,         10,             19,                6,  6,   12]
        for i, (t, w) in enumerate(zip(labels, widths)):
            ttk.Label(header, text=t, width=w).grid(row=0, column=i, padx=4, pady=2, sticky="w")

        self.canvas = tk.Canvas(roomsf, highlightthickness=0)
        self.scroll = ttk.Scrollbar(roomsf, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0,0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.room_rows: Dict[str, RoomRow] = {}
        self.rebuild_room_rows()

        # Alsó gombsor
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bottom, text="Minden szobaváltoztatás mentése", command=self.save_all_rooms).pack(side=tk.RIGHT, padx=6)
        ttk.Button(bottom, text="Szobák frissítése a szám alapján", command=self.apply_num_rooms).pack(side=tk.RIGHT, padx=6)

        # Status bar
        self.status_var = tk.StringVar(value="Kész.")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(8,4))
        status.pack(side=tk.BOTTOM, fill=tk.X)

        # Periodikus státuszfrissítés
        self.after(self.refresh_interval_ms, self.periodic_refresh)

        self.logger.log("GUI elindult.")

    # ---------- Akciók ----------

    def change_base(self):
        try:
            newdir = filedialog.askdirectory(initialdir=self.fs.base_dir, title="Válassz projekt gyökeret")
            if not newdir:
                return
            self.base_var.set(newdir)
            self.fs = FS(newdir)  # új FS + saját logger a mappába
            self.logger = self.fs.logger
            self.reload_all()
            self.logger.log(f"Base dir módosítva: {newdir}")
        except Exception as e:
            self.logger.log(f"change_base error: {e}")
            messagebox.showerror("Hiba", f"Nem sikerült módosítani az elérési utat:\n{e}")

    def reload_all(self):
        try:
            cfg = load_json(self.fs.p_controller_cfg, {})
            self.num_rooms_var.set(int(cfg.get("num_rooms", DEFAULT_NUM_ROOMS)))
            self.loop_var.set(str(cfg.get("loop_seconds", DEFAULT_LOOP_SECONDS)))
            self.hyst_global_var.set(str(cfg.get("global_hysteresis", DEFAULT_HYSTERESIS)))
            self.min_on_var.set(str(cfg.get("min_on_seconds", DEFAULT_MIN_ON_SECONDS)))
            self.min_off_var.set(str(cfg.get("min_off_seconds", DEFAULT_MIN_OFF_SECONDS)))
            self.tau_var.set(str(cfg.get("tau_minutes", DEFAULT_TAU_MIN)))

            self.mode_var.set((safe_read_text(self.fs.p_mode) or "auto").lower())
            self.outdoor_var.set(safe_read_text(self.fs.p_outdoor_temp) or "10.0")

            self.rebuild_room_rows()
            self.status_var.set("Újratöltve.")
            self.logger.log("GUI Reload kész.")
        except Exception as e:
            self.logger.log(f"reload_all error: {e}")
            messagebox.showerror("Hiba", f"Reload sikertelen:\n{e}")

    def rebuild_room_rows(self):
        for child in self.inner.winfo_children():
            child.destroy()
        rooms_map = load_json(self.fs.p_rooms_map, {"rooms": []})
        self.room_rows.clear()
        for i, r in enumerate(rooms_map.get("rooms", [])):
            row = RoomRow(self.inner, self.fs, r)
            row.grid(row=i, column=0, sticky="w", padx=2, pady=1)
            self.room_rows[r.get("id")] = row

    def open_log(self):
        try:
            os.startfile(self.fs.p_log)  # controller log
        except Exception:
            try:
                os.startfile(self.fs.p_gui_log)  # GUI log, ha a controller log még nincs
            except Exception:
                messagebox.showinfo("Info", "Még nincs logfájl vagy nem megnyitható.")

    def save_global_config(self):
        try:
            cfg = load_json(self.fs.p_controller_cfg, {})
            cfg["num_rooms"] = int(self.num_rooms_var.get())
            cfg["loop_seconds"] = float(str(self.loop_var.get()).replace(",", "."))
            cfg["global_hysteresis"] = float(str(self.hyst_global_var.get()).replace(",", "."))
            cfg["min_on_seconds"] = float(str(self.min_on_var.get()).replace(",", "."))
            cfg["min_off_seconds"] = float(str(self.min_off_var.get()).replace(",", "."))
            cfg["tau_minutes"] = float(str(self.tau_var.get()).replace(",", "."))
            save_json_atomic(self.fs.p_controller_cfg, cfg)
            self.fs.ensure_rooms_exist(int(cfg["num_rooms"]))  # ha bővül, azonnal generáljuk a fájlokat
            self.logger.log("Globális konfig mentve.")
            messagebox.showinfo("OK", "Globális konfig mentve.")
        except Exception as e:
            self.logger.log(f"Konfig mentése hiba: {e}")
            messagebox.showerror("Hiba", f"Konfig mentése sikertelen:\n{e}")

    def save_external(self):
        try:
            mode = self.mode_var.get().strip().lower()
            if mode not in ("auto", "off"):
                messagebox.showwarning("Figyelem", "Mód csak 'auto' vagy 'off' lehet.")
                return
            atomic_write_text(self.fs.p_mode, mode)
            out = float(self.outdoor_var.get().replace(",", "."))
            atomic_write_text(self.fs.p_outdoor_temp, f"{out:.2f}")
            self.logger.log(f"Külső bemenetek mentve: mode={mode}, outdoor={out:.2f}")
            messagebox.showinfo("OK", "Bemenetek mentve.")
        except Exception as e:
            self.logger.log(f"Bemenetek mentése hiba: {e}")
            messagebox.showerror("Hiba", f"Bemenetek mentése sikertelen:\n{e}")

    def save_all_rooms(self):
        err = 0
        for rr in self.room_rows.values():
            try:
                rr.save_changes()
            except Exception as e:
                err += 1
                self.logger.log(f"save_all_rooms item error: {e}")
        if err == 0:
            messagebox.showinfo("OK", "Minden szoba mentve.")
            self.logger.log("Minden szoba mentve.")
        else:
            messagebox.showwarning("Figyelem", f"{err} szobánál hiba történt. Részletek a logban.")
            self.logger.log(f"Minden szoba mentve, de {err} hibával.")

    def apply_num_rooms(self):
        try:
            n = int(self.num_rooms_var.get())
            if n < 1 or n > 50:
                raise ValueError("1..50")
            self.fs.ensure_rooms_exist(n)
            self.rebuild_room_rows()
            self.logger.log(f"Szobák frissítve: {n}")
            messagebox.showinfo("OK", "Szobák frissítve a megadott számmal.")
        except Exception as e:
            self.logger.log(f"apply_num_rooms error: {e}")
            messagebox.showwarning("Figyelem", f"Szobaszám frissítés hiba:\n{e}")

    def periodic_refresh(self):
        try:
            for rr in self.room_rows.values():
                try:
                    rr.refresh_from_files()
                except Exception as e_row:
                    self.logger.log(f"{rr.room_id}: periodic refresh error: {e_row}")
            self.status_var.set(f"Frissítve: {iso_now()}")
        except Exception as e:
            # itt sem állunk le, csak logolunk
            self.logger.log(f"periodic_refresh error: {e}")
        finally:
            # mindenképp újraütemezünk
            self.after(self.refresh_interval_ms, self.periodic_refresh)

# -------------------- Belépési pont --------------------

if __name__ == "__main__":
    app = SupervisorApp(base_dir=DEFAULT_BASE_DIR)
    app.mainloop()
