#!/usr/bin/env python3
"""
camb_gui.py - desktop viewer for "Plug2View"/HK-family mini cameras (self-contained, no app, no cloud).

  python3 camb_gui.py                         discover cameras on the LAN and show them all
  python3 camb_gui.py --cams 192.168.1.50     also add cameras by IP (comma separated)
  python3 camb_gui.py --bcast 192.168.178.255 extra broadcast address(es) for discovery

Main window: one live tile per camera; new cameras are picked up by a LAN search every --scan seconds.
Cameras found once are saved in ~/.config/camb_gui/cameras.json and reconnected at the next start.
Toolbar "Camera list": switch cameras on/off, rename, remove/restore, add by IP, auto-add on/off.
Camera window: Settings… (video quality) and WiFi… (join the camera to a network; it restarts).
Click a tile to open that camera in its own window (double-click / F11 = fullscreen, S = snapshot, Esc = close).
"""
import argparse, json, os, random, re, select, socket, string, sys, threading, time
from collections import OrderedDict, deque

try:
    from PyQt6.QtCore import Qt, QTimer, QRectF, QObject, pyqtSignal
    from PyQt6.QtGui import QImage, QPainter, QColor, QFont, QKeySequence, QShortcut, QAction
    from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
                                 QScrollArea, QLabel, QPushButton, QFileDialog, QToolBar, QFrame,
                                 QDialog, QFormLayout, QComboBox, QSpinBox, QDialogButtonBox, QMenu,
                                 QLineEdit, QCheckBox, QMessageBox, QDockWidget, QTableWidget,
                                 QTableWidgetItem, QHeaderView, QAbstractItemView, QInputDialog)
except ImportError:
    from PyQt5.QtCore import Qt, QTimer, QRectF, QObject, pyqtSignal
    from PyQt5.QtGui import QImage, QPainter, QColor, QFont, QKeySequence
    from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
                                 QScrollArea, QLabel, QPushButton, QFileDialog, QToolBar, QFrame, QShortcut, QAction,
                                 QDialog, QFormLayout, QComboBox, QSpinBox, QDialogButtonBox, QMenu,
                                 QLineEdit, QCheckBox, QMessageBox, QDockWidget, QTableWidget,
                                 QTableWidgetItem, QHeaderView, QAbstractItemView, QInputDialog)

# ------------------------------------------------------------------ camera protocol (from camb6.py)
# UDP 5000 <-> 5000, LAN search on UDP 2627; see README.md for the packet formats
XOR = 0xE9
CLIENT_TAG = b"\xd1\x07"
PORT = 5000
# 13-byte hello: 00 00 d0 00 [type|2] [conn-id] [hi|conn-lo] 09 00 d1 07 00 00
# the app picks a new connection id each start (seen: 9e/7 and cd/3); reusing an old one makes the
# camera treat us as the previous connection and ignore our fresh sequence numbers.
def hello_pkt(kind, cid, lo):
    t, hi = {"open1": (0x82, 0x00), "open2": (0xA2, 0x30), "keep": (0x62, 0x70)}[kind]
    return bytes([0, 0, 0xD0, 0, t, cid, hi | lo, 0x09, 0, 0xD1, 0x07, 0, 0])

def new_conn():
    return random.randrange(1, 255), random.randrange(0, 16)
SECRET_KEYS = ("wifipassword", "Psw", "pswd", "password")

# ------------------------------------------------------------------ codec
def benc(d):
    out = b"d"
    for k, v in d.items():
        k, v = k.encode(), str(v).encode()
        out += b"%d:%s%d:%s" % (len(k), k, len(v), v)
    return out + b"e"

def bdec(b):
    res, i = OrderedDict(), 1 if b[:1] == b"d" else 0
    try:
        while i < len(b) and b[i:i+1] not in (b"e", b"\0"):
            vals = []
            for _ in range(2):
                m = re.match(rb"(\d+):", b[i:]); n = int(m.group(1)); i += len(m.group(0))
                vals.append(b[i:i+n].decode("latin-1")); i += n
            res[vals[0]] = vals[1]
    except Exception:
        res["_raw"] = b.decode("latin-1")
    return res

def build_ctl(d, n, b45=None):
    body = bytes(c ^ XOR for c in benc(d) + b"\0")
    total = 13 + len(body)
    b4, b5 = b45 or (0x10 | (n & 3), 0x29)
    return (b"\0\0" + (total << 4).to_bytes(2, "little") + bytes([b4, b5, 0]) +
            (total - 4).to_bytes(2, "little") + CLIENT_TAG + b"\0\0" + body)

def parse_ctl(pl):
    return bdec(bytes(c ^ XOR for c in pl[13:]))

def build_data(seq, text, code):
    t = text.encode("latin-1")
    body = t[:2] + bytes(c ^ XOR for c in t[2:])
    total = 13 + len(body)
    return (seq.to_bytes(2, "little") + (total << 4).to_bytes(2, "little") + b"\x32\x01\x00" +
            (total - 4).to_bytes(2, "little") + code.to_bytes(2, "little") + b"\0\0" + body)

def data_text(pl):
    t = pl[13:]
    return (t[:2] + bytes(c ^ XOR for c in t[2:])).decode("latin-1", "replace")

def kv(text):
    out = OrderedDict()
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = bdec(v.encode("latin-1")) if v.startswith("d") and v.endswith("e") and ":" in v else v
    return out

def lan_text(pl):
    raw = pl[13:].rstrip(b"\0")
    cands = [raw, bytes(c ^ XOR for c in raw), raw[:2] + bytes(c ^ XOR for c in raw[2:])]
    return max(cands, key=lambda b: b.count(b"=") + 10 * (b"MainCmd" in b)).decode("latin-1")

def mask(d, show=False):
    if show: return d
    out = OrderedDict()
    for k, v in d.items():
        out[k] = mask(v) if isinstance(v, dict) else ("***" if k in SECRET_KEYS and v else v)
    return out

def jpeg_size(j):
    """(width, height) from the SOF marker, without PIL"""
    i = 2
    while i + 9 < len(j):
        if j[i] != 0xFF: i += 1; continue
        m = j[i+1]
        if m in (0xC0, 0xC1, 0xC2): return int.from_bytes(j[i+7:i+9], "big"), int.from_bytes(j[i+5:i+7], "big")
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7: i += 2; continue
        i += 2 + int.from_bytes(j[i+2:i+4], "big")
    return None

def rnd(n): return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))

def discovery_packet():
    end = int(time.time()) + 3600
    return bytes.fromhex("0000E007B2855A7A0000000000") + (
        f"TIME=3600;endTime={end};MainCmd=LocalData;userType=hkclient;"
        f"status=1;Prot=5000;MacIP=0x0xedabddb0:977017447;").encode()

def mksock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", port)); return s

# ------------------------------------------------------------------ discovery
def discover(targets=("255.255.255.255",), wait=3.0):
    """returns {ip: info-dict} from the LAN-search replies"""
    s = mksock(2726)
    for t in targets:
        try: s.sendto(discovery_packet(), (t, 2627))
        except OSError as e: print(f"send to {t}: {e}")
    found, end = {}, time.time() + wait
    while time.time() < end:
        r, _, _ = select.select([s], [], [], 0.2)
        if not r: continue
        pl, addr = s.recvfrom(4096)
        if pl[:2] != b"\0\0" or len(pl) < 20: continue
        info = kv(lan_text(pl))
        if info.get("MainCmd") == "LocalData" and "devid" in info:
            found[addr[0]] = info
    s.close()
    return found

def bcast_targets(extra):
    t = ["255.255.255.255"] + [x for x in (extra or "").split(",") if x]
    return t

# ------------------------------------------------------------------ one camera
class Frames:
    def __init__(self): self.cv = threading.Condition(); self.jpg = None; self.count = 0
    def put(self, j):
        with self.cv: self.jpg = j; self.count += 1; self.cv.notify_all()

class Cam:
    def __init__(self, hub, ip, devid=None, save=None):
        self.hub, self.ip, self.devid, self.save = hub, ip, devid or ip, save
        self.frames = Frames(); self.n = 0; self.tx = 1
        self.pkts = {}; self.nxt = None; self.ack = None
        self.buf = bytearray(); self.in_frame = False
        self.nframes = 0; self.lost = 0; self.last_rx = 0; self.gap_since = None
        self.sid = None; self.sids = set(); self.started = False; self.req_at = 0; self.retries = 0
        self.verbose = False
        self.guard = 0; self.next_guard = 0; self.next_hello = 0
        self.cid, self.clo = new_conn(); self.hello_reply = None
        self.replies = []          # text replies (config mode)
        self.acked = set()         # our data seqs the camera acknowledged
        if save: os.makedirs(save, exist_ok=True)

    # -- send helpers
    def send_raw(self, b): self.hub.sock.sendto(b, (self.ip, PORT))
    def send_ctl(self, d, b45=None): self.n += 1; self.send_raw(build_ctl(d, self.n, b45))
    def send_data(self, text, code):
        seq = self.tx; self.send_raw(build_data(seq, text, code)); self.tx = (self.tx + 1) & 0xFFFF; return seq
    def hello(self, new=False):
        if new:
            self.cid, self.clo = new_conn(); self.tx = 1          # fresh connection, sequence restarts
        self.send_raw(hello_pkt("open1", self.cid, self.clo)); self.send_raw(hello_pkt("open2", self.cid, self.clo))

    # -- video session
    def request(self):
        self.sid = f"{rnd(5)}.{random.getrandbits(32):08x}"; self.sids.add(self.sid)
        self.send_data(f"id=11;ftN0=video.vbVideo.MPEG4;ftN1=net.0;ftN2=HKPCPresent.HKPCPresent;"
                       f"opN2={self.sid};Callid={rnd(24)}{random.getrandbits(32):08x};sidN={self.sid};"
                       f"AsCode=337;MainCmd=HK_RES_REQ;user=Lan user;", 337)
        self.req_at = time.time()

    def start_session(self, fd):
        self.send_data(f"MainCmd=SessionStart;sidN={self.sid};ftN0=HKPCPresent.HKPCPresent;"
                       f"FD0=9;ftN1=net.{fd};FD1={fd};", 100)
        self.started = True
        print(f"[{self.devid}] session started")

    def stop(self):
        if self.sid: self.send_data(f"sidN={self.sid};MainCmd=SessionDelete;coz=;", 100)

    def open(self):
        self.hello(); time.sleep(0.3); self.request()
        self.send_ctl(OrderedDict(ICMD=29, SEQ=1, GUARDSEQ=0), b45=(0x10, 0x00))

    def tick(self, now):
        if self.sid and not self.started and now - self.req_at > 3:
            self.retries += 1
            if self.retries % 3 == 0:          # only every 3rd retry starts a new connection
                print(f"[{self.devid}] no SessionCreate yet, new connection id")
                self.hello(new=True); time.sleep(0.3)
            else:
                print(f"[{self.devid}] no SessionCreate yet, asking again")
            self.request()
        if now >= self.next_guard:
            self.guard += 1; self.send_ctl(OrderedDict(ICMD=29, SEQ=self.guard + 1, GUARDSEQ=self.guard), b45=(0x10, 0x00))
            self.next_guard = now + 1.5
        if now >= self.next_hello:
            self.send_raw(hello_pkt("keep", self.cid, self.clo)); self.next_hello = now + 10
        self.consume()

    # -- receive
    def on_packet(self, pl):
        if pl[:2] == b"\0\0":
            if len(pl) == 13:
                self.hello_reply = pl; return
            if len(pl) > 13:
                d = parse_ctl(pl)
                if d.get("ICMD") == "1" and "SEQ" in d: self.acked.add(int(d["SEQ"]))
            return
        seq = int.from_bytes(pl[0:2], "little")
        self.last_rx = time.time()
        if self.verbose and not self.started:
            print(f"[{self.devid}] << data seq={seq} {len(pl)}B {pl[:16].hex(' ')}")
        if b"\xff\xd8" not in pl:
            t = data_text(pl)
            d = None
            if "MainCmd=" in t:
                d = kv(t)
            elif re.match(r"d\d+:", t):                     # bencode reply (e.g. answer to MainCmd=800)
                b = bdec(t.rstrip("\0").encode("latin-1"))
                if "_raw" not in b: d = b
            if d is not None:
                self.replies.append(d)
                self.send_ctl(OrderedDict(ICMD=1, SEQ=seq, TIME=0))      # ack replies right away
                if d.get("MainCmd") == "SessionCreate" and not self.started and d.get("sidN") in self.sids:
                    self.sid = d["sidN"]; self.start_session(d.get("FD0", "1025"))
                elif d.get("MainCmd") != "SessionCreate" or self.verbose:
                    print(f"[{self.devid}] << {dict(mask(d))}")
                if not self.started: return
        if not self.started: return
        if self.nxt is None: self.nxt = seq; self.ack = seq
        if ((seq - self.nxt) & 0xFFFF) > 0x8000: return
        self.pkts[seq] = pl[4:]
        self.consume()
        while all(((self.ack + k) & 0xFFFF) in self.pkts or ((self.ack + k - self.nxt) & 0xFFFF) > 0x8000
                  for k in range(5)):
            self.send_ctl(OrderedDict(ICMD=1, SEQ=self.ack, TIME=0))
            self.ack = (self.ack + 5) & 0xFFFF

    def consume(self):
        while self.nxt is not None:
            if self.nxt in self.pkts:
                self.feed(self.pkts.pop(self.nxt)); self.nxt = (self.nxt + 1) & 0xFFFF; self.gap_since = None
                continue
            if not self.pkts: return
            ahead = max(((s - self.nxt) & 0xFFFF) for s in self.pkts)
            if self.gap_since is None:
                self.gap_since = time.time(); self.request_resend(ahead)
            if ahead > 30 or time.time() - self.gap_since > 0.3:
                self.lost += 1; self.in_frame = False; self.buf.clear()
                self.nxt = (self.nxt + 1) & 0xFFFF; self.gap_since = None
                continue
            return

    def request_resend(self, ahead):
        missing = [(self.nxt + k) & 0xFFFF for k in range(min(ahead, 31)) if ((self.nxt + k) & 0xFFFF) not in self.pkts]
        if not missing: return
        d = OrderedDict(ICMD=1, SEQ=self.ack, TIME=0, Flag=1)
        for i, m in enumerate(missing): d[f"LostSEQ_{i}"] = m
        d["Count"] = len(missing); self.send_ctl(d)

    def feed(self, data):
        a = data.find(b"\xff\xd8\xff")
        if a >= 0: self.buf = bytearray(data[a:]); self.in_frame = True
        elif self.in_frame: self.buf += data
        if self.in_frame:
            e = self.buf.find(b"\xff\xd9")
            if e >= 0:
                jpg = bytes(self.buf[:e+2]); self.in_frame = False; self.buf.clear()
                self.nframes += 1; self.frames.put(jpg)
                if self.save:
                    with open(os.path.join(self.save, f"{self.devid}_{self.nframes:06d}.jpg"), "wb") as f: f.write(jpg)

# ------------------------------------------------------------------ hub (one UDP socket, many cameras)
class Hub:
    def __init__(self):
        self.sock = mksock(PORT); self.cams = {}; self.lock = threading.Lock()
    def add(self, cam): self.cams[cam.ip] = cam
    def poll(self, timeout=0.2):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if r:
            pl, addr = self.sock.recvfrom(65535)
            cam = self.cams.get(addr[0])
            if cam:
                with self.lock: cam.on_packet(pl)
        now = time.time()
        with self.lock:
            for c in self.cams.values(): c.tick(now) if c.sid else None
    def wait_for(self, cond, timeout):
        end = time.time() + timeout
        while time.time() < end:
            self.poll(0.1)
            if cond(): return True
        return False

STALL_SECS = 8          # no data for this long while streaming -> reconnect
CONNECT_SECS = 25       # no session after this long -> start over with a fresh Cam
STORE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "camb_gui", "cameras.json")


def lan_benc(fields):
    """bencode a list of (key, str|bytes) pairs, byte-exact (the encoded WiFi password is not UTF-8 safe)"""
    out = b"d"
    for k, v in fields:
        k = k.encode(); v = v if isinstance(v, bytes) else str(v).encode()
        out += b"%d:%s%d:%s" % (len(k), k, len(v), v)
    return out + b"e"


def parse_lan_reply(pl):
    """a LAN-port reply as dict: bencode or key=value text, plain or XOR 0xE9"""
    body = pl[13:].rstrip(b"\0")
    for b in (body, bytes(c ^ XOR for c in body)):
        if b[:1] == b"d" and re.match(rb"d\d+:", b):
            d = bdec(b)
            if "_raw" not in d: return d
    d = kv(lan_text(pl))
    return d or None


class KnownCameras:
    """the camera list, kept in a JSON file:
    {"auto_add": bool, "cameras": {devid: {ip, name, alias, enabled, ignored, last_seen}}}
    enabled=False: listed but not streamed; ignored=True: removed by the user, never added back by a scan"""
    def __init__(self, path=STORE):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f: data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if "cameras" not in data: data = {"cameras": data}            # first version: flat {devid: {...}}
        self.auto_add = bool(data.get("auto_add", True))
        self.cams = {d: {"ip": v.get("ip", ""), "name": v.get("name", ""), "alias": v.get("alias", ""),
                         "enabled": v.get("enabled", True), "ignored": v.get("ignored", False),
                         "last_seen": v.get("last_seen", 0)} for d, v in data["cameras"].items()}

    def items(self):
        with self.lock: return [(d, dict(v)) for d, v in self.cams.items()]

    def get(self, devid):
        with self.lock: return dict(self.cams[devid]) if devid in self.cams else None

    def remember(self, devid, ip, name=""):
        """a camera answered: record it (new ones are enabled when auto_add is on); returns its entry"""
        with self.lock:
            old = self.cams.get(devid)
            e = dict(old) if old else {"alias": "", "enabled": self.auto_add, "ignored": False, "last_seen": 0}
            changed = old is None or e.get("ip") != ip or (name and e.get("name") != name)
            stale = time.time() - e["last_seen"] > 3600
            e.update(ip=ip, name=name or e.get("name", ""), last_seen=int(time.time()))
            self.cams[devid] = e
            if changed or stale: self._save()            # refresh last_seen on disk at most once an hour
            return dict(e)

    def update(self, devid, **kw):
        with self.lock:
            if devid in self.cams: self.cams[devid].update(kw); self._save()

    def add(self, devid, ip, **kw):
        with self.lock:
            e = self.cams.setdefault(devid, {"ip": ip, "name": "", "alias": "", "enabled": True,
                                             "ignored": False, "last_seen": 0})
            e.update(ip=ip, **kw); self._save()

    def forget(self, devid):
        with self.lock:
            if self.cams.pop(devid, None) is not None: self._save()

    def set_auto_add(self, on):
        with self.lock: self.auto_add = bool(on); self._save()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"auto_add": self.auto_add, "cameras": self.cams}, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"cannot save {self.path}: {e}")

# ------------------------------------------------------------------ backend (threads, no Qt)
class CameraManager:
    """owns the UDP hub, keeps every known camera streaming, finds new ones periodically"""
    def __init__(self, extra_ips=(), bcast=None, scan_every=15.0, known=None):
        self.hub = Hub()
        self.known = known if known is not None else KnownCameras()
        self.extra_ips, self.bcast, self.scan_every = list(extra_ips), bcast, scan_every
        self.by_devid = {}              # devid -> Cam (the Cam object is replaced on reconnect)
        self.info = {}                  # devid -> LAN-search info dict
        self.opened_at = {}             # devid -> time the current Cam was opened
        self.restarts = {}              # devid -> reconnect count
        self.seen = {}                  # devid -> (ip, time) of the last LAN-search answer (any camera, also removed ones)
        self.busy = set()               # devids currently being configured (watchdog leaves them alone)
        self.lock = threading.Lock()
        self.lan_lock = threading.Lock()  # one user of the LAN-search reply port (2726) at a time
        self.running = True
        self.last_scan = 0.0
        self.scanning = False

    def start(self):
        for devid, k in self.known.items():                  # reconnect to cameras from last time right away
            with self.lock: self.info[devid] = {"devid": devid, "DEVNAME": k.get("name", ""), "_saved": True}
            if k["enabled"] and not k["ignored"] and k["ip"]:
                print(f"[{devid}] known camera, trying {k['ip']}")
                self._open(k["ip"], devid)
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._scan_loop, daemon=True).start()

    def stop(self):
        self.running = False
        with self.hub.lock:
            for c in self.hub.cams.values():
                try: c.stop()
                except OSError: pass

    def cam(self, devid):
        with self.lock: return self.by_devid.get(devid)

    def devids(self):
        with self.lock: return list(self.by_devid)

    def scan_now(self):
        self.last_scan = 0.0

    # -- camera list (called from the GUI; blocking parts run in threads)
    def name(self, devid):
        k = self.known.get(devid) or {}
        return k.get("alias") or self.info.get(devid, {}).get("DEVNAME") or k.get("name") or devid

    def _close(self, devid):
        with self.lock: c = self.by_devid.pop(devid, None)
        if c:
            with self.hub.lock:
                if self.hub.cams.get(c.ip) is c: del self.hub.cams[c.ip]
            try: c.stop()
            except OSError: pass

    def _ip_of(self, devid):
        c = self.cam(devid)
        return c.ip if c else self.seen.get(devid, ((self.known.get(devid) or {}).get("ip"), 0))[0]

    def _open_async(self, devid):
        ip = self._ip_of(devid)
        if ip: threading.Thread(target=self._open, args=(ip, devid), daemon=True).start()

    def set_enabled(self, devid, on):
        """off: stop streaming but keep the camera in the list"""
        self.known.update(devid, enabled=bool(on))
        if on: self._open_async(devid)
        else: self._close(devid)

    def remove(self, devid):
        """stop it and keep it out: scans will not add it back until it is restored"""
        if self.known.get(devid) is None: self.known.add(devid, self._ip_of(devid) or devid)
        self.known.update(devid, ignored=True, enabled=False)
        self._close(devid)

    def restore(self, devid):
        self.known.update(devid, ignored=False, enabled=True); self._open_async(devid)

    def forget(self, devid):
        """drop it from the list completely (a scan adds it again when auto-add is on)"""
        self.known.forget(devid); self._close(devid)
        with self.lock: self.info.pop(devid, None); self.seen.pop(devid, None)

    def rename(self, devid, alias):
        self.known.update(devid, alias=alias.strip())

    def add_ip(self, ip):
        """add a camera by address (blocking); returns its devid, or None if it did not answer"""
        with self.lan_lock: found = discover([ip], wait=2.0)
        info = found.get(ip)
        if info and "devid" in info:
            devid = info["devid"]
            self.known.remember(devid, ip, info.get("DEVNAME", ""))
            self.known.update(devid, enabled=True, ignored=False)
            self._add(ip, info); return devid
        return None

    def add_ip_anyway(self, ip):
        """keep a silent address in the list (it is tried like any camera; devid = the IP)"""
        self.known.add(ip, ip, enabled=True, ignored=False)
        with self.lock: self.info.setdefault(ip, {"devid": ip})
        self._open_async(ip)

    # -- camera lifecycle
    def _open(self, ip, devid):
        c = Cam(self.hub, ip, devid)
        c.open()                                    # hello + HK_RES_REQ (sends only, safe outside the lock)
        with self.hub.lock:
            old = self.hub.cams.get(ip)
            self.hub.cams[ip] = c
        with self.lock:
            prev = self.by_devid.get(devid)
            self.by_devid[devid] = c
            self.opened_at[devid] = time.time()
        if prev and prev.ip != ip:                  # camera moved to a new IP (DHCP)
            with self.hub.lock:
                if self.hub.cams.get(prev.ip) is prev: del self.hub.cams[prev.ip]
        for o in {old, prev} - {None, c}:
            try: o.stop()
            except OSError: pass
        print(f"[{devid}] {ip}: requesting video")

    def _add(self, ip, info):
        devid = info.get("devid", ip)
        real = "devid" in info and not info.get("_manual")
        entry = None
        if real:                                            # a real answer from the camera -> remember it
            with self.lock: self.seen[devid] = (ip, time.time())
            entry = self.known.remember(devid, ip, info.get("DEVNAME", ""))
        with self.lock:
            if info.get("_manual") and devid in self.info: info = self.info[devid]
            self.info[devid] = info
            known = self.by_devid.get(devid)
        entry = entry or self.known.get(devid)
        if entry and (entry["ignored"] or not entry["enabled"]): return     # removed / switched off in the list
        if known is None or known.ip != ip:
            self._open(ip, devid)

    # -- WiFi (LAN port 2627, plain bencode, as sent by the Plug2View app): 703 = scan, 700 = set station WiFi
    def _lan_command(self, ip, fields, flag, wait):
        """send one LAN-port command to one camera (unicast, never broadcast) and collect its replies"""
        body = lan_benc(fields)
        hdr = (b"\0\0" + ((13 + len(body)) << 4).to_bytes(2, "little") + b"\x32" + os.urandom(3) +
               bytes([flag]) + b"\x64\0\0\0")
        replies = []
        with self.lan_lock:                         # discovery uses the same reply port (2726)
            s = mksock(2726)
            try:
                s.sendto(hdr + body, (ip, 2627))
                end = time.time() + wait
                while time.time() < end:
                    r, _, _ = select.select([s], [], [], 0.2)
                    if not r: continue
                    pl, addr = s.recvfrom(65535)
                    if addr[0] == ip and pl[:2] == b"\0\0" and len(pl) > 13:
                        d = parse_lan_reply(pl)
                        if d and d.get("MainCmd") != "LocalData": replies.append(d)
            finally:
                s.close()
        return replies

    def wifi_scan(self, devid):
        """list of (ssid, cipher, auth) the camera can see"""
        c = self.cam(devid)
        if not c: return []
        nets = []
        for d in self._lan_command(c.ip, [("MainCmd", "703"), ("MacIP", ""), ("lParam", "0")], 0, 6.0):
            for k, v in d.items():
                m = re.fullmatch(r"ValueN(\d+)", k)
                if m and v:
                    i = m.group(1)
                    nets.append((v, d.get(f"WifiDataEnc{i}", ""), d.get(f"WifiSafeType{i}", "")))
        return sorted(set(nets), key=lambda n: n[0].lower())

    def wifi_set(self, devid, ssid, password, static=None):
        """join the camera to a WiFi network; static = (ip, netmask, gateway, dns) or None for DHCP.
        The camera restarts afterwards. Returns the camera's replies (often none)."""
        c = self.cam(devid)
        if not c: return None
        ip, mask, gw, dns = static or (c.ip, "255.255.255.0", c.ip.rsplit(".", 1)[0] + ".1", c.ip.rsplit(".", 1)[0] + ".1")
        fields = [("MainCmd", "700"), ("isopen", "1"), ("MacIP", ""), ("encrytype", "auto"),
                  ("wifisid", ssid.encode()), ("safetype", "auto"),
                  ("wifipassword", bytes(b ^ 0x3C for b in password.encode())),     # EncodeStr: XOR 0x3C
                  ("bootproto", "static" if static else "dhcp"),
                  ("Ip", ip), ("netmask", mask), ("netwet", gw), ("dns0", dns), ("dns1", dns)]
        return self._lan_command(c.ip, fields, 1, 3.0)

    # -- settings (MainCmd=800 read, MainCmd=801;SubCmd=803 write); blocking, call from a worker thread
    def _wait(self, cond, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if cond(): return True
            time.sleep(0.05)                        # the poll thread delivers the packets
        return cond()

    def _command(self, ip, devid, text):
        """send one command on a fresh connection; returns (acknowledged, [reply dicts])"""
        for attempt in range(3):
            c = Cam(self.hub, ip, devid)
            with self.hub.lock: self.hub.cams[ip] = c
            c.hello(new=True)
            if not self._wait(lambda: c.hello_reply is not None, 1.5): continue
            seq = c.send_data(text, 337)
            ok = self._wait(lambda: seq in c.acked or c.replies, 2.5)
            self._wait(lambda: bool(c.replies), 1.0)
            if ok or c.replies: return True, list(c.replies)
        return False, []

    def configure(self, devid, text=None):
        """optionally send a set command, then read the settings back; the stream is paused meanwhile.
        returns (ok, settings dict or {})"""
        with self.lock:
            cur = self.by_devid.get(devid)
            if cur is None or devid in self.busy: return False, {}
            self.busy.add(devid)
        ip = cur.ip
        try:
            try: cur.stop()                         # end the video session first
            except OSError: pass
            with self.hub.lock: cur.sid = None      # stop its keepalives
            time.sleep(0.5)
            ok = True
            if text:
                ok, _ = self._command(ip, devid, text)
                time.sleep(1.0)
            # only the first command per connection is answered -> read back on a new one
            got, replies = self._command(ip, devid, f"MainCmd=800;id={devid};")
            settings = {}
            def flat(d):
                for k, v in d.items():
                    if isinstance(v, dict): flat(v)
                    else: settings.setdefault(k, v)
            for d in replies: flat(d)
            return ok and (got or not text), settings
        finally:
            self._open(ip, devid)                   # resume streaming
            with self.lock: self.busy.discard(devid)

    def _watchdog(self):
        now = time.time()
        with self.lock: items = [(d, c) for d, c in self.by_devid.items() if d not in self.busy]
        for devid, c in items:
            if c.started:
                stalled = now - (c.last_rx or self.opened_at.get(devid, now)) > STALL_SECS
            else:
                stalled = now - self.opened_at.get(devid, now) > CONNECT_SECS
            if stalled:
                print(f"[{devid}] stream stalled, reconnecting")
                with self.lock: self.restarts[devid] = self.restarts.get(devid, 0) + 1
                self._open(c.ip, devid)

    # -- threads
    def _poll_loop(self):
        next_dog = time.time() + 1
        while self.running:
            try:
                self.hub.poll(0.05)
            except OSError as e:
                print("poll:", e); time.sleep(0.2)
            if time.time() >= next_dog:
                self._watchdog(); next_dog = time.time() + 1

    def _scan_loop(self):
        while self.running:
            if time.time() - self.last_scan >= self.scan_every:
                self.scanning = True
                try:
                    saved = [k["ip"] for _, k in self.known.items()]
                    targets = list(dict.fromkeys(self.extra_ips + saved + bcast_targets(self.bcast)))
                    with self.lan_lock:
                        found = discover(targets, wait=2.0)   # saved IPs are asked directly too
                    for ip in self.extra_ips:              # manual IPs are shown even if they stay silent
                        if ip not in found and not any(c.ip == ip for c in list(self.by_devid.values())):
                            found[ip] = {"devid": ip, "_manual": True}
                    for ip, info in sorted(found.items()):
                        self._add(ip, info)
                except OSError as e:
                    print("discovery:", e)
                finally:
                    self.scanning = False
                    self.last_scan = time.time()
            time.sleep(0.25)

# ------------------------------------------------------------------ widgets
class VideoView(QWidget):
    """paints the latest JPEG of one camera, aspect ratio kept, with an optional overlay text"""
    clicked = pyqtSignal()
    double_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.img = None; self.overlay = "connecting…"
        self.setMinimumSize(160, 120)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_jpeg(self, jpg):
        img = QImage.fromData(jpg, "JPG")
        if not img.isNull():
            self.img = img; self.update()

    def set_overlay(self, text):
        if text != self.overlay: self.overlay = text; self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self.img is not None:
            s = self.img.size(); s.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            r = QRectF((self.width() - s.width()) / 2, (self.height() - s.height()) / 2, s.width(), s.height())
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(r, self.img)
        if self.overlay:
            p.setPen(QColor(230, 230, 230)); p.setFont(QFont("sans", 11))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.overlay)
        p.end()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton: self.clicked.emit()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton: self.double_clicked.emit()


class FeedState:
    """per-view bookkeeping: which frame was shown last, fps estimate, status text"""
    def __init__(self, devid):
        self.devid = devid; self.cam = None; self.last_count = -1; self.times = deque(maxlen=30)

    def update(self, mgr, view):
        """returns True when a new frame was drawn"""
        c = mgr.cam(self.devid)
        if c is not self.cam: self.cam, self.last_count = c, -1          # reconnected -> new Cam object
        if c is None: return False
        now = time.time()
        if c.frames.count != self.last_count and c.frames.jpg:
            self.last_count = c.frames.count; self.times.append(now)
            view.set_jpeg(c.frames.jpg)
            view.set_overlay("")
            return True
        if not c.started:
            view.set_overlay("not reachable — retrying…" if mgr.restarts.get(self.devid) else "connecting…")
        elif now - (c.last_rx or now) > 3: view.set_overlay("no signal")
        elif view.img is None: view.set_overlay("waiting for first frame…")
        return False

    def fps(self):
        t = [x for x in self.times if time.time() - x < 3]
        return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0


class Tile(QFrame):
    def __init__(self, mgr, devid, on_open, on_remove):
        super().__init__()
        self.on_remove = on_remove
        self.mgr, self.state = mgr, FeedState(devid)
        self.setObjectName("tile")
        self.view = VideoView(); self.view.setMinimumSize(300, 225)
        self.view.clicked.connect(lambda: on_open(devid))
        self.title = QLabel(); self.title.setObjectName("title")
        self.stats = QLabel(); self.stats.setObjectName("stats")
        cap = QHBoxLayout(); cap.setContentsMargins(8, 4, 8, 6); cap.addWidget(self.title); cap.addStretch(); cap.addWidget(self.stats)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        lay.addWidget(self.view, 1); lay.addLayout(cap)

    def contextMenuEvent(self, e):
        m = QMenu(self)
        m.addAction("Open").triggered.connect(lambda: self.view.clicked.emit())
        m.addAction("WiFi…").triggered.connect(lambda: WifiDialog(self.mgr, self.state.devid, self).exec())
        m.addAction("Remove camera…").triggered.connect(lambda: self.on_remove(self.state.devid))
        m.exec(e.globalPos())

    def refresh(self):
        self.state.update(self.mgr, self.view)
        name = self.mgr.name(self.state.devid)
        c = self.state.cam
        self.title.setText(f"{name}" + (f"  ·  {c.ip}" if c else ""))
        self.stats.setText(f"{self.state.fps():.0f} fps" if c and c.started else "")


# quality presets as in the Plug2View app (ConfigVideo.java): label -> (stream, enc, rate)
PRESETS = [("Low", 1, 3, 6), ("Normal", 2, 5, 6), ("Good", 3, 5, 12), ("High", 4, 5, 15), ("Excellent", 5, 5, 15)]
RESOLUTIONS = [("QVGA 320×240", 3), ("VGA 640×480", 5), ("HD 1280×720", 9)]
RATES = [3, 6, 9, 12, 15, 30]
PICTURE_KEYS = ("sharpness", "brightness", "saturation", "contrast")


class Worker(QObject):
    """runs a blocking call in a thread and hands the result back to the GUI thread"""
    done = pyqtSignal(object)
    def run(self, fn, *args):
        threading.Thread(target=lambda: self.done.emit(fn(*args)), daemon=True).start()


class SettingsDialog(QDialog):
    """read and change the video quality of one camera (stream pauses for a few seconds while talking to it)"""
    def __init__(self, mgr, devid, parent=None):
        super().__init__(parent)
        self.mgr, self.devid, self.current = mgr, devid, {}
        self.setWindowTitle(f"Video settings — {devid}")
        self.setMinimumWidth(380)
        self.quality = QComboBox()
        for label, *_ in PRESETS: self.quality.addItem(label)
        self.quality.addItem("Custom")
        self.bitrate = QSpinBox(); self.bitrate.setRange(6, 4096); self.bitrate.setValue(100); self.bitrate.setSuffix(" kbps")
        self.res = QComboBox()
        for label, enc in RESOLUTIONS: self.res.addItem(label, enc)
        self.fps = QComboBox()
        for r in RATES: self.fps.addItem(f"{r} fps", r)
        form = QFormLayout()
        form.addRow("Quality", self.quality); form.addRow("Bitrate", self.bitrate)
        form.addRow("Resolution", self.res); form.addRow("Frame rate", self.fps)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setObjectName("stats")
        self.buttons = QDialogButtonBox()
        self.apply_btn = self.buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.reload_btn = self.buttons.addButton("Reload", QDialogButtonBox.ButtonRole.ResetRole)
        self.buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_btn.clicked.connect(self.apply); self.reload_btn.clicked.connect(self.load)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addWidget(self.status); lay.addWidget(self.buttons)
        self.quality.currentIndexChanged.connect(self.preset_changed)
        self.worker = Worker(); self.worker.done.connect(self.finished)
        self.preset_changed(); self.load()

    def preset_changed(self):
        i = self.quality.currentIndex(); custom = i >= len(PRESETS)
        self.bitrate.setEnabled(custom)
        if not custom and not getattr(self, "_filling", False):
            _, _, enc, rate = PRESETS[i]
            self.res.setCurrentIndex(self.res.findData(enc)); self.select_rate(rate)

    def select_rate(self, rate):
        i = self.fps.findData(rate)
        if i < 0:
            self.fps.addItem(f"{rate} fps", rate); i = self.fps.count() - 1
        self.fps.setCurrentIndex(i)

    def busy(self, text):
        self.status.setText(text)
        for w in (self.apply_btn, self.reload_btn, self.quality, self.res, self.fps, self.bitrate): w.setEnabled(False)

    def idle(self):
        for w in (self.apply_btn, self.reload_btn, self.quality, self.res, self.fps): w.setEnabled(True)
        self.bitrate.setEnabled(self.quality.currentIndex() >= len(PRESETS))

    def load(self):
        self.busy("Reading settings from the camera (video pauses briefly)…")
        self.worker.run(self.mgr.configure, self.devid)

    def apply(self):
        i = self.quality.currentIndex()
        stream = PRESETS[i][1] if i < len(PRESETS) else self.bitrate.value()
        text = f"MainCmd=801;SubCmd=803;stream={stream};enc={self.res.currentData()};rate={self.fps.currentData()};"
        text += "".join(f"{k}={self.current[k]};" for k in PICTURE_KEYS if k in self.current)   # keep picture settings
        self.busy("Sending settings to the camera (video pauses briefly)…")
        self.worker.run(self.mgr.configure, self.devid, text)

    def finished(self, result):
        ok, settings = result
        self.idle()
        if settings: self.fill(settings)
        if not ok:
            self.status.setText("The camera did not answer. Check that it is online and try again.")
        elif not settings:
            self.status.setText("Command acknowledged, but the camera did not report its settings.")
        else:
            shown = "  ".join(f"{k}={settings[k]}" for k in ("stream", "enc", "rate") + PICTURE_KEYS if k in settings)
            self.status.setText(f"Camera reports: {shown}")

    def fill(self, s):
        self.current = s; self._filling = True
        try:
            if str(s.get("stream", "")).isdigit():
                st = int(s["stream"])
                if 1 <= st <= len(PRESETS): self.quality.setCurrentIndex(st - 1)
                else: self.quality.setCurrentIndex(len(PRESETS)); self.bitrate.setValue(max(6, min(4096, st)))
            if str(s.get("enc", "")).isdigit():
                e = int(s["enc"]); self.res.setCurrentIndex(self.res.findData(3 if e <= 3 else 9 if e == 9 else 5))
            if str(s.get("rate", "")).isdigit(): self.select_rate(int(s["rate"]))
        finally:
            self._filling = False
        self.bitrate.setEnabled(self.quality.currentIndex() >= len(PRESETS))


IPV4 = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")


class WifiDialog(QDialog):
    """join a camera to a WiFi network (MainCmd=700 on the LAN port, exactly like the Plug2View app)"""
    def __init__(self, mgr, devid, parent=None):
        super().__init__(parent)
        self.mgr, self.devid = mgr, devid
        self.setWindowTitle(f"WiFi — {devid}"); self.setMinimumWidth(420)
        c = mgr.cam(devid); ip = c.ip if c else ""
        net = ip.rsplit(".", 1)[0] if ip.count(".") == 3 else "192.168.1"
        attr = mgr.info.get(devid, {}).get("allAttribute")
        current = attr.get("wifisid", "") if isinstance(attr, dict) else ""

        self.ssid = QComboBox(); self.ssid.setEditable(True); self.ssid.setEditText(current)
        self.ssid.lineEdit().setPlaceholderText("network name (SSID)")
        self.scan_btn = QPushButton("Scan"); self.scan_btn.clicked.connect(self.scan)
        row = QHBoxLayout(); row.addWidget(self.ssid, 1); row.addWidget(self.scan_btn)
        self.security = QLabel("—"); self.security.setObjectName("stats")
        self.pwd = QLineEdit(); self.pwd.setEchoMode(QLineEdit.EchoMode.Password)
        show = QCheckBox("show"); show.toggled.connect(lambda on: self.pwd.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        prow = QHBoxLayout(); prow.addWidget(self.pwd, 1); prow.addWidget(show)
        self.mode = QComboBox(); self.mode.addItems(["DHCP (automatic)", "Static IP"])
        self.ip, self.mask = QLineEdit(ip), QLineEdit("255.255.255.0")
        self.gw, self.dns = QLineEdit(net + ".1"), QLineEdit(net + ".1")
        form = QFormLayout()
        form.addRow("Network", row); form.addRow("Security", self.security); form.addRow("Password", prow)
        form.addRow("Address", self.mode); form.addRow("IP", self.ip); form.addRow("Netmask", self.mask)
        form.addRow("Gateway", self.gw); form.addRow("DNS", self.dns)
        self.status = QLabel("The camera restarts after the change and then joins the new network.")
        self.status.setWordWrap(True); self.status.setObjectName("stats")
        self.buttons = QDialogButtonBox()
        self.apply_btn = self.buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_btn.clicked.connect(self.apply); self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addWidget(self.status); lay.addWidget(self.buttons)
        self.mode.currentIndexChanged.connect(self.mode_changed); self.mode_changed()
        self.ssid.currentTextChanged.connect(self.network_picked)
        self.worker = Worker(); self.worker.done.connect(self.on_result)
        self.nets = []

    def mode_changed(self):
        static = self.mode.currentIndex() == 1
        for w in (self.ip, self.mask, self.gw, self.dns): w.setEnabled(static)

    def network_picked(self, text):
        for ssid, enc, auth in self.nets:
            if ssid == text.strip():
                self.security.setText(" / ".join(x for x in (auth, enc) if x) or "unknown"); return
        self.security.setText("—")

    def scan(self):
        self.scan_btn.setEnabled(False); self.status.setText("Asking the camera for nearby networks…")
        self.worker.run(lambda: ("scan", self.mgr.wifi_scan(self.devid)))

    def apply(self):
        ssid, pwd = self.ssid.currentText().strip(), self.pwd.text()
        err = None
        if not ssid: err = "Enter the network name."
        elif len(ssid.encode()) > 32: err = "The network name is longer than 32 bytes."
        elif "<" in pwd: err = "The camera cannot store a '<' in the password (the app's encoding turns it into a string end)."
        elif pwd and len(pwd) < 8 and not self.security.text().upper().startswith("WEP"):
            err = "WPA passwords have at least 8 characters."
        static = None
        if not err and self.mode.currentIndex() == 1:
            vals = [w.text().strip() for w in (self.ip, self.mask, self.gw, self.dns)]
            if not all(IPV4.match(v) for v in vals): err = "IP, netmask, gateway and DNS must be addresses like 192.168.1.20."
            static = tuple(vals)
        if err: QMessageBox.warning(self, "WiFi", err); return
        ok = QMessageBox.question(self, "Change WiFi?",
            f"Camera {self.devid} will restart and try to join \"{ssid}\".\n\n"
            "If the name or password is wrong it will not come back on this network — "
            "you may then need its own hotspot or a reset button to reach it again.\n\nContinue?")
        if ok != QMessageBox.StandardButton.Yes: return
        self.apply_btn.setEnabled(False); self.status.setText("Sending…")
        self.worker.run(lambda: ("set", self.mgr.wifi_set(self.devid, ssid, pwd, static), ssid))

    def on_result(self, result):
        if result[0] == "scan":
            self.scan_btn.setEnabled(True)
            self.nets = result[1]; text = self.ssid.currentText()
            self.ssid.blockSignals(True); self.ssid.clear()
            for ssid, enc, auth in self.nets: self.ssid.addItem(ssid)
            self.ssid.setEditText(text); self.ssid.blockSignals(False)
            self.status.setText(f"{len(self.nets)} network(s) found — pick one or type the name."
                                if self.nets else "The camera did not report any networks (you can still type the name).")
            if self.nets and not text: self.ssid.setCurrentIndex(0)
            self.network_picked(self.ssid.currentText())
            return
        _, replies, ssid = result
        self.apply_btn.setEnabled(True)
        if replies is None:
            self.status.setText("Camera not connected."); return
        extra = f" Camera answered: {dict(mask(replies[0]))}" if replies else ""
        self.status.setText(f"Sent. The camera is restarting and will join \"{ssid}\". If this computer is on that "
                            "network too, the camera reappears here by itself after the next scan." + extra)


class CameraWindow(QWidget):
    """one camera, large, in its own top-level window"""
    def __init__(self, mgr, devid, on_close):
        super().__init__()
        self.mgr, self.state, self.on_close = mgr, FeedState(devid), on_close
        self.setWindowTitle(f"{mgr.name(devid)} — camera {devid}")
        self.resize(960, 760)
        self.view = VideoView(); self.view.setCursor(Qt.CursorShape.ArrowCursor)
        self.view.double_clicked.connect(self.toggle_fullscreen)
        self.stats = QLabel(); self.stats.setObjectName("stats")
        snap = QPushButton("Snapshot (S)"); snap.clicked.connect(self.snapshot)
        self.rec = QPushButton("Record frames"); self.rec.setCheckable(True); self.rec.toggled.connect(self.record)
        full = QPushButton("Fullscreen (F11)"); full.clicked.connect(self.toggle_fullscreen)
        sett = QPushButton("Settings…"); sett.clicked.connect(self.open_settings)
        wifi = QPushButton("WiFi…"); wifi.clicked.connect(lambda: WifiDialog(self.mgr, self.state.devid, self).exec())
        self.bar = QWidget(); bl = QHBoxLayout(self.bar); bl.setContentsMargins(8, 6, 8, 6)
        bl.addWidget(self.stats); bl.addStretch(); bl.addWidget(sett); bl.addWidget(wifi); bl.addWidget(self.rec); bl.addWidget(snap); bl.addWidget(full)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        lay.addWidget(self.view, 1); lay.addWidget(self.bar)
        QShortcut(QKeySequence("F11"), self, self.toggle_fullscreen)
        QShortcut(QKeySequence("S"), self, self.snapshot)
        QShortcut(QKeySequence("Escape"), self, self.escape)
        self.rec_dir = None

    def refresh(self):
        self.state.update(self.mgr, self.view)
        c = self.state.cam
        if not c: self.stats.setText("not connected"); return
        if self.rec_dir and c.save != self.rec_dir: c.save = self.rec_dir   # keep recording across reconnects
        j = c.frames.jpg; size = jpeg_size(j) if j else None
        parts = [c.ip]
        if size: parts.append(f"{size[0]}×{size[1]}")
        if c.started: parts.append(f"{self.state.fps():.1f} fps")
        if j: parts.append(f"{len(j) // 1024} KB/frame")
        parts.append(f"frames {c.nframes}  dropped {c.lost}")
        if self.mgr.restarts.get(self.state.devid): parts.append(f"reconnects {self.mgr.restarts[self.state.devid]}")
        if self.rec_dir: parts.append("● REC")
        self.stats.setText("   ".join(parts))

    def snapshot(self):
        c = self.state.cam
        if not c or not c.frames.jpg: return
        default = os.path.join(os.path.expanduser("~/Pictures") if os.path.isdir(os.path.expanduser("~/Pictures"))
                               else os.getcwd(), f"{self.state.devid}_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
        path, _ = QFileDialog.getSaveFileName(self, "Save snapshot", default, "JPEG (*.jpg)")
        if path:
            with open(path, "wb") as f: f.write(c.frames.jpg)

    def record(self, on):
        c = self.state.cam
        if on:
            d = QFileDialog.getExistingDirectory(self, "Store frames in", os.getcwd())
            if not d: self.rec.setChecked(False); return
            self.rec_dir = os.path.join(d, f"{self.state.devid}_{time.strftime('%Y%m%d_%H%M%S')}")
            os.makedirs(self.rec_dir, exist_ok=True)
            self.rec.setText("Stop recording")
        else:
            self.rec_dir = None; self.rec.setText("Record frames")
            if c: c.save = None

    def open_settings(self):
        SettingsDialog(self.mgr, self.state.devid, self).exec()

    def toggle_fullscreen(self):
        if self.isFullScreen(): self.showNormal(); self.bar.show()
        else: self.showFullScreen(); self.bar.hide()

    def escape(self):
        if self.isFullScreen(): self.toggle_fullscreen()
        else: self.close()

    def closeEvent(self, e):
        if self.rec_dir: self.record(False)
        self.on_close(self.state.devid); super().closeEvent(e)


def ago(t):
    if not t: return "never"
    d = time.time() - t
    if d < 60: return "just now"
    if d < 3600: return f"{d // 60:.0f} min ago"
    if d < 86400: return f"{d // 3600:.0f} h ago"
    return time.strftime("%Y-%m-%d", time.localtime(t))


class CameraListPanel(QWidget):
    """every known camera: switch streaming on/off, rename, remove/restore, add by IP"""
    COLS = ["On", "Name", "ID", "IP", "Status", "Last seen"]

    def __init__(self, mgr, main):
        super().__init__()
        self.mgr, self.main, self.order = mgr, main, []
        self.table = QTableWidget(0, len(self.COLS)); self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        h = self.table.horizontalHeader()
        for i in range(len(self.COLS)): h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self.item_changed)
        self.table.itemSelectionChanged.connect(self.update_buttons)
        self.table.cellDoubleClicked.connect(lambda r, c: c and self.selected() and self.main.open_camera(self.selected()))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        self.add_btn = QPushButton("Add by IP…"); self.add_btn.clicked.connect(self.add_ip)
        self.ren_btn = QPushButton("Rename…"); self.ren_btn.clicked.connect(self.rename)
        self.rm_btn = QPushButton("Remove"); self.rm_btn.clicked.connect(self.remove_or_restore)
        self.auto = QCheckBox("Auto-add new cameras found on the network")
        self.auto.setChecked(mgr.known.auto_add); self.auto.toggled.connect(mgr.known.set_auto_add)
        btns = QHBoxLayout(); btns.addWidget(self.add_btn); btns.addWidget(self.ren_btn); btns.addWidget(self.rm_btn); btns.addStretch()
        lay = QVBoxLayout(self); lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.table, 1); lay.addLayout(btns); lay.addWidget(self.auto)
        self.setMinimumWidth(520)
        self.worker = Worker(); self.worker.done.connect(self.added)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1000)
        self.refresh(); self.update_buttons()

    # -- data
    def rows(self):
        known = dict(self.mgr.known.items())
        ids = set(known) | set(self.mgr.devids())
        return sorted(ids, key=lambda d: (known.get(d, {}).get("ignored", False), self.mgr.name(d).lower(), d)), known

    def status(self, devid, k):
        seen_ip, seen_t = self.mgr.seen.get(devid, (None, 0))
        here = " · on network" if time.time() - seen_t < 3 * self.mgr.scan_every + 10 else ""
        if k.get("ignored"): return "removed" + here
        if k and not k.get("enabled", True): return "off" + here
        c = self.mgr.cam(devid)
        if c is None: return "waiting"
        if c.started: return "streaming" if time.time() - (c.last_rx or 0) < 3 else "no signal"
        return "not reachable" if self.mgr.restarts.get(devid) else "connecting"

    def refresh(self):
        if not self.isVisible() and self.order: return
        ids, known = self.rows()
        sel = self.selected()
        self.table.blockSignals(True)
        if ids != self.order:
            self.order = ids; self.table.setRowCount(len(ids))
            for r in range(len(ids)):
                for col in range(len(self.COLS)): self.table.setItem(r, col, QTableWidgetItem())
        for r, devid in enumerate(ids):
            k = known.get(devid, {"enabled": True, "ignored": False})
            c = self.mgr.cam(devid)
            ip = c.ip if c else self.mgr.seen.get(devid, (k.get("ip", ""), 0))[0] or k.get("ip", "")
            last = max(self.mgr.seen.get(devid, (None, 0))[1], k.get("last_seen", 0))
            on = self.table.item(r, 0)
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            on.setFlags(flags if k.get("ignored") else flags | Qt.ItemFlag.ItemIsUserCheckable)
            on.setCheckState(Qt.CheckState.Checked if k.get("enabled", True) and not k.get("ignored") else Qt.CheckState.Unchecked)
            for col, text in enumerate([self.mgr.name(devid), devid, ip, self.status(devid, k), ago(last)], 1):
                it = self.table.item(r, col); it.setText(text)
                it.setForeground(QColor(110, 110, 110) if k.get("ignored") else QColor(221, 221, 221))
        if sel in ids and sel != self.selected(): self.table.selectRow(ids.index(sel))
        self.table.blockSignals(False)
        self.update_buttons()

    def selected(self):
        r = self.table.currentRow()
        return self.order[r] if 0 <= r < len(self.order) and self.table.selectedItems() else None

    def is_removed(self, devid):
        return bool((self.mgr.known.get(devid) or {}).get("ignored"))

    def update_buttons(self):
        d = self.selected()
        self.ren_btn.setEnabled(bool(d)); self.rm_btn.setEnabled(bool(d))
        self.rm_btn.setText("Restore" if d and self.is_removed(d) else "Remove")

    # -- actions
    def item_changed(self, it):
        if it.column() == 0 and 0 <= it.row() < len(self.order):
            devid = self.order[it.row()]
            if self.mgr.known.get(devid) is None: self.mgr.known.add(devid, self.mgr._ip_of(devid) or devid)
            on = it.checkState() == Qt.CheckState.Checked
            if not on and (w := self.main.windows.get(devid)): w.close()
            self.mgr.set_enabled(devid, on)

    def remove_or_restore(self):
        d = self.selected()
        if not d: return
        if self.is_removed(d): self.mgr.restore(d)
        else: self.main.remove_camera(d)
        self.refresh()

    def rename(self):
        d = self.selected()
        if not d: return
        if self.mgr.known.get(d) is None: self.mgr.known.add(d, self.mgr._ip_of(d) or d)
        text, ok = QInputDialog.getText(self, "Rename camera", f"Name for {d} (empty = camera's own name):",
                                        text=(self.mgr.known.get(d) or {}).get("alias", ""))
        if ok:
            self.mgr.rename(d, text); self.refresh()
            if (w := self.main.windows.get(d)): w.setWindowTitle(f"{self.mgr.name(d)} — camera {d}")

    def add_ip(self):
        ip, ok = QInputDialog.getText(self, "Add camera", "Camera IP address:")
        ip = ip.strip()
        if not ok or not ip: return
        if not IPV4.match(ip): QMessageBox.warning(self, "Add camera", f"\"{ip}\" is not an IP address."); return
        self.add_btn.setEnabled(False); self.add_btn.setText("Asking…")
        self.worker.run(lambda: (ip, self.mgr.add_ip(ip)))

    def added(self, result):
        ip, devid = result
        self.add_btn.setEnabled(True); self.add_btn.setText("Add by IP…")
        if devid is None:
            if QMessageBox.question(self, "Add camera", f"No camera answered at {ip}.\n\nAdd the address anyway? "
                                    "It will be tried like any other camera.") == QMessageBox.StandardButton.Yes:
                self.mgr.add_ip_anyway(ip)
        self.refresh()

    def menu(self, pos):
        d = self.selected()
        if not d: return
        m = QMenu(self)
        if not self.is_removed(d):
            m.addAction("Open").triggered.connect(lambda: self.main.open_camera(d))
            m.addAction("WiFi…").triggered.connect(lambda: WifiDialog(self.mgr, d, self).exec())
        m.addAction("Rename…").triggered.connect(self.rename)
        m.addAction("Restore" if self.is_removed(d) else "Remove…").triggered.connect(self.remove_or_restore)
        m.addSeparator()
        m.addAction("Forget completely…").triggered.connect(lambda: self.main.forget_camera(d))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def showEvent(self, e):
        super().showEvent(e); self.order = []; self.refresh()


class MainWindow(QMainWindow):
    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr; self.tiles = {}; self.windows = {}; self.cols = 0
        self.setWindowTitle("Cameras"); self.resize(1100, 760)
        tb = QToolBar(); tb.setMovable(False); self.addToolBar(tb)
        scan = QAction("Scan now", self); scan.triggered.connect(mgr.scan_now); tb.addAction(scan)
        self.panel = CameraListPanel(mgr, self)
        self.dock = QDockWidget("Camera list", self); self.dock.setWidget(self.panel)
        self.dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock); self.dock.hide()
        lst = self.dock.toggleViewAction(); lst.setText("Camera list"); tb.addAction(lst)
        self.grid_host = QWidget(); self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(12); self.grid.setContentsMargins(12, 12, 12, 12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty = QLabel("Searching the network for cameras…"); self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("empty"); self.grid.addWidget(self.empty, 0, 0)
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(self.grid_host); self.setCentralWidget(sa)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(33)

    def refresh(self):
        current = self.mgr.devids()
        for devid in current:
            if devid not in self.tiles:
                self.tiles[devid] = Tile(self.mgr, devid, self.open_camera, self.remove_camera); self.relayout(force=True)
        for devid in [d for d in self.tiles if d not in current]:
            self.tiles.pop(devid).deleteLater(); self.relayout(force=True)
        self.relayout()                                 # no-op unless the free width changed (e.g. list panel shown)
        for t in self.tiles.values(): t.refresh()
        for w in self.windows.values(): w.refresh()
        n = len(self.tiles)
        scan = "scanning…" if self.mgr.scanning else f"next scan in {max(0, self.mgr.scan_every - (time.time() - self.mgr.last_scan)):.0f}s"
        live = sum(1 for d in self.tiles if (c := self.mgr.cam(d)) and c.started)
        self.statusBar().showMessage(f"{n} camera(s), {live} streaming   ·   {scan}   ·   click a camera to open it")

    def relayout(self, force=False):
        cols = max(1, (self.centralWidget().viewport().width() - 24) // 340)   # space left beside the list panel
        if cols == self.cols and not force: return
        self.cols = cols
        while self.grid.count(): self.grid.takeAt(0)
        if not self.tiles:
            self.grid.addWidget(self.empty, 0, 0); self.empty.show(); return
        self.empty.hide()
        for i, devid in enumerate(sorted(self.tiles)):
            self.grid.addWidget(self.tiles[devid], i // cols, i % cols)
        for c in range(self.grid.columnCount()): self.grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e); self.relayout()

    def remove_camera(self, devid, ask=True):
        if ask and QMessageBox.question(self, "Remove camera",
                f"Remove \"{self.mgr.name(devid)}\" ({devid})?\n\nIt stops streaming and network scans will not add it "
                "back. You can restore it from the camera list.") != QMessageBox.StandardButton.Yes:
            return
        if (w := self.windows.get(devid)): w.close()
        self.mgr.remove(devid); self.panel.refresh()

    def forget_camera(self, devid):
        if QMessageBox.question(self, "Forget camera",
                f"Delete \"{self.mgr.name(devid)}\" ({devid}) from the list completely?\n\n"
                "If it is still on the network and auto-add is on, the next scan adds it again.") != QMessageBox.StandardButton.Yes:
            return
        if (w := self.windows.get(devid)): w.close()
        self.mgr.forget(devid); self.panel.refresh()

    def open_camera(self, devid):
        w = self.windows.get(devid)
        if w is None:
            w = self.windows[devid] = CameraWindow(self.mgr, devid, lambda d: self.windows.pop(d, None))
            w.setStyleSheet(STYLE)
        w.show(); w.raise_(); w.activateWindow()

    def closeEvent(self, e):
        for w in list(self.windows.values()): w.close()
        self.mgr.stop(); super().closeEvent(e)


STYLE = """
QWidget { background: #111; color: #ddd; font-size: 13px; }
QFrame#tile { background: #1b1b1b; border: 1px solid #333; border-radius: 6px; }
QFrame#tile:hover { border-color: #8ab4f8; }
QLabel#title { background: transparent; font-weight: 600; }
QLabel#stats { background: transparent; color: #999; }
QLabel#empty { color: #888; font-size: 16px; padding: 60px; }
QToolBar { border: none; border-bottom: 1px solid #333; spacing: 6px; padding: 4px; }
QPushButton, QToolButton { background: #2a2a2a; border: 1px solid #444; border-radius: 4px; padding: 5px 12px; }
QPushButton:hover, QToolButton:hover { border-color: #8ab4f8; }
QPushButton:checked { background: #7a1f1f; }
QStatusBar { color: #999; border-top: 1px solid #333; }
QTableWidget { gridline-color: #2a2a2a; selection-background-color: #2b3f5c; }
QHeaderView::section { background: #1b1b1b; color: #aaa; border: none; border-bottom: 1px solid #333; padding: 4px; }
QDockWidget::title { padding: 4px; background: #1b1b1b; }
QCheckBox::indicator, QTableWidget::indicator { width: 13px; height: 13px; border: 1px solid #888; border-radius: 2px; background: #1e1e1e; }
QCheckBox::indicator:checked, QTableWidget::indicator:checked { background: #8ab4f8; border-color: #8ab4f8; }
QLineEdit, QComboBox, QSpinBox { background: #1e1e1e; border: 1px solid #444; border-radius: 3px; padding: 3px; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #555; border-color: #2a2a2a; }
"""

# ------------------------------------------------------------------ main
def port_free(port):
    """mksock binds with SO_REUSEADDR, so a second instance would bind silently and split the camera traffic;
    a bind without the flag fails while anyone holds the port"""
    t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        t.bind(("", port)); return True
    except OSError:
        return False
    finally:
        t.close()

def main():
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cams", help="camera IPs to add besides the discovered ones, comma separated")
    ap.add_argument("--bcast", help="extra broadcast addresses, comma separated (e.g. 192.168.178.255)")
    ap.add_argument("--scan", type=float, default=15.0, help="seconds between LAN searches (default 15)")
    a = ap.parse_args()

    app = QApplication(sys.argv[:1]); app.setApplicationName("Cameras"); app.setStyleSheet(STYLE)
    if not port_free(PORT):
        msg = (f"UDP port {PORT} is already in use - another camb_gui.py or camb6.py serve is running.\n"
               "Two programs on this port steal each other's camera packets; close the other one first.")
        QMessageBox.critical(None, "Cameras", msg); sys.exit(msg)
    try:
        mgr = CameraManager([x for x in (a.cams or "").split(",") if x], a.bcast, a.scan)
    except OSError as e:
        sys.exit(f"cannot open UDP port {PORT} ({e}) - is another camera program still running?")
    mgr.start()
    win = MainWindow(mgr); win.show()
    try:
        sys.exit(app.exec())
    finally:
        mgr.stop()

if __name__ == "__main__":
    main()
