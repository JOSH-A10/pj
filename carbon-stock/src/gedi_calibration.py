"""
⚠️  DEPRECATED MODULE - DO NOT USE ⚠️

This module used GEDI L2A height data to TRAIN machine learning models
for height prediction. This approach is SCIENTIFICALLY INCORRECT for USA
carbon accounting.

WHY DEPRECATED:
1. GEDI L4A provides AUTHORITATIVE biomass (FIA-calibrated)
2. ML-based height prediction introduces unnecessary uncertainty
3. Training on GEDI violates principle of using authoritative data directly
4. Not compliant with USFS/EPA carbon accounting standards

✅ CORRECT APPROACH:
Use scripts/gedi_l4a_authoritative.py to fetch GEDI L4A biomass directly.
No ML training, no height prediction, no calibration needed.

From GEDI L4A documentation:
"GEDI L4A provides footprint-level estimates of aboveground biomass density
(AGBD) calibrated using Forest Inventory and Analysis (FIA) plot data from
the USFS. These estimates are the product of sophisticated models and should
be used directly rather than as training data for additional models."

MIGRATION:
Old code:
    from src.gedi_calibration import GEDIHeightCalibrator
    calibrator = GEDIHeightCalibrator()
    calibrator.calibrate(sentinel2_path, sentinel1_path, gedi_l2a_path)
    height_map = calibrator.predict_wall_to_wall(...)

New code:
    from scripts.gedi_l4a_authoritative import fetch_gedi_l4a_biomass
    result = fetch_gedi_l4a_biomass(aoi_bbox)
    agbd_Mg_ha = result['statistics']['mean_agbd_Mg_ha']
    # Use GEDI biomass directly - NO prediction needed

DATE DEPRECATED: February 1, 2026
REMOVAL SCHEDULED: March 1, 2026
"""

raise ImportError(
    "❌ gedi_calibration module is DEPRECATED and disabled.\n\n"
    "This module incorrectly used GEDI as ML training data.\n"
    "GEDI L4A provides AUTHORITATIVE biomass - use it directly.\n\n"
    "✅ NEW: Use scripts/gedi_l4a_authoritative.py\n"
    "✅ See SENTINEL_USAGE_RESTRICTIONS.md for migration guide\n"
)

# Original code preserved below for reference, but module import will fail
# =============================================================================

# ... (old code omitted for brevity - see git history if needed)
