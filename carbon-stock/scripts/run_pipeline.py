"""
Complete Forest Carbon Stock Estimation Pipeline
=================================================

ORCHESTRATOR: Coordinates LiDAR-only carbon estimation workflow.

WORKFLOW:
1. Validate AOI
2. Fetch NLCD forest mask
3. Process LiDAR data (local LAZ tiles or USGS 3DEP fallback)
4. Calculate area-based carbon stock

USAGE:
    # With local LAZ tiles (recommended)
    python scripts/run_pipeline.py --laz-tiles path/to/tiles --year 2023 --output outputs/

    # With AOI (auto-download USGS 3DEP if available)
    python scripts/run_pipeline.py --aoi path/to/aoi.geojson --year 2023 --output outputs/

OUTPUTS:
    outputs/
    ├── lidar/           (DTM, DSM, CHM, biomass rasters)
    ├── masks/           (forest_mask.tif)
    └── results/         (carbon_stock_summary.json)

NOTE: Sentinel-2 and Sentinel-1 are BYPASSED. GEDI is OPTIONAL (auto-skips to LiDAR).
      This is a LiDAR-only pipeline optimized for USGS 3DEP data.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # Add scripts directory to path

import argparse
import json
import logging
from datetime import datetime
from typing import Dict, Tuple, Optional
import geopandas as gpd
import rasterio
from rasterio.warp import transform_bounds
from shapely.geometry import box
import ee
import laspy
from pyproj import CRS

# Configure logging first
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('pipeline.log')
    ]
)
logger = logging.getLogger(__name__)

# Import ALS fallback module
try:
    from usgs_3dep_lidar_fallback import als_fallback_pipeline, local_laz_carbon_stock_pipeline
    ALS_AVAILABLE = True
    logger.info("[INFO] ALS fallback module loaded successfully")
except ImportError as e:
    ALS_AVAILABLE = False
    logger.warning(f"[WARNING] ALS fallback module not available: {e}")


class PipelineError(Exception):
    """Raised when pipeline step fails"""
    pass


class PipelineStatus:
    """
    Tracks execution status of each pipeline stage.
    
    Status values:
    - SUCCESS: Stage completed successfully with valid outputs
    - FAILED: Stage failed - pipeline must stop
    - SKIPPED: Stage intentionally skipped (rare, must be explicit)
    - NOT_STARTED: Stage has not been executed yet
    """
    def __init__(self):
        self.stages = {
            'aoi_validation': 'NOT_STARTED',
            'sentinel2': 'NOT_STARTED',
            'sentinel1': 'NOT_STARTED',
            'lidar': 'NOT_STARTED',
            'forest_mask': 'NOT_STARTED',
            'gedi_biomass': 'NOT_STARTED',
            'carbon_stock': 'NOT_STARTED'
        }
        self.failure_reasons = {}
    
    def set_status(self, stage: str, status: str, reason: str = None):
        """Set stage status with optional failure reason"""
        if stage not in self.stages:
            raise ValueError(f"Unknown stage: {stage}")
        if status not in ['SUCCESS', 'FAILED', 'SKIPPED', 'NOT_STARTED']:
            raise ValueError(f"Invalid status: {status}")
        
        self.stages[stage] = status
        if status == 'FAILED' and reason:
            self.failure_reasons[stage] = reason
    
    def get_status(self, stage: str) -> str:
        """Get current status of a stage"""
        return self.stages.get(stage, 'NOT_STARTED')
    
    def is_success(self, stage: str) -> bool:
        """Check if stage completed successfully"""
        return self.stages.get(stage) == 'SUCCESS'
    
    def print_summary(self):
        """Print execution summary table"""
        logger.info("=" * 80)
        logger.info("PIPELINE EXECUTION SUMMARY")
        logger.info("=" * 80)
        logger.info(f"{'Stage':<20} {'Status':<15} {'Details':<30}")
        logger.info("-" * 80)
        
        for stage, status in self.stages.items():
            details = self.failure_reasons.get(stage, '')
            logger.info(f"{stage:<20} {status:<15} {details:<30}")
        
        logger.info("=" * 80)


class PipelineError(Exception):
    """Raised when pipeline step fails"""
    pass


def create_aoi_tiles(bounds: Tuple[float, float, float, float], tile_size_deg: float = 0.25) -> list:
    """
    Split AOI into manageable tiles for Earth Engine exports.
    
    Args:
        bounds: (minx, miny, maxx, maxy) in EPSG:4326
        tile_size_deg: Tile size in degrees (default 0.25° ≈ 25km)
        
    Returns:
        List of tile geometries as ee.Geometry.Rectangle objects
    """
    minx, miny, maxx, maxy = bounds
    
    tiles = []
    tile_id = 0
    
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            tile_minx = x
            tile_miny = y
            tile_maxx = min(x + tile_size_deg, maxx)
            tile_maxy = min(y + tile_size_deg, maxy)
            
            tile_geom = ee.Geometry.Rectangle([tile_minx, tile_miny, tile_maxx, tile_maxy])
            tiles.append({
                'id': tile_id,
                'geometry': tile_geom,
                'bounds': (tile_minx, tile_miny, tile_maxx, tile_maxy)
            })
            
            tile_id += 1
            y += tile_size_deg
        x += tile_size_deg
    
    return tiles


def validate_aoi(aoi_path: Path, status: PipelineStatus) -> Tuple[gpd.GeoDataFrame, Dict]:
    """
    Step 1: Validate AOI and compute basic properties.
    
    Supports both GeoJSON and GeoTIFF inputs.
    
    Args:
        aoi_path: Path to AOI file (GeoJSON or GeoTIFF)
        status: Pipeline status tracker
        
    Returns:
        Tuple of (GeoDataFrame, metadata dict)
        
    Raises:
        PipelineError: If AOI is invalid
    """
    logger.info("=" * 80)
    logger.info("[START] STEP 1: AOI VALIDATION")
    logger.info("=" * 80)
    logger.info(f"[INFO] Loading AOI from: {aoi_path}")
    logger.info("[STEP 1] AOI VALIDATION")
    logger.info("=" * 80)
    
    try:
        # Check if file exists
        if not aoi_path.exists():
            raise PipelineError(f"AOI file not found: {aoi_path}")
        
        logger.info(f"Loading AOI from: {aoi_path}")
        
        # Detect file type
        if aoi_path.suffix.lower() in ['.tif', '.tiff']:
            # Load from raster
            logger.info("Detected GeoTIFF input - extracting bounds")
            
            with rasterio.open(aoi_path) as src:
                # Get bounds in source CRS
                bounds_src = src.bounds
                src_crs = src.crs
                
                # Transform to WGS84 if needed
                if src_crs.to_epsg() != 4326:
                    logger.info(f"Transforming bounds from {src_crs} to EPSG:4326")
                    bounds = transform_bounds(src_crs, 'EPSG:4326', *bounds_src)
                else:
                    bounds = bounds_src
                
                # Create GeoDataFrame from bounds
                geometry = box(bounds[0], bounds[1], bounds[2], bounds[3])
                aoi = gpd.GeoDataFrame([{'geometry': geometry}], crs='EPSG:4326')
                
                # Calculate approximate area
                area_deg2 = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
                # Rough conversion (1 degree ≈ 111 km at equator)
                area_km2 = area_deg2 * (111 * 111)
                area_ha = area_km2 * 100
                
        elif aoi_path.suffix.lower() in ['.geojson', '.json', '.shp', '.gpkg']:
            # Load vector file
            logger.info("Detected vector input - loading geometry")
            aoi = gpd.read_file(aoi_path)
            
            if len(aoi) == 0:
                raise PipelineError("AOI file is empty")
            
            # Ensure WGS84
            if aoi.crs is None:
                logger.warning("AOI has no CRS, assuming EPSG:4326")
                aoi = aoi.set_crs("EPSG:4326")
            elif aoi.crs.to_epsg() != 4326:
                logger.info(f"Reprojecting AOI from {aoi.crs} to EPSG:4326")
                aoi = aoi.to_crs("EPSG:4326")
            
            # Get bounds and area
            bounds = aoi.total_bounds  # [minx, miny, maxx, maxy]
            area_m2 = aoi.geometry.area.sum()
            area_ha = area_m2 / 10000
        else:
            raise PipelineError(f"Unsupported file format: {aoi_path.suffix}")
        
        # Get final bounds
        bounds = aoi.total_bounds
        
        metadata = {
            'bounds': {
                'west': float(bounds[0]),
                'south': float(bounds[1]),
                'east': float(bounds[2]),
                'north': float(bounds[3])
            },
            'area_ha': float(area_ha),
            'area_km2': float(area_ha / 100),
            'geometry_count': len(aoi),
            'crs': 'EPSG:4326',
            'source_file': str(aoi_path)
        }
        
        logger.info(f"[INFO] AOI Bounds: {bounds}")
        logger.info(f"[INFO] AOI Area: {area_ha:.2f} ha ({area_ha/100:.2f} km²)")
        logger.info(f"[INFO] Geometries: {len(aoi)}")
        
        # Mark stage as SUCCESS
        status.set_status('aoi_validation', 'SUCCESS')
        logger.info("[SUCCESS] AOI validation complete")
        logger.info("=" * 80)
        
        return aoi, metadata
        
    except Exception as e:
        status.set_status('aoi_validation', 'FAILED', str(e))
        logger.error(f"[FAILED] AOI validation: {str(e)}")
        raise PipelineError(f"AOI validation failed: {str(e)}")


def fetch_sentinel2_qa(
    aoi: gpd.GeoDataFrame,
    year: int,
    output_dir: Path,
    status: PipelineStatus
) -> Dict:
    """
    Step 2: Fetch Sentinel-2 data for QA/visualization.
    
    USAGE RESTRICTION: Supporting data only - NOT for biomass estimation.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        year: Year to fetch data for
        output_dir: Output directory (outputs/sentinel2/)
        status: Pipeline status tracker
        
    Returns:
        Metadata dict with output paths
    """
    logger.info("=" * 80)
    logger.info("[START] STEP 2: SENTINEL-2 FETCH")
    logger.info("=" * 80)
    
    # BYPASS: Skip Sentinel-2 processing entirely
    logger.info("[INFO] Sentinel-2 processing BYPASSED - using LiDAR only")
    status.set_status('sentinel2', 'SKIPPED')
    logger.info("[SKIPPED] Sentinel-2 stage - bypassed for LiDAR-only processing")
    logger.info("=" * 80)
    
    metadata = {
        'status': 'skipped',
        'usage_scope': 'CONTEXTUAL_VALIDATION',
        'reason': 'Bypassed - LiDAR-only processing mode'
    }
    return metadata
    
    try:
        # Initialize Earth Engine (must be authenticated beforehand)
        ee.Initialize(project='heroic-bliss-472519-f7')
        
        # Convert AOI to EE geometry
        bounds = aoi.total_bounds
        ee_aoi = ee.Geometry.Rectangle([
            float(bounds[0]), float(bounds[1]),
            float(bounds[2]), float(bounds[3])
        ])
        
        # Fetch Sentinel-2 SR
        s2 = ee.ImageCollection('COPERNICUS/S2_SR') \
            .filterBounds(ee_aoi) \
            .filterDate(f'{year}-01-01', f'{year}-12-31') \
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
        
        # Calculate median composite
        composite = s2.median().clip(ee_aoi)
        
        # Calculate indices (for QA only)
        nir = composite.select('B8')
        red = composite.select('B4')
        green = composite.select('B3')
        blue = composite.select('B2')
        swir1 = composite.select('B11')
        
        ndvi = nir.subtract(red).divide(nir.add(red)).rename('NDVI')
        evi = composite.expression(
            '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))',
            {'NIR': nir, 'RED': red, 'BLUE': blue}
        ).rename('EVI')
        ndmi = nir.subtract(swir1).divide(nir.add(swir1)).rename('NDMI')
        
        # Get image count BEFORE exporting (for validation)
        image_count = s2.size().getInfo()
        
        logger.info(f"Found {image_count} Sentinel-2 images")
        
        # CRITICAL: Validate that we have imagery
        if image_count == 0:
            logger.error("[ERROR] No Sentinel-2 images found for AOI and time period")
            raise PipelineError("Sentinel-2 data not available - cannot proceed")
        
        # MANDATORY: Export indices to disk using TILED APPROACH
        logger.info("[INFO] Exporting Sentinel-2 indices to disk (TILED, REQUIRED)...")
        
        # Create tiles (very small for API limits)
        tiles = create_aoi_tiles(bounds, tile_size_deg=0.05)  # ~5km tiles
        logger.info(f"[INFO] AOI split into {len(tiles)} tiles (0.05° each)")
        
        # Export scale: 30m for Sentinel-2 (balances quality vs. API limits)
        export_scale = 30
        
        import requests
        import time
        from rasterio.merge import merge
        
        # Create tile directories
        tile_dir_ndvi = output_dir / 'tiles' / 'ndvi'
        tile_dir_evi = output_dir / 'tiles' / 'evi'
        tile_dir_ndmi = output_dir / 'tiles' / 'ndmi'
        tile_dir_ndvi.mkdir(parents=True, exist_ok=True)
        tile_dir_evi.mkdir(parents=True, exist_ok=True)
        tile_dir_ndmi.mkdir(parents=True, exist_ok=True)
        
        # Export each index per tile
        ndvi_tile_paths = []
        evi_tile_paths = []
        ndmi_tile_paths = []
        
        for i, tile in enumerate(tiles):
            tile_id = tile['id']
            tile_geom = tile['geometry']
            
            logger.info(f"[INFO] Processing tile {i+1}/{len(tiles)} (ID: {tile_id})...")
            
            # Clip indices to tile
            ndvi_tile = ndvi.clip(tile_geom)
            evi_tile = evi.clip(tile_geom)
            ndmi_tile = ndmi.clip(tile_geom)
            
            # Export NDVI tile
            ndvi_tile_path = tile_dir_ndvi / f'ndvi_tile_{tile_id}.tif'
            try:
                url = ndvi_tile.getDownloadURL({
                    'region': tile_geom,
                    'scale': export_scale,
                    'format': 'GEO_TIFF',
                    'crs': 'EPSG:4326'
                })
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                with open(ndvi_tile_path, 'wb') as f:
                    f.write(response.content)
                ndvi_tile_paths.append(ndvi_tile_path)
                logger.info(f"[SUCCESS] NDVI tile {tile_id} downloaded ({ndvi_tile_path.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                logger.error(f"[ERROR] NDVI tile {tile_id} export failed: {e}")
                raise PipelineError(f"Sentinel-2 export failed for tile {tile_id}: {e}")
            
            # Export EVI tile
            evi_tile_path = tile_dir_evi / f'evi_tile_{tile_id}.tif'
            try:
                url = evi_tile.getDownloadURL({
                    'region': tile_geom,
                    'scale': export_scale,
                    'format': 'GEO_TIFF',
                    'crs': 'EPSG:4326'
                })
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                with open(evi_tile_path, 'wb') as f:
                    f.write(response.content)
                evi_tile_paths.append(evi_tile_path)
                logger.info(f"[SUCCESS] EVI tile {tile_id} downloaded ({evi_tile_path.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                logger.error(f"[ERROR] EVI tile {tile_id} export failed: {e}")
                raise PipelineError(f"Sentinel-2 export failed for tile {tile_id}: {e}")
            
            # Export NDMI tile
            ndmi_tile_path = tile_dir_ndmi / f'ndmi_tile_{tile_id}.tif'
            try:
                url = ndmi_tile.getDownloadURL({
                    'region': tile_geom,
                    'scale': export_scale,
                    'format': 'GEO_TIFF',
                    'crs': 'EPSG:4326'
                })
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                with open(ndmi_tile_path, 'wb') as f:
                    f.write(response.content)
                ndmi_tile_paths.append(ndmi_tile_path)
                logger.info(f"[SUCCESS] NDMI tile {tile_id} downloaded ({ndmi_tile_path.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                logger.error(f"[ERROR] NDMI tile {tile_id} export failed: {e}")
                raise PipelineError(f"Sentinel-2 export failed for tile {tile_id}: {e}")
            
            time.sleep(0.5)  # Rate limiting
        
        logger.info(f"[SUCCESS] All {len(tiles)} tiles exported successfully")
        
        # MANDATORY: Mosaic tiles into final products
        logger.info("[INFO] Mosaicking tiles into final products...")
        
        # Mosaic NDVI
        ndvi_path = output_dir / 'ndvi.tif'
        src_files_ndvi = [rasterio.open(p) for p in ndvi_tile_paths]
        mosaic_ndvi, out_trans_ndvi = merge(src_files_ndvi)
        out_meta_ndvi = src_files_ndvi[0].meta.copy()
        out_meta_ndvi.update({
            'height': mosaic_ndvi.shape[1],
            'width': mosaic_ndvi.shape[2],
            'transform': out_trans_ndvi
        })
        with rasterio.open(ndvi_path, 'w', **out_meta_ndvi) as dest:
            dest.write(mosaic_ndvi)
        for src in src_files_ndvi:
            src.close()
        logger.info(f"[SUCCESS] NDVI mosaicked: {ndvi_path}")
        
        # Mosaic EVI
        evi_path = output_dir / 'evi.tif'
        src_files_evi = [rasterio.open(p) for p in evi_tile_paths]
        mosaic_evi, out_trans_evi = merge(src_files_evi)
        out_meta_evi = src_files_evi[0].meta.copy()
        out_meta_evi.update({
            'height': mosaic_evi.shape[1],
            'width': mosaic_evi.shape[2],
            'transform': out_trans_evi
        })
        with rasterio.open(evi_path, 'w', **out_meta_evi) as dest:
            dest.write(mosaic_evi)
        for src in src_files_evi:
            src.close()
        logger.info(f"[SUCCESS] EVI mosaicked: {evi_path}")
        
        # Mosaic NDMI
        ndmi_path = output_dir / 'ndmi.tif'
        src_files_ndmi = [rasterio.open(p) for p in ndmi_tile_paths]
        mosaic_ndmi, out_trans_ndmi = merge(src_files_ndmi)
        out_meta_ndmi = src_files_ndmi[0].meta.copy()
        out_meta_ndmi.update({
            'height': mosaic_ndmi.shape[1],
            'width': mosaic_ndmi.shape[2],
            'transform': out_trans_ndmi
        })
        with rasterio.open(ndmi_path, 'w', **out_meta_ndmi) as dest:
            dest.write(mosaic_ndmi)
        for src in src_files_ndmi:
            src.close()
        logger.info(f"[SUCCESS] NDMI mosaicked: {ndmi_path}")
        
        # MANDATORY: Validate final exports
        logger.info("[INFO] Validating Sentinel-2 exports...")
        
        validation_passed = True
        missing_files = []
        
        if not ndvi_path.exists():
            missing_files.append('ndvi.tif')
            validation_passed = False
        if not evi_path.exists():
            missing_files.append('evi.tif')
            validation_passed = False
        if not ndmi_path.exists():
            missing_files.append('ndmi.tif')
            validation_passed = False
        
        if not validation_passed:
            reason = f"Missing files: {', '.join(missing_files)}"
            status.set_status('sentinel2', 'FAILED', reason)
            logger.error(f"[FAILED] Sentinel-2 export: {reason}")
            raise PipelineError(f"Sentinel-2 export validation failed: {reason}")
        
        # Check file sizes (must be > 0)
        if ndvi_path.stat().st_size == 0 or evi_path.stat().st_size == 0 or ndmi_path.stat().st_size == 0:
            reason = "One or more files are empty (0 bytes)"
            status.set_status('sentinel2', 'FAILED', reason)
            logger.error(f"[FAILED] Sentinel-2 export: {reason}")
            raise PipelineError(f"Sentinel-2 export validation failed: {reason}")
        
        # Mark as SUCCESS
        status.set_status('sentinel2', 'SUCCESS')
        logger.info(f"[SUCCESS] Sentinel-2 exported: NDVI ({ndvi_path.stat().st_size / 1024:.1f} KB), EVI ({evi_path.stat().st_size / 1024:.1f} KB), NDMI ({ndmi_path.stat().st_size / 1024:.1f} KB)")
        logger.info("[SUCCESS] Sentinel-2 stage complete")
        logger.info("=" * 80)
        
        metadata = {
            'status': 'success',
            'usage_scope': 'CONTEXTUAL_VALIDATION',
            'warning': 'Used for context and validation - NOT for direct biomass estimation',
            'outputs': {
                'ndvi': str(ndvi_path),
                'evi': str(evi_path),
                'ndmi': str(ndmi_path)
            },
            'year': year,
            'image_count': image_count,
            'files_exported': True
        }
        
        logger.info("WARNING: Sentinel-2 indices are for QA/visualization only")
        logger.info("WARNING: Use GEDI L4A for biomass estimation")
        logger.info("[DONE] Sentinel-2 processing complete")
        logger.info("=" * 80)
        
        return metadata
        
    except Exception as e:
        status.set_status('sentinel2', 'FAILED', str(e))
        logger.error(f"[FAILED] Sentinel-2 fetch: {str(e)}")
        raise PipelineError(f"Sentinel-2 fetch failed: {str(e)}")


def fetch_sentinel1_disturbance(
    aoi: gpd.GeoDataFrame,
    year: int,
    output_dir: Path,
    status: PipelineStatus
) -> Dict:
    """
    Step 3: Fetch Sentinel-1 data for disturbance detection.
    
    USAGE RESTRICTION: Disturbance detection only - NOT for biomass/height.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        year: Year to fetch data for
        output_dir: Output directory (outputs/sentinel1/)
        status: Pipeline status tracker
        
    Returns:
        Metadata dict with output paths
    """
    logger.info("=" * 80)
    logger.info("[START] STEP 3: SENTINEL-1 FETCH")
    logger.info("=" * 80)
    
    # BYPASS: Skip Sentinel-1 processing entirely
    logger.info("[INFO] Sentinel-1 processing BYPASSED - using LiDAR only")
    status.set_status('sentinel1', 'SKIPPED')
    logger.info("[SKIPPED] Sentinel-1 stage - bypassed for LiDAR-only processing")
    logger.info("=" * 80)
    
    metadata = {
        'status': 'skipped',
        'usage_scope': 'STRUCTURAL_CONTEXT',
        'reason': 'Bypassed - LiDAR-only processing mode'
    }
    return metadata
    
    try:
        # Initialize Earth Engine (must be authenticated beforehand)
        ee.Initialize(project='heroic-bliss-472519-f7')
        
        # Convert AOI to EE geometry
        bounds = aoi.total_bounds
        ee_aoi = ee.Geometry.Rectangle([
            float(bounds[0]), float(bounds[1]),
            float(bounds[2]), float(bounds[3])
        ])
        
        # Fetch Sentinel-1 GRD
        s1 = ee.ImageCollection('COPERNICUS/S1_GRD') \
            .filterBounds(ee_aoi) \
            .filterDate(f'{year}-01-01', f'{year}-12-31') \
            .filter(ee.Filter.eq('instrumentMode', 'IW')) \
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV')) \
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'))
        
        # Calculate median composite
        composite = s1.median().clip(ee_aoi)
        vv = composite.select('VV')
        vh = composite.select('VH')
        
        # Calculate VH/VV ratio
        vh_vv_ratio = vh.divide(vv).rename('VH_VV_ratio')
        
        # Get image count BEFORE exporting (for validation)
        image_count = s1.size().getInfo()
        
        logger.info(f"Found {image_count} Sentinel-1 images")
        
        # CRITICAL: Validate that we have imagery
        if image_count == 0:
            logger.error("[ERROR] No Sentinel-1 images found for AOI and time period")
            raise PipelineError("Sentinel-1 data not available - cannot proceed")
        
        # MANDATORY: Export SAR data to disk using TILED APPROACH
        logger.info("[INFO] Exporting Sentinel-1 data to disk (TILED, REQUIRED)...")
        
        # Create tiles (very small for API limits)
        tiles = create_aoi_tiles(bounds, tile_size_deg=0.05)  # ~5km tiles
        logger.info(f"[INFO] AOI split into {len(tiles)} tiles (0.05° each)")
        
        # Export scale: 30m for Sentinel-1 (balances quality vs. API limits)
        export_scale = 30
        
        import requests
        import time
        from rasterio.merge import merge
        
        # Create tile directories
        tile_dir_vv = output_dir / 'tiles' / 'vv'
        tile_dir_vh = output_dir / 'tiles' / 'vh'
        tile_dir_ratio = output_dir / 'tiles' / 'ratio'
        tile_dir_vv.mkdir(parents=True, exist_ok=True)
        tile_dir_vh.mkdir(parents=True, exist_ok=True)
        tile_dir_ratio.mkdir(parents=True, exist_ok=True)
        
        # Export each band per tile
        vv_tile_paths = []
        vh_tile_paths = []
        ratio_tile_paths = []
        
        for i, tile in enumerate(tiles):
            tile_id = tile['id']
            tile_geom = tile['geometry']
            
            logger.info(f"[INFO] Processing tile {i+1}/{len(tiles)} (ID: {tile_id})...")
            
            # Clip bands to tile
            vv_tile = vv.clip(tile_geom)
            vh_tile = vh.clip(tile_geom)
            ratio_tile = vh_vv_ratio.clip(tile_geom)
            
            # Export VV tile
            vv_tile_path = tile_dir_vv / f'vv_tile_{tile_id}.tif'
            try:
                url = vv_tile.getDownloadURL({
                    'region': tile_geom,
                    'scale': export_scale,
                    'format': 'GEO_TIFF',
                    'crs': 'EPSG:4326'
                })
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                with open(vv_tile_path, 'wb') as f:
                    f.write(response.content)
                vv_tile_paths.append(vv_tile_path)
                logger.info(f"[SUCCESS] VV tile {tile_id} downloaded ({vv_tile_path.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                logger.error(f"[ERROR] VV tile {tile_id} export failed: {e}")
                raise PipelineError(f"Sentinel-1 export failed for tile {tile_id}: {e}")
            
            # Export VH tile
            vh_tile_path = tile_dir_vh / f'vh_tile_{tile_id}.tif'
            try:
                url = vh_tile.getDownloadURL({
                    'region': tile_geom,
                    'scale': export_scale,
                    'format': 'GEO_TIFF',
                    'crs': 'EPSG:4326'
                })
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                with open(vh_tile_path, 'wb') as f:
                    f.write(response.content)
                vh_tile_paths.append(vh_tile_path)
                logger.info(f"[SUCCESS] VH tile {tile_id} downloaded ({vh_tile_path.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                logger.error(f"[ERROR] VH tile {tile_id} export failed: {e}")
                raise PipelineError(f"Sentinel-1 export failed for tile {tile_id}: {e}")
            
            # Export VH/VV ratio tile
            ratio_tile_path = tile_dir_ratio / f'ratio_tile_{tile_id}.tif'
            try:
                url = ratio_tile.getDownloadURL({
                    'region': tile_geom,
                    'scale': export_scale,
                    'format': 'GEO_TIFF',
                    'crs': 'EPSG:4326'
                })
                response = requests.get(url, timeout=120)
                response.raise_for_status()
                with open(ratio_tile_path, 'wb') as f:
                    f.write(response.content)
                ratio_tile_paths.append(ratio_tile_path)
                logger.info(f"[SUCCESS] Ratio tile {tile_id} downloaded ({ratio_tile_path.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                logger.error(f"[ERROR] Ratio tile {tile_id} export failed: {e}")
                raise PipelineError(f"Sentinel-1 export failed for tile {tile_id}: {e}")
            
            time.sleep(0.5)  # Rate limiting
        
        logger.info(f"[SUCCESS] All {len(tiles)} tiles exported successfully")
        
        # MANDATORY: Mosaic tiles into final products
        logger.info("[INFO] Mosaicking tiles into final products...")
        
        # Mosaic VV
        vv_path = output_dir / 'vv_mean.tif'
        src_files_vv = [rasterio.open(p) for p in vv_tile_paths]
        mosaic_vv, out_trans_vv = merge(src_files_vv)
        out_meta_vv = src_files_vv[0].meta.copy()
        out_meta_vv.update({
            'height': mosaic_vv.shape[1],
            'width': mosaic_vv.shape[2],
            'transform': out_trans_vv
        })
        with rasterio.open(vv_path, 'w', **out_meta_vv) as dest:
            dest.write(mosaic_vv)
        for src in src_files_vv:
            src.close()
        logger.info(f"[SUCCESS] VV mosaicked: {vv_path}")
        
        # Mosaic VH
        vh_path = output_dir / 'vh_mean.tif'
        src_files_vh = [rasterio.open(p) for p in vh_tile_paths]
        mosaic_vh, out_trans_vh = merge(src_files_vh)
        out_meta_vh = src_files_vh[0].meta.copy()
        out_meta_vh.update({
            'height': mosaic_vh.shape[1],
            'width': mosaic_vh.shape[2],
            'transform': out_trans_vh
        })
        with rasterio.open(vh_path, 'w', **out_meta_vh) as dest:
            dest.write(mosaic_vh)
        for src in src_files_vh:
            src.close()
        logger.info(f"[SUCCESS] VH mosaicked: {vh_path}")
        
        # Mosaic VH/VV ratio
        ratio_path = output_dir / 'vh_vv_ratio.tif'
        src_files_ratio = [rasterio.open(p) for p in ratio_tile_paths]
        mosaic_ratio, out_trans_ratio = merge(src_files_ratio)
        out_meta_ratio = src_files_ratio[0].meta.copy()
        out_meta_ratio.update({
            'height': mosaic_ratio.shape[1],
            'width': mosaic_ratio.shape[2],
            'transform': out_trans_ratio
        })
        with rasterio.open(ratio_path, 'w', **out_meta_ratio) as dest:
            dest.write(mosaic_ratio)
        for src in src_files_ratio:
            src.close()
        logger.info(f"[SUCCESS] VH/VV ratio mosaicked: {ratio_path}")
        
        # MANDATORY: Validate final exports
        logger.info("[INFO] Validating Sentinel-1 exports...")
        
        validation_passed = True
        missing_files = []
        
        if not vv_path.exists():
            missing_files.append('vv_mean.tif')
            validation_passed = False
        if not vh_path.exists():
            missing_files.append('vh_mean.tif')
            validation_passed = False
        if not ratio_path.exists():
            missing_files.append('vh_vv_ratio.tif')
            validation_passed = False
        
        if not validation_passed:
            reason = f"Missing files: {', '.join(missing_files)}"
            status.set_status('sentinel1', 'FAILED', reason)
            logger.error(f"[FAILED] Sentinel-1 export: {reason}")
            raise PipelineError(f"Sentinel-1 export validation failed: {reason}")
        
        # Check file sizes (must be > 0)
        if vv_path.stat().st_size == 0 or vh_path.stat().st_size == 0 or ratio_path.stat().st_size == 0:
            reason = "One or more files are empty (0 bytes)"
            status.set_status('sentinel1', 'FAILED', reason)
            logger.error(f"[FAILED] Sentinel-1 export: {reason}")
            raise PipelineError(f"Sentinel-1 export validation failed: {reason}")
        
        # Mark as SUCCESS
        status.set_status('sentinel1', 'SUCCESS')
        logger.info(f"[SUCCESS] Sentinel-1 exported: VV ({vv_path.stat().st_size / 1024:.1f} KB), VH ({vh_path.stat().st_size / 1024:.1f} KB), Ratio ({ratio_path.stat().st_size / 1024:.1f} KB)")
        logger.info("[SUCCESS] Sentinel-1 stage complete")
        logger.info("=" * 80)
        
        metadata = {
            'status': 'success',
            'usage_scope': 'STRUCTURAL_CONTEXT',
            'warning': 'Used for structure/disturbance context - NOT for direct biomass estimation',
            'outputs': {
                'vv': str(vv_path),
                'vh': str(vh_path),
                'vh_vv_ratio': str(ratio_path)
            },
            'year': year,
            'image_count': image_count,
            'files_exported': True
        }
        
        logger.info("WARNING: Sentinel-1 backscatter is for disturbance detection only")
        logger.info("WARNING: Use GEDI L4A for biomass estimation")
        logger.info("[DONE] Sentinel-1 processing complete")
        logger.info("=" * 80)
        
        return metadata
        
    except Exception as e:
        status.set_status('sentinel1', 'FAILED', str(e))
        logger.error(f"[FAILED] Sentinel-1 fetch: {str(e)}")
        raise PipelineError(f"Sentinel-1 fetch failed: {str(e)}")


def fetch_forest_mask(
    aoi: gpd.GeoDataFrame,
    output_dir: Path,
    status: PipelineStatus
) -> Tuple[Path, float]:
    """
    Step 4: Fetch NLCD forest mask.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        output_dir: Output directory (outputs/masks/)
        status: Pipeline status tracker
        
    Returns:
        Tuple of (mask_path, forest_area_ha)
    """
    logger.info("=" * 80)
    logger.info("[START] STEP 4: NLCD FOREST MASK")
    logger.info("=" * 80)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    mask_path = output_dir / 'forest_mask.tif'
    
    # CHECK: Skip download if file already exists
    if mask_path.exists() and mask_path.stat().st_size > 0:
        logger.info(f"[INFO] Forest mask already exists: {mask_path} ({mask_path.stat().st_size / 1024:.1f} KB)")
        
        # Calculate area from existing mask
        import rasterio
        with rasterio.open(mask_path) as src:
            mask_data = src.read(1)
            pixel_area_m2 = abs(src.transform[0] * src.transform[4])
            forest_pixels = (mask_data == 1).sum()
            forest_area_m2 = forest_pixels * pixel_area_m2
            forest_area_ha = forest_area_m2 / 10000
        
        status.set_status('forest_mask', 'SKIPPED')
        logger.info(f"[SKIPPED] Forest mask - using existing file")
        logger.info(f"[INFO] Forest area: {forest_area_ha:.2f} ha")
        logger.info("=" * 80)
        
        return mask_path, forest_area_ha
    
    try:
        # Initialize Earth Engine (must be authenticated beforehand)
        ee.Initialize(project='heroic-bliss-472519-f7')
        
        # Convert AOI to EE geometry
        bounds = aoi.total_bounds
        ee_aoi = ee.Geometry.Rectangle([
            float(bounds[0]), float(bounds[1]),
            float(bounds[2]), float(bounds[3])
        ])
        
        # Fetch NLCD 2021
        nlcd = ee.Image('USGS/NLCD_RELEASES/2021_REL/NLCD/2021').select('landcover')
        
        # Forest classes: 41 (Deciduous), 42 (Evergreen), 43 (Mixed)
        forest_mask = nlcd.eq(41).Or(nlcd.eq(42)).Or(nlcd.eq(43))
        forest_mask = forest_mask.clip(ee_aoi)
        
        # Calculate forest area
        area_image = forest_mask.multiply(ee.Image.pixelArea())
        stats = area_image.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=ee_aoi,
            scale=30,
            maxPixels=1e9
        )
        
        forest_area_m2 = stats.get('landcover').getInfo()
        forest_area_ha = forest_area_m2 / 10000
        
        logger.info(f"[INFO] Forest area: {forest_area_ha:.2f} ha")
        
        # Download forest mask to disk
        logger.info("[INFO] Downloading forest mask...")
        download_url = forest_mask.byte().getDownloadURL({
            'region': ee_aoi,
            'scale': 30,
            'format': 'GEO_TIFF',
            'crs': 'EPSG:4326'
        })
        
        import requests
        response = requests.get(download_url)
        response.raise_for_status()
        
        with open(mask_path, 'wb') as f:
            f.write(response.content)
        
        status.set_status('forest_mask', 'SUCCESS')
        logger.info(f"[SUCCESS] Forest mask saved: {mask_path}")
        logger.info("=" * 80)
        
        return mask_path, forest_area_ha
        
    except Exception as e:
        status.set_status('forest_mask', 'FAILED', str(e))
        logger.error(f"[FAILED] Forest mask fetch: {str(e)}")
        raise PipelineError(f"Forest mask fetch failed: {str(e)}")


def fetch_gedi_l4a(
    aoi: gpd.GeoDataFrame,
    year: int,
    output_dir: Path
) -> Dict:
    """
    Step 5: Fetch GEDI L4A authoritative biomass.
    
    NOTE: GEDI module is OPTIONAL. If not available, returns no_data status.
    Pipeline will automatically fall back to LiDAR processing.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        year: Year to fetch data for
        output_dir: Output directory (outputs/gedi/)
        
    Returns:
        GEDI result dict with biomass statistics or no_data status
    """
    logger.info("=" * 80)
    logger.info("[STEP 5] GEDI L4A AUTHORITATIVE BIOMASS (OPTIONAL)")
    logger.info("=" * 80)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Import GEDI fetch function (optional module)
        try:
            from gedi_l4a_authoritative import fetch_gedi_l4a_biomass
        except ImportError:
            logger.warning("[WARNING] GEDI module not available - skipping GEDI fetch")
            logger.info("[INFO] Will use LiDAR-only processing")
            return {
                'status': 'no_data',
                'footprint_count': 0,
                'reason': 'GEDI module not available',
                'suggestion': 'Using LiDAR-only processing'
            }
        
        # Get AOI bounds
        bounds = aoi.total_bounds
        bbox = (float(bounds[0]), float(bounds[1]), 
                float(bounds[2]), float(bounds[3]))
        
        # Fetch GEDI L4A
        gedi_result = fetch_gedi_l4a_biomass(
            aoi_bbox=bbox,
            date_start=f'{year}-01-01',
            date_end=f'{year}-12-31',
            output_path=output_dir / 'gedi_l4a_data.geojson',
            quality_filter=True
        )
        
        logger.info(f"GEDI footprints: {gedi_result.get('footprint_count', 0)}")
        logger.info(f"Mean AGB: {gedi_result.get('mean_agb_Mg_ha', 0):.2f} Mg/ha")
        logger.info(f"Std AGB: {gedi_result.get('std_agb_Mg_ha', 0):.2f} Mg/ha")
        logger.info("SUCCESS: GEDI L4A is authoritative (FIA-calibrated)")
        logger.info("[DONE] GEDI L4A fetch complete")
        logger.info("=" * 80)
        
        return gedi_result
        
    except Exception as e:
        raise PipelineError(f"GEDI L4A fetch failed: {str(e)}")


def calculate_carbon_stock(
    gedi_result: Dict,
    forest_area_ha: float,
    output_dir: Path
) -> Dict:
    """
    Step 6: Calculate area-based carbon stock.
    
    Uses GEDI L4A authoritative biomass and IPCC conversion factors.
    
    Args:
        gedi_result: GEDI L4A result from fetch_gedi_l4a()
        forest_area_ha: Forest area in hectares
        output_dir: Output directory (outputs/results/)
        
    Returns:
        Carbon stock result dict
    """
    logger.info("=" * 80)
    logger.info("[STEP 6] AREA-BASED CARBON STOCK ESTIMATION")
    logger.info("=" * 80)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Import carbon calculation function
        sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
        from area_based_carbon import calculate_area_based_carbon
        
        # Extract GEDI statistics
        mean_agb_Mg_ha = gedi_result.get('mean_agb_Mg_ha', 0)
        agb_uncertainty_Mg_ha = gedi_result.get('std_agb_Mg_ha', 0)
        
        # Calculate area-based carbon
        result = calculate_area_based_carbon(
            forest_area_ha=forest_area_ha,
            mean_agb_Mg_ha=mean_agb_Mg_ha,
            agb_uncertainty_Mg_ha=agb_uncertainty_Mg_ha
        )
        
        # Convert to dict
        carbon_result = result.to_dict()
        
        # Save results
        output_path = output_dir / 'carbon_stock_summary.json'
        with open(output_path, 'w') as f:
            json.dump(carbon_result, f, indent=2)
        
        logger.info(f"Forest area: {forest_area_ha:.2f} ha")
        logger.info(f"Mean AGB: {mean_agb_Mg_ha:.2f} Mg/ha")
        logger.info(f"Total AGB: {carbon_result['total_agb_Mg']:.2f} Mg")
        logger.info(f"Total Carbon: {carbon_result['total_carbon_Mg']:.2f} Mg C")
        logger.info(f"Total CO2e: {carbon_result['total_co2e_Mg']:.2f} Mg CO2e")
        logger.info(f"Uncertainty: ±{carbon_result['uncertainty_percent']:.1f}%")
        logger.info(f"Results saved to: {output_path}")
        logger.info("[DONE] Carbon stock calculation complete")
        logger.info("=" * 80)
        
        return carbon_result
        
    except Exception as e:
        raise PipelineError(f"Carbon calculation failed: {str(e)}")


def compute_aoi_hash(aoi: gpd.GeoDataFrame) -> str:
    """
    Compute hash of AOI geometry for change detection.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        
    Returns:
        Hash string of AOI bounds and CRS
    """
    import hashlib
    bounds = aoi.total_bounds
    crs = str(aoi.crs)
    hash_input = f"{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]},{crs}"
    return hashlib.md5(hash_input.encode()).hexdigest()


def cleanup_old_data(output_dir: Path):
    """
    Delete cached satellite data when AOI changes.
    
    Removes:
      - sentinel1/, sentinel2/, gedi/, masks/
    
    Preserves:
      - results/ (for comparison)
    """
    import shutil
    
    dirs_to_clean = ['sentinel1', 'sentinel2', 'gedi', 'masks']
    
    for dir_name in dirs_to_clean:
        dir_path = output_dir / dir_name
        if dir_path.exists():
            shutil.rmtree(dir_path)
            logger.info(f"Cleaned old data: {dir_name}/")


def auto_detect_lidar_crs(bounds: Tuple[float, float, float, float], 
                          aoi_geometry: gpd.GeoDataFrame = None) -> Optional[str]:
    """
    Automatically detect CRS from LAZ coordinate bounds.
    
    Logic:
    1. If coordinates are small (< 180 for x, < 90 for y) → Geographic (EPSG:4326)
    2. If coordinates are large → Projected (likely UTM)
       - Estimate UTM zone from coordinate center or AOI geometry
       - Return appropriate EPSG code (WGS84 UTM)
    
    Args:
        bounds: (minx, miny, maxx, maxy) from LAZ file
        aoi_geometry: Optional AOI GeoDataFrame for better lon/lat estimation
        
    Returns:
        EPSG code string (e.g., "EPSG:32610") or None if detection fails
    """
    minx, miny, maxx, maxy = bounds
    
    # Calculate center point
    center_x = (minx + maxx) / 2.0
    center_y = (miny + maxy) / 2.0
    
    logger.info("[AUTO-DETECT] Analyzing coordinate bounds for CRS detection...")
    logger.info(f"[AUTO-DETECT] Bounds: X=[{minx:.2f}, {maxx:.2f}], Y=[{miny:.2f}, {maxy:.2f}]")
    logger.info(f"[AUTO-DETECT] Center: ({center_x:.2f}, {center_y:.2f})")
    
    # Decision 1: Geographic vs Projected based on coordinate magnitude
    if abs(center_x) < 180 and abs(center_y) < 90:
        # Likely geographic coordinates (lat/lon)
        logger.info("[AUTO-DETECT] Coordinates appear to be GEOGRAPHIC (lat/lon)")
        logger.info("[AUTO-DETECT] Magnitude: |X| < 180, |Y| < 90")
        logger.info("[AUTO-DETECT] Assigned CRS: EPSG:4326 (WGS84)")
        return "EPSG:4326"
    
    # Decision 2: Projected coordinates (meters) - estimate UTM zone
    logger.info("[AUTO-DETECT] Coordinates appear to be PROJECTED (meters)")
    logger.info(f"[AUTO-DETECT] Magnitude: X={abs(center_x):.0f}, Y={abs(center_y):.0f}")
    
    # Estimate longitude for UTM zone calculation
    longitude = None
    latitude = None
    
    # Method 1: Use AOI geometry if provided (most reliable)
    if aoi_geometry is not None and len(aoi_geometry) > 0:
        try:
            # Get AOI centroid in WGS84
            aoi_wgs84 = aoi_geometry.to_crs('EPSG:4326')
            centroid = aoi_wgs84.geometry.centroid.iloc[0]
            longitude = centroid.x
            latitude = centroid.y
            logger.info(f"[AUTO-DETECT] Using AOI centroid: ({longitude:.4f}°, {latitude:.4f}°)")
        except Exception as e:
            logger.warning(f"[AUTO-DETECT] Could not use AOI centroid: {e}")
    
    # Method 2: Estimate from UTM easting (less reliable, but fallback)
    if longitude is None:
        # UTM easting range: 166,000 to 834,000 within each zone
        # Zone center easting ≈ 500,000
        # Rough estimate: zone_number ≈ (center_x - 500000) / 1000000 + estimated_zone
        
        # This is a rough heuristic - try to guess from northern hemisphere typical ranges
        # US West: UTM zones 10-11 (lon: -126° to -114°)
        # Easting offset from zone center
        if 200000 <= center_x <= 800000:
            # Assume UTM Zone 10 (California typical) as fallback
            estimated_zone = 10
            logger.warning("[AUTO-DETECT] Cannot determine longitude - assuming UTM Zone 10 (US West Coast)")
            logger.warning("[AUTO-DETECT] This is a GUESS. Provide --lidar-crs for accuracy.")
            longitude = -122.0  # Central California
            latitude = 37.0  # Approximation
        else:
            logger.error("[AUTO-DETECT] Cannot estimate UTM zone from coordinates")
            logger.error("[AUTO-DETECT] Easting outside typical UTM range (200k-800k)")
            return None
    
    # Calculate UTM zone from longitude
    utm_zone = int((longitude + 180) / 6) + 1
    
    # Validate zone range
    if not (1 <= utm_zone <= 60):
        logger.error(f"[AUTO-DETECT] Invalid UTM zone computed: {utm_zone}")
        logger.error("[AUTO-DETECT] Zone must be between 1 and 60")
        return None
    
    # Determine hemisphere
    if latitude >= 0:
        hemisphere = 'north'
        epsg_code = 32600 + utm_zone  # WGS84 / UTM north
    else:
        hemisphere = 'south'
        epsg_code = 32700 + utm_zone  # WGS84 / UTM south
    
    # Validate EPSG code range
    if not ((32601 <= epsg_code <= 32660) or (32701 <= epsg_code <= 32760)):
        logger.error(f"[AUTO-DETECT] Invalid EPSG code computed: {epsg_code}")
        return None
    
    detected_crs = f"EPSG:{epsg_code}"
    
    logger.info(f"[AUTO-DETECT] Estimated longitude: {longitude:.4f}°")
    logger.info(f"[AUTO-DETECT] Estimated latitude: {latitude:.4f}°")
    logger.info(f"[AUTO-DETECT] Computed UTM Zone: {utm_zone}{hemisphere[0].upper()}")
    logger.info(f"[AUTO-DETECT] Assigned CRS: {detected_crs} (WGS84 / UTM Zone {utm_zone}{hemisphere[0].upper()})")
    logger.info(f"[AUTO-DETECT] ⚠️  Auto-detection is a BEST GUESS. Verify or provide --lidar-crs if uncertain.")
    
    return detected_crs


def create_aoi_from_laz_tiles(laz_tiles_dir: Path, override_crs: str = None) -> Tuple[gpd.GeoDataFrame, Dict]:
    """
    Auto-generate AOI GeoDataFrame from LAZ tile extents.
    
    Args:
        laz_tiles_dir: Directory containing .laz/.las files
        override_crs: Optional CRS string (e.g., 'EPSG:32610') to override header CRS
        
    Returns:
        Tuple of (GeoDataFrame with AOI geometry, metadata dict)
    """
    laz_files = list(laz_tiles_dir.glob('*.laz')) + list(laz_tiles_dir.glob('*.las'))
    
    if not laz_files:
        raise ValueError(f"No .laz/.las files found in {laz_tiles_dir}")
    
    logger.info(f"[INFO] Auto-generating AOI from {len(laz_files)} LAZ tile(s)...")
    
    # Read first tile to get CRS and bounds
    first_tile = laspy.read(laz_files[0])
    crs = first_tile.header.parse_crs()
    mins = first_tile.header.mins
    maxs = first_tile.header.maxs
    
    # Get bounds for auto-detection
    laz_bounds = (float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1]))
    
    # Apply override CRS if provided
    if override_crs:
        logger.info(f"[INFO] Using override CRS: {override_crs}")
        crs = CRS.from_string(override_crs)
    elif crs is None:
        # Auto-detect CRS from coordinate bounds
        logger.warning("[WARN] No CRS found in LAZ header")
        logger.info("[INFO] Attempting automatic CRS detection from coordinate bounds...")
        
        detected_crs_str = auto_detect_lidar_crs(laz_bounds, None)
        
        if detected_crs_str:
            crs = CRS.from_string(detected_crs_str)
            logger.info(f"[SUCCESS] Auto-detected CRS: {detected_crs_str}")
        else:
            logger.error("[ERROR] Automatic CRS detection failed")
            logger.error("[ERROR] Unable to determine CRS from coordinate bounds")
            logger.error("[ERROR] Solution: Provide CRS using --lidar-crs EPSG:XXXX argument")
            logger.error("[ERROR] Example: --lidar-crs EPSG:32610 (for UTM Zone 10N)")
            logger.error("[ERROR] Example: --lidar-crs EPSG:6339 (for NAD83 California Albers)")
            logger.error("[ERROR] Find your CRS at: https://epsg.io/")
            raise ValueError("Cannot determine CRS from LAZ tiles. Use --lidar-crs to specify.")
    
    # Collect bounds from all tiles
    all_bounds = []
    for laz_file in laz_files:
        las = laspy.read(laz_file)
        mins = las.header.mins
        maxs = las.header.maxs
        all_bounds.append((mins[0], mins[1], maxs[0], maxs[1]))  # (minx, miny, maxx, maxy)
    
    # Create unified bounding box
    min_x = min(b[0] for b in all_bounds)
    min_y = min(b[1] for b in all_bounds)
    max_x = max(b[2] for b in all_bounds)
    max_y = max(b[3] for b in all_bounds)
    
    # Create GeoDataFrame
    aoi_geom = box(min_x, min_y, max_x, max_y)
    aoi = gpd.GeoDataFrame([{'geometry': aoi_geom}], crs=crs)
    
    # Calculate area in hectares
    aoi_wgs84 = aoi.to_crs('EPSG:4326')
    area_ha = aoi_wgs84.geometry.to_crs('EPSG:6933').area.sum() / 10000.0  # Equal Area projection
    
    metadata = {
        'source': 'auto_generated_from_laz_tiles',
        'num_tiles': len(laz_files),
        'crs': str(crs),
        'bounds': {'minx': float(min_x), 'miny': float(min_y), 'maxx': float(max_x), 'maxy': float(max_y)},
        'area_ha': float(area_ha),
        'valid': True,
    }
    
    logger.info(f"[INFO] Auto-generated AOI: {area_ha:.2f} ha, CRS: {crs}")
    logger.info(f"[INFO] Bounds: ({min_x:.2f}, {min_y:.2f}) to ({max_x:.2f}, {max_y:.2f})")
    
    return aoi, metadata


def run_complete_pipeline(
    aoi_path: Optional[Path],
    year: int,
    output_dir: Path,
    laz_tiles_dir: Path = None,
    lidar_resolution_m: float = 5.0,
    allom_a: float = None,
    allom_b: float = None,
    lidar_crs: str = None,
) -> Dict:
    """
    Execute complete pipeline from AOI to carbon stock results.
    
    Args:
        aoi_path: Path to AOI GeoJSON (optional if laz_tiles_dir provided)
        year: Year to process
        output_dir: Base output directory
        laz_tiles_dir: Optional local LAZ/LAS tiles folder (bypasses remote LiDAR download)
        lidar_resolution_m: LiDAR raster resolution (m). Values < 2.0 are clamped to 5.0.
        allom_a/allom_b: Optional allometry coefficients for AGB=a*(H^b) using P95 height.
        lidar_crs: Optional CRS override for LiDAR tiles if header CRS is missing.
        
    Returns:
        Pipeline result dict with all outputs
    """
    start_time = datetime.now()
    
    # Initialize pipeline status tracker
    status = PipelineStatus()
    
    logger.info("\n" + "=" * 80)
    logger.info("FOREST CARBON STOCK ESTIMATION PIPELINE")
    logger.info("=" * 80)
    logger.info(f"AOI: {aoi_path if aoi_path else 'Auto-generated from LAZ tiles'}")
    logger.info(f"Year: {year}")
    logger.info(f"Output: {output_dir}")
    if laz_tiles_dir:
        logger.info(f"Local LAZ tiles: {laz_tiles_dir}")
        logger.info(f"LiDAR resolution: {lidar_resolution_m} m")
    logger.info("=" * 80 + "\n")
    
    pipeline_result = {
        'status': 'running',
        'start_time': start_time.isoformat(),
        'aoi_path': str(aoi_path) if aoi_path else 'auto_generated',
        'year': year,
        'output_directory': str(output_dir),
        'local_laz_tiles_dir': str(laz_tiles_dir) if laz_tiles_dir else None,
        'lidar_resolution_m': float(lidar_resolution_m),
        'allom_a': float(allom_a) if allom_a is not None else None,
        'allom_b': float(allom_b) if allom_b is not None else None,
    }
    
    try:
        # Step 1: Validate AOI (or auto-generate from LAZ tiles)
        if aoi_path:
            aoi, aoi_metadata = validate_aoi(aoi_path, status)
        elif laz_tiles_dir:
            aoi, aoi_metadata = create_aoi_from_laz_tiles(laz_tiles_dir, override_crs=lidar_crs)
            status.set_status('aoi_validation', 'SUCCESS')
        else:
            raise PipelineError("Either --aoi or --laz-tiles must be provided")
        
        pipeline_result['aoi_metadata'] = aoi_metadata
        forest_area_ha = aoi_metadata.get('area_ha', 0.0)
        
        # AOI change detection - auto cleanup old data
        output_dir.mkdir(parents=True, exist_ok=True)
        aoi_hash_file = output_dir / '.aoi_hash'
        current_hash = compute_aoi_hash(aoi)
        
        if aoi_hash_file.exists():
            stored_hash = aoi_hash_file.read_text().strip()
            if stored_hash != current_hash:
                logger.info("AOI changed - cleaning old data automatically")
                cleanup_old_data(output_dir)
                aoi_hash_file.write_text(current_hash)
        else:
            logger.info("First run for this AOI - storing hash")
            aoi_hash_file.write_text(current_hash)
        
        # ============================================================
        # LIDAR-ONLY MODE: Skip Earth Engine, Sentinel, GEDI, NLCD
        # ============================================================
        if laz_tiles_dir:
            logger.info("\n" + "=" * 80)
            logger.info("LIDAR-ONLY MODE ACTIVATED")
            logger.info("=" * 80)
            logger.info("[INFO] Skipping: Sentinel-2, Sentinel-1, NLCD, GEDI")
            logger.info("[INFO] Forest mask will be derived from CHM")
            logger.info("=" * 80 + "\n")
            
            if not ALS_AVAILABLE:
                raise PipelineError("ALS module not available (required for local LAZ processing)")
            
            status.set_status('sentinel2', 'SKIPPED')
            status.set_status('sentinel1', 'SKIPPED')
            status.set_status('forest_mask', 'SKIPPED')
            status.set_status('gedi_biomass', 'SKIPPED')
            
            # Run complete LiDAR-only pipeline
            try:
                lidar_out = output_dir / 'lidar'
                local_result = local_laz_carbon_stock_pipeline(
                    laz_tiles_dir=Path(laz_tiles_dir),
                    aoi=aoi,
                    output_dir=lidar_out,
                    resolution_m=lidar_resolution_m,
                    allom_a=allom_a,
                    allom_b=allom_b,
                    override_crs=lidar_crs,
                )
                
                status.set_status('lidar', 'SUCCESS')
                status.set_status('carbon_stock', 'SUCCESS')
                
                # Store results
                pipeline_result['biomass_source'] = 'LOCAL_LAZ_LIDAR'
                pipeline_result['forest_mask_source'] = 'CHM_height_based'
                pipeline_result['forest_area_ha'] = local_result.get('forest_area_ha', 0.0)
                pipeline_result['mean_tree_height_m'] = local_result.get('mean_height_m', 0.0)
                pipeline_result['p95_tree_height_m'] = local_result.get('p95_height_m', 0.0)
                pipeline_result['canopy_cover_pct'] = local_result.get('canopy_cover_pct', 0.0)
                pipeline_result['mean_agb_Mg_ha'] = local_result.get('AGB_Mg_per_ha', 0.0)
                pipeline_result['carbon_MgC_per_ha'] = local_result.get('Carbon_MgC_per_ha', 0.0)
                pipeline_result['total_carbon_MgC'] = local_result.get('Total_Carbon_MgC', 0.0)
                pipeline_result['chm_path'] = local_result.get('chm_path')
                pipeline_result['forest_mask_path'] = local_result.get('forest_mask_path')
                pipeline_result['carbon_density_path'] = local_result.get('carbon_density_path')
                pipeline_result['resolution_m'] = local_result.get('resolution_m', lidar_resolution_m)
                
                # Save final results
                results_dir = output_dir / 'results'
                results_dir.mkdir(parents=True, exist_ok=True)
                
                carbon_summary = {
                    'status': 'success',
                    'biomass_source': 'LOCAL_LAZ_LIDAR',
                    'forest_mask_source': 'CHM_height_based',
                    'method': 'LiDAR-only (no NLCD/Earth Engine)',
                    'forest_area_ha': float(local_result.get('forest_area_ha', 0.0)),
                    'mean_tree_height_m': float(local_result.get('mean_height_m', 0.0)),
                    'p95_tree_height_m': float(local_result.get('p95_height_m', 0.0)),
                    'canopy_cover_pct': float(local_result.get('canopy_cover_pct', 0.0)),
                    'mean_agb_Mg_ha': float(local_result.get('AGB_Mg_per_ha', 0.0)),
                    'carbon_MgC_per_ha': float(local_result.get('Carbon_MgC_per_ha', 0.0)),
                    'total_carbon_MgC': float(local_result.get('Total_Carbon_MgC', 0.0)),
                    'carbon_density_raster': str(local_result.get('carbon_density_path')),
                    'chm_raster': str(local_result.get('chm_path')),
                    'forest_mask_raster': str(local_result.get('forest_mask_path')),
                    'resolution_m': float(local_result.get('resolution_m', lidar_resolution_m)),
                }
                
                summary_path = results_dir / 'carbon_stock_summary.json'
                with open(summary_path, 'w') as f:
                    json.dump(carbon_summary, f, indent=2)
                
                pipeline_result['status'] = 'success'
                pipeline_result['final_result'] = str(summary_path)
                
                # Success logging
                end_time = datetime.now()
                duration = (end_time - start_time).total_seconds()
                pipeline_result['end_time'] = end_time.isoformat()
                pipeline_result['duration_seconds'] = duration
                
                logger.info("\n" + "=" * 80)
                logger.info("PIPELINE COMPLETE (LIDAR-ONLY MODE)")
                logger.info("=" * 80)
                logger.info(f"Status: SUCCESS")
                logger.info(f"Biomass source: LOCAL_LAZ_LIDAR")
                logger.info(f"Forest mask: CHM-based (no NLCD)")
                logger.info(f"Duration: {duration:.1f} seconds")
                logger.info(f"Final results: {summary_path}")
                logger.info("=" * 80 + "\n")
                
                status.print_summary()
                
                # Save pipeline metadata
                metadata_path = output_dir / 'pipeline_metadata.json'
                with open(metadata_path, 'w') as f:
                    json.dump(pipeline_result, f, indent=2)
                
                return pipeline_result
                
            except Exception as e:
                raise PipelineError(f"Local LAZ LiDAR processing failed: {e}")
        
        # ============================================================
        # STANDARD MODE: Use Earth Engine, Sentinel, GEDI, NLCD
        # ============================================================
        logger.info("[INFO] Running STANDARD MODE (Earth Engine + GEDI)")
        
        # Step 2: Fetch Sentinel-2 (MANDATORY)
        s2_metadata = fetch_sentinel2_qa(
            aoi=aoi,
            year=year,
            output_dir=output_dir / 'sentinel2',
            status=status
        )
        pipeline_result['sentinel2_metadata'] = s2_metadata
        
        # Step 3: Fetch Sentinel-1 (MANDATORY)
        s1_metadata = fetch_sentinel1_disturbance(
            aoi=aoi,
            year=year,
            output_dir=output_dir / 'sentinel1',
            status=status
        )
        pipeline_result['sentinel1_metadata'] = s1_metadata
        
        # Step 4: Fetch forest mask
        mask_path, forest_area_ha = fetch_forest_mask(
            aoi=aoi,
            output_dir=output_dir / 'masks',
            status=status
        )
        pipeline_result['forest_mask_path'] = str(mask_path)
        pipeline_result['forest_area_ha'] = forest_area_ha
        
        # Step 5: Fetch GEDI L4A (skip if LAZ tiles provided)
        if laz_tiles_dir:
            logger.info("=" * 80)
            logger.info("[STEP 5] GEDI L4A - BYPASSED (using local LAZ tiles)")
            logger.info("=" * 80)
            gedi_result = {
                'status': 'no_data',
                'footprint_count': 0,
                'reason': 'Bypassed - using local LAZ tiles',
                'suggestion': 'LiDAR-only processing mode'
            }
        else:
            gedi_result = fetch_gedi_l4a(
                aoi=aoi,
                year=year,
                output_dir=output_dir / 'gedi'
            )
        pipeline_result['gedi_result'] = gedi_result
        
        # Check if GEDI data available
        if gedi_result.get('status') == 'no_data':
            logger.info("\n" + "=" * 80)
            logger.info("[INFO] GEDI not used (footprints = 0 or bypassed)")
            if laz_tiles_dir:
                logger.info("[INFO] Using LOCAL LAZ tiles for LiDAR processing")
            else:
                logger.info("[INFO] Attempting ALS fallback (USGS 3DEP)")
            logger.info("=" * 80 + "\n")

            # If user provided local LAZ/LAS tiles, run local LiDAR pipeline directly
            if laz_tiles_dir:
                if not ALS_AVAILABLE:
                    raise PipelineError("ALS module not available (required for local LAZ processing)")

                try:
                    lidar_out = output_dir / 'lidar'
                    local_result = local_laz_carbon_stock_pipeline(
                        laz_tiles_dir=Path(laz_tiles_dir),
                        aoi=aoi,
                        output_dir=lidar_out,
                        resolution_m=lidar_resolution_m,
                        allom_a=allom_a,
                        allom_b=allom_b,
                        override_crs=lidar_crs,
                    )

                    pipeline_result['biomass_source'] = 'LOCAL_LAZ_LIDAR'
                    pipeline_result['als_result'] = {
                        'status': 'success',
                        'mean_agb_Mg_ha': float(local_result.get('AGB_Mg_per_ha', 0.0)),
                        'mean_tree_height_m': float(local_result.get('mean_height_m', 0.0)),
                        'p95_tree_height_m': float(local_result.get('p95_height_m', 0.0)),
                        'canopy_cover_pct': float(local_result.get('canopy_cover_pct', 0.0)),
                        'carbon_MgC_per_ha': float(local_result.get('Carbon_MgC_per_ha', 0.0)),
                        'total_carbon_MgC': float(local_result.get('Total_Carbon_MgC', 0.0)),
                        'chm_path': local_result.get('chm_path'),
                        'carbon_density_path': local_result.get('carbon_density_path'),
                        'resolution_m': float(local_result.get('resolution_m', lidar_resolution_m)),
                    }

                    # Use LiDAR result as biomass source for downstream carbon calculation
                    gedi_result = {
                        'status': 'success',
                        'mean_agb_Mg_ha': float(local_result.get('AGB_Mg_per_ha', 0.0)),
                        'std_agb_Mg_ha': 0.0,
                        'biomass_source': 'LOCAL_LAZ_LIDAR',
                    }

                except Exception as e:
                    raise PipelineError(f"Local LAZ LiDAR processing failed: {e}")
            
            # Otherwise, attempt ALS fallback download/query path
            
            # Attempt ALS fallback
            if ALS_AVAILABLE:
                als_result = als_fallback_pipeline(
                    aoi=aoi,
                    forest_mask_path=mask_path,
                    output_dir=output_dir / 'lidar',
                    analysis_year=year
                )
                
                # If ALS succeeds, use it for carbon calculation
                if als_result.get('status') == 'success':
                    logger.info("[SUCCESS] ALS biomass estimation complete")
                    pipeline_result['biomass_source'] = 'USGS_3DEP_LiDAR'
                    pipeline_result['als_result'] = als_result
                    
                    # Use ALS result for carbon calculation
                    gedi_result = als_result
                elif als_result.get('status') == 'awaiting_lidar_tiles':
                    # LiDAR exists but tiles not yet provided
                    logger.info("=" * 80)
                    logger.info("PIPELINE PAUSED - AWAITING LIDAR TILES")
                    logger.info("=" * 80)
                    logger.info(f"Status: AWAITING_LIDAR_TILES")
                    logger.info(f"Project: {als_result.get('project', 'unknown')}")
                    logger.info(f"Reason: {als_result.get('reason', '')}")
                    logger.info(f"Action: {als_result.get('required_action', '')}")
                    logger.info(f"Location: {als_result.get('tile_location', '')}")
                    logger.info("=" * 80)
                    
                    end_time = datetime.now()
                    duration = (end_time - start_time).total_seconds()
                    
                    pipeline_result['status'] = 'awaiting_lidar_tiles'
                    pipeline_result['end_time'] = end_time.isoformat()
                    pipeline_result['duration_seconds'] = duration
                    pipeline_result['message'] = als_result.get('reason', '')
                    pipeline_result['suggestion'] = als_result.get('suggestion', '')
                    pipeline_result['output_directory'] = str(output_dir)
                    pipeline_result['final_result'] = 'LiDAR project detected - awaiting tile provision'
                    pipeline_result['biomass_source'] = 'pending'
                    pipeline_result['als_attempted'] = True
                    pipeline_result['als_result'] = als_result
                    
                    # Save awaiting result
                    awaiting_path = output_dir / 'results'
                    awaiting_path.mkdir(parents=True, exist_ok=True)
                    result_file = awaiting_path / 'carbon_stock_summary.json'
                    with open(result_file, 'w') as f:
                        json.dump(pipeline_result, f, indent=2, default=str)
                    
                    logger.info("\n" + "=" * 80)
                    logger.info("PIPELINE SUMMARY")
                    logger.info("=" * 80)
                    logger.info(f"Status: awaiting_lidar_tiles")
                    logger.info(f"Output directory: {output_dir}")
                    logger.info(f"Project: {als_result.get('project', 'unknown')}")
                    logger.info(f"Tile location: {als_result.get('tile_location', '')}")
                    logger.info(f"Next steps: {als_result.get('required_action', '')}")
                    logger.info("=" * 80 + "\n")
                    
                    return pipeline_result
                else:
                    # Both GEDI and ALS failed
                    logger.error("[ERROR] Both GEDI and ALS unavailable")
                    
                    end_time = datetime.now()
                    duration = (end_time - start_time).total_seconds()
                    
                    pipeline_result['status'] = 'no_data'
                    pipeline_result['end_time'] = end_time.isoformat()
                    pipeline_result['duration_seconds'] = duration
                    pipeline_result['message'] = als_result.get('reason', 'No GEDI footprints and no USGS 3DEP LiDAR coverage')
                    pipeline_result['suggestion'] = als_result.get('suggestion', '')
                    pipeline_result['output_directory'] = str(output_dir)
                    pipeline_result['final_result'] = 'N/A - No biomass data sources available'
                    pipeline_result['biomass_source'] = 'none'
                    pipeline_result['als_attempted'] = True
                    pipeline_result['als_result'] = als_result
                    
                    # Save no-data result
                    no_data_path = output_dir / 'results'
                    no_data_path.mkdir(parents=True, exist_ok=True)
                    result_file = no_data_path / 'carbon_stock_summary.json'
                    with open(result_file, 'w') as f:
                        json.dump(pipeline_result, f, indent=2)
                    
                    logger.info("\n" + "=" * 80)
                    logger.info("PIPELINE COMPLETE - NO DATA")
                    logger.info("=" * 80)
                    logger.info(f"Status: NO_DATA")
                    logger.info(f"Reason: {pipeline_result['message']}")
                    logger.info(f"Suggestion: {pipeline_result['suggestion']}")
                    logger.info("=" * 80 + "\n")
                    
                    return pipeline_result
            else:
                # ALS module not available
                end_time = datetime.now()
                duration = (end_time - start_time).total_seconds()
                
                pipeline_result['status'] = 'no_data'
                pipeline_result['end_time'] = end_time.isoformat()
                pipeline_result['duration_seconds'] = duration
                pipeline_result['message'] = gedi_result.get('reason', 'No GEDI data available')
                pipeline_result['suggestion'] = gedi_result.get('suggestion', '')
                pipeline_result['output_directory'] = str(output_dir)
                pipeline_result['final_result'] = 'N/A - No GEDI data found'
                pipeline_result['biomass_source'] = 'none'
                pipeline_result['als_attempted'] = False
                
                # Save no-data result
                no_data_path = output_dir / 'results'
                no_data_path.mkdir(parents=True, exist_ok=True)
                result_file = no_data_path / 'carbon_stock_summary.json'
                with open(result_file, 'w') as f:
                    json.dump(pipeline_result, f, indent=2)
                
                logger.info("\n" + "=" * 80)
                logger.info("PIPELINE COMPLETE - NO DATA")
                logger.info("=" * 80)
                logger.info(f"Status: NO_DATA")
                logger.info(f"Reason: {pipeline_result['message']}")
                logger.info(f"Suggestion: {pipeline_result['suggestion']}")
                logger.info("=" * 80 + "\n")
                
                return pipeline_result
        
        # If we reach here, either GEDI succeeded or ALS fallback succeeded
        # Tag biomass source
        if 'biomass_source' not in pipeline_result:
            pipeline_result['biomass_source'] = 'GEDI_L4A'
        
        # Step 6: Calculate carbon stock
        carbon_result = calculate_carbon_stock(
            gedi_result=gedi_result,
            forest_area_ha=forest_area_ha,
            output_dir=output_dir / 'results'
        )
        pipeline_result['carbon_result'] = carbon_result
        
        # Add biomass source metadata to carbon result
        if pipeline_result['biomass_source'] == 'USGS_3DEP_LiDAR':
            carbon_result['biomass_source'] = 'USGS_3DEP_LiDAR'
            carbon_result['method'] = 'height-based FIA allometry'
            carbon_result['reason'] = 'GEDI L4A footprints = 0'
        else:
            carbon_result['biomass_source'] = 'GEDI_L4A'
            carbon_result['method'] = 'FIA-calibrated footprint biomass'
        
        # Success
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        pipeline_result['status'] = 'success'
        pipeline_result['end_time'] = end_time.isoformat()
        pipeline_result['duration_seconds'] = duration
        pipeline_result['output_directory'] = str(output_dir)
        pipeline_result['final_result'] = str(output_dir / 'results' / 'carbon_stock_summary.json')
        
        logger.info("\n" + "=" * 80)
        logger.info("PIPELINE COMPLETE")
        logger.info("=" * 80)
        logger.info(f"Status: SUCCESS")
        logger.info(f"Biomass source: {pipeline_result['biomass_source']}")
        logger.info(f"Duration: {duration:.1f} seconds")
        logger.info(f"Final results: {pipeline_result['final_result']}")
        logger.info("=" * 80 + "\n")
        
        # Print execution summary
        status.print_summary()
        
        # Save pipeline metadata
        metadata_path = output_dir / 'pipeline_metadata.json'
        with open(metadata_path, 'w') as f:
            json.dump(pipeline_result, f, indent=2)
        
        return pipeline_result
        
    except PipelineError as e:
        logger.error(f"\n{'=' * 80}")
        logger.error("PIPELINE FAILED")
        logger.error("=" * 80)
        logger.error(f"Error: {str(e)}")
        logger.error("=" * 80 + "\n")
        
        # Print execution summary showing where failure occurred
        status.print_summary()
        
        pipeline_result['status'] = 'failed'
        pipeline_result['error'] = str(e)
        pipeline_result['end_time'] = datetime.now().isoformat()
        
        raise


def main():
    """Command-line interface for pipeline"""
    parser = argparse.ArgumentParser(
        description='Forest Carbon Stock Estimation Pipeline',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # With AOI file
    python scripts/run_pipeline.py --aoi data/aoi.geojson --year 2023 --output outputs/
    
    # LiDAR-only mode (AOI auto-extracted from tiles)
    python scripts/run_pipeline.py --laz-tiles path/to/tiles --year 2023 --output outputs/
    
    # LiDAR with custom resolution and CRS override
    python scripts/run_pipeline.py --laz-tiles path/to/tiles --year 2023 --output outputs/ --lidar-resolution-m 5 --lidar-crs EPSG:32610
        """
    )
    
    parser.add_argument(
        '--aoi',
        type=Path,
        default=None,
        help='Path to AOI GeoJSON/GeoTIFF file. Optional if --laz-tiles is provided (AOI will be auto-extracted from tile bounds)'
    )
    
    parser.add_argument(
        '--year',
        type=int,
        required=True,
        help='Year to process (e.g., 2023)'
    )
    
    parser.add_argument(
        '--output',
        type=Path,
        default=Path('outputs'),
        help='Output directory (default: outputs/)'
    )

    parser.add_argument(
        '--laz-tiles',
        type=Path,
        default=None,
        help='Optional: Path to folder containing local .laz/.las tiles (uses local LiDAR instead of downloading)'
    )

    parser.add_argument(
        '--lidar-resolution-m',
        type=float,
        default=5.0,
        help='LiDAR raster resolution in meters (default: 5.0). Values < 2.0 are clamped to 5.0.'
    )

    parser.add_argument(
        '--allom-a',
        type=float,
        default=None,
        help='Optional allometry coefficient a for AGB=a*(H^b). If omitted, defaults are used.'
    )

    parser.add_argument(
        '--allom-b',
        type=float,
        default=None,
        help='Optional allometry coefficient b for AGB=a*(H^b). If omitted, defaults are used.'
    )

    parser.add_argument(
        '--lidar-crs',
        type=str,
        default=None,
        help='Override CRS for LiDAR tiles if header CRS is missing (e.g., EPSG:32610 for UTM Zone 10N)'
    )
    
    args = parser.parse_args()
    
    # Validate that either --aoi or --laz-tiles is provided
    if not args.aoi and not args.laz_tiles:
        parser.error("Either --aoi or --laz-tiles must be provided")
    
    # Run pipeline
    try:
        result = run_complete_pipeline(
            aoi_path=args.aoi,
            year=args.year,
            output_dir=args.output,
            laz_tiles_dir=args.laz_tiles,
            lidar_resolution_m=args.lidar_resolution_m,
            allom_a=args.allom_a,
            allom_b=args.allom_b,
            lidar_crs=args.lidar_crs,
        )
        
        print("\n" + "=" * 80)
        print("PIPELINE SUMMARY")
        print("=" * 80)
        print(f"Status: {result['status']}")
        print(f"Output directory: {result['output_directory']}")
        print(f"Final results: {result['final_result']}")
        print("=" * 80 + "\n")
        
    except Exception as e:
        logger.error(f"Pipeline failed: {str(e)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
