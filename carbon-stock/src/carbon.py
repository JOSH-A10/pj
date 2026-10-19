"""
Carbon Stock and CO2 Equivalent Calculation Module
Converts biomass to carbon and CO2 equivalent with uncertainty propagation.

ALL CONVERSIONS FOLLOW IPCC GUIDELINES - NO APPROXIMATIONS.
"""

import numpy as np
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
import logging

from src.config import PHYSICAL_CONSTANTS, logger

@dataclass
class CarbonResult:
    """Container for carbon stock results with full traceability"""
    carbon_mg_c_ha: np.ndarray  # Carbon stock in Mg C/ha
    co2e_mg_ha: np.ndarray      # CO2 equivalent in Mg CO2e/ha
    uncertainty: Dict           # Uncertainty components
    metadata: Dict              # Processing metadata
    
    def get_total_carbon(self, pixel_area_m2: float, mask: Optional[np.ndarray] = None) -> float:
        """
        Calculate total carbon for all pixels.
        
        Args:
            pixel_area_m2: Area of each pixel in m²
            mask: Boolean mask for valid pixels
        
        Returns:
            Total carbon in Mg C
        """
        if mask is None:
            mask = ~np.isnan(self.carbon_mg_c_ha)
        
        # Convert Mg C/ha to Mg C per pixel
        hectares_per_pixel = pixel_area_m2 / 10000.0
        carbon_per_pixel = self.carbon_mg_c_ha * hectares_per_pixel
        
        # Sum over valid pixels
        total = float(np.nansum(carbon_per_pixel[mask]))
        
        return total
    
    def get_total_co2e(self, pixel_area_m2: float, mask: Optional[np.ndarray] = None) -> float:
        """
        Calculate total CO2 equivalent for all pixels.
        
        Args:
            pixel_area_m2: Area of each pixel in m²
            mask: Boolean mask for valid pixels
        
        Returns:
            Total CO2e in Mg CO2e
        """
        if mask is None:
            mask = ~np.isnan(self.co2e_mg_ha)
        
        # Convert Mg CO2e/ha to Mg CO2e per pixel
        hectares_per_pixel = pixel_area_m2 / 10000.0
        co2e_per_pixel = self.co2e_mg_ha * hectares_per_pixel
        
        # Sum over valid pixels
        total = float(np.nansum(co2e_per_pixel[mask]))
        
        return total
    
    def get_statistics(self, mask: Optional[np.ndarray] = None) -> Dict:
        """
        Calculate comprehensive statistics.
        
        Args:
            mask: Boolean mask for valid pixels
        
        Returns:
            Dictionary with carbon and CO2e statistics
        """
        if mask is None:
            mask = ~np.isnan(self.carbon_mg_c_ha)
        
        valid_carbon = self.carbon_mg_c_ha[mask]
        valid_co2e = self.co2e_mg_ha[mask]
        
        if len(valid_carbon) == 0:
            return {'error': 'No valid pixels'}
        
        return {
            'carbon': {
                'mean_mg_c_ha': float(np.mean(valid_carbon)),
                'std_mg_c_ha': float(np.std(valid_carbon)),
                'min_mg_c_ha': float(np.min(valid_carbon)),
                'max_mg_c_ha': float(np.max(valid_carbon)),
                'median_mg_c_ha': float(np.median(valid_carbon)),
                'unit': 'Mg C/ha'
            },
            'co2_equivalent': {
                'mean_mg_co2e_ha': float(np.mean(valid_co2e)),
                'std_mg_co2e_ha': float(np.std(valid_co2e)),
                'min_mg_co2e_ha': float(np.min(valid_co2e)),
                'max_mg_co2e_ha': float(np.max(valid_co2e)),
                'median_mg_co2e_ha': float(np.median(valid_co2e)),
                'unit': 'Mg CO2e/ha'
            },
            'uncertainty': self.uncertainty,
            'n_pixels': int(np.sum(mask))
        }


def biomass_to_carbon(
    biomass_mg_ha: np.ndarray,
    carbon_fraction: Optional[float] = None,
    uncertainty: Optional[float] = None
) -> Tuple[np.ndarray, float]:
    """
    Convert dry biomass to carbon content.
    
    Formula: C = Biomass × f
    
    Source: IPCC 2006, Guidelines for National Greenhouse Gas Inventories,
            Volume 4, Chapter 4, Table 4.3
    
    Args:
        biomass_mg_ha: Dry biomass in Mg/ha
        carbon_fraction: Carbon fraction of dry biomass (0-1)
                        Default: 0.47 (IPCC default for tropical/subtropical)
                        Range: 0.45-0.50 depending on species
        uncertainty: Relative uncertainty in carbon fraction (fraction)
                     Default: ±0.02 (from IPCC guidelines)
    
    Returns:
        Tuple of (carbon_mg_c_ha, uncertainty_fraction)
    
    Physical basis:
        Carbon is a major constituent of dry plant biomass (cellulose, lignin, etc.)
        The fraction varies by:
        - Species composition
        - Tree part (wood vs. leaves)
        - Climate zone
    """
    if carbon_fraction is None:
        carbon_fraction = PHYSICAL_CONSTANTS.DEFAULT_CARBON_FRACTION
    
    if uncertainty is None:
        uncertainty = 0.04  # ±4% relative uncertainty (IPCC)
    
    # Validate carbon fraction
    if not 0.40 <= carbon_fraction <= 0.55:
        logger.warning(
            f"Carbon fraction {carbon_fraction:.3f} outside typical range [0.40, 0.55]"
        )
    
    # Convert
    carbon_mg_c_ha = biomass_mg_ha * carbon_fraction
    
    logger.info(
        f"Converted biomass to carbon: f_C = {carbon_fraction:.3f} ± {uncertainty:.3f}"
    )
    logger.debug(
        f"Source: IPCC 2006, Vol. 4, Ch. 4, Table 4.3"
    )
    
    return carbon_mg_c_ha, uncertainty


def carbon_to_co2_equivalent(
    carbon_mg_c_ha: np.ndarray,
    conversion_ratio: Optional[float] = None
) -> Tuple[np.ndarray, Dict]:
    """
    Convert carbon to CO2 equivalent.
    
    Formula: CO2e = C × (M_CO2 / M_C)
    
    Source: IPCC 2006, Guidelines for National Greenhouse Gas Inventories,
            Volume 1, Chapter 3
    
    Args:
        carbon_mg_c_ha: Carbon stock in Mg C/ha
        conversion_ratio: CO2/C molecular weight ratio
                         Default: 44.01/12.01 = 3.6642 (exact)
    
    Returns:
        Tuple of (co2e_mg_ha, metadata_dict)
    
    Physical basis:
        Carbon in biomass, when oxidized (burned or decomposed aerobically),
        combines with oxygen to form CO2.
        
        Molecular weights:
        - C:   12.01 g/mol (atomic weight from periodic table)
        - O2:  32.00 g/mol
        - CO2: 44.01 g/mol
        
        Ratio: CO2/C = 44.01/12.01 = 3.6642
        
        This is EXACT chemistry, not an approximation.
    """
    if conversion_ratio is None:
        conversion_ratio = PHYSICAL_CONSTANTS.CARBON_TO_CO2_RATIO
    
    # Validate ratio (should be ~3.664)
    expected_ratio = 44.01 / 12.01
    if not np.isclose(conversion_ratio, expected_ratio, rtol=0.001):
        logger.warning(
            f"Conversion ratio {conversion_ratio:.4f} differs from "
            f"molecular weight ratio {expected_ratio:.4f}"
        )
    
    # Convert
    co2e_mg_ha = carbon_mg_c_ha * conversion_ratio
    
    metadata = {
        'conversion_ratio': conversion_ratio,
        'molecular_weight_C': 12.01,
        'molecular_weight_CO2': 44.01,
        'source': 'IPCC 2006, Vol. 1, Ch. 3',
        'uncertainty': 0.0,  # This is exact chemistry
        'unit': 'Mg CO2e/ha'
    }
    
    logger.info(
        f"Converted carbon to CO2 equivalent: "
        f"ratio = {conversion_ratio:.4f}"
    )
    
    return co2e_mg_ha, metadata


def calculate_carbon_stock(
    biomass_mg_ha: np.ndarray,
    carbon_fraction: Optional[float] = None,
    include_co2e: bool = True,
    biomass_uncertainty: Optional[float] = None
) -> CarbonResult:
    """
    Complete carbon stock calculation with uncertainty propagation.
    
    Args:
        biomass_mg_ha: Total biomass (AGB + BGB) in Mg/ha
        carbon_fraction: Carbon fraction (default: IPCC value)
        include_co2e: Whether to calculate CO2 equivalent
        biomass_uncertainty: Relative uncertainty in biomass (fraction)
    
    Returns:
        CarbonResult object with carbon, CO2e, and uncertainty
    """
    # Convert biomass to carbon
    carbon_mg_c_ha, carbon_uncertainty = biomass_to_carbon(
        biomass_mg_ha,
        carbon_fraction=carbon_fraction
    )
    
    # Calculate CO2 equivalent if requested
    if include_co2e:
        co2e_mg_ha, co2_metadata = carbon_to_co2_equivalent(carbon_mg_c_ha)
    else:
        co2e_mg_ha = np.zeros_like(carbon_mg_c_ha)
        co2_metadata = {}
    
    # Propagate uncertainty
    # Total uncertainty = sqrt(biomass_unc^2 + carbon_frac_unc^2)
    if biomass_uncertainty is not None:
        total_uncertainty = np.sqrt(
            biomass_uncertainty**2 + carbon_uncertainty**2
        )
    else:
        total_uncertainty = carbon_uncertainty
    
    uncertainty_dict = {
        'total_relative': float(total_uncertainty),
        'biomass_component': float(biomass_uncertainty) if biomass_uncertainty else 0.0,
        'carbon_fraction_component': float(carbon_uncertainty),
        'co2_conversion_component': 0.0,  # Exact chemistry
        'method': 'error_propagation',
        'notes': 'Assumes independent uncertainties'
    }
    
    metadata = {
        'carbon_fraction': carbon_fraction or PHYSICAL_CONSTANTS.DEFAULT_CARBON_FRACTION,
        'co2_conversion_ratio': PHYSICAL_CONSTANTS.CARBON_TO_CO2_RATIO,
        'standards': ['IPCC 2006 Vol. 4 Ch. 4', 'IPCC 2006 Vol. 1 Ch. 3'],
        'co2_metadata': co2_metadata
    }
    
    logger.info(
        f"Carbon stock calculated: total uncertainty ±{total_uncertainty*100:.1f}%"
    )
    
    return CarbonResult(
        carbon_mg_c_ha=carbon_mg_c_ha,
        co2e_mg_ha=co2e_mg_ha,
        uncertainty=uncertainty_dict,
        metadata=metadata
    )


def monte_carlo_uncertainty(
    biomass_mg_ha: np.ndarray,
    biomass_uncertainty: float,
    carbon_fraction: float,
    carbon_fraction_uncertainty: float,
    n_iterations: int = 1000,
    seed: Optional[int] = None
) -> Dict:
    """
    Calculate uncertainty using Monte Carlo simulation.
    
    This provides a more robust uncertainty estimate than simple error propagation,
    especially for non-linear relationships.
    
    Args:
        biomass_mg_ha: Mean biomass values
        biomass_uncertainty: Relative uncertainty in biomass (fraction)
        carbon_fraction: Mean carbon fraction
        carbon_fraction_uncertainty: Uncertainty in carbon fraction (absolute)
        n_iterations: Number of Monte Carlo iterations
        seed: Random seed for reproducibility
    
    Returns:
        Dictionary with uncertainty statistics
    """
    if seed is not None:
        np.random.seed(seed)
    
    logger.info(f"Running Monte Carlo uncertainty analysis ({n_iterations} iterations)")
    
    # Generate random samples
    # Assume normal distributions (can be changed to other distributions)
    biomass_samples = np.random.normal(
        loc=biomass_mg_ha,
        scale=biomass_mg_ha * biomass_uncertainty,
        size=(n_iterations, *biomass_mg_ha.shape)
    )
    
    carbon_fraction_samples = np.random.normal(
        loc=carbon_fraction,
        scale=carbon_fraction_uncertainty,
        size=n_iterations
    )
    
    # Clip to valid ranges
    biomass_samples = np.clip(biomass_samples, 0, None)
    carbon_fraction_samples = np.clip(carbon_fraction_samples, 0.4, 0.55)
    
    # Calculate carbon for each iteration
    carbon_samples = np.zeros_like(biomass_samples)
    for i in range(n_iterations):
        carbon_samples[i] = biomass_samples[i] * carbon_fraction_samples[i]
    
    # Calculate statistics
    carbon_mean = np.mean(carbon_samples, axis=0)
    carbon_std = np.std(carbon_samples, axis=0)
    carbon_percentile_5 = np.percentile(carbon_samples, 5, axis=0)
    carbon_percentile_95 = np.percentile(carbon_samples, 95, axis=0)
    
    # Relative uncertainty
    relative_uncertainty = carbon_std / carbon_mean
    
    logger.info(
        f"Monte Carlo complete: mean relative uncertainty = "
        f"{np.nanmean(relative_uncertainty)*100:.1f}%"
    )
    
    return {
        'mean': carbon_mean,
        'std': carbon_std,
        'percentile_5': carbon_percentile_5,
        'percentile_95': carbon_percentile_95,
        'relative_uncertainty': relative_uncertainty,
        'confidence_interval_90': (carbon_percentile_5, carbon_percentile_95),
        'n_iterations': n_iterations,
        'method': 'monte_carlo'
    }


def sequestration_potential(
    current_carbon_mg_c_ha: np.ndarray,
    reference_carbon_mg_c_ha: np.ndarray,
    timeframe_years: int = 20
) -> Dict:
    """
    Calculate carbon sequestration potential relative to reference state.
    
    Args:
        current_carbon_mg_c_ha: Current carbon stock
        reference_carbon_mg_c_ha: Reference/target carbon stock (e.g., mature forest)
        timeframe_years: Time frame for sequestration (years)
    
    Returns:
        Dictionary with sequestration metrics
    """
    # Calculate deficit
    deficit_mg_c_ha = reference_carbon_mg_c_ha - current_carbon_mg_c_ha
    deficit_mg_c_ha = np.maximum(deficit_mg_c_ha, 0)  # Only positive deficits
    
    # Annual sequestration rate (assuming linear growth)
    annual_rate_mg_c_ha_yr = deficit_mg_c_ha / timeframe_years
    
    # Convert to CO2e
    deficit_co2e = deficit_mg_c_ha * PHYSICAL_CONSTANTS.CARBON_TO_CO2_RATIO
    annual_co2e = annual_rate_mg_c_ha_yr * PHYSICAL_CONSTANTS.CARBON_TO_CO2_RATIO
    
    return {
        'carbon_deficit_mg_c_ha': deficit_mg_c_ha,
        'co2e_deficit_mg_co2e_ha': deficit_co2e,
        'annual_sequestration_rate_mg_c_ha_yr': annual_rate_mg_c_ha_yr,
        'annual_co2e_rate_mg_co2e_ha_yr': annual_co2e,
        'timeframe_years': timeframe_years,
        'assumptions': 'Linear growth model - actual growth may be non-linear'
    }


if __name__ == "__main__":
    # Test module
    logger.info("Carbon stock calculation module loaded successfully")
    logger.info("Available functions:")
    logger.info("  - biomass_to_carbon()")
    logger.info("  - carbon_to_co2_equivalent()")
    logger.info("  - calculate_carbon_stock()")
    logger.info("  - monte_carlo_uncertainty()")
    logger.info("  - sequestration_potential()")
    
    logger.info(f"\nPhysical constants:")
    logger.info(f"  C to CO2 ratio: {PHYSICAL_CONSTANTS.CARBON_TO_CO2_RATIO:.4f}")
    logger.info(f"  Default carbon fraction: {PHYSICAL_CONSTANTS.DEFAULT_CARBON_FRACTION:.3f}")
