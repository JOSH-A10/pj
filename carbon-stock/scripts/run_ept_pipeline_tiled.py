"""
Run LiDAR pipeline using EPT with spatial tiling and parallel processing

Improvements over original:
- Spatial tiling to reduce memory and avoid timeouts
- Parallel tile processing with ProcessPoolExecutor
- Tile-level caching (skip existing DTM/DSM)
- Configurable resolution (default 2m instead of 1m)
- GDAL VRT merging for efficient tile assembly
- Robust error handling (tile failures don't crash pipeline)
"""

import sys
from pathlib import Path
import logging
import json
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
import geopandas as gpd
from shapely.geometry import box
import math
import requests
import pyproj
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

# FIA allometry constants (unchanged)
FIA_A = 0.168
FIA_B = 2.47

# IPCC carbon factors (unchanged)
CARBON_FRACTION = 0.47
CO2_TO_C = 3.67

# Raster resolution (single configurable variable)
RASTER_RES_M = 5.0

# Safety guard: prevent accidental very-high-resolution runs
if RASTER_RES_M < 2.0:
    logging.warning(f"Configured raster resolution {RASTER_RES_M}m is below safe threshold; clamping to 5.0m")
    RASTER_RES_M = 5.0

# Configuration
@dataclass
class PipelineConfig:
    """Pipeline configuration parameters"""
    tile_size_deg: float = 0.01  # ~1 km at mid-latitudes
    resolution_m: float = RASTER_RES_M     # Output raster resolution
    max_workers: int = 4          # Parallel tile processing
    timeout_per_tile: int = 600   # 10 min timeout per tile
    pdal_exe: str = r"C:\Users\matt3\miniforge3\Library\bin\pdal.exe"
    max_data_size_gb: float = 5.0  # Abort if estimated data > 5 GB
    bytes_per_point: int = 30      # Average EPT point size


def estimate_data_volume(ept_url: str, 
                        aoi_bounds: Tuple[float, float, float, float],
                        utm_epsg: int) -> Dict:
    """
    Pre-flight estimation of LiDAR data volume without downloading points.
    
    Fetches EPT metadata to estimate:
    - Number of points intersecting AOI
    - Data size in MB/GB
    - Size per tile
    
    Args:
        ept_url: EPT endpoint URL
        aoi_bounds: (minx, miny, maxx, maxy) in EPSG:4326
        utm_epsg: Target UTM EPSG code for area calculations
        
    Returns:
        Dictionary with estimation results
    """
    logger.info("=" * 80)
    logger.info("PRE-FLIGHT DATA VOLUME ESTIMATION")
    logger.info("=" * 80)
    
    try:
        # Fetch EPT metadata
        logger.info(f"[ESTIMATE] Fetching EPT metadata: {ept_url}")
        response = requests.get(ept_url, timeout=30)
        response.raise_for_status()
        ept_metadata = response.json()
        
        # Extract metadata
        total_points = ept_metadata.get('points', 0)
        dataset_bounds = ept_metadata.get('bounds', [])
        span = ept_metadata.get('span', 0)
        
        if not total_points or not dataset_bounds:
            logger.warning("[ESTIMATE] Insufficient EPT metadata - skipping estimation")
            return {'estimated': False}
        
        logger.info(f"[ESTIMATE] EPT total points: {total_points:,}")
        logger.info(f"[ESTIMATE] EPT bounds: {dataset_bounds}")
        logger.info(f"[ESTIMATE] EPT span: {span}")
        
        # Dataset bounds are typically [minx, miny, minz, maxx, maxy, maxz] in native CRS
        # Try to determine if bounds are in projected or geographic coordinates
        ds_minx, ds_miny = dataset_bounds[0], dataset_bounds[1]
        ds_maxx, ds_maxy = dataset_bounds[3], dataset_bounds[4]
        
        # Detect CRS (heuristic: if values > 180, assume projected)
        if abs(ds_minx) > 180 or abs(ds_maxx) > 180:
            # Bounds likely in projected CRS - convert to WGS84 for comparison
            # Assume Web Mercator (EPSG:3857) for USGS data
            transformer = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
            ds_minx_wgs, ds_miny_wgs = transformer.transform(ds_minx, ds_miny)
            ds_maxx_wgs, ds_maxy_wgs = transformer.transform(ds_maxx, ds_maxy)
        else:
            # Already in geographic coordinates
            ds_minx_wgs, ds_miny_wgs = ds_minx, ds_miny
            ds_maxx_wgs, ds_maxy_wgs = ds_maxx, ds_maxy
        
        # Calculate areas in UTM for accurate metric area
        transformer_to_utm = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
        
        # Dataset area
        ds_utm_minx, ds_utm_miny = transformer_to_utm.transform(ds_minx_wgs, ds_miny_wgs)
        ds_utm_maxx, ds_utm_maxy = transformer_to_utm.transform(ds_maxx_wgs, ds_maxy_wgs)
        dataset_area_m2 = (ds_utm_maxx - ds_utm_minx) * (ds_utm_maxy - ds_utm_miny)
        
        # AOI area
        aoi_minx, aoi_miny, aoi_maxx, aoi_maxy = aoi_bounds
        aoi_utm_minx, aoi_utm_miny = transformer_to_utm.transform(aoi_minx, aoi_miny)
        aoi_utm_maxx, aoi_utm_maxy = transformer_to_utm.transform(aoi_maxx, aoi_maxy)
        aoi_area_m2 = (aoi_utm_maxx - aoi_utm_minx) * (aoi_utm_maxy - aoi_utm_miny)
        
        # Estimate points in AOI using area proportion
        area_ratio = aoi_area_m2 / dataset_area_m2 if dataset_area_m2 > 0 else 0
        estimated_points = int(total_points * area_ratio)
        
        # Estimate data size
        bytes_per_point = 30  # Typical EPT point size (XYZ + attributes)
        estimated_bytes = estimated_points * bytes_per_point
        estimated_mb = estimated_bytes / (1024 * 1024)
        estimated_gb = estimated_mb / 1024
        
        logger.info("=" * 80)
        logger.info("ESTIMATION RESULTS")
        logger.info("=" * 80)
        logger.info(f"Dataset area: {dataset_area_m2/1e6:.2f} km²")
        logger.info(f"AOI area: {aoi_area_m2/1e6:.2f} km²")
        logger.info(f"Area ratio: {area_ratio:.4f} ({area_ratio*100:.2f}%)")
        logger.info(f"Estimated points in AOI: {estimated_points:,}")
        logger.info(f"Estimated data size: {estimated_mb:.2f} MB ({estimated_gb:.3f} GB)")
        logger.info(f"Point density: ~{estimated_points/aoi_area_m2:.1f} pts/m²")
        logger.info("=" * 80)
        
        return {
            'estimated': True,
            'total_points_dataset': total_points,
            'estimated_points_aoi': estimated_points,
            'estimated_mb': estimated_mb,
            'estimated_gb': estimated_gb,
            'area_ratio': area_ratio,
            'dataset_area_m2': dataset_area_m2,
            'aoi_area_m2': aoi_area_m2
        }
        
    except requests.RequestException as e:
        logger.warning(f"[ESTIMATE] Failed to fetch EPT metadata: {e}")
        return {'estimated': False}
    except Exception as e:
        logger.warning(f"[ESTIMATE] Estimation error: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return {'estimated': False}


def generate_tiles(aoi_bounds: Tuple[float, float, float, float], 
                   tile_size: float) -> List[Tuple[int, int, Tuple[float, float, float, float]]]:
    """
    Generate spatial tiles covering the AOI.
    
    Args:
        aoi_bounds: (minx, miny, maxx, maxy) in EPSG:4326
        tile_size: Tile size in degrees
        
    Returns:
        List of (tile_x, tile_y, (minx, miny, maxx, maxy)) tuples
    """
    minx, miny, maxx, maxy = aoi_bounds
    
    # Calculate tile grid
    n_tiles_x = math.ceil((maxx - minx) / tile_size)
    n_tiles_y = math.ceil((maxy - miny) / tile_size)
    
    tiles = []
    for ix in range(n_tiles_x):
        for iy in range(n_tiles_y):
            tile_minx = minx + ix * tile_size
            tile_miny = miny + iy * tile_size
            tile_maxx = min(tile_minx + tile_size, maxx)
            tile_maxy = min(tile_miny + tile_size, maxy)
            
            tiles.append((ix, iy, (tile_minx, tile_miny, tile_maxx, tile_maxy)))
    
    logger.info(f"[TILING] Generated {len(tiles)} tiles ({n_tiles_x}x{n_tiles_y} grid)")
    return tiles


def show_progress(description: str, stop_event: threading.Event):
    """Display a spinner and elapsed time while a background task runs."""
    start_time = time.time()
    spinner_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    i = 0
    try:
        while not stop_event.is_set():
            elapsed = time.time() - start_time
            elapsed_str = str(timedelta(seconds=int(elapsed)))
            spinner = spinner_chars[i % len(spinner_chars)]
            print(f"\r{spinner} {description} [{elapsed_str}]", end='', flush=True)
            i += 1
            time.sleep(0.1)
    finally:
        elapsed = time.time() - start_time
        elapsed_str = str(timedelta(seconds=int(elapsed)))
        print(f"\r✓ {description} [{elapsed_str}]" + ' ' * 20)


def process_tile(tile_info: Tuple[int, int, Tuple[float, float, float, float]],
                ept_url: str,
                utm_epsg: int,
                output_dir: Path,
                config: PipelineConfig) -> Tuple[bool, str, Optional[Path], Optional[Path]]:
    """
    Process a single tile: generate DTM and DSM.
    
    Args:
        tile_info: (tile_x, tile_y, bounds)
        ept_url: EPT endpoint URL
        utm_epsg: Target UTM EPSG code
        output_dir: Output directory for tiles
        config: Pipeline configuration
        
    Returns:
        (success, message, dtm_path, dsm_path)
    """
    tile_x, tile_y, (minx, miny, maxx, maxy) = tile_info
    tile_id = f"{tile_x}_{tile_y}"
    
    # Output paths
    tile_dir = output_dir / 'tiles'
    tile_dir.mkdir(parents=True, exist_ok=True)
    
    # Include resolution in tile filenames
    dtm_path = tile_dir / f'dtm_{tile_id}_{int(config.resolution_m)}m.tif'
    dsm_path = tile_dir / f'dsm_{tile_id}_{int(config.resolution_m)}m.tif'
    
    # Check cache
    if dtm_path.exists() and dsm_path.exists():
        return True, f"[TILE {tile_id}] Using cached tiles", dtm_path, dsm_path
    
    bounds_str = f"([{minx}, {maxx}], [{miny}, {maxy}])"
    
    # DTM pipeline
    dtm_pipeline = {
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
                "limits": "Classification[2:2]"
            },
            {
                "type": "writers.gdal",
                "filename": str(dtm_path),
                "resolution": config.resolution_m,
                "output_type": "min",
                "gdalopts": "COMPRESS=DEFLATE,TILED=YES"
            }
        ]
    }
    
    # DSM pipeline
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
                "resolution": config.resolution_m,
                "output_type": "max",
                "gdalopts": "COMPRESS=DEFLATE,TILED=YES"
            }
        ]
    }
    
    # Execute DTM with progress indicator
    try:
        dtm_json = json.dumps(dtm_pipeline)

        stop_event = threading.Event()
        progress_thread = threading.Thread(target=show_progress, args=(f"TILE {tile_id} - DTM", stop_event))
        progress_thread.start()

        result = subprocess.run(
            [config.pdal_exe, 'pipeline', '--stdin'],
            input=dtm_json,
            capture_output=True,
            text=True,
            timeout=config.timeout_per_tile
        )

        stop_event.set()
        progress_thread.join()

        if result.returncode != 0:
            return False, f"[TILE {tile_id}] DTM failed: {result.stderr[:200]}", None, None

    except subprocess.TimeoutExpired:
        stop_event.set()
        progress_thread.join()
        return False, f"[TILE {tile_id}] DTM timeout", None, None
    except Exception as e:
        stop_event.set()
        progress_thread.join()
        return False, f"[TILE {tile_id}] DTM error: {str(e)[:200]}", None, None
    
    # Execute DSM with progress indicator
    try:
        dsm_json = json.dumps(dsm_pipeline)

        stop_event = threading.Event()
        progress_thread = threading.Thread(target=show_progress, args=(f"TILE {tile_id} - DSM", stop_event))
        progress_thread.start()

        result = subprocess.run(
            [config.pdal_exe, 'pipeline', '--stdin'],
            input=dsm_json,
            capture_output=True,
            text=True,
            timeout=config.timeout_per_tile
        )

        stop_event.set()
        progress_thread.join()

        if result.returncode != 0:
            # Clean up partial DTM
            if dtm_path.exists():
                dtm_path.unlink()
            return False, f"[TILE {tile_id}] DSM failed: {result.stderr[:200]}", None, None
            
    except subprocess.TimeoutExpired:
        stop_event.set()
        progress_thread.join()
        if dtm_path.exists():
            dtm_path.unlink()
        return False, f"[TILE {tile_id}] DSM timeout", None, None
    except Exception as e:
        stop_event.set()
        progress_thread.join()
        if dtm_path.exists():
            dtm_path.unlink()
        return False, f"[TILE {tile_id}] DSM error: {str(e)[:200]}", None, None
    
    return True, f"[TILE {tile_id}] Success", dtm_path, dsm_path


def merge_tiles(tile_paths: List[Path], output_path: Path, nodata: float = -9999) -> bool:
    """
    Merge multiple raster tiles into a single raster.
    
    Args:
        tile_paths: List of tile raster paths
        output_path: Output merged raster path
        nodata: NoData value
        
    Returns:
        Success boolean
    """
    try:
        # Filter existing tiles
        existing_tiles = [p for p in tile_paths if p and p.exists()]
        
        if not existing_tiles:
            logger.error(f"[MERGE] No valid tiles to merge for {output_path.name}")
            return False
        
        # Open all tiles
        src_files_to_mosaic = []
        for tile in existing_tiles:
            src = rasterio.open(tile)
            src_files_to_mosaic.append(src)
        
        # Merge
        mosaic, out_trans = merge(src_files_to_mosaic, nodata=nodata)
        
        # Copy metadata from first tile
        out_meta = src_files_to_mosaic[0].meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_trans,
            "nodata": nodata,
            "compress": "deflate",
            "tiled": True
        })
        
        # Write merged raster
        with rasterio.open(output_path, "w", **out_meta) as dest:
            dest.write(mosaic)
        
        # Close source files
        for src in src_files_to_mosaic:
            src.close()
        
        logger.info(f"[MERGE] Created {output_path.name} from {len(existing_tiles)} tiles")
        return True
        
    except Exception as e:
        logger.error(f"[MERGE] Failed to merge {output_path.name}: {e}")
        return False


def compute_chm_and_agb(dtm_path: Path, dsm_path: Path, output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """
    Compute CHM and AGB from DTM and DSM (unchanged logic from original).
    
    Args:
        dtm_path: DTM raster path
        dsm_path: DSM raster path
        output_dir: Output directory
        
    Returns:
        (chm_path, agb_path) or (None, None) on failure
    """
    # Include resolution in output filenames
    chm_path = output_dir / f'chm_{int(RASTER_RES_M)}m.tif'
    agb_path = output_dir / f'agb_{int(RASTER_RES_M)}m.tif'
    
    try:
        logger.info("[CHM] Computing CHM = DSM - DTM...")
        
        with rasterio.open(dsm_path) as dsm_src, rasterio.open(dtm_path) as dtm_src:
            dsm = dsm_src.read(1)
            dtm = dtm_src.read(1)
            dsm_nodata = dsm_src.nodata
            dtm_nodata = dtm_src.nodata
            
            # Mask nodata (unchanged logic)
            dsm_valid = (dsm != dsm_nodata) if dsm_nodata is not None else np.ones_like(dsm, dtype=bool)
            dtm_valid = (dtm != dtm_nodata) if dtm_nodata is not None else np.ones_like(dtm, dtype=bool)
            both_valid = dsm_valid & dtm_valid
            
            # Compute CHM (unchanged logic)
            chm = np.zeros_like(dsm, dtype=np.float32)
            chm[both_valid] = dsm[both_valid] - dtm[both_valid]
            chm[~both_valid] = -9999
            
            # Clip negative values (unchanged logic)
            chm[chm < 0] = 0
            
            # Validation (unchanged logic)
            valid_chm = chm[chm > 0]
            if len(valid_chm) == 0:
                logger.error("[CHM] CHM is all zeros - data quality issue")
                return None, None
            
            chm_max = valid_chm.max()
            chm_mean = valid_chm.mean()
            chm_p95 = np.percentile(valid_chm, 95)
            
            logger.info(f"[CHM] Stats: max={chm_max:.1f}m, mean={chm_mean:.1f}m, p95={chm_p95:.1f}m, valid_pixels={len(valid_chm)}")
            
            if chm_max < 2.0:
                logger.error(f"[CHM] Max height < 2m - rejecting as unrealistic")
                return None, None
            
            # Save CHM
            profile = dsm_src.profile.copy()
            profile.update(dtype=rasterio.float32, nodata=-9999, compress='deflate')
            
            with rasterio.open(chm_path, 'w', **profile) as chm_dst:
                chm_dst.write(chm, 1)
            
            logger.info(f"[CHM] Saved: {chm_path}")
            
            # Apply FIA allometry (unchanged logic)
            logger.info("[AGB] Applying FIA allometry: AGB = 0.168 × H^2.47")
            agb = np.zeros_like(chm, dtype=np.float32)
            valid_mask = (chm > 0) & (chm != -9999)
            agb[valid_mask] = FIA_A * (chm[valid_mask] ** FIA_B)
            agb[~valid_mask] = -9999
            
            # Save AGB
            with rasterio.open(agb_path, 'w', **profile) as agb_dst:
                agb_dst.write(agb, 1)
            
            logger.info(f"[AGB] Saved: {agb_path}")
            
            return chm_path, agb_path
            
    except Exception as e:
        logger.error(f"[CHM/AGB] Failed: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, None


def calculate_carbon_stock(chm_path: Path, agb_path: Path, aoi_area_ha: float) -> dict:
    """
    Calculate carbon stock statistics (unchanged logic from original).
    
    Args:
        chm_path: CHM raster path
        agb_path: AGB raster path
        aoi_area_ha: AOI area in hectares
        
    Returns:
        Results dictionary
    """
    logger.info("=" * 80)
    logger.info("CALCULATING CARBON STOCK")
    logger.info("=" * 80)
    
    with rasterio.open(chm_path) as chm_src, rasterio.open(agb_path) as agb_src:
        chm = chm_src.read(1)
        agb = agb_src.read(1)
        chm_profile = chm_src.profile
        
        # Calculate carbon stock (unchanged logic)
        pixel_area_ha = abs(chm_profile['transform'][0] * chm_profile['transform'][4]) / 10000
        
        valid_agb = agb[agb > 0]
        valid_chm = chm[(chm > 0) & (chm != -9999)]
        
        total_agb_mg = valid_agb.sum() * pixel_area_ha
        total_carbon_mg = total_agb_mg * CARBON_FRACTION
        total_co2e_mg = total_carbon_mg * CO2_TO_C
        
        forest_area_ha = len(valid_agb) * pixel_area_ha
        mean_agb_mg_ha = total_agb_mg / forest_area_ha if forest_area_ha > 0 else 0
        mean_chm_m = valid_chm.mean() if len(valid_chm) > 0 else 0
        
        logger.info(f"AOI area: {aoi_area_ha:.2f} ha")
        logger.info(f"Forest area (LiDAR): {forest_area_ha:.2f} ha")
        logger.info(f"Mean tree height: {mean_chm_m:.2f} m")
        logger.info(f"Mean AGB: {mean_agb_mg_ha:.2f} Mg/ha")
        logger.info(f"Total AGB: {total_agb_mg:.2f} Mg")
        logger.info(f"Total Carbon: {total_carbon_mg:.2f} Mg C")
        logger.info(f"Total CO2e: {total_co2e_mg:.2f} Mg CO2e")
        logger.info("=" * 80)
        
        return {
            'aoi_area_ha': float(aoi_area_ha),
            'forest_area_ha': float(forest_area_ha),
            'mean_tree_height_m': float(mean_chm_m),
            'mean_agb_mg_ha': float(mean_agb_mg_ha),
            'total_agb_mg': float(total_agb_mg),
            'total_carbon_mg': float(total_carbon_mg),
            'total_co2e_mg': float(total_co2e_mg)
        }


def main():
    """Run tiled EPT pipeline"""
    
    # Configuration
    config = PipelineConfig(
        tile_size_deg=0.005,      # ~500m tiles (smaller = faster per tile)
        resolution_m=RASTER_RES_M,         # controlled by RASTER_RES_M
        max_workers=2,            # 2 parallel tiles (reduce contention)
        timeout_per_tile=1200,    # 20 min per tile (increased)
        max_data_size_gb=5.0      # 5 GB threshold
    )

    # Safety guard: clamp any accidental low-resolution config
    if config.resolution_m < 2.0:
        logger.warning(f"Configured resolution {config.resolution_m}m is below safe threshold; clamping to {int(RASTER_RES_M)}m")
        config.resolution_m = RASTER_RES_M
    
    # Setup
    logger.info("=" * 80)
    logger.info("TILED LIDAR EPT PROCESSING - TREE HEIGHT & AGB")
    logger.info("=" * 80)
    logger.info(f"Config: {config.tile_size_deg:.4f}° tiles, {config.resolution_m}m resolution, {config.max_workers} workers")
    
    # Paths
    aoi_geojson_path = Path('data/raw/oregon_aoi.geojson')
    output_dir = Path('outputs/lidar_ept_tiled')
    
    if not aoi_geojson_path.exists():
        logger.error(f"AOI not found: {aoi_geojson_path}")
        return 1
    
    # Load AOI
    logger.info(f"Loading AOI: {aoi_geojson_path}")
    aoi = gpd.read_file(aoi_geojson_path)
    aoi_bounds = aoi.total_bounds
    logger.info(f"AOI bounds (WGS84): {aoi_bounds}")
    
    # Calculate area
    aoi_utm = aoi.to_crs(aoi.estimate_utm_crs())
    aoi_area_ha = aoi_utm.geometry.area.sum() / 10000
    logger.info(f"AOI area: {aoi_area_ha:.2f} ha")
    
    # Calculate UTM zone
    centroid_lon = (aoi_bounds[0] + aoi_bounds[2]) / 2
    centroid_lat = (aoi_bounds[1] + aoi_bounds[3]) / 2
    utm_zone = int((centroid_lon + 180) / 6) + 1
    hemisphere = 'north' if centroid_lat >= 0 else 'south'
    utm_epsg = 32600 + utm_zone if hemisphere == 'north' else 32700 + utm_zone
    logger.info(f"Output CRS: EPSG:{utm_epsg} (Zone {utm_zone}{hemisphere[0].upper()})")
    
    # EPT URL
    ept_url = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public/OR_McKenzieRiver_1_2021/ept.json"
    logger.info(f"EPT source: {ept_url}")
    
    # Pre-flight data volume estimation
    estimation = estimate_data_volume(ept_url, aoi_bounds, utm_epsg)
    
    if estimation.get('estimated'):
        # Check if data size exceeds threshold
        estimated_gb = estimation.get('estimated_gb', 0)
        if estimated_gb > config.max_data_size_gb:
            logger.error("=" * 80)
            logger.error(f"[ABORT] Estimated data size ({estimated_gb:.2f} GB) exceeds threshold ({config.max_data_size_gb} GB)")
            logger.error("[ABORT] To proceed anyway, increase config.max_data_size_gb")
            logger.error("=" * 80)
            return 1
        else:
            logger.info(f"[CHECK] Data size OK ({estimated_gb:.3f} GB < {config.max_data_size_gb} GB threshold)")
    else:
        logger.warning("[CHECK] Proceeding without volume estimation")
    
    logger.info("")
    
    # Generate tiles
    logger.info("=" * 80)
    logger.info("PHASE 1: SPATIAL TILING")
    logger.info("=" * 80)
    tiles = generate_tiles(aoi_bounds, config.tile_size_deg)
    
    # Process tiles in parallel
    logger.info("=" * 80)
    logger.info("PHASE 2: PARALLEL TILE PROCESSING")
    logger.info("=" * 80)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    dtm_tiles = []
    dsm_tiles = []
    success_count = 0
    fail_count = 0
    
    with ProcessPoolExecutor(max_workers=config.max_workers) as executor:
        # Submit all tiles
        futures = {
            executor.submit(process_tile, tile, ept_url, utm_epsg, output_dir, config): tile
            for tile in tiles
        }
        
        # Collect results
        for future in as_completed(futures):
            success, message, dtm_path, dsm_path = future.result()
            logger.info(message)
            
            if success:
                success_count += 1
                dtm_tiles.append(dtm_path)
                dsm_tiles.append(dsm_path)
            else:
                fail_count += 1
    
    logger.info(f"[TILES] Completed: {success_count} success, {fail_count} failed")
    
    if success_count == 0:
        logger.error("[PIPELINE] No tiles succeeded - aborting")
        return 1
    
    # Merge tiles
    logger.info("=" * 80)
    logger.info("PHASE 3: TILE MERGING")
    logger.info("=" * 80)
    
    dtm_merged = output_dir / f'dtm_{int(config.resolution_m)}m.tif'
    dsm_merged = output_dir / f'dsm_{int(config.resolution_m)}m.tif'
    
    if not merge_tiles(dtm_tiles, dtm_merged):
        logger.error("[MERGE] DTM merge failed")
        return 1
    
    if not merge_tiles(dsm_tiles, dsm_merged):
        logger.error("[MERGE] DSM merge failed")
        return 1
    
    # Compute CHM and AGB
    logger.info("=" * 80)
    logger.info("PHASE 4: CHM & AGB CALCULATION")
    logger.info("=" * 80)
    
    chm_path, agb_path = compute_chm_and_agb(dtm_merged, dsm_merged, output_dir)
    
    if chm_path is None:
        logger.error("[PIPELINE] CHM/AGB calculation failed")
        return 1
    
    # Calculate carbon stock
    results = calculate_carbon_stock(chm_path, agb_path, aoi_area_ha)
    
    # Add metadata
    results.update({
        'status': 'SUCCESS',
        'source': 'USGS_3DEP_EPT',
        'project': 'OR_McKenzieRiver_1_2021',
        'acquisition_year': 2021,
        'resolution_m': config.resolution_m,
        'tiles_processed': success_count,
        'tiles_failed': fail_count,
        'chm_path': str(chm_path),
        'agb_path': str(agb_path)
    })
    
    # Save results
    results_path = output_dir / 'results.json'
    with open(results_path, 'w') as f:
        json.dump(results, f, indent=2)
    
    logger.info(f"Results saved: {results_path}")
    logger.info("=" * 80)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 80)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
