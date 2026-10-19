/**
 * Google Earth Engine Script: GEDI L2A Canopy Height Extraction
 * 
 * PURPOSE: Extract GEDI LiDAR-derived canopy height (RH98) with quality filtering
 * 
 * CRITICAL: GEDI provides sparse footprints, NOT wall-to-wall coverage
 * These are used as REFERENCE/TRAINING data for ML height prediction
 * 
 * STANDARDS:
 *   - GEDI L2A Algorithm Theoretical Basis Document (ATBD) v2.0
 *   - NASA GEDI Data User Guide
 *   - Quality filtering per Dubayah et al., 2020, Science of Remote Sensing
 * 
 * NO INTERPOLATION - Raw footprints only, filtered for quality
 */

// =============================================================================
// CONFIGURATION
// =============================================================================

var CONFIG = {
  // Area of Interest - Kerala, India (centered on 76.8127°E, 9.4660°N)
  // Expanded to ~5km × 5km for adequate GEDI coverage (~25 km²)
  // NOTE: GEDI footprints are sparse (60m × 600m spacing)
  //       Use this larger region for calibration data
  aoi: ee.Geometry.Rectangle([76.79, 9.44, 76.84, 9.49]),
  
  // Alternative larger region if more data needed (uncomment to use):
  // aoi: ee.Geometry.Rectangle([76.75, 9.40, 76.85, 9.50]),  // ~10 km × 10 km
  
  // Temporal range
  startDate: '2019-04-18', // GEDI mission start
  endDate: '2024-12-31',
  
  // GEDI quality filters (CRITICAL for reliable height estimates)
  // Source: GEDI L2A Data User Guide, Section 5.2
  qualityFilters: {
    minBeamSensitivity: 0.98,     // Beam sensitivity threshold
    maxSolarElevation: 90,         // degrees (no limit, but can filter daytime)
    minRHQuality: 1,               // RH quality flag (1 = good)
    degrade: 0,                    // Degrade flag (0 = not degraded)
    qualityFlag: 1                 // L2 quality flag (1 = good)
  },
  
  // Height metrics to extract
  // RH = Relative Height (percentile of waveform energy return)
  // RH98 ≈ canopy top height
  heightMetrics: ['rh98', 'rh95', 'rh75', 'rh50', 'rh25'],
  
  // Additional useful metrics
  additionalMetrics: [
    'sensitivity',           // Beam sensitivity
    'solar_elevation',       // Solar elevation angle
    'quality_flag',          // Overall quality flag
    'degrade_flag',          // Degradation flag
    'cover',                 // Canopy cover fraction
    'pai',                   // Plant Area Index
    'fhd_normal'            // Foliage Height Diversity
  ],
  
  // Export settings
  exportFolder: 'carbon_estimator_gedi',
  exportCrs: 'EPSG:4326',
  
  debugMode: true
};

// =============================================================================
// DATA LOADING
// =============================================================================

/**
 * Load GEDI L2A (Elevation and Height Metrics)
 * Collection: LARSE/GEDI/GEDI02_A_002_MONTHLY
 * 
 * Each point is a 25m diameter footprint with LiDAR-derived height metrics
 */
var gediL2A = ee.ImageCollection('LARSE/GEDI/GEDI02_A_002_MONTHLY')
  .filterBounds(CONFIG.aoi)
  .filterDate(CONFIG.startDate, CONFIG.endDate);

print('GEDI L2A images found:', gediL2A.size());

// =============================================================================
// QUALITY FILTERING (CRITICAL)
// =============================================================================

/**
 * Apply strict quality filters to GEDI footprints
 * Source: Dubayah et al., 2020; Potapov et al., 2021
 * 
 * Filters applied:
 * 1. Beam sensitivity > 0.98 (ensures strong return signal)
 * 2. Quality flag = 1 (algorithm flagged as good)
 * 3. Degrade flag = 0 (not degraded by clouds, atmosphere)
 * 4. RH98 > 0 (valid height measurement)
 */
function applyQualityFilters(image) {
  var qf = CONFIG.qualityFilters;
  
  // Create quality mask
  var qualityMask = image.select('sensitivity').gte(qf.minBeamSensitivity)
    .and(image.select('quality_flag').eq(qf.qualityFlag))
    .and(image.select('degrade_flag').eq(qf.degrade))
    .and(image.select('rh98').gt(0))  // Valid height
    .and(image.select('rh98').lt(100)); // Realistic height (< 100m)
  
  return image.updateMask(qualityMask)
    .set('quality_filtered', true);
}

var gediFiltered = gediL2A.map(applyQualityFilters);

// Flatten to get all valid footprints across time
var gediMosaic = gediFiltered.mosaic();

print('GEDI quality filtering complete');

// =============================================================================
// SELECT HEIGHT METRICS
// =============================================================================

// Select height metrics and additional data
var heightBands = CONFIG.heightMetrics.concat(CONFIG.additionalMetrics);
var gediHeights = gediMosaic.select(heightBands);

// =============================================================================
// EXTRACT TO FEATURE COLLECTION (for ML training)
// =============================================================================

/**
 * Convert GEDI image to point feature collection
 * Each feature represents a single GEDI footprint with height metrics
 */
var gediPoints = gediHeights.sample({
  region: CONFIG.aoi,
  scale: 25,  // GEDI footprint size
  projection: CONFIG.exportCrs,
  geometries: true
});

print('GEDI footprints extracted:', gediPoints.size());

// =============================================================================
// DEBUG: VALIDATION
// =============================================================================

if (CONFIG.debugMode) {
  print('Sample GEDI footprints (first 10):', gediPoints.limit(10));
  
  // Statistics for RH98 (canopy top height)
  var rh98Stats = gediPoints.aggregate_stats('rh98');
  print('RH98 Statistics (canopy height, meters):', rh98Stats);
  
  // Check distribution
  var heightHistogram = ee.Feature(null, {
    'rh98_histogram': gediPoints.aggregate_histogram('rh98')
  });
  print('Height distribution:', heightHistogram);
  
  // Spatial coverage check
  print('AOI area (km²):', CONFIG.aoi.area().divide(1e6).getInfo());
  var footprintArea = gediPoints.size().multiply(Math.PI * 12.5 * 12.5); // 25m diameter
  print('Total GEDI footprint area (m²):', footprintArea);
  print('Coverage is SPARSE - this is normal for GEDI');
}

// =============================================================================
// VISUALIZATION
// =============================================================================

Map.centerObject(CONFIG.aoi, 12);
Map.addLayer(CONFIG.aoi, {color: 'red'}, 'AOI');

// Visualize GEDI heights
Map.addLayer(
  gediHeights.select('rh98'),
  {min: 0, max: 40, palette: ['white', 'yellow', 'green', 'darkgreen']},
  'GEDI RH98 (Canopy Height)'
);

// Visualize footprints as points
Map.addLayer(
  gediPoints.style({color: 'blue', pointSize: 2}),
  {},
  'GEDI Footprints'
);

// Canopy cover
Map.addLayer(
  gediHeights.select('cover'),
  {min: 0, max: 1, palette: ['white', 'green']},
  'Canopy Cover'
);

// =============================================================================
// EXPORT TO GOOGLE DRIVE
// =============================================================================

/**
 * Export GEDI heights as raster (for visualization)
 * Note: This is sparse - most pixels will be masked
 */
Export.image.toDrive({
  image: gediHeights.toFloat(),
  description: 'GEDI_heights_raster',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'gedi_heights_' + CONFIG.startDate + '_' + CONFIG.endDate,
  region: CONFIG.aoi,
  scale: 25,
  crs: CONFIG.exportCrs,
  maxPixels: 1e13,
  fileFormat: 'GeoTIFF',
  formatOptions: {
    cloudOptimized: true
  }
});

/**
 * Export GEDI footprints as vector (CSV) - RECOMMENDED
 * This preserves exact point locations and values for ML training
 */
Export.table.toDrive({
  collection: gediPoints,
  description: 'GEDI_footprints_vector',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'gedi_footprints_' + CONFIG.startDate + '_' + CONFIG.endDate,
  fileFormat: 'CSV'
});

// Also export as GeoJSON for GIS software
Export.table.toDrive({
  collection: gediPoints,
  description: 'GEDI_footprints_geojson',
  folder: CONFIG.exportFolder,
  fileNamePrefix: 'gedi_footprints_' + CONFIG.startDate + '_' + CONFIG.endDate,
  fileFormat: 'GeoJSON'
});

// =============================================================================
// METADATA
// =============================================================================

var metadata = ee.Dictionary({
  'data_source': 'LARSE/GEDI/GEDI02_A_002_MONTHLY',
  'product_level': 'L2A',
  'version': '002',
  'mission_start': '2019-04-18',
  'start_date': CONFIG.startDate,
  'end_date': CONFIG.endDate,
  'footprint_diameter_m': 25,
  'quality_filters': CONFIG.qualityFilters,
  'height_metrics': CONFIG.heightMetrics,
  'additional_metrics': CONFIG.additionalMetrics,
  'crs': CONFIG.exportCrs,
  'coverage': 'SPARSE (point samples only)',
  'use_case': 'ML training reference for canopy height prediction',
  'standards': 'GEDI L2A ATBD v2.0, Dubayah et al. 2020',
  'processing_date': ee.Date(Date.now()).format('YYYY-MM-dd').getInfo()
});

print('=== METADATA ===');
print(metadata);
print('=== IMPORTANT NOTES ===');
print('1. GEDI provides SPARSE footprints, not continuous coverage');
print('2. Use these as REFERENCE data for ML model training');
print('3. DO NOT interpolate GEDI heights blindly');
print('4. Export both raster (visualization) and vector (ML training)');
print('=== Export tasks created. Click "Run" in the Tasks tab ===');
