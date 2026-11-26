# Kernel retention plan: which kernel releases to keep and which packages to remove.
# Pure Python, no target-side dependencies; the playbook feeds it rpm/dpkg query output.
import functools
import re

DEB_IMAGE = re.compile(r'^linux-image-(\d.*?)(-unsigned)?$')


def _rpmvercmp(a, b):
    """rpm's version comparison (segments, numeric beats alpha, ~ sorts before, ^ after)."""
    if a == b:
        return 0
    i = j = 0
    while i < len(a) or j < len(b):
        while i < len(a) and not a[i].isalnum() and a[i] not in '~^':
            i += 1
        while j < len(b) and not b[j].isalnum() and b[j] not in '~^':
            j += 1
        if (i < len(a) and a[i] == '~') or (j < len(b) and b[j] == '~'):
            if i >= len(a) or a[i] != '~':
                return 1
            if j >= len(b) or b[j] != '~':
                return -1
            i += 1
            j += 1
            continue
        if (i < len(a) and a[i] == '^') or (j < len(b) and b[j] == '^'):
            if i >= len(a):
                return -1
            if j >= len(b):
                return 1
            if a[i] != '^':
                return 1
            if b[j] != '^':
                return -1
            i += 1
            j += 1
            continue
        if i >= len(a) or j >= len(b):
            break
        if a[i].isdigit():
            m, n = re.match(r'\d+', a[i:]), re.match(r'\d+', b[j:])
            if not n:
                return 1                      # numeric segment is newer than alpha
            x, y = m.group().lstrip('0'), n.group().lstrip('0')
            if len(x) != len(y):
                return 1 if len(x) > len(y) else -1
            if x != y:
                return 1 if x > y else -1
        else:
            m, n = re.match(r'[A-Za-z]+', a[i:]), re.match(r'[A-Za-z]+', b[j:])
            if not n:
                return -1
            if m.group() != n.group():
                return 1 if m.group() > n.group() else -1
        i += len(m.group())
        j += len(n.group())
    if i >= len(a) and j >= len(b):
        return 0
    return 1 if i < len(a) else -1


def _release_cmp(kind, a, b):
    """rpm compares version, then release; Debian compares the whole release string."""
    if kind == 'rpm':
        av, _, ar = a.partition('-')
        bv, _, br = b.partition('-')
        return _rpmvercmp(av, bv) or _rpmvercmp(ar, br)
    return _rpmvercmp(a, b)


def kernel_retention_plan(rows, kind, running, keep, core_names=None, rpm_names=None):
    """
    rpm: rows = [[name, 'VERSION-RELEASE.ARCH'], ...] (rpm -qa 'kernel*'); a release is
         the VRA of a core package (kernel-core, else kernel), which is what uname -r says.
    deb: rows = [package, ...] installed (dpkg status ii); a release is what follows
         linux-image- (6.12.48+deb13-amd64), which is what uname -r says.
    Keeps the running release plus the newest others, up to `keep`. Never plans to remove
    the running release.
    """
    keep = int(keep)
    core = None
    if kind == 'rpm':
        core_names = core_names or ['kernel-core', 'kernel']
        rpm_names = set(rpm_names or [])
        rows = [r for r in rows if len(r) == 2]
        core = next((c for c in core_names if any(r[0] == c for r in rows)), None)
        installed = {r[1] for r in rows if r[0] == core}
    elif kind == 'deb':
        installed = set()
        for p in rows:
            m = DEB_IMAGE.match(p)
            if m and not p.endswith('-dbg'):
                installed.add(m.group(1))
    else:
        raise ValueError('kind must be rpm or deb')

    newest_first = sorted(installed, key=functools.cmp_to_key(lambda a, b: _release_cmp(kind, a, b)),
                          reverse=True)
    kept = [running] if running in installed else []
    for r in newest_first:
        if len(kept) >= keep:
            break
        if r not in kept:
            kept.append(r)
    remove = [r for r in newest_first if r not in kept and r != running]

    if kind == 'rpm':
        # Every installonly kernel package not of a kept release (orphan kernel-devel too).
        packages = sorted('%s-%s' % (n, v) for n, v in rows
                          if n in rpm_names and v not in kept and v != running)
    else:
        present = set(rows)
        kept_abis = {k.rsplit('-', 1)[0] for k in kept}
        packages = set()
        for r in remove:
            abi = r.rsplit('-', 1)[0]
            cands = ['linux-image-%s' % r, 'linux-image-%s-unsigned' % r, 'linux-image-%s-dbg' % r,
                     'linux-headers-%s' % r, 'linux-modules-%s' % r, 'linux-modules-extra-%s' % r]
            if abi not in kept_abis:
                cands += ['linux-headers-%s-common' % abi, 'linux-headers-%s-common-rt' % abi,
                          'linux-kbuild-%s' % abi]
            packages.update(c for c in cands if c in present)
        packages = sorted(packages)

    return {
        'installed': newest_first,
        'keep': kept,
        'remove': remove,
        'packages': packages,
        'running_installed': running in installed,
        'core': core,                       # rpm: the package that defines a release
    }


class FilterModule(object):
    def filters(self):
        return {'kernel_retention_plan': kernel_retention_plan}
