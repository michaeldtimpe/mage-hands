"""Memory accounting, especially on kernels with no MemAvailable (DSM 3.10)."""

from mage_hands_core import available_kb, memory_stats, parse_meminfo

# Verbatim from alpha (DS1517+, DSM 7.3.1, kernel 3.10.108) on 2026-08-13. Note the
# absence of MemAvailable — that absence is the whole point of this fixture. At the same
# instant `free -m` reported: total 16040, used 2396, available 13326, swap 1194 used.
DSM_MEMINFO = """MemTotal:       16425908 kB
MemFree:          173472 kB
Buffers:           14200 kB
Cached:         13166724 kB
SwapCached:       296060 kB
Shmem:             78204 kB
SReclaimable:     617272 kB
SUnreclaim:       103904 kB
SwapTotal:      11956140 kB
SwapFree:       10732932 kB
"""

# A modern kernel that does publish MemAvailable.
MODERN_MEMINFO = """MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    6000000 kB
Buffers:          100000 kB
Cached:          5000000 kB
Shmem:             50000 kB
SReclaimable:     200000 kB
SwapTotal:       2000000 kB
SwapFree:        2000000 kB
"""


def test_prefers_kernel_memavailable_when_present():
    mem = parse_meminfo(MODERN_MEMINFO)
    avail, estimated = available_kb(mem)
    assert avail == 6000000
    assert estimated is False


def test_estimates_when_memavailable_absent():
    mem = parse_meminfo(DSM_MEMINFO)
    assert "MemAvailable" not in mem
    avail, estimated = available_kb(mem)
    assert estimated is True
    # MemFree + Buffers + Cached - Shmem + SReclaimable
    assert avail == 173472 + 14200 + 13166724 - 78204 + 617272


def test_dsm_estimate_tracks_free_within_two_percent():
    """The regression that mattered: this used to report 161 MiB / 99% used."""
    stats = memory_stats(DSM_MEMINFO)
    free_m_available = 13326  # what `free -m` said at the same instant
    assert abs(stats["available_mib"] - free_m_available) / free_m_available < 0.02
    assert stats["used_pct"] < 20.0
    assert stats["available_estimated"] is True


def test_swap_pressure_and_totals():
    stats = memory_stats(DSM_MEMINFO)
    assert stats["total_mib"] == 16041  # `free -m` floors to 16040; we round
    assert stats["swap_used_mib"] == 1195  # 1223208 kB; `free -m` floors to 1194
    # 1223208 kB of 11956140 kB is ~10.2% -> moderate
    assert stats["swap_pressure"] == "moderate"


def test_swap_pressure_none_when_unused():
    stats = memory_stats(MODERN_MEMINFO)
    assert stats["swap_used_mib"] == 0
    assert stats["swap_pressure"] == "none"


def test_estimate_never_exceeds_total():
    """Guard the clamp: absurd cache values must not yield available > total."""
    text = """MemTotal:        1000000 kB
MemFree:          900000 kB
Cached:          9000000 kB
"""
    stats = memory_stats(text)
    assert stats["available_mib"] <= stats["total_mib"]
    assert stats["used_pct"] >= 0.0


def test_unavailable_without_memtotal():
    assert memory_stats("Cached: 123 kB\n") == "unavailable"
    assert memory_stats("") == "unavailable"


def test_ignores_unparseable_lines():
    mem = parse_meminfo("MemTotal:  notanumber kB\nMemFree:  100 kB\n")
    assert mem == {"MemFree": 100}
