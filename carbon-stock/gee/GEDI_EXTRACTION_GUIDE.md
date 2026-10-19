# GEDI Data Extraction Guide for GEE Code Editor

## Overview
This guide explains how to fetch GEDI L2A canopy height data using Google Earth Engine Code Editor for manual download and processing.

## Why GEE Code Editor Instead of Python API?

Your plantation AOI (3 ha, 161m × 179m) is **too small** for sparse GEDI coverage:
- GEDI footprint spacing: 60m along-track, 600m cross-track
- Your AOI width: 161m
- Result: 0 GEDI shots found in direct fetch

**Solution**: Use interactive GEE Code Editor to:
1. Visually expand AOI to capture GEDI footprints (~5-25 km²)
2. Train calibration model on regional GEDI data
3. Apply model to your 3 ha plantation for wall-to-wall height map

---

## Step-by-Step Instructions

### 1. Open Google Earth Engine Code Editor
- Navigate to: https://code.earthengine.google.com/
- Sign in with your Google account (must have Earth Engine access)

### 2. Load the GEDI Extraction Script
- Open file: `gee/03_extract_gedi.js`
- Copy entire contents
- Paste into GEE Code Editor main panel

### 3. Adjust AOI (if needed)

**Default AOI** (5 km × 5 km, centered on your plantation):
```javascript
aoi: ee.Geometry.Rectangle([76.79, 9.44, 76.84, 9.49])
```

**Option A: Expand for more data** (10 km × 10 km):
```javascript
// Uncomment this line in the script:
aoi: ee.Geometry.Rectangle([76.75, 9.40, 76.85, 9.50])
```

**Option B: Draw custom polygon**:
1. Use map drawing tools (polygon icon in top-left of map)
2. Draw region around your plantation
3. In Geometry Imports panel, name it "userAOI"
4. Replace `aoi:` line with: `aoi: userAOI`

### 4. Run the Script
- Click **"Run"** button at top of editor
- Map will center on your AOI
- Console will show:
  ```
  GEDI L2A images found: [N]
  GEDI footprints extracted: [N]
  RH98 Statistics (canopy height, meters): {min, max, mean, stdDev}
  ```

**Expected Results**:
- **5 km × 5 km AOI**: 50-200 GEDI footprints (good for calibration)
- **10 km × 10 km AOI**: 200-800 footprints (better for training)
- **3 ha plantation**: 0-2 footprints (not sufficient)

### 5. Inspect Visualization
Map layers added:
- **Red boundary**: Your AOI
- **Blue points**: GEDI footprints (25m diameter)
- **Green raster**: RH98 canopy height (sparse coverage)
- **Canopy cover**: GEDI-derived cover fraction

**Verify**:
- GEDI points are distributed across AOI
- Heights are realistic (0-40m for tropical plantations)
- Coverage is sparse but adequate for calibration

### 6. Export Data

**Three export tasks** are created automatically in the **Tasks** tab (right panel):

#### Task 1: GEDI Footprints (CSV) - **RECOMMENDED FOR ML**
- **Description**: `GEDI_footprints_vector`
- **Format**: CSV with columns:
  - `.geo` (coordinates)
  - `rh98` (canopy top height, meters)
  - `rh95`, `rh75`, `rh50`, `rh25` (height percentiles)
  - `sensitivity`, `quality_flag`, `cover`, etc.
- **Use**: Machine learning training data
- **Click "Run"** → Data exports to Google Drive folder `carbon_estimator_gedi/`

#### Task 2: GEDI Footprints (GeoJSON)
- **Description**: `GEDI_footprints_geojson`
- **Format**: GeoJSON for GIS software (QGIS, ArcGIS)
- **Use**: Visualization, spatial analysis
- **Click "Run"** → Exports to same Drive folder

#### Task 3: GEDI Heights (GeoTIFF Raster)
- **Description**: `GEDI_heights_raster`
- **Format**: Multi-band raster (rh98, rh95, cover, etc.)
- **Note**: Very sparse - most pixels are NoData
- **Use**: Quick visualization only
- **Click "Run"** → Exports to Drive (may take 5-10 minutes)

### 7. Download from Google Drive
1. Open Google Drive: https://drive.google.com/
2. Navigate to folder: `carbon_estimator_gedi/`
3. Files will appear as:
   - `gedi_footprints_2019-04-18_2024-12-31.csv`
   - `gedi_footprints_2019-04-18_2024-12-31.geojson`
   - `gedi_heights_2019-04-18_2024-12-31.tif`
4. Download CSV and/or GeoJSON (CSV recommended for ML pipeline)

### 8. Place in Project Directory
```bash
# Move downloaded file to project data folder:
carbon-estimator/
└── data/
    └── gedi/
        └── gedi_footprints_2019-04-18_2024-12-31.csv
```

### 9. Convert CSV to GeoJSON (if needed)
If you downloaded CSV and need GeoJSON for the calibration pipeline:

```python
python scripts/convert_gedi_csv_to_geojson.py \
  --input data/gedi/gedi_footprints_2019-04-18_2024-12-31.csv \
  --output data/gedi/gedi_points.geojson
```

Or manually convert:
```python
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point

# Read CSV
df = pd.read_csv('data/gedi/gedi_footprints_2019-04-18_2024-12-31.csv')

# Extract coordinates from .geo column (format: "{lon lat}")
df[['lon', 'lat']] = df['.geo'].str.strip('{}').str.split(expand=True).astype(float)
df['geometry'] = df.apply(lambda row: Point(row['lon'], row['lat']), axis=1)

# Create GeoDataFrame
gdf = gpd.GeoDataFrame(df, geometry='geometry', crs='EPSG:4326')

# Save as GeoJSON
gdf.to_file('data/gedi/gedi_points.geojson', driver='GeoJSON')
```

---

## Using GEDI Data in Calibration Pipeline

### Run GEDI-Calibrated Height Estimation
```bash
python scripts/plantation_carbon_estimation.py \
  --gedi-points data/gedi/gedi_points.geojson \
  --use-gedi \
  --output results/carbon_gedi_calibrated.json
```

### Expected Uncertainty Improvement
| Method | Height Uncertainty | Carbon Uncertainty |
|--------|-------------------|-------------------|
| SAR-only (current) | ±28% | ±68.5% |
| **GEDI-calibrated** | **±10-15%** | **±30-40%** |

---

## Troubleshooting

### Q: No GEDI footprints found (count = 0)
**A**: AOI too small. Expand to at least 5 km × 5 km (25 km²).

### Q: Very few footprints (< 20)
**A**: Expand AOI further (10 km × 10 km) or extend date range.

### Q: Export task fails with "User memory limit exceeded"
**A**: Reduce AOI size or export only CSV (not raster).

### Q: Heights look unrealistic (> 50m for plantation)
**A**: Check quality filters in script:
```javascript
qualityFilters: {
  minBeamSensitivity: 0.98,  // Increase to 0.99 for stricter filtering
  qualityFlag: 1,
  degrade: 0
}
```

### Q: CSV has strange .geo column format
**A**: This is Earth Engine's lat/lon format: `{lon lat}`. Use conversion script above.

---

## Technical Details

### GEDI L2A Dataset
- **Collection**: `LARSE/GEDI/GEDI02_A_002_MONTHLY`
- **Product Level**: L2A (Elevation and Height Metrics)
- **Temporal Coverage**: 2019-04-18 to present
- **Footprint Size**: 25m diameter
- **Spacing**: ~60m along-track, ~600m cross-track
- **Coverage**: Sparse point samples, NOT wall-to-wall

### Height Metrics Exported
| Metric | Description | Use |
|--------|-------------|-----|
| **rh98** | 98th percentile height | **Canopy top** (primary for ML) |
| rh95 | 95th percentile | Upper canopy |
| rh75 | 75th percentile | Mid-upper canopy |
| rh50 | 50th percentile (median) | Mid canopy |
| rh25 | 25th percentile | Lower canopy |
| cover | Canopy cover fraction (0-1) | Validation |
| sensitivity | Beam sensitivity (0-1) | Quality check |
| quality_flag | Algorithm quality (1=good) | Filtering |

### Quality Filters Applied
```javascript
sensitivity >= 0.98        // Strong return signal
quality_flag == 1          // Algorithm flagged as good quality
degrade_flag == 0          // Not degraded by clouds/atmosphere
rh98 > 0 AND rh98 < 100   // Valid, realistic height
```

### References
- Dubayah, R. et al. (2020). *Science of Remote Sensing*, 2, 100002
- NASA GEDI L2A Data User Guide v2.0
- GEDI L2A Algorithm Theoretical Basis Document (ATBD) v2.0

---

## Next Steps

1. **Fetch GEDI data** using this guide (5-10 minutes)
2. **Download CSV** from Google Drive
3. **Convert to GeoJSON** (if needed)
4. **Run calibration pipeline**:
   ```bash
   python scripts/plantation_carbon_estimation.py \
     --gedi-points data/gedi/gedi_points.geojson \
     --use-gedi
   ```
5. **Compare results**:
   - SAR-only: 31.54 ± 21.61 Mg CO₂e (±68.5%)
   - GEDI-calibrated: [to be determined] (expected ±30-40%)

---

## Questions?
- Check GEE Code Editor console for error messages
- Inspect map visualization to verify GEDI coverage
- Ensure AOI is large enough (≥25 km²) for adequate footprints
- Verify Google Drive has sufficient storage (exports are small, <10 MB)

**Key Insight**: GEDI is sparse by design - it provides reference points for calibration, not continuous coverage. Train model on regional GEDI data, then apply to your 3 ha plantation for wall-to-wall height prediction.
