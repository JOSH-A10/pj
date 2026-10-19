"""
DEPRECATED: Individual tree detection and per-tree carbon
==========================================================

This module is DEPRECATED and DISABLED.

REASON:
Per-tree carbon estimation using individual tree detection is NOT compliant 
with USA forest carbon accounting standards (USFS/EPA).

PROBLEMS:
- Requires tree counts (unreliable)
- Uses species-specific allometry (site calibration needed)
- Per-tree biomass calculations (high uncertainty)
- Total uncertainty: ±35-45% (too high for carbon accounting)

✅ USE INSTEAD:
- src/area_based_carbon.py (area-based carbon accounting)
- scripts/usa_forest_carbon_estimation.py (complete workflow)

NEW APPROACH:
- GEDI L4A authoritative biomass (FIA-calibrated)
- Area-based aggregation (landscape scale)
- IPCC default carbon factors
- Uncertainty: ±12-25% (suitable for USFS/EPA)

DATE DEPRECATED: February 1, 2026
REMOVAL SCHEDULED: March 1, 2026
"""

class PerTreeEstimationError(ImportError):
    """Raised when attempting to use deprecated per-tree estimation."""
    pass


raise PerTreeEstimationError(
    "individual_tree_detection.py module is DEPRECATED and disabled. "
    "Per-tree carbon estimation is not compliant with USA carbon accounting standards. "
    "Use src/area_based_carbon.py for area-based accounting instead. "
    "See GEDI_USAGE_REFACTORING.md for migration guide."
)
