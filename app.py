# thermostat_controller.py
# Windows-kompatibilis, zónás, textfájl-alapú, öntanuló termosztát váz Home Assistant / Node-RED integrációhoz.
# - Mappák és fájlok automatikus létrehozása (failsafe)
# - Hiszterézises vezérlés + overshoot (túlugrás) előrejelzés és tanulás kültéri hőmérséklet-sávok szerint
# - Stabil, atomi fájlírások; JSON-konfigok és per-szoba státusz
# - CLI: --rooms N, --loop-seconds S, --base-dir PATH

import os
import sys
import json
import time
import argparse
import threading
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

# ------------------------- Alapbeállítások (módosítható) -------------------------

DEFAULT_NUM_ROOMS = 5
DEFAULT_LOOP_SECONDS = 2.0
DEFAULT_SETPOINT = 21.0
DEFAULT_HYSTERESIS = 0.3        # °C
DEFAULT_MIN_ON_SECONDS = 120     # min. bekapcsolva tartási idő (rövid ciklusok ellen)
DEFAULT_MIN_OFF_SECONDS = 120    # min. kikapcsolva tartási idő
DEFAULT_TAU_MIN = 5.0            # előrejelzéshez használt "lag" percben
DEFAULT_SLOPE_EMA_WINDOW_S = 300 # EMA ablak a meredekséghez (~5 perc)
DEFAULT_OVERSHOOT_EMA_ALPHA = 0.2
DEFAULT_EPS_TEMP = 0.01

OUTDOOR_BINS = [(-30, -10), (-10, 0), (0, 10), (10, 20), (20, 35), (35, 60)]
# -------------------------------------------------------------------------------

def now_ts() -> float:
    return time.time()

def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def safe_read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
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
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_atomic(path: str, obj: Any):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def temp_to_bin_key(outdoor_temp: Optional[float]) -> str:
    if outdoor_temp is None:
        return "unknown"
    for lo, hi in OUTDOOR_BINS:
        if lo <= outdoor_temp < hi:
            return f"{lo}..{hi}"
    return "unknown"

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

class EventLogger:
    def __init__(self, log_path: str):
        self.log_path = log_path
        ensure_dir(os.path.dirname(log_path))
        self.lock = threading.Lock()

    def log(self, msg: str):
        line = f"{iso_now()} | {msg}\n"
        try:
            with self.lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass

class RoomController:
    def __init__(self, base_dir: str, room_id: str, friendly_name: str, logger: EventLogger, learning_store: Dict[str, Any], config: Dict[str, Any]):
        self.base_dir = base_dir
        self.room_id = room_id
        self.friendly_name = friendly_name
        self.logger = logger
        self.learning_store = learning_store  # shared dict, will be saved by main loop
        self.config = config

        # Paths
        self.room_dir = os.path.join(base_dir, "data", "rooms", room_id)
        ensure_dir(self.room_dir)

        # Input files (Node-RED/HA tölti):
        self.p_temp = os.path.join(self.room_dir, "sensor_temperature.txt")
        self.p_hum  = os.path.join(self.room_dir, "sensor_humidity.txt")
        self.p_setp = os.path.join(self.room_dir, "setpoint.txt")
        self.p_hyst = os.path.join(self.room_dir, "hysteresis.txt")

        # Output files (mi írjuk):
        self.p_cmd  = os.path.join(self.room_dir, "command_heat.txt")
        self.p_stat = os.path.join(self.room_dir, "status.json")

        # Failsafe default file generation
        self._ensure_default_files()

        # Runtime state
        self.heating_cmd = 0  # 0/1 aktuális parancs (amit mi írunk)
        self.last_cmd = 0
        self.last_switch_ts: float = 0.0
        self.last_temp: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.ema_slope_on: Optional[float] = None   # °C/min
        self.ema_slope_off: Optional[float] = None  # °C/min

        # Overshoot mérés 1->0 váltások után
        self.pending_ov: Optional[Dict[str, Any]] = None  # {'off_ts', 'temp_at_off', 'setpoint_at_off', 'peak_temp'}

    def _ensure_default_files(self):
        if safe_read_text(self.p_temp) is None:
            atomic_write_text(self.p_temp, f"{DEFAULT_SETPOINT:.2f}")  # kezdő temp = 21.0
        if safe_read_text(self.p_hum) is None:
            atomic_write_text(self.p_hum, "50.0")
        if safe_read_text(self.p_setp) is None:
            atomic_write_text(self.p_setp, f"{DEFAULT_SETPOINT:.2f}")
        if safe_read_text(self.p_hyst) is None:
            atomic_write_text(self.p_hyst, f"{DEFAULT_HYSTERESIS:.2f}")
        if safe_read_text(self.p_cmd) is None:
            atomic_write_text(self.p_cmd, "0")
        if safe_read_text(self.p_stat) is None:
            save_json_atomic(self.p_stat, {"init": True, "room_id": self.room_id, "name": self.friendly_name})

    def _learning_node(self) -> Dict[str, Any]:
        # learning_store struktúra: {'rooms': {room_id: {'bins': {key: {'overshoot': val, 'count': n}}, 'ema_slope_on': x, 'ema_slope_off': y}}}
        rooms = self.learning_store.setdefault("rooms", {})
        node = rooms.setdefault(self.room_id, {})
        node.setdefault("bins", {})
        if "ema_slope_on" not in node:
            node["ema_slope_on"] = None
        if "ema_slope_off" not in node:
            node["ema_slope_off"] = None
        return node

    def _update_ema(self, current: Optional[float], new_val: float, dt_sec: float, window_s: float) -> float:
        # Időalapú EMA: alpha = dt / (window + dt)
        alpha = dt_sec / (window_s + dt_sec)
        if current is None:
            return new_val
        return (1 - alpha) * current + alpha * new_val

    def _read_inputs(self) -> Tuple[float, float, float]:
        temp = safe_read_float(self.p_temp, DEFAULT_SETPOINT)
        hum = safe_read_float(self.p_hum, 50.0)
        setp = safe_read_float(self.p_setp, DEFAULT_SETPOINT)
        hyst = safe_read_float(self.p_hyst, DEFAULT_HYSTERESIS)
        # Hyst per file, de a hívóban elérhető a globális is, itt visszaadjuk külön:
        return temp, hum, max(0.0, hyst if hyst == hyst else DEFAULT_HYSTERESIS)

    def _write_outputs(self, status: Dict[str, Any]):
        # command
        atomic_write_text(self.p_cmd, "1" if self.heating_cmd == 1 else "0")
        # status
        status_out = {
            "timestamp": iso_now(),
            "room_id": self.room_id,
            "name": self.friendly_name,
            "heating_command": int(self.heating_cmd),
            **status
        }
        save_json_atomic(self.p_stat, status_out)

    def _overshoot_bin(self, key: str) -> Dict[str, Any]:
        node = self._learning_node()
        bins = node["bins"]
        b = bins.setdefault(key, {})
        if "overshoot" not in b:
            b["overshoot"] = 0.0
            b["count"] = 0
        return b

    def _update_overshoot_model(self, bin_key: str, observed_overshoot: float):
        b = self._overshoot_bin(bin_key)
        # EMA-s jellegű frissítés
        if b["count"] == 0:
            b["overshoot"] = max(0.0, observed_overshoot)
        else:
            b["overshoot"] = (1 - DEFAULT_OVERSHOOT_EMA_ALPHA) * b["overshoot"] + DEFAULT_OVERSHOOT_EMA_ALPHA * max(0.0, observed_overshoot)
        b["count"] += 1

    def _predicted_overshoot(self, bin_key: str, tau_min: float) -> float:
        node = self._learning_node()
        b = self._overshoot_bin(bin_key)
        learned = b.get("overshoot", 0.0)
        ema_on = node.get("ema_slope_on", None)
        # fallback komponens: aktuális melegedési meredekség * tau
        fallback = 0.0
        if ema_on is not None and ema_on > 0:
            fallback = ema_on * tau_min
        # óvatos kombináció: a nagyobbikat vesszük (konzervatív, hogy inkább előbb kapcsoljunk ki)
        return max(learned, fallback)

    def _finalize_pending_overshoot_if_ready(self, current_temp: float, setpoint: float, dt_sec: float, outdoor_bin_key: str):
        if not self.pending_ov:
            return
        # Feltétel: hőmérséklet csúcs már megvolt (t <= last_temp) vagy eltelt 15 perc, vagy újra bekapcsoltunk
        # Ezt a függvényt csak OFF állapotban hívjuk (ON-ban reseteli az új ON), így itt csúcsfigyelést végzünk
        elapsed = now_ts() - self.pending_ov["off_ts"]
        peak = self.pending_ov["peak_temp"]
        # Ha csökkenni kezd, vagy lejár 15 perc, lezárjuk
        closing = False
        if self.last_temp is not None and current_temp <= self.last_temp - DEFAULT_EPS_TEMP:
            closing = True
        if elapsed >= 15 * 60:
            closing = True
        if closing:
            overshoot_vs_setp = max(0.0, peak - self.pending_ov["setpoint_at_off"])
            self._update_overshoot_model(outdoor_bin_key, overshoot_vs_setp)
            self.logger.log(f"{self.room_id}: Overshoot measured {overshoot_vs_setp:.3f} °C in bin {outdoor_bin_key}")
            self.pending_ov = None

    def step(self, outdoor_temp: Optional[float], mode: str, tau_min: float, min_on_s: float, min_off_s: float, loop_seconds: float, global_hyst: float):
        """
        Egy vezérlési ciklus végrehajtása.
        """
        temp, hum, room_hyst = self._read_inputs()
        hyst = max(room_hyst, global_hyst)  # biztos ami biztos

        # Idők, deriváltak
        ts = now_ts()
        dt_sec = (ts - self.last_ts) if self.last_ts is not None else loop_seconds
        dt_sec = max(0.001, dt_sec)
        slope_per_min = None
        if self.last_temp is not None:
            slope_per_min = (temp - self.last_temp) / (dt_sec / 60.0)

        # EMA frissítések
        node = self._learning_node()
        if slope_per_min is not None:
            if self.heating_cmd == 1:
                self.ema_slope_on = self._update_ema(self.ema_slope_on, slope_per_min, dt_sec, DEFAULT_SLOPE_EMA_WINDOW_S)
                node["ema_slope_on"] = self.ema_slope_on
            else:
                self.ema_slope_off = self._update_ema(self.ema_slope_off, slope_per_min, dt_sec, DEFAULT_SLOPE_EMA_WINDOW_S)
                node["ema_slope_off"] = self.ema_slope_off

        # Overshoot mérés kezelése OFF-ban
        bin_key = temp_to_bin_key(outdoor_temp)
        if self.heating_cmd == 0:
            if self.pending_ov:
                # Frissítjük a lokális csúcsot
                if temp > self.pending_ov["peak_temp"]:
                    self.pending_ov["peak_temp"] = temp
                self._finalize_pending_overshoot_if_ready(temp, DEFAULT_SETPOINT, dt_sec, bin_key)

        # Vezérlési logika
        prev_cmd = self.heating_cmd
        setpoint = safe_read_float(self.p_setp, DEFAULT_SETPOINT)

        # Globális mód
        if mode.lower() == "off":
            self.heating_cmd = 0
        else:
            # Anticipatív OFF: ha bekapcsolva vagyunk, előre kikapcsolunk, hogy a csúcs pont a setpoint körül legyen
            min_since_switch = (ts - self.last_switch_ts) / 60.0 if self.last_switch_ts > 0 else 1e9
            # Min időzár figyelése:
            if self.heating_cmd == 1:
                # Overshoot előrejelzés
                pred_ov = self._predicted_overshoot(bin_key, tau_min)
                off_trigger_temp = setpoint - pred_ov
                # Hysterézissel óvatosan: ne kapcsolgasson gyorsan, ezért az off_trigger_temp-et ne húzzuk le nagyon a setpont alá
                off_trigger_temp = min(off_trigger_temp, setpoint)  # sose legyen a setpont felett
                # Minimális ON időkorlát:
                if temp >= off_trigger_temp and min_since_switch * 60.0 >= min_on_s:
                    self.heating_cmd = 0
            else:
                # Bekapcsolási feltétel (klasszikus hiszterézis)
                if temp <= setpoint - hyst and min_since_switch * 60.0 >= min_off_s:
                    self.heating_cmd = 1

            # Biztonsági OFF, ha túlmelegedne:
            if temp >= setpoint + hyst:
                self.heating_cmd = 0

        # Állapotváltás detektálás
        if prev_cmd != self.heating_cmd:
            self.last_switch_ts = ts
            if prev_cmd == 1 and self.heating_cmd == 0:
                # OFF-ba váltottunk -> indul overshoot mérés
                self.pending_ov = {
                    "off_ts": ts,
                    "temp_at_off": temp,
                    "setpoint_at_off": setpoint,
                    "peak_temp": temp,
                }
                self.logger.log(f"{self.room_id}: SWITCH OFF at {temp:.2f}°C (sp={setpoint:.2f}) bin={bin_key}")
            elif prev_cmd == 0 and self.heating_cmd == 1:
                # ON-ba váltottunk -> korábbi mérés érvénytelen
                self.pending_ov = None
                self.logger.log(f"{self.room_id}: SWITCH ON at {temp:.2f}°C (sp={setpoint:.2f}) bin={bin_key}")

        # Státusz kiírás
        status = {
            "temperature": round(temp, 3),
            "humidity": round(hum, 1),
            "setpoint": round(setpoint, 2),
            "hysteresis": round(hyst, 2),
            "outdoor_temp": None if outdoor_temp is None else round(outdoor_temp, 2),
            "outdoor_bin": bin_key,
            "ema_slope_on_c_per_min": None if self.ema_slope_on is None else round(self.ema_slope_on, 4),
            "ema_slope_off_c_per_min": None if self.ema_slope_off is None else round(self.ema_slope_off, 4),
            "predicted_overshoot": round(self._predicted_overshoot(bin_key, tau_min), 3),
            "last_switch_iso": datetime.fromtimestamp(self.last_switch_ts).isoformat(timespec="seconds") if self.last_switch_ts > 0 else None,
        }
        self._write_outputs(status)

        # Következő körhöz
        self.last_temp = temp
        self.last_ts = ts
        self.last_cmd = prev_cmd

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
        self.p_learning = os.path.join(self.config_dir, "learning.json")
        self.p_outdoor_temp = os.path.join(self.external_dir, "outdoor_temperature.txt")
        self.p_mode = os.path.join(self.external_dir, "mode.txt")
        self.p_status_all = os.path.join(self.data_dir, "status_all.json")

        self.logger = EventLogger(os.path.join(self.logs_dir, "events.log"))

        # Konfig/failsafe
        self.controller_cfg = self._ensure_controller_cfg()
        self.rooms_map = self._ensure_rooms_map()
        self.learning = load_json(self.p_learning, {"rooms": {}})

        # Szobák példányosítása
        self.rooms: Dict[str, RoomController] = {}
        for idx in range(1, self.num_rooms + 1):
            rid = f"room{idx}"
            friendly = self._friendly_name_for(rid)
            self.rooms[rid] = RoomController(self.base_dir, rid, friendly, self.logger, self.learning, self.controller_cfg)

        # Külső bemenetek failsafe
        if safe_read_text(self.p_outdoor_temp) is None:
            atomic_write_text(self.p_outdoor_temp, "10.0")
        if safe_read_text(self.p_mode) is None:
            atomic_write_text(self.p_mode, "auto")

    def _ensure_controller_cfg(self) -> Dict[str, Any]:
        cfg = load_json(self.p_controller_cfg, None)
        if cfg is None:
            cfg = {
                "num_rooms": self.num_rooms,
                "loop_seconds": self.loop_seconds,
                "global_hysteresis": DEFAULT_HYSTERESIS,
                "min_on_seconds": DEFAULT_MIN_ON_SECONDS,
                "min_off_seconds": DEFAULT_MIN_OFF_SECONDS,
                "tau_minutes": DEFAULT_TAU_MIN
            }
            save_json_atomic(self.p_controller_cfg, cfg)
        # CLI felülírhatja a számot és a loop időt
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
            # egészítsük ki, ha kevesebb szoba van benne
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
        # Lehetővé teszi a GUI/Node-RED átnevezést újraindítás nélkül (rooms.json módosításakor)
        m = load_json(self.p_rooms_map, None)
        if not m:
            return
        # Ha változott, frissítjük a példányok friendly_name mezőjét
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
        save_json_atomic(self.p_status_all, out)

    def run(self):
        loop_s = float(self.controller_cfg.get("loop_seconds", DEFAULT_LOOP_SECONDS))
        global_hyst = float(self.controller_cfg.get("global_hysteresis", DEFAULT_HYSTERESIS))
        min_on_s = float(self.controller_cfg.get("min_on_seconds", DEFAULT_MIN_ON_SECONDS))
        min_off_s = float(self.controller_cfg.get("min_off_seconds", DEFAULT_MIN_OFF_SECONDS))
        tau_min = float(self.controller_cfg.get("tau_minutes", DEFAULT_TAU_MIN))

        self.logger.log(f"Controller started: rooms={self.num_rooms}, loop={loop_s}s, hyst={global_hyst}, tau={tau_min}min")
        try:
            while True:
                self._reload_room_names_if_changed()

                outdoor = self._read_outdoor_temp()
                mode = self._read_mode()

                for rid, rc in self.rooms.items():
                    # Engedélyezés ellenőrzés (ha valaki kikapcsolt egy szobát a rooms.json-ban)
                    enabled = True
                    for r in self.rooms_map.get("rooms", []):
                        if r.get("id") == rid:
                            enabled = bool(r.get("enabled", True))
                            break
                    if not enabled:
                        # Szoba tiltva -> parancs 0 és státusz kiírás
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

                    rc.step(outdoor, mode, tau_min, min_on_s, min_off_s, loop_s, global_hyst)

                # learning mentése és összesített státusz
                save_json_atomic(self.p_learning, self.learning)
                self._write_all_status(outdoor, mode)

                time.sleep(loop_s)
        except KeyboardInterrupt:
            self.logger.log("Controller stopped by user (KeyboardInterrupt)")
            save_json_atomic(self.p_learning, self.learning)
        except Exception as e:
            self.logger.log(f"FATAL ERROR: {e}")
            save_json_atomic(self.p_learning, self.learning)
            raise

def parse_args():
    ap = argparse.ArgumentParser(description="Zónás, textfájl-alapú, öntanuló termosztátváz (Home Assistant / Node-RED)")
    ap.add_argument("--rooms", type=int, default=DEFAULT_NUM_ROOMS, help="Szobák száma (room1..roomN)")
    ap.add_argument("--loop-seconds", type=float, default=DEFAULT_LOOP_SECONDS, help="Vezérlési ciklus másodpercben")
    ap.add_argument("--base-dir", type=str, default="I:\\thermostat", help="Projekt gyökér mappa (alapértelmezés: aktuális könyvtár)")
    return ap.parse_args()

def main():
    args = parse_args()
    app = ThermostatApp(base_dir=args.base_dir, num_rooms=args.rooms, loop_seconds=args.loop_seconds)
    app.run()

if __name__ == "__main__":
    main()
