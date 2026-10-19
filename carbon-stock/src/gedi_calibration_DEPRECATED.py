"""
GEDI-Calibrated Height Estimation Module
=========================================

Calibrates Sentinel-based height estimation using GEDI L2A point data.

Workflow:
1. Load GEDI points (rh98 canopy height)
2. Extract Sentinel features at GEDI locations
3. Train Random Forest: Height = f(NDVI, EVI, NDMI, VV, VH, VH/VV, Texture)
4. Apply model wall-to-wall
5. Validate and report accuracy

Uncertainty reduction: SAR-only (±28%) → GEDI-calibrated (±10-15%)
"""

import json
import logging
from pathlib import Path
from typing import Dict, Tuple, Optional
from dataclasses import dataclass

import numpy as np
import rasterio
from rasterio.transform import rowcol
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error, r2_score
import joblib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class GEDICalibrationResult:
    """Results from GEDI calibration"""
    model_type: str
    n_training_points: int
    rmse: float  # Root Mean Square Error (meters)
    r2: float  # R-squared
    feature_importance: Dict[str, float]
    height_range_m: Tuple[float, float]
    uncertainty_pct: float
    gedi_used: bool


class GEDIHeightCalibrator:
    """
    Calibrates satellite-based height estimation using GEDI LiDAR
    
    IMPORTANT: This uses Sentinel-2 indices (NDVI, EVI, NDMI) ONLY for
    calibration training with GEDI ground truth. Sentinel-2 alone cannot
    estimate height - it requires GEDI/LiDAR calibration.
    
    For USA projects: Consider using FIA plots instead of Sentinel-2 features.
    """
    
    def __init__(self):
        self.model = None
        # RESTRICTED: NDVI/EVI/NDMI allowed ONLY when paired with GEDI/FIA
        self.feature_names = [
            'ndvi',   # S2 - allowed as ML feature WITH GEDI
            'evi',    # S2 - allowed as ML feature WITH GEDI  
            'ndmi',   # S2 - moisture/change detection
            'vv',     # S1 - backscatter
            'vh',     # S1 - backscatter
            'vh_vv_ratio',  # S1 - cross-pol ratio
        ]
        # REMOVED: ndvi_texture (texture is structural inference - use GEDI/SAR only)
        self.calibration_result = None
        
        logger.warning("⚠️  Sentinel-2 features used ONLY with GEDI/FIA ground truth")
        logger.warning("⚠️  Sentinel-2 alone CANNOT estimate height")
    
    def load_gedi_points(self, gedi_geojson_path: Path) -> Dict:
        """Load GEDI point data from GeoJSON"""
        with open(gedi_geojson_path, 'r') as f:
            gedi_data = json.load(f)
        
        # Check if valid GeoJSON with features
        if gedi_data.get('type') != 'FeatureCollection':
            logger.warning("Invalid GEDI data format - not a FeatureCollection")
            return None
        
        feature_count = len(gedi_data.get('features', []))
        if feature_count == 0:
            logger.warning("No GEDI points found in file")
            return None
        
        logger.info(f"✓ Loaded {feature_count} GEDI points")
        if 'metadata' in gedi_data:
            meta = gedi_data['metadata']
            if meta.get('aoi_expanded'):
                logger.info(f"  AOI expanded from {meta.get('aoi_original_area_km2', 0):.2f} km² to {meta.get('aoi_expanded_area_km2', 0):.2f} km²")
        
        return gedi_data
    
    def extract_features_at_gedi_locations(
        self,
        gedi_data: Dict,
        s2_path: Path,
        s1_path: Path
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Extract Sentinel features at GEDI footprint locations
        
        Returns:
            (features, heights) arrays
        """
        logger.info("Extracting Sentinel features at GEDI locations...")
        
        features_list = []
        heights_list = []
        
        # Open rasters
        with rasterio.open(s2_path) as s2, rasterio.open(s1_path) as s1:
            # Log raster bounds
            logger.info(f"Sentinel-2 bounds: {s2.bounds}")
            logger.info(f"Sentinel-1 bounds: {s1.bounds}")
            
            # Check GEDI spatial extent
            lons = [f['properties']['longitude'] for f in gedi_data['features']]
            lats = [f['properties']['latitude'] for f in gedi_data['features']]
            logger.info(f"GEDI extent: lon {min(lons):.4f} to {max(lons):.4f}, lat {min(lats):.4f} to {max(lats):.4f}")
            
            # Get band indices
            s2_bands = {desc: i+1 for i, desc in enumerate(s2.descriptions) if desc}
            s1_bands = {desc: i+1 for i, desc in enumerate(s1.descriptions) if desc}
            
            logger.info(f"S2 bands: {list(s2_bands.keys())}")
            logger.info(f"S1 bands: {list(s1_bands.keys())}")
            
            points_checked = 0
            points_outside = 0
            points_invalid = 0
            
            # Process each GEDI point
            for feat in gedi_data['features']:
                points_checked += 1
                height = feat['properties']['rh98']
                if height is None or height < 0:
                    points_invalid += 1
                    continue
                
                lon = feat['properties']['longitude']
                lat = feat['properties']['latitude']
                
                # Convert to pixel coordinates
                try:
                    row_s2, col_s2 = rowcol(s2.transform, lon, lat)
                    row_s1, col_s1 = rowcol(s1.transform, lon, lat)
                    
                    # Check bounds
                    if (row_s2 < 0 or row_s2 >= s2.height or 
                        col_s2 < 0 or col_s2 >= s2.width):
                        points_outside += 1
                        continue
                    
                    # Extract Sentinel-2 features
                    ndvi = s2.read(s2_bands.get('NDVI', 1), window=((row_s2, row_s2+1), (col_s2, col_s2+1)))[0, 0]
                    evi = s2.read(s2_bands.get('EVI', 2), window=((row_s2, row_s2+1), (col_s2, col_s2+1)))[0, 0]
                    ndmi = s2.read(s2_bands.get('NDMI', 3), window=((row_s2, row_s2+1), (col_s2, col_s2+1)))[0, 0]
                    
                    # Extract Sentinel-1 features
                    vv = s1.read(s1_bands.get('VV', 1), window=((row_s1, row_s1+1), (col_s1, col_s1+1)))[0, 0]
                    vh = s1.read(s1_bands.get('VH', 2), window=((row_s1, row_s1+1), (col_s1, col_s1+1)))[0, 0]
                    
                    # Compute derived features
                    vh_vv_ratio = vh / (vv + 1e-10)
                    
                    # REMOVED: ndvi_texture calculation (texture = structural inference)
                    # For structural metrics, rely on GEDI/FIA ground truth only
                    
                    # Check for invalid values
                    if (np.isnan([ndvi, evi, ndmi, vv, vh]).any() or
                        np.isinf([ndvi, evi, ndmi, vv, vh]).any()):
                        continue
                    
                    # Store (6 features: 3 optical + 3 radar)
                    features_list.append([
                        ndvi, evi, ndmi,
                        vv, vh, vh_vv_ratio
                    ])
                    heights_list.append(height)
                
                except Exception as e:
                    points_invalid += 1
                    continue
        
        features = np.array(features_list)
        heights = np.array(heights_list)
        
        logger.info(f"GEDI point extraction summary:")
        logger.info(f"  Total GEDI points: {points_checked}")
        logger.info(f"  Outside Sentinel bounds: {points_outside}")
        logger.info(f"  Invalid data: {points_invalid}")
        logger.info(f"  ✓ Valid features extracted: {len(heights)}")
        
        if len(heights) == 0:
            logger.error("❌ No GEDI points fall within Sentinel raster bounds!")
            logger.error("   GEDI was fetched from expanded 21 km² area,")
            logger.error("   but Sentinel composites only cover your 3 ha plantation.")
            logger.error("   Solution: Re-fetch Sentinel data for the expanded GEDI AOI.")
            return features, heights
        
        logger.info(f"  Height range: {heights.min():.1f} - {heights.max():.1f} m")
        
        return features, heights
    
    def train_model(
        self,
        features: np.ndarray,
        heights: np.ndarray,
        test_size: float = 0.2
    ) -> GEDICalibrationResult:
        """
        Train Random Forest height model
        """
        logger.info("Training GEDI-calibrated height model...")
        
        # Split data
        X_train, X_test, y_train, y_test = train_test_split(
            features, heights, test_size=test_size, random_state=42
        )
        
        logger.info(f"Training: {len(X_train)} points, Testing: {len(X_test)} points")
        
        # Train Random Forest
        self.model = RandomForestRegressor(
            n_estimators=100,
            max_depth=15,
            min_samples_split=10,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1
        )
        
        self.model.fit(X_train, y_train)
        
        # Predict on test set
        y_pred = self.model.predict(X_test)
        
        # Metrics
        rmse = np.sqrt(mean_squared_error(y_test, y_pred))
        r2 = r2_score(y_test, y_pred)
        
        # Feature importance
        importance = dict(zip(self.feature_names, self.model.feature_importances_))
        
        # Uncertainty
        residuals = y_test - y_pred
        uncertainty_pct = (np.std(residuals) / np.mean(y_test)) * 100
        
        logger.info("="*70)
        logger.info("GEDI CALIBRATION RESULTS")
        logger.info("="*70)
        logger.info(f"Training points: {len(X_train)}")
        logger.info(f"Test points: {len(X_test)}")
        logger.info(f"RMSE: {rmse:.2f} m")
        logger.info(f"R²: {r2:.3f}")
        logger.info(f"Uncertainty: ±{uncertainty_pct:.1f}%")
        logger.info(f"\nFeature Importance:")
        for feat, imp in sorted(importance.items(), key=lambda x: x[1], reverse=True):
            logger.info(f"  {feat:15s}: {imp:.3f}")
        logger.info("="*70)
        
        self.calibration_result = GEDICalibrationResult(
            model_type='RandomForest',
            n_training_points=len(X_train) + len(X_test),
            rmse=float(rmse),
            r2=float(r2),
            feature_importance=importance,
            height_range_m=(float(heights.min()), float(heights.max())),
            uncertainty_pct=float(uncertainty_pct),
            gedi_used=True
        )
        
        return self.calibration_result
    
    def predict_wall_to_wall(
        self,
        s2_path: Path,
        s1_path: Path,
        output_path: Path,
        mask: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """
        Apply trained model wall-to-wall to generate height map
        """
        logger.info("Generating wall-to-wall height map...")
        
        if self.model is None:
            raise ValueError("Model not trained. Call train_model() first.")
        
        with rasterio.open(s2_path) as s2, rasterio.open(s1_path) as s1:
            # Get band indices
            s2_bands = {desc: i+1 for i, desc in enumerate(s2.descriptions) if desc}
            s1_bands = {desc: i+1 for i, desc in enumerate(s1.descriptions) if desc}
            
            # Read features
            ndvi = s2.read(s2_bands.get('NDVI', 1))
            evi = s2.read(s2_bands.get('EVI', 2))
            ndmi = s2.read(s2_bands.get('NDMI', 3))
            
            vv = s1.read(s1_bands.get('VV', 1))
            vh = s1.read(s1_bands.get('VH', 2))
            vh_vv_ratio = vh / (vv + 1e-10)
            
            # REMOVED: Texture calculation (structural inference - use GEDI/SAR only)
            
            # Stack features (6 features: 3 optical + 3 radar)
            shape = ndvi.shape
            features_stack = np.stack([
                ndvi, evi, ndmi,
                vv, vh, vh_vv_ratio
            ], axis=0)
            
            # Reshape for prediction
            features_flat = features_stack.reshape(len(self.feature_names), -1).T
            
            # Predict
            logger.info("Predicting heights...")
            height_flat = self.model.predict(features_flat)
            height_map = height_flat.reshape(shape)
            
            # Apply mask
            if mask is not None:
                height_map[~mask] = -9999
            
            # Save
            profile = s2.profile
            profile.update(
                dtype=rasterio.float32,
                count=1,
                nodata=-9999
            )
            
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            
            with rasterio.open(output_path, 'w', **profile) as dst:
                dst.write(height_map.astype(np.float32), 1)
                dst.set_band_description(1, 'Canopy_Height_GEDI_Calibrated_m')
            
            logger.info(f"✓ Saved height map: {output_path}")
            
            # Statistics
            valid_heights = height_map[height_map != -9999]
            if len(valid_heights) > 0:
                logger.info(f"Height statistics:")
                logger.info(f"  Min: {valid_heights.min():.1f} m")
                logger.info(f"  Mean: {valid_heights.mean():.1f} m")
                logger.info(f"  Max: {valid_heights.max():.1f} m")
            
            return height_map
    
    def save_model(self, path: Path):
        """Save trained model"""
        joblib.dump(self.model, path)
        logger.info(f"✓ Saved model: {path}")
    
    def load_model(self, path: Path):
        """Load trained model"""
        self.model = joblib.load(path)
        logger.info(f"✓ Loaded model: {path}")


def calibrate_height_with_gedi(
    gedi_geojson_path: Path,
    s2_path: Path,
    s1_path: Path,
    output_height_map: Path,
    output_model: Optional[Path] = None
) -> GEDICalibrationResult:
    """
    Complete GEDI calibration workflow
    
    Returns calibration result or None if GEDI unavailable
    """
    calibrator = GEDIHeightCalibrator()
    
    # Load GEDI
    gedi_data = calibrator.load_gedi_points(gedi_geojson_path)
    
    if gedi_data is None:
        logger.warning("GEDI not available - use SAR proxy fallback")
        return GEDICalibrationResult(
            model_type='SAR_proxy_fallback',
            n_training_points=0,
            rmse=0.0,
            r2=0.0,
            feature_importance={},
            height_range_m=(0.0, 0.0),
            uncertainty_pct=28.0,  # SAR-only uncertainty
            gedi_used=False
        )
    
    # Extract features
    features, heights = calibrator.extract_features_at_gedi_locations(
        gedi_data, s2_path, s1_path
    )
    
    if len(heights) < 20:
        logger.warning(f"Only {len(heights)} GEDI points - insufficient for calibration")
        return GEDICalibrationResult(
            model_type='insufficient_data',
            n_training_points=len(heights),
            rmse=0.0,
            r2=0.0,
            feature_importance={},
            height_range_m=(0.0, 0.0),
            uncertainty_pct=28.0,
            gedi_used=False
        )
    
    # Train model
    result = calibrator.train_model(features, heights)
    
    # Generate height map
    calibrator.predict_wall_to_wall(s2_path, s1_path, output_height_map)
    
    # Save model
    if output_model:
        calibrator.save_model(output_model)
    
    return result
