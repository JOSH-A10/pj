"""
DEPRECATED: Tree-based carbon estimation
=========================================

This module is DEPRECATED and DISABLED.

REASON:
Tree-based carbon estimation using tree counts and species is NOT compliant 
with USA forest carbon accounting standards (USFS/EPA).

✅ USE INSTEAD:
- src/area_based_carbon.py (area-based carbon accounting)
- scripts/usa_forest_carbon_estimation.py (complete workflow)

NEW APPROACH:
- NO tree counts
- NO species requirements
- NO per-tree biomass calculations
- GEDI L4A authoritative biomass
- IPCC default carbon factors
- Area-based aggregation

DATE DEPRECATED: February 1, 2026
REMOVAL SCHEDULED: March 1, 2026
"""

class TreeBasedEstimationError(ImportError):
    """Raised when attempting to use deprecated tree-based estimation."""
    pass


raise TreeBasedEstimationError(
    "trees.py module is DEPRECATED and disabled. "
    "Tree-based carbon estimation is not compliant with USA carbon accounting standards. "
    "Use src/area_based_carbon.py for area-based accounting instead. "
    "See GEDI_USAGE_REFACTORING.md for migration guide."
)
