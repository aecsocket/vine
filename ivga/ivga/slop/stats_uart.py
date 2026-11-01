"""Legacy ivga1 snapshot format for the older statistics harness."""

from ivga.util.stats_uart import StatsUART as NamedStatsUART

FIELDS = (
  "status", "selection", "alloc_allocs", "alloc_frees", "alloc_peak",
  "link_erase", "interact_annihilate",
)


class StatsUART(NamedStatsUART):
  def __init__(self, values, divisor=868):
    values = tuple(values)
    if len(values) != len(FIELDS):
      raise ValueError("ivga1 requires seven fields of at most 32 bits")
    super().__init__(dict(zip(FIELDS, values, strict=True)), divisor,
                     prefix="ivga1", queue_requests=False)
