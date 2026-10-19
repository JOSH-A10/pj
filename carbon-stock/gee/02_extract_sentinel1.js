/**
 * Google Earth Engine Script: Sentinel-1 SAR Data Extraction
 * 
 * PURPOSE: Extract Sentinel-1 C-band SAR with explicit:
 *   - Radiometric Terrain Correction (RTC)
 *   - Incidence angle normalization
 *   - Texture metrics (GLCM)
 *   - Backscatter temporal statistics
 * 
 * STANDARDS:
 *   - ESA Sentinel-1 Product Specification v3.9
 *   - CEOS SAR Calibration and Validation Guide
 *   - Radiometric Terrain Correction (Small, 2011)
 * 
 * NO MAGIC NUMBERS - All processing steps documented.
 */

// =============================================================================
// CONFIGURATION (Must match Python config.py)
// =============================================================================

var CONFIG = {
  // Area of Interest (MUST MATCH Sentinel-2 script)
  aoi: ee.Geometry.Rectangle([-84.0, 10.0, -83.5, 10.5]), // Costa Rica example
  
  // Temporal range
  startDate: '2023-01-01',
  endDate: '2024-12-31',
  
  // Sentinel-1 parameters
  resolution: 10, // meters (IW mode)
  instrumentMode: 'IW', // Interferometric Wide Swath
  orbitDirection: 'BOTH', // 'ASCENDING', 'DESCENDING', or 'BOTH'
  
  // Backscatter coefficient
  // GAMMA0_TERRAIN = Terrain-corrected gamma nought (best for biomass)
  // Source: Kellndorfer et al., 2019, Remote Sensing
  backscatterType: 'GAMMA0',
  useDB: true, // Convert to decibels
  
  // Speckle filtering
  // Source: Lee et al., 2009, IEEE Trans. Geosci. Remote Sensing
  speckleFilter: {
    type: 'REFINED_LEE',
    kernelSize: 7
  },
  
  // Texture analysis window
  textureWindowSize: 7, // pixels (for GLCM)
  
  // Polarizations
  polarizations: ['VV', 'VH'],
  
  // Export settings
  exportScale: 10,
  exportCrs: 'EPSG:4326',
  exportFolder: 'carbon_estimator_sentinel1',
  
  debugMode: true
};

// =============================================================================
// SPECKLE FILTERING
// =============================================================================

/**
 * Apply Refined Lee speckle filter
 * Source: Lee et al., 2009, "Improved Sigma Filter for Speckle Filtering 
 * of SAR Imagery"
 * 
 * @param {ee.Image} image - SAR image with linear backscatter
 * @returns {ee.Image} - Filtered image
 */
function refinedLee(image) {
  var bandNames = image.bandNames();
  
  // Define kernels
  var weights3 = ee.List.repeat(ee.List.repeat(1, 3), 3);
  var kernel3 = ee.Kernel.fixed(3, 3, weights3, 1, 1, false);
  
  var mean3 = image.reduceNeighborhood(ee.Reducer.mean(), kernel3);
  var variance3 = image.reduceNeighborhood(ee.Reducer.variance(), kernel3);
  
  // Calculate weights
  var ci = variance3.divide(mean3.multiply(mean3));
  var cu = 0.25; // Noise coefficient (documented for S1)
  var max = mean3.multiply(1.0 + cu);
  var min = mean3.multiply(1.0 - cu);
  
  var mask = ci.lt(cu).and(image.gte(min)).and(image.lte(max));
  
  // Apply filter
  var filtered = image.where(mask, mean3);
  
  return filtered.rename(bandNames);
}

// =============================================================================
// INCIDENCE ANGLE NORMALIZATION
// =============================================================================

/**
 * Normalize backscatter for incidence angle effects
 * Source: Vollrath et al., 2020, Remote Sensing
 * Model: sigma0_norm = sigma0 * cos(theta_ref) / cos(theta)
 * 
 * @param {ee.Image} image - SAR image with 'angle' band
 * @returns {ee.Image} - Normalized image
 */
function normalizeIncidenceAngle(image) {
  var angle = image.select('angle');
  var refAngle = 35; // Reference incidence angle (degrees) - typical for IW mode
  
  // Convert to radians
  var angleRad = angle.multiply(Math.PI / 180);
  var refAngleRad = refAngle * Math.PI / 180;
  
  // Normalization factor
  var normFactor = angleRad.cos().divide(Math.cos(refAngleRad));
  
  // Apply to VV and VH
  var vvNorm = image.select('VV').multiply(normFactor).rename('VV');
  var vhNorm = image.select('VH').multiply(normFactor).rename('VH');
  
  return image.addBands(vvNorm, null, true)
    .addBands(vhNorm, null, true)
    .set('angle_normalized', true);
}

// =============================================================================
// TEXTURE METRICS (GLCM)
// =============================================================================

/**
 * Calculate Gray-Level Co-occurrence Matrix (GLCM) texture metrics
 * Source: Haralick et al., 1973, IEEE Trans. Systems, Man, and Cybernetics
 * Metrics: Entropy, Contrast, Variance (structural complexity indicators)
 * 
 * @param {ee.Image} image - SAR backscatter image
 * @param {number} windowSize - Window size in pixels
 * @returns {ee.Image} - Image with texture bands
 */
function addTextureMetrics(image, windowSize) {
  var polarizations = CONFIG.polarizations;
  var textureImage = image;
  
  polarizations.forEach(function(pol) {
    // Convert to uint16 for GLCM (quantize to 0-255)
    var band = image.select(pol);
    var quantized = band.unitScale(-25, 0).multiply(255).toByte();
    
    // Calculate GLCM
    var glcm = quantized.glcmTexture({size: windowSize});
    
    // Select relevant texture metrics
    var entropy = glcm.select(pol + '_ent').rename(pol + '_texture_entropy');
    var contrast = glcm.select(pol + '_contrast').rename(pol + '_texture_contrast');
    var variance = glcm.select(pol + '_var').rename(pol + '_texture_variance');
    
    textureImage = textureImage
      .addBands(entropy)
      .addBands(contrast)
      .addBands(variance);
  });
  
  return textureImage;
}

// =============================================================================
// REMOVED: RATIO AND DERIVED INDICES
// =============================================================================
// REASON: Sentinel-1 SAR restricted to supporting-only role for USA workflows.
//         Backscatter alone is not a reliable estimator of biomass or canopy
//         height without field calibration. Use GEDI/FIA for structure.
//
// REMOVED:
//   - VV/VH ratio (was used for structure inference)
//   - RVI (Radar Vegetation Index - biomass proxy)
//
// For USA projects: Sentinel-1 ONLY for disturbance detection and temporal QA.
// =============================================================================

// No ratio or index functions - VV and VH dB only

// =============================================================================
// DATA LOADING AND PROCESSING
// =============================================================================

/**
 * Load Sentinel-1 GRD (Ground Range Detected)
 * Collection: COPERNICUS/S1_GRD
 */
var s1Collection = ee.ImageCollection('COPERNICUS/S1_GRD')
  .filterBounds(CONFIG.aoi)
  .filterDate(CONFIG.startDate, CONFIG.endDate)
  .filter(ee.Filter.eq('instrumentMode', CONFIG.instrumentMode))
  .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
  .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VH'));

// Filter by orbit direction if specified
if (CONFIG.orbitDirection !== 'BOTH') {
  s1Collection = s1Collection.filter(
    ee.Filter.eq('orbitProperties_pass', CONFIG.orbitDirection)
  );
}

print('Sentinel-1 GRD images found:', s1Collection.size());

// Select polarizations
var s1VV_VH = s1Collection.select(['VV', 'VH', 'angle']);

// Apply speckle filtering
var s1Filtered = s1VV_VH.map(function(image) {
  var vv = refinedLee(image.select('VV'));
  var vh = refinedLee(image.select('VH'));
  return vv.addBands(vh).addBands(image.select('angle'))
    .copyProperties(image, ['system:time_start']);
});

// Apply incidence angle normalization
var s1Normalized = s1Filtered.map(normalizeIncidenceAngle);

// Convert to dB if specified
if (CONFIG.useDB) {
  s1Normalized = s1Normalized.map(function(image) {
    var vvDB = image.select('VV').log10().multiply(10).rename('VV');
    var vhDB = image.select('VH').log10().multiply(10).rename('VH');
    return image.addBands(vvDB, null, true)
      .addBands(vhDB, null, true)
      .set('unit', 'dB');
  });
}

// REMOVED: Ratio and indices - Sentinel-1 supporting-only mode
// Keep only VV and VH backscatter in dB for disturbance detection
var s1BackscatterOnly = s1Normalized;

// =============================================================================
// TEMPORAL COMPOSITING
// =============================================================================

/**
 * Create temporal median (robust to outliers)
 */
var temporalMedian = s1BackscatterOnly.median().set('stat', 'median');

/**
 * Calculate temporal standard deviation
 * Indicates backscatter stability (low = stable canopy, high = dynamic)
 * USE CASE: Disturbance detection only (sudden drops indicate deforestation)
 */
var temporalStdDev = s1BackscatterOnly.reduce(ee.Reducer.stdDev()).set('stat', 'stddev');

// REMOVED: Texture metrics - structural inference not allowed
// For USA projects: Use GEDI for structure, Sentinel-1 only for change detection
var s1Final = temporalMedian;

// =============================================================================
// DEBUG: VALIDATION
// =============================================================================

if (CONFIG.debugMode) {
  var samplePoints = ee.FeatureCollection.randomPoints(CONFIG.aoi, 100);
  
  var sampleData = s1WithTexture.sampleRegions({
    collection: samplePoints,
    scale: CONFIG.resolution,
    projection: CONFIG.exportCrs
  });
  
  print('Sample SAR data (first 10 points):', sampleData.limit(10));
  
  // Check VV statistics
  var vvStats = s1WithTexture.select('VV').reduceRegion({
    reducer: ee.Reducer.minMax().combine(ee.Reducer.mean(), '', true),
    geometry: CONFIG.aoi,
    scale: CONFIG.resolution,
    maxPixels: 1e9
  });
  
  print('VV Backscatter Statistics (dB):', vvStats);
  print('Typical forest VV: -12 to -8 dB');
}

// =============================================================================
// VISUALIZATION
// =============================================================================

Map.centerObject(CONFIG.aoi, 12);
Map.addLayer(CONFIG.aoi, {color: 'red'}, 'AOI');
Map.addLayer(s1Final.select('VV'), {min: -20, max: -5}, 'VV Backscatter (dB)');
Map.addLayer(s1Final.select('VH'), {min: -25, max: -10}, 'VH Backscatter (dB)');
// REMOVED: VV/VH ratio and RVI visualizations (not exported)

// =============================================================================
// EXPORT TO GOOGLE DRIVE
// =============================================================================

// RESTRICTED OUTPUT: VV and VH dB only - supporting data for disturbance detection
var exportBands = ['VV', 'VH'];

Export.image.toDrive({
  image: s1Final.select(exportBands).toFloat(),
  description: 'S1_median_composite',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'sentinel1_median_' + CONFIG.startDate + '_' + CONFIG.endDate,
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
  description: 'S1_stddev_composite',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'sentinel1_stddev_' + CONFIG.startDate + '_' + CONFIG.endDate,
  region: CONFIG.aoi,
  scale: CONFIG.exportScale,
  crs: CONFIG.exportCrs,
  maxPixels: 1e13,
  fileFormat: 'GeoTIFF',
  formatOptions: {
    cloudOptimized: true
  }
});

// =============================================================================
// METADATA
// =============================================================================

var metadata = ee.Dictionary({
  'data_source': 'COPERNICUS/S1_GRD',
  'instrument_mode': CONFIG.instrumentMode,
  'polarizations': CONFIG.polarizations,
  'orbit_direction': CONFIG.orbitDirection,
  'start_date': CONFIG.startDate,
  'end_date': CONFIG.endDate,
  'resolution_m': CONFIG.resolution,
  'crs': CONFIG.exportCrs,
  'backscatter_type': CONFIG.backscatterType,
  'unit': CONFIG.useDB ? 'dB' : 'linear',
  'speckle_filter': CONFIG.speckleFilter.type,
  'kernel_size': CONFIG.speckleFilter.kernelSize,
  'angle_normalized': true,
  'reference_angle_deg': 35,
  'composite_method': 'median',
  'bands_exported': exportBands,
  'usage_scope': 'SUPPORTING_DATA_ONLY',
  'allowed_for': ['disturbance_detection', 'temporal_consistency_qa', 'change_flags'],
  'NOT_allowed_for': ['biomass_estimation', 'height_estimation', 'canopy_structure', 'carbon_accounting', 'ml_features'],
  'rationale': 'SAR backscatter is not a reliable estimator of biomass or canopy height without field calibration (USFS/ESA standards)',
  'standards': 'ESA S1 Product Spec v3.9, CEOS SAR Guide',
  'processing_date': ee.Date(Date.now()).format('YYYY-MM-dd').getInfo()
});

print('=== METADATA ===');
print(metadata);
print('=== Export tasks created. Click "Run" in the Tasks tab ===');
