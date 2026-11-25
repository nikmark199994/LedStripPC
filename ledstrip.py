#!/usr/bin/env python3
"""Лента — управление BLEDOM RGB-лентой с Linux. Retro-futurism edition."""
import asyncio, json, math, random, re, struct, subprocess, sys, threading, time, traceback
from datetime import datetime
from pathlib import Path
from bleak import BleakClient, BleakScanner
from PySide6 import QtCore, QtGui, QtWidgets

CFG = Path.home() / ".config" / "ledstrip" / "config.json"
DEFAULT = dict(
    h=340, s=1.0, v=1.0, speed=.45, mode="static", cols=["#ff0055", "#ffaa00", "#00ffd5"], addr=None,
    gain=.6, rate=20, sched=dict(on=False, t_on="18:00", t_off="23:30", days=[0, 1, 2, 3, 4, 5, 6]),
    notif=True, notif_off=True, notif_kde=False, notif_all=True, dflt="#ff2bd6",
    rules=[{"m": "telegram", "c": "#2aabee"}, {"m": "whatsapp", "c": "#25d366"}, {"m": "viber", "c": "#7360f2"},
           {"m": "call|звон|вызов", "c": "#ff2020"}, {"m": "mail|gmail|почт", "c": "#ffc83d"}])
MODES = [("static", "Статика"), ("breath", "Дыхание"), ("heart", "Пульс"), ("rainbow", "Радуга"), ("fire", "Огонь"),
         ("aurora", "Сияние"), ("candle", "Свеча"), ("sunset", "Закат"), ("storm", "Гроза"), ("meteor", "Метеор"),
         ("police", "Мигалка"), ("custom", "Свой"), ("music", "Музыка")]
HUE_MODES = {"static", "breath", "heart", "meteor"}
ON = bytes([0x7e, 0, 4, 0xf0, 0, 1, 0xff, 0, 0xef])
OFF = bytes([0x7e, 0, 4, 0, 0, 0, 0xff, 0, 0xef])
POL = "R.R.R...B.B.B..."


def load():
    c = dict(DEFAULT)
    try: c.update(json.loads(CFG.read_text()))
    except Exception: pass
    # Старые/повреждённые настройки не должны ломать математические операции.
    for key, fallback in (("h", 340), ("s", 1.0), ("v", 1.0),
                          ("speed", .45), ("gain", .6), ("rate", 20)):
        value = c.get(key)
        try:
            c[key] = float(value) if value is not None else fallback
        except (TypeError, ValueError):
            c[key] = fallback
    c["rate"] = max(5, min(60, int(c["rate"])))
    return c


def save(c):
    try:
        CFG.parent.mkdir(parents=True, exist_ok=True)
        CFG.write_text(json.dumps(c, ensure_ascii=False, indent=1))
    except Exception: pass


def hex2rgb(x): return tuple(int(x[i:i + 2], 16) / 255 for i in (1, 3, 5))
SUN = [hex2rgb(x) for x in ("#ffd27a", "#ff8a3d", "#e8345a", "#8a2f9e", "#2b2f8f")]


def hsv(h, s, v):
    hh = (h % 360) / 60; i = int(hh); f = hh - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i % 6]


def grad(cs, p):
    n = len(cs); x = (p % 1) * n; i = int(x); f = x - i; f = f * f * (3 - 2 * f)
    a, b = cs[i % n], cs[(i + 1) % n]
    return tuple(a[k] + (b[k] - a[k]) * f for k in range(3))


def _mins(t): return int(t[:2]) * 60 + int(t[3:5])


def sched_inside(c):
    """Должна ли лента быть включена сейчас по расписанию."""
    sc = c["sched"]; d = datetime.now(); cur = d.hour * 60 + d.minute; wd = d.weekday()
    on, off = _mins(sc["t_on"]), _mins(sc["t_off"])
    if on <= off: inside, start = on <= cur < off, wd
    elif cur >= on: inside, start = True, wd
    elif cur < off: inside, start = True, (wd - 1) % 7
    else: inside, start = False, wd
    return inside and start in sc["days"]


class AudioIn:
    """Захват системного звука через PulseAudio/PipeWire и расчёт спектра по 8 полосам."""
    BANDS = [(30, 60), (60, 120), (120, 250), (250, 500), (500, 1000),
             (1000, 2000), (2000, 4000), (4000, 8000)]

    @staticmethod
    def _monitor_source():
        """Находит monitor-источник именно текущего устройства вывода."""
        try:
            sink = subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True,
                text=True, timeout=3, check=True
            ).stdout.strip()
            sources = subprocess.run(
                ["pactl", "list", "short", "sources"], capture_output=True,
                text=True, timeout=3, check=True
            ).stdout.splitlines()
            parsed = []
            for line in sources:
                fields = line.split()
                if len(fields) >= 2:
                    parsed.append(fields[1])
            preferred = f"{sink}.monitor" if sink else None
            if preferred and preferred in parsed:
                return preferred
            monitors = [name for name in parsed if name.endswith(".monitor")]
            if monitors:
                return monitors[0]
        except (OSError, subprocess.SubprocessError):
            pass
        # PulseAudio/PipeWire также поддерживает это специальное имя.
        return "@DEFAULT_MONITOR@"

    def __init__(self):
        import numpy as np
        self.np = np
        self.rms = 0.0
        self.error = None
        self.running = True
        self.p = None
        self.source = self._monitor_source()

        fr = np.fft.rfftfreq(1024, 1 / 22050)
        self.idx = [(fr >= a) & (fr < b) for a, b in self.BANDS]
        self.win = np.hanning(1024).astype(np.float32)
        self.buf = np.zeros(1024, dtype=np.float32)
        self.bands = np.zeros(len(self.BANDS), dtype=np.float32)

        command = [
            "parec", "--device", self.source, "--raw",
            "--format=s16le", "--rate=22050", "--channels=1",
            "--latency-msec=50"
        ]
        try:
            self.p = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0
            )
        except FileNotFoundError as exc:
            raise RuntimeError("не найден parec: установи пакет pulseaudio-utils") from exc

        # Даём parec короткое время сообщить об ошибке запуска/источника.
        time.sleep(0.15)
        if self.p.poll() is not None:
            detail = self.p.stderr.read().decode("utf-8", "replace").strip()
            raise RuntimeError(detail or f"не удалось открыть аудиоисточник {self.source}")

        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def feed(self, raw):
        np = self.np
        # Читаем поток как есть: размер блока pipe не обязан совпадать с FFT-окном.
        if len(raw) < 2:
            return
        if len(raw) % 2:
            raw = raw[:-1]
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if not x.size:
            return
        self.buf = np.concatenate((self.buf, x))[-1024:]
        self.rms = float(np.sqrt(np.mean(x * x)))
        if self.buf.size < 1024:
            return
        spectrum = np.abs(np.fft.rfft(self.buf * self.win)) / 512.0
        self.bands = np.array(
            [spectrum[mask].mean() if mask.any() else 0.0 for mask in self.idx],
            dtype=np.float32
        )

    def _read_loop(self):
        try:
            while self.running and self.p and self.p.poll() is None:
                block = self.p.stdout.read(2048)
                if not block:
                    break
                self.feed(block)
            if self.running and self.p and self.p.poll() not in (None, 0):
                detail = self.p.stderr.read().decode("utf-8", "replace").strip()
                self.error = detail or "захват системного звука завершился с ошибкой"
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self):
        self.running = False
        if self.p and self.p.poll() is None:
            try:
                self.p.terminate()
                self.p.wait(timeout=1)
            except Exception:
                try:
                    self.p.kill()
                except Exception:
                    pass


class Bridge(QtCore.QObject):
    devices = QtCore.Signal(list)


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg; self.on = True; self.live = (26, 18, 38)
        self.client = None; self.char = None; self.want = bool(cfg.get("addr")); self.connecting = False
        self.retry_at = 0; self.statustext = "Не подключено"; self.sleep = None; self.flash = None
        self.audio = None; self.last_notif = "—"; self.lastkey = None; self.lastsend = 0
        self.t0 = time.time(); self.fl = self.cf = .5; self.ls = self.mt = 0.0
        self.env = self.avg = self.mh = 0.0; self.peak = .02; self.last_beat = 0
        self.prev = 0; self.dt = .05; self.hue = 0.0; self.act = 0.0; self.pk = self.lo = self.ref = self.tf = 0.0; self.err = None; self.flash = 0.0; self.drift = self.tdrift = 0.0; self.pb = None; self.bavg = None; self.sched_min = None
        if cfg["sched"]["on"]: self.on = sched_inside(cfg)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    # ---- служебное ----
    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._main()); self.loop.run_forever()

    def submit(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop)
    def say(self, t): self.statustext = t

    @property
    def connected(self): return self.client is not None and self.char is not None and self.client.is_connected

    async def _cmd(self, b):
        try: await self.client.write_gatt_char(self.char, b, response=False)
        except Exception: pass

    async def scan(self):
        devs = await BleakScanner.discover(timeout=6)
        l = sorted({(d.name, d.address) for d in devs if d.name})
        return sorted(l, key=lambda x: 0 if re.search("bledom|elk", x[0], re.I) else 1)

    def _lost(self, _c):
        self.char = None; self.retry_at = time.time() + 2
        self.say("Связь потеряна, переподключаюсь…" if self.want else "Отключено")

    async def _connect(self, addr):
        self.connecting = True; self.say("Подключаюсь…")
        try:
            if self.client:
                try: await self.client.disconnect()
                except Exception: pass
            dev = await BleakScanner.find_device_by_address(addr, timeout=8)
            if dev is None: raise RuntimeError("лента не найдена рядом (закрой приложение на телефоне)")
            cl = BleakClient(dev, disconnected_callback=self._lost)
            await cl.connect()
            ch = None
            for u in ("0000fff3-0000-1000-8000-00805f9b34fb", "0000ffe9-0000-1000-8000-00805f9b34fb"):
                ch = cl.services.get_characteristic(u)
                if ch: break
            if not ch:
                await cl.disconnect(); raise RuntimeError("не нашёл нужную характеристику")
            self.client, self.char, self.lastkey = cl, ch, None
            await self._cmd(ON if self.on else OFF)
            self.say("Подключено")
        except Exception as e:
            self.say(f"Не вышло: {e}"); self.retry_at = time.time() + 4
        finally: self.connecting = False

    def connect(self, addr): self.want = True; self.submit(self._connect(addr))

    def disconnect(self):
        self.want = False; self.say("Отключено")
        if self.client: self.submit(self.client.disconnect())

    def set_power(self, on):
        self.on = on; self.lastkey = None
        if self.connected: self.submit(self._cmd(ON if on else OFF))

    def set_mode(self, m):
        if m == "music" and not self.audio:
            try: self.audio = AudioIn(); self.bavg = None
            except Exception as e:
                self.say(f"Музыка недоступна: {e}")
                m = "static"
        elif m != "music" and self.audio: self.audio.stop(); self.audio = None
        self.cfg["mode"] = m; self.lastkey = None; save(self.cfg)

    def start_sleep(self, minutes):
        if not self.on: self.set_power(True)
        self.sleep = (time.time(), time.time() + minutes * 60) if minutes else None

    def flash_color(self, hexcol):
        if not self.on and not self.cfg["notif_off"]: return
        self.flash = dict(c=hex2rgb(hexcol), t=time.time(), dur=2.0, was_off=not self.on, sent=False)

    def shutdown(self):
        if self.audio: self.audio.stop()

    # ---- эффекты ----
    def frame(self, t, now):
        c = self.cfg; h, s, v = c["h"], c["s"], c["v"]; spd = c["speed"]; sp = .2 + spd * 2.8; m = c["mode"]
        if m == "static": return hsv(h, s, v)
        if m == "breath":
            k = (1 - math.cos(t * sp * 1.4)) / 2; return hsv(h, s, v * (.04 + .96 * k * k * (3 - 2 * k)))
        if m == "heart":
            p = (t * sp * .5) % 1; b = math.exp(-(p * 9) ** 2) + .7 * math.exp(-((p - .28) * 9) ** 2)
            return hsv(h, s, v * (.05 + .95 * min(1, b)))
        if m == "rainbow": return hsv(t * (20 + spd * 160), s, v)
        if m == "fire":
            self.fl += (random.random() - self.fl) * (.12 + spd * .3); return hsv(4 + self.fl * 30, 1, v * (.3 + .7 * self.fl))
        if m == "aurora":
            k = sp * .35; a = math.sin(t * k); b = math.sin(t * k * 1.9 + 1.3); cc = .5 + .5 * math.sin(t * k * 1.3 + 2)
            return hsv(170 + 60 * a + 40 * b, .7 + .3 * cc, v * (.35 + .65 * cc))
        if m == "candle":
            self.cf += (random.random() - self.cf) * .08; return hsv(22 + self.cf * 10, .95, v * (.5 + .5 * self.cf))
        if m == "sunset":
            p = .5 - .5 * math.cos(t * sp * .12); return [x * v for x in grad(SUN, p * (len(SUN) - 1) / len(SUN))]
        if m == "storm":
            if random.random() < .012 + spd * .03: self.ls = 1.0
            self.ls *= .8; b = hsv(235, 1, v * .12); return tuple(x + (v - x) * self.ls for x in b)
        if m == "meteor":
            if self.mt < .05 and random.random() < .02 + spd * .03: self.mt = 1.0
            self.mt *= .9 + spd * .06; return hsv(h, s, v * (.04 + .96 * self.mt ** 2))
        if m == "police":
            ch = POL[int(now / ((140 - spd * 90) / 1000)) % len(POL)]
            return (v, 0, 0) if ch == "R" else (0, 0, v) if ch == "B" else (0, 0, 0)
        if m == "custom": return [x * v for x in grad([hex2rgb(x) for x in c["cols"]], t * sp * .08)]
        if m == "music":
            a = self.audio
            if not a: return hsv(h, s, v * .1)
            if a.error:
                self.say(f"Ошибка звука: {a.error}")
                return hsv(h, s, v * .1)
            dt = self.dt; b = a.bands; calm = 1 - spd
            try: g = float(c.get("gain", .6))
            except (TypeError, ValueError): g = .6
            if not math.isfinite(g): g = .6
            g = max(0.0, min(1.0, g))
            if self.bavg is None: self.bavg = b.copy()
            if self.pb is None: self.pb = b.copy()
            tot = float(b.sum()) if a.rms > .002 else 0.0
            if self.ref <= 0: self.ref = self.tf = tot
            self.tf += (tot - self.tf) * (1 - math.exp(-dt / .04))        # быстрая громкость
            self.ref += (self.tf - self.ref) * (1 - math.exp(-dt / 1.0))  # её среднее за ~секунду
            self.pk = tot if tot > self.pk else self.pk + (tot - self.pk) * (1 - math.exp(-dt / 8))
            # выше своего среднего — ярко, ниже — тускло (подходит и для тихих, и для громких треков)
            x = self.tf / max(self.ref, 1e-4)
            l = min(1.0, max(0.0, .5 + (x - 1) * (1.2 + 1.8 * g))) if tot > 0 else 0.0
            l *= .4 + .6 * min(1.0, tot / max(self.pk, 1e-4))             # тихие места темнее громких
            # вспышка на ударе: резкий рост энергии полос относительно среднего уровня
            df = a.np.maximum(b - self.pb, 0); self.pb = b.copy()
            flux = float(df[:4].sum() + .35 * df[4:].sum())   # бас важнее хай-хэтов
            self.flash = max(self.flash * math.exp(-dt / .15), min(1.0, flux / max(self.ref, 1e-4) * 2.0 * (.5 + g)))
            if self.flash > .75 and t - self.last_beat > .3: self.tdrift += 18; self.last_beat = t
            # цвет: где в спектре сейчас пик + сдвиг оттенка на каждый удар
            self.bavg += (b - self.bavg) * (1 - math.exp(-dt / 1.5))
            if l > .15:
                w = (b / (self.bavg + 1e-5)) ** 3
                ctr = float((w * a.np.arange(len(b))).sum() / w.sum())
                self.hue += (ctr / (len(b) - 1) * 300 - self.hue) * (1 - math.exp(-dt / (.15 + calm * .5)))
            self.drift += (self.tdrift - self.drift) * (1 - math.exp(-dt / .25))
            lvl = max(l, self.flash * .9)
            tau = (.03 + calm * .07) if lvl > self.env else (.03 + calm * .14)
            self.env += (lvl - self.env) * (1 - math.exp(-dt / tau))
            self.act += (l - self.act) * (1 - math.exp(-dt / .4))
            floor = .06 * min(1.0, self.act * 4)
            return hsv(self.hue + self.drift, s, v * (floor + (.65 - floor) * self.env ** (1.2 - .4 * g)))
        return (0, 0, 0)

    def check_sched(self):
        sc = self.cfg["sched"]
        if not sc["on"]: return
        d = datetime.now(); cur = d.hour * 60 + d.minute
        if cur == self.sched_min: return
        self.sched_min = cur
        on, off = _mins(sc["t_on"]), _mins(sc["t_off"]); wd = d.weekday()
        if cur == on and wd in sc["days"]: self.set_power(True)
        elif cur == off and ((wd if on <= off else (wd - 1) % 7) in sc["days"]): self.set_power(False)

    async def _main(self):
        while True:
            t0 = time.time()
            try:
                if self.want and not self.connected and not self.connecting and t0 > self.retry_at and self.cfg.get("addr"):
                    self.connecting = True
                    self.loop.create_task(self._connect(self.cfg["addr"]))
                await self._tick(t0)
            except Exception as ex:
                self.err = (f"{type(ex).__name__}: {ex}", time.time())
                try:
                    log = CFG.parent / "error.log"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    with log.open("a", encoding="utf-8") as f:
                        f.write("\n===== " + datetime.now().isoformat() + " =====\n")
                        f.write(traceback.format_exc())
                except Exception:
                    pass
            await asyncio.sleep(max(0, 1 / max(5, min(60, self.cfg.get("rate", 20))) - (time.time() - t0)))

    async def _tick(self, now):
        self.dt = min(.25, now - self.prev) if self.prev else .05; self.prev = now
        self.check_sched()
        mult = 1.0
        if self.sleep:
            r = (self.sleep[1] - now) / (self.sleep[1] - self.sleep[0])
            if r <= 0:
                self.sleep = None; self.set_power(False)
            else: mult = r
        f = self.flash
        if f and now - f["t"] >= f["dur"]:
            self.flash = None
            if f["was_off"] and not self.on and self.connected: await self._cmd(OFF)
            f = None
        if f and f["was_off"] and not f["sent"] and self.connected:
            f["sent"] = True; await self._cmd(ON)
        base = [x * mult for x in self.frame(now - self.t0, now)] if self.on else [0, 0, 0]
        if f:
            k = (1 - math.cos((now - f["t"]) * 2 * math.pi / .5)) / 2
            base = [base[i] * (1 - k) + f["c"][i] * k for i in range(3)]
        self.live = tuple(int(max(0, min(1, x)) * 255) for x in base) if (self.on or f) else (26, 18, 38)
        if not (self.on or f) or not self.connected: return
        o = tuple(round(255 * max(0, min(1, x)) ** 2) for x in base)
        if o != self.lastkey or now - self.lastsend > 1.5:
            self.lastkey, self.lastsend = o, now
            await self._cmd(bytes([0x7e, 0, 5, 3, o[0], o[1], o[2], 0, 0xef]))


def watch_notifications(cb):
    """Слушает уведомления рабочего стола (в т.ч. от KDE Connect) через dbus-monitor."""
    try:
        p = subprocess.Popen(["dbus-monitor", "interface='org.freedesktop.Notifications',member='Notify'"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError: return False

    def run():
        strs = None; cur = None
        for line in p.stdout:
            if line.startswith("method call") and "member=Notify" in line: strs, cur = [], None; continue
            if strs is None: continue
            ls = line.strip()
            if cur is not None:
                cur += "\n" + ls
                if ls.endswith('"'): strs.append(cur[:-1]); cur = None
                continue
            if ls.startswith('string "'):
                v = ls[8:]
                if v.endswith('"'): strs.append(v[:-1])
                else: cur = v
            elif ls.startswith("array ["):
                if len(strs) >= 3: cb(strs[0], strs[2], strs[3] if len(strs) > 3 else "")
                strs = None
    threading.Thread(target=run, daemon=True).start()
    return True


def on_notify(cfg, eng, app, summary, body):
    eng.last_notif = f"{app} | {summary}"[:70]
    if not cfg["notif"]: return
    if cfg["notif_kde"] and "kde" not in app.lower(): return
    text = f"{app} {summary}"; col = None
    for r in cfg["rules"]:
        try:
            if r["m"] and re.search(r["m"], text, re.I): col = r["c"]; break
        except re.error: pass
    if col is None and cfg["notif_all"]: col = cfg["dflt"]
    if col: eng.flash_color(col)


# ================= интерфейс =================
QSS = """
* { font-family:'DejaVu Sans Mono','Monospace'; color:#f3ebff; }
QWidget#win { background:#07030f; }
QFrame#panel { background:rgba(18,10,36,225); border:1px solid #7a35e8; border-radius:3px; }
QPushButton { background:transparent; border:1.5px solid #19e3ff; color:#19e3ff; padding:8px 12px; font-weight:bold; border-radius:2px; }
QPushButton:hover { background:rgba(25,227,255,40); }
QPushButton:checked { background:#ff2bd6; color:#000; border-color:#ff2bd6; }
QPushButton#gold { border-color:#ffc83d; color:#ffc83d; }
QPushButton#mag { border-color:#ff2bd6; color:#ff2bd6; }
QSlider::groove:horizontal { height:6px; background:#2b1b4d; border-radius:3px; }
QSlider::sub-page:horizontal { background:#ff2bd6; border-radius:3px; }
QSlider::handle:horizontal { background:#19e3ff; width:18px; margin:-7px 0; border-radius:9px; }
QLineEdit { background:#1c1033; border:1px solid #4a3a78; padding:6px; }
QCheckBox { spacing:8px; }
QTimeEdit { background:#1c1033; border:1px solid #4a3a78; padding:6px; }
QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; border:none; }
QScrollBar:vertical { background:transparent; width:8px; } QScrollBar::handle:vertical { background:#4a3a78; border-radius:4px; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; }
"""


class Retro(QtWidgets.QWidget):
    def paintEvent(self, e):
        p = QtGui.QPainter(self); w, h = self.width(), self.height(); hz = h * .76
        g = QtGui.QLinearGradient(0, 0, 0, hz)
        for pos, col in ((0, "#07030f"), (.5, "#1b0638"), (1, "#3a0b5e")): g.setColorAt(pos, QtGui.QColor(col))
        p.fillRect(QtCore.QRectF(0, 0, w, hz), g)
        r = min(w * .3, h * .3); cx, cy = w / 2, hz - r * .35
        sg = QtGui.QLinearGradient(0, cy - r, 0, cy + r); sg.setColorAt(0, QtGui.QColor("#ffc83d")); sg.setColorAt(1, QtGui.QColor("#ff2bd6"))
        p.setBrush(sg); p.setPen(QtCore.Qt.PenStyle.NoPen); p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
        for i in range(7): p.fillRect(QtCore.QRectF(cx - r, cy + r * (.05 + i * .14), 2 * r, r * (.012 + i * .012)), QtGui.QColor("#2e0a52"))
        p.fillRect(QtCore.QRectF(0, hz, w, h - hz), QtGui.QColor("#07030f"))
        p.setPen(QtGui.QPen(QtGui.QColor(25, 227, 255, 70), 1.2))
        for i in range(-12, 13): p.drawLine(QtCore.QPointF(w / 2, hz), QtCore.QPointF(w / 2 + i * w * .1, h))
        for i in range(1, 10):
            y = hz + (h - hz) * (i / 9) ** 2; p.drawLine(QtCore.QPointF(0, y), QtCore.QPointF(w, y))
        p.setPen(QtGui.QPen(QtGui.QColor("#ff2bd6"), 2.5)); p.drawLine(QtCore.QPointF(0, hz), QtCore.QPointF(w, hz))


class HueBar(QtWidgets.QWidget):
    changed = QtCore.Signal(float)
    def __init__(self):
        super().__init__(); self.h = 0.0; self.setFixedHeight(36)
    def paintEvent(self, e):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        g = QtGui.QLinearGradient(0, 0, self.width(), 0)
        for i in range(13): g.setColorAt(i / 12, QtGui.QColor.fromHsvF(min(i * 30, 359) / 360, 1, 1))
        p.setBrush(g); p.setPen(QtGui.QPen(QtGui.QColor("#efe6ff"), 1.5)); p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 4, 4)
        c = QtCore.QPointF(self.h / 360 * self.width(), self.height() / 2); p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor("#000"), 5)); p.drawEllipse(c, 13, 13)
        p.setPen(QtGui.QPen(QtGui.QColor("#fff"), 2.5)); p.drawEllipse(c, 13, 13)
    def _set(self, e):
        self.h = max(0.0, min(360.0, e.position().x() / self.width() * 360)); self.changed.emit(self.h); self.update()
    mousePressEvent = _set
    mouseMoveEvent = _set


class Visor(QtWidgets.QWidget):
    def __init__(self, eng):
        super().__init__(); self.eng = eng; self.setFixedHeight(130); self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    def paintEvent(self, e):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2); path = QtGui.QPainterPath(); path.addRoundedRect(r, 46, 46)
        c = QtGui.QColor(*self.eng.live); c2 = QtGui.QColor(c); c2.setAlphaF(.4)
        g = QtGui.QRadialGradient(r.center(), r.width() * .5); g.setColorAt(0, c); g.setColorAt(.55, c2); g.setColorAt(1, QtGui.QColor("#0b0516"))
        p.fillPath(path, g)
        hl = QtGui.QLinearGradient(0, r.top(), 0, r.top() + r.height() * .5); hl.setColorAt(0, QtGui.QColor(255, 255, 255, 70)); hl.setColorAt(1, QtGui.QColor(255, 255, 255, 0))
        p.fillPath(path, hl); p.setPen(QtGui.QPen(QtGui.QColor("#efe6ff"), 2.5)); p.drawPath(path)
        p.setFont(QtGui.QFont("monospace", 9, QtGui.QFont.Weight.Bold))
        txt = "● ВКЛ — КЛИК, ЧТОБЫ ВЫКЛЮЧИТЬ" if self.eng.on else "○ ВЫКЛ — КЛИК, ЧТОБЫ ВКЛЮЧИТЬ"
        tr = p.fontMetrics().boundingRect(txt).adjusted(-10, -5, 10, 5); tr.moveCenter(QtCore.QPoint(int(r.center().x()), int(r.bottom() - 22)))
        p.setBrush(QtGui.QColor(0, 0, 0, 150)); p.setPen(QtCore.Qt.PenStyle.NoPen); p.drawRoundedRect(tr, 6, 6)
        p.setPen(QtGui.QColor("#fff")); p.drawText(tr, QtCore.Qt.AlignmentFlag.AlignCenter, txt)
    def mousePressEvent(self, e): self.eng.set_power(not self.eng.on)


def panel(title):
    f = QtWidgets.QFrame(); f.setObjectName("panel"); lay = QtWidgets.QVBoxLayout(f); lay.setSpacing(8)
    t = QtWidgets.QLabel(title.upper()); ft = t.font(); ft.setBold(True); ft.setPointSize(9)
    ft.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 3); t.setFont(ft); t.setStyleSheet("color:#19e3ff"); lay.addWidget(t)
    return f, lay


def button(text, fn, name=None, checkable=False):
    b = QtWidgets.QPushButton(text.upper()); b.setCheckable(checkable); b.clicked.connect(fn)
    if name: b.setObjectName(name)
    return b


def slider(lay, label, val, fn):
    l = QtWidgets.QLabel(label.upper()); l.setStyleSheet("color:#9c8cc4;font-size:11px"); lay.addWidget(l)
    s = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); s.setRange(0, 100); s.setValue(int(val * 100)); s.valueChanged.connect(fn); lay.addWidget(s)
    return s


def clear(lay):
    while lay.count():
        w = lay.takeAt(0).widget()
        if w: w.deleteLater()


class Win(Retro):
    def __init__(self, cfg, eng, bridge):
        super().__init__(); self.cfg, self.eng = cfg, eng; self.setObjectName("win"); self.setWindowTitle("Лента"); self.resize(540, 880)
        bridge.devices.connect(self.pick)
        outer = QtWidgets.QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        sc = QtWidgets.QScrollArea(); sc.setWidgetResizable(True); outer.addWidget(sc)
        body = QtWidgets.QWidget(); body.setAutoFillBackground(False); sc.setWidget(body); sc.viewport().setAutoFillBackground(False)
        L = QtWidgets.QVBoxLayout(body); L.setContentsMargins(18, 14, 18, 24); L.setSpacing(14)

        title = QtWidgets.QLabel("ЛЕНТА"); ft = QtGui.QFont("monospace", 30, QtGui.QFont.Weight.Black)
        ft.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 10); title.setFont(ft); L.addWidget(title)
        bar = QtWidgets.QFrame(); bar.setFixedHeight(3)
        bar.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #ffc83d,stop:.5 #ff2bd6,stop:1 #19e3ff)"); L.addWidget(bar)
        self.status = QtWidgets.QLabel(); self.status.setStyleSheet("color:#9c8cc4;font-size:11px"); self.status.setWordWrap(True); L.addWidget(self.status)

        self.visor = Visor(eng); L.addWidget(self.visor)
        row = QtWidgets.QHBoxLayout(); self.bconn = button("Найти ленту", self.conn_click, "gold"); row.addWidget(self.bconn); row.addStretch(); L.addLayout(row)

        f, lay = panel("Цвет")
        self.hue = HueBar(); self.hue.h = cfg["h"]; self.hue.changed.connect(self.on_hue); lay.addWidget(self.hue)
        slider(lay, "Яркость", cfg["v"], lambda x: self.setv("v", max(2, x) / 100))
        slider(lay, "Насыщенность", cfg["s"], lambda x: self.setv("s", x / 100))
        slider(lay, "Скорость эффектов", cfg["speed"], lambda x: self.setv("speed", x / 100)); L.addWidget(f)

        f, lay = panel("Режимы"); grid = QtWidgets.QGridLayout(); self.mbtn = {}
        for i, (k, n) in enumerate(MODES):
            b = button(n, lambda _=False, k=k: self.pick_mode(k), checkable=True); self.mbtn[k] = b; grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid); L.addWidget(f); self.sync_modes()

        f, lay = panel("Музыка")
        slider(lay, "Чувствительность", cfg["gain"], lambda x: self.setv("gain", x / 100))
        hint = QtWidgets.QLabel("Выше — лента ярче реагирует даже на тихие и ровные места. «Скорость эффектов» меняет резкость."); hint.setWordWrap(True)
        hint.setStyleSheet("color:#9c8cc4;font-size:11px"); lay.addWidget(hint); L.addWidget(f)

        f, lay = panel("Свой градиент"); self.colrow = QtWidgets.QHBoxLayout(); lay.addLayout(self.colrow); self.rebuild_cols(); L.addWidget(f)

        f, lay = panel("Таймер сна"); r2 = QtWidgets.QHBoxLayout()
        for m in (15, 30, 60): r2.addWidget(button(f"{m} мин", lambda _=False, m=m: eng.start_sleep(m)))
        r2.addWidget(button("Отмена", lambda: eng.start_sleep(0), "mag")); lay.addLayout(r2)
        self.sleeplbl = QtWidgets.QLabel(""); self.sleeplbl.setStyleSheet("color:#9c8cc4;font-size:11px"); lay.addWidget(self.sleeplbl); L.addWidget(f)

        f, lay = panel("Расписание"); sc_ = cfg["sched"]
        cb = QtWidgets.QCheckBox("Включать и выключать по времени"); cb.setChecked(sc_["on"]); cb.toggled.connect(lambda v: self.setsched("on", v)); lay.addWidget(cb)
        tr = QtWidgets.QHBoxLayout()
        for lbl, key in (("Вкл", "t_on"), ("Выкл", "t_off")):
            tr.addWidget(QtWidgets.QLabel(lbl)); te = QtWidgets.QTimeEdit(QtCore.QTime.fromString(sc_[key], "HH:mm")); te.setDisplayFormat("HH:mm")
            te.timeChanged.connect(lambda t, key=key: self.setsched(key, t.toString("HH:mm"))); tr.addWidget(te)
        tr.addStretch(); lay.addLayout(tr)
        dr = QtWidgets.QHBoxLayout()
        for i, dn in enumerate(("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")):
            b = button(dn, lambda _=False, i=i: self.toggle_day(i), checkable=True); b.setChecked(i in sc_["days"]); b.setStyleSheet("padding:8px 2px"); dr.addWidget(b)
        lay.addLayout(dr); L.addWidget(f)

        f, lay = panel("Связь с лентой"); self.ratelbl = QtWidgets.QLabel(); self.ratelbl.setStyleSheet("color:#9c8cc4;font-size:11px"); lay.addWidget(self.ratelbl)
        rs = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); rs.setRange(5, 60); rs.setValue(int(cfg["rate"])); rs.valueChanged.connect(self.set_rate); lay.addWidget(rs)
        hint = QtWidgets.QLabel("Больше — плавнее. Если лента начинает отставать от звука, уменьши."); hint.setWordWrap(True); hint.setStyleSheet("color:#9c8cc4;font-size:11px"); lay.addWidget(hint)
        L.addWidget(f); self.set_rate(rs.value())

        f, lay = panel("Уведомления (KDE Connect)")
        for key, text in (("notif", "Мигать на уведомления"), ("notif_off", "Мигать, даже если лента выключена"),
                          ("notif_kde", "Только от KDE Connect"), ("notif_all", "Мигать на все, что не подошло под правила")):
            cb = QtWidgets.QCheckBox(text); cb.setChecked(cfg[key]); cb.toggled.connect(lambda v, k=key: self.setv(k, v)); lay.addWidget(cb)
        self.rules = QtWidgets.QVBoxLayout(); lay.addLayout(self.rules); self.rebuild_rules()
        r3 = QtWidgets.QHBoxLayout(); r3.addWidget(button("+ правило", self.add_rule)); r3.addWidget(button("Проверить", lambda: eng.flash_color(cfg["dflt"]), "gold")); r3.addStretch(); lay.addLayout(r3)
        self.notiflbl = QtWidgets.QLabel(); self.notiflbl.setStyleSheet("color:#9c8cc4;font-size:11px"); self.notiflbl.setWordWrap(True); lay.addWidget(self.notiflbl)
        L.addWidget(f)

        t = QtCore.QTimer(self); t.timeout.connect(self.refresh); t.start(50)

    def setv(self, k, v): self.cfg[k] = v; save(self.cfg)
    def setsched(self, k, v): self.cfg["sched"][k] = v; save(self.cfg)

    def toggle_day(self, i):
        d = set(self.cfg["sched"]["days"]); d ^= {i}; self.cfg["sched"]["days"] = sorted(d); save(self.cfg)

    def set_rate(self, v):
        self.cfg["rate"] = v; self.ratelbl.setText(f"ЧАСТОТА ОБНОВЛЕНИЯ: {v} КОМАНД/С"); save(self.cfg)

    def refresh(self):
        e = self.eng; self.visor.update()
        self.status.setText(("● " if e.connected else "○ ") + e.statustext.upper() + (("  ⚠ " + e.err[0]) if e.err and time.time() - e.err[1] < 10 else "")); self.bconn.setText("ОТКЛЮЧИТЬ" if e.connected else "НАЙТИ ЛЕНТУ")
        self.sleeplbl.setText(f"Погаснет через {math.ceil((e.sleep[1] - time.time()) / 60)} мин" if e.sleep else "")
        self.notiflbl.setText("Последнее уведомление: " + e.last_notif)

    def conn_click(self):
        if self.eng.connected: self.eng.disconnect(); return
        self.eng.say("Ищу устройства…")
        fu = self.eng.submit(self.eng.scan())
        fu.add_done_callback(lambda f: self.bridge_emit(f))

    def bridge_emit(self, f):
        try: self.findchild_bridge.devices.emit(f.result())
        except Exception: self.findchild_bridge.devices.emit([])

    def pick(self, devs):
        if not devs: self.eng.say("Ничего не найдено. Включи Bluetooth и ленту."); return
        names = [f"{n}  [{a}]" for n, a in devs]
        item, ok = QtWidgets.QInputDialog.getItem(self, "Лента", "Выбери устройство:", names, 0, False)
        if ok:
            self.cfg["addr"] = devs[names.index(item)][1]; save(self.cfg); self.eng.connect(self.cfg["addr"])
        else: self.eng.say("Не подключено")

    def sync_modes(self):
        for k, b in self.mbtn.items(): b.setChecked(k == self.cfg["mode"])

    def pick_mode(self, k): self.eng.set_mode(k); self.sync_modes()

    def on_hue(self, h):
        self.cfg["h"] = h
        if self.cfg["mode"] not in HUE_MODES: self.eng.set_mode("static"); self.sync_modes()
        else: save(self.cfg)

    def rebuild_cols(self):
        clear(self.colrow)
        for i, c in enumerate(self.cfg["cols"]):
            b = QtWidgets.QPushButton(); b.setFixedSize(42, 36); b.setStyleSheet(f"background:{c};border:1.5px solid #f3ebff")
            b.clicked.connect(lambda _=False, i=i: self.edit_col(i)); self.colrow.addWidget(b)
        if len(self.cfg["cols"]) < 8: self.colrow.addWidget(button("+", self.add_col))
        if len(self.cfg["cols"]) > 2: self.colrow.addWidget(button("−", self.del_col, "mag"))
        self.colrow.addStretch()

    def edit_col(self, i):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.cfg["cols"][i]), self, "Цвет")
        if c.isValid(): self.cfg["cols"][i] = c.name(); self.eng.set_mode("custom"); self.sync_modes(); self.rebuild_cols()

    def add_col(self): self.cfg["cols"].append("#ffffff"); self.eng.set_mode("custom"); self.sync_modes(); self.rebuild_cols()
    def del_col(self): self.cfg["cols"].pop(); save(self.cfg); self.rebuild_cols()

    def rebuild_rules(self):
        clear(self.rules)
        for i, r in enumerate(self.cfg["rules"]):
            w = QtWidgets.QWidget(); h = QtWidgets.QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
            le = QtWidgets.QLineEdit(r["m"]); le.setPlaceholderText("слово или regex"); le.textChanged.connect(lambda t, r=r: (r.__setitem__("m", t), save(self.cfg)))
            cb = QtWidgets.QPushButton(); cb.setFixedSize(42, 32); cb.setStyleSheet(f"background:{r['c']};border:1.5px solid #f3ebff")
            cb.clicked.connect(lambda _=False, r=r: self.rule_color(r))
            x = button("×", lambda _=False, i=i: self.del_rule(i), "mag"); x.setFixedWidth(40)
            h.addWidget(le); h.addWidget(cb); h.addWidget(x); self.rules.addWidget(w)

    def rule_color(self, r):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(r["c"]), self, "Цвет")
        if c.isValid(): r["c"] = c.name(); save(self.cfg); self.rebuild_rules()

    def add_rule(self): self.cfg["rules"].append({"m": "", "c": "#ffffff"}); save(self.cfg); self.rebuild_rules()
    def del_rule(self, i): self.cfg["rules"].pop(i); save(self.cfg); self.rebuild_rules()

    def closeEvent(self, e):
        if getattr(self, "tray_ok", False): e.ignore(); self.hide()
        else: e.accept()


def main():
    cfg = load()
    app = QtWidgets.QApplication(sys.argv); app.setStyleSheet(QSS)
    bridge = Bridge(); eng = Engine(cfg); win = Win(cfg, eng, bridge); win.findchild_bridge = bridge
    if cfg["mode"] == "music": eng.set_mode("music")
    if not watch_notifications(lambda a, s, b: on_notify(cfg, eng, a, s, b)):
        eng.last_notif = "dbus-monitor не найден: sudo apt install dbus-bin"
    tray_ok = QtWidgets.QSystemTrayIcon.isSystemTrayAvailable(); win.tray_ok = tray_ok
    app.setQuitOnLastWindowClosed(not tray_ok)
    if tray_ok:
        pm = QtGui.QPixmap(64, 64); pm.fill(QtCore.Qt.GlobalColor.transparent); p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing); p.setBrush(QtGui.QColor("#ff2bd6")); p.setPen(QtGui.QPen(QtGui.QColor("#19e3ff"), 5)); p.drawEllipse(6, 6, 52, 52); p.end()
        tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(pm), app); menu = QtWidgets.QMenu()
        menu.addAction("Открыть", win.show); menu.addAction("Вкл / выкл", lambda: eng.set_power(not eng.on)); menu.addAction("Выход", app.quit)
        tray.setContextMenu(menu); tray.activated.connect(lambda r: win.show() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None); tray.show()
    app.aboutToQuit.connect(eng.shutdown)
    if "--tray" not in sys.argv or not tray_ok: win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
