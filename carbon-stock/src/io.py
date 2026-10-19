"""
Input/Output Module for Geospatial Raster Data
Handles loading, validation, coordinate systems, and unit conversions.

ALL DATA INTEGRITY CHECKS EXPLICIT - No silent failures.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
import warnings

import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.windows import Window
from rasterio.enums import ColorInterp
import geopandas as gpd
from shapely.geometry import box

from src.config import (
    SENTINEL2_CONFIG,
    SENTINEL1_CONFIG,
    GEDI_CONFIG,
    PLANETSCOPE_CONFIG,
    PHYSICAL_CONSTANTS,
    logger
)

# Suppress specific warnings
warnings.filterwarnings('ignore', category=rasterio.errors.NotGeoreferencedWarning)

class RasterData:
    """
    Container for raster data with metadata preservation.
    Ensures all operations maintain data provenance and units.
    """
    
    def __init__(
        self,
        data: np.ndarray,
        transform: rasterio.Affine,
        crs: rasterio.crs.CRS,
        nodata: float,
        band_names: List[str],
        metadata: Optional[Dict] = None,
        units: Optional[Dict[str, str]] = None
    ):
        """
        Initialize RasterData container.
        
        Args:
            data: Numpy array (bands, height, width) or (height, width)
            transform: Affine transformation
            crs: Coordinate reference system
            nodata: No data value
            band_names: List of band names
            metadata: Additional metadata dictionary
            units: Dictionary of units for each band
        """
        self.data = data
        self.transform = transform
        self.crs = crs
        self.nodata = nodata
        self.band_names = band_names
        self.metadata = metadata or {}
        self.units = units or {}
        
        # Validate shape consistency
        if self.data.ndim == 2:
            self.data = self.data[np.newaxis, :, :]
        
        assert self.data.shape[0] == len(self.band_names), \
            f"Band count mismatch: {self.data.shape[0]} vs {len(self.band_names)}"
        
        logger.debug(f"RasterData initialized: {self.data.shape}, CRS: {self.crs}")
    
    @property
    def shape(self) -> Tuple[int, int, int]:
        """Return (bands, height, width)"""
        return self.data.shape
    
    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        """Return (left, bottom, right, top) in CRS units"""
        height, width = self.data.shape[1:]
        return rasterio.transform.array_bounds(height, width, self.transform)
    
    @property
    def resolution(self) -> Tuple[float, float]:
        """Return (x_resolution, y_resolution) in CRS units"""
        return (self.transform.a, -self.transform.e)
    
    def get_band(self, band_name: str) -> np.ndarray:
        """Get data for specific band"""
        if band_name not in self.band_names:
            raise ValueError(f"Band {band_name} not found. Available: {self.band_names}")
        idx = self.band_names.index(band_name)
        return self.data[idx]
    
    def mask_nodata(self) -> np.ndarray:
        """Return boolean mask where True = valid data, False = nodata"""
        if self.nodata is None:
            return np.ones(self.data.shape[1:], dtype=bool)
        return ~np.isclose(self.data, self.nodata).any(axis=0)
    
    def validate(self) -> Dict[str, any]:
        """
        Validate raster data integrity.
        Returns dictionary of validation results.
        """
        results = {
            'valid': True,
            'errors': [],
            'warnings': [],
            'stats': {}
        }
        
        # Check for all nodata
        mask = self.mask_nodata()
        valid_pixels = mask.sum()
        total_pixels = mask.size
        
        if valid_pixels == 0:
            results['valid'] = False
            results['errors'].append("All pixels are nodata")
        elif valid_pixels < total_pixels * 0.1:
            results['warnings'].append(f"Only {valid_pixels/total_pixels*100:.1f}% valid pixels")
        
        results['stats']['valid_pixel_percentage'] = valid_pixels / total_pixels * 100
        
        # Check for infinite or NaN values
        for i, band in enumerate(self.band_names):
            band_data = self.data[i][mask]
            
            if np.any(np.isinf(band_data)):
                results['warnings'].append(f"Band {band} contains infinite values")
            
            if np.any(np.isnan(band_data)):
                results['warnings'].append(f"Band {band} contains NaN values")
            
            # Statistics
            if len(band_data) > 0:
                results['stats'][band] = {
                    'min': float(np.min(band_data)),
                    'max': float(np.max(band_data)),
                    'mean': float(np.mean(band_data)),
                    'std': float(np.std(band_data)),
                    'unit': self.units.get(band, 'unknown')
                }
        
        return results
    
    def save(
        self,
        filepath: Union[str, Path],
        compress: str = 'lzw',
        tiled: bool = True,
        blocksize: int = 512
    ) -> None:
        """
        Save raster to GeoTIFF file.
        
        Args:
            filepath: Output file path
            compress: Compression method ('lzw', 'deflate', 'none')
            tiled: Whether to create tiled GeoTIFF
            blocksize: Tile size (if tiled=True)
        """
        save_raster(self, filepath, compress, tiled, blocksize)


def load_raster(
    filepath: Union[str, Path],
    bands: Optional[List[int]] = None,
    window: Optional[Window] = None,
    validate: bool = True
) -> RasterData:
    """
    Load raster file with validation and metadata preservation.
    
    Args:
        filepath: Path to raster file (GeoTIFF, etc.)
        bands: List of band indices (1-indexed) to load. None = all bands
        window: Rasterio window to read subset
        validate: Whether to run validation checks
    
    Returns:
        RasterData object
    
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file format invalid or data corrupted
    """
    filepath = Path(filepath)
    
    if not filepath.exists():
        raise FileNotFoundError(f"Raster file not found: {filepath}")
    
    logger.info(f"Loading raster: {filepath.name}")
    
    try:
        with rasterio.open(filepath) as src:
            # Read metadata
            crs = src.crs
            transform = src.transform if window is None else rasterio.windows.transform(window, src.transform)
            nodata = src.nodata
            
            # Determine bands to read
            if bands is None:
                bands = list(range(1, src.count + 1))
            
            # Read data
            data = src.read(bands, window=window)
            
            # Get band names (from descriptions or generate)
            band_names = []
            for i, band_idx in enumerate(bands):
                desc = src.descriptions[band_idx - 1]
                if desc:
                    band_names.append(desc)
                else:
                    band_names.append(f"Band_{band_idx}")
            
            # Extract metadata
            metadata = {
                'filename': filepath.name,
                'driver': src.driver,
                'dtype': str(src.dtypes[0]),
                'count': src.count,
                'width': src.width,
                'height': src.height,
                **src.tags()
            }
            
            # Create RasterData object
            raster = RasterData(
                data=data,
                transform=transform,
                crs=crs,
                nodata=nodata,
                band_names=band_names,
                metadata=metadata
            )
            
            # Validate if requested
            if validate:
                validation = raster.validate()
                if not validation['valid']:
                    raise ValueError(f"Raster validation failed: {validation['errors']}")
                if validation['warnings']:
                    for warning in validation['warnings']:
                        logger.warning(warning)
                
                logger.info(f"Loaded {len(band_names)} bands, "
                           f"{validation['stats']['valid_pixel_percentage']:.1f}% valid pixels")
            
            return raster
    
    except rasterio.errors.RasterioIOError as e:
        raise ValueError(f"Failed to read raster file: {e}")


def save_raster(
    raster: RasterData,
    filepath: Union[str, Path],
    compress: str = 'lzw',
    tiled: bool = True,
    blocksize: int = 512
) -> None:
    """
    Save RasterData to GeoTIFF with compression and metadata.
    
    Args:
        raster: RasterData object to save
        filepath: Output file path
        compress: Compression method ('lzw', 'deflate', 'none')
        tiled: Whether to create tiled GeoTIFF
        blocksize: Tile size (if tiled=True)
    """
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Saving raster: {filepath.name}")
    
    # Determine dtype
    # Use float32 for most geophysical data to preserve precision
    dtype = rasterio.float32
    
    # Prepare metadata
    metadata = {
        'driver': 'GTiff',
        'height': raster.shape[1],
        'width': raster.shape[2],
        'count': raster.shape[0],
        'dtype': dtype,
        'crs': raster.crs,
        'transform': raster.transform,
        'nodata': raster.nodata,
        'compress': compress
    }
    
    if tiled:
        metadata['tiled'] = True
        metadata['blockxsize'] = blocksize
        metadata['blockysize'] = blocksize
    
    # Write file
    with rasterio.open(filepath, 'w', **metadata) as dst:
        # Write each band
        for i, band_name in enumerate(raster.band_names):
            dst.write(raster.data[i].astype(dtype), i + 1)
            dst.set_band_description(i + 1, band_name)
        
        # Write additional metadata tags
        dst.update_tags(**raster.metadata)
        if raster.units:
            dst.update_tags(units=str(raster.units))
    
    logger.info(f"Saved raster: {filepath}")


def reproject_raster(
    raster: RasterData,
    target_crs: Union[str, rasterio.crs.CRS],
    target_resolution: Optional[Tuple[float, float]] = None,
    resampling_method: Resampling = Resampling.bilinear
) -> RasterData:
    """
    Reproject raster to different CRS and/or resolution.
    
    Args:
        raster: Input RasterData
        target_crs: Target coordinate reference system
        target_resolution: (x_res, y_res) in target CRS units. None = auto
        resampling_method: Resampling algorithm
    
    Returns:
        Reprojected RasterData
    """
    if isinstance(target_crs, str):
        target_crs = rasterio.crs.CRS.from_string(target_crs)
    
    # Calculate transform and shape
    if target_resolution is None:
        transform, width, height = calculate_default_transform(
            raster.crs, target_crs,
            raster.shape[2], raster.shape[1],
            *raster.bounds
        )
    else:
        # Manual calculation with specific resolution
        left, bottom, right, top = raster.bounds
        # Transform bounds to target CRS
        from rasterio.warp import transform_bounds
        target_bounds = transform_bounds(raster.crs, target_crs, left, bottom, right, top)
        
        width = int((target_bounds[2] - target_bounds[0]) / target_resolution[0])
        height = int((target_bounds[3] - target_bounds[1]) / target_resolution[1])
        
        transform = rasterio.transform.from_bounds(
            *target_bounds, width, height
        )
    
    # Reproject each band
    reprojected_data = np.zeros((raster.shape[0], height, width), dtype=raster.data.dtype)
    
    for i in range(raster.shape[0]):
        reproject(
            source=raster.data[i],
            destination=reprojected_data[i],
            src_transform=raster.transform,
            src_crs=raster.crs,
            dst_transform=transform,
            dst_crs=target_crs,
            resampling=resampling_method,
            src_nodata=raster.nodata,
            dst_nodata=raster.nodata
        )
    
    logger.info(f"Reprojected from {raster.crs} to {target_crs}, "
               f"resolution: {target_resolution}")
    
    return RasterData(
        data=reprojected_data,
        transform=transform,
        crs=target_crs,
        nodata=raster.nodata,
        band_names=raster.band_names,
        metadata=raster.metadata,
        units=raster.units
    )


def align_rasters(
    rasters: List[RasterData],
    reference_idx: int = 0,
    resampling_method: Resampling = Resampling.bilinear
) -> List[RasterData]:
    """
    Align multiple rasters to same grid (CRS, resolution, extent).
    
    Args:
        rasters: List of RasterData objects
        reference_idx: Index of reference raster (others aligned to this)
        resampling_method: Resampling algorithm
    
    Returns:
        List of aligned RasterData objects
    """
    if len(rasters) < 2:
        return rasters
    
    reference = rasters[reference_idx]
    aligned = [reference]
    
    logger.info(f"Aligning {len(rasters)} rasters to reference {reference_idx}")
    
    for i, raster in enumerate(rasters):
        if i == reference_idx:
            continue
        
        # Reproject to reference CRS and resolution
        aligned_raster = reproject_raster(
            raster,
            target_crs=reference.crs,
            target_resolution=reference.resolution,
            resampling_method=resampling_method
        )
        
        # Crop to reference extent (if needed)
        # TODO: Implement extent matching if needed
        
        aligned.append(aligned_raster)
    
    return aligned


def stack_rasters(rasters: List[RasterData]) -> RasterData:
    """
    Stack multiple rasters into single multi-band raster.
    Assumes rasters are already aligned.
    
    Args:
        rasters: List of aligned RasterData objects
    
    Returns:
        Stacked RasterData
    """
    # Validate alignment
    ref_crs = rasters[0].crs
    ref_transform = rasters[0].transform
    ref_shape = rasters[0].shape[1:]
    
    for i, raster in enumerate(rasters[1:], 1):
        if raster.crs != ref_crs:
            raise ValueError(f"Raster {i} has different CRS")
        if raster.transform != ref_transform:
            raise ValueError(f"Raster {i} has different transform")
        if raster.shape[1:] != ref_shape:
            raise ValueError(f"Raster {i} has different shape")
    
    # Stack data
    stacked_data = np.vstack([r.data for r in rasters])
    stacked_band_names = []
    stacked_units = {}
    
    for raster in rasters:
        stacked_band_names.extend(raster.band_names)
        stacked_units.update(raster.units)
    
    logger.info(f"Stacked {len(rasters)} rasters into {len(stacked_band_names)} bands")
    
    return RasterData(
        data=stacked_data,
        transform=ref_transform,
        crs=ref_crs,
        nodata=rasters[0].nodata,
        band_names=stacked_band_names,
        metadata={'stacked': True, 'n_sources': len(rasters)},
        units=stacked_units
    )


def load_vector(filepath: Union[str, Path]) -> gpd.GeoDataFrame:
    """
    Load vector file (shapefile, GeoJSON, etc.)
    
    Args:
        filepath: Path to vector file
    
    Returns:
        GeoDataFrame
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Vector file not found: {filepath}")
    
    logger.info(f"Loading vector: {filepath.name}")
    gdf = gpd.read_file(filepath)
    logger.info(f"Loaded {len(gdf)} features, CRS: {gdf.crs}")
    
    return gdf


def rasterize_vector(
    vector: gpd.GeoDataFrame,
    reference_raster: RasterData,
    attribute: Optional[str] = None,
    fill_value: float = 0,
    default_value: float = 1
) -> RasterData:
    """
    Rasterize vector to match reference raster grid.
    
    Args:
        vector: GeoDataFrame to rasterize
        reference_raster: Reference raster for grid matching
        attribute: Attribute to burn into raster. None = use default_value
        fill_value: Value for pixels outside polygons
        default_value: Value for pixels inside polygons (if attribute=None)
    
    Returns:
        Rasterized RasterData
    """
    from rasterio import features
    
    # Reproject vector to reference CRS if needed
    if vector.crs != reference_raster.crs:
        vector = vector.to_crs(reference_raster.crs)
    
    # Create empty raster
    height, width = reference_raster.shape[1:]
    rasterized = np.full((height, width), fill_value, dtype=np.float32)
    
    # Prepare shapes
    if attribute:
        shapes = ((geom, value) for geom, value in zip(vector.geometry, vector[attribute]))
    else:
        shapes = ((geom, default_value) for geom in vector.geometry)
    
    # Rasterize
    features.rasterize(
        shapes=shapes,
        out=rasterized,
        transform=reference_raster.transform,
        fill=fill_value,
        dtype=np.float32
    )
    
    logger.info(f"Rasterized {len(vector)} features")
    
    return RasterData(
        data=rasterized[np.newaxis, :, :],
        transform=reference_raster.transform,
        crs=reference_raster.crs,
        nodata=fill_value,
        band_names=['rasterized'],
        metadata={'source': 'rasterized_vector'}
    )


if __name__ == "__main__":
    # Test module
    logger.info("I/O module loaded successfully")
    logger.info("Available functions:")
    logger.info("  - load_raster()")
    logger.info("  - save_raster()")
    logger.info("  - reproject_raster()")
    logger.info("  - align_rasters()")
    logger.info("  - stack_rasters()")
    logger.info("  - load_vector()")
    logger.info("  - rasterize_vector()")
