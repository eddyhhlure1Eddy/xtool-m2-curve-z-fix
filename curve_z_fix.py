"""xTool M2 curved-surface mode: focus height (Z) fix.

Problem (xTool Studio 1.9.11, M2 device plugin JS002 0.2.4, 455 nm 40 W head):
  every curved-surface point is stored as   z = G - distance + Sg     (G = head calibration, Sg = 5.7)
  the job then moves the head to             Z = (G - Sg) - z
  plane mode uses                            Z = (G - Sg) - thickness
So the curved model is Sg (5.7 mm) too high and the laser focus sits that far above the material.

Fix: correct the measured model, atomm/curve/surface.txt, before the job is built. Studio copies that file
into the task folder every time 加工 / Process builds a job. Nothing in Studio or the plugin is modified.

The correction is measured, not fixed:
  1. if there is a plane-mode thickness reading of the same board, taken before the curved measurement with
     the lid kept closed, the offset is (model z - thickness) at that point;
  2. otherwise the offset the installed plugin adds (Sg) is read from its code;
  3. lowering the head (model too high) is only done when 1 and 2 agree within 1 mm, or from 2 alone when
     there is no reading; raising the head (model too low) is done from 1 alone; anything else is refused.

usage: python curve_z_fix.py <command> [--surface PATH] [--dry-run] [--no-tasks]
  check      show the plugin state, the model state and the measured offset
  fix        check, then correct the model if needed (never twice)
  verify     after clicking 加工 / Process: did the newest curved job use a corrected model
  watch      keep running: correct every new model, verify every new curved job, pop up a warning on problems
  install    start "watch" at logon (writes a small .vbs into the Startup folder) and start it now
  uninstall  remove the Startup entry (stop the running pythonw.exe yourself, or log off)
State, backups and logs: %LOCALAPPDATA%\\xtool-m2-curve-z-fix
"""
import ctypes
import glob
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime

CLOSE = 0.3        # mm: model within this of the plane reading counts as correct
SAME = 0.5         # mm: same grid as the last corrected model, median difference within this of 0 / of the offset
AGREE = 1.0        # mm: a measured offset that lowers the head must agree with the plugin's number within this
MAX_OFFSET = 15.0  # mm: larger differences are treated as a bad reading
MAX_THICKNESS = 26.5
ROOT = os.path.join(os.environ["APPDATA"], "xTool Studio")
ATOMM = os.path.join(ROOT, "atomm")
CURVE = os.path.join(ATOMM, "curve", "surface.txt")
PLUGIN = os.path.join(ATOMM, "exts", "JS002")
STATE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "xtool-m2-curve-z-fix")
LOG = os.path.join(STATE, "patch_log.json")
LAST = os.path.join(STATE, "last_corrected_surface.txt")
WATCH_LOG = os.path.join(STATE, "watch.log")
STARTUP = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup", "xtool-m2-curve-z-fix.vbs")
FORMULA = [
    "t.z=this.getZDistance()-Number(n.distance??0)+Sg(",  # measure(): point height + Sg
    "l=Sg(c)",                                             # job reference uses Sg ...
    "z_max:s.getZDistance()-l",                            # ... and subtracts it again
]
SG = re.compile(r"fte=([\d.]+),pte=([\d.]+);function Sg\(e\)\{return e\?pte:fte\}")


def now():
    return datetime.now().strftime("%m-%d %H:%M:%S")


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def load_log():
    return json.load(open(LOG, encoding="utf-8")) if os.path.exists(LOG) else []


def corrected_hashes(log):
    hashes = {e["patched_sha256"] for e in log}
    marker = CURVE + ".zfix.json"  # travels with Studio's file, in case the state folder is lost
    if os.path.exists(marker):
        try:
            hashes.add(json.load(open(marker, encoding="utf-8"))["sha256"])
        except (ValueError, KeyError):
            pass
    return hashes


def code_offset():
    """(offset the plugin adds for the blue laser head or None, version, digest, note), read from its code."""
    try:
        meta = json.load(open(os.path.join(PLUGIN, "meta", "manifest.json"), encoding="utf-8"))
        text = open(os.path.join(PLUGIN, "index.js"), encoding="utf-8", errors="replace").read()
    except OSError:
        return None, "?", "?", "plugin files not found"
    version, digest = meta.get("version", "?"), meta.get("contentDigest", "?")
    m = SG.search(text)
    if m and all(p in text for p in FORMULA):
        return float(m.group(1)), version, digest, "measure() adds Sg and the job subtracts it again"
    return None, version, digest, "plugin code changed; its offset cannot be read"


def load_points(path):
    rows = json.load(open(path, encoding="utf-8"))
    return rows, [p for r in rows for p in r]


def grid_of(pts):
    return {(round(p["x"], 3), round(p["y"], 3)): p["z"] for p in pts}


def z_at(pts, x, y):
    """Bilinear z on the regular model grid; None outside it."""
    xs = sorted({round(p["x"], 3) for p in pts})
    ys = sorted({round(p["y"], 3) for p in pts})
    if not (xs[0] <= x <= xs[-1] and ys[0] <= y <= ys[-1]):
        return None
    g = grid_of(pts)

    def bracket(v, arr):
        for i in range(len(arr) - 1):
            if arr[i] <= v <= arr[i + 1]:
                return i
        return len(arr) - 2

    i, j = bracket(x, xs), bracket(y, ys)
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[j], ys[j + 1]
    tx = (x - x0) / (x1 - x0) if x1 > x0 else 0
    ty = (y - y0) / (y1 - y0) if y1 > y0 else 0
    return ((g[(x0, y0)] * (1 - tx) + g[(x1, y0)] * tx) * (1 - ty)
            + (g[(x0, y1)] * (1 - tx) + g[(x1, y1)] * tx) * ty)


def read_events():
    """Plane thickness readings, lid openings and curved measurement ends from Studio's newest two logs.

    The plugin writes these log lines in Chinese whatever the UI language (seen with the Chinese UI).
    """
    stamp = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\]")
    head = re.compile(r'enter:setLaserHead data=\{"x":([\d.]+),"y":([\d.]+)')
    plane, lids, curves, pending = [], [], [], None
    for path in sorted(glob.glob(os.path.join(ROOT, "logs", "*", "renderer.log")))[-2:]:
        for line in open(path, encoding="utf-8", errors="replace"):
            m = stamp.match(line)
            if not m or "%c" in line:
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
            if "startMeasurement x, y" in line:
                pending = {"t": t, "x": None, "y": None}
            elif pending and pending["x"] is None and "enter:setLaserHead" in line:
                h = head.search(line)
                if h:
                    pending["x"], pending["y"] = float(h.group(1)), float(h.group(2))
            elif "厚度矫正远景结果" in line and "thick: ," in line:  # plane auto-measure result
                if pending and pending["x"] is not None:
                    pending["T"] = float(line.split("thick: ,")[1].strip())
                    plane.append(pending)
                pending = None
            elif "[JS002]=> 盖子已打开" in line:  # lid opened
                lids.append(t)
            elif "getCurveData=>" in line:  # curved measurement finished
                curves.append(t)
    return plane, lids, curves


def reference(pts):
    """Plane readings on the same untouched board inside the model:
    (latest reading, model z there, median of z - thickness, curve time) or None."""
    plane, lids, curves = read_events()
    if not curves:
        return None
    tc = curves[-1]
    good = []
    for r in plane:
        lo, hi = min(r["t"], tc), max(r["t"], tc)
        if any(lo < t < hi for t in lids):
            continue
        z = z_at(pts, r["x"], r["y"])
        if z is not None:
            good.append((r, z, z - r["T"]))
    if not good:
        return None
    r, z, _ = good[-1]
    return r, z, statistics.median(d for _, _, d in good), tc


def evaluate(path, log):
    """(decision patch/ok/stop, offset, source, report lines)."""
    lines = []
    code_val, version, digest, note = code_offset()
    lines.append(f"plugin JS002 {version} ({digest[:19]}...): "
                 + (f"adds an extra {code_val} mm ({note})" if code_val is not None else note))
    _, pts = load_points(path)
    zs = [p["z"] for p in pts]
    lines.append(f"model {path}: z {min(zs):.3f}..{max(zs):.3f}, written "
                 + time.strftime("%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))))
    if sha(path) in corrected_hashes(log):
        return "ok", None, None, lines + ["already corrected"]
    ref = reference(pts)
    if ref:
        r, z, dev, _ = ref
        lines.append(f"plane reading {r['t']:%m-%d %H:%M:%S} at head ({r['x']:.1f}, {r['y']:.1f}): thickness "
                     f"{r['T']:.3f}; model z there {z:.3f}; measured offset {dev:+.3f} mm")
        if abs(dev) <= CLOSE:
            return "ok", None, None, lines + ["matches the plane reading, nothing to correct"]
        if abs(dev) > MAX_OFFSET:
            return "stop", None, None, lines + [f"offset {dev:+.3f} mm is too large to trust; check by hand"]
        if dev < 0:
            # model too low: correcting raises the head, so the measurement alone is enough
            return "patch", round(dev, 3), "measured against the plane reading (model too low, head goes up)", lines
        # model too high: correcting lowers the head, so the plugin's own number has to confirm it
        if code_val is None:
            return "stop", None, None, lines + [f"measured {dev:+.3f} mm but the plugin code changed, so it cannot "
                                                "be confirmed; lowering the head is not done automatically"]
        if abs(dev - code_val) > AGREE:
            return "stop", None, None, lines + [f"measured {dev:+.3f} mm but the plugin adds {code_val} mm; "
                                                "lowering the head is not done automatically"]
        return "patch", round(dev, 3), "measured against the plane reading", lines
    if os.path.exists(LAST) and log:
        last_off = log[-1].get("offset", 5.7)
        _, last = load_points(LAST)
        g_new, g_last = grid_of(pts), grid_of(last)
        if g_new.keys() == g_last.keys():
            d = statistics.median(g_new[k] - g_last[k] for k in g_new)
            lines.append(f"same grid as the last corrected model, median difference {d:+.3f} mm")
            if abs(d) < SAME:
                return "ok", None, None, lines + ["already corrected (Studio rewrote corrected values)"]
            if abs(d - last_off) < SAME:
                return "patch", last_off, "Studio rewrote the uncorrected values; same offset as last time", lines
    if code_val is not None:
        return "patch", code_val, "no plane reading; the plugin's own number", lines
    return "stop", None, None, lines + ["cannot tell the offset: measure the thickness once in plane mode "
                                        "(automation / 自动化 「启动」) without opening the lid, then measure the curve again"]


def apply_patch(path, log, offset, source):
    _, pts = load_points(path)
    zs = [p["z"] for p in pts]
    lo, hi = min(zs) - offset, max(zs) - offset
    if lo <= 0.5 or hi >= MAX_THICKNESS:
        return False, f"refused: z {min(zs):.3f}..{max(zs):.3f} minus {offset} gives {lo:.3f}..{hi:.3f}"
    os.makedirs(STATE, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(STATE, f"surface_{stamp}_original.txt")
    original = sha(path)
    shutil.copy2(path, backup)
    rows, _ = load_points(path)
    for r in rows:
        for q in r:
            q["z"] = round(q["z"] - offset, 6)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    os.replace(tmp, path)
    shutil.copy2(path, LAST)
    new_sha = sha(path)
    if path == CURVE:
        with open(CURVE + ".zfix.json", "w", encoding="utf-8") as f:
            json.dump({"sha256": new_sha, "offset": offset, "time": stamp}, f)
    _, pts2 = load_points(path)
    zs2 = [p["z"] for p in pts2]
    log.append({"time": stamp, "path": path, "backup": backup, "offset": offset, "source": source,
                "original_sha256": original, "patched_sha256": new_sha,
                "z_before": [min(zs), max(zs)], "z_after": [min(zs2), max(zs2)]})
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    return True, (f"corrected by {offset:+.3f} mm ({source}): z {min(zs):.3f}..{max(zs):.3f} -> "
                  f"{min(zs2):.3f}..{max(zs2):.3f}; backup {backup}")


def newest_task_gcode():
    gcodes = glob.glob(os.path.join(ATOMM, "preview", "*", "JS002", "task", "*-laser_gcode.txt"))
    return max(gcodes, key=os.path.getmtime) if gcodes else None


def verify_task(g, log):
    """(ok, lines) for one job; ok is None when it is not a curved job."""
    task = os.path.dirname(g)
    surf = os.path.join(task, "surface.txt")
    manifests = glob.glob(os.path.join(task, "*-laser_manifest.json"))
    # curved jobs carry MX_Z in the manifest (proc_mode is 2 for fills, 3 for bitmaps, so it is not used)
    if not (os.path.exists(surf) and manifests and "MX_Z" in open(manifests[0], encoding="utf-8").read()):
        return None, [f"not a curved job: {g}"]
    text = open(g, encoding="utf-8").read()
    used = sha(surf) in corrected_hashes(log)
    zs = [float(v) for v in re.findall(r"^G1[^\n ]*Z([\d.]+)", text, re.M) if float(v) > 0.5]
    date = re.search(r"# date=(\S+)", text)
    lines = [f"job {task}", f"built {date.group(1) if date else '?'}; model used: "
             + ("corrected" if used else "not marked as corrected")]
    if zs:
        lines.append(f"head Z on the job: {min(zs):.3f}..{max(zs):.3f}")
    _, pts = load_points(surf)
    ref = reference(pts)
    if ref:
        lines.append(f"against plane reading: model z {ref[1]:.3f} vs thickness {ref[0]['T']:.3f}, "
                     f"offset {ref[2]:+.3f} mm")
        return abs(ref[2]) <= CLOSE, lines
    return used, lines


def alert(text):
    def run():
        ctypes.windll.user32.MessageBoxW(0, text, "xTool M2 curved-surface Z fix", 0x30 | 0x40000)
    threading.Thread(target=run, daemon=True).start()


def pid_alive(pid):
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def handle_model(path):
    log = load_log()
    decision, offset, source, lines = evaluate(path, log)
    for l in lines:
        print(f"{now()}   {l}")
    if decision == "patch":
        done, msg = apply_patch(path, log, offset, source)
        print(f"{now()}   {msg}")
        if not done:
            alert(f"The curved model was NOT corrected / 曲面数据没有补偿:\n{msg}\n"
                  "Do not start the job / 先别开始加工.")
    elif decision == "stop":
        alert("The curved model was NOT corrected / 曲面数据没有自动补偿:\n" + lines[-1]
              + "\nDo not start the job / 先别开始加工.")
    return decision


def watch(path, tasks):
    os.makedirs(STATE, exist_ok=True)
    lock = os.path.join(STATE, "watch_" + hashlib.sha1(path.lower().encode()).hexdigest()[:8] + ".lock")
    if os.path.exists(lock):
        try:
            pid, beat = open(lock).read().split()
            if pid_alive(int(pid)) and time.time() - float(beat) < 30:
                print(f"{now()} another watcher is running (pid {pid}), exit")
                return
        except ValueError:
            pass
    print(f"{now()} watcher started, pid {os.getpid()}, model {path}")

    def sig(p):
        try:
            st = os.stat(p)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    last_model = sig(path)
    if last_model:
        print(f"{now()} start check: {handle_model(path)}")
        last_model = sig(path)
    last_task = newest_task_gcode() if tasks else None
    last_task_sig = sig(last_task) if last_task else None
    beat = 0
    while True:
        try:
            if time.time() - beat > 10:
                with open(lock, "w") as f:
                    f.write(f"{os.getpid()} {time.time()}")
                beat = time.time()
            s = sig(path)
            if s and s != last_model:
                time.sleep(1.0)  # let Studio finish writing
                if sig(path) == s:
                    print(f"{now()} new model: {handle_model(path)}")
                    last_model = sig(path)
            if tasks:
                g = newest_task_gcode()
                gs = sig(g) if g else None
                if g and (g, gs) != (last_task, last_task_sig):
                    time.sleep(1.0)
                    if sig(g) == gs:
                        ok, lines = verify_task(g, load_log())
                        for l in lines:
                            print(f"{now()}   {l}")
                        if ok is False:
                            alert("This curved job does not use a corrected height; the focus will be off.\n"
                                  "这单曲面任务的高度没有补偿对，焦点会偏。\n"
                                  "Do not start: go back (返回), wait a few seconds, then click Process (加工) again.")
                        print(f"{now()} new job: {'not curved' if ok is None else ('OK' if ok else 'NOT OK')}")
                        last_task, last_task_sig = g, sig(g)
        except Exception as e:  # keep watching whatever happens
            print(f"{now()} error: {e!r}")
        time.sleep(0.5)


def install():
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        sys.exit(f"pythonw.exe not found next to {sys.executable}")
    script = os.path.abspath(__file__)
    # a UTF-16 .vbs runs pythonw hidden and copes with non-ASCII folder names
    with open(STARTUP, "w", encoding="utf-16") as f:
        f.write(f'CreateObject("WScript.Shell").Run """{pythonw}"" -I ""{script}"" watch", 0, False\r\n')
    subprocess.Popen([pythonw, "-I", script, "watch"], close_fds=True)
    print(f"installed: {STARTUP}\nwatcher started; log: {WATCH_LOG}")


def uninstall():
    if os.path.exists(STARTUP):
        os.remove(STARTUP)
        print(f"removed: {STARTUP}")
    else:
        print("no Startup entry found")
    print("a running watcher keeps going until you end pythonw.exe (curve_z_fix.py watch) or log off")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    path = args[args.index("--surface") + 1] if "--surface" in args else CURVE
    if cmd == "watch":
        os.makedirs(STATE, exist_ok=True)
        out = open(WATCH_LOG, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = out
        watch(path, "--no-tasks" not in args)
        return
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # consoles that cannot show Chinese still work
    if cmd == "install":
        install()
        return
    if cmd == "uninstall":
        uninstall()
        return
    log = load_log()
    if cmd in ("check", "fix"):
        decision, offset, source, lines = evaluate(path, log)
        print("\n".join(lines))
        if cmd == "check":
            print(f"DECISION: {decision}" + (f" by {offset:+.3f} mm ({source})" if decision == "patch" else ""))
        elif decision == "patch":
            if "--dry-run" in args:
                print(f"RESULT (dry run): would correct by {offset:+.3f} mm ({source})")
            else:
                done, msg = apply_patch(path, log, offset, source)
                print(("RESULT: " if done else "STOP: ") + msg)
        elif decision == "ok":
            print("RESULT: nothing to do")
        else:
            sys.exit("STOP: " + lines[-1])
    elif cmd == "verify":
        g = newest_task_gcode()
        if not g:
            sys.exit("no job found")
        ok, lines = verify_task(g, log)
        print("\n".join(lines))
        print("RESULT: " + {None: "not a curved job", True: "OK, the job focuses like plane mode",
                            False: "NOT OK - go back (返回), run fix, then click Process (加工) again"}[ok])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
