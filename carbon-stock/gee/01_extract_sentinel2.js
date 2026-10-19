/**
 * Google Earth Engine Script: Sentinel-2 L2A Data Extraction
 * 
 * PURPOSE: Extract Sentinel-2 Level-2A surface reflectance with explicit:
 *   - Cloud masking using Scene Classification Layer (SCL)
 *   - Vegetation indices with documented formulas
 *   - Temporal compositing with no undocumented smoothing
 *   - Explicit scale factors and coordinate systems
 * 
 * STANDARDS:
 *   - ESA Sentinel-2 Product Specification Document v14.9
 *   - ESA Sen2Cor Algorithm Theoretical Basis Document
 *   - CEOS Analysis Ready Data (ARD) specifications
 * 
 * NO MAGIC NUMBERS - All values are traced to documentation.
 */

// =============================================================================
// CONFIGURATION (Must match Python config.py)
// =============================================================================

var CONFIG = {
  // Area of Interest (MODIFY THIS)
  // Example: Small tropical forest plot
  aoi: ee.Geometry.Rectangle([-84.0, 10.0, -83.5, 10.5]), // Costa Rica example
  
  // Temporal range
  startDate: '2023-01-01',
  endDate: '2024-12-31',
  
  // Composite period (days)
  compositePeriod: 30, // Monthly composites
  
  // Sentinel-2 parameters
  resolution: 10, // meters
  scaleFactor: 0.0001, // DN to reflectance (ESA S2 L2A spec)
  
  // Scene Classification Layer (SCL) values
  // Source: Sen2Cor Configuration and User Manual v2.9
  sclClasses: {
    NO_DATA: 0,
    SATURATED_OR_DEFECTIVE: 1,
    DARK_AREA_PIXELS: 2,
    CLOUD_SHADOWS: 3,
    VEGETATION: 4,
    NOT_VEGETATED: 5,
    WATER: 6,
    UNCLASSIFIED: 7,
    CLOUD_MEDIUM_PROBABILITY: 8,
    CLOUD_HIGH_PROBABILITY: 9,
    THIN_CIRRUS: 10,
    SNOW: 11
  },
  
  // Valid classes for analysis (exclude clouds, shadows, cirrus, snow)
  validSclClasses: [4, 5, 6, 7], // Vegetation, Not vegetated, Water, Unclassified
  
  // Bands to export (RESTRICTED to indices only - no biomass modeling)
  // B2=Blue, B3=Green, B4=Red, B8=NIR, B11=SWIR1
  bands: ['B2', 'B3', 'B4', 'B8', 'B11'],
  
  // Usage scope (IMPORTANT)
  usageScope: 'QA_VISUALIZATION_ONLY', // NOT for biomass/height/structure
  
  // Export settings
  exportScale: 10,
  exportCrs: 'EPSG:4326', // WGS84
  exportFolder: 'carbon_estimator_sentinel2',
  
  // Validation mode (add debug reducers)
  debugMode: true
};

// =============================================================================
// CLOUD MASKING FUNCTION (SCL-based)
// =============================================================================

/**
 * Apply cloud mask using Scene Classification Layer (SCL)
 * Source: ESA Sen2Cor v2.9 - Scene Classification Algorithm
 * 
 * @param {ee.Image} image - Sentinel-2 L2A image
 * @returns {ee.Image} - Masked image with invalid pixels removed
 */
function maskClouds(image) {
  var scl = image.select('SCL');
  
  // Create mask: 1 = valid, 0 = invalid
  // Valid classes: Vegetation (4), Not vegetated (5), Water (6), Unclassified (7)
  var mask = scl.eq(4)
    .or(scl.eq(5))
    .or(scl.eq(6))
    .or(scl.eq(7));
  
  // Apply mask to all bands
  return image.updateMask(mask)
    .set('cloud_mask_applied', true)
    .set('system:time_start', image.get('system:time_start'));
}

// =============================================================================
// SPECTRAL INDEX CALCULATIONS (Explicit Formulas)
// =============================================================================

/**
 * Calculate NDVI - Normalized Difference Vegetation Index
 * Formula: (NIR - Red) / (NIR + Red)
 * Source: Rouse et al., 1974, NASA SP-351
 * Range: [-1, 1], typical vegetation: 0.2-0.9
 */
function addNDVI(image) {
  var ndvi = image.select('B8').subtract(image.select('B4'))
    .divide(image.select('B8').add(image.select('B4')))
    .rename('NDVI');
  return image.addBands(ndvi);
}

/**
 * Calculate EVI - Enhanced Vegetation Index
 * Formula: 2.5 * ((NIR - Red) / (NIR + 6*Red - 7.5*Blue + 1))
 * Source: Huete et al., 2002, Remote Sensing of Environment
 * Advantages: Reduced atmospheric and soil background effects
 */
function addEVI(image) {
  var evi = image.expression(
    '2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))', {
      'NIR': image.select('B8'),
      'RED': image.select('B4'),
      'BLUE': image.select('B2')
    }
  ).rename('EVI');
  return image.addBands(evi);
}

/**
 * Calculate NDMI - Normalized Difference Moisture Index
 * Formula: (NIR - SWIR1) / (NIR + SWIR1)
 * Source: Hardisky et al., 1983; Wilson & Sader, 2002
 * Range: [-1, 1], higher values = more water content
 * 
 * NOTE: Used for QA and visualization only, NOT for biomass modeling
 */
function addNDMI(image) {
  var ndmi = image.select('B8').subtract(image.select('B11'))
    .divide(image.select('B8').add(image.select('B11')))
    .rename('NDMI');
  return image.addBands(ndmi);
}

// ============================================================================
// REMOVED INDICES (Not needed for USA GEDI/FIA workflow)
// ============================================================================
// - NDRE: Requires B5 (RedEdge) - removed to reduce data volume
// - GNDVI: Redundant with NDVI for QA purposes
// - SAVI: Not needed for forest QA (NDVI sufficient)
// - MSI: Redundant with NDMI
// - FAPAR: Removed (was used for biomass - not allowed)
// 
// RATIONALE: Sentinel-2 is ONLY for:
//   1. Forest mask QA/validation
//   2. Visualization layers
//   3. Change detection
// NOT for biomass, canopy structure, or height inference

/**
 * Add RESTRICTED indices (QA/visualization only)
 * 
 * USAGE SCOPE: These indices are ONLY for:
 *   - Forest mask validation
 *   - Visualization
 *   - Change detection
 * 
 * NOT ALLOWED:
 *   - Biomass estimation
 *   - Canopy structure inference
 *   - Height modeling
 *   - Input to Random Forest/regression models
 */
function addRestrictedIndices(image) {
  return addNDVI(addEVI(addNDMI(image)));
}

// =============================================================================
// DATA LOADING AND PROCESSING
// =============================================================================

/**
 * Load Sentinel-2 Level-2A Surface Reflectance
 * Collection: COPERNICUS/S2_SR_HARMONIZED
 * Source: ESA via Google Earth Engine
 */
var s2Collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
  .filterBounds(CONFIG.aoi)
  .filterDate(CONFIG.startDate, CONFIG.endDate)
  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 80)); // Pre-filter heavy clouds

print('Sentinel-2 L2A images found:', s2Collection.size());

// Apply cloud masking
var s2Masked = s2Collection.map(maskClouds);

// Apply scale factor to convert DN to reflectance
// Source: ESA S2 L2A Product Format Specification
// DN values are stored as uint16 scaled by 10000
var s2Reflectance = s2Masked.map(function(image) {
  var bands = CONFIG.bands.concat(['SCL']);
  var scaled = image.select(CONFIG.bands).multiply(CONFIG.scaleFactor);
  return scaled.addBands(image.select('SCL'))
    .copyProperties(image, ['system:time_start']);
});

// Calculate RESTRICTED indices (QA/visualization only)
var s2WithIndices = s2Reflectance.map(addRestrictedIndices);

// =============================================================================
// TEMPORAL COMPOSITING (Monthly Median)
// =============================================================================

/**
 * Create monthly median composites
 * Method: Median reducer (robust to outliers)
 * No temporal smoothing applied
 */
function createMonthlyComposites(collection, startDate, endDate) {
  var months = ee.List.sequence(0, ee.Date(endDate).difference(ee.Date(startDate), 'month').subtract(1));
  
  var monthlyComposites = months.map(function(monthOffset) {
    var start = ee.Date(startDate).advance(monthOffset, 'month');
    var end = start.advance(1, 'month');
    
    var composite = collection
      .filterDate(start, end)
      .median()
      .set('system:time_start', start.millis())
      .set('month', start.format('YYYY-MM'));
    
    return composite;
  });
  
  return ee.ImageCollection.fromImages(monthlyComposites);
}

var monthlyComposites = createMonthlyComposites(s2WithIndices, CONFIG.startDate, CONFIG.endDate);

print('Monthly composites created:', monthlyComposites.size());

// =============================================================================
// MULTI-TEMPORAL METRICS
// =============================================================================

/**
 * Calculate temporal statistics (median and standard deviation)
 * Used to capture phenological signals and temporal stability
 */
var temporalMedian = monthlyComposites.median().set('stat', 'median');
var temporalStdDev = monthlyComposites.reduce(ee.Reducer.stdDev()).set('stat', 'stddev');

// =============================================================================
// DEBUG: VALIDATION REDUCERS
// =============================================================================

if (CONFIG.debugMode) {
  // Sample points for validation
  var samplePoints = ee.FeatureCollection.randomPoints(CONFIG.aoi, 100);
  
  // Extract values at sample points
  var sampleData = temporalMedian.sampleRegions({
    collection: samplePoints,
    scale: CONFIG.resolution,
    projection: CONFIG.exportCrs
  });
  
  print('Sample validation data (first 10 points):', sampleData.limit(10));
  
  // Check for non-zero values
  var stats = temporalMedian.select('NDVI').reduceRegion({
    reducer: ee.Reducer.minMax().combine(ee.Reducer.mean(), '', true),
    geometry: CONFIG.aoi,
    scale: CONFIG.resolution,
    maxPixels: 1e9
  });
  
  print('NDVI Statistics:', stats);
  print('If min/max/mean are zero or null, check masking and date range');
}

// =============================================================================
// VISUALIZATION (Not used for computation)
// =============================================================================

var visParams = {
  bands: ['B4', 'B3', 'B2'],
  min: 0,
  max: 0.3,
  gamma: 1.4
};

Map.centerObject(CONFIG.aoi, 12);
Map.addLayer(CONFIG.aoi, {color: 'red'}, 'AOI');
Map.addLayer(temporalMedian, visParams, 'Sentinel-2 RGB (Median)');
Map.addLayer(temporalMedian.select('NDVI'), {min: 0, max: 0.9, palette: ['red', 'yellow', 'green']}, 'NDVI');
Map.addLayer(temporalMedian.select('EVI'), {min: 0, max: 0.8, palette: ['brown', 'yellow', 'darkgreen']}, 'EVI');

// =============================================================================
// EXPORT TO GOOGLE DRIVE
// =============================================================================

/**
 * Export all bands and indices as GeoTIFF
 * Includes explicit metadata for traceability
 */

// List of all bands to export
// RESTRICTED EXPORT: Only NDVI, EVI, NDMI for QA/visualization
var exportBands = CONFIG.bands.concat(['NDVI', 'EVI', 'NDMI']);

// Add metadata tag to prevent misuse
var exportMetadata = {
  'usage_scope': 'QA_VISUALIZATION_ONLY',
  'NOT_FOR': 'biomass,height,canopy_structure,carbon_modeling',
  'allowed_use': 'forest_mask_qa,visualization,change_detection'
};

Export.image.toDrive({
  image: temporalMedian.select(exportBands).toFloat(),
  description: 'S2_median_composite',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'sentinel2_median_' + CONFIG.startDate + '_' + CONFIG.endDate,
  region: CONFIG.aoi,
  scale: CONFIG.exportScale,
  crs: CONFIG.exportCrs,
  maxPixels: 1e13,
  fileFormat: 'GeoTIFF',
  formatOptions: {
    cloudOptimized: true
  }
});

Export.image.toDrive({
  image: temporalStdDev.toFloat(),
  description: 'S2_stddev_composite',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'sentinel2_stddev_' + CONFIG.startDate + '_' + CONFIG.endDate,
  region: CONFIG.aoi,
  scale: CONFIG.exportScale,
  crs: CONFIG.exportCrs,
  maxPixels: 1e13,
  fileFormat: 'GeoTIFF',
  formatOptions: {
    cloudOptimized: true
  }
});

// Export individual monthly composites (optional, for time series analysis)
var exportMonthly = false; // Set to true if needed

if (exportMonthly) {
  var monthlyList = monthlyComposites.toList(monthlyComposites.size());
  var numMonths = monthlyList.size().getInfo();
  
  for (var i = 0; i < Math.min(numMonths, 24); i++) { // Limit to 24 months
    var monthlyImg = ee.Image(monthlyList.get(i));
    var monthStr = monthlyImg.get('month').getInfo();
    
    Export.image.toDrive({
      image: monthlyImg.select(exportBands).toFloat(),
      description: 'S2_monthly_' + monthStr.replace('-', '_'),
      folder: CONFIG.exportFolder,
      fileNamePrefix: 'sentinel2_' + monthStr,
      region: CONFIG.aoi,
      scale: CONFIG.exportScale,
      crs: CONFIG.exportCrs,
      maxPixels: 1e13,
      fileFormat: 'GeoTIFF'
    });
  }
}

// =============================================================================
// METADATA EXPORT
// =============================================================================

var metadata = ee.Dictionary({
  'data_source': 'COPERNICUS/S2_SR_HARMONIZED',
  'processing_level': 'L2A',
  'start_date': CONFIG.startDate,
  'end_date': CONFIG.endDate,
  'scale_factor': CONFIG.scaleFactor,
  'resolution_m': CONFIG.resolution,
  'crs': CONFIG.exportCrs,
  'cloud_mask': 'SCL-based',
  'valid_scl_classes': CONFIG.validSclClasses,
  'composite_method': 'median',
  'composite_period_days': CONFIG.compositePeriod,
  'indices_calculated': ['NDVI', 'EVI', 'NDMI'],
  'bands_exported': exportBands,
  'standards': 'ESA S2 Product Spec v14.9, CEOS ARD',
  'processing_date': ee.Date(Date.now()).format('YYYY-MM-dd').getInfo(),
  // USAGE RESTRICTIONS
  'usage_scope': 'QA_VISUALIZATION_ONLY',
  'allowed_for': ['forest_mask_validation', 'visualization', 'change_detection'],
  'NOT_allowed_for': ['biomass_estimation', 'height_modeling', 'canopy_structure', 'carbon_accounting'],
  'note': 'Sentinel-2 data restricted to supporting analysis only. Use GEDI/FIA for biomass/height.'
});

print('=== METADATA ===');
print(metadata);
print('=== IMPORTANT: USAGE RESTRICTIONS ===');
print('Sentinel-2 outputs are for QA/visualization ONLY');
print('NOT for biomass, height, or carbon modeling');
print('Use GEDI/FIA data for structural metrics');
print('=== Export tasks created. Click "Run" in the Tasks tab ===');
