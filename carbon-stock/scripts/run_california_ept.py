"""
Run pipeline for California NoCAL Wildfires using EPT (Entwine Point Tiles)

This script processes LiDAR directly from USGS S3 EPT servers using PDAL readers.ept,
bypassing the need to download individual LAZ files.
"""

import sys
from pathlib import Path
import logging
import json
import subprocess
import numpy as np
import rasterio
from rasterio.transform import from_bounds
import geopandas as gpd

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

logger = logging.getLogger(__name__)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# FIA allometry constants
FIA_A = 0.168
FIA_B = 2.47

# IPCC carbon factors
CARBON_FRACTION = 0.47
CO2_TO_C = 3.67

# Raster resolution (single configurable variable)
RASTER_RES_M = 5.0

# Safety guard: prevent accidental very-high-resolution runs
if RASTER_RES_M < 2.0:
    logging.warning(f"Configured raster resolution {RASTER_RES_M}m is below safe threshold; clamping to 5.0m")
    RASTER_RES_M = 5.0


def process_lidar_ept(ept_url: str, aoi: gpd.GeoDataFrame, output_dir: Path) -> tuple:
    """
    Process LiDAR from EPT using PDAL readers.ept.
    
    Args:
        ept_url: URL to EPT endpoint (e.g., s3://usgs-lidar-public/project/ept.json)
        aoi: Area of interest GeoDataFrame (EPSG:4326)
        output_dir: Output directory for rasters
        
    Returns:
        (chm_path, acquisition_year) or (None, None) on failure
    """
    logger.info(f"[EPT] Processing from: {ept_url}")
    
    # Get AOI bounds in WGS84 (EPSG:4326)
    bounds_wgs84 = aoi.total_bounds  # minx, miny, maxx, maxy
    
    logger.info(f"[EPT] AOI bounds (WGS84): {bounds_wgs84}")
    
    # Calculate UTM zone from AOI centroid for output CRS
    centroid_lon = (bounds_wgs84[0] + bounds_wgs84[2]) / 2
    centroid_lat = (bounds_wgs84[1] + bounds_wgs84[3]) / 2
    utm_zone = int((centroid_lon + 180) / 6) + 1
    hemisphere = 'north' if centroid_lat >= 0 else 'south'
    utm_epsg = 32600 + utm_zone if hemisphere == 'north' else 32700 + utm_zone
    
    logger.info(f"[EPT] Output CRS: EPSG:{utm_epsg} (Zone {utm_zone}{hemisphere[0].upper()})")
    
    # Create output paths (include resolution in filename)
    dtm_path = output_dir / f'dtm_{int(RASTER_RES_M)}m.tif'
    dsm_path = output_dir / f'dsm_{int(RASTER_RES_M)}m.tif'
    chm_path = output_dir / f'chm_{int(RASTER_RES_M)}m.tif'
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # PDAL readers.ept accepts bounds in format: "([xmin, xmax], [ymin, ymax])"
    # EPT reader will handle reprojection from native CRS
    bounds_str = f"([{bounds_wgs84[0]}, {bounds_wgs84[2]}], [{bounds_wgs84[1]}, {bounds_wgs84[3]}])"
    
    logger.info(f"[EPT] Bounds string: {bounds_str}")
    
    # PDAL pipeline for DTM (ground points only - Class 2)
    dtm_pipeline = {
        "pipeline": [
            {
                "type": "readers.ept",
                "filename": ept_url
                # EPT reader will automatically handle bounds and CRS conversion
            },
            {
                "type": "filters.crop",
                "bounds": bounds_str  # Crop in WGS84
            },
            {
                "type": "filters.reprojection",
                "in_srs": "EPSG:4326",  # Assume WGS84 bounds
                "out_srs": f"EPSG:{utm_epsg}"
            },
            {
                "type": "filters.range",
                "limits": "Classification[2:2]"  # Ground only
            },
            {
                "type": "writers.gdal",
                "filename": str(dtm_path),
                "resolution": RASTER_RES_M,
                "output_type": "min",
                "gdalopts": "COMPRESS=DEFLATE,TILED=YES"
            }
        ]
    }
    
    # PDAL pipeline for DSM (exclude ground, noise, water - Classes 2,7,18)
    dsm_pipeline = {
        "pipeline": [
            {
                "type": "readers.ept",
                "filename": ept_url
            },
            {
                "type": "filters.crop",
                "bounds": bounds_str
            },
            {
                "type": "filters.reprojection",
                "in_srs": "EPSG:4326",
                "out_srs": f"EPSG:{utm_epsg}"
            },
            {
                "type": "filters.range",
                "limits": "Classification![2:2],Classification![7:7],Classification![18:18]"
            },
            {
                "type": "writers.gdal",
                "filename": str(dsm_path),
                "resolution": RASTER_RES_M,
                "output_type": "max",
                "gdalopts": "COMPRESS=DEFLATE,TILED=YES"
            }
        ]
    }
    
    # Run DTM generation
    logger.info("[EPT] Generating DTM (ground points)...")
    dtm_json = json.dumps(dtm_pipeline)
    result = subprocess.run(
        ['pdal', 'pipeline', '--stdin'],
        input=dtm_json,
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        logger.error(f"[EPT] DTM generation failed: {result.stderr}")
        return None, None
    
    logger.info(f"[EPT] DTM saved: {dtm_path}")
    
    # Run DSM generation
    logger.info("[EPT] Generating DSM (surface points)...")
    dsm_json = json.dumps(dsm_pipeline)
    result = subprocess.run(
        ['pdal', 'pipeline', '--stdin'],
        input=dsm_json,
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        logger.error(f"[EPT] DSM generation failed: {result.stderr}")
        return None, None
    
    logger.info(f"[EPT] DSM saved: {dsm_path}")
    
    # Compute CHM = DSM - DTM
    logger.info("[EPT] Computing CHM...")
    with rasterio.open(dsm_path) as dsm_src, rasterio.open(dtm_path) as dtm_src:
        dsm = dsm_src.read(1)
        dtm = dtm_src.read(1)
        dsm_nodata = dsm_src.nodata
        dtm_nodata = dtm_src.nodata
        
        # Mask nodata
        dsm_valid = (dsm != dsm_nodata) if dsm_nodata is not None else np.ones_like(dsm, dtype=bool)
        dtm_valid = (dtm != dtm_nodata) if dtm_nodata is not None else np.ones_like(dtm, dtype=bool)
        both_valid = dsm_valid & dtm_valid
        
        # Compute CHM
        chm = np.zeros_like(dsm, dtype=np.float32)
        chm[both_valid] = dsm[both_valid] - dtm[both_valid]
        chm[~both_valid] = -9999
        
        # Clip negative values
        chm[chm < 0] = 0
        
        # Validation
        valid_chm = chm[chm > 0]
        if len(valid_chm) == 0:
            logger.error("[EPT] CHM is all zeros - data quality issue")
            return None, None
        
        chm_max = valid_chm.max()
        chm_mean = valid_chm.mean()
        
        logger.info(f"[EPT] CHM stats: max={chm_max:.1f}m, mean={chm_mean:.1f}m, valid_pixels={len(valid_chm)}")
        
        if chm_max < 2.0:
            logger.error(f"[EPT] CHM max height < 2m - rejecting as unrealistic")
            return None, None
        
        # Save CHM
        profile = dsm_src.profile.copy()
        profile.update(dtype=rasterio.float32, nodata=-9999, compress='deflate')
        
        with rasterio.open(chm_path, 'w', **profile) as chm_dst:
            chm_dst.write(chm, 1)
        
        logger.info(f"[EPT] CHM saved: {chm_path}")
    
    # EPT doesn't have explicit acquisition year in URL, use 2018 from project name
    return chm_path, 2018


def main():
    """Run California NoCAL Wildfires EPT pipeline"""
    
    # Setup
    logger.info("=" * 80)
    logger.info("CALIFORNIA NOCAL WILDFIRES - EPT PROCESSING")
    logger.info("=" * 80)
    
    # Paths
    aoi_path = Path('data/raw/aoi_california_nocal_wildfires_b4_2018.geojson')
    output_dir = Path('outputs/california_ept')
    lidar_dir = output_dir / 'lidar'
    
    # Check AOI exists
    if not aoi_path.exists():
        logger.error(f"AOI not found: {aoi_path}")
        return 1
    
    # Load AOI
    aoi = gpd.read_file(aoi_path)
    logger.info(f"AOI loaded: {aoi_path}")
    logger.info(f"AOI bounds: {aoi.total_bounds}")
    
    # EPT URL (try QL2 first as it's higher quality)
    ept_url = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/CA_NoCAL_Wildfires_TL_QL2_2018/ept.json"
    
    # Process LiDAR
    chm_path, acquisition_year = process_lidar_ept(ept_url, aoi, lidar_dir)
    
    if chm_path is None:
        logger.error("EPT processing failed")
        return 1
    
    logger.info(f"✓ CHM generated: {chm_path}")
    logger.info(f"✓ Acquisition year: {acquisition_year}")
    
    # Calculate biomass from CHM
    logger.info("=" * 80)
    logger.info("BIOMASS CALCULATION")
    logger.info("=" * 80)
    
    with rasterio.open(chm_path) as chm_src:
        chm = chm_src.read(1)
        chm_profile = chm_src.profile
        
        # Apply FIA allometry: AGB = 0.168 * H^2.47
        agb = np.zeros_like(chm, dtype=np.float32)
        valid_mask = (chm > 0) & (chm != -9999)
        agb[valid_mask] = FIA_A * (chm[valid_mask] ** FIA_B)
        agb[~valid_mask] = -9999
        
        # Save AGB raster
        agb_path = output_dir / 'agb_30m.tif'
        with rasterio.open(agb_path, 'w', **chm_profile) as agb_dst:
            agb_dst.write(agb, 1)
        
        logger.info(f"AGB saved: {agb_path}")
        
        # Calculate carbon stock
        pixel_area_ha = (chm_profile['transform'][0] * chm_profile['transform'][4]) / 10000  # m² to ha
        
        total_agb_mg = (agb[agb > 0].sum() * pixel_area_ha)
        total_carbon_mg = total_agb_mg * CARBON_FRACTION
        total_co2e_mg = total_carbon_mg * CO2_TO_C
        
        forest_area_ha = (agb > 0).sum() * pixel_area_ha
        
        logger.info("=" * 80)
        logger.info("CARBON STOCK RESULTS")
        logger.info("=" * 80)
        logger.info(f"Forest area: {forest_area_ha:.2f} ha")
        logger.info(f"Total AGB: {total_agb_mg:.2f} Mg")
        logger.info(f"Total Carbon: {total_carbon_mg:.2f} Mg C")
        logger.info(f"Total CO2e: {total_co2e_mg:.2f} Mg CO2e")
        logger.info(f"Mean AGB: {total_agb_mg/forest_area_ha:.2f} Mg/ha")
        logger.info("=" * 80)
        
        # Save results
        results = {
            'status': 'SUCCESS',
            'source': 'USGS_3DEP_EPT',
            'project': 'CA_NoCAL_Wildfires_TL_QL2_2018',
            'acquisition_year': acquisition_year,
            'forest_area_ha': float(forest_area_ha),
            'total_agb_mg': float(total_agb_mg),
            'total_carbon_mg': float(total_carbon_mg),
            'total_co2e_mg': float(total_co2e_mg),
            'mean_agb_mg_ha': float(total_agb_mg/forest_area_ha),
            'chm_path': str(chm_path),
            'agb_path': str(agb_path)
        }
        
        results_path = output_dir / 'results.json'
        with open(results_path, 'w') as f:
            json.dump(results, f, indent=2)
        
        logger.info(f"Results saved: {results_path}")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
