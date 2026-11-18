# vigil-gvm.py: run by gvm-script inside the gvm-tools container.
#   setup   create a Greenbone target, weekly schedule and task per scope (idempotent)
#   export  write each scope's newest finished report to /reports/<scope>.xml
# Scopes come from /vigil-conf/gvm-scopes.json (rendered by Ansible from vigil_scopes).
# gvm-script provides the globals `gmp` (authenticated GMP connection) and `args`.
import json
import sys
from pathlib import Path

from lxml import etree

CONF = json.loads(Path("/vigil-conf/gvm-scopes.json").read_text())
PREFIX = "vigil-"

# Well-known IDs shipped in the Greenbone community feed
FULL_AND_FAST = "daba56c8-73ec-11df-a475-002264764cea"
OPENVAS_SCANNER = "08b69003-5fc2-4037-a479-93b440211c73"
ALL_IANA_TCP = "33d0cd82-57c6-11e1-8ed1-406186ea4fc5"
XML_FORMAT = "a994b278-1f62-11e1-96ac-406186ea4fc5"

NOT_READY = 2  # systemd retries vigil-gb-setup until the feed is loaded


def log(msg):
    print(f"vigil-gvm: {msg}", flush=True)


def by_name(items, name):
    """Exact-name match on a get_* response (GMP filters are substring-ish)."""
    return [i for i in items if i.findtext("name") == name]


def alive_test():
    """ICMP + TCP-ACK + ARP ping; the enum moved between python-gvm releases."""
    for mod in ("gvm.protocols.gmp.requests.v224", "gvm.protocols.gmp.requests",
                "gvm.protocols.gmpv224", "gvm.protocols.gmpv208"):
        try:
            module = __import__(mod, fromlist=["AliveTest"])
            return module.AliveTest.ICMP_TCP_ACK_SERVICE_AND_ARP_PING
        except (ImportError, AttributeError):
            continue
    types = getattr(gmp, "types", None)  # noqa: F821 (gvm-script global)
    return getattr(getattr(types, "AliveTest", None), "ICMP_TCP_ACK_SERVICE_AND_ARP_PING", None)


def ical(byday, hour):
    return "\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//vigil//EN",
        "BEGIN:VEVENT",
        f"DTSTART:20260104T{int(hour):02d}0000",
        f"RRULE:FREQ=WEEKLY;BYDAY={byday}",
        f"UID:vigil-{byday}-{hour}@vigil",
        "DTSTAMP:20260101T000000Z",
        "END:VEVENT", "END:VCALENDAR", ""])


def setup():
    g = gmp  # noqa: F821
    if not g.get_scan_configs(filter_string=f"uuid={FULL_AND_FAST}").xpath("config"):
        log("feed not loaded into gvmd yet (no 'Full and fast' scan config); will retry")
        sys.exit(NOT_READY)
    if not g.get_port_lists(filter_string=f"uuid={ALL_IANA_TCP}").xpath("port_list"):
        log("feed not loaded into gvmd yet (no 'All IANA assigned TCP' port list); will retry")
        sys.exit(NOT_READY)

    at = alive_test()
    for scope in CONF["scopes"]:
        name = PREFIX + scope["name"]
        hosts, exclude = scope["hosts"], CONF.get("exclude", [])

        target = by_name(g.get_targets(filter_string=f"name={name} rows=-1").xpath("target"), name)
        if target:
            target_id = target[0].get("id")
            have = sorted(h.strip() for h in (target[0].findtext("hosts") or "").split(","))
            if have != sorted(hosts):
                log(f"{name}: target hosts are {have}, vars say {sorted(hosts)}; "
                    "Greenbone can't change hosts on a target in use, delete the task+target in GSA to rebuild")
        else:
            kwargs = {"hosts": hosts, "port_list_id": ALL_IANA_TCP}
            if exclude:
                kwargs["exclude_hosts"] = exclude
            if at is not None:
                kwargs["alive_test"] = at
            target_id = g.create_target(name, **kwargs).get("id")
            log(f"{name}: created target {hosts}")

        sched = scope["gvm_schedule"]
        sched_name = f"{name}-weekly"
        found = by_name(g.get_schedules(filter_string=f"name={sched_name} rows=-1").xpath("schedule"), sched_name)
        if found:
            sched_id = found[0].get("id")
        else:
            sched_id = g.create_schedule(sched_name, ical(sched["byday"], sched["hour"]),
                                         CONF["timezone"]).get("id")
            log(f"{name}: created schedule {sched['byday']} {sched['hour']}:00 {CONF['timezone']}")

        if by_name(g.get_tasks(filter_string=f"name={name} rows=-1").xpath("task"), name):
            log(f"{name}: task exists")
            continue
        g.create_task(name, FULL_AND_FAST, target_id, OPENVAS_SCANNER, schedule_id=sched_id,
                      comment="vigil: managed by Ansible")
        log(f"{name}: created task (Full and fast, weekly)")


def export():
    g = gmp  # noqa: F821
    state = Path("/reports/.exported")
    state.mkdir(exist_ok=True)
    for task in g.get_tasks(filter_string=f"name~{PREFIX} rows=-1").xpath("task"):
        name = task.findtext("name") or ""
        if not name.startswith(PREFIX):
            continue
        scope = name[len(PREFIX):]
        report = task.find("last_report/report")
        if report is None:
            log(f"{name}: no finished report yet")
            continue
        rid = report.get("id")
        marker = state / scope
        if marker.exists() and marker.read_text().strip() == rid:
            log(f"{name}: report {rid} already exported")
            continue
        resp = g.get_report(rid, report_format_id=XML_FORMAT, ignore_pagination=True, details=True,
                            filter_string="apply_overrides=0 min_qod=70 levels=hml rows=-1")
        Path(f"/reports/{scope}.xml").write_bytes(etree.tostring(resp.find("report")))
        marker.write_text(rid)
        log(f"{name}: exported report {rid}")


ACTIONS = {"setup": setup, "export": export}
action = args.script[1] if len(args.script) > 1 else ""  # noqa: F821
if action not in ACTIONS:
    log(f"usage: vigil-gvm.py {{{'|'.join(ACTIONS)}}}")
    sys.exit(64)
ACTIONS[action]()
