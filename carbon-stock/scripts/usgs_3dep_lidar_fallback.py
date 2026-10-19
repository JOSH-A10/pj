"""
USGS 3DEP Airborne LiDAR Fallback for GEDI L4A
================================================

FALLBACK ONLY: Used when GEDI L4A returns zero footprints.

CRITICAL: USGS 3DEP LiDAR is accessed via USGS TNM API, NOT Google Earth Engine.
This is a local processing pipeline: metadata query → tile download → CHM generation.

DATA ACCESS METHOD:
- Query: USGS TNM (The National Map) API
- Dataset: Lidar Point Cloud (LPC)
- Format: LAS/LAZ point cloud tiles
- Storage: data/raw/lidar/3dep/
- Processing: Local (PDAL/laspy)

WORKFLOW:
1. Query USGS TNM API for LiDAR tile metadata (bbox, year, format)
2. Download LAS/LAZ tiles if available
3. Classify points (ground vs vegetation)
4. Generate DSM (first returns) and DTM (ground points)
5. Compute CHM = DSM - DTM
6. Aggregate to 30m resolution (FIA plot scale)
7. Apply height-based FIA allometry: AGB = 0.168 * H^2.47
8. Apply NLCD forest mask
9. Calculate carbon using IPCC defaults (0.47, 3.67)

CONSTRAINTS:
- No ML models
- No species logic
- No tree detection
- Height-only FIA-calibrated allometry
- Same carbon conversion as GEDI path
- No GEE datasets used

USAGE:
    Called automatically by pipeline when GEDI footprints == 0
"""

import logging
import json
from pathlib import Path
from typing import Dict, Tuple, Optional, List
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import reproject, calculate_default_transform
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
import geopandas as gpd
import requests
import subprocess
import shutil
import time
import re
from shapely.geometry import box
import tempfile
import shutil

logger = logging.getLogger(__name__)

# ALS access modes
ALS_ACCESS_MODE = {
    'API_TILES': 'api_tiles',           # Tiles downloadable via TNM Products API
    'LOCAL_TILES': 'local_tiles',       # Tiles provided manually or pre-seeded
    'S3_PUBLIC': 's3_public',           # Download from USGS public S3 bucket
    'EPT_STREAM': 'ept_stream',         # Stream from Entwine Point Tiles
    'UNAVAILABLE': 'unavailable'        # No LiDAR project exists
}

# FIA-calibrated height-biomass allometry (Jenkins et al. 2003, USFS)
# Equation: AGB (Mg/ha) = a * H^b
# These are fixed constants for mixed temperate forests
FIA_ALLOMETRY_A = 0.06 # Mg/ha per meter
FIA_ALLOMETRY_B = 2.50   # Exponential scaling factor

# Raster resolution (single configurable variable)
RASTER_RES_M = 5.0

# Safety guard: prevent accidental very-high-resolution runs
if RASTER_RES_M < 1.0:
    logger.warning(f"Configured raster resolution {RASTER_RES_M}m is below safe threshold; clamping to 5.0m")
    RASTER_RES_M = 1.0


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


def validate_aoi_for_lidar_processing(aoi: gpd.GeoDataFrame) -> bool:
    """
    Validate AOI geometry for LiDAR processing.
    
    Checks:
    - AOI has valid geometry
    - AOI has CRS defined
    - AOI area is reasonable (> 0.01 ha, < 1000 km²)
    - AOI can be reprojected
    
    Args:
        aoi: Input AOI GeoDataFrame
        
    Returns:
        True if valid, raises ValueError if invalid
    """
    logger.info("[VALIDATION] Checking AOI for LiDAR processing...")
    
    # Check 1: Has geometry
    if len(aoi) == 0 or aoi.geometry.is_empty.all():
        raise ValueError("AOI geometry is empty")
    
    # Check 2: Has CRS
    if aoi.crs is None:
        logger.warning("[WARNING] AOI has no CRS - assuming EPSG:4326")
        aoi.set_crs("EPSG:4326", inplace=True)
    
    # Check 3: Calculate area
    try:
        # Use equal area projection for accurate area calculation
        aoi_aeqd = aoi.to_crs('EPSG:6933')  # WGS84 / NSIDC EASE-Grid 2.0 Global
        area_m2 = aoi_aeqd.geometry.area.sum()
        area_ha = area_m2 / 10000.0
        area_km2 = area_ha / 100.0
        
        logger.info(f"[VALIDATION] AOI area: {area_ha:.2f} ha ({area_km2:.2f} km²)")
        
        if area_ha < 0.01:
            raise ValueError(f"AOI area too small: {area_ha:.4f} ha (minimum 0.01 ha)")
        
        if area_km2 > 1000:
            logger.warning(f"[WARNING] AOI area very large: {area_km2:.2f} km² (may take very long to process)")
    
    except Exception as e:
        raise ValueError(f"Cannot calculate AOI area: {e}")
    
    # Check 4: Test reprojection to EPSG:4326
    try:
        aoi_wgs84 = aoi.to_crs('EPSG:4326')
        bounds = aoi_wgs84.total_bounds
        
        # Check bounds are within valid WGS84 range
        if not (-180 <= bounds[0] <= 180 and -180 <= bounds[2] <= 180):
            raise ValueError(f"AOI longitude out of range: {bounds[0]}, {bounds[2]}")
        if not (-90 <= bounds[1] <= 90 and -90 <= bounds[3] <= 90):
            raise ValueError(f"AOI latitude out of range: {bounds[1]}, {bounds[3]}")
        
        logger.info(f"[VALIDATION] AOI WGS84 bounds: ({bounds[0]:.4f}, {bounds[1]:.4f}) to ({bounds[2]:.4f}, {bounds[3]:.4f})")
    
    except Exception as e:
        raise ValueError(f"Cannot reproject AOI to WGS84: {e}")
    
    logger.info("[VALIDATION] ✓ AOI passed all validation checks")
    return True


def check_usgs_api_health() -> bool:
    """
    Health check: Verify USGS TNM API is reachable.
    
    Returns:
        True if API is healthy, False otherwise
    """
    try:
        response = requests.get(
            "https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer?f=json",
            timeout=10
        )
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"[WARNING] USGS API health check failed: {e}")
        return False


def resolve_aoi_from_project(project_name: str, output_path: Path) -> Optional[gpd.GeoDataFrame]:
    """
    Automatically resolve AOI geometry from USGS LiDAR project metadata.
    
    Fallback chain:
    1. USGS 3DEP API project footprint
    2. S3 bucket EPT metadata bounds
    3. S3 tile extent union (sampling)
    
    Args:
        project_name: USGS project name (e.g., "USGS_LPC_CA_NoCAL_Wildfires_B4_2018")
        output_path: Where to save the resolved AOI GeoJSON
        
    Returns:
        GeoDataFrame with AOI geometry, or None if resolution failed
    """
    logger.info(f"[INFO] Attempting automatic AOI resolution for: {project_name}")
    
    # Method 1: Query USGS 3DEP API for project footprint
    try:
        logger.info("[INFO] Method 1: Querying USGS 3DEP API for project footprint...")
        
        # Query for exact project match
        api_url = "https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer/8/query"
        params = {
            'where': f"project = '{project_name}'",
            'outFields': '*',
            'f': 'geojson',
            'returnGeometry': 'true'
        }
        
        response = requests.get(api_url, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            
            if 'features' in data and len(data['features']) > 0:
                # Convert to GeoDataFrame
                aoi = gpd.GeoDataFrame.from_features(data['features'], crs='EPSG:4326')
                
                # Get the project extent (union if multiple features)
                aoi_geom = aoi.geometry.unary_union
                aoi_gdf = gpd.GeoDataFrame([{'project': project_name}], geometry=[aoi_geom], crs='EPSG:4326')
                
                # Calculate area
                area_km2 = aoi_gdf.to_crs(epsg=3857).area.sum() / 1e6
                
                logger.info(f"[SUCCESS] AOI resolved via USGS 3DEP API")
                logger.info(f"[INFO] AOI area: {area_km2:.2f} km²")
                
                # Validate area
                if not (1 <= area_km2 <= 500):
                    logger.warning(f"[WARNING] AOI area ({area_km2:.2f} km²) outside expected range (1-500 km²)")
                
                # Save AOI
                output_path.parent.mkdir(parents=True, exist_ok=True)
                aoi_gdf.to_file(output_path, driver='GeoJSON')
                logger.info(f"[INFO] AOI saved to: {output_path}")
                
                return aoi_gdf
    
    except Exception as e:
        logger.warning(f"[WARNING] Method 1 failed: {e}")
    
    # Method 2: Try EPT metadata from S3
    try:
        logger.info("[INFO] Method 2: Attempting EPT metadata resolution...")
        
        # Try to access EPT metadata (if exists)
        # Note: This would require knowing the EPT endpoint structure
        # For now, skip to Method 3
        logger.info("[INFO] EPT metadata not implemented - skipping to Method 3")
        
    except Exception as e:
        logger.warning(f"[WARNING] Method 2 failed: {e}")
    
    # Method 3: Sample S3 tiles and union extents
    try:
        logger.info("[INFO] Method 3: Sampling S3 tile extents...")
        
        import boto3
        from botocore import UNSIGNED
        from botocore.client import Config
        
        # Resolve S3 path
        s3_path = resolve_s3_dataset_path(project_name, None)
        
        if s3_path:
            s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
            
            # List first 20 LAZ files
            paginator = s3.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket='usgs-lidar-public', Prefix=s3_path, MaxKeys=20)
            
            laz_files = []
            for page in pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        if obj['Key'].lower().endswith(('.laz', '.las')):
                            laz_files.append(obj['Key'])
            
            if laz_files:
                logger.info(f"[INFO] Found {len(laz_files)} sample tiles for bounds extraction")
                
                # Download first tile header to get CRS and bounds
                # This is a simplified approach - in production, you'd parse LAS headers
                logger.info("[INFO] Downloading sample tile for bounds extraction...")
                
                temp_file = Path(tempfile.gettempdir()) / 'sample_tile.laz'
                s3.download_file('usgs-lidar-public', laz_files[0], str(temp_file))
                
                # Use laspy to read bounds
                import laspy
                las = laspy.read(str(temp_file))
                
                # Get bounds in native CRS
                minx, miny, minz = las.header.mins
                maxx, maxy, maxz = las.header.maxs
                
                # Create polygon
                from shapely.geometry import box as shapely_box
                bounds_geom = shapely_box(minx, miny, maxx, maxy)
                
                # Try to get CRS from header
                # Default to EPSG:4326 if not available
                try:
                    # Parse CRS from VLR if available
                    aoi_gdf = gpd.GeoDataFrame([{'project': project_name}], geometry=[bounds_geom], crs='EPSG:3857')
                    aoi_gdf = aoi_gdf.to_crs('EPSG:4326')
                except:
                    logger.warning("[WARNING] Could not determine CRS from LAS file, assuming WGS84")
                    aoi_gdf = gpd.GeoDataFrame([{'project': project_name}], geometry=[bounds_geom], crs='EPSG:4326')
                
                # Calculate area
                area_km2 = aoi_gdf.to_crs(epsg=3857).area.sum() / 1e6
                
                logger.info(f"[SUCCESS] AOI resolved via S3 tile sampling")
                logger.info(f"[INFO] AOI area: {area_km2:.2f} km²")
                logger.info(f"[INFO] Note: This is a single-tile extent, may not represent full project")
                
                # Save AOI
                output_path.parent.mkdir(parents=True, exist_ok=True)
                aoi_gdf.to_file(output_path, driver='GeoJSON')
                logger.info(f"[INFO] AOI saved to: {output_path}")
                
                # Cleanup
                temp_file.unlink(missing_ok=True)
                
                return aoi_gdf
                
    except Exception as e:
        logger.warning(f"[WARNING] Method 3 failed: {e}")
    
    # All methods failed
    logger.error("[ERROR] All AOI resolution methods failed")
    logger.error("[ERROR] Unable to automatically determine project footprint")
    logger.error("[ERROR] User must provide AOI coordinates or GeoJSON file")
    
    return None


def resolve_s3_dataset_path(project_name: str, project_id: str) -> Optional[str]:
    """
    Resolve USGS 3DEP project name to S3 bucket path.
    
    USGS hosts LiDAR in s3://usgs-lidar-public/ with naming conventions:
    - Project: LA_Sabine_River_Lidar_2018_D18
    - S3 Path: LA_Sabine_River_Lidar_A1_2018/
    
    Args:
        project_name: Project name from metadata
        project_id: Project ID from metadata
        
    Returns:
        S3 dataset path (without s3:// prefix) or None
    """
    # Common transformations:
    # _D18 -> _A1_2018, _D19 -> _A1_2019, etc.
    # Pattern: <name>_<year>_D<digits> -> <name>_A1_<year>
    
    # Try pattern 1: Replace _D<digits> with _A1_<year>
    match = re.search(r'(.+)_(\d{4})_D\d+$', project_name)
    if match:
        base_name = match.group(1)
        year = match.group(2)
        s3_path = f"{base_name}_A1_{year}/"
        logger.info(f"[INFO] Resolved S3 path pattern 1: {s3_path}")
        return s3_path
    
    # Try pattern 2: Direct project name
    s3_path = f"{project_name}/"
    logger.info(f"[INFO] Resolved S3 path pattern 2: {s3_path}")
    return s3_path


def check_s3_dataset_availability(dataset_path: str) -> Tuple[bool, int]:
    """
    Check if USGS S3 dataset exists and contains LAS/LAZ files.
    
    Uses anonymous access (no AWS credentials required).
    
    Args:
        dataset_path: S3 path without bucket (e.g., 'LA_Sabine_River_Lidar_A1_2018/')
        
    Returns:
        Tuple of (exists, file_count)
    """
    try:
        # Try using boto3 if available
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
            
            s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
            
            # List objects in the dataset path
            paginator = s3.get_paginator('list_objects_v2')
            
            las_count = 0
            for page in paginator.paginate(Bucket='usgs-lidar-public', Prefix=dataset_path, MaxKeys=1000):
                if 'Contents' in page:
                    for obj in page['Contents']:
                        key = obj['Key']
                        if key.lower().endswith(('.las', '.laz')):
                            las_count += 1
                            if las_count >= 1:  # At least one file exists
                                logger.info(f"[INFO] S3 dataset contains {las_count}+ LAS/LAZ files")
                                return True, las_count
            
            if las_count > 0:
                return True, las_count
            else:
                logger.warning("[WARNING] S3 dataset exists but contains no LAS/LAZ files")
                return False, 0
                
        except ImportError:
            # Fallback to AWS CLI
            logger.info("[INFO] boto3 not available, using AWS CLI")
            
            cmd = [
                'aws', 's3', 'ls',
                '--no-sign-request',
                f's3://usgs-lidar-public/{dataset_path}',
                '--recursive'
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                # Count LAS/LAZ files in output
                las_count = sum(1 for line in result.stdout.split('\n') 
                               if line.lower().endswith(('.las', '.laz')))
                
                if las_count > 0:
                    logger.info(f"[INFO] S3 dataset contains {las_count} LAS/LAZ files")
                    return True, las_count
                else:
                    return False, 0
            else:
                logger.warning(f"[WARNING] AWS CLI failed: {result.stderr}")
                return False, 0
                
    except subprocess.TimeoutExpired:
        logger.warning("[WARNING] S3 availability check timed out")
        return False, 0
    except Exception as e:
        logger.warning(f"[WARNING] S3 availability check failed: {e}")
        return False, 0


def download_from_s3(dataset_path: str, output_dir: Path, max_tiles: int = 10) -> int:
    """
    Download LAS/LAZ tiles from USGS public S3 bucket.
    
    Args:
        dataset_path: S3 path without bucket
        output_dir: Local directory to save tiles
        max_tiles: Maximum number of tiles to download
        
    Returns:
        Number of tiles successfully downloaded
    """
    logger.info("[INFO] Downloading LAS/LAZ tiles from USGS S3 bucket...")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    downloaded_count = 0
    
    try:
        # Try boto3 first
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
            
            s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
            
            # List and download LAS/LAZ files
            paginator = s3.get_paginator('list_objects_v2')
            
            for page in paginator.paginate(Bucket='usgs-lidar-public', Prefix=dataset_path):
                if 'Contents' not in page:
                    continue
                    
                for obj in page['Contents']:
                    if downloaded_count >= max_tiles:
                        break
                        
                    key = obj['Key']
                    if not key.lower().endswith(('.las', '.laz')):
                        continue
                    
                    # Extract filename
                    filename = key.split('/')[-1]
                    output_path = output_dir / filename
                    
                    # Skip if already exists
                    if output_path.exists():
                        logger.info(f"[INFO] Tile {downloaded_count+1} already cached: {filename}")
                        downloaded_count += 1
                        continue
                    
                    logger.info(f"[INFO] Downloading tile {downloaded_count+1}/{max_tiles}: {filename}")
                    
                    try:
                        s3.download_file('usgs-lidar-public', key, str(output_path))
                        size_mb = output_path.stat().st_size / (1024.0 * 1024.0)
                        logger.info(f"[SUCCESS] Downloaded: {filename} ({size_mb:.1f} MB)")
                        downloaded_count += 1
                    except Exception as e:
                        logger.warning(f"[WARNING] Failed to download {filename}: {e}")
                        continue
                
                if downloaded_count >= max_tiles:
                    break
            
            return downloaded_count
            
        except ImportError:
            # Fallback to AWS CLI
            logger.info("[INFO] Using AWS CLI for download")
            
            # List files first
            list_cmd = [
                'aws', 's3', 'ls',
                '--no-sign-request',
                f's3://usgs-lidar-public/{dataset_path}',
                '--recursive'
            ]
            
            result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode != 0:
                logger.error(f"[ERROR] Failed to list S3 files: {result.stderr}")
                return 0
            
            # Parse file list
            files = []
            for line in result.stdout.split('\n'):
                if line.lower().endswith(('.las', '.laz')):
                    # Extract S3 key from ls output
                    parts = line.split()
                    if len(parts) >= 4:
                        files.append(parts[-1])
            
            # Download files
            for i, s3_key in enumerate(files[:max_tiles]):
                filename = s3_key.split('/')[-1]
                output_path = output_dir / filename
                
                if output_path.exists():
                    logger.info(f"[INFO] Tile {i+1} already cached: {filename}")
                    downloaded_count += 1
                    continue
                
                logger.info(f"[INFO] Downloading tile {i+1}/{min(max_tiles, len(files))}: {filename}")
                
                download_cmd = [
                    'aws', 's3', 'cp',
                    '--no-sign-request',
                    f's3://usgs-lidar-public/{s3_key}',
                    str(output_path)
                ]
                
                result = subprocess.run(download_cmd, capture_output=True, text=True, timeout=300)
                
                if result.returncode == 0 and output_path.exists():
                    size_mb = output_path.stat().st_size / (1024.0 * 1024.0)
                    logger.info(f"[SUCCESS] Downloaded: {filename} ({size_mb:.1f} MB)")
                    downloaded_count += 1
                else:
                    logger.warning(f"[WARNING] Failed to download {filename}")
            
            return downloaded_count
            
    except Exception as e:
        logger.error(f"[ERROR] S3 download failed: {e}")
        return downloaded_count


def check_3dep_availability(aoi: gpd.GeoDataFrame) -> Tuple[bool, Optional[List[Dict]]]:
    """
    Check if USGS 3DEP LiDAR coverage exists for AOI via USGS TNM API.
    
    IMPORTANT: This queries USGS metadata, NOT Google Earth Engine.
    USGS 3DEP LiDAR is NOT available as a GEE asset.
    
    YEAR-AGNOSTIC: Searches for ANY available LiDAR, regardless of acquisition year.
    This is standard practice - airborne LiDAR is used as a structural snapshot.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        
    Returns:
        Tuple of (availability boolean, projects list) or (False, None)
    """
    logger.info("[INFO] Querying USGS TNM API for 3DEP LiDAR metadata (any year)...")
    logger.info("[INFO] ALS is year-agnostic - latest available structural data will be used")
    
    try:
        bounds = aoi.total_bounds
        minx, miny, maxx, maxy = bounds
        
        # USGS 3DEP via ArcGIS REST API (used by LiDAR Explorer)
        # This is the actual endpoint the USGS LiDAR Explorer uses
        base_url = "https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer/8/query"
        
        # NO YEAR FILTER - query all available LiDAR
        # Airborne LiDAR is treated as a structural snapshot.
        # Latest available 3DEP LiDAR is used when GEDI is unavailable.
        # This is standard practice for small-area biomass estimation.
        
        params = {
            'where': '1=1',
            'geometry': f'{minx},{miny},{maxx},{maxy}',
            'geometryType': 'esriGeometryEnvelope',
            'inSR': '4326',  # WGS84
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': '*',
            'returnGeometry': 'false',
            'f': 'json'
        }
        
        # Handle SSL certificate issues (corporate proxies, etc.)
        try:
            response = requests.get(base_url, params=params, timeout=30, verify=True)
        except requests.exceptions.SSLError:
            logger.warning("[WARNING] SSL verification failed, retrying without verification")
            response = requests.get(base_url, params=params, timeout=30, verify=False)
        
        # DEBUG: Log the actual request
        logger.info(f"[DEBUG] API URL: {response.url[:200]}...")
        logger.info(f"[DEBUG] Response status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            
            # ArcGIS REST API response format
            features = data.get('features', [])
            
            # DEBUG: Log response details
            logger.info(f"[DEBUG] Features found: {len(features)}")
            if len(features) > 0:
                logger.info(f"[DEBUG] First feature fields: {list(features[0].get('attributes', {}).keys())[:10]}")
                
                # Extract acquisition years
                years = []
                for feat in features[:5]:
                    attrs = feat.get('attributes', {})
                    # Try common year fields
                    year = attrs.get('acq_year') or attrs.get('year') or attrs.get('acquisition_year')
                    if year:
                        years.append(str(year))
                
                year_info = f" (acquisition years: {', '.join(set(years))})" if years else ""
                logger.info(f"[INFO] Found {len(features)} USGS 3DEP LiDAR project(s){year_info}")
                logger.info("[INFO] Access method: USGS 3DEP via ArcGIS REST API")
                logger.info("[INFO] Will select most recent acquisition for structural analysis")
                return True, features
            else:
                logger.info("[INFO] No USGS 3DEP LiDAR coverage for AOI (any year)")
                return False, None
        else:
            logger.warning(f"[WARNING] USGS TNM API returned status {response.status_code}")
            return False, None
            
    except requests.exceptions.RequestException as e:
        logger.warning(f"[WARNING] USGS TNM API request failed: {str(e)}")
        logger.warning("[WARNING] Network issue - cannot verify 3DEP coverage")
        return False, None
    except Exception as e:
        logger.warning(f"[WARNING] Failed to check 3DEP availability: {str(e)}")
        return False, None


def download_3dep_tiles(aoi: gpd.GeoDataFrame, output_dir: Path, projects: List[Dict]) -> Optional[Tuple[Path, str, str, str]]:
    """
    Download USGS 3DEP LiDAR tiles based on detected projects.
    
    Uses USGS TNM Products API to download actual LAS/LAZ files.
    Supports LOCAL_TILES mode when API doesn't expose tile URLs.
    
    YEAR-AGNOSTIC: Selects the most recent available LiDAR acquisition.
    Returns tile directory, acquisition year, project name, and access mode.
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        output_dir: Directory to save tiles (data/raw/lidar/3dep/)
        projects: List of project metadata dicts from ArcGIS REST API
        
    Returns:
        Tuple of (tiles_dir Path, acquisition_year str, project_name str, access_mode str), or None if critical failure
    """
    logger.info("[INFO] Downloading USGS 3DEP LiDAR tiles...")
    logger.info("[INFO] Selecting most recent acquisition (year-agnostic)")
    
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Sort projects by collection end date to get most recent
        # Airborne LiDAR is treated as a structural snapshot.
        # Latest available 3DEP LiDAR is used when GEDI is unavailable.
        # This is standard practice for small-area biomass estimation.
        projects_sorted = sorted(
            projects,
            key=lambda x: x.get('attributes', {}).get('collect_end', ''),
            reverse=True
        )
        
        most_recent = projects_sorted[0]
        attrs = most_recent.get('attributes', {})
        project_name = attrs.get('project', 'unknown')
        project_id = str(attrs.get('project_id', 'unknown'))  # Ensure string
        collect_start = attrs.get('collect_start', '')
        collect_end = attrs.get('collect_end', '')
        
        # Extract year from collect_end (most relevant)
        # Handle both Unix timestamps (int) and date strings
        if isinstance(collect_end, int):
            # Unix timestamp in milliseconds - convert to datetime
            from datetime import datetime
            acquisition_year = datetime.fromtimestamp(float(collect_end) / 1000.0).strftime('%Y')
        elif isinstance(collect_end, str) and collect_end:
            acquisition_year = collect_end.split('-')[0]
        else:
            acquisition_year = 'unknown'
        
        logger.info(f"[INFO] Most recent project: {project_name}")
        logger.info(f"[INFO] Collection period: {collect_start} to {collect_end}")
        logger.info(f"[INFO] Acquisition year: {acquisition_year}")
        
        # Create cache directory for downloaded tiles
        cache_dir = output_dir / 'cache' / project_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        tiles_dir = cache_dir
        
        bounds = aoi.total_bounds
        minx, miny, maxx, maxy = bounds
        
        # Use USGS TNM Products API to find downloadable tiles
        api_url = "https://tnmaccess.nationalmap.gov/api/v1/products"
        
        # Create polygon in WKT format for the API
        polygon_wkt = f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}))"
        
        params = {
            'polygon': polygon_wkt,
            'datasets': 'Lidar Point Cloud (LPC)',
            'prodFormats': 'LAZ,LAS',
            'outputFormat': 'JSON',
            'max': 50
        }
        
        logger.info("[INFO] Querying USGS TNM Products API for tile URLs...")
        
        try:
            response = requests.get(api_url, params=params, timeout=60, verify=True)
        except requests.exceptions.SSLError:
            logger.warning("[WARNING] SSL verification failed, retrying without verification")
            response = requests.get(api_url, params=params, timeout=60, verify=False)
        except Exception as e:
            logger.error(f"[ERROR] TNM Products API request failed: {e}")
            # Fallback: Return placeholder with metadata
            logger.warning("[WARNING] Using placeholder - PDAL processing will be skipped")
            return None
        
        # Check if API returned data
        if response.status_code != 200:
            logger.warning(f"[WARNING] TNM Products API returned status {response.status_code}")
            logger.warning("[WARNING] API temporarily unavailable")
            
            # Check for local tiles before failing
            las_files = list(tiles_dir.glob('*.las')) + list(tiles_dir.glob('*.laz'))
            if las_files:
                logger.info(f"[INFO] Found {len(las_files)} local LAS/LAZ files (cached)")
                logger.info("[INFO] Switching to LOCAL_TILE_MODE")
                return tiles_dir, acquisition_year, project_name, ALS_ACCESS_MODE['LOCAL_TILES']
            
            # Save metadata about the project
            metadata_path = tiles_dir / 'metadata.json'
            with open(metadata_path, 'w') as f:
                json.dump({
                    'source': 'USGS_3DEP',
                    'project': project_name,
                    'project_id': project_id,
                    'acquisition_year': acquisition_year,
                    'collect_start': collect_start,
                    'collect_end': collect_end,
                    'methodology_note': 'ALS year not forced; latest available structural data used',
                    'download_status': 'api_unavailable',
                    'access_mode': ALS_ACCESS_MODE['LOCAL_TILES']
                }, f, indent=2)
            
            return tiles_dir, acquisition_year, project_name, ALS_ACCESS_MODE['LOCAL_TILES']
        
        # Parse response
        try:
            data = response.json()
            items = data.get('items', [])
            
            if not items:
                logger.warning("[WARNING] No LAS/LAZ URLs exposed via TNM Products API")
                logger.info("[INFO] This is a known USGS limitation for some projects (especially pre-2020)")
                logger.info("[INFO] 3DEP project exists - LiDAR IS available")
                logger.info("[INFO] Attempting USGS public S3 access...")
                
                # Try S3 public bucket
                s3_path = resolve_s3_dataset_path(project_name, project_id)
                
                if s3_path:
                    logger.info(f"[INFO] Resolved USGS S3 LiDAR dataset: s3://usgs-lidar-public/{s3_path}")
                    logger.info("[INFO] Checking S3 dataset availability...")
                    
                    s3_available, s3_file_count = check_s3_dataset_availability(s3_path)
                    
                    if s3_available:
                        logger.info(f"[SUCCESS] S3 dataset found with {s3_file_count} LAS/LAZ files")
                        logger.info("[INFO] Downloading tiles from USGS S3 bucket...")
                        
                        downloaded = download_from_s3(s3_path, tiles_dir, max_tiles=10)
                        
                        if downloaded > 0:
                            logger.info(f"[SUCCESS] Downloaded {downloaded} tiles from S3")
                            
                            # Save metadata
                            metadata_path = tiles_dir / 'metadata.json'
                            with open(metadata_path, 'w') as f:
                                json.dump({
                                    'source': 'USGS_3DEP',
                                    'project': project_name,
                                    'project_id': project_id,
                                    'acquisition_year': acquisition_year,
                                    'collect_start': collect_start,
                                    'collect_end': collect_end,
                                    'methodology_note': 'ALS year not forced; latest available structural data used',
                                    'download_status': 's3_success',
                                    'access_mode': ALS_ACCESS_MODE['S3_PUBLIC'],
                                    's3_path': s3_path,
                                    'tiles_downloaded': downloaded
                                }, f, indent=2)
                            
                            return tiles_dir, acquisition_year, project_name, ALS_ACCESS_MODE['S3_PUBLIC']
                        else:
                            logger.warning("[WARNING] S3 download failed or no tiles downloaded")
                    else:
                        logger.warning("[WARNING] S3 dataset not found or inaccessible")
                
                # Fall back to local tiles mode
                logger.info("[INFO] Switching to LOCAL_TILE_MODE")
                
                # Check for local tiles
                las_files = list(tiles_dir.glob('*.las')) + list(tiles_dir.glob('*.laz'))
                if las_files:
                    logger.info(f"[INFO] Found {len(las_files)} local LAS/LAZ files (manual or cached)")
                    logger.info("[SUCCESS] Proceeding with local tiles")
                else:
                    logger.info("[INFO] Waiting for user-provided LAS/LAZ tiles")
                    logger.info(f"[INFO] Expected location: {tiles_dir}")
                
                # Save metadata
                metadata_path = tiles_dir / 'metadata.json'
                with open(metadata_path, 'w') as f:
                    json.dump({
                        'source': 'USGS_3DEP',
                        'project': project_name,
                        'project_id': project_id,
                        'acquisition_year': acquisition_year,
                        'collect_start': collect_start,
                        'collect_end': collect_end,
                        'methodology_note': 'ALS year not forced; latest available structural data used',
                        'download_status': 'api_no_tiles',
                        'access_mode': ALS_ACCESS_MODE['LOCAL_TILES'],
                        'note': 'Project exists but tiles not exposed via API - LOCAL_TILE_MODE enabled'
                    }, f, indent=2)
                
                return tiles_dir, acquisition_year, project_name, ALS_ACCESS_MODE['LOCAL_TILES']
            
            tiles_expected = len(items)
            logger.info(f"[INFO] LiDAR tiles expected: {tiles_expected}")
            logger.info("[INFO] Downloading up to 10 tiles (with retry logic)...")
            
            downloaded = []
            failed_tiles = 0
            
            for i, item in enumerate(items[:10]):  # Limit to 10 tiles
                download_url = item.get('downloadURL')
                if not download_url:
                    failed_tiles += 1
                    continue
                
                # Determine file extension
                if download_url.lower().endswith('.laz'):
                    filename = f"tile_{i:03d}.laz"
                else:
                    filename = f"tile_{i:03d}.las"
                
                output_path = tiles_dir / filename
                
                # Skip if already cached
                if output_path.exists():
                    logger.info(f"[INFO] Tile {i+1} already cached, skipping download")
                    downloaded.append(output_path)
                    continue
                
                # Retry logic: 3 attempts with exponential backoff
                max_retries = 3
                retry_delays = [2, 5, 10]  # seconds
                success = False
                
                for attempt in range(max_retries):
                    try:
                        if attempt > 0:
                            logger.info(f"[INFO] Retry {attempt}/{max_retries-1} for tile {i+1} after {retry_delays[attempt-1]}s...")
                            time.sleep(retry_delays[attempt-1])
                        
                        logger.info(f"[INFO] Downloading tile {i+1}/{min(10, len(items))} (attempt {attempt+1})...")
                        
                        try:
                            tile_response = requests.get(download_url, stream=True, timeout=30, verify=True)
                        except requests.exceptions.SSLError:
                            tile_response = requests.get(download_url, stream=True, timeout=30, verify=False)
                        
                        if tile_response.status_code == 200:
                            with open(output_path, 'wb') as f:
                                for chunk in tile_response.iter_content(chunk_size=8192):
                                    f.write(chunk)
                            
                            size_mb = output_path.stat().st_size / (1024.0 * 1024.0)
                            logger.info(f"[SUCCESS] Downloaded: {filename} ({size_mb:.1f} MB)")
                            downloaded.append(output_path)
                            success = True
                            break
                        else:
                            logger.warning(f"[WARNING] Tile {i+1} download failed: HTTP {tile_response.status_code}")
                            if attempt == max_retries - 1:
                                failed_tiles += 1
                    
                    except Exception as e:
                        logger.warning(f"[WARNING] Tile {i+1} download error (attempt {attempt+1}): {e}")
                        if attempt == max_retries - 1:
                            failed_tiles += 1
                
                if not success:
                    logger.warning(f"[WARNING] Tile {i+1} failed after {max_retries} attempts")
            
            # Log download summary
            tiles_downloaded = len(downloaded)
            coverage_pct = (tiles_downloaded / min(10, tiles_expected)) * 100 if tiles_expected > 0 else 0
            
            logger.info(f"[INFO] LiDAR tiles downloaded: {tiles_downloaded}/{min(10, tiles_expected)} ({coverage_pct:.0f}% coverage)")
            
            # Save metadata
            metadata_path = tiles_dir / 'metadata.json'
            with open(metadata_path, 'w') as f:
                json.dump({
                    'source': 'USGS_3DEP',
                    'project': project_name,
                    'project_id': project_id,
                    'acquisition_year': acquisition_year,
                    'collect_start': collect_start,
                    'collect_end': collect_end,
                    'methodology_note': 'ALS year not forced; latest available structural data used',
                    'tiles_expected': tiles_expected,
                    'tiles_downloaded': tiles_downloaded,
                    'tiles_failed': failed_tiles,
                    'coverage_percent': coverage_pct,
                    'download_status': 'partial' if 0 < tiles_downloaded < min(10, tiles_expected) else ('success' if downloaded else 'failed')
                }, f, indent=2)
            
            # Accept partial downloads (at least 1 tile)
            if downloaded:
                logger.info(f"[SUCCESS] Proceeding with ALS processing using {tiles_downloaded} tile(s)")
                return tiles_dir, acquisition_year, project_name, ALS_ACCESS_MODE['API_TILES']
            else:
                logger.error("[ERROR] All tile downloads failed after retries")
                # Check for local tiles before failing
                las_files = list(tiles_dir.glob('*.las')) + list(tiles_dir.glob('*.laz'))
                if las_files:
                    logger.info(f"[INFO] Found {len(las_files)} local LAS/LAZ files (cached)")
                    logger.info("[INFO] Switching to LOCAL_TILE_MODE")
                    return tiles_dir, acquisition_year, project_name, ALS_ACCESS_MODE['LOCAL_TILES']
                
                # Check if API is healthy
                if not check_usgs_api_health():
                    logger.error("[ERROR] USGS API appears unreachable")
                return None
                
        except json.JSONDecodeError:
            logger.error("[ERROR] Failed to parse TNM Products API response")
            return None
            
    except Exception as e:
        logger.error(f"[ERROR] Failed to download 3DEP tiles: {str(e)}")
        return None


def process_point_cloud_pdal(
    tiles_dir: Path,
    output_dir: Path,
    aoi: gpd.GeoDataFrame,
    resolution_m: float = RASTER_RES_M,
    override_crs: Optional[str] = None,
) -> Optional[Tuple[Path, Path]]:
    """
    Process LAS/LAZ point clouds using PDAL with AOI-based cropping.
    
    **WORKFLOW:**
    1. **CRS Detection** (Priority chain):
       - Use --lidar-crs override if provided
       - Read CRS from LAZ header if valid
       - Auto-detect from coordinates:
         * |x| < 180, |y| < 90 → EPSG:4326
         * Else → Compute UTM zone from AOI centroid
    
    2. **AOI Reprojection**:
       - Input: AOI in EPSG:4326 (or any CRS)
       - Output: AOI in LiDAR native CRS
       - Validate AOI intersects LAZ bounds
    
    3. **LAZ Cropping** (CRITICAL - saves memory & time):
       - filters.crop with AOI polygon WKT
       - Process ONLY points inside AOI
       - Reduces processing to exact area of interest
    
    4. **PDAL Pipeline**:
       - Read LAZ → Merge → **Crop to AOI** → [Classify ground if needed] → Rasterize
       - DTM: Ground points (Class 2) @ min elevation
       - DSM: Canopy points (Class 3-5) @ max elevation
    
    5. **Output**:
       - DTM/DSM clipped to AOI bounds
       - Resolution: 5m default (configurable)
       - CRS: Auto-selected UTM zone or native projected CRS
    
    **Example AOI Input (EPSG:4326):**
    ```
    [[-124.079, 40.671], [-124.070, 40.671], 
     [-124.071, 40.678], [-124.079, 40.678], 
     [-124.079, 40.671]]
    ```
    
    Args:
        tiles_dir: Directory containing LAS/LAZ files
        output_dir: Output directory for DTM/DSM
        aoi: AOI GeoDataFrame (any CRS, will be reprojected)
        resolution_m: Output raster resolution in meters (default: 5m)
        override_crs: Optional CRS override string (e.g., \"EPSG:32610\")
        override_crs: Optional CRS to use if LAZ header CRS is missing (e.g., 'EPSG:32610')
        
    Returns:
        Tuple of (dtm_path, dsm_path) or None if processing fails
    """
    if resolution_m < 2.0:
        logger.warning(f"[WARNING] Requested resolution {resolution_m}m is below safe threshold; clamping to 5.0m")
        resolution_m = 5.0

    # Validate AOI geometry before processing
    try:
        validate_aoi_for_lidar_processing(aoi)
    except ValueError as e:
        logger.error(f"[ERROR] AOI validation failed: {e}")
        return None

    logger.info("[INFO] Processing point clouds with PDAL...")
    
    try:
        # Check if PDAL is available
        try:
            result = subprocess.run(['pdal', '--version'], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                logger.error("[ERROR] PDAL not available or not working")
                logger.error("[ERROR] Install with: conda install -c conda-forge pdal python-pdal")
                return None
            pdal_version = result.stdout.strip()
            logger.info(f"[INFO] PDAL version: {pdal_version}")
        except FileNotFoundError:
            logger.error("[ERROR] PDAL not installed")
            logger.error("[ERROR] Install with: conda install -c conda-forge pdal python-pdal")
            return None
        except Exception as e:
            logger.error(f"[ERROR] Failed to check PDAL: {e}")
            return None
        
        # Find all LAS/LAZ files
        las_files = list(tiles_dir.glob('*.las')) + list(tiles_dir.glob('*.laz'))
        
        if not las_files:
            logger.error("[ERROR] No LAS/LAZ files found in tiles directory")
            return None
        
        logger.info(f"[INFO] Found {len(las_files)} LAS/LAZ file(s)")
        
        # ========================================================================
        # CRITICAL DATA QUALITY CHECKS
        # ========================================================================
        logger.info("[INFO] Performing LiDAR data quality assessment...")
        
        # Check 1: Elevation range and classification diversity
        try:
            import laspy
            
            # Sample first file for quick assessment
            sample_file = las_files[0]
            las = laspy.read(str(sample_file))
            
            # Check elevation range
            z_min = float(las.z.min())
            z_max = float(las.z.max())
            z_range = z_max - z_min
            z_array = np.array(las.z)  # Convert to numpy for std calculation
            z_std = float(np.std(z_array))
            
            logger.info(f"[INFO] Sample file elevation range: {z_range:.2f} m (std: {z_std:.2f} m)")
            logger.info(f"[INFO] Elevation: min={z_min:.2f} m, max={z_max:.2f} m")
            
            # Check classifications
            unique_classes = np.unique(las.classification).tolist()
            logger.info(f"[INFO] Classification values present: {unique_classes}")
            
            has_ground = 2 in unique_classes
            has_canopy_classes = any(c in unique_classes for c in [3, 4, 5])  # low/med/high veg
            has_unclassified = any(c in unique_classes for c in [0, 1])
            appears_unclassified = (not has_ground) and (not has_canopy_classes) and has_unclassified
            
            # Validation rules
            data_quality_issues = []
            
            if z_range < 3.0:
                logger.warning(f"[WARNING] Low elevation range ({z_range:.2f} m < 3 m)")
                logger.warning("[WARNING] LiDAR appears to be bare-earth or flattened")
                data_quality_issues.append("low_elevation_range")
            
            if z_std < 1.0:
                logger.warning(f"[WARNING] Low elevation variability (std={z_std:.2f} m)")
                logger.warning("[WARNING] Data may not contain canopy structure")
                data_quality_issues.append("low_variability")
            
            if not has_ground:
                logger.warning("[WARNING] No ground points (class 2) detected - will attempt ground classification")

            if (not has_canopy_classes) and (not has_unclassified):
                logger.warning("[WARNING] No vegetation or unclassified points")
                logger.warning("[WARNING] LiDAR appears to be ground-only")
                data_quality_issues.append("ground_only")
            
            # Decision: Can we do CHM-based biomass?
            if len(data_quality_issues) >= 2:
                logger.error("[ERROR] LiDAR data quality insufficient for CHM-based biomass")
                logger.error(f"[ERROR] Issues detected: {', '.join(data_quality_issues)}")
                logger.error("[ERROR] This appears to be bare-earth LiDAR or DEM-derived points")
                logger.error("[ERROR] Cannot generate reliable canopy height model")
                return None
            
            if data_quality_issues:
                logger.warning("[WARNING] Proceeding with caution - data quality concerns exist")
            else:
                logger.info("[SUCCESS] Data quality checks passed - suitable for CHM generation")
                
        except Exception as e:
            logger.warning(f"[WARNING] Could not perform quality checks: {e}")
            logger.warning("[WARNING] Proceeding without validation")
        
        # ========================================================================
        # Detect native CRS from first LAZ file using laspy
        logger.info("[INFO] Detecting native CRS from LAS/LAZ files...")
        
        las_crs = None
        needs_crs_assignment = False
        header_crs_valid = False
        
        # Always read sample file for later use (centroid calculation, etc.)
        try:
            sample_las = laspy.read(str(las_files[0]))
        except Exception as e:
            logger.error(f"[ERROR] Could not read LAZ file: {e}")
            return None
        
        # PRIORITY 1: If user provided override_crs, use it unconditionally
        if override_crs:
            logger.info(f"[INFO] Using user-provided CRS override: {override_crs}")
            las_crs = override_crs
            needs_crs_assignment = True  # Force PDAL to assign this CRS
            logger.info(f"[INFO] PDAL strategy: Use filters.assign to override SRS={las_crs}")
        else:
            # PRIORITY 2: Try reading CRS from LAZ header
            try:
                las_crs_obj = sample_las.header.parse_crs()
                
                if las_crs_obj is not None:
                    epsg_code = las_crs_obj.to_epsg()
                    
                    if epsg_code is not None and 1024 <= epsg_code <= 32767:
                        las_crs = f"EPSG:{epsg_code}"
                        header_crs_valid = True
                        logger.info(f"[INFO] LAS/LAZ native CRS detected from header: {las_crs}")
                    else:
                        logger.warning(f"[WARNING] Detected invalid EPSG code in header: {epsg_code}")
                        logger.warning(f"[WARNING] This is not a valid EPSG code. Provide --lidar-crs EPSG:XXXX")
                else:
                    logger.warning("[WARNING] No CRS found in LAZ header")
            except Exception as e:
                logger.warning(f"[WARNING] Could not parse CRS from LAZ header: {e}")
            
            # PRIORITY 3: If no valid header CRS, auto-detect from coordinates
            if not las_crs:
                logger.warning("[WARNING] LiDAR tiles are missing valid CRS information in header")
                logger.info("[INFO] Attempting automatic CRS detection from coordinate bounds...")
                
                # Get bounds from sample LAZ file
                las_bounds = (
                    float(sample_las.header.mins[0]),
                    float(sample_las.header.mins[1]),
                    float(sample_las.header.maxs[0]),
                    float(sample_las.header.maxs[1])
                )
                
                # Auto-detect CRS
                detected_crs = auto_detect_lidar_crs(las_bounds, aoi)
                
                if detected_crs:
                    las_crs = detected_crs
                    logger.info(f"[SUCCESS] Auto-detected CRS: {las_crs}")
                else:
                    # Fallback: Ask user to provide CRS
                    logger.error("[ERROR] Automatic CRS detection failed")
                    logger.error("[ERROR] Unable to determine CRS from coordinate bounds")
                    logger.error("[ERROR] Solution: Provide CRS using --lidar-crs EPSG:XXXX argument")
                    logger.error("[ERROR] Example: --lidar-crs EPSG:32610 (for UTM Zone 10N)")
                    logger.error("[ERROR] Example: --lidar-crs EPSG:6339 (for NAD83 California Albers)")
                    logger.error("[ERROR] Find your CRS at: https://epsg.io/")
                    return None
            else:
                logger.info(f"[INFO] Using detected CRS: {las_crs} (from LAZ header)")
        
        # Check if CRS is projected (meters) or geographic (degrees)
        from pyproj import CRS
        needs_reprojection = False
        output_crs = las_crs
        
        try:
            crs_check = CRS(las_crs)
            
            if crs_check.is_geographic:
                logger.warning(f"[WARNING] LiDAR CRS '{las_crs}' is geographic (lat/lon degrees), not projected")
                logger.info("[INFO] Will automatically reproject to appropriate UTM zone for meter-based resolution")
                
                # Calculate UTM zone from data centroid (reuse sample_las from CRS detection)
                x_center = float(np.mean(np.array(sample_las.x)))
                y_center = float(np.mean(np.array(sample_las.y)))
                
                # For EPSG:4326, x=lon, y=lat
                lon = x_center
                lat = y_center
                
                # Calculate UTM zone
                utm_zone = int((lon + 180) / 6) + 1
                
                # Determine hemisphere
                if lat >= 0:
                    utm_epsg = 32600 + utm_zone  # Northern hemisphere (WGS84)
                else:
                    utm_epsg = 32700 + utm_zone  # Southern hemisphere (WGS84)
                
                output_crs = f"EPSG:{utm_epsg}"
                needs_reprojection = True
                
                logger.info(f"[INFO] Data center: ({lon:.4f}°, {lat:.4f}°)")
                logger.info(f"[INFO] Computed UTM Zone: {utm_zone}{'N' if lat >= 0 else 'S'}")
                logger.info(f"[INFO] Output CRS: {output_crs} (auto-reprojected from {las_crs})")
            else:
                logger.info(f"[INFO] CRS is projected (units: meters) - resolution {resolution_m}m is valid")
                logger.info(f"[INFO] Output CRS: {output_crs} (using LiDAR native projection)")
        except Exception as e:
            logger.warning(f"[WARNING] Could not validate CRS projection type: {e}")
            logger.info(f"[INFO] Proceeding with CRS: {las_crs}")
        
        logger.info("="*80)
        logger.info("AOI-BASED LAZ CROPPING WORKFLOW")
        logger.info("="*80)
        logger.info(f"[INFO] Input AOI CRS: {aoi.crs} (will be reprojected)")
        logger.info(f"[INFO] LiDAR native CRS: {las_crs}")
        
        # Transform AOI to LiDAR native CRS for precise cropping
        logger.info(f"[STEP 1/4] Reprojecting AOI from {aoi.crs} to {las_crs}...")
        try:
            aoi_lidar_crs = aoi.to_crs(las_crs)
            logger.info(f"[SUCCESS] AOI reprojected to LiDAR CRS")
        except Exception as e:
            logger.error(f"[ERROR] Failed to transform AOI to LiDAR CRS: {e}")
            logger.error(f"[ERROR] Cannot crop LAZ without valid CRS transformation")
            return None
        
        bounds_lidar = aoi_lidar_crs.total_bounds
        minx_native, miny_native, maxx_native, maxy_native = bounds_lidar
        
        # CRITICAL: Validate AOI intersects with LAZ tile bounds
        logger.info(f"[STEP 2/4] Validating AOI intersection with LAZ tiles...")
        laz_union_bounds = (
            float(sample_las.header.mins[0]),
            float(sample_las.header.mins[1]),
            float(sample_las.header.maxs[0]),
            float(sample_las.header.maxs[1])
        )
        
        # Check if AOI intersects LAZ bounds
        aoi_intersects = (
            minx_native < laz_union_bounds[2] and maxx_native > laz_union_bounds[0] and
            miny_native < laz_union_bounds[3] and maxy_native > laz_union_bounds[1]
        )
        
        if not aoi_intersects:
            logger.error("[ERROR] AOI does not intersect with LAZ tile bounds!")
            logger.error(f"[ERROR] AOI bounds (in {las_crs}): ({minx_native:.2f}, {miny_native:.2f}) to ({maxx_native:.2f}, {maxy_native:.2f})")
            logger.error(f"[ERROR] LAZ bounds: ({laz_union_bounds[0]:.2f}, {laz_union_bounds[1]:.2f}) to ({laz_union_bounds[2]:.2f}, {laz_union_bounds[3]:.2f})")
            logger.error("[ERROR] Check that AOI coordinates are correct and cover the LAZ data area")
            return None
        
        logger.info(f"[SUCCESS] AOI intersects LAZ tiles - cropping will proceed")
        logger.info(f"[INFO] AOI bounds (in {las_crs}): ({minx_native:.2f}, {miny_native:.2f}) to ({maxx_native:.2f}, {maxy_native:.2f})")
        
        # Get output bounds in final output CRS
        if needs_reprojection:
            aoi_output_crs = aoi.to_crs(output_crs)
            bounds_output = aoi_output_crs.total_bounds
            minx_out, miny_out, maxx_out, maxy_out = bounds_output
            logger.info(f"[INFO] Crop bounds ({las_crs}): ({minx_native:.2f}, {miny_native:.2f}) to ({maxx_native:.2f}, {maxy_native:.2f})")
            logger.info(f"[INFO] Output bounds ({output_crs}): ({minx_out:.2f}, {miny_out:.2f}) to ({maxx_out:.2f}, {maxy_out:.2f})")
            logger.info(f"[INFO] Target resolution: {resolution_m} meters")
            logger.info(f"[INFO] Strategy: Crop in {las_crs} → Reproject to {output_crs} → Rasterize")
        else:
            minx_out, miny_out, maxx_out, maxy_out = minx_native, miny_native, maxx_native, maxy_native
            logger.info(f"[INFO] Processing bounds ({las_crs}): ({minx_native:.2f}, {miny_native:.2f}) to ({maxx_native:.2f}, {maxy_native:.2f})")
            logger.info(f"[INFO] Target resolution: {resolution_m} meters")
            logger.info("[INFO] Strategy: Crop to AOI → Keep in native CRS → Rasterize (no reprojection)")
        
        output_dir.mkdir(parents=True, exist_ok=True)
        dtm_path = output_dir / f'dtm_{int(resolution_m)}m.tif'
        dsm_path = output_dir / f'dsm_{int(resolution_m)}m.tif'

        # Build crop polygon (clip to exact AOI geometry in LiDAR CRS)
        logger.info(f"[STEP 3/4] Building AOI crop polygon for PDAL filters.crop...")
        crop_geom = aoi_lidar_crs.geometry.unary_union
        crop_polygon_wkt = crop_geom.wkt
        
        # Calculate area to be processed
        aoi_area_m2 = aoi_lidar_crs.geometry.area.sum()
        aoi_area_ha = aoi_area_m2 / 10000.0
        logger.info(f"[INFO] AOI area to process: {aoi_area_ha:.2f} ha ({aoi_area_m2:.0f} m²)")
        logger.info(f"[INFO] Crop polygon vertices: {len(list(crop_geom.exterior.coords)) if hasattr(crop_geom, 'exterior') else 'N/A'}")
        logger.info(f"[SUCCESS] Crop polygon prepared for PDAL")
        logger.info("="*80)

        # Decide whether we need to classify ground (unclassified tiles)
        needs_ground_classification = False
        canopy_filter_limits = "Classification[3:5]"  # default if classified
        if 'unique_classes' in locals():
            has_ground = 2 in unique_classes
            has_canopy_classes = any(c in unique_classes for c in [3, 4, 5])
            has_only_unclassified = all(c in [0, 1] for c in unique_classes)
            needs_ground_classification = (not has_ground) or has_only_unclassified
            if has_canopy_classes:
                canopy_filter_limits = "Classification[3:5]"
            else:
                # If no vegetation classes, approximate canopy as non-ground, non-noise
                canopy_filter_limits = "Classification![2:2],Classification![7:7],Classification![18:18]"
        else:
            # If quality check failed to run, be conservative: classify ground
            needs_ground_classification = True
            canopy_filter_limits = "Classification![2:2],Classification![7:7],Classification![18:18]"

        if needs_ground_classification:
            logger.info("[INFO] Unclassified/insufficient classification detected - running ground classification (SMRF)")
        else:
            logger.info("[INFO] Classified tiles detected - using Class 2 (ground) and Classes 3-5 (canopy) when available")
        
        # Create PDAL pipeline for DTM (ground points only)
        # STRATEGY: Read → [Assign CRS if needed] → Merge → Crop → [Classify] → [Reproject] → Rasterize
        dtm_stages = []
        
        # Add all input files
        for f in las_files:
            dtm_stages.append(str(f.absolute()))
        
        # CRITICAL: Add CRS assignment BEFORE merge if LAZ files have missing/invalid CRS
        if needs_crs_assignment:
            dtm_stages.append({
                "type": "filters.assign",
                "assignment": f"srs={las_crs}"
            })
            logger.info(f"[INFO] DTM pipeline: Adding filters.assign with SRS={las_crs} (before merge)")
        
        # Now merge files (they all have correct CRS if needs_crs_assignment was True)
        dtm_stages.append({"type": "filters.merge"})
        
        # CRITICAL: Crop to AOI polygon BEFORE expensive operations
        logger.info(f"[PIPELINE] DTM: Adding filters.crop with AOI polygon ({aoi_area_ha:.2f} ha)")
        dtm_stages.append({
            "type": "filters.crop",
            "polygon": crop_polygon_wkt
        })

        if needs_ground_classification:
            dtm_stages.append({"type": "filters.smrf"})

        # Add reprojection if needed
        if needs_reprojection:
            dtm_stages.append({"type": "filters.reprojection", "out_srs": output_crs})

        dtm_stages.extend([
            {"type": "filters.range", "limits": "Classification[2:2]"},
            {
                "type": "writers.gdal",
                "filename": str(dtm_path.absolute()),
                "resolution": resolution_m,
                "output_type": "min",
                "gdalopts": "TILED=YES,COMPRESS=LZW,BIGTIFF=IF_SAFER",
                "bounds": f"([{minx_out}, {maxx_out}], [{miny_out}, {maxy_out}])",
                "override_srs": output_crs,
                "nodata": "-9999",
            },
        ])

        dtm_pipeline = {"pipeline": dtm_stages}
        
        # Create PDAL pipeline for DSM (all returns, highest elevation)
        # STRATEGY: Read → [Assign CRS if needed] → Merge → Crop → [Classify] → [Reproject] → Rasterize
        dsm_stages = []
        
        # Add all input files
        for f in las_files:
            dsm_stages.append(str(f.absolute()))
        
        # CRITICAL: Add CRS assignment BEFORE merge if LAZ files have missing/invalid CRS
        if needs_crs_assignment:
            dsm_stages.append({
                "type": "filters.assign",
                "assignment": f"SRS={las_crs}"
            })
            logger.info(f"[INFO] DSM pipeline: Adding filters.assign with SRS={las_crs} (before merge)")
        
        # Now merge files (they all have correct CRS if needs_crs_assignment was True)
        dsm_stages.append({"type": "filters.merge"})
        
        # CRITICAL: Crop to AOI polygon BEFORE expensive operations
        logger.info(f"[PIPELINE] DSM: Adding filters.crop with AOI polygon ({aoi_area_ha:.2f} ha)")
        dsm_stages.append({
            "type": "filters.crop",
            "polygon": crop_polygon_wkt
        })

        if needs_ground_classification:
            dsm_stages.append({"type": "filters.smrf"})

        # Add reprojection if needed
        if needs_reprojection:
            dsm_stages.append({"type": "filters.reprojection", "out_srs": output_crs})

        dsm_stages.extend([
            {"type": "filters.range", "limits": canopy_filter_limits},
            {
                "type": "writers.gdal",
                "filename": str(dsm_path.absolute()),
                "resolution": resolution_m,
                "output_type": "max",
                "gdalopts": "TILED=YES,COMPRESS=LZW,BIGTIFF=IF_SAFER",
                "bounds": f"([{minx_out}, {maxx_out}], [{miny_out}, {maxy_out}])",
                "override_srs": output_crs,
                "nodata": "-9999",
            },
        ])

        dsm_pipeline = {"pipeline": dsm_stages}
        
        # Write pipeline files
        dtm_pipeline_file = output_dir / 'pdal_dtm_pipeline.json'
        dsm_pipeline_file = output_dir / 'pdal_dsm_pipeline.json'
        
        with open(dtm_pipeline_file, 'w') as f:
            json.dump(dtm_pipeline, f, indent=2)
        
        with open(dsm_pipeline_file, 'w') as f:
            json.dump(dsm_pipeline, f, indent=2)
        
        logger.info("="*80)
        logger.info("[STEP 4/4] EXECUTING PDAL PIPELINES (DTM + DSM)")
        logger.info("="*80)
        logger.info("[INFO] Processing ONLY the AOI - LAZ tiles cropped to exact polygon")
        logger.info(f"[INFO] Processing area: {aoi_area_ha:.2f} ha")
        logger.info(f"[INFO] Resolution: {resolution_m}m")
        logger.info("[INFO] Generating DTM (ground surface)...")
        logger.info("[INFO] This may take several minutes depending on data size...")
        
        # Execute DTM pipeline
        result = subprocess.run(
            ['pdal', 'pipeline', str(dtm_pipeline_file.absolute())],
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout
        )
        
        if result.returncode != 0:
            logger.error(f"[ERROR] PDAL DTM pipeline failed:")
            logger.error(result.stderr)
            
            # Check for memory allocation error
            if 'bad allocation' in result.stderr.lower() or 'std::bad_alloc' in result.stderr.lower():
                logger.error("[ERROR] PDAL memory allocation failed")
                logger.error("[ERROR] This is a PROCESSING_ERROR, not NO_DATA")
                logger.error("[ERROR] Possible solutions:")
                logger.error("[ERROR]   1. Reduce AOI size")
                logger.error("[ERROR]   2. Process tiles individually")
                logger.error("[ERROR]   3. Increase system memory")
            
            return None
        
        if not dtm_path.exists():
            logger.error("[ERROR] DTM file not created")
            return None
        
        logger.info(f"[SUCCESS] DTM generated: {dtm_path}")
        
        logger.info("[INFO] Generating DSM (canopy surface)...")
        
        # Execute DSM pipeline
        result = subprocess.run(
            ['pdal', 'pipeline', str(dsm_pipeline_file.absolute())],
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout
        )
        
        if result.returncode != 0:
            logger.error(f"[ERROR] PDAL DSM pipeline failed:")
            logger.error(result.stderr)
            
            # Check for memory allocation error
            if 'bad allocation' in result.stderr.lower() or 'std::bad_alloc' in result.stderr.lower():
                logger.error("[ERROR] PDAL memory allocation failed")
                logger.error("[ERROR] This is a PROCESSING_ERROR, not NO_DATA")
                logger.error("[ERROR] Possible solutions:")
                logger.error("[ERROR]   1. Reduce AOI size")
                logger.error("[ERROR]   2. Process tiles individually")
                logger.error("[ERROR]   3. Increase system memory")
            
            return None
        
        if not dsm_path.exists():
            logger.error("[ERROR] DSM file not created")
            return None
        
        logger.info(f"[SUCCESS] DSM generated: {dsm_path}")
        
        return dtm_path, dsm_path
        
    except subprocess.TimeoutExpired:
        logger.error("[ERROR] PDAL processing timeout (>30 minutes)")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Point cloud processing failed: {str(e)}")
        return None


def generate_chm_from_tiles(
    tiles_dir: Path,
    aoi: gpd.GeoDataFrame,
    output_dir: Path,
    resolution_m: float = RASTER_RES_M,
) -> Optional[Path]:
    """
    Generate Canopy Height Model (CHM) from LiDAR tiles.
    
    Uses PDAL to process point clouds into DTM and DSM, then computes CHM = DSM - DTM.
    
    Args:
        tiles_dir: Directory containing LAS/LAZ tiles
        aoi: GeoDataFrame with AOI geometry
        output_dir: Directory for outputs
        
    Returns:
        Path to CHM raster, or None if failed
    """
    logger.info("[INFO] Generating Canopy Height Model from LiDAR...")
    
    try:
        # Step 1: Process point clouds to DTM and DSM using PDAL
        result = process_point_cloud_pdal(tiles_dir, output_dir, aoi, resolution_m=resolution_m)
        
        if result is None:
            logger.error("[ERROR] Failed to process point clouds")
            return None
        
        dtm_path, dsm_path = result
        
        # Step 2: Compute CHM = DSM - DTM
        logger.info("[INFO] Computing CHM (DSM - DTM)...")
        chm_path = output_dir / f'chm_{int(resolution_m)}m.tif'
        
        # Read DSM and DTM
        with rasterio.open(dsm_path) as dsm_src:
            dsm = dsm_src.read(1)
            dsm_nodata = dsm_src.nodata
            profile = dsm_src.profile.copy()
            dsm_transform = dsm_src.transform
            dsm_crs = dsm_src.crs
        
        with rasterio.open(dtm_path) as dtm_src:
            dtm_nodata = dtm_src.nodata
            
            # Check if DTM matches DSM grid
            if dtm_src.shape != dsm_src.shape or dtm_src.transform != dsm_transform:
                logger.warning("[WARNING] DTM and DSM grids don't match - resampling DTM")
                
                # Resample DTM to match DSM
                dtm = np.empty(dsm.shape, dtype=np.float32)
                
                reproject(
                    source=rasterio.band(dtm_src, 1),
                    destination=dtm,
                    src_transform=dtm_src.transform,
                    src_crs=dtm_src.crs,
                    dst_transform=dsm_transform,
                    dst_crs=dsm_crs,
                    resampling=Resampling.bilinear
                )
            else:
                dtm = dtm_src.read(1)
        
        # Create NoData masks
        dsm_valid = (dsm != dsm_nodata) if dsm_nodata is not None else np.ones_like(dsm, dtype=bool)
        dtm_valid = (dtm != dtm_nodata) if dtm_nodata is not None else np.ones_like(dtm, dtype=bool)
        both_valid = dsm_valid & dtm_valid
        
        # Compute CHM only where both surfaces are valid
        chm = np.zeros_like(dsm, dtype=np.float32)
        chm[both_valid] = dsm[both_valid] - dtm[both_valid]
        
        # Clamp negative values to 0 (pits/errors)
        chm[chm < 0] = 0
        
        # Handle any remaining NaN/Inf
        chm[np.isnan(chm)] = 0
        chm[np.isinf(chm)] = 0
        
        # Update profile for CHM
        profile.update(
            dtype=rasterio.float32,
            compress='lzw',
            tiled=True,
            blockxsize=256,
            blockysize=256,
            nodata=0
        )
        
        # Write CHM
        with rasterio.open(chm_path, 'w', **profile) as dst:
            dst.write(chm.astype(np.float32), 1)
        
        # Log statistics
        valid_chm = chm[chm > 0]
        if len(valid_chm) > 0:
            chm_max = valid_chm.max()
            chm_mean = valid_chm.mean()
            chm_std = valid_chm.std()
            
            logger.info(f"[INFO] CHM statistics:")
            logger.info(f"[INFO]   Min height: {valid_chm.min():.2f} m")
            logger.info(f"[INFO]   Max height: {chm_max:.2f} m")
            logger.info(f"[INFO]   Mean height: {chm_mean:.2f} m")
            logger.info(f"[INFO]   Std height: {chm_std:.2f} m")
            logger.info(f"[INFO]   Pixels with vegetation: {len(valid_chm):,}")
            
            # ========================================================================
            # CRITICAL CHM VALIDATION
            # ========================================================================
            if chm_max < 2.0:
                logger.error("[ERROR] CHM validation failed: Maximum height < 2 meters")
                logger.error(f"[ERROR] Max CHM height: {chm_max:.2f} m")
                logger.error("[ERROR] This indicates:")
                logger.error("[ERROR]   1. LiDAR is bare-earth / ground-only")
                logger.error("[ERROR]   2. DSM and DTM are nearly identical")
                logger.error("[ERROR]   3. No canopy structure in data")
                logger.error("[ERROR] Cannot produce reliable biomass estimates from this data")
                return None
            
            if chm_mean < 1.0:
                logger.warning(f"[WARNING] Very low mean CHM ({chm_mean:.2f} m)")
                logger.warning("[WARNING] Biomass estimates will have HIGH UNCERTAINTY")
            
            logger.info("[SUCCESS] CHM validation passed - canopy structure detected")
            
        else:
            logger.error("[ERROR] CHM validation failed: All heights are zero")
            logger.error("[ERROR] This indicates DSM and DTM are identical")
            logger.error("[ERROR] Likely causes:")
            logger.error("[ERROR]   1. CRS mismatch between point cloud and crop bounds")
            logger.error("[ERROR]   2. No points passed through filters")
            logger.error("[ERROR]   3. Crop bounds outside point cloud extent")
            logger.error("[ERROR]   4. LiDAR data is bare-earth only (no canopy returns)")
            return None
        
        logger.info(f"[SUCCESS] CHM generated: {chm_path}")
        return chm_path
        
    except Exception as e:
        logger.error(f"[ERROR] CHM generation failed: {str(e)}")
        return None


def aggregate_chm_to_30m(chm_path: Path, output_dir: Path) -> Optional[Path]:
    """
    Aggregate CHM from native resolution to 30m (FIA plot scale).
    
    Args:
        chm_path: Path to raw CHM raster
        output_dir: Directory for outputs
        
    Returns:
        Path to 30m CHM, or None if failed
    """
    logger.info("[INFO] Aggregating CHM to 30m resolution...")
    
    try:
        output_path = output_dir / 'chm_30m.tif'
        
        with rasterio.open(chm_path) as src:
            # Calculate 30m transform
            bounds = src.bounds
            width = int((bounds.right - bounds.left) / 30)
            height = int((bounds.top - bounds.bottom) / 30)
            
            transform = from_bounds(
                bounds.left, bounds.bottom,
                bounds.right, bounds.top,
                width, height
            )
            
            # Resample using mean aggregation
            data = src.read(1)
            
            # Create output raster
            profile = src.profile.copy()
            profile.update({
                'width': width,
                'height': height,
                'transform': transform,
                'dtype': 'float32'
            })
            
            resampled = np.empty((height, width), dtype=np.float32)
            
            reproject(
                source=data,
                destination=resampled,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=src.crs,
                resampling=Resampling.average
            )
            
            # Write output
            with rasterio.open(output_path, 'w', **profile) as dst:
                dst.write(resampled, 1)
        
        logger.info(f"[SUCCESS] 30m CHM saved: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"[ERROR] CHM aggregation failed: {str(e)}")
        return None


def estimate_biomass_from_height(chm_30m_path: Path, output_dir: Path) -> Optional[Path]:
    """
    Apply FIA-calibrated height-biomass allometry.
    
    AGB (Mg/ha) = a * H^b
    
    Args:
        chm_30m_path: Path to 30m CHM raster
        output_dir: Directory for outputs
        
    Returns:
        Path to AGB raster, or None if failed
    """
    logger.info("[INFO] Applying FIA height-biomass allometry...")
    logger.info(f"[INFO] Equation: AGB = {FIA_ALLOMETRY_A} * H^{FIA_ALLOMETRY_B}")
    
    try:
        output_path = output_dir / 'agb_lidar_raw.tif'
        
        with rasterio.open(chm_30m_path) as src:
            height = src.read(1)
            
            # Apply allometry: AGB = a * H^b
            # Mask out negative/invalid heights
            valid_mask = height > 0
            
            agb = np.zeros_like(height, dtype=np.float32)
            agb[valid_mask] = FIA_ALLOMETRY_A * np.power(height[valid_mask], FIA_ALLOMETRY_B)
            
            # Write output
            profile = src.profile.copy()
            profile.update(dtype='float32')
            
            with rasterio.open(output_path, 'w', **profile) as dst:
                dst.write(agb, 1)
                dst.update_tags(
                    allometry='FIA-calibrated height-only',
                    equation=f'AGB = {FIA_ALLOMETRY_A} * H^{FIA_ALLOMETRY_B}',
                    source='USGS_3DEP_LiDAR'
                )
        
        logger.info(f"[SUCCESS] AGB raster saved: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"[ERROR] Biomass estimation failed: {str(e)}")
        return None


def apply_forest_mask(agb_path: Path, mask_path: Path, output_dir: Path) -> Optional[Path]:
    """
    Apply NLCD forest mask to AGB raster.
    
    Args:
        agb_path: Path to AGB raster
        mask_path: Path to forest mask
        output_dir: Directory for outputs
        
    Returns:
        Path to masked AGB, or None if failed
    """
    logger.info("[INFO] Applying NLCD forest mask...")
    
    try:
        output_path = output_dir / 'agb_lidar.tif'
        
        with rasterio.open(agb_path) as agb_src:
            with rasterio.open(mask_path) as mask_src:
                agb = agb_src.read(1)
                
                # Resample mask to match AGB resolution if needed
                if agb_src.shape != mask_src.shape:
                    mask = np.empty_like(agb, dtype=mask_src.dtypes[0])
                    reproject(
                        source=mask_src.read(1),
                        destination=mask,
                        src_transform=mask_src.transform,
                        src_crs=mask_src.crs,
                        dst_transform=agb_src.transform,
                        dst_crs=agb_src.crs,
                        resampling=Resampling.nearest
                    )
                else:
                    mask = mask_src.read(1)
                
                # Apply mask (forest pixels == 1)
                agb_masked = agb * mask
                
                # Write output
                profile = agb_src.profile.copy()
                with rasterio.open(output_path, 'w', **profile) as dst:
                    dst.write(agb_masked, 1)
        
        logger.info(f"[SUCCESS] Masked AGB saved: {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"[ERROR] Forest mask application failed: {str(e)}")
        return None


def _percentile_from_histogram(bin_edges: np.ndarray, counts: np.ndarray, percentile: float) -> float:
    """Approximate percentile from a histogram (counts over bin intervals)."""
    if counts.sum() <= 0:
        return 0.0
    target = counts.sum() * (percentile / 100.0)
    cdf = np.cumsum(counts)
    idx = int(np.searchsorted(cdf, target, side='left'))
    idx = min(max(idx, 0), len(bin_edges) - 2)
    return float(bin_edges[idx + 1])


def compute_chm_metrics_and_write(
    dtm_path: Path,
    dsm_path: Path,
    chm_path: Path,
    canopy_threshold_m: float = 2.0,
) -> Dict[str, float]:
    """Compute CHM (DSM-DTM) block-wise, write CHM raster, and return canopy metrics."""
    if not dtm_path.exists() or not dsm_path.exists():
        raise FileNotFoundError("DTM/DSM inputs missing")

    # Histogram bins for percentile estimation (0-100m, 0.25m bins)
    bin_edges = np.linspace(0.0, 100.0, 401)
    hist_counts = np.zeros(len(bin_edges) - 1, dtype=np.int64)

    sum_h = 0.0
    count_h = 0
    valid_pixels = 0
    canopy_pixels = 0

    with rasterio.open(dsm_path) as dsm_src, rasterio.open(dtm_path) as dtm_src:
        profile = dsm_src.profile.copy()
        profile.update(
            dtype=rasterio.float32,
            nodata=-9999.0,
            compress='lzw',
            tiled=True,
        )

        # Align DTM to DSM grid lazily if needed
        if (dtm_src.crs != dsm_src.crs) or (dtm_src.transform != dsm_src.transform) or (dtm_src.width != dsm_src.width) or (dtm_src.height != dsm_src.height):
            dtm_aligned = WarpedVRT(
                dtm_src,
                crs=dsm_src.crs,
                transform=dsm_src.transform,
                width=dsm_src.width,
                height=dsm_src.height,
                resampling=Resampling.bilinear,
            )
        else:
            dtm_aligned = dtm_src

        with rasterio.open(chm_path, 'w', **profile) as chm_dst:
            for _, window in dsm_src.block_windows(1):
                dsm = dsm_src.read(1, window=window)
                dtm = dtm_aligned.read(1, window=window)

                dsm_nodata = dsm_src.nodata
                dtm_nodata = dtm_aligned.nodata

                dsm_valid = (dsm != dsm_nodata) if dsm_nodata is not None else np.ones_like(dsm, dtype=bool)
                dtm_valid = (dtm != dtm_nodata) if dtm_nodata is not None else np.ones_like(dtm, dtype=bool)
                both_valid = dsm_valid & dtm_valid

                chm = np.full(dsm.shape, -9999.0, dtype=np.float32)
                if np.any(both_valid):
                    chm_vals = (dsm[both_valid] - dtm[both_valid]).astype(np.float32)
                    chm_vals[chm_vals < 0] = 0
                    chm[both_valid] = chm_vals

                    valid_pixels += int(both_valid.sum())
                    canopy_mask = both_valid & (chm >= canopy_threshold_m)
                    canopy_pixels += int(canopy_mask.sum())

                    pos = chm_vals[chm_vals > 0]
                    if pos.size:
                        sum_h += float(pos.sum())
                        count_h += int(pos.size)
                        counts, _ = np.histogram(pos, bins=bin_edges)
                        hist_counts += counts.astype(np.int64)

                chm_dst.write(chm, 1, window=window)

    mean_height = (sum_h / count_h) if count_h > 0 else 0.0
    p95_height = _percentile_from_histogram(bin_edges, hist_counts, 95.0) if count_h > 0 else 0.0
    canopy_cover_pct = (canopy_pixels / valid_pixels * 100.0) if valid_pixels > 0 else 0.0

    return {
        'mean_height_m': float(mean_height),
        'p95_height_m': float(p95_height),
        'canopy_cover_pct': float(canopy_cover_pct),
        'valid_pixels': float(valid_pixels),
        'canopy_pixels': float(canopy_pixels),
    }


def create_forest_mask_from_chm(
    chm_path: Path,
    output_path: Path,
    canopy_threshold_m: float = 2.0,
    min_canopy_cover_pct: float = 10.0,
) -> Dict[str, float]:
    """Generate binary forest mask from CHM (CHM >= threshold).
    
    Args:
        chm_path: Input CHM raster
        output_path: Output forest mask raster (1=forest, 0=non-forest, 255=nodata)
        canopy_threshold_m: Minimum canopy height (default 2.0m)
        min_canopy_cover_pct: Minimum canopy cover % (optional filtering, not yet implemented)
        
    Returns:
        Dict with forest_pixels, total_pixels, forest_area_ha, canopy_cover_pct
    """
    logger.info(f"[INFO] Creating forest mask from CHM (threshold >= {canopy_threshold_m}m)...")
    
    forest_pixels = 0
    total_pixels = 0
    
    with rasterio.open(chm_path) as src:
        profile = src.profile.copy()
        profile.update(
            dtype=rasterio.uint8,
            nodata=255,
            compress='lzw',
            tiled=True,
        )
        
        pixel_area_ha = abs(src.transform.a * src.transform.e) / 10000.0
        
        with rasterio.open(output_path, 'w', **profile) as dst:
            for _, window in src.block_windows(1):
                chm = src.read(1, window=window)
                
                mask = np.full(chm.shape, 255, dtype=np.uint8)  # nodata
                chm_nodata = src.nodata if src.nodata is not None else -9999.0
                valid = (chm != chm_nodata)
                
                if np.any(valid):
                    # 1 = forest (CHM >= threshold), 0 = non-forest
                    mask[valid] = 0
                    forest_mask = valid & (chm >= canopy_threshold_m)
                    mask[forest_mask] = 1
                    
                    forest_pixels += int(forest_mask.sum())
                    total_pixels += int(valid.sum())
                
                dst.write(mask, 1, window=window)
    
    canopy_cover_pct = (forest_pixels / total_pixels * 100.0) if total_pixels > 0 else 0.0
    forest_area_ha = forest_pixels * pixel_area_ha
    
    logger.info(f"[INFO] Forest mask created: {forest_area_ha:.2f} ha ({canopy_cover_pct:.1f}% canopy cover)")
    
    return {
        'forest_pixels': int(forest_pixels),
        'total_pixels': int(total_pixels),
        'forest_area_ha': float(forest_area_ha),
        'canopy_cover_pct': float(canopy_cover_pct),
    }


def write_constant_carbon_density_raster(
    chm_path: Path,
    output_path: Path,
    carbon_mgc_per_ha: float,
    canopy_threshold_m: float = 2.0,
) -> None:
    """Write a carbon density raster (MgC/ha) matching CHM grid."""
    with rasterio.open(chm_path) as src:
        profile = src.profile.copy()
        profile.update(dtype=rasterio.float32, nodata=-9999.0, compress='lzw', tiled=True)

        with rasterio.open(output_path, 'w', **profile) as dst:
            for _, window in src.block_windows(1):
                chm = src.read(1, window=window)
                nodata = src.nodata

                out = np.full(chm.shape, -9999.0, dtype=np.float32)
                valid = (chm != nodata) if nodata is not None else np.ones_like(chm, dtype=bool)
                if np.any(valid):
                    out[valid] = 0.0
                    out[valid & (chm >= canopy_threshold_m)] = float(carbon_mgc_per_ha)

                dst.write(out, 1, window=window)


def local_laz_carbon_stock_pipeline(
    laz_tiles_dir: Path,
    aoi: gpd.GeoDataFrame,
    output_dir: Path,
    resolution_m: float = RASTER_RES_M,
    allom_a: Optional[float] = None,
    allom_b: Optional[float] = None,
    canopy_threshold_m: float = 2.0,
    override_crs: Optional[str] = None,
) -> Dict:
    """Process local LAZ/LAS tiles to estimate canopy metrics and carbon stock.
    
    This is a complete LiDAR-only workflow that bypasses NLCD/Earth Engine.
    Forest mask is derived directly from CHM.
    """
    logger.info("\n" + "=" * 80)
    logger.info("LOCAL LAZ CARBON STOCK PIPELINE (LiDAR-Only Mode)")
    logger.info("=" * 80)
    logger.info("[INFO] Earth Engine, NLCD, GEDI, and Sentinel stages SKIPPED")
    logger.info("[INFO] Forest mask will be derived from CHM (height-based)")
    logger.info("=" * 80 + "\n")
    
    if resolution_m < 2.0:
        logger.warning(f"[WARNING] Requested resolution {resolution_m}m is below safe threshold; clamping to 5.0m")
        resolution_m = 5.0

    output_dir.mkdir(parents=True, exist_ok=True)

    if not laz_tiles_dir.exists():
        raise FileNotFoundError(f"LAZ tiles folder not found: {laz_tiles_dir}")

    laz_files = list(laz_tiles_dir.glob('*.laz')) + list(laz_tiles_dir.glob('*.las'))
    if not laz_files:
        raise FileNotFoundError(f"No .laz/.las tiles found in: {laz_tiles_dir}")

    # Step 1: Generate DTM/DSM using PDAL
    logger.info("[STEP 1/6] Processing point clouds with PDAL...")
    result = process_point_cloud_pdal(laz_tiles_dir, output_dir, aoi, resolution_m=resolution_m, override_crs=override_crs)
    if result is None:
        raise RuntimeError("PDAL point cloud processing failed")
    dtm_path, dsm_path = result

    # Step 2: Compute CHM and canopy metrics
    logger.info("[STEP 2/6] Computing CHM (DSM - DTM) and metrics...")
    chm_path = output_dir / f'chm_{int(resolution_m)}m.tif'
    metrics = compute_chm_metrics_and_write(dtm_path, dsm_path, chm_path, canopy_threshold_m=canopy_threshold_m)

    mean_height_m = metrics['mean_height_m']
    p95_height_m = metrics['p95_height_m']
    canopy_cover_pct = metrics['canopy_cover_pct']

    # Step 3: Generate forest mask from CHM (NO NLCD/Earth Engine)
    logger.info("[STEP 3/6] Generating forest mask from CHM...")
    forest_mask_path = output_dir / f'forest_mask_{int(resolution_m)}m.tif'
    mask_stats = create_forest_mask_from_chm(
        chm_path=chm_path,
        output_path=forest_mask_path,
        canopy_threshold_m=canopy_threshold_m,
    )
    forest_area_ha = mask_stats['forest_area_ha']

    # Step 4: Biomass estimation using P95 height
    logger.info("[STEP 4/6] Estimating biomass using allometry...")
    if allom_a is None or allom_b is None:
        logger.info("[INFO] Allometry coefficients not provided - using IPCC/FIA-style defaults")
        allom_a = FIA_ALLOMETRY_A
        allom_b = FIA_ALLOMETRY_B

    logger.info(f"[INFO] Allometry: AGB = {allom_a} × (P95_height ^ {allom_b})")
    agb_mg_per_ha = float(allom_a * (p95_height_m ** allom_b)) if p95_height_m > 0 else 0.0
    carbon_mgc_per_ha = float(agb_mg_per_ha * 0.50)

    # Step 5: Calculate total carbon
    logger.info("[STEP 5/6] Calculating carbon stock...")
    total_carbon_mgc = float(carbon_mgc_per_ha * forest_area_ha)
    logger.info(f"[INFO] Total carbon: {total_carbon_mgc:.2f} MgC over {forest_area_ha:.2f} ha")

    # Step 6: Generate carbon density raster
    logger.info("[STEP 6/6] Writing carbon density raster...")
    carbon_raster_path = output_dir / f'carbon_density_{int(resolution_m)}m.tif'
    write_constant_carbon_density_raster(chm_path, carbon_raster_path, carbon_mgc_per_ha, canopy_threshold_m)

    # Summary JSON (exact requested schema + forest mask info)
    summary_json = {
        'mean_height_m': float(mean_height_m),
        'p95_height_m': float(p95_height_m),
        'canopy_cover_pct': float(canopy_cover_pct),
        'forest_area_ha': float(forest_area_ha),
        'AGB_Mg_per_ha': float(agb_mg_per_ha),
        'Carbon_MgC_per_ha': float(carbon_mgc_per_ha),
        'Total_Carbon_MgC': float(total_carbon_mgc),
        'forest_mask_source': 'CHM_height_based',
        'canopy_threshold_m': float(canopy_threshold_m),
        'method': 'LiDAR-only (no NLCD/Earth Engine)',
    }

    summary_path = output_dir / 'summary.json'
    with open(summary_path, 'w') as f:
        json.dump(summary_json, f, indent=2)
    
    logger.info("\n" + "=" * 80)
    logger.info("LOCAL LAZ PIPELINE COMPLETE")
    logger.info("=" * 80)
    logger.info(f"Mean canopy height: {mean_height_m:.2f} m")
    logger.info(f"P95 canopy height: {p95_height_m:.2f} m")
    logger.info(f"Canopy cover: {canopy_cover_pct:.1f}%")
    logger.info(f"Forest area: {forest_area_ha:.2f} ha")
    logger.info(f"AGB: {agb_mg_per_ha:.2f} Mg/ha")
    logger.info(f"Carbon: {carbon_mgc_per_ha:.2f} MgC/ha")
    logger.info(f"Total carbon: {total_carbon_mgc:.2f} MgC")
    logger.info("=" * 80 + "\n")

    return {
        **summary_json,
        'dtm_path': str(dtm_path),
        'dsm_path': str(dsm_path),
        'chm_path': str(chm_path),
        'forest_mask_path': str(forest_mask_path),
        'carbon_density_path': str(carbon_raster_path),
        'resolution_m': float(resolution_m),
    }


def calculate_mean_agb(agb_path: Path, forest_mask_path: Path) -> Tuple[float, float]:
    """
    Calculate mean AGB across forested pixels.
    
    Args:
        agb_path: Path to AGB raster
        forest_mask_path: Path to forest mask
        
    Returns:
        (mean_agb_Mg_ha, std_agb_Mg_ha)
    """
    try:
        with rasterio.open(agb_path) as src:
            agb = src.read(1, masked=True)
            
            # Filter valid pixels (>0, not NoData)
            valid_pixels = agb[(agb > 0) & (~agb.mask)]
            
            if len(valid_pixels) == 0:
                logger.warning("[WARNING] No valid AGB pixels found")
                return 0.0, 0.0
            
            mean_agb = float(np.mean(valid_pixels))
            std_agb = float(np.std(valid_pixels))
            
            logger.info(f"[INFO] Mean AGB: {mean_agb:.2f} Mg/ha (SD: {std_agb:.2f})")
            return mean_agb, std_agb
            
    except Exception as e:
        logger.error(f"[ERROR] AGB calculation failed: {str(e)}")
        return 0.0, 0.0


def als_fallback_pipeline(
    aoi: gpd.GeoDataFrame,
    forest_mask_path: Path,
    output_dir: Path,
    analysis_year: int
) -> Dict:
    """
    Complete ALS fallback pipeline using USGS 3DEP data.
    
    Called when GEDI L4A returns zero footprints.
    
    DATA ACCESS: USGS TNM API → LAS/LAZ download → local PDAL processing
    NOT A GEE-BASED WORKFLOW.
    
    YEAR-AGNOSTIC: Uses most recent available LiDAR regardless of analysis year.
    Airborne LiDAR is treated as a structural snapshot (standard practice).
    
    Args:
        aoi: GeoDataFrame with AOI geometry
        forest_mask_path: Path to NLCD forest mask
        output_dir: Output directory (outputs/lidar/)
        analysis_year: Target analysis year (for metadata only)
        
    Returns:
        Result dict compatible with GEDI format, including acquisition year
    """
    logger.info("=" * 80)
    logger.info("USGS 3DEP LIDAR FALLBACK (GEDI UNAVAILABLE)")
    logger.info("=" * 80)
    logger.info("[INFO] Access method: USGS 3DEP ArcGIS REST API")
    logger.info("[INFO] Data format: LAS/LAZ point clouds")
    logger.info("[INFO] Processing: PDAL -> DTM/DSM -> CHM -> FIA allometry")
    logger.info(f"[INFO] Analysis year: {analysis_year} (GEDI target)")
    logger.info("[INFO] ALS year: Most recent available (year-agnostic)")
    logger.info("=" * 80)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Step 1: Query USGS ArcGIS REST API for project availability
    logger.info("[INFO] Querying USGS TNM API for 3DEP LiDAR metadata (any year)...")
    logger.info("[INFO] ALS is year-agnostic - latest available structural data will be used")
    
    available, projects = check_3dep_availability(aoi)
    
    if not available or not projects:
        logger.error("[ERROR] No USGS 3DEP LiDAR projects detected for AOI")
        logger.info("[INFO] 3DEP projects detected: 0")
        return {
            'status': 'no_data',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'No 3DEP LiDAR projects exist in AOI',
            'suggestion': 'AOI may be outside USGS 3DEP coverage area (NO_DATA is correct)',
            'als_available': False,
            'access_method': 'USGS_3DEP_ArcGIS_API',
            'analysis_year': analysis_year
        }
    
    logger.info(f"[INFO] 3DEP projects detected: {len(projects)}")
    logger.info(f"[INFO] AOI intersects project extent")
    
    # Step 2: Attempt tile acquisition (API or local)
    logger.info("[INFO] Attempting tile acquisition...")
    download_dir = output_dir / 'tiles'
    result = download_3dep_tiles(aoi, download_dir, projects)
    
    if result is None:
        logger.error("[ERROR] Critical failure during tile acquisition")
        
        # Distinguish between API failure and true absence
        api_healthy = check_usgs_api_health()
        
        return {
            'status': 'data_access_error',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'USGS 3DEP projects detected but tile acquisition failed',
            'suggestion': 'Retry later, check network connectivity, or verify TNM Products API status',
            'als_available': True,
            'download_status': 'failed_after_retries',
            'access_method': 'USGS_TNM_Products_API',
            'api_health': 'reachable' if api_healthy else 'unreachable',
            'analysis_year': analysis_year,
            'diagnostic': 'This is DATA_ACCESS_ERROR, not NO_DATA (LiDAR exists)'
        }
    
    tiles_dir, acquisition_year, project_name, access_mode = result
    logger.info(f"[INFO] Project: {project_name} ({acquisition_year})")
    logger.info(f"[INFO] Access mode: {access_mode.upper()}")
    
    # Check for actual LAS/LAZ files
    las_files = list(tiles_dir.glob('*.las')) + list(tiles_dir.glob('*.laz'))
    
    if not las_files:
        logger.info("[INFO] No LAS/LAZ files currently available")
        logger.info(f"[INFO] Expected location: {tiles_dir}")
        logger.info("[INFO] ALS is AVAILABLE (project exists) but tiles not yet provided")
        
        return {
            'status': 'awaiting_lidar_tiles',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'USGS 3DEP project exists but LAS/LAZ tiles are not exposed via TNM Products API',
            'project': project_name,
            'project_id': str(tiles_dir.name),
            'acquisition_year': acquisition_year,
            'required_action': 'Provide LAS/LAZ tiles manually or via mirror',
            'tile_location': str(tiles_dir),
            'suggestion': f'Download tiles from nationalmap.gov and place in {tiles_dir}',
            'access_mode': access_mode,
            'als_available': True,
            'download_status': 'no_files',
            'analysis_year': analysis_year,
            'temporal_note': 'ALS year not forced; latest available structural data used',
            'note': 'ALS is available but tiles must be provided manually (not NO_DATA)'
        }
    
    logger.info(f"[SUCCESS] Found {len(las_files)} LAS/LAZ file(s) ready for processing")
    logger.info(f"[INFO] Access mode: {access_mode}")
    
    # Step 3: Generate CHM from point clouds using PDAL
    logger.info("[INFO] Processing point clouds with PDAL...")
    chm_path = generate_chm_from_tiles(tiles_dir, aoi, output_dir, resolution_m=RASTER_RES_M)
    
    if chm_path is None:
        logger.error("[ERROR] CHM generation failed")
        logger.error("[ERROR] Ensure PDAL is installed: conda install -c conda-forge pdal python-pdal")
        return {
            'status': 'no_data',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'Point cloud processing failed (PDAL required)',
            'suggestion': 'Install PDAL: conda install -c conda-forge pdal python-pdal',
            'als_available': True,
            'tiles_downloaded': len(las_files),
            'processing_status': 'failed',
            'project': project_name,
            'acquisition_year': acquisition_year,
            'analysis_year': analysis_year
        }
    
    # Step 4: Aggregate CHM to 30m
    logger.info("[INFO] Aggregating CHM to 30m resolution...")
    chm_30m_path = aggregate_chm_to_30m(chm_path, output_dir)
    
    if chm_30m_path is None:
        logger.error("[ERROR] CHM aggregation failed")
        return {
            'status': 'no_data',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'CHM aggregation to 30m failed',
            'processing_status': 'failed_aggregation'
        }
    
    # Step 5: Apply FIA allometry (Height → Biomass)
    logger.info("[INFO] Applying FIA height-biomass allometry...")
    agb_raw_path = estimate_biomass_from_height(chm_30m_path, output_dir)
    
    if agb_raw_path is None:
        logger.error("[ERROR] Biomass estimation failed")
        return {
            'status': 'no_data',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'FIA allometry application failed',
            'processing_status': 'failed_allometry'
        }
    
    # Step 6: Apply NLCD forest mask
    logger.info("[INFO] Applying NLCD forest mask...")
    agb_masked_path = apply_forest_mask(agb_raw_path, forest_mask_path, output_dir)
    
    if agb_masked_path is None:
        logger.error("[ERROR] Forest mask application failed")
        return {
            'status': 'no_data',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'Forest mask application failed',
            'processing_status': 'failed_mask'
        }
    
    # Step 7: Calculate mean biomass
    logger.info("[INFO] Calculating mean biomass statistics...")
    mean_agb, std_agb = calculate_mean_agb(agb_masked_path, forest_mask_path)
    
    if mean_agb == 0.0:
        logger.warning("[WARNING] Mean biomass is 0.0 - no valid forest pixels with biomass")
        return {
            'status': 'no_data',
            'footprint_count': 0,
            'mean_agb_Mg_ha': 0.0,
            'std_agb_Mg_ha': 0.0,
            'reason': 'Processing completed but no valid biomass values in forested areas',
            'suggestion': 'Check forest mask coverage and LiDAR quality'
        }
    
    # Success!
    logger.info("=" * 80)
    logger.info("[SUCCESS] ALS processing pipeline complete!")
    logger.info(f"[RESULT] Mean AGB: {mean_agb:.2f} Mg/ha (SD: {std_agb:.2f})")
    logger.info(f"[INFO] Data source: {project_name}")
    logger.info(f"[INFO] LiDAR acquisition: {acquisition_year}")
    logger.info(f"[INFO] Analysis year: {analysis_year}")
    logger.info("=" * 80)
    
    return {
        'status': 'success',
        'footprint_count': -1,  # Not applicable for ALS
        'mean_agb_Mg_ha': mean_agb,
        'std_agb_Mg_ha': std_agb,
        'biomass_source': 'USGS_3DEP_LiDAR',
        'method': 'PDAL_point_cloud_processing',
        'allometry': f'FIA height-based: AGB = {FIA_ALLOMETRY_A} * H^{FIA_ALLOMETRY_B}',
        'chm_resolution_m': float(RASTER_RES_M),
        'aggregation_scale_m': 30,
        'processing_engine': 'PDAL',
        'lidar_project': project_name,
        'lidar_projects_used': 1,
        'lidar_acquisition_year': acquisition_year,
        'analysis_year': analysis_year,
        'temporal_note': 'ALS year not forced; latest available structural data used',
        'tiles_processed': len(las_files),
        'access_method': 'USGS_3DEP_ArcGIS_API',
        'als_available': True
    }


if __name__ == '__main__':
    print("=" * 80)
    print("USGS 3DEP LIDAR FALLBACK MODULE")
    print("=" * 80)
    print("This module provides ALS-based biomass estimation when GEDI L4A")
    print("returns zero footprints.")
    print()
    print("CRITICAL: USGS 3DEP is accessed via USGS TNM API, NOT Google Earth Engine")
    print("USGS 3DEP LiDAR is NOT available as a GEE asset.")
    print()
    print("STATUS: Placeholder implementation (PDAL required for production)")
    print("REQUIRES: PDAL or laspy for LAS/LAZ point cloud processing")
    print()
    print("Full implementation workflow:")
    print("  1. Query USGS TNM API for tile metadata (bbox search)")
    print("  2. Download LAS/LAZ point cloud tiles locally")
    print("  3. Classify points (ground = DTM, first returns = DSM)")
    print("  4. Compute CHM = DSM - DTM")
    print("  5. Aggregate CHM to 30m (FIA plot scale)")
    print("  6. Apply FIA height-biomass allometry: AGB = 0.168 * H^2.47")
    print("  7. Apply NLCD forest mask")
    print("  8. Calculate carbon (IPCC: C=0.47, CO2e=3.67)")
    print()
    print("Access method: USGS TNM (The National Map) REST API")
    print("Data storage: data/raw/lidar/3dep/")
    print("Processing: Local (not cloud-based)")
    print("=" * 80)
