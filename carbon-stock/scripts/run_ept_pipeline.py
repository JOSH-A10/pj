"""
Run LiDAR pipeline using EPT (Entwine Point Tiles) from USGS S3

This processes LiDAR directly from the cloud without downloading LAZ files.
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
from shapely.geometry import box
import threading
import time
from datetime import timedelta

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


def show_progress(process, description: str, stop_event: threading.Event):
    """Display progress with elapsed time while process runs"""
    start_time = time.time()
    spinner_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    i = 0
    
    while not stop_event.is_set():
        elapsed = time.time() - start_time
        elapsed_str = str(timedelta(seconds=int(elapsed)))
        
        # Show spinner with elapsed time
        spinner = spinner_chars[i % len(spinner_chars)]
        print(f"\r{spinner} {description} [{elapsed_str}]", end='', flush=True)
        
        i += 1
        time.sleep(0.1)
    
    # Clear the line
    elapsed = time.time() - start_time
    elapsed_str = str(timedelta(seconds=int(elapsed)))
    print(f"\r✓ {description} [{elapsed_str}]" + ' ' * 20)


def create_aoi_from_tif(tif_path: Path) -> gpd.GeoDataFrame:
    """Create GeoDataFrame from raster bounds"""
    with rasterio.open(tif_path) as src:
        bounds = src.bounds
        crs = src.crs
        
    # Create box geometry
    geom = box(bounds.left, bounds.bottom, bounds.right, bounds.top)
    gdf = gpd.GeoDataFrame({'geometry': [geom]}, crs=crs)
    
    # Reproject to WGS84 for EPT query
    gdf = gdf.to_crs('EPSG:4326')
    
    return gdf


def process_lidar_ept(ept_url: str, aoi: gpd.GeoDataFrame, output_dir: Path) -> tuple:
    """
    Process LiDAR from EPT using PDAL readers.ept.
    
    Args:
        ept_url: URL to EPT endpoint
        aoi: Area of interest GeoDataFrame (EPSG:4326)
        output_dir: Output directory for rasters
        
    Returns:
        (chm_path, acquisition_year) or (None, None) on failure
    """
    logger.info(f"[EPT] Processing from: {ept_url}")
    logger.info(f"[EPT] Mode: Cloud-native EPT streaming (no LAZ downloads)")
    
    # Get AOI bounds in WGS84 (EPSG:4326)
    bounds_wgs84 = aoi.total_bounds  # minx, miny, maxx, maxy
    
    logger.info(f"[EPT] AOI bounds (WGS84): {bounds_wgs84}")
    
    # Calculate UTM zone from AOI centroid - this is the OUTPUT CRS
    centroid_lon = (bounds_wgs84[0] + bounds_wgs84[2]) / 2
    centroid_lat = (bounds_wgs84[1] + bounds_wgs84[3]) / 2
    utm_zone = int((centroid_lon + 180) / 6) + 1
    hemisphere = 'north' if centroid_lat >= 0 else 'south'
    utm_epsg = 32600 + utm_zone if hemisphere == 'north' else 32700 + utm_zone
    
    logger.info(f"[EPT] Output CRS: EPSG:{utm_epsg} (Zone {utm_zone}{hemisphere[0].upper()})")
    
    # CRITICAL: EPT dataset is in EPSG:3857 (Web Mercator)
    # Transform AOI bounds to Web Mercator for EPT octree filtering
    import pyproj
    transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    minx_merc, miny_merc = transformer.transform(bounds_wgs84[0], bounds_wgs84[1])
    maxx_merc, maxy_merc = transformer.transform(bounds_wgs84[2], bounds_wgs84[3])
    bounds_mercator = (minx_merc, miny_merc, maxx_merc, maxy_merc)
    
    logger.info(f"[EPT] AOI bounds (Web Mercator EPSG:3857): {bounds_mercator}")
    logger.info(f"[EPT] CRS-aligned bounds (EPSG:3857 = dataset CRS) enable true EPT octree filtering")
    
    # Fail-fast validation: Ensure we're using Web Mercator for EPT bounds
    assert abs(bounds_mercator[0]) < 20037509, \
        f"Invalid Web Mercator bounds: {bounds_mercator}. Must be in EPSG:3857."
    
    # Create output paths (include resolution in filename)
    dtm_path = output_dir / f'dtm_{int(RASTER_RES_M)}m.tif'
    dsm_path = output_dir / f'dsm_{int(RASTER_RES_M)}m.tif'
    chm_path = output_dir / f'chm_{int(RASTER_RES_M)}m.tif'
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # CRITICAL: Bounds for readers.ept MUST be in dataset CRS (EPSG:3857) for octree filtering
    # Format: "([xmin, xmax], [ymin, ymax])"
    bounds_str_mercator = f"([{bounds_mercator[0]}, {bounds_mercator[2]}], [{bounds_mercator[1]}, {bounds_mercator[3]}])"
    
    logger.info(f"[EPT] Bounds string for readers.ept (EPSG:3857): {bounds_str_mercator}")
    logger.info(f"[EPT] Dataset CRS bounds = efficient EPT octree source filtering")
    
    # PDAL pipeline for DTM (ground points only - Class 2)
    # OPTIMIZED: Bounds in EPSG:3857 (dataset CRS) enable EPT octree source filtering
    dtm_pipeline = {
        "pipeline": [
            {
                "type": "readers.ept",
                "filename": ept_url,
                "bounds": bounds_str_mercator  # Web Mercator bounds for EPT octree filtering
            },
            {
                "type": "filters.range",
                "limits": "Classification[2:2]"  # Ground only
            },
            {
                "type": "filters.reprojection",
                "in_srs": "EPSG:3857",  # EPT data is in Web Mercator
                "out_srs": f"EPSG:{utm_epsg}"  # Reproject to UTM for output
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
    
    # PDAL pipeline for DSM (all vegetation points)
    # OPTIMIZED: Bounds in EPSG:3857 (dataset CRS) enable EPT octree source filtering
    dsm_pipeline = {
        "pipeline": [
            {
                "type": "readers.ept",
                "filename": ept_url,
                "bounds": bounds_str_mercator  # Web Mercator bounds for EPT octree filtering
            },
            {
                "type": "filters.range",
                "limits": "Classification![2:2],Classification![7:7],Classification![18:18]"  # Exclude ground/noise/water
            },
            {
                "type": "filters.reprojection",
                "in_srs": "EPSG:3857",  # EPT data is in Web Mercator
                "out_srs": f"EPSG:{utm_epsg}"  # Reproject to UTM for output
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
    logger.info(f"[EPT] Streaming at {int(RASTER_RES_M)}m resolution with CRS-aligned bounds (fast!)")
    logger.info("[EPT] NOTE: No LAZ files downloaded - pure cloud streaming with EPT octree filtering")
    pdal_exe = r"C:\Users\matt3\miniforge3\Library\bin\pdal.exe"
    dtm_json = json.dumps(dtm_pipeline)
    
    try:
        # Start progress display in separate thread
        stop_event = threading.Event()
        progress_thread = threading.Thread(
            target=show_progress,
            args=(None, "Processing DTM (ground points)", stop_event)
        )
        progress_thread.start()
        
        # Run PDAL pipeline
        result = subprocess.run(
            [pdal_exe, 'pipeline', '--stdin', '--verbose', '8'],
            input=dtm_json,
            capture_output=True,
            text=True,
            timeout=900  # 15 minute timeout
        )
        
        # Stop progress display
        stop_event.set()
        progress_thread.join()
        
        if result.returncode != 0:
            logger.error(f"[EPT] DTM generation failed: {result.stderr}")
            logger.error(f"[EPT] stdout: {result.stdout}")
            return None, None
        
        logger.info(f"[EPT] DTM saved: {dtm_path}")
        if result.stdout:
            logger.debug(f"[EPT] PDAL output: {result.stdout[-500:]}")
    except subprocess.TimeoutExpired:
        stop_event.set()
        progress_thread.join()
        logger.error("[EPT] DTM generation timed out after 5 minutes")
        logger.error("[EPT] This should be fast with CRS-aligned bounds - check EPT dataset availability")
        return None, None
    except Exception as e:
        stop_event.set()
        progress_thread.join()
        logger.error(f"[EPT] DTM generation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, None
    
    # Run DSM generation
    logger.info("[EPT] Generating DSM (surface points)...")
    logger.info(f"[EPT] Streaming at {int(RASTER_RES_M)}m resolution with CRS-aligned bounds (fast!)")
    dsm_json = json.dumps(dsm_pipeline)
    
    try:
        # Start progress display in separate thread
        stop_event = threading.Event()
        progress_thread = threading.Thread(
            target=show_progress,
            args=(None, "Processing DSM (surface points)", stop_event)
        )
        progress_thread.start()
        
        # Run PDAL pipeline
        result = subprocess.run(
            [pdal_exe, 'pipeline', '--stdin', '--verbose', '8'],
            input=dsm_json,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout (fast with CRS-aligned bounds)
        )
        
        # Stop progress display
        stop_event.set()
        progress_thread.join()
        
        if result.returncode != 0:
            logger.error(f"[EPT] DSM generation failed: {result.stderr}")
            logger.error(f"[EPT] stdout: {result.stdout}")
            return None, None
        
        logger.info(f"[EPT] DSM saved: {dsm_path}")
        if result.stdout:
            logger.debug(f"[EPT] PDAL output: {result.stdout[-500:]}")
    except subprocess.TimeoutExpired:
        stop_event.set()
        progress_thread.join()
        logger.error("[EPT] DSM generation timed out after 5 minutes")
        logger.error("[EPT] This should be fast with CRS-aligned bounds - check EPT dataset availability")
        return None, None
    except Exception as e:
        stop_event.set()
        progress_thread.join()
        logger.error(f"[EPT] DSM generation failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, None
    
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
        chm_p95 = np.percentile(valid_chm, 95)
        
        logger.info(f"[EPT] CHM stats: max={chm_max:.1f}m, mean={chm_mean:.1f}m, p95={chm_p95:.1f}m, valid_pixels={len(valid_chm)}")
        
        if chm_max < 2.0:
            logger.error(f"[EPT] CHM max height < 2m - rejecting as unrealistic")
            return None, None
        
        # Save CHM
        profile = dsm_src.profile.copy()
        profile.update(dtype=rasterio.float32, nodata=-9999, compress='deflate')
        
        with rasterio.open(chm_path, 'w', **profile) as chm_dst:
            chm_dst.write(chm, 1)
        
        logger.info(f"[EPT] CHM saved: {chm_path}")
    
    # Extract year from EPT URL
    # CA_NoCAL_Wildfires_TL_QL2_2018 -> 2018
    import re
    year_match = re.search(r'_(\d{4})', ept_url)
    acquisition_year = int(year_match.group(1)) if year_match else 2018
    
    return chm_path, acquisition_year


def main():
    """Run EPT pipeline for current AOI"""
    
    # Setup
    logger.info("=" * 80)
    logger.info("CLOUD-NATIVE EPT LIDAR PROCESSING - TREE HEIGHT & AGB")
    logger.info("=" * 80)
    logger.info(f"Resolution: {int(RASTER_RES_M)}m (optimized for cloud streaming)")
    logger.info("Mode: EPT streaming (no LAZ tile downloads)")
    logger.info("=" * 80)
    logger.info("NOTE: Using Oregon McKenzie River 2021 dataset")
    logger.info("California NoCAL Wildfires EPT dataset is currently unavailable")
    logger.info("(timeout/no points even without bounds filtering)")
    logger.info("=" * 80)
    
    # Paths
    aoi_geojson_path = Path('data/raw/oregon_center_test.geojson')  # Dataset center test
    output_dir = Path('outputs/lidar_ept_oregon_center_test')
    
    # Check AOI exists
    if not aoi_geojson_path.exists():
        logger.error(f"AOI GeoJSON not found: {aoi_geojson_path}")
        return 1
    
    # Load AOI from GeoJSON
    logger.info(f"Loading AOI from: {aoi_geojson_path}")
    aoi = gpd.read_file(aoi_geojson_path)
    logger.info(f"AOI bounds (WGS84): {aoi.total_bounds}")
    
    # Calculate area
    aoi_utm = aoi.to_crs(aoi.estimate_utm_crs())
    area_ha = aoi_utm.geometry.area.sum() / 10000
    logger.info(f"AOI area: {area_ha:.2f} ha")
    
    # EPT URL - Oregon McKenzie River 2021 (confirmed working)
    ept_url = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/OR_McKenzieRiver_1_2021/ept.json"
    
    logger.info(f"[EPT] Streaming from AWS S3 (no tile downloads): {ept_url}")
    logger.info(f"[EPT] Resolution: {int(RASTER_RES_M)}m (optimized for cloud streaming)")
    
    logger.info("=" * 80)
    logger.info("PROCESSING LIDAR FROM EPT")
    logger.info("=" * 80)
    
    # Process LiDAR
    chm_path, acquisition_year = process_lidar_ept(ept_url, aoi, output_dir)
    
    if chm_path is None:
        logger.error("EPT processing failed")
        return 1
    
    logger.info(f"✓ CHM generated: {chm_path}")
    logger.info(f"✓ Acquisition year: {acquisition_year}")
    
    # Calculate biomass from CHM
    logger.info("=" * 80)
    logger.info("CALCULATING BIOMASS (FIA ALLOMETRY)")
    logger.info("=" * 80)
    
    with rasterio.open(chm_path) as chm_src:
        chm = chm_src.read(1)
        chm_profile = chm_src.profile
        
        # Apply FIA allometry: AGB = 0.168 * H^2.47
        agb = np.zeros_like(chm, dtype=np.float32)
        valid_mask = (chm > 0) & (chm != -9999)
        agb[valid_mask] = FIA_A * (chm[valid_mask] ** FIA_B)
        agb[~valid_mask] = -9999
        
        # Save AGB raster (include resolution)
        agb_path = output_dir / f'agb_{int(RASTER_RES_M)}m.tif'
        with rasterio.open(agb_path, 'w', **chm_profile) as agb_dst:
            agb_dst.write(agb, 1)
        
        logger.info(f"AGB saved: {agb_path}")
        
        # Calculate carbon stock
        pixel_area_ha = abs(chm_profile['transform'][0] * chm_profile['transform'][4]) / 10000  # m² to ha
        
        valid_agb = agb[agb > 0]
        total_agb_mg = valid_agb.sum() * pixel_area_ha
        total_carbon_mg = total_agb_mg * CARBON_FRACTION
        total_co2e_mg = total_carbon_mg * CO2_TO_C
        
        forest_area_ha = len(valid_agb) * pixel_area_ha
        mean_agb_mg_ha = total_agb_mg / forest_area_ha if forest_area_ha > 0 else 0
        mean_chm_m = chm[valid_mask].mean()
        
        logger.info("=" * 80)
        logger.info("CARBON STOCK RESULTS")
        logger.info("=" * 80)
        logger.info(f"AOI area: {area_ha:.2f} ha")
        logger.info(f"Forest area (LiDAR): {forest_area_ha:.2f} ha")
        logger.info(f"Mean tree height: {mean_chm_m:.2f} m")
        logger.info(f"Mean AGB: {mean_agb_mg_ha:.2f} Mg/ha")
        logger.info(f"Total AGB: {total_agb_mg:.2f} Mg")
        logger.info(f"Total Carbon: {total_carbon_mg:.2f} Mg C")
        logger.info(f"Total CO2e: {total_co2e_mg:.2f} Mg CO2e")
        logger.info("=" * 80)
        
        # Save results
        results = {
            'status': 'SUCCESS',
            'source': 'USGS_3DEP_EPT_Cloud_Native',
            'project': 'OR_McKenzieRiver_1_2021',
            'acquisition_year': acquisition_year,
            'resolution_m': int(RASTER_RES_M),
            'aoi_area_ha': float(area_ha),
            'forest_area_ha': float(forest_area_ha),
            'mean_tree_height_m': float(mean_chm_m),
            'mean_agb_mg_ha': float(mean_agb_mg_ha),
            'total_agb_mg': float(total_agb_mg),
            'total_carbon_mg': float(total_carbon_mg),
            'total_co2e_mg': float(total_co2e_mg),
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
