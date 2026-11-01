"""Logic for the kismet-* tools, importable for tests. Each function returns a
schema v1 document (dict); the tool scripts just emit() the result."""
import time

import kismet_client as kc
from flipper_tool import fit, item, kv, list_, status, text

ERR_TITLE = "Kismet"


def err(e):
    level = "warn" if e.kind in ("down", "config") else "error"
    return status(level, str(e), title=ERR_TITLE)


def status_doc(client=None, unit_state=None, now=None, log_glob=None, log_dir=None):
    now = now or time.time()
    state = unit_state or kc.unit_active()
    size, free = kc.log_usage(log_glob, log_dir)
    pairs = [("Service", state)]
    try:
        c = client or kc.Client()
        st = c.system_status()
        start = st.get("kismet.system.timestamp.start_sec") or 0
        pairs.append(("Uptime", kc.human_secs(now - int(start)) if start else "?"))
        pairs.append(("Devices", str(st.get("kismet.system.devices.count", "?"))))
        rate = c.packet_rate()
        pairs.append(("Packets/s", "?" if rate is None else str(rate)))
        srcs = c.datasources()
        if not srcs:
            pairs.append(("Sources", "none"))
        for s in srcs[:6]:
            name = s.get("kismet.datasource.name") or s.get("kismet.datasource.interface") or "?"
            if s.get("kismet.datasource.error"):
                st_ = "ERROR"
            elif s.get("kismet.datasource.running"):
                ch = s.get("kismet.datasource.channel") or ""
                st_ = "up" + (f" ch{ch}" if ch else "")
            else:
                st_ = "down"
            pairs.append((fit("Src " + str(name), 20), st_))
    except kc.KismetError as e:
        pairs.append(("API", fit(str(e), 40)))
    except (AttributeError, TypeError, ValueError):
        pairs.append(("API", "unexpected reply"))
    pairs.append(("Log size", kc.human_bytes(size)))
    pairs.append(("Free disk", kc.human_bytes(free)))
    return kv(pairs[:16], title="Kismet status")


def devices_doc(sort="last_seen", limit=10, client=None, cache_path=None, now=None):
    if sort not in ("last_seen", "signal"):
        raise ValueError("sort")
    limit = max(1, min(int(limit), 20))
    try:
        devs = (client or kc.Client()).devices()
    except kc.KismetError as e:
        return err(e)
    devs = [d for d in kc.sort_devices(devs, sort)
            if isinstance(d.get(kc.F + "key"), str) and kc.KEY_RE.match(d[kc.F + "key"])][:limit]
    if not devs:
        return status("ok", "No devices seen in the last hour", title="Kismet devices")
    kc.write_cache([d[kc.F + "key"] for d in devs], cache_path, now)
    items = []
    for i, d in enumerate(devs):
        s = kc.sig(d)
        suffix = f" {s}" if s is not None else ""
        items.append(item(fit(kc.dname(d), 32 - len(suffix)) + suffix, "kismet-device", idx=i))
    return list_(items, title="Kismet devices")


def device_doc(idx, mac="masked", client=None, cache_path=None, now=None):
    if mac not in ("masked", "full"):
        raise ValueError("mac")
    try:
        key = kc.read_cache_key(int(idx), cache_path, now)
        d = (client or kc.Client()).device(key)
    except kc.KismetError as e:
        return err(e)
    s = kc.sig(d)
    pairs = [("Type", d.get(kc.F + "type") or "?"),
             ("Name", kc.dname(d)),
             ("MAC", kc.mask_mac(d.get(kc.F + "macaddr"), mac)),
             ("Channel", d.get(kc.F + "channel") or "?"),
             ("Signal", f"{s} dBm" if s is not None else "?"),
             ("Manuf", d.get(kc.F + "manuf") or "?"),
             ("First seen", kc.ago(d.get(kc.F + "first_time"), now)),
             ("Last seen", kc.ago(d.get(kc.F + "last_time"), now)),
             ("Packets", str(d.get(kc.F + "packets.total", "?")))]
    return kv([(a, str(b)) for a, b in pairs], title="Kismet device")


def alerts_doc(client=None, now=None):
    try:
        alerts = (client or kc.Client()).alerts()
    except kc.KismetError as e:
        return err(e)
    if not alerts:
        return status("ok", "No Kismet alerts", title="Kismet alerts")
    alerts.sort(key=lambda a: -float(a.get("kismet.alert.timestamp") or 0))
    lines = []
    for a in alerts[:16]:
        hdr = a.get("kismet.alert.header") or "ALERT"
        lines.append(fit(f"{kc.ago(int(float(a.get('kismet.alert.timestamp') or 0)), now)} {hdr}", 64))
        txt = str(a.get("kismet.alert.text") or "")
        for i in range(0, min(len(txt), 128), 62):
            lines.append("  " + txt[i:i + 62])
    return text(lines[:64], title="Kismet alerts")
