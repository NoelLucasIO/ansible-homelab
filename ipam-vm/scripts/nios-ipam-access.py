#!/usr/bin/env python3
"""Check, and with --apply create, the NIOS objects ipam-vm needs:

  - the IPv4 network VMs are allocated from
  - optionally a reserved range NIOS must not hand out (e.g. a DHCP pool served elsewhere)
  - an admin group that may use the API only (no GUI/CLI) and is not a superuser
  - Read/Write permissions for that group on the network and its forward/reverse zones
  - the API user (nios_api_user) in that group
  - optional Owner / Role extensible attributes (String)

Then it tests the API user: next_available_ip must work, a Grid backup must be refused.
Useful on a new appliance and after restoring an older Grid backup, which drops
anything created after that backup was taken.

  nios-ipam-access.py --wapi https://192.0.2.25/wapi/v2.12 --network 192.0.2.0/24 \\
      --zone example.com --reserved 192.0.2.51-192.0.2.200          # report only
  ... --apply                                                       # create what's missing

Prompts for the NIOS admin password, and for the API user's password when it has to
create or test that user. Standard library only.
"""
import argparse
import base64
import getpass
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--wapi", default="https://192.0.2.25/wapi/v2.12", help="WAPI base URL")
p.add_argument("--admin", default="admin", help="NIOS superuser used to make the changes")
p.add_argument("--network", default="192.0.2.0/24", help="IPv4 network VMs are allocated from")
p.add_argument("--zone", action="append", default=[],
               help="authoritative zone the API user may write (repeatable; default: example.com "
                    "and the network's reverse zone, which WAPI names by CIDR)")
p.add_argument("--reserved", default="", help="reserved range START-END inside the network (optional)")
p.add_argument("--group", default="api-ipam", help="admin group for the API user")
p.add_argument("--user", default="aap-ipam", help="API user (ipam-vm's nios_api_user)")
p.add_argument("--no-extattrs", action="store_true", help="skip the Owner/Role extensible attributes")
p.add_argument("--verify-tls", action="store_true", help="verify the appliance certificate")
p.add_argument("--apply", action="store_true", help="create missing objects (default: report only)")
args = p.parse_args()
zones = args.zone or ["example.com", args.network]
ctx = ssl.create_default_context() if args.verify_tls else ssl._create_unverified_context()
q = urllib.parse.quote


def call(method, path, body=None, user=None, pw=None):
    req = urllib.request.Request(f"{args.wapi}/{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


admin_pw = getpass.getpass(f"NIOS {args.admin} password: ")


def A(method, path, body=None):
    return call(method, path, body, args.admin, admin_pw)


def found(res):
    return isinstance(res, list) and len(res) > 0


missing = 0


def ensure(label, present, create):
    global missing
    if present:
        print(f"  ok       {label}")
        return True
    if not args.apply:
        print(f"  MISSING  {label}")
        missing += 1
        return False
    st, res = create()
    good = st in (200, 201)
    print(f"  {'created ' if good else 'FAILED  '} {label}" + ("" if good else f"  ({st}: {res})"))
    missing += not good
    return good


st, nets = A("GET", f"network?network={q(args.network)}")
if st != 200:
    sys.exit(f"WAPI login as {args.admin} failed: {st} {nets}")

print(f"NIOS objects at {args.wapi}:")
ensure(f"network {args.network}", found(nets),
       lambda: A("POST", "network", {"network": args.network, "comment": "ipam-vm"}))
_, nets = A("GET", f"network?network={q(args.network)}")

if args.reserved:
    start, end = args.reserved.split("-")
    _, rng = A("GET", f"range?start_addr={start}&end_addr={end}")
    ensure(f"reserved range {start}-{end}", found(rng), lambda: A("POST", "range", {
        "start_addr": start, "end_addr": end, "network": args.network,
        "server_association_type": "NONE", "comment": "reserved: not for ipam-vm"}))

_, grp = A("GET", f"admingroup?name={args.group}&_return_fields=name,access_method,superuser")
ensure(f"admin group {args.group} (API only, not superuser)", found(grp), lambda: A("POST", "admingroup", {
    "name": args.group, "access_method": ["API"], "superuser": False,
    "comment": "ipam-vm: IPAM and DNS records only"}))
if found(grp):
    g = grp[0]
    if g.get("superuser") or "GUI" in g.get("access_method", []):
        print(f"  WARNING  {args.group}: superuser={g.get('superuser')} access_method={g.get('access_method')}")

targets = [(f"network {args.network}", nets[0]["_ref"] if found(nets) else None)]
for z in zones:
    _, zr = A("GET", f"zone_auth?fqdn={q(z)}&view=default")
    targets.append((f"zone {z}", zr[0]["_ref"] if found(zr) else None))
_, perms = A("GET", f"permission?group={args.group}&_return_fields=object,permission,resource_type")
perms = perms if isinstance(perms, list) else []
for label, ref in targets:
    if not ref:
        print(f"  MISSING  {label} (the object itself does not exist)")
        missing += 1
        continue
    have = any(x.get("object") == ref and x.get("permission") == "WRITE" and not x.get("resource_type")
               for x in perms)
    ensure(f"{args.group} Read/Write on {label}", have,
           lambda ref=ref: A("POST", "permission", {"group": args.group, "object": ref, "permission": "WRITE"}))

_, usr = A("GET", f"adminuser?name={args.user}&_return_fields=name,admin_groups")
user_pw = None


def make_user():
    global user_pw
    user_pw = getpass.getpass(f"Password for {args.user} (ipam-vm's nios_api_password): ")
    return A("POST", "adminuser", {"name": args.user, "password": user_pw,
                                    "admin_groups": [args.group], "comment": "ipam-vm API account"})


ensure(f"admin user {args.user} in {args.group}", found(usr), make_user)

if not args.no_extattrs:
    for ea in ("Owner", "Role"):
        _, d = A("GET", f"extensibleattributedef?name={ea}")
        ensure(f"extensible attribute {ea} (optional)", found(d),
               lambda ea=ea: A("POST", "extensibleattributedef", {"name": ea, "type": "STRING"}))

if found(usr) or user_pw:
    print(f"\nTesting {args.user}:")
    user_pw = user_pw or getpass.getpass(f"Password for {args.user} (to test it): ")
    _, nets = A("GET", f"network?network={q(args.network)}")
    st, r = call("POST", f"{nets[0]['_ref']}?_function=next_available_ip", {"num": 1}, args.user, user_pw)
    print(f"  next_available_ip: {st} {r}")
    st, _ = call("POST", "fileop?_function=getgriddata", {"type": "BACKUP"}, args.user, user_pw)
    print(f"  Grid backup (must be refused): {'refused, good' if st != 200 else 'ALLOWED: the group is too broad'}")

if not args.apply and missing:
    print("\nReport only. Rerun with --apply to create what's missing.")
sys.exit(1 if missing else 0)
