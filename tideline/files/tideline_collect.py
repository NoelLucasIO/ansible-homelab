#!/usr/bin/python3
# Tideline collector: what can be updated on this host, read-only.
#
# Prints one JSON document on stdout. Nothing is installed, upgraded or removed; the only
# writes are package-manager metadata caches (dnf makecache, apt-get update, a private
# pacman sync db for checkupdates, fwupd metadata).
#
# Sources, each optional and isolated (one failing never hides the others):
#   dnf      Fedora / RHEL / CentOS Stream: dnf5 JSON advisories, or dnf4 updateinfo.xml
#   pacman   Arch / CachyOS: checkupdates, security.archlinux.org advisories
#   aur      foreign packages vs the AUR RPC (VCS -git packages skipped)
#   apt      Debian / Ubuntu / Proxmox: apt-get -s, security origins, top changelog entry
#   flatpak  system installation
#   fwupd    device firmware (LVFS)
#   container  running Podman containers (root and rootless users) vs their registry:
#            same tag rebuilt (digest) or a newer tag of the same shape (0.17.2-tensorrt ->
#            0.18.0-tensorrt), with GitHub release notes when the image says where it lives.
#            Registry checks use HEAD and tag lists, which Docker Hub doesn't count as pulls.
#
# Standard library only; Python 3.6+.
import argparse
import bz2
import glob
import gzip
import hashlib
import json
import lzma
import os
import platform
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "tideline/1.0 (+https://github.com/NoelLucasIO/ansible-homelab)"
SEVERITY = {
    "critical": "critical", "urgent": "critical",
    "important": "high", "high": "high",
    "moderate": "medium", "medium": "medium",
    "low": "low",
}
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}")
DEBUG = bool(os.environ.get("TIDELINE_DEBUG"))


def norm_severity(value):
    return SEVERITY.get((value or "").strip().lower())


def run(cmd, env=None, timeout=900, stdin=None):
    """(rc, stdout, stderr); never raises for a missing binary or a timeout."""
    full_env = dict(os.environ)
    full_env.update({"LC_ALL": "C", "LANG": "C"})
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=full_env, timeout=timeout, input=stdin)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "%s: not found" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "%s: timed out after %ss" % (cmd[0], timeout)


def http(url, method="GET", headers=None, timeout=30, data=None):
    """(status, headers, body bytes); HTTP errors are returned, not raised."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=h, data=data)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        res = e.code, e.headers, e.read() if e.fp else b""
    if DEBUG:
        sys.stderr.write("%6.2fs %s %s %s\n" % (time.time() - t0, res[0], method, url[:160]))
    return res


def http_json(url, headers=None, timeout=30):
    status, _, body = http(url, headers=headers, timeout=timeout)
    if status != 200:
        raise RuntimeError("HTTP %s from %s" % (status, url))
    return json.loads(body.decode("utf-8", "replace"))


def clip(text, limit):
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def strip_html(text):
    text = re.sub(r"<\s*(br|/p|/li)\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def os_release():
    data = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    if k:
                        data[k] = v.strip().strip('"')
            break
    return data


def boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return int(line.split()[1])
    return 0


def kernel_reboot_heuristic():
    """True when the running kernel's modules are gone (it was upgraded since boot), or a
    vmlinuz newer than the boot exists for another kernel."""
    rel = platform.release()
    if os.path.isdir("/usr/lib/modules") and not os.path.isdir("/usr/lib/modules/" + rel):
        return True
    btime = boot_time()
    for k in glob.glob("/boot/vmlinuz-*"):
        if not k.endswith(rel) and "rescue" not in k and os.path.getmtime(k) > btime + 60:
            return True
    return False


def new_row(source, name, **kw):
    row = {"source": source, "name": name, "arch": "", "installed_version": None,
           "installed_at": None, "new_version": None, "repo": None, "summary": None,
           "security": False, "severity": None, "advisory_id": None, "advisory_type": None,
           "advisory_title": None, "advisory_text": None, "advisory_url": None,
           "advisory_issued": None, "advisories": [], "cves": [], "package_url": None,
           "changes_url": None}
    row.update(kw)
    return row


# --------------------------------------------------------------------------- rpm / dnf
def rpmvercmp(a, b):
    """rpm's version segment comparison (pure Python; used when the rpm module isn't there)."""
    if a == b:
        return 0
    seg = re.compile(r"(~|\^|[0-9]+|[A-Za-z]+)")
    xa, xb = seg.findall(a), seg.findall(b)
    i = 0
    while True:
        ta = xa[i] if i < len(xa) else None
        tb = xb[i] if i < len(xb) else None
        i += 1
        if ta == "~" or tb == "~":                  # ~ sorts before anything, even the end
            if ta != tb:
                return -1 if ta == "~" else 1
            continue
        if ta == "^" or tb == "^":                  # ^ sorts after the end, before anything else
            if ta is None:
                return -1
            if tb is None:
                return 1
            if ta != tb:
                return -1 if ta == "^" else 1
            continue
        if ta is None or tb is None:
            return 0 if ta == tb else (-1 if ta is None else 1)
        if ta.isdigit() != tb.isdigit():
            return 1 if ta.isdigit() else -1
        if ta.isdigit():
            ia, ib = int(ta), int(tb)
            if ia != ib:
                return 1 if ia > ib else -1
        elif ta != tb:
            return 1 if ta > tb else -1


try:
    import rpm as _rpm  # noqa: E402

    def evr_cmp(a, b):
        return _rpm.labelCompare(a, b)
except ImportError:  # pragma: no cover
    def evr_cmp(a, b):
        for x, y in zip(a, b):
            c = rpmvercmp(str(x or "0"), str(y or "0"))
            if c:
                return c
        return 0


def evr_str(e, v, r):
    e = "" if str(e) in ("", "0", "(none)", "None") else "%s:" % e
    return "%s%s-%s" % (e, v, r)


def rpm_installed():
    fmt = "%{NAME}\t%{ARCH}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{INSTALLTIME}\t%{SOURCERPM}\t%{SUMMARY}\t%{URL}\n"
    rc, out, err = run(["rpm", "-qa", "--qf", fmt])
    if rc != 0:
        raise RuntimeError("rpm -qa: " + err.strip())
    inst = {}
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 9:
            continue
        key = (f[0], f[1])
        evr = (f[2], f[3], f[4])
        cur = inst.get(key)
        if cur is None or evr_cmp(evr, cur["evr"]) > 0:      # install-only (kernel): newest
            inst[key] = {"evr": evr, "time": int(f[5] or 0), "srpm": f[6], "summary": f[7], "url": f[8]}
    return inst


def srpm_name(srpm):
    return srpm.rsplit("-", 2)[0] if srpm and srpm.count("-") >= 2 else srpm


def rpm_links(osr, src, name, upstream):
    distro = osr.get("ID", "")
    ver = osr.get("VERSION_ID", "")
    if distro == "fedora" and src:
        return ("https://packages.fedoraproject.org/pkgs/%s/%s/" % (src, name),
                "https://src.fedoraproject.org/rpms/%s/commits/f%s" % (src, ver))
    if distro == "centos" and src:
        return (upstream or None,
                "https://gitlab.com/redhat/centos-stream/rpms/%s/-/commits/c%ss" % (src, ver.split(".")[0]))
    return (upstream or None, None)


def advisory_url(adv_id, refs):
    for r in refs:
        if r.get("type") == "self" and r.get("href"):
            return r["href"]
    if adv_id.startswith("FEDORA-"):
        return "https://bodhi.fedoraproject.org/updates/" + adv_id
    if re.match(r"^(RH|AL)[SBE]A-", adv_id):
        return "https://access.redhat.com/errata/" + adv_id
    return None


def dnf_collect(args, osr, out):
    dnf = shutil.which("dnf5") or shutil.which("dnf") or shutil.which("yum")
    if not dnf:
        return False
    is5 = os.path.basename(os.path.realpath(dnf)).startswith("dnf5")
    base = [dnf, "-q"] +["--repo=%s" % r for r in args.dnf_repo]
    if not args.no_refresh:
        mk = base + ["--refresh", "makecache"]
        if is5:
            mk.insert(3, "--setopt=optional_metadata_types=comps,updateinfo")
        rc, _, err = run(mk, timeout=1800)
        if rc != 0:
            out["errors"].append("dnf makecache: rc=%s %s" % (rc, err.strip()[-400:]))
    inst = rpm_installed()
    repo_tag = "%{repo_id}" if is5 else "%{repoid}"
    qf = "\x1f".join(["%{name}", "%{epoch}", "%{version}", "%{release}", "%{arch}", repo_tag,
                      "%{sourcerpm}", "%{summary}", "%{url}"]) + ("\n" if is5 else "")
    rc, stdout, err = run(base + ["repoquery", "--upgrades", "--latest-limit=1", "--qf", qf], timeout=1800)
    if rc != 0:
        raise RuntimeError("dnf repoquery --upgrades: rc=%s %s" % (rc, err.strip()[-400:]))
    cands = {}
    for line in stdout.splitlines():
        f = line.split("\x1f")
        if len(f) < 9:
            continue
        name, epoch, ver, rel, arch, repo, srpm, summ, url = f[:9]
        key = (name, arch)
        if key not in inst:
            alt = [k for k in inst if k[0] == name]
            if not alt:
                continue
            key = alt[0]
        i = inst[key]
        new = (epoch or "0", ver, rel)
        if evr_cmp(new, i["evr"]) <= 0:
            continue
        prev = cands.get(key)
        if prev and evr_cmp(new, prev["_new"]) <= 0:
            continue
        src = srpm_name(srpm) or srpm_name(i["srpm"])
        purl, curl = rpm_links(osr, src, name, url or i["url"])
        cands[key] = new_row(
            "dnf", name, arch=key[1], installed_version=evr_str(*i["evr"]), installed_at=i["time"],
            new_version=evr_str(*new), repo=repo, summary=summ or i["summary"],
            package_url=purl, changes_url=curl, _new=new, _src=src)
    if cands:
        if is5:
            dnf5_advisories(base, cands, inst, args)
        else:
            dnf4_advisories(cands, inst, args)
    for row in cands.values():
        row.pop("_new", None)
        row.pop("_src", None)
        if row["advisories"]:
            sec = [a for a in row["advisories"] if a["type"] == "security"]
            rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, None: 0}
            pick = max(sec or row["advisories"], key=lambda a: (rank[a.get("severity")], a.get("issued") or 0))
            row.update(security=bool(sec), severity=pick.get("severity") if sec else None,
                       advisory_id=pick["id"], advisory_type=pick["type"], advisory_title=pick.get("title"),
                       advisory_text=pick.get("text"), advisory_url=pick.get("url"),
                       advisory_issued=pick.get("issued"))
            row["cves"] = sorted({c for a in row["advisories"] for c in a.get("cves", [])})
            if row["advisory_url"] and not row["changes_url"]:
                row["changes_url"] = row["advisory_url"]
        out["updates"].append(row)
    rc, _, _ = run([dnf, "-q", "needs-restarting", "-r"], timeout=300)
    out["reboot_required"] = rc == 1 if rc in (0, 1) else kernel_reboot_heuristic()
    out["managers"].append("dnf5" if is5 else "dnf")
    return True


def nevra_split(nevra):
    m = re.match(r"^(.+)-(?:(\d+):)?([^-]+)-([^-]+)\.([^.]+)$", nevra)
    if not m:
        return None
    name, epoch, ver, rel, arch = m.groups()
    return name, arch, (epoch or "0", ver, rel)


def attach_advisory(cands, inst, name, arch, evr, adv):
    """An advisory covers a candidate when its package is newer than what's installed and
    not newer than the candidate (skipped intermediate updates count too)."""
    key = (name, arch)
    row = cands.get(key)
    if row is None and arch == "noarch":
        hits = [k for k in cands if k[0] == name]
        key = hits[0] if hits else None
        row = cands.get(key) if key else None
    if row is None:
        return
    if evr_cmp(evr, inst[key]["evr"]) <= 0 or evr_cmp(evr, row["_new"]) > 0:
        return
    if adv["id"] not in [a["id"] for a in row["advisories"]]:
        row["advisories"].append(adv)


def dnf5_advisories(base, cands, inst, args):
    rc, stdout, err = run(base + ["advisory", "list", "--updates", "--json"], timeout=900)
    if rc != 0:
        raise RuntimeError("dnf advisory list: " + err.strip()[-300:])
    pairs = []
    for a in json.loads(stdout or "[]"):
        p = nevra_split(a.get("nevra", ""))
        if p:
            pairs.append((a["name"], p))
    ids = sorted({i for i, _ in pairs})
    info = {}
    for chunk in [ids[i:i + 50] for i in range(0, len(ids), 50)]:
        rc, stdout, err = run(base + ["advisory", "info", "--json", "--all"] + chunk, timeout=900)
        if rc == 0:
            for a in json.loads(stdout or "[]"):
                info[a.get("Name")] = a
    for adv_id, (name, arch, evr) in pairs:
        a = info.get(adv_id, {})
        refs = [{"type": (r.get("Type") or r.get("type") or "").lower(),
                 "href": r.get("Url") or r.get("href") or r.get("URL"),
                 "id": r.get("Id") or r.get("id") or r.get("ID")}
                for r in (a.get("references") or a.get("References") or []) if isinstance(r, dict)]
        text = a.get("Description") or ""
        cves = sorted({r["id"] for r in refs if r["type"] == "cve" and r["id"]}
                      | set(CVE_RE.findall(text + " " + (a.get("Title") or ""))))
        adv = {"id": adv_id, "type": (a.get("Type") or "").lower() or "unspecified",
               "severity": norm_severity(a.get("Severity")), "title": a.get("Title"),
               "text": clip(text, args.max_text), "url": advisory_url(adv_id, refs),
               "issued": a.get("Issued"), "cves": cves}
        attach_advisory(cands, inst, name, arch, evr, adv)


def open_compressed(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    if path.endswith(".bz2"):
        return bz2.open(path, "rb")
    if path.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14+
            return zstd.open(path, "rb")
        except ImportError:
            rc, _, _ = run(["zstd", "--version"])
            if rc != 0:
                return None
            tmp = tempfile.TemporaryFile()
            subprocess.run(["zstd", "-dc", path], stdout=tmp, check=True)
            tmp.seek(0)
            return tmp
    if path.endswith(".xml"):
        return open(path, "rb")
    return None


def dnf4_advisories(cands, inst, args):
    names = {k[0] for k in cands}
    files = []
    for d in ("/var/cache/dnf", os.path.expanduser("~/.cache/dnf"), "/var/tmp"):
        files += glob.glob(os.path.join(d, "*", "repodata", "*updateinfo.xml*"))
    for path in files:
        fh = open_compressed(path)
        if fh is None:
            continue
        with fh:
            for _, el in ET.iterparse(fh):
                if el.tag != "update":
                    continue
                pkgs = []
                for p in el.iter("package"):
                    if p.get("name") in names:
                        pkgs.append((p.get("name"), p.get("arch"),
                                     (p.get("epoch") or "0", p.get("version"), p.get("release"))))
                if pkgs:
                    adv_id = (el.findtext("id") or "").strip()
                    refs = [{"type": r.get("type"), "href": r.get("href"), "id": r.get("id")}
                            for r in el.iter("reference")]
                    issued = el.find("issued")
                    ts = None
                    if issued is not None and issued.get("date"):
                        try:
                            ts = int(time.mktime(time.strptime(issued.get("date")[:19], "%Y-%m-%d %H:%M:%S")))
                        except ValueError:
                            ts = None
                    text = el.findtext("description") or ""
                    adv = {"id": adv_id, "type": el.get("type") or "unspecified",
                           "severity": norm_severity(el.findtext("severity")),
                           "title": el.findtext("title"), "text": clip(text, args.max_text),
                           "url": advisory_url(adv_id, refs), "issued": ts,
                           "cves": sorted({r["id"] for r in refs if r["type"] == "cve" and r["id"]}
                                          | set(CVE_RE.findall(text)))}
                    for name, arch, evr in pkgs:
                        attach_advisory(cands, inst, name, arch, evr, adv)
                el.clear()


# --------------------------------------------------------------------------- pacman / AUR
def pacman_local():
    db = {}
    for desc in glob.glob("/var/lib/pacman/local/*/desc"):
        fields, key = {}, None
        with open(desc, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                elif line and key and key not in fields:
                    fields[key] = line
        if "NAME" in fields:
            db[fields["NAME"]] = fields
    return db


_vercmp_cache = {}


def vercmp(a, b):
    k = (a, b)
    if k not in _vercmp_cache:
        rc, stdout, _ = run(["vercmp", a, b])
        _vercmp_cache[k] = int(stdout.strip()) if rc == 0 and stdout.strip() else 0
    return _vercmp_cache[k]


def arch_links(repo, arch, name, base):
    gl = "https://gitlab.archlinux.org/archlinux/packaging/packages/%s/-/commits/main" % (
        base.replace("++", "plusplus").replace("+", "plus"))
    if repo and repo.startswith("cachyos"):
        return "https://packages.cachyos.org/package/%s/%s/%s" % (repo, arch, name), (None if repo == "cachyos" else gl)
    return "https://archlinux.org/packages/%s/%s/%s/" % (repo, arch, name), gl


def pacman_collect(args, out):
    if not shutil.which("pacman"):
        return False
    out["managers"].append("pacman")
    local = pacman_local()
    if not shutil.which("checkupdates"):
        raise RuntimeError("checkupdates not found (install pacman-contrib)")
    dbpath = os.path.join(tempfile.gettempdir(), "tideline-checkup-db-%d" % os.getuid())
    rc, stdout, err = run(["checkupdates"], env={"CHECKUPDATES_DB": dbpath}, timeout=1200)
    if rc not in (0, 2):
        raise RuntimeError("checkupdates rc=%s %s" % (rc, err.strip()[-300:]))
    ups = []
    for line in stdout.splitlines():
        m = re.match(r"^(\S+) (\S+) -> (\S+)", line)
        if m:
            ups.append(m.groups())
    repos = {}
    if ups:
        rc, sl, _ = run(["pacman", "-Sl", "--dbpath", dbpath])
        for line in sl.splitlines():
            p = line.split()
            if len(p) >= 3 and p[1] not in repos:
                repos[p[1]] = p[0]
    security = arch_security(args, out) if ups else {}
    for name, old, new in ups:
        l = local.get(name, {})
        arch = l.get("ARCH", platform.machine())
        repo = repos.get(name) or l.get("INSTALLED_DB")
        purl, curl = arch_links(repo, arch, name, l.get("BASE", name))
        row = new_row("pacman", name, arch=arch, installed_version=old,
                      installed_at=int(l["INSTALLDATE"]) if l.get("INSTALLDATE", "").isdigit() else None,
                      new_version=new, repo=repo, summary=l.get("DESC"), package_url=purl,
                      changes_url=curl)
        for g in security.get(name, []):
            if g.get("fixed") and vercmp(old, g["fixed"]) < 0 <= vercmp(new, g["fixed"]):
                adv = {"id": g["name"], "type": "security", "severity": norm_severity(g.get("severity")),
                       "title": "%s: %s" % (name, g.get("type") or "security issue"),
                       "text": "Fixed in %s. Issues: %s" % (g["fixed"], ", ".join(g.get("issues", []))),
                       "url": "https://security.archlinux.org/" + g["name"], "issued": None,
                       "cves": [c for c in g.get("issues", []) if c.startswith("CVE-")]}
                row["advisories"].append(adv)
        if row["advisories"]:
            rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, None: 0}
            pick = max(row["advisories"], key=lambda a: rank[a["severity"]])
            row.update(security=True, severity=pick["severity"], advisory_id=pick["id"],
                       advisory_type="security", advisory_title=pick["title"],
                       advisory_text=pick["text"], advisory_url=pick["url"],
                       cves=sorted({c for a in row["advisories"] for c in a["cves"]}))
        out["updates"].append(row)
    out["sources_ok"].append("pacman")
    return True


def arch_security(args, out):
    try:
        data = http_json("https://security.archlinux.org/issues/all.json", timeout=60)
    except Exception as e:  # advisories are a bonus; the package list still counts
        out["errors"].append("security.archlinux.org: %s" % e)
        return {}
    by_pkg = {}
    for g in data:
        if g.get("status") != "Fixed":
            continue
        for p in g.get("packages", []):
            by_pkg.setdefault(p, []).append(g)
    return by_pkg


def aur_collect(args, out):
    rc, stdout, err = run(["pacman", "-Qm"])
    if rc not in (0, 1):
        raise RuntimeError("pacman -Qm: " + err.strip())
    foreign = dict(line.split()[:2] for line in stdout.splitlines() if len(line.split()) >= 2)
    names = [n for n in foreign if not re.search(r"-(git|svn|hg|bzr|darcs|fossil)$", n)]
    local = pacman_local()
    for chunk in [names[i:i + 100] for i in range(0, len(names), 100)]:
        q = "&".join("arg[]=" + urllib.parse.quote(n) for n in chunk)
        res = http_json("https://aur.archlinux.org/rpc/v5/info?" + q, timeout=60)
        for p in res.get("results", []):
            name, new = p["Name"], p["Version"]
            old = foreign.get(name)
            if not old or vercmp(new, old) <= 0:
                continue
            l = local.get(name, {})
            out["updates"].append(new_row(
                "aur", name, arch=l.get("ARCH", ""), installed_version=old,
                installed_at=int(l["INSTALLDATE"]) if l.get("INSTALLDATE", "").isdigit() else None,
                new_version=new, repo="aur", summary=p.get("Description"),
                package_url="https://aur.archlinux.org/packages/" + name,
                changes_url="https://aur.archlinux.org/cgit/aur.git/log/?h=" + p.get("PackageBase", name),
                advisory_url=p.get("URL"),
                advisory_issued=p.get("LastModified")))
    out["sources_ok"].append("aur")


# --------------------------------------------------------------------------- apt
def apt_collect(args, osr, out):
    if not shutil.which("apt-get"):
        return False
    out["managers"].append("apt")
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    if not args.no_refresh:
        rc, _, err = run(["apt-get", "-qq", "update"], env=env, timeout=1800)
        if rc != 0:
            out["errors"].append("apt-get update: " + err.strip()[-400:])
    rc, stdout, err = run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "dist-upgrade"], env=env, timeout=900)
    if rc != 0:
        raise RuntimeError("apt-get -s dist-upgrade: " + err.strip()[-400:])
    rc, q, _ = run(["dpkg-query", "-W", "-f", "${binary:Package}\t${Package}\t${source:Package}\t${binary:Summary}\t${Architecture}\n"])
    meta = {}
    for line in q.splitlines():
        f = line.split("\t")
        if len(f) >= 5:
            meta[f[0]] = f
            meta.setdefault(f[1], f)
    distro = osr.get("ID", "debian")
    rows, changelog_budget = [], args.apt_changelogs
    pat = re.compile(r"^Inst (\S+) (?:\[([^\]]+)\] )?\((\S+) (.*?) \[([^\]]+)\]\)")
    for line in stdout.splitlines():
        m = pat.match(line)
        if not m:
            continue
        name, old, new, origin, arch = m.groups()
        f = meta.get(name) or meta.get(name.split(":")[0]) or [name, name, name, None, arch]
        src = f[2] or f[1]
        installed_at = None
        for cand in ("/var/lib/dpkg/info/%s.list" % name, "/var/lib/dpkg/info/%s:%s.list" % (name.split(":")[0], arch)):
            if os.path.exists(cand):
                installed_at = int(os.path.getmtime(cand))
                break
        sec = "security" in origin.lower()
        if "proxmox" in origin.lower():
            purl = "https://git.proxmox.com/?p=%s.git;a=summary" % src
            curl = "https://git.proxmox.com/?p=%s.git;a=log" % src
        elif distro == "ubuntu":
            purl = "https://launchpad.net/ubuntu/+source/" + src
            curl = purl + "/+changelog"
        else:
            purl = "https://tracker.debian.org/pkg/" + src
            curl = "https://tracker.debian.org/media/packages/%s/%s/changelog-%s" % (
                src[:4] if src.startswith("lib") else src[0], src, new.split(":")[-1])
        rows.append(new_row(
            "apt", name.split(":")[0], arch=arch, installed_version=old, installed_at=installed_at,
            new_version=new, repo=origin, summary=f[3] if len(f) > 3 else None, security=sec,
            advisory_type="security" if sec else None, package_url=purl, changes_url=curl,
            advisory_url=("https://security-tracker.debian.org/tracker/source-package/" + src) if sec and distro != "ubuntu" else None,
            _src=src))
    # Top changelog entry per source package (security first), as the update's notes.
    notes = {}
    for row in sorted(rows, key=lambda r: not r["security"]):
        src = row["_src"]
        if src in notes or changelog_budget <= 0:
            continue
        changelog_budget -= 1
        rc, cl, _ = run(["apt-get", "changelog", row["name"]], env=env, timeout=30)
        entry = []
        for ln in cl.splitlines():
            entry.append(ln)
            if ln.startswith(" -- "):
                break
        notes[src] = clip("\n".join(entry), args.max_text) if rc == 0 and entry else None
    for row in rows:
        text = notes.get(row.pop("_src"))
        if text:
            row["advisory_text"] = text
            row["advisory_title"] = text.splitlines()[0]
            row["cves"] = sorted(set(CVE_RE.findall(text)))
        out["updates"].append(row)
    out["reboot_required"] = os.path.exists("/var/run/reboot-required") or kernel_reboot_heuristic()
    out["sources_ok"].append("apt")
    return True


# --------------------------------------------------------------------------- flatpak / fwupd
def flatpak_collect(args, out):
    rc, inst, _ = run(["flatpak", "list", "--system", "--columns=application,version,branch,arch,origin,name"])
    have = {}
    for line in inst.splitlines():
        f = line.split("\t")
        if len(f) >= 6:
            have[(f[0], f[2])] = f
    rc, stdout, err = run(["flatpak", "remote-ls", "--system", "--updates",
                           "--columns=application,version,branch,arch,origin"], timeout=600)
    if rc != 0:
        raise RuntimeError("flatpak remote-ls: " + err.strip()[-300:])
    for line in stdout.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        app, ver, branch, arch, origin = f[:5]
        cur = have.get((app, branch), [app, "", branch, arch, origin, app])
        active = "/var/lib/flatpak/app/%s/%s/%s/active" % (app, arch, branch)
        out["updates"].append(new_row(
            "flatpak", app, arch=arch, installed_version=cur[1] or branch,
            installed_at=int(os.path.getmtime(active)) if os.path.exists(active) else None,
            new_version=ver or "(new commit on %s)" % branch, repo=origin, summary=cur[5],
            package_url="https://flathub.org/apps/" + app if origin == "flathub" else None))
    out["managers"].append("flatpak")
    out["sources_ok"].append("flatpak")


def fwupd_collect(args, out):
    if not args.no_refresh:
        run(["fwupdmgr", "refresh", "--no-unreported-check"], timeout=300)
    rc, stdout, err = run(["fwupdmgr", "get-updates", "--json", "--no-unreported-check"], timeout=300)
    if rc not in (0, 2):
        raise RuntimeError("fwupdmgr get-updates: rc=%s %s" % (rc, err.strip()[-300:]))
    try:
        data = json.loads(stdout or "{}")
    except ValueError:
        data = {}
    for dev in data.get("Devices", []):
        rels = dev.get("Releases") or []
        if not rels:
            continue
        r = rels[0]
        urg = norm_severity(r.get("Urgency"))
        issues = r.get("Issues") or []
        out["updates"].append(new_row(
            "fwupd", dev.get("Name", "device"), arch=dev.get("DeviceId", "")[:12],
            installed_version=dev.get("Version"), new_version=r.get("Version"),
            repo=r.get("RemoteId"), summary=r.get("Summary") or dev.get("Summary"),
            security=bool(issues), severity=urg if issues else None,
            advisory_type="security" if issues else "firmware",
            advisory_title="%s %s" % (dev.get("Name", ""), r.get("Version", "")),
            advisory_text=clip(strip_html(r.get("Description", "")), args.max_text),
            advisory_url=r.get("DetailsUrl") or r.get("Homepage"),
            advisory_issued=r.get("Created"), cves=[i for i in issues if i.startswith("CVE-")],
            package_url="https://fwupd.org/lvfs/devices/" + (r.get("AppstreamId") or "")))
    out["managers"].append("fwupd")
    out["sources_ok"].append("fwupd")


# --------------------------------------------------------------------------- containers
class Registry:
    """Anonymous OCI registry client: Bearer-token dance, HEAD for digests, tag lists."""
    ACCEPT = ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json"])

    def __init__(self):
        self.tokens = {}

    @staticmethod
    def split(ref):
        name, _, tag = ref.rpartition(":")
        if "/" in tag or not name:
            name, tag = ref, "latest"
        host, _, path = name.partition("/")
        if "." not in host and ":" not in host and host != "localhost":
            host, path = "docker.io", name
        if host == "docker.io" and "/" not in path:
            path = "library/" + path
        return host, path, tag

    def _api(self, host):
        return "registry-1.docker.io" if host == "docker.io" else host

    def req(self, host, path, method="GET", accept=None):
        url = "https://%s%s" % (self._api(host), path)
        hdr = {"Accept": accept} if accept else {}
        key = (host, path.split("/manifests/")[0].split("/tags/")[0])
        if key in self.tokens:
            hdr["Authorization"] = "Bearer " + self.tokens[key]
        status, headers, body = http(url, method=method, headers=hdr, timeout=30)
        if status == 401 and "Authorization" not in hdr:
            auth = headers.get("WWW-Authenticate", "")
            params = dict(re.findall(r'(\w+)="([^"]*)"', auth))
            if auth.lower().startswith("bearer") and "realm" in params:
                q = {k: v for k, v in params.items() if k in ("service", "scope")}
                status_t, _, tb = http(params["realm"] + "?" + urllib.parse.urlencode(q), timeout=30)
                if status_t == 200:
                    tj = json.loads(tb.decode())
                    self.tokens[key] = tj.get("token") or tj.get("access_token")
                    return self.req(host, path, method, accept)
        return status, headers, body

    def digest(self, host, path, tag):
        status, headers, _ = self.req(host, "/v2/%s/manifests/%s" % (path, tag), "HEAD", self.ACCEPT)
        if status != 200:
            raise RuntimeError("HEAD %s/%s:%s -> HTTP %s" % (host, path, tag, status))
        return headers.get("Docker-Content-Digest")

    def tags(self, host, path, pages=2):
        """(tags, complete). Docker Hub: its own API, newest first (repos like library/postgres
        have thousands of tags). Elsewhere the registry list, which is alphabetical and can be
        huge (docker.elastic.co); past `pages` pages it's reported incomplete and the caller
        probes likely next versions with HEAD instead."""
        if host == "docker.io":
            ns, _, name = path.partition("/")
            url = "https://hub.docker.com/v2/namespaces/%s/repositories/%s/tags?page_size=100&ordering=last_updated" % (ns, name)
            out = []
            for _ in range(3):
                data = http_json(url, timeout=30)
                out += [t["name"] for t in data.get("results") or []]
                url = data.get("next")
                if not url:
                    break
            return out, True
        out, nxt = [], "/v2/%s/tags/list?n=1000" % path
        for _ in range(pages):
            status, headers, body = self.req(host, nxt)
            if status != 200:
                raise RuntimeError("tags %s/%s -> HTTP %s" % (host, path, status))
            out += json.loads(body.decode()).get("tags") or []
            m = re.search(r"<([^>]+)>", headers.get("Link", ""))
            if not m:
                return out, True
            nxt = m.group(1) if m.group(1).startswith("/") else urllib.parse.urlparse(m.group(1))._replace(scheme="", netloc="").geturl()
        return out, False

    def exists(self, host, path, tag):
        status, _, _ = self.req(host, "/v2/%s/manifests/%s" % (path, tag), "HEAD", self.ACCEPT)
        return status == 200

    def walk_up(self, host, path, tag, max_steps=25):
        """Climb from tag to the highest same-shape release by probing bumps with HEAD
        (major first, then minor, then patch; x.0 and x.y.0/x.y.1 forms)."""
        m = VER_TAG.match(tag)
        if not m:
            return []
        prefix, nums, suffix = m.groups()
        cur = [int(x) for x in nums.split(".")]
        found = []
        for _ in range(max_steps):
            moved = False
            for i in range(len(cur)):
                for last in (0, 1):
                    cand = cur[:i] + [cur[i] + 1] + [0] * (len(cur) - i - 1)
                    if i < len(cur) - 1:
                        cand[-1] = last
                    elif last:
                        continue
                    t = prefix + ".".join(map(str, cand)) + suffix
                    if self.exists(host, path, t):
                        cur, moved = cand, True
                        found.append(t)
                        break
                if moved:
                    break
            if not moved:
                break
        return found

    def labels(self, host, path, tag):
        """Config labels of the linux/amd64 image (costs a manifest GET: only for changed images)."""
        status, _, body = self.req(host, "/v2/%s/manifests/%s" % (path, tag), "GET", self.ACCEPT)
        if status != 200:
            return {}
        m = json.loads(body.decode())
        if "manifests" in m:
            want = [x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "amd64"] or m["manifests"]
            status, _, body = self.req(host, "/v2/%s/manifests/%s" % (path, want[0]["digest"]), "GET", self.ACCEPT)
            if status != 200:
                return {}
            m = json.loads(body.decode())
        cfg = (m.get("config") or {}).get("digest")
        if not cfg:
            return {}
        status, _, body = self.req(host, "/v2/%s/blobs/%s" % (path, cfg))
        if status != 200:
            return {}
        c = json.loads(body.decode())
        return (c.get("config") or {}).get("Labels") or {}


VER_TAG = re.compile(r"^(v?)(\d+(?:\.\d+)*)(.*)$")


def newer_tag(cur, tags, allow_pre=False):
    m = VER_TAG.match(cur)
    if not m:
        return None, None
    prefix, nums, suffix = m.groups()
    n = tuple(int(x) for x in nums.split("."))
    best = None
    for t in tags:
        mm = VER_TAG.match(t)
        if not mm or mm.group(1) != prefix or mm.group(3) != suffix:
            continue
        nn = tuple(int(x) for x in mm.group(2).split("."))
        if len(nn) != len(n) or nn <= n:
            continue
        if not allow_pre and re.search(r"(alpha|beta|rc|dev|pre|nightly)", t, re.I):
            continue
        if best is None or nn > best[0]:
            best = (nn, t)
    if not best:
        return None, None
    nn = best[0]
    kind = "major" if nn[0] != n[0] else ("minor" if len(n) > 1 and nn[1] != n[1] else "patch")
    return best[1], kind


NUM_CORE = re.compile(r"(\d+(?:\.\d+)*)")
PRE_RE = re.compile(r"(alpha|beta|rc\d*\b|-rc|preview|nightly|snapshot)", re.I)


def iso_ts(value):
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone)
    except ValueError:
        return None


def forge_release(source, version, token, cache, max_text):
    """Release notes for a version from GitHub, or a Gitea/Forgejo forge (Codeberg).
    The release tag may say v1.2.3, release-1.2.3 or 1.2.3.0-stable; a container tag of
    "9.2" matches the newest 9.2.x release."""
    m = re.match(r"https?://([^/]+)/([^/]+)/([^/#?]+)", source or "")
    if not m or not version:
        return None
    forge, repo = m.group(1), "%s/%s" % (m.group(2), m.group(3).replace(".git", ""))
    key = forge + "/" + repo
    if key not in cache:
        hdr = {}
        if forge == "github.com":
            url = "https://api.github.com/repos/%s/releases?per_page=50" % repo
            hdr["Accept"] = "application/vnd.github+json"
            if token:
                hdr["Authorization"] = "Bearer " + token
        else:
            url = "https://%s/api/v1/repos/%s/releases?limit=50" % (forge, repo)
        try:
            cache[key] = http_json(url, headers=hdr)
        except Exception:
            cache[key] = []
    w = NUM_CORE.search(version)
    if not w:
        return None
    want = w.group(1)
    for r in cache[key] if isinstance(cache[key], list) else []:
        tag = (r.get("tag_name") or "").split("/")[-1]
        t = NUM_CORE.search(tag)
        if not t or PRE_RE.search(tag) or r.get("prerelease") or r.get("draft"):
            continue
        if t.group(1) == want or t.group(1).startswith(want + "."):
            return {"title": r.get("name") or tag, "url": r.get("html_url"),
                    "text": clip(r.get("body") or "", max_text),
                    "issued": iso_ts(r.get("published_at") or r.get("created_at"))}
    return None


def registry_page(host, path):
    if host == "docker.io":
        return ("https://hub.docker.com/_/%s/tags" % path[8:]) if path.startswith("library/") else "https://hub.docker.com/r/%s/tags" % path
    if host == "quay.io":
        return "https://quay.io/repository/%s?tab=tags" % path
    if host == "ghcr.io":
        return "https://github.com/%s/pkgs/container/%s" % tuple(path.split("/", 1)) if "/" in path else None
    if host == "codeberg.org" and "/" in path:
        return "https://codeberg.org/%s/-/packages/container/%s" % tuple(path.split("/", 1))
    return "https://%s/%s" % (host, path)


def as_user(user, cmd):
    """Run cmd as a rootless user when we're root, with that user's runtime dir."""
    if user in ("root", "") or os.getuid() != 0:
        return cmd
    pw = pwd.getpwnam(user)
    return ["runuser", "-u", user, "--", "env", "XDG_RUNTIME_DIR=/run/user/%d" % pw.pw_uid,
            "HOME=" + pw.pw_dir] + cmd


def container_collect(args, out):
    if not shutil.which("podman"):
        return False
    me = pwd.getpwuid(os.getuid()).pw_name
    users = args.container_users or [me]
    if os.getuid() != 0:
        users = [me]
    sources = json.loads(args.container_sources or "{}")
    reg, gh_cache, seen = Registry(), {}, {}
    for user in users:
        try:
            uid = pwd.getpwnam(user).pw_uid
        except KeyError:
            continue                                # listed for another host; not here
        if uid != 0 and os.getuid() == 0 and not os.path.isdir("/run/user/%d" % uid):
            continue                                # no session/linger: no rootless containers running
        cmd = as_user(user if user != me else "", ["podman", "ps", "--format", "json"])
        rc, stdout, err = run(cmd, timeout=120)
        if rc != 0:
            out["errors"].append("podman ps (%s): %s" % (user, err.strip()[-200:]))
            continue
        for c in json.loads(stdout or "[]"):
            ref = c.get("Image") or ""
            if "@sha256:" in ref or ref.startswith("localhost/") or not ref or ref.startswith("sha256:"):
                continue
            key = (user, ref)
            ent = seen.setdefault(key, {"names": [], "image_id": c.get("ImageID"), "created": None})
            ent["names"] += c.get("Names") or []
            made = c.get("Created") if isinstance(c.get("Created"), int) else None
            if made and (ent["created"] is None or made < ent["created"]):
                ent["created"] = made
    for (user, ref), ent in sorted(seen.items()):
        try:
            cmd = as_user(user if user != me else "", ["podman", "image", "inspect", ent["image_id"]])
            rc, stdout, _ = run(cmd, timeout=60)
            img = (json.loads(stdout) or [{}])[0] if rc == 0 else {}
            local_digests = {d.split("@", 1)[1] for d in img.get("RepoDigests") or [] if "@" in d}
            if img.get("Digest"):
                local_digests.add(img["Digest"])
            labels = img.get("Labels") or (img.get("Config") or {}).get("Labels") or {}
            host, path, tag = reg.split(ref)
            repo = "%s/%s" % (host, path)
            source = labels.get("org.opencontainers.image.source") or sources.get(repo) or sources.get(repo.replace("docker.io/library/", "docker.io/"))
            remote = reg.digest(host, path, tag)
            new_tag, kind = None, None
            if VER_TAG.match(tag):
                tags, complete = reg.tags(host, path)
                if not complete:
                    tags = reg.walk_up(host, path, tag)
                new_tag, kind = newer_tag(tag, tags, args.allow_prerelease)
            rebuilt = bool(remote and local_digests and remote not in local_digests)
            if not new_tag and not rebuilt:
                continue
            # The version label is only worth showing for floating tags (latest, 15-rootless):
            # on versioned tags it's often the base OS's (clickhouse says 22.04).
            have_ver = labels.get("org.opencontainers.image.version")
            tm, hm = VER_TAG.match(tag), NUM_CORE.search(have_ver or "")
            if have_ver == tag or (tm and not (hm and (hm.group(1) + ".").startswith(tm.group(2) + "."))):
                have_ver = None
            installed = tag + (" (%s)" % have_ver if have_ver else "") + " @" + (sorted(local_digests)[0][7:19] if local_digests else "?")
            if new_tag:
                newv, rel_ver, change = new_tag, new_tag, kind
            else:
                rl = reg.labels(host, path, tag)
                rel_ver = rl.get("org.opencontainers.image.version")
                source = source or rl.get("org.opencontainers.image.source")
                newv = "%s (new build%s @%s)" % (tag, (" " + rel_ver) if rel_ver and rel_ver != tag else "", remote[7:19])
                change = "rebuild"
            rel = forge_release(source, rel_ver, args.github_token, gh_cache, args.max_text) if rel_ver else None
            created = img.get("Created", "")
            out["updates"].append(new_row(
                "container", repo + ":" + tag, arch=user,
                installed_version=installed, installed_at=ent["created"],
                new_version=newv, repo=host,
                summary="%s; containers: %s" % (labels.get("org.opencontainers.image.title") or path.split("/")[-1],
                                                 ", ".join(sorted(set(ent["names"])))) + ("; image built %s" % created[:10] if created else ""),
                advisory_type=change, advisory_title=rel["title"] if rel else None,
                advisory_text=rel["text"] if rel else None,
                advisory_url=rel["url"] if rel else None, advisory_issued=rel["issued"] if rel else None,
                cves=sorted(set(CVE_RE.findall(rel["text"] or ""))) if rel else [],
                package_url=registry_page(host, path),
                changes_url=(source.rstrip("/") + "/releases") if source else None))
        except Exception as e:
            out["errors"].append("container %s (%s): %s" % (ref, user, e))
    out["managers"].append("podman")
    out["sources_ok"].append("container")
    return True


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-refresh", action="store_true", help="use cached package metadata")
    ap.add_argument("--dnf-repo", action="append", default=[], help="limit dnf to these repo ids (testing)")
    ap.add_argument("--max-text", type=int, default=4000, help="truncate advisory text to this many characters")
    ap.add_argument("--apt-changelogs", type=int, default=40, help="fetch the top changelog entry for up to N source packages")
    ap.add_argument("--no-aur", action="store_true")
    ap.add_argument("--no-flatpak", action="store_true")
    ap.add_argument("--no-fwupd", action="store_true")
    ap.add_argument("--no-containers", action="store_true")
    ap.add_argument("--container-users", default="", help="comma-separated: root,nlucas (default: the current user)")
    ap.add_argument("--container-sources", default="", help='JSON {"registry/repo": "https://github.com/o/r"} for images without a source label')
    ap.add_argument("--allow-prerelease", action="store_true")
    args = ap.parse_args()
    args.container_users = [u for u in args.container_users.split(",") if u]
    args.github_token = os.environ.get("TIDELINE_GITHUB_TOKEN", "")

    osr = os_release()
    out = {"host": socket.getfqdn(), "collected_at": int(time.time()), "os": osr.get("NAME"),
           "os_version": osr.get("VERSION_ID") or osr.get("BUILD_ID"), "os_id": osr.get("ID"),
           "kernel": platform.release(), "reboot_required": None, "managers": [], "sources_ok": [],
           "errors": [], "updates": []}

    def guard(source, fn, *a):
        try:
            if fn(*a) is not False and source and source not in out["sources_ok"]:
                out["sources_ok"].append(source)
        except Exception as e:
            out["errors"].append("%s: %s" % (source or fn.__name__, e))

    guard("dnf", dnf_collect, args, osr, out)
    guard(None, pacman_collect, args, out)
    if shutil.which("pacman") and not args.no_aur:
        guard(None, aur_collect, args, out)
    guard(None, apt_collect, args, osr, out)
    if shutil.which("flatpak") and not args.no_flatpak:
        guard(None, flatpak_collect, args, out)
    if shutil.which("fwupdmgr") and not args.no_fwupd:
        guard(None, fwupd_collect, args, out)
    if not args.no_containers:
        guard(None, container_collect, args, out)
    if out["reboot_required"] is None:
        out["reboot_required"] = kernel_reboot_heuristic()
    if not out["managers"]:
        out["errors"].append("no supported package manager found")
    json.dump(out, sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
