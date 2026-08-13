"""Memory accounting from /proc/meminfo, correct on pre-3.14 kernels.

Why this module exists: `MemAvailable` was only added to /proc/meminfo in Linux 3.14.
DSM 7.x still ships kernel 3.10.108, so on every Synology appliance the field is simply
absent. Code that does `mem.get("MemAvailable", mem.get("MemFree", 0))` therefore falls
through to `MemFree` *every single time* and reports a box with 13 GB of reclaimable page
cache as ~99% used — which is not just noise, it actively misdirects an operator (it sent
a real disk-churn investigation down a swap/paging dead end).

When MemAvailable is missing we approximate it the way procps does, minus Shmem (tmpfs
pages are page cache but are *not* reclaimable under pressure):

    available ~= MemFree + Buffers + Cached - Shmem + SReclaimable

This ignores per-zone watermarks (the kernel subtracts wmark_low and caps the reclaimable
pagecache/slab contributions at half), so it runs a little optimistic — measured ~1.8%
above `free -m`'s own estimate on a 16 GiB DS1517+. That is close enough to make the
number actionable, and callers are told it is an estimate via `available_estimated`.
"""

_FIELDS = (
    "MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached",
    "Shmem", "SReclaimable", "SwapTotal", "SwapFree",
)


def parse_meminfo(text):
    """Parse /proc/meminfo into a {field: kB} dict, keeping only fields we use."""
    mem = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            if key in _FIELDS:
                try:
                    mem[key] = int(parts[1])  # kB
                except ValueError:
                    pass
    return mem


def available_kb(mem):
    """Best available-memory estimate in kB, plus whether it had to be estimated.

    Returns (available_kb, estimated: bool). Prefers the kernel's own MemAvailable;
    falls back to the procps-style approximation described in the module docstring.
    """
    if "MemAvailable" in mem:
        return mem["MemAvailable"], False
    avail = (
        mem.get("MemFree", 0)
        + mem.get("Buffers", 0)
        + mem.get("Cached", 0)
        - mem.get("Shmem", 0)
        + mem.get("SReclaimable", 0)
    )
    # Never claim more available than exists, and never go negative on odd input.
    avail = max(0, min(avail, mem.get("MemTotal", avail)))
    return avail, True


def memory_stats(text):
    """Build the `performance` tool's memory block from raw /proc/meminfo text.

    Returns the string "unavailable" when MemTotal is missing or unparseable, matching
    what the appliance tools already emit for an unreadable source.
    """
    mem = parse_meminfo(text)
    total = mem.get("MemTotal")
    if not total:
        return "unavailable"

    avail, estimated = available_kb(mem)
    sw_total = mem.get("SwapTotal", 0)
    sw_used = sw_total - mem.get("SwapFree", 0)
    sw_pct = round(100 * sw_used / sw_total, 1) if sw_total else 0.0

    return {
        "total_mib": round(total / 1024),
        "available_mib": round(avail / 1024),
        "used_pct": round(100 * (1 - avail / total), 1),
        # True when the kernel predates MemAvailable (e.g. DSM's 3.10) and the number
        # above is our approximation rather than the kernel's own figure.
        "available_estimated": estimated,
        "swap_used_mib": round(sw_used / 1024),
        "swap_pressure": "none" if sw_used == 0 else "low" if sw_pct < 10 else
                         "moderate" if sw_pct < 50 else "high",
    }
