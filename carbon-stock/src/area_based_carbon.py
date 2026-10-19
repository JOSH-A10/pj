"""
Area-Based Carbon Estimation
=============================

AUTHORITATIVE APPROACH: Landscape-scale carbon accounting using GEDI L4A biomass.

Carbon stock is calculated using area-weighted above-ground biomass from GEDI L4A 
products calibrated with USFS FIA plots. No tree-level or species-based estimation 
is performed.

STRICT REQUIREMENTS:
- NO tree counts
- NO species as required input (metadata only)
- NO per-tree biomass calculations
- NO height as biomass input
- IPCC defaults only (C = AGB × 0.47, CO2e = C × 3.67)

INPUTS:
- forest_area_ha: Forest area in hectares (from mask or AOI)
- mean_agb_Mg_ha: Mean above-ground biomass density (from GEDI L4A)
- agb_uncertainty_Mg_ha: Optional uncertainty (from GEDI L4A)

OUTPUTS:
- Total AGB, carbon, CO2e (all in Mg)
- Per-hectare densities
- Uncertainty quantification
- Methodology metadata

COMPLIANCE: Suitable for USFS/EPA carbon accounting when GEDI L4A is validated 
with regional FIA data.
"""

import logging
from dataclasses import dataclass
from typing import Optional, Dict
import numpy as np

logger = logging.getLogger(__name__)

# IPCC default factors (DO NOT MODIFY)
CARBON_FRACTION = 0.47  # Carbon content of dry biomass
CO2E_CONVERSION = 3.67  # CO2 equivalent factor (44/12)


class TreeBasedInputError(ValueError):
    """Raised when tree-based inputs are detected."""
    pass


@dataclass
class AreaBasedCarbonResult:
    """
    Results from area-based carbon estimation.
    
    All values are landscape-scale totals or per-hectare densities.
    NO per-tree values allowed.
    """
    # Input parameters
    forest_area_ha: float
    mean_agb_Mg_ha: float
    
    # Total stocks (landscape scale)
    total_agb_Mg: float
    total_carbon_Mg: float
    total_co2e_Mg: float
    
    # Per-hectare densities
    carbon_Mg_ha: float
    co2e_Mg_ha: float
    
    # Uncertainty
    uncertainty_percent: float
    agb_uncertainty_Mg_ha: Optional[float] = None
    
    # Methodology metadata
    methodology: str = "GEDI L4A area-based biomass estimation"
    data_source: str = "GEDI L4A"
    ipcc_carbon_fraction: float = CARBON_FRACTION
    co2e_conversion_factor: float = CO2E_CONVERSION
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON output."""
        return {
            "aoi_area_ha": float(self.forest_area_ha),
            "forest_area_ha": float(self.forest_area_ha),
            "mean_agb_Mg_ha": float(self.mean_agb_Mg_ha),
            "total_agb_Mg": float(self.total_agb_Mg),
            "total_carbon_Mg": float(self.total_carbon_Mg),
            "total_co2e_Mg": float(self.total_co2e_Mg),
            "carbon_Mg_ha": float(self.carbon_Mg_ha),
            "co2e_Mg_ha": float(self.co2e_Mg_ha),
            "uncertainty_percent": float(self.uncertainty_percent),
            "methodology": self.methodology,
            "data_source": self.data_source,
            "ipcc_carbon_fraction": float(self.ipcc_carbon_fraction),
            "co2e_conversion_factor": float(self.co2e_conversion_factor),
            "notes": [
                "Carbon stock represents landscape-scale total for research purposes",
                "Results are area-weighted from GEDI L4A FIA-calibrated biomass",
                "No tree-level or species-based estimation performed"
            ]
        }


def guard_against_tree_based_inputs(**kwargs):
    """
    Validate that no tree-based inputs are provided.
    
    Raises:
        TreeBasedInputError: If tree_count, species (required), height, or 
                             per-tree parameters are detected.
    """
    forbidden_params = {
        'tree_count': 'tree counts',
        'number_of_trees': 'tree counts',
        'trees_per_hectare': 'tree density',
        'species': 'species (when required for calculations)',
        'dbh': 'DBH (diameter at breast height)',
        'height': 'height (when used as biomass input)',
        'height_m': 'height measurements',
        'carbon_per_tree': 'per-tree carbon values',
        'agb_per_tree': 'per-tree biomass values',
        'biomass_kg_per_tree': 'per-tree biomass values'
    }
    
    detected = [
        (param, desc) for param, desc in forbidden_params.items() 
        if param in kwargs and kwargs[param] is not None
    ]
    
    if detected:
        params_list = ', '.join([desc for _, desc in detected])
        raise TreeBasedInputError(
            f"Carbon estimation is area-based and derived directly from GEDI biomass. "
            f"Tree- or species-based logic is not permitted. "
            f"Detected forbidden parameters: {params_list}"
        )


def calculate_area_based_carbon(
    forest_area_ha: float,
    mean_agb_Mg_ha: float,
    agb_uncertainty_Mg_ha: Optional[float] = None,
    mask_uncertainty_percent: float = 8.0,
    **kwargs
) -> AreaBasedCarbonResult:
    """
    Calculate landscape-scale carbon stock from area-weighted biomass.
    
    This function implements STRICT area-based accounting:
    - NO tree counts required or accepted
    - NO species required or used in calculations
    - NO per-tree logic
    - IPCC defaults only
    
    Args:
        forest_area_ha: Forest area in hectares
        mean_agb_Mg_ha: Mean above-ground biomass density from GEDI L4A (Mg/ha)
        agb_uncertainty_Mg_ha: Optional GEDI-provided uncertainty (Mg/ha)
        mask_uncertainty_percent: Forest mask classification uncertainty (default 8%)
        **kwargs: Catches any additional parameters to validate against tree-based inputs
    
    Returns:
        AreaBasedCarbonResult with total stocks and per-hectare densities
    
    Raises:
        TreeBasedInputError: If tree-based inputs detected
        ValueError: If input validation fails
    """
    # SAFETY CHECK: Prevent tree-based inputs
    guard_against_tree_based_inputs(**kwargs)
    
    # Input validation
    if forest_area_ha <= 0:
        raise ValueError(f"Forest area must be positive, got {forest_area_ha} ha")
    
    if mean_agb_Mg_ha < 0:
        raise ValueError(f"Mean AGBD must be non-negative, got {mean_agb_Mg_ha} Mg/ha")
    
    if mean_agb_Mg_ha > 1000:
        logger.warning(
            f"Mean AGBD of {mean_agb_Mg_ha:.1f} Mg/ha is unusually high. "
            f"Typical temperate forests: 100-300 Mg/ha. Verify data quality."
        )
    
    logger.info("=" * 80)
    logger.info("AREA-BASED CARBON ESTIMATION")
    logger.info("=" * 80)
    logger.info(f"Input forest area: {forest_area_ha:.2f} ha")
    logger.info(f"Input mean AGBD: {mean_agb_Mg_ha:.2f} Mg/ha")
    
    # Calculate total AGB
    total_agb_Mg = mean_agb_Mg_ha * forest_area_ha
    
    # IPCC carbon conversion (FIXED - DO NOT MODIFY)
    carbon_Mg_ha = mean_agb_Mg_ha * CARBON_FRACTION
    total_carbon_Mg = total_agb_Mg * CARBON_FRACTION
    
    # CO₂e conversion (FIXED - DO NOT MODIFY)
    co2e_Mg_ha = carbon_Mg_ha * CO2E_CONVERSION
    total_co2e_Mg = total_carbon_Mg * CO2E_CONVERSION
    
    # Uncertainty propagation
    if agb_uncertainty_Mg_ha is not None and agb_uncertainty_Mg_ha > 0:
        agb_uncertainty_pct = (agb_uncertainty_Mg_ha / mean_agb_Mg_ha) * 100
    else:
        # Default GEDI L4A uncertainty if not provided
        agb_uncertainty_pct = 15.0
        logger.info(f"Using default GEDI L4A uncertainty: ±{agb_uncertainty_pct}%")
    
    # Combine uncertainties in quadrature (independent errors)
    total_uncertainty_pct = np.sqrt(
        agb_uncertainty_pct**2 + 
        mask_uncertainty_percent**2 + 
        3.0**2  # IPCC factor uncertainty
    )
    
    logger.info("")
    logger.info("RESULTS:")
    logger.info(f"  Total AGB: {total_agb_Mg:.2f} Mg")
    logger.info(f"  Total Carbon: {total_carbon_Mg:.2f} Mg C")
    logger.info(f"  Total CO2e: {total_co2e_Mg:.2f} Mg CO2e")
    logger.info("")
    logger.info(f"  Carbon density: {carbon_Mg_ha:.2f} Mg C/ha")
    logger.info(f"  CO2e density: {co2e_Mg_ha:.2f} Mg CO2e/ha")
    logger.info("")
    logger.info(f"UNCERTAINTY ANALYSIS:")
    logger.info(f"  GEDI L4A: ±{agb_uncertainty_pct:.1f}%")
    logger.info(f"  Forest mask: ±{mask_uncertainty_percent:.1f}%")
    logger.info(f"  IPCC factors: ±3.0%")
    logger.info(f"  Total (quadrature): ±{total_uncertainty_pct:.1f}%")
    logger.info("=" * 80)
    
    return AreaBasedCarbonResult(
        forest_area_ha=forest_area_ha,
        mean_agb_Mg_ha=mean_agb_Mg_ha,
        total_agb_Mg=total_agb_Mg,
        total_carbon_Mg=total_carbon_Mg,
        total_co2e_Mg=total_co2e_Mg,
        carbon_Mg_ha=carbon_Mg_ha,
        co2e_Mg_ha=co2e_Mg_ha,
        uncertainty_percent=total_uncertainty_pct,
        agb_uncertainty_Mg_ha=agb_uncertainty_Mg_ha
    )


def validate_area_based_output(result: Dict) -> bool:
    """
    Validate that output contains only area-based metrics.
    
    Args:
        result: Output dictionary to validate
    
    Returns:
        True if valid, raises exception otherwise
    
    Raises:
        ValueError: If forbidden per-tree outputs detected
    """
    forbidden_keys = [
        'carbon_per_tree',
        'carbon_per_species',
        'estimated_number_of_trees',
        'tree_count',
        'trees_per_hectare',
        'agb_per_tree',
        'co2e_per_tree'
    ]
    
    detected = [key for key in forbidden_keys if key in result]
    
    if detected:
        raise ValueError(
            f"Output contains forbidden per-tree metrics: {', '.join(detected)}. "
            f"Carbon estimation must be area-based only."
        )
    
    # Validate required keys
    required_keys = [
        'forest_area_ha',
        'mean_agb_Mg_ha',
        'total_agb_Mg',
        'total_carbon_Mg',
        'total_co2e_Mg',
        'uncertainty_percent',
        'methodology'
    ]
    
    missing = [key for key in required_keys if key not in result]
    
    if missing:
        raise ValueError(
            f"Output missing required area-based metrics: {', '.join(missing)}"
        )
    
    return True


# ============================================================================
# Example Usage
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    print("\n" + "=" * 80)
    print("EXAMPLE: Area-Based Carbon Estimation")
    print("=" * 80)
    
    # Example: Forest carbon from GEDI L4A
    result = calculate_area_based_carbon(
        forest_area_ha=1250.0,
        mean_agb_Mg_ha=147.3,
        agb_uncertainty_Mg_ha=18.4
    )
    
    print("\nJSON OUTPUT:")
    print("=" * 80)
    import json
    print(json.dumps(result.to_dict(), indent=2))
    
    print("\n" + "=" * 80)
    print("SAFETY CHECK: Testing forbidden inputs")
    print("=" * 80)
    
    try:
        # This should fail
        result = calculate_area_based_carbon(
            forest_area_ha=100.0,
            mean_agb_Mg_ha=150.0,
            tree_count=5000  # FORBIDDEN
        )
        print("ERROR: Safety check failed - tree_count was accepted!")
    except TreeBasedInputError as e:
        print(f"✓ Safety check passed: {e}")
    
    try:
        # This should also fail
        result = calculate_area_based_carbon(
            forest_area_ha=100.0,
            mean_agb_Mg_ha=150.0,
            species="teak"  # FORBIDDEN when used for calculations
        )
        print("ERROR: Safety check failed - species was accepted!")
    except TreeBasedInputError as e:
        print(f"✓ Safety check passed: {e}")
    
    print("\n" + "=" * 80)
    print("All checks passed!")
    print("=" * 80)
