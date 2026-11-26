# Tideline filters.
#   "22.9r7" | tideline_vercmp("22.9r10")  -> -1
# Natural version comparison for vendor version strings (22.9r7, 9.1.0-45678, 15.1R7-S6):
# digit runs compare as numbers, letter runs as text, numbers sort before letters, and a
# version that runs out first is older. Never raises on mixed shapes (LooseVersion does).
import re


def _key(version):
    parts = re.findall(r"\d+|[A-Za-z]+", str(version or ""))
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts]


def tideline_vercmp(a, b):
    ka, kb = _key(a), _key(b)
    return (ka > kb) - (ka < kb)


class FilterModule:
    def filters(self):
        return {"tideline_vercmp": tideline_vercmp}
