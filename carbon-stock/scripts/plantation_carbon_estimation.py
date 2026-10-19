"""
⚠️  DEPRECATED SCRIPT - USE usa_forest_carbon_estimation.py INSTEAD ⚠️

Plantation Carbon Estimation Pipeline (DEPRECATED)
===================================================

This script uses ML-based height prediction and species-specific allometry,
which is NOT compliant with USA carbon accounting standards.

PROBLEMS WITH THIS APPROACH:
1. Uses GEDI for ML training (incorrect - GEDI L4A is authoritative)
2. Predicts height from SAR (unreliable without calibration)
3. Requires species input and tree counts (rarely available accurately)
4. Uses generic allometric equations (site-specific calibration needed)
5. Total uncertainty: ±35-45% (too high for carbon stock monetization)

✅ CORRECT APPROACH FOR USA:
Use scripts/usa_forest_carbon_estimation.py which:
- Fetches GEDI L4A authoritative biomass (FIA-calibrated)
- Uses IPCC default carbon factors
- NO ML prediction or height estimation
- Uncertainty: ±12-25% (suitable for USFS/EPA accounting)

MIGRATION:
Old: python scripts/plantation_carbon_estimation.py --species teak --trees 5000 ...
New: python scripts/gedi_l4a_authoritative.py --bbox W S E N --output gedi.geojson
     python scripts/usa_forest_carbon_estimation.py --gedi-geojson gedi.geojson --forest-mask mask.tif --output carbon.json

DATE DEPRECATED: February 1, 2026
REMOVAL SCHEDULED: March 1, 2026

==================================================

ORIGINAL DOCUMENTATION (FOR REFERENCE):

SCOPE: Single-species plantations only
INPUTS: User-provided species name, known tree count, SAR + optical data
OUTPUTS: Carbon stock estimates with uncertainty quantification

LIMITATIONS:
- Height from SAR: ±25-30% uncertainty (NO LiDAR available)
- Wood density: Literature values (NOT site-measured)
- Allometry: Generic equations (NOT site-calibrated)
- Total uncertainty: ±35-45% (NOT suitable for carbon stock monetization)

USE CASE: Monitoring plantation carbon dynamics, NOT regulatory compliance
"""

import argparse
import json
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Tuple, Optional

import numpy as np
import rasterio
from rasterio.transform import from_bounds

# Configure logging
logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Print deprecation warning
print("")
print("=" * 80)
print("⚠️  DEPRECATION WARNING")
print("=" * 80)
print("This script (plantation_carbon_estimation.py) is DEPRECATED.")
print("")
print("REASON:")
print("  Uses ML-based height prediction and species allometry, which is NOT")
print("  compliant with USA carbon accounting standards (USFS/EPA).")
print("")
print("✅ USE INSTEAD:")
print("  1. scripts/gedi_l4a_authoritative.py (fetch GEDI L4A biomass)")
print("  2. scripts/usa_forest_carbon_estimation.py (calculate carbon)")
print("")
print("NEW APPROACH:")
print("  • GEDI L4A: Authoritative biomass (FIA-calibrated)")
print("  • NO ML prediction or height estimation")
print("  • IPCC default carbon factors")
print("  • Uncertainty: ±12-25% (vs ±35-45% with old method)")
print("=" * 80)
print("")

import warnings
warnings.warn(
    "plantation_carbon_estimation.py is deprecated. "
    "Use usa_forest_carbon_estimation.py for USA-compliant carbon accounting.",
    DeprecationWarning,
    stacklevel=2
)


# Carbon estimation is area-based and derived directly from GEDI L4A biomass (no tree-level logic)
def estimate_carbon_area_based(
    forest_area_ha: float,
    mean_agb_Mg_ha: float,
    agb_uncertainty_Mg_ha: Optional[float] = None,
    **kwargs
) -> dict:
    """
    Area-based carbon estimation using GEDI L4A biomass.
    
    Args:
        forest_area_ha: Forest area in hectares
        mean_agb_Mg_ha: Mean above-ground biomass density (Mg/ha) from GEDI L4A
        agb_uncertainty_Mg_ha: Optional uncertainty in biomass (Mg/ha)
        **kwargs: Ignored tree-based inputs (tree_count, species, height, dbh)
    
    Returns:
        Dictionary with carbon stock estimates
    """
    # Check for tree-based inputs and log warning
    tree_based_inputs = [k for k in kwargs.keys() if k in ['tree_count', 'species', 'height', 'dbh', 'number_of_trees']]
    if tree_based_inputs:
        logger.warning(f"Ignoring tree- or species-based inputs: area-based estimation in use. Ignored: {', '.join(tree_based_inputs)}")
    
    # Carbon calculation (IPCC defaults)
    total_agb_Mg = mean_agb_Mg_ha * forest_area_ha
    total_carbon_Mg = total_agb_Mg * 0.47
    total_co2e_Mg = total_carbon_Mg * 3.67
    
    # Uncertainty calculation
    if agb_uncertainty_Mg_ha is not None:
        co2e_uncertainty_Mg = agb_uncertainty_Mg_ha * forest_area_ha * 0.47 * 3.67
        uncertainty_percent = (co2e_uncertainty_Mg / total_co2e_Mg) * 100
    else:
        uncertainty_percent = None
    
    return {
        "forest_area_ha": forest_area_ha,
        "mean_agb_Mg_ha": mean_agb_Mg_ha,
        "total_agb_Mg": total_agb_Mg,
        "total_carbon_Mg": total_carbon_Mg,
        "total_co2e_Mg": total_co2e_Mg,
        "uncertainty_percent": uncertainty_percent,
        "methodology": "Area-based carbon stock estimation using GEDI L4A AGB"
    }


@dataclass
class SpeciesData:
    """Species-specific parameters for biomass calculation"""
    common_name: str
    scientific_name: str
    wood_density: float  # g/cm³
    allometry_a: float  # Biomass = a * H^b
    allometry_b: float
    carbon_fraction: float  # default 0.47
    source: str  # Literature reference


# Plantation species database
SPECIES_DATABASE = {
    'teak': SpeciesData(
        common_name='Teak',
        scientific_name='Tectona grandis',
        wood_density=0.55,  # g/cm³
        allometry_a=0.1428,  # Biomass (kg) = 0.1428 * H^2.343 (Perez & Kanninen 2003)
        allometry_b=2.343,
        carbon_fraction=0.47,
        source='Perez & Kanninen (2003), ICRAF Wood Density Database'
    ),
    'eucalyptus': SpeciesData(
        common_name='Eucalyptus',
        scientific_name='Eucalyptus spp.',
        wood_density=0.65,
        allometry_a=0.0876,  # Biomass = 0.0876 * H^2.471 (generic Eucalyptus)
        allometry_b=2.471,
        carbon_fraction=0.47,
        source='Ribeiro et al. (2015), Global Wood Density Database'
    ),
    'mahogany': SpeciesData(
        common_name='Mahogany',
        scientific_name='Swietenia macrophylla',
        wood_density=0.55,
        allometry_a=0.1235,
        allometry_b=2.398,
        carbon_fraction=0.47,
        source='Chave et al. (2014), ICRAF Database'
    ),
    'rubber': SpeciesData(
        common_name='Rubber',
        scientific_name='Hevea brasiliensis',
        wood_density=0.56,
        allometry_a=0.0985,
        allometry_b=2.512,
        carbon_fraction=0.47,
        source='Wauters et al. (2008), ICRAF Database'
    ),
    'pine': SpeciesData(
        common_name='Pine',
        scientific_name='Pinus spp.',
        wood_density=0.48,
        allometry_a=0.0645,
        allometry_b=2.621,
        carbon_fraction=0.47,
        source='Jenkins et al. (2003), softwood generic'
    ),
    'acacia': SpeciesData(
        common_name='Acacia',
        scientific_name='Acacia mangium',
        wood_density=0.52,
        allometry_a=0.1089,
        allometry_b=2.445,
        carbon_fraction=0.47,
        source='Krisnawati et al. (2011), ICRAF Database'
    ),
}


class HeightEstimator:
    """
    Estimate canopy height with GEDI calibration option
    
    Methods:
    1. GEDI-calibrated (preferred): Random Forest trained on GEDI + Sentinel
       Uncertainty: ±10-15%
    
    2. SAR proxy (fallback): Empirical correlation from literature
       Uncertainty: ±28%
    """
    
    def __init__(self, gedi_calibration_result=None, gedi_height_map=None):
        """
        Args:
            gedi_calibration_result: GEDICalibrationResult object (if available)
            gedi_height_map: Pre-computed GEDI-calibrated height raster (if available)
        """
        self.gedi_result = gedi_calibration_result
        self.gedi_height_map = gedi_height_map
        self.method_used = None
        
        # SAR-only fallback coefficients
        self.sar_coef_intercept = 5.0
        self.sar_coef_vv = 1.5
        self.sar_coef_vh = 2.0
        self.sar_coef_ndvi = 8.0
        self.sar_uncertainty_pct = 28.0
    
    def estimate_height(
        self,
        vv_db: np.ndarray,
        vh_db: np.ndarray,
        ndvi: np.ndarray,
        mask: np.ndarray
    ) -> Tuple[float, float]:
        """
        Estimate mean canopy height for plantation
        
        Returns:
            mean_height (m), uncertainty (m)
        """
        # Method 1: Use GEDI-calibrated height map (preferred)
        if self.gedi_height_map is not None and self.gedi_result and self.gedi_result.gedi_used:
            heights = self.gedi_height_map[mask]
            heights = heights[heights != -9999]  # Remove nodata
            
            if len(heights) > 0:
                mean_height = np.mean(heights)
                std_height = np.std(heights)
                
                # Uncertainty from GEDI calibration
                uncertainty = mean_height * (self.gedi_result.uncertainty_pct / 100.0)
                
                self.method_used = 'gedi_calibrated'
                
                logger.info(
                    f"✓ Height from GEDI-calibrated model (Random Forest)\n"
                    f"   Mean height: {mean_height:.1f} m ± {uncertainty:.1f} m "
                    f"({self.gedi_result.uncertainty_pct:.1f}% uncertainty)\n"
                    f"   RMSE: {self.gedi_result.rmse:.2f} m, R²: {self.gedi_result.r2:.3f}\n"
                    f"   Training points: {self.gedi_result.n_training_points}"
                )
                
                return mean_height, uncertainty
        
        # Method 2: SAR-only fallback
        self.method_used = 'sar_proxy'
        
        vv_masked = vv_db[mask]
        vh_masked = vh_db[mask]
        ndvi_masked = ndvi[mask]
        
        # Normalize inputs (0-1)
        vv_norm = (vv_masked - vv_masked.min()) / (vv_masked.max() - vv_masked.min() + 1e-6)
        vh_norm = (vh_masked - vh_masked.min()) / (vh_masked.max() - vh_masked.min() + 1e-6)
        ndvi_norm = np.clip(ndvi_masked, 0, 1)
        
        # Empirical height model
        height_pixels = (
            self.sar_coef_intercept +
            self.sar_coef_vv * vv_norm +
            self.sar_coef_vh * vh_norm +
            self.sar_coef_ndvi * ndvi_norm
        )
        
        mean_height = np.mean(height_pixels)
        std_height = np.std(height_pixels)
        
        # Total uncertainty
        model_uncertainty = mean_height * (self.sar_uncertainty_pct / 100.0)
        total_uncertainty = np.sqrt(model_uncertainty**2 + std_height**2)
        
        logger.warning(
            f"⚠️ Height estimated from SAR backscatter (NO GEDI/LiDAR validation)\n"
            f"   Mean height: {mean_height:.1f} m ± {total_uncertainty:.1f} m "
            f"({total_uncertainty/mean_height*100:.0f}% uncertainty)\n"
            f"   Method: Empirical correlation (NOT site-calibrated)\n"
            f"   Recommendation: Use GEDI calibration to reduce uncertainty"
        )
        
        return mean_height, total_uncertainty


class PlantationCarbonCalculator:
    """
    Calculate carbon stock for single-species plantations
    
    Pipeline:
    1. Height estimation from SAR (±28%)
    2. Species-specific allometry: Height → Biomass
    3. Carbon conversion: Biomass × Carbon_fraction
    4. CO2 equivalent: Carbon × 3.67
    
    UNCERTAINTY PROPAGATION:
    - Height uncertainty: ±28%
    - Allometry uncertainty: ±15-20%
    - Total uncertainty: ±35-45%
    """
    
    def __init__(self, species_key: str):
        if species_key.lower() not in SPECIES_DATABASE:
            available = ', '.join(SPECIES_DATABASE.keys())
            raise ValueError(
                f"Species '{species_key}' not in database.\n"
                f"Available: {available}"
            )
        
        self.species = SPECIES_DATABASE[species_key.lower()]
        self.allometry_uncertainty_pct = 18.0  # Generic allometry error
    
    def calculate_tree_biomass(
        self,
        height_m: float,
        height_uncertainty_m: float
    ) -> Tuple[float, float]:
        """
        Calculate individual tree biomass using allometric equation
        
        Biomass (kg/tree) = a * Height^b
        
        Returns:
            biomass (kg/tree), uncertainty (kg/tree)
        """
        # Mean biomass
        biomass_kg = self.species.allometry_a * (height_m ** self.species.allometry_b)
        
        # Uncertainty propagation (first-order Taylor approximation)
        # ΔB/B ≈ b * ΔH/H (for B = a*H^b)
        relative_height_error = height_uncertainty_m / height_m
        relative_biomass_error = self.species.allometry_b * relative_height_error
        
        # Add allometry model uncertainty
        total_relative_error = np.sqrt(
            relative_biomass_error**2 +
            (self.allometry_uncertainty_pct / 100.0)**2
        )
        
        biomass_uncertainty = biomass_kg * total_relative_error
        
        return biomass_kg, biomass_uncertainty
    
    def calculate_carbon_stock(
        self,
        plantation_area_ha: float,
        tree_count: int,
        mean_height_m: float,
        height_uncertainty_m: float
    ) -> Dict:
        """
        Calculate plantation carbon stock
        
        Returns:
            Dictionary with carbon metrics and uncertainties
        """
        # Tree biomass
        biomass_per_tree, biomass_uncertainty = self.calculate_tree_biomass(
            mean_height_m, height_uncertainty_m
        )
        
        # Total biomass (kg → Mg)
        total_biomass_mg = (biomass_per_tree * tree_count) / 1000.0
        total_biomass_uncertainty_mg = (biomass_uncertainty * tree_count) / 1000.0
        
        # Biomass density (Mg/ha)
        biomass_density_mg_ha = total_biomass_mg / plantation_area_ha
        biomass_density_uncertainty = total_biomass_uncertainty_mg / plantation_area_ha
        
        # Carbon stock (Mg C)
        carbon_mg = total_biomass_mg * self.species.carbon_fraction
        carbon_uncertainty_mg = total_biomass_uncertainty_mg * self.species.carbon_fraction
        
        # Carbon density (Mg C/ha)
        carbon_density_mg_ha = carbon_mg / plantation_area_ha
        carbon_density_uncertainty = carbon_uncertainty_mg / plantation_area_ha
        
        # CO2 equivalent (Mg CO2e)
        co2e_mg = carbon_mg * 3.67  # Conversion factor C → CO2
        co2e_uncertainty_mg = carbon_uncertainty_mg * 3.67
        
        # CO2 density (Mg CO2e/ha)
        co2e_density_mg_ha = co2e_mg / plantation_area_ha
        co2e_density_uncertainty = co2e_uncertainty_mg / plantation_area_ha
        
        # Calculate relative uncertainty %
        relative_uncertainty_pct = (co2e_uncertainty_mg / co2e_mg) * 100.0
        
        results = {
            'species': {
                'common_name': self.species.common_name,
                'scientific_name': self.species.scientific_name,
                'wood_density_g_cm3': float(self.species.wood_density),
                'allometry_equation': f"Biomass = {self.species.allometry_a:.4f} × Height^{self.species.allometry_b:.3f}",
                'source': self.species.source
            },
            'plantation': {
                'area_ha': float(plantation_area_ha),
                'tree_count': int(tree_count),
                'tree_density_per_ha': float(tree_count / plantation_area_ha)
            },
            'height': {
                'mean_m': float(round(mean_height_m, 2)),
                'uncertainty_m': float(round(height_uncertainty_m, 2)),
                'relative_uncertainty_pct': float(round((height_uncertainty_m / mean_height_m) * 100, 1)),
                'method': 'SAR backscatter correlation (Sentinel-1)',
                'validation': 'NONE - generic model, not site-calibrated'
            },
            'biomass': {
                'per_tree_kg': float(round(biomass_per_tree, 2)),
                'per_tree_uncertainty_kg': float(round(biomass_uncertainty, 2)),
                'total_mg': float(round(total_biomass_mg, 2)),
                'total_uncertainty_mg': float(round(total_biomass_uncertainty_mg, 2)),
                'density_mg_ha': float(round(biomass_density_mg_ha, 2)),
                'density_uncertainty_mg_ha': float(round(biomass_density_uncertainty, 2))
            },
            'carbon': {
                'total_mg_c': float(round(carbon_mg, 2)),
                'total_uncertainty_mg_c': float(round(carbon_uncertainty_mg, 2)),
                'density_mg_c_ha': float(round(carbon_density_mg_ha, 2)),
                'density_uncertainty_mg_c_ha': float(round(carbon_density_uncertainty, 2)),
                'carbon_fraction': float(self.species.carbon_fraction)
            },
            'co2_equivalent': {
                'total_mg_co2e': float(round(co2e_mg, 2)),
                'total_uncertainty_mg_co2e': float(round(co2e_uncertainty_mg, 2)),
                'density_mg_co2e_ha': float(round(co2e_density_mg_ha, 2)),
                'density_uncertainty_mg_co2e_ha': float(round(co2e_density_uncertainty, 2)),
                'relative_uncertainty_pct': float(round(relative_uncertainty_pct, 1))
            },
            'limitations': [
                'Height from SAR (±28%) - NO LiDAR validation',
                'Generic allometry (±18%) - NOT site-calibrated',
                f'Total uncertainty: ±{relative_uncertainty_pct:.0f}% - NOT suitable for carbon stock monetization',
                'Wood density from literature - NOT field-measured',
                'Single-age cohort assumed - age variation NOT accounted',
                'Mortality and understory biomass NOT included',
                'Soil carbon and root biomass NOT included',
                'NO registry compliance - monitoring & analysis only'
            ]
        }
        
        return results


def load_raster_band(filepath: Path, band_name: str) -> Tuple[np.ndarray, dict]:
    """Load a single band from raster"""
    with rasterio.open(filepath) as src:
        if hasattr(src, 'descriptions') and band_name in src.descriptions:
            band_idx = src.descriptions.index(band_name) + 1
            data = src.read(band_idx)
        else:
            # Assume single band
            data = src.read(1)
        
        metadata = {
            'transform': src.transform,
            'crs': src.crs,
            'bounds': src.bounds,
            'shape': data.shape
        }
    
    return data, metadata


def main():
    parser = argparse.ArgumentParser(
        description='Plantation Carbon Estimation (Single-Species)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python plantation_carbon_estimation.py \\
      --s2 data/processed/sentinel2_composite.tif \\
      --s1 data/processed/sentinel1_composite.tif \\
      --species teak \\
      --area 3.0 \\
      --trees 300 \\
      --output outputs/carbon_estimation/

Available species: teak, eucalyptus, mahogany, rubber, pine, acacia

LIMITATIONS:
  - Height uncertainty: ±28% (SAR-based, no LiDAR)
  - Total uncertainty: ±35-45%
  - NOT suitable for carbon stock monetization
  - Monitoring & analysis only
        """
    )
    
    parser.add_argument('--s2', type=Path, required=True,
                       help='Sentinel-2 composite (must contain NDVI band)')
    parser.add_argument('--s1', type=Path, required=True,
                       help='Sentinel-1 composite (must contain VV, VH bands)')
    parser.add_argument('--species', type=str, required=True,
                       help='Plantation species (teak, eucalyptus, mahogany, rubber, pine, acacia)')
    parser.add_argument('--area', type=float, required=True,
                       help='Plantation area in hectares')
    parser.add_argument('--trees', type=int, required=True,
                       help='Number of trees (user-provided ground count)')
    parser.add_argument('--output', type=Path, required=True,
                       help='Output directory for results')
    parser.add_argument('--gedi-points', type=Path, required=False,
                       help='Path to GEDI points GeoJSON (optional, for calibration)')
    parser.add_argument('--use-gedi', action='store_true',
                       help='Use GEDI calibration if available')
    
    args = parser.parse_args()
    
    # Validate species
    if args.species.lower() not in SPECIES_DATABASE:
        available = ', '.join(SPECIES_DATABASE.keys())
        logger.error(f"Species '{args.species}' not in database. Available: {available}")
        return 1
    
    # Create output directory
    args.output.mkdir(parents=True, exist_ok=True)
    
    logger.info("="*70)
    logger.info("PLANTATION CARBON ESTIMATION PIPELINE")
    logger.info("="*70)
    logger.info(f"Species: {args.species}")
    logger.info(f"Area: {args.area} ha")
    logger.info(f"Trees: {args.trees} ({args.trees/args.area:.1f} trees/ha)")
    logger.info("")
    
    # Load data
    logger.info("Loading satellite data...")
    try:
        # Load using rasterio directly
        with rasterio.open(args.s2) as src:
            # Try to find NDVI band
            ndvi_band_idx = None
            if hasattr(src, 'descriptions') and src.descriptions:
                for i, desc in enumerate(src.descriptions, 1):
                    if desc and 'NDVI' in desc.upper():
                        ndvi_band_idx = i
                        break
            if ndvi_band_idx is None:
                ndvi_band_idx = 1  # Default to first band
            ndvi = src.read(ndvi_band_idx).astype(np.float32)
        
        with rasterio.open(args.s1) as src:
            # Try to find VV and VH bands
            vv_idx, vh_idx = None, None
            if hasattr(src, 'descriptions') and src.descriptions:
                for i, desc in enumerate(src.descriptions, 1):
                    if desc:
                        if 'VV' in desc.upper():
                            vv_idx = i
                        elif 'VH' in desc.upper():
                            vh_idx = i
            # Default to first two bands if not found
            if vv_idx is None:
                vv_idx = 1
            if vh_idx is None:
                vh_idx = 2 if src.count >= 2 else 1
            
            vv_db = src.read(vv_idx).astype(np.float32)
            vh_db = src.read(vh_idx).astype(np.float32)
        
    except Exception as e:
        logger.error(f"Failed to load data: {e}")
        return 1
    
    # Create plantation mask (use AOI extent - all pixels are plantation)
    logger.info("Creating plantation mask...")
    plantation_mask = ~np.isnan(ndvi)
    logger.info(f"Plantation pixels: {plantation_mask.sum()}")
    logger.info(f"Using AOI boundary as plantation extent: {args.area} ha")
    logger.info("")
    
    # GEDI Calibration (if requested and available)
    gedi_result = None
    gedi_height_map = None
    
    if args.use_gedi and args.gedi_points and args.gedi_points.exists():
        logger.info("="*70)
        logger.info("GEDI CALIBRATION")
        logger.info("="*70)
        
        try:
            # Add parent directory to path for src module import
            import sys
            from pathlib import Path as PathLib
            sys.path.insert(0, str(PathLib(__file__).parent.parent))
            
            from src.gedi_calibration import calibrate_height_with_gedi
            
            # Run GEDI calibration
            # Create output directory from the output JSON path
            output_dir = args.output.parent / (args.output.stem + "_gedi")
            output_dir.mkdir(parents=True, exist_ok=True)
            
            gedi_height_output = output_dir / "gedi_calibrated_height.tif"
            gedi_model_output = output_dir / "gedi_height_model.pkl"
            
            gedi_result = calibrate_height_with_gedi(
                gedi_geojson_path=args.gedi_points,
                s2_path=args.s2,
                s1_path=args.s1,
                output_height_map=gedi_height_output,
                output_model=gedi_model_output
            )
            
            if gedi_result.gedi_used:
                # Load height map
                with rasterio.open(gedi_height_output) as src:
                    gedi_height_map = src.read(1)
                
                logger.info(f"✓ GEDI calibration successful")
                logger.info(f"  Uncertainty reduced: 28% → {gedi_result.uncertainty_pct:.1f}%")
            else:
                logger.warning("⚠️  GEDI calibration failed - using SAR proxy")
        
        except Exception as e:
            logger.error(f"GEDI calibration error: {e}")
            logger.warning("Falling back to SAR proxy")
    
    elif args.use_gedi and not args.gedi_points:
        logger.warning("--use-gedi specified but --gedi-points not provided")
    
    # Estimate height from SAR
    logger.info("Estimating canopy height...")
    height_estimator = HeightEstimator(
        gedi_calibration_result=gedi_result,
        gedi_height_map=gedi_height_map
    )
    mean_height, height_uncertainty = height_estimator.estimate_height(
        vv_db, vh_db, ndvi, plantation_mask
    )
    logger.info("")
    
    # Calculate carbon
    logger.info("Calculating carbon stock...")
    carbon_calculator = PlantationCarbonCalculator(args.species)
    results = carbon_calculator.calculate_carbon_stock(
        plantation_area_ha=args.area,
        tree_count=args.trees,
        mean_height_m=mean_height,
        height_uncertainty_m=height_uncertainty
    )
    
    # Save results
    output_json = args.output / 'carbon_estimation_results.json'
    with open(output_json, 'w') as f:
        json.dump(results, f, indent=2)
    
    logger.info("="*70)
    logger.info("RESULTS SUMMARY")
    logger.info("="*70)
    logger.info(f"Species: {results['species']['common_name']} ({results['species']['scientific_name']})")
    logger.info(f"Plantation: {results['plantation']['area_ha']} ha, {results['plantation']['tree_count']} trees")
    logger.info("")
    logger.info(f"Mean Height: {results['height']['mean_m']} ± {results['height']['uncertainty_m']} m "
               f"({results['height']['relative_uncertainty_pct']}% uncertainty)")
    logger.info("")
    logger.info(f"Biomass per Tree: {results['biomass']['per_tree_kg']} ± {results['biomass']['per_tree_uncertainty_kg']} kg")
    logger.info(f"Total Biomass: {results['biomass']['total_mg']} ± {results['biomass']['total_uncertainty_mg']} Mg")
    logger.info(f"Biomass Density: {results['biomass']['density_mg_ha']} ± {results['biomass']['density_uncertainty_mg_ha']} Mg/ha")
    logger.info("")
    logger.info(f"Total Carbon: {results['carbon']['total_mg_c']} ± {results['carbon']['total_uncertainty_mg_c']} Mg C")
    logger.info(f"Carbon Density: {results['carbon']['density_mg_c_ha']} ± {results['carbon']['density_uncertainty_mg_c_ha']} Mg C/ha")
    logger.info("")
    logger.info(f"🌲 CO2 Equivalent: {results['co2_equivalent']['total_mg_co2e']} ± {results['co2_equivalent']['total_uncertainty_mg_co2e']} Mg CO2e")
    logger.info(f"   CO2e Density: {results['co2_equivalent']['density_mg_co2e_ha']} ± {results['co2_equivalent']['density_uncertainty_mg_co2e_ha']} Mg CO2e/ha")
    logger.info(f"   Uncertainty: ±{results['co2_equivalent']['relative_uncertainty_pct']}%")
    logger.info("")
    logger.info("⚠️ LIMITATIONS:")
    for limitation in results['limitations']:
        logger.info(f"   - {limitation}")
    logger.info("")
    logger.info(f"Results saved: {output_json}")
    logger.info("="*70)
    
    return 0


if __name__ == '__main__':
    exit(main())
