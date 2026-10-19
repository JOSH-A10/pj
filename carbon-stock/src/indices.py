"""
Spectral Indices Calculation Module
All formulas explicitly documented with sources and valid ranges.

NO ASSUMPTIONS - Every index mathematically defined and validated.
"""

import numpy as np
import logging
from typing import Dict, Optional
from dataclasses import dataclass

from src.config import VegetationIndices, logger

@dataclass
class IndexResult:
    """Container for index calculation results with validation"""
    values: np.ndarray
    name: str
    valid_range: tuple
    formula: str
    source: str
    unit: str = "dimensionless"
    
    def validate(self) -> Dict[str, any]:
        """Validate index values are within expected range"""
        results = {
            'valid': True,
            'warnings': [],
            'stats': {}
        }
        
        # Remove nodata/nan
        valid_data = self.values[~np.isnan(self.values)]
        
        if len(valid_data) == 0:
            results['valid'] = False
            results['warnings'].append("All values are NaN")
            return results
        
        # Check range
        min_val = np.min(valid_data)
        max_val = np.max(valid_data)
        
        if min_val < self.valid_range[0] or max_val > self.valid_range[1]:
            results['warnings'].append(
                f"{self.name} values outside expected range {self.valid_range}: "
                f"[{min_val:.3f}, {max_val:.3f}]"
            )
        
        results['stats'] = {
            'min': float(min_val),
            'max': float(max_val),
            'mean': float(np.mean(valid_data)),
            'std': float(np.std(valid_data)),
            'valid_pixels': len(valid_data)
        }
        
        return results


def ndvi(nir: np.ndarray, red: np.ndarray, nodata: Optional[float] = None) -> IndexResult:
    """
    Calculate Normalized Difference Vegetation Index
    
    Formula: (NIR - Red) / (NIR + Red)
    Source: Rouse et al., 1974, "Monitoring vegetation systems in the 
            Great Plains with ERTS", NASA SP-351
    
    Args:
        nir: Near-infrared band (e.g., Sentinel-2 B8)
        red: Red band (e.g., Sentinel-2 B4)
        nodata: No data value to mask
    
    Returns:
        IndexResult with NDVI values
    
    Physical meaning:
        - Range: [-1, 1]
        - < 0: Water, clouds, snow
        - 0 - 0.2: Bare soil, rock
        - 0.2 - 0.5: Sparse vegetation, grassland
        - 0.5 - 0.9: Dense vegetation, forest
    """
    # Mask nodata
    if nodata is not None:
        mask = (nir != nodata) & (red != nodata)
        nir_masked = np.where(mask, nir, np.nan)
        red_masked = np.where(mask, red, np.nan)
    else:
        nir_masked, red_masked = nir, red
    
    # Calculate NDVI with division by zero protection
    denominator = nir_masked + red_masked
    ndvi_values = np.where(
        np.abs(denominator) > 1e-8,
        (nir_masked - red_masked) / denominator,
        np.nan
    )
    
    return IndexResult(
        values=ndvi_values,
        name="NDVI",
        valid_range=(-1.0, 1.0),
        formula="(NIR - Red) / (NIR + Red)",
        source="Rouse et al., 1974, NASA SP-351"
    )


def evi(
    nir: np.ndarray,
    red: np.ndarray,
    blue: np.ndarray,
    G: float = 2.5,
    C1: float = 6.0,
    C2: float = 7.5,
    L: float = 1.0,
    nodata: Optional[float] = None
) -> IndexResult:
    """
    Calculate Enhanced Vegetation Index
    
    Formula: G * ((NIR - Red) / (NIR + C1*Red - C2*Blue + L))
    Source: Huete et al., 2002, "Overview of the radiometric and biophysical 
            performance of the MODIS vegetation indices", Remote Sensing of Environment
    
    Args:
        nir: Near-infrared band
        red: Red band
        blue: Blue band
        G: Gain factor (default: 2.5)
        C1: Red aerosol correction coefficient (default: 6.0)
        C2: Blue aerosol correction coefficient (default: 7.5)
        L: Canopy background adjustment (default: 1.0)
        nodata: No data value to mask
    
    Returns:
        IndexResult with EVI values
    
    Advantages over NDVI:
        - Reduced atmospheric effects
        - Reduced soil background effects
        - Better sensitivity in high biomass regions
        - Does not saturate as quickly as NDVI
    """
    if nodata is not None:
        mask = (nir != nodata) & (red != nodata) & (blue != nodata)
        nir_masked = np.where(mask, nir, np.nan)
        red_masked = np.where(mask, red, np.nan)
        blue_masked = np.where(mask, blue, np.nan)
    else:
        nir_masked, red_masked, blue_masked = nir, red, blue
    
    denominator = nir_masked + C1 * red_masked - C2 * blue_masked + L
    evi_values = np.where(
        np.abs(denominator) > 1e-8,
        G * (nir_masked - red_masked) / denominator,
        np.nan
    )
    
    return IndexResult(
        values=evi_values,
        name="EVI",
        valid_range=(-1.0, 1.0),
        formula=f"{G} * ((NIR - Red) / (NIR + {C1}*Red - {C2}*Blue + {L}))",
        source="Huete et al., 2002, RSE"
    )


def ndre(
    nir: np.ndarray,
    red_edge: np.ndarray,
    nodata: Optional[float] = None
) -> IndexResult:
    """
    Calculate Normalized Difference Red Edge Index
    
    Formula: (NIR - RedEdge) / (NIR + RedEdge)
    Source: Gitelson & Merzlyak, 1994, "Spectral reflectance changes 
            associated with autumn senescence"
    
    Args:
        nir: Near-infrared band (e.g., Sentinel-2 B8)
        red_edge: Red edge band (e.g., Sentinel-2 B5)
        nodata: No data value to mask
    
    Returns:
        IndexResult with NDRE values
    
    Applications:
        - Sensitive to chlorophyll content
        - Monitors vegetation stress
        - Less sensitive to atmospheric effects than NDVI
        - Useful for canopy closure estimation
    """
    if nodata is not None:
        mask = (nir != nodata) & (red_edge != nodata)
        nir_masked = np.where(mask, nir, np.nan)
        re_masked = np.where(mask, red_edge, np.nan)
    else:
        nir_masked, re_masked = nir, red_edge
    
    denominator = nir_masked + re_masked
    ndre_values = np.where(
        np.abs(denominator) > 1e-8,
        (nir_masked - re_masked) / denominator,
        np.nan
    )
    
    return IndexResult(
        values=ndre_values,
        name="NDRE",
        valid_range=(-1.0, 1.0),
        formula="(NIR - RedEdge) / (NIR + RedEdge)",
        source="Gitelson & Merzlyak, 1994"
    )


def gndvi(
    nir: np.ndarray,
    green: np.ndarray,
    nodata: Optional[float] = None
) -> IndexResult:
    """
    Calculate Green Normalized Difference Vegetation Index
    
    Formula: (NIR - Green) / (NIR + Green)
    Source: Gitelson et al., 1996, "Use of a green channel in remote sensing 
            of global vegetation from EOS-MODIS"
    
    Args:
        nir: Near-infrared band
        green: Green band
        nodata: No data value to mask
    
    Returns:
        IndexResult with GNDVI values
    
    Applications:
        - More sensitive to chlorophyll concentration than NDVI
        - Better for nitrogen status assessment
        - Useful in agriculture and forest monitoring
    """
    if nodata is not None:
        mask = (nir != nodata) & (green != nodata)
        nir_masked = np.where(mask, nir, np.nan)
        green_masked = np.where(mask, green, np.nan)
    else:
        nir_masked, green_masked = nir, green
    
    denominator = nir_masked + green_masked
    gndvi_values = np.where(
        np.abs(denominator) > 1e-8,
        (nir_masked - green_masked) / denominator,
        np.nan
    )
    
    return IndexResult(
        values=gndvi_values,
        name="GNDVI",
        valid_range=(-1.0, 1.0),
        formula="(NIR - Green) / (NIR + Green)",
        source="Gitelson et al., 1996"
    )


# REMOVED: def savi() function
# REASON: SAVI is used for soil-brightness correction in vegetation indices.
#         For USA projects with Sentinel-2 restricted to QA/visualization only,
#         SAVI is not needed. Use NDVI or NDMI instead.
#         If soil-adjusted indices are required, use GEDI/FIA ground data.


def msi(
    swir: np.ndarray,
    nir: np.ndarray,
    nodata: Optional[float] = None
) -> IndexResult:
    """
    Calculate Moisture Stress Index
    
    Formula: SWIR / NIR
    Source: Rock et al., 1986, "Remote detection of forest damage"
    
    Args:
        swir: Short-wave infrared band (e.g., Sentinel-2 B11)
        nir: Near-infrared band
        nodata: No data value to mask
    
    Returns:
        IndexResult with MSI values
    
    Interpretation:
        - Higher values indicate water stress
        - Lower values indicate high water content
        - Typical range: 0.2 (wet) to 2.0 (stressed)
    """
    if nodata is not None:
        mask = (swir != nodata) & (nir != nodata)
        swir_masked = np.where(mask, swir, np.nan)
        nir_masked = np.where(mask, nir, np.nan)
    else:
        swir_masked, nir_masked = swir, nir
    
    msi_values = np.where(
        np.abs(nir_masked) > 1e-8,
        swir_masked / nir_masked,
        np.nan
    )
    
    return IndexResult(
        values=msi_values,
        name="MSI",
        valid_range=(0.0, 3.0),
        formula="SWIR / NIR",
        source="Rock et al., 1986"
    )


def ndwi(
    nir: np.ndarray,
    swir: np.ndarray,
    nodata: Optional[float] = None
) -> IndexResult:
    """
    Calculate Normalized Difference Water Index (vegetation water content)
    
    Formula: (NIR - SWIR) / (NIR + SWIR)
    Source: Gao, 1996, "NDWI - A normalized difference water index for 
            remote sensing of vegetation liquid water from space", 
            Remote Sensing of Environment
    
    Args:
        nir: Near-infrared band
        swir: Short-wave infrared band (e.g., Sentinel-2 B11)
        nodata: No data value to mask
    
    Returns:
        IndexResult with NDWI values
    
    Note: This is the vegetation water content NDWI (Gao, 1996), 
          not the water body NDWI (McFeeters, 1996)
    
    Interpretation:
        - Range: [-1, 1]
        - Higher values = more water content
        - Lower values = drought stress
        - Typical vegetation: 0.2 to 0.8
    """
    if nodata is not None:
        mask = (nir != nodata) & (swir != nodata)
        nir_masked = np.where(mask, nir, np.nan)
        swir_masked = np.where(mask, swir, np.nan)
    else:
        nir_masked, swir_masked = nir, swir
    
    denominator = nir_masked + swir_masked
    ndwi_values = np.where(
        np.abs(denominator) > 1e-8,
        (nir_masked - swir_masked) / denominator,
        np.nan
    )
    
    return IndexResult(
        values=ndwi_values,
        name="NDWI",
        valid_range=(-1.0, 1.0),
        formula="(NIR - SWIR) / (NIR + SWIR)",
        source="Gao, 1996, RSE"
    )


def calculate_all_indices(
    bands: Dict[str, np.ndarray],
    nodata: Optional[float] = None
) -> Dict[str, IndexResult]:
    """
    Calculate all available indices from band dictionary.
    
    Args:
        bands: Dictionary with band names as keys, arrays as values
               Required: 'B2', 'B3', 'B4', 'B5', 'B8', 'B11' (Sentinel-2)
        nodata: No data value to mask
    
    Returns:
        Dictionary of IndexResult objects
    """
    results = {}
    
    # NDVI (requires NIR, Red)
    if 'B8' in bands and 'B4' in bands:
        results['NDVI'] = ndvi(bands['B8'], bands['B4'], nodata)
        logger.debug("Calculated NDVI")
    
    # EVI (requires NIR, Red, Blue)
    if 'B8' in bands and 'B4' in bands and 'B2' in bands:
        results['EVI'] = evi(bands['B8'], bands['B4'], bands['B2'], nodata=nodata)
        logger.debug("Calculated EVI")
    
    # NDRE (requires NIR, Red Edge)
    if 'B8' in bands and 'B5' in bands:
        results['NDRE'] = ndre(bands['B8'], bands['B5'], nodata)
        logger.debug("Calculated NDRE")
    
    # GNDVI (requires NIR, Green)
    if 'B8' in bands and 'B3' in bands:
        results['GNDVI'] = gndvi(bands['B8'], bands['B3'], nodata)
        logger.debug("Calculated GNDVI")
    
    # SAVI removed (not needed for Sentinel-2 QA/visualization)
    
    # MSI (requires SWIR, NIR)
    if 'B11' in bands and 'B8' in bands:
        results['MSI'] = msi(bands['B11'], bands['B8'], nodata)
        logger.debug("Calculated MSI")
    
    # NDWI (requires NIR, SWIR)
    if 'B8' in bands and 'B11' in bands:
        results['NDWI'] = ndwi(bands['B8'], bands['B11'], nodata)
        logger.debug("Calculated NDWI")
    
    logger.info(f"Calculated {len(results)} vegetation indices")
    
    return results


if __name__ == "__main__":
    # Test module
    logger.info("Spectral indices module loaded successfully")
    logger.info("Available functions:")
    logger.info("  - ndvi()")
    logger.info("  - evi()")
    logger.info("  - ndre()")
    logger.info("  - gndvi()")
    logger.info("  - msi()")
    logger.info("  - ndwi()")
    logger.info("  - calculate_all_indices()")
