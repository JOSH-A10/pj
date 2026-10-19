"""
Run carbon stock estimation pipeline for California NoCAL Wildfires B4 2018.

This script:
1. Automatically resolves AOI from USGS project metadata
2. Runs complete carbon estimation pipeline
3. Produces validated carbon stock outputs
"""

import sys
from pathlib import Path
import logging

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from usgs_3dep_lidar_fallback import resolve_aoi_from_project
from run_pipeline import run_complete_pipeline

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """Run pipeline for California NoCAL Wildfires B4 2018."""
    
    # Configuration
    project_name = "USGS_LPC_CA_NoCAL_Wildfires_B4_2018"
    year = 2018  # LiDAR acquisition year
    output_dir = Path("outputs")
    aoi_path = Path("data/raw/aoi_california_nocal_wildfires_b4_2018.geojson")
    
    logger.info("=" * 80)
    logger.info("CALIFORNIA NOCAL WILDFIRES B4 2018 - CARBON STOCK ESTIMATION")
    logger.info("=" * 80)
    logger.info(f"Project: {project_name}")
    logger.info(f"Year: {year}")
    logger.info("=" * 80)
    
    # Step 1: Resolve AOI automatically
    logger.info("\n[STEP 1] Automatic AOI Resolution")
    logger.info("=" * 80)
    
    if not aoi_path.exists():
        logger.info(f"AOI file not found - attempting automatic resolution...")
        aoi = resolve_aoi_from_project(project_name, aoi_path)
        
        if aoi is None:
            logger.error("Failed to resolve AOI automatically")
            logger.error("Please provide AOI coordinates or GeoJSON file")
            return 1
    else:
        logger.info(f"Using existing AOI: {aoi_path}")
    
    # Step 2: Run complete pipeline
    logger.info("\n[STEP 2] Running Complete Carbon Stock Pipeline")
    logger.info("=" * 80)
    
    try:
        result = run_complete_pipeline(
            aoi_path=aoi_path,
            year=year,
            output_dir=output_dir
        )
        
        # Print summary
        logger.info("\n" + "=" * 80)
        logger.info("PIPELINE COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Status: {result.get('status', 'unknown').upper()}")
        
        if result.get('status') == 'success':
            logger.info(f"Biomass source: {result.get('biomass_source', 'unknown')}")
            logger.info(f"Total carbon: {result.get('total_carbon_Mg', 0):.2f} Mg C")
            logger.info(f"Total CO2e: {result.get('total_CO2e_Mg', 0):.2f} Mg CO2e")
            logger.info(f"Uncertainty: ±{result.get('uncertainty_percent', 0):.1f}%")
            logger.info(f"Results: {result.get('output_directory', 'N/A')}/results/carbon_stock_summary.json")
        else:
            logger.info(f"Reason: {result.get('message', 'Unknown error')}")
            logger.info(f"Suggestion: {result.get('suggestion', 'Check logs for details')}")
        
        logger.info("=" * 80)
        
        return 0 if result.get('status') == 'success' else 1
        
    except Exception as e:
        logger.error(f"Pipeline failed with exception: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
