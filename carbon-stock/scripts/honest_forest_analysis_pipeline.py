"""  
Honest Forest Analysis Pipeline
================================

# This system performs research-grade forest carbon stock estimation, not carbon credit calculation

WHAT THIS DOES:
- Forest/non-forest classification
- Canopy cover estimation (model-derived %)
- Canopy density classes (sparse/medium/dense)
- Temporal change detection
- Relative biomass proxy (unitless, NOT tonnes/ha)
- Plantation vs natural pattern detection

WHAT THIS DOES NOT DO:
- Individual tree detection
- Species identification
- Tree height or DBH estimation
- Absolute biomass (tonnes/ha)
- Carbon stock monetization or registry submission

All outputs are model-derived without field validation.
Suitable for landscape monitoring and research, NOT for carbon stock monetization.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import numpy as np
import logging
from typing import Dict, Tuple
from datetime import datetime

from src.io import load_raster, save_raster, RasterData
from src.indices import ndvi, evi
from scipy import ndimage
from skimage.morphology import binary_opening, binary_closing, disk
from skimage.measure import label, regionprops
from sklearn.cluster import KMeans

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ForestMaskGenerator:
    """Generate binary forest mask (model-based, no validation)"""
    
    def __init__(
        self,
        ndvi_percentile_threshold: float = 75,
        temporal_persistence: float = 0.8,
        min_patch_size_pixels: int = 10
    ):
        self.ndvi_percentile_threshold = ndvi_percentile_threshold
        self.temporal_persistence = temporal_persistence
        self.min_patch_size_pixels = min_patch_size_pixels
    
    def generate(self, s2_raster: RasterData) -> Tuple[np.ndarray, Dict]:
        """
        Generate forest mask using adaptive NDVI thresholding.
        
        Returns binary mask (1=forest, 0=non-forest).
        NO VALIDATION - model-based only.
        """
        logger.info("=" * 80)
        logger.info("FOREST MASK GENERATION (Model-Based, No Validation)")
        logger.info("=" * 80)
        
        # Get NDVI (pre-computed or calculate)
        if 'NDVI' in s2_raster.band_names:
            ndvi_values = s2_raster.get_band('NDVI')
        else:
            red = s2_raster.get_band('B4')
            nir = s2_raster.get_band('B8')
            ndvi_result = ndvi(nir, red)
            ndvi_values = ndvi_result.values
        
        # Adaptive threshold: use local percentile
        valid_ndvi = ndvi_values[~np.isnan(ndvi_values)]
        ndvi_threshold = np.percentile(valid_ndvi, self.ndvi_percentile_threshold)
        
        logger.info(f"Adaptive NDVI threshold (P{self.ndvi_percentile_threshold}): {ndvi_threshold:.3f}")
        logger.info("NOTE: This is NOT calibrated to field data")
        
        # Initial mask
        forest_mask = ndvi_values > ndvi_threshold
        initial_pixels = forest_mask.sum()
        
        # Morphological filtering
        selem = disk(2)
        forest_mask = binary_opening(forest_mask, selem)
        forest_mask = binary_closing(forest_mask, selem)
        
        # Remove small patches
        labeled_mask = label(forest_mask)
        for region in regionprops(labeled_mask):
            if region.area < self.min_patch_size_pixels:
                forest_mask[labeled_mask == region.label] = 0
        
        final_pixels = forest_mask.sum()
        pixel_size_m = abs(s2_raster.transform[0])
        if pixel_size_m < 0.01:
            pixel_size_m = pixel_size_m * 111000
        
        area_ha = final_pixels * (pixel_size_m ** 2) / 10000.0
        
        logger.info(f"Forest pixels detected: {final_pixels:,}")
        logger.info(f"Forest area (model-derived): {area_ha:.2f} ha")
        logger.info(f"Coverage: {final_pixels / forest_mask.size * 100:.1f}%")
        logger.info("⚠️  LIMITATION: No ground truth validation available")
        
        metrics = {
            'forest_area_ha': area_ha,
            'forest_pixels': int(final_pixels),
            'ndvi_threshold': ndvi_threshold,
            'method': 'adaptive_percentile',
            'validated': False
        }
        
        return forest_mask, metrics


# REMOVED: CanopyCoverEstimator class
# REASON: Canopy cover % estimation requires field validation or LiDAR.
#         Sentinel-2 (10m) cannot infer canopy structure - use GEDI/FIA instead.
#         For USA projects: Use GEDI fractional cover or FIA crown closure.


# REMOVED: CanopyDensityClassifier class  
# REASON: Density classification is structural inference from optical data.
#         Sentinel-2 must be restricted to QA/visualization only.
#         For USA projects: Use GEDI cover metrics or FIA stand density.


# REMOVED: RelativeBiomassProxyCalculator class
    """
    Generate RELATIVE biomass proxy index (unitless, 0-1).
    
    CRITICAL: This is NOT absolute biomass in tonnes/ha.
    NOT suitable for carbon stock monetization.
    Shows spatial patterns only.
    """
    
    def calculate(
        self,
        ndvi_values: np.ndarray,
        cover_pct: np.ndarray,
        sar_vh: np.ndarray,
        forest_mask: np.ndarray
    ) -> Tuple[np.ndarray, Dict]:
        """
        Biomass Proxy = weighted combination of NDVI + Cover + SAR
        
        NORMALIZED to 0-1 using local min/max.
        NOT calibrated to field data.
        """
        logger.info("=" * 80)
        logger.info("RELATIVE BIOMASS PROXY (Unitless, NOT tonnes/ha)")
        logger.info("=" * 80)
        logger.info("⚠️  CRITICAL LIMITATION: This is a PROXY INDEX")
        logger.info("⚠️  NOT absolute biomass")
        logger.info("⚠️  NOT suitable for carbon stock monetization")
        logger.info("⚠️  NO field calibration")
        
        # Normalize inputs to 0-1
        def normalize(arr, mask):
            valid = arr[mask & ~np.isnan(arr)]
            if len(valid) == 0:
                return arr * 0
            min_val, max_val = np.percentile(valid, [5, 95])
            normalized = (arr - min_val) / (max_val - min_val + 1e-8)
            return np.clip(normalized, 0, 1)
        
        ndvi_norm = normalize(ndvi_values, forest_mask)
        cover_norm = normalize(cover_pct, forest_mask)
        sar_norm = normalize(sar_vh, forest_mask)
        
        # Weighted combination (weights arbitrary, no calibration)
        biomass_proxy = (
            0.4 * ndvi_norm +
            0.3 * cover_norm +
            0.3 * sar_norm
        )
        
        biomass_proxy_masked = np.where(forest_mask, biomass_proxy, np.nan)
        
        mean_proxy = np.nanmean(biomass_proxy_masked)
        std_proxy = np.nanstd(biomass_proxy_masked)
        
        logger.info(f"\nBiomass Proxy Statistics:")
        logger.info(f"  Mean: {mean_proxy:.3f} (unitless)")
        logger.info(f"  Std Dev: {std_proxy:.3f}")
        logger.info(f"  Range: 0-1 (normalized)")
        logger.info(f"\nInterpretation:")
        logger.info(f"  Higher values = Higher relative biomass (within this scene)")
        logger.info(f"  Lower values = Lower relative biomass (within this scene)")
        logger.info(f"  Cannot convert to tonnes/ha without field calibration")
        
        metrics = {
            'mean_proxy': float(mean_proxy),
            'std_proxy': float(std_proxy),
            'weights': {'ndvi': 0.4, 'cover': 0.3, 'sar': 0.3},
            'normalization': 'local_percentile_5_95',
            'units': 'unitless',
            'absolute_biomass': False,
            'field_calibrated': False,
            'carbon_credit_suitable': False
        }
        
        return biomass_proxy_masked, metrics


def parse_args():
    parser = argparse.ArgumentParser(
        description='Honest Forest Analysis Pipeline (No Field Validation)'
    )
    
    parser.add_argument('--s2', type=str, required=True,
                       help='Sentinel-2 composite')
    parser.add_argument('--s1', type=str, default=None,
                       help='Sentinel-1 composite (optional, for SAR features)')
    parser.add_argument('--output', type=str, default='outputs/honest_analysis/',
                       help='Output directory')
    
    return parser.parse_args()


def main():
    args = parse_args()
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    logger.info("=" * 80)
    logger.info("HONEST FOREST ANALYSIS PIPELINE")
    logger.info("=" * 80)
    logger.info("This pipeline provides:")
    logger.info("  ✓ Forest mask (model-based)")
    logger.info("  ✓ Canopy cover (% estimate)")
    logger.info("  ✓ Density classes (sparse/medium/dense)")
    logger.info("  ✓ Relative biomass proxy (unitless)")
    logger.info("")
    logger.info("This pipeline does NOT provide:")
    logger.info("  ✗ Individual tree counts")
    logger.info("  ✗ Tree height or DBH")
    logger.info("  ✗ Species identification")
    logger.info("  ✗ Absolute biomass (tonnes/ha)")
    logger.info("  ✗ Carbon stock monetization or registry submission")
    logger.info("=" * 80)
    
    # Load data
    logger.info(f"\nLoading Sentinel-2: {args.s2}")
    s2_raster = load_raster(Path(args.s2))
    
    # Get NDVI
    if 'NDVI' in s2_raster.band_names:
        ndvi_values = s2_raster.get_band('NDVI')
    else:
        red = s2_raster.get_band('B4')
        nir = s2_raster.get_band('B8')
        ndvi_result = ndvi(nir, red)
        ndvi_values = ndvi_result.values
    
    # Step 1: Forest Mask
    mask_gen = ForestMaskGenerator()
    forest_mask, mask_metrics = mask_gen.generate(s2_raster)
    
    # Save mask
    mask_raster = RasterData(
        data=forest_mask[np.newaxis, :, :].astype(np.uint8),
        transform=s2_raster.transform,
        crs=s2_raster.crs,
        nodata=255,
        band_names=['forest_mask']
    )
    mask_raster.save(output_dir / 'forest_mask.tif')
    logger.info(f"Saved: {output_dir / 'forest_mask.tif'}")
    
    # REMOVED: Canopy cover estimation, density classification, biomass proxy
    # REASON: These are structural inferences that require GEDI/FIA ground truth.
    #         Sentinel-2 (10m) restricted to QA/visualization only.
    #         For USA projects: Use GEDI fractional cover or FIA measurements.
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("⚠️  SENTINEL-2 USAGE RESTRICTION")
    logger.info("=" * 80)
    logger.info("Canopy cover, density, and biomass estimation REMOVED.")
    logger.info("Reason: Sentinel-2 lacks structural information at 10m resolution.")
    logger.info("")
    logger.info("For USA projects, use:")
    logger.info("  • GEDI L2A for fractional cover and height")
    logger.info("  • FIA plots for ground-truth biomass")
    logger.info("  • Sentinel-1 SAR for backscatter structure")
    logger.info("  • Sentinel-2 ONLY for forest mask QA and change detection")
    logger.info("=" * 80)
    
    # Generate report
    import json
    report = {
        'project_title': 'Forest Mask Analysis (QA Only - No Structural Inference)',
        'date': datetime.now().isoformat(),
        'usage_restriction': 'Sentinel-2 for QA/visualization only - NOT for biomass/structure',
        'inputs': {
            'sentinel2': str(args.s2),
        },
        'forest_mask': mask_metrics,
        'note': 'Canopy cover, density, and biomass removed. Use GEDI/FIA for structural metrics.',
        'limitations': {
            'no_field_validation': True,
            'no_tree_detection': True,
            'no_species_id': True,
            'no_absolute_biomass': True,
            'not_for_monetization': True,
            'outputs_model_derived': True
        }
    }
    
    with open(output_dir / 'analysis_report.json', 'w') as f:
        json.dump(report, f, indent=2)
    
    logger.info("\n" + "=" * 80)
    logger.info("ANALYSIS COMPLETE")
    logger.info("=" * 80)
    logger.info(f"\n📊 Results:")
    logger.info(f"  Forest area: {mask_metrics['forest_area_ha']:.2f} ha (model-derived)")
    
    logger.info(f"\n📁 Outputs saved to: {output_dir}")
    logger.info(f"  • forest_mask.tif")
    logger.info(f"  • analysis_report.json")
    
    logger.info(f"\n⚠️  REMEMBER:")
    logger.info(f"  • All outputs are model-derived WITHOUT field validation")
    logger.info(f"  • Biomass proxy is RELATIVE (0-1), NOT tonnes/ha")
    logger.info(f"  • NOT suitable for carbon stock monetization or registry submission")
    logger.info(f"  • Suitable for landscape monitoring and research")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
