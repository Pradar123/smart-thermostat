# thermostat_controller.py
# Windows-kompatibilis, zónás, textfájlos termosztát.
# Stabil I/O (retry-olt atomi írás/olvasás), hibatűrő főciklus, log-rotáció.
# ÖNTANULÁS: a vezérlésből kivéve; szobánként külön TXT-be logoljuk az
#            ON/OFF szegmenseket és a kikapcsolás utáni overshootot.

import os
import sys
import json
import time
import argparse
import threading
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

# ------------------------- Alapbeállítások -------------------------

DEFAULT_NUM_ROOMS = 5
DEFAULT_LOOP_SECONDS = 2.0
DEFAULT_SETPOINT = 21.0
DEFAULT_HYSTERESIS = 0.3        # °C
DEFAULT_MIN_ON_SECONDS = 120
DEFAULT_MIN_OFF_SECONDS = 120
DEFAULT_EPS_TEMP = 0.01

# Windows hálózati meghajtó alapértelmezés
DEFAULT_BASE_DIR = r"I:\thermostat"

OUTDOOR_BINS = [(-30, -10), (-10, 0), (0, 10), (10, 20), (20, 35), (35, 60)]

# I/O retry beállítások (Samba/Windows lockok ellen)
IO_RETRIES = 5
IO_DELAY_S = 0.2

# Log rotáció
MAX_LOG_BYTES = 2 * 1024 * 1024  # ~2 MB

# -------------------------------------------------------------------

def now_ts() -> float:
    return time.time()

def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

# ------------------------- Stabil I/O segédek -------------------------

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
        return float(s.strip().replace(",", "."))
    except Exception:
        return default

def safe_read_str(path: str, default: str) -> str:
    s = safe_read_text(path)
    if s is None or s == "":
        return default
    return s

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

def temp_to_bin_key(outdoor_temp: Optional[float]) -> str:
    if outdoor_temp is None:
        return "unknown"
    for lo, hi in OUTDOOR_BINS:
        if lo <= outdoor_temp < hi:
            return f"{lo}..{hi}"
    return "unknown"

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

# ------------------------- Logger -------------------------

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
            # végső fallback: stderr
            try:
                sys.stderr.write(line)
            except Exception:
                pass

# ------------------------- Szobavezérlő -------------------------

class RoomController:
    def __init__(self, base_dir: str, room_id: str, friendly_name: str, logger: EventLogger, config: Dict[str, Any]):
        self.base_dir = base_dir
        self.room_id = room_id
        self.friendly_name = friendly_name
        self.logger = logger
        self.config = config

        # Paths
        self.room_dir = os.path.join(base_dir, "data", "rooms", room_id)
        ensure_dir(self.room_dir)

        # Inputok
        self.p_temp = os.path.join(self.room_dir, "sensor_temperature.txt")
        self.p_hum  = os.path.join(self.room_dir, "sensor_humidity.txt")
        self.p_setp = os.path.join(self.room_dir, "setpoint.txt")
        self.p_hyst = os.path.join(self.room_dir, "hysteresis.txt")

        # Outputok
        self.p_cmd  = os.path.join(self.room_dir, "command_heat.txt")
        self.p_stat = os.path.join(self.room_dir, "status.json")

        # Tanulási LOG (szobánként)
        self.p_learn = os.path.join(self.room_dir, f"{self.room_id}-learn.txt")
        self.learn_logger = EventLogger(self.p_learn)

        self._ensure_default_files()

        # Runtime
        self.heating_cmd = 0
        self.last_cmd = 0
        self.last_switch_ts: float = 0.0
        self.last_temp: Optional[float] = None
        self.last_ts: Optional[float] = None

        # ON szegmens méréséhez
        self.on_start_temp: Optional[float] = None

        # Overshoot mérés OFF után
        self.pending_ov: Optional[Dict[str, Any]] = None

        # hiba státusz
        self.last_write_error: Optional[str] = None

    def _ensure_default_files(self):
        try:
            if safe_read_text(self.p_temp) is None:
                atomic_write_text(self.p_temp, f"{DEFAULT_SETPOINT:.2f}")
            if safe_read_text(self.p_hum) is None:
                atomic_write_text(self.p_hum, "50.0")
            if safe_read_text(self.p_setp) is None:
                atomic_write_text(self.p_setp, f"{DEFAULT_SETPOINT:.2f}")
            if safe_read_text(self.p_hyst) is None:
                atomic_write_text(self.p_hyst, f"{DEFAULT_HYSTERESIS:.2f}")
            if safe_read_text(self.p_cmd) is None:
                atomic_write_text(self.p_cmd, "0")
            if load_json(self.p_stat, None) is None:
                save_json_atomic(self.p_stat, {"init": True, "room_id": self.room_id, "name": self.friendly_name})
        except Exception as e:
            self.logger.log(f"{self.room_id}: default file ensure error: {e}")

    def _read_inputs(self) -> Tuple[float, float, float, float]:
        temp = safe_read_float(self.p_temp, DEFAULT_SETPOINT)
        hum = safe_read_float(self.p_hum, 50.0)
        setp = safe_read_float(self.p_setp, DEFAULT_SETPOINT)
        hyst = safe_read_float(self.p_hyst, DEFAULT_HYSTERESIS)
        return temp, hum, setp, max(0.0, hyst if hyst == hyst else DEFAULT_HYSTERESIS)

    def _write_outputs(self, status: Dict[str, Any]):
        try:
            atomic_write_text(self.p_cmd, "1" if self.heating_cmd == 1 else "0")
        except Exception as e:
            self.logger.log(f"{self.room_id}: write command_heat error: {e}")
        try:
            status_out = {
                "timestamp": iso_now(),
                "room_id": self.room_id,
                "name": self.friendly_name,
                "heating_command": int(self.heating_cmd),
                **status
            }
            save_json_atomic(self.p_stat, status_out)
            self.last_write_error = None
        except Exception as e:
            self.last_write_error = str(e)
            self.logger.log(f"{self.room_id}: write status.json error: {e}")

    # --------- Tanulási log segédek (csak írás, nincs beavatkozás) ---------

    def _log_on_event(self, setpoint: float, temp: float, outdoor: Optional[float], bin_key: str, hyst: float):
        self.learn_logger.log(
            f"{self.room_id} | ON | name='{self.friendly_name}' "
            f"setpoint={setpoint:.2f}C, start_temp={temp:.2f}C, hyst={hyst:.2f}C, "
            f"outdoor={'' if outdoor is None else f'{outdoor:.2f}C'}, bin={bin_key}"
        )

    def _log_off_event(self, setpoint: float, temp: float, outdoor: Optional[float], bin_key: str, on_dur_s: float):
        dT = None if self.on_start_temp is None else (temp - self.on_start_temp)
        slope = None
        if dT is not None and on_dur_s > 1.0:
            slope = dT / (on_dur_s / 60.0)
        dur_txt = f"{int(on_dur_s//60)}m{int(on_dur_s%60)}s"
        self.learn_logger.log(
            f"{self.room_id} | OFF | name='{self.friendly_name}' "
            f"setpoint={setpoint:.2f}C, temp_at_off={temp:.2f}C, on_duration={dur_txt}, "
            f"deltaT={'' if dT is None else f'{dT:.3f}C'}, slope_on={'' if slope is None else f'{slope:.4f}C/min'}, "
            f"outdoor={'' if outdoor is None else f'{outdoor:.2f}C'}, bin={bin_key}"
        )

    def _log_overshoot(self, setpoint_at_off: float, peak: float, elapsed_s: float, outdoor: Optional[float], bin_key: str):
        overshoot = max(0.0, peak - setpoint_at_off)
        dur_txt = f"{int(elapsed_s//60)}m{int(elapsed_s%60)}s"
        # Pár baráti javaslat a sor végén
        tip = []
        if overshoot > 0.5:
            tip.append("nagy túlfutás; érdemes kisebb min_on-t kipróbálni")
        elif overshoot < 0.1:
            tip.append("kicsi túlfutás; rendben")
        msg_tip = (" | tip: " + ", ".join(tip)) if tip else ""
        self.learn_logger.log(
            f"{self.room_id} | OVERSHOOT | setpoint_at_off={setpoint_at_off:.2f}C, "
            f"peak={peak:.2f}C, overshoot={overshoot:.3f}C, after={dur_txt}, "
            f"outdoor={'' if outdoor is None else f'{outdoor:.2f}C'}, bin={bin_key}{msg_tip}"
        )

    # ------------------------- Egy ciklus -------------------------

    def step(self, outdoor_temp: Optional[float], mode: str, min_on_s: float, min_off_s: float, loop_seconds: float, global_hyst: float):
        temp, hum, setpoint, room_hyst = self._read_inputs()
        hyst = max(room_hyst, global_hyst)

        ts = now_ts()
        dt_sec = (ts - self.last_ts) if self.last_ts is not None else loop_seconds
        dt_sec = max(0.001, dt_sec)
        bin_key = temp_to_bin_key(outdoor_temp)

        # Overshoot csúcsfigyelés OFF állapotban
        if self.heating_cmd == 0 and self.pending_ov:
            if temp > self.pending_ov["peak_temp"]:
                self.pending_ov["peak_temp"] = temp
            # ha csökkenni kezd vagy 15 perc eltelt -> lezárjuk és logoljuk
            elapsed = now_ts() - self.pending_ov["off_ts"]
            closing = False
            if self.last_temp is not None and temp <= self.last_temp - DEFAULT_EPS_TEMP:
                closing = True
            if elapsed >= 15 * 60:
                closing = True
            if closing:
                self._log_overshoot(
                    setpoint_at_off=self.pending_ov["setpoint_at_off"],
                    peak=self.pending_ov["peak_temp"],
                    elapsed_s=elapsed,
                    outdoor=outdoor_temp,
                    bin_key=bin_key
                )
                self.pending_ov = None

        prev_cmd = self.heating_cmd

        if mode.lower() == "off":
            # Globálisan tiltva
            self.heating_cmd = 0
        else:
            # Klasszikus hiszterézis + min on/off korlátok
            min_since_switch_s = (ts - self.last_switch_ts) if self.last_switch_ts > 0 else 1e9

            if self.heating_cmd == 1:
                # Csak akkor kapcsolunk ki, ha eléggé meleg és letelt a minimum ON
                if temp >= setpoint + hyst and min_since_switch_s >= min_on_s:
                    self.heating_cmd = 0
            else:
                # Csak akkor kapcsolunk be, ha eléggé hideg és letelt a minimum OFF
                if temp <= setpoint - hyst and min_since_switch_s >= min_off_s:
                    self.heating_cmd = 1

            # Biztonsági OFF: ha nagyon túllőne (pl. szenzor-hiba), azonnal OFF
            if temp >= setpoint + 3.0 * hyst:
                self.heating_cmd = 0

        # Állapotváltás kezelése + tanulási log
        if prev_cmd != self.heating_cmd:
            # váltás pillanata
            if self.heating_cmd == 1:
                # OFF -> ON
                self.last_switch_ts = ts
                self.on_start_temp = temp
                self.pending_ov = None  # új ON nullázza a korábbi overshoot mérést
                self.logger.log(f"{self.room_id}: SWITCH ON at {temp:.2f}°C (sp={setpoint:.2f}) bin={bin_key}")
                self._log_on_event(setpoint, temp, outdoor_temp, bin_key, hyst)
            else:
                # ON -> OFF
                on_dur_s = ts - self.last_switch_ts if self.last_switch_ts > 0 else 0.0
                self.last_switch_ts = ts
                self.pending_ov = {
                    "off_ts": ts,
                    "temp_at_off": temp,
                    "setpoint_at_off": setpoint,
                    "peak_temp": temp
                }
                self.logger.log(f"{self.room_id}: SWITCH OFF at {temp:.2f}°C (sp={setpoint:.2f}) bin={bin_key}")
                self._log_off_event(setpoint, temp, outdoor_temp, bin_key, on_dur_s)

        # Státusz írás
        status = {
            "temperature": round(temp, 3),
            "humidity": round(hum, 1),
            "setpoint": round(setpoint, 2),
            "hysteresis": round(hyst, 2),
            "outdoor_temp": None if outdoor_temp is None else round(outdoor_temp, 2),
            "outdoor_bin": bin_key,
            "ema_slope_on_c_per_min": None,     # kompat: most nem számolunk
            "ema_slope_off_c_per_min": None,    # kompat: most nem számolunk
            "predicted_overshoot": None,        # kompat: nincs beavatkozó öntanulás
            "last_switch_iso": datetime.fromtimestamp(self.last_switch_ts).isoformat(timespec="seconds") if self.last_switch_ts > 0 else None,
            "last_write_error": self.last_write_error
        }
        self._write_outputs(status)

        # Következő körhöz
        self.last_temp = temp
        self.last_ts = ts
        self.last_cmd = prev_cmd

# ------------------------- Fő app -------------------------

class ThermostatApp:
    def __init__(self, base_dir: str, num_rooms: int, loop_seconds: float):
        self.base_dir = base_dir
        self.num_rooms = num_rooms
        self.loop_seconds = loop_seconds

        # Paths
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
        self.p_outdoor_temp = os.path.join(self.external_dir, "outdoor_temperature.txt")
        self.p_mode = os.path.join(self.external_dir, "mode.txt")
        self.p_status_all = os.path.join(self.data_dir, "status_all.json")

        self.logger = EventLogger(os.path.join(self.logs_dir, "events.log"))

        # Konfig/failsafe
        self.controller_cfg = self._ensure_controller_cfg()
        self.rooms_map = self._ensure_rooms_map()

        # Szobák
        self.rooms: Dict[str, RoomController] = {}
        for idx in range(1, self.num_rooms + 1):
            rid = f"room{idx}"
            friendly = self._friendly_name_for(rid)
            self.rooms[rid] = RoomController(self.base_dir, rid, friendly, self.logger, self.controller_cfg)

        # Külső bemenetek failsafe
        if safe_read_text(self.p_outdoor_temp) is None:
            atomic_write_text(self.p_outdoor_temp, "10.0")
        if safe_read_text(self.p_mode) is None:
            atomic_write_text(self.p_mode, "auto")

        self.consecutive_errors = 0

    def _ensure_controller_cfg(self) -> Dict[str, Any]:
        cfg = load_json(self.p_controller_cfg, None)
        if cfg is None:
            cfg = {
                "num_rooms": self.num_rooms,
                "loop_seconds": self.loop_seconds,
                "global_hysteresis": DEFAULT_HYSTERESIS,
                "min_on_seconds": DEFAULT_MIN_ON_SECONDS,
                "min_off_seconds": DEFAULT_MIN_OFF_SECONDS,
                # a tau itt maradhat kompat. okból, de nincs használva
                "tau_minutes": 5.0
            }
            save_json_atomic(self.p_controller_cfg, cfg)
        cfg["num_rooms"] = self.num_rooms
        cfg["loop_seconds"] = self.loop_seconds
        return cfg

    def _ensure_rooms_map(self) -> Dict[str, Any]:
        m = load_json(self.p_rooms_map, None)
        if m is None:
            rooms = []
            for idx in range(1, self.num_rooms + 1):
                rooms.append({"id": f"room{idx}", "name": f"Szoba {idx}", "enabled": True})
            m = {"rooms": rooms}
            save_json_atomic(self.p_rooms_map, m)
        else:
            current_ids = {r["id"] for r in m.get("rooms", [])}
            changed = False
            for idx in range(1, self.num_rooms + 1):
                rid = f"room{idx}"
                if rid not in current_ids:
                    m["rooms"].append({"id": rid, "name": f"Szoba {idx}", "enabled": True})
                    changed = True
            if changed:
                save_json_atomic(self.p_rooms_map, m)
        return m

    def _friendly_name_for(self, room_id: str) -> str:
        for r in self.rooms_map.get("rooms", []):
            if r.get("id") == room_id:
                return r.get("name", room_id)
        return room_id

    def _reload_room_names_if_changed(self):
        m = load_json(self.p_rooms_map, None)
        if not m:
            return
        for r in m.get("rooms", []):
            rid = r.get("id")
            if rid in self.rooms:
                self.rooms[rid].friendly_name = r.get("name", rid)

    def _read_outdoor_temp(self) -> Optional[float]:
        s = safe_read_text(self.p_outdoor_temp)
        if s is None or s.strip() == "":
            return None
        try:
            return float(s.replace(",", "."))
        except Exception:
            return None

    def _read_mode(self) -> str:
        mode = safe_read_str(self.p_mode, "auto").lower()
        return "off" if mode not in ("auto", "heat", "on") else "auto"

    def _write_all_status(self, outdoor_temp: Optional[float], mode: str):
        out = {
            "timestamp": iso_now(),
            "mode": mode,
            "outdoor_temp": None if outdoor_temp is None else round(outdoor_temp, 2),
            "rooms": []
        }
        for rid, rc in self.rooms.items():
            s = load_json(rc.p_stat, {})
            out["rooms"].append(s)
        try:
            save_json_atomic(self.p_status_all, out)
        except Exception as e:
            self.logger.log(f"write status_all.json error: {e}")

    def run(self):
        loop_s = float(self.controller_cfg.get("loop_seconds", DEFAULT_LOOP_SECONDS))
        global_hyst = float(self.controller_cfg.get("global_hysteresis", DEFAULT_HYSTERESIS))
        min_on_s = float(self.controller_cfg.get("min_on_seconds", DEFAULT_MIN_ON_SECONDS))
        min_off_s = float(self.controller_cfg.get("min_off_seconds", DEFAULT_MIN_OFF_SECONDS))

        self.logger.log(f"Controller started: base_dir='{self.base_dir}', rooms={self.num_rooms}, loop={loop_s}s, hyst={global_hyst}")
        try:
            while True:
                try:
                    self._reload_room_names_if_changed()

                    outdoor = self._read_outdoor_temp()
                    mode = self._read_mode()

                    for rid, rc in self.rooms.items():
                        try:
                            # enabled flag ellenőrzése
                            enabled = True
                            for r in self.rooms_map.get("rooms", []):
                                if r.get("id") == rid:
                                    enabled = bool(r.get("enabled", True))
                                    break
                            if not enabled:
                                prev_cmd = rc.heating_cmd
                                rc.heating_cmd = 0
                                if prev_cmd != 0:
                                    rc.last_switch_ts = now_ts()
                                    rc.pending_ov = None
                                    self.logger.log(f"{rid}: DISABLED -> FORCE OFF")
                                rc._write_outputs({
                                    "disabled": True,
                                    "note": "Room disabled via rooms.json"
                                })
                                continue

                            rc.step(outdoor, mode, min_on_s, min_off_s, loop_s, global_hyst)
                        except Exception as e_room:
                            self.logger.log(f"{rid}: step error: {e_room}")

                    self._write_all_status(outdoor, mode)

                    self.consecutive_errors = 0
                except Exception as e_loop:
                    self.consecutive_errors += 1
                    self.logger.log(f"LOOP ERROR ({self.consecutive_errors}): {e_loop}")
                    time.sleep(min(loop_s * (1 + self.consecutive_errors*0.5), 10.0))
                finally:
                    time.sleep(loop_s)
        except KeyboardInterrupt:
            self.logger.log("Controller stopped by user (KeyboardInterrupt)")

# ------------------------- CLI -------------------------

def parse_args():
    ap = argparse.ArgumentParser(description="Zónás, textfájl-alapú termosztát (hiszterézis + min on/off). Öntanulás: csak log.")
    ap.add_argument("--rooms", type=int, default=DEFAULT_NUM_ROOMS, help="Szobák száma (room1..roomN)")
    ap.add_argument("--loop-seconds", type=float, default=DEFAULT_LOOP_SECONDS, help="Vezérlési ciklus másodpercben")
    ap.add_argument("--base-dir", type=str, default=DEFAULT_BASE_DIR, help="Projekt gyökér mappa (pl. hálózati meghajtó)")
    return ap.parse_args()

def main():
    args = parse_args()
    app = ThermostatApp(base_dir=args.base_dir, num_rooms=args.rooms, loop_seconds=args.loop_seconds)
    app.run()

if __name__ == "__main__":
    main()
