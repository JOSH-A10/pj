# GEDI Data Fetching - Quick Reference

## The Problem
- Your 3 ha plantation is too small for sparse GEDI coverage (0 footprints found)
- GEDI spacing: 60m × 600m (much larger than your 161m × 179m AOI)

## The Solution
Use Google Earth Engine Code Editor for **interactive manual extraction**:
1. Expand AOI to ~5-25 km² (captures 50-800 GEDI footprints)
2. Export GEDI points to Google Drive
3. Use as training data to calibrate height model
4. Apply calibrated model to your 3 ha plantation

---

## Step-by-Step Workflow

### 1. Open GEE Code Editor
https://code.earthengine.google.com/

### 2. Load Script
Copy contents of: `gee/03_extract_gedi.js`

### 3. Run Script
Click **"Run"** button
- Map shows: AOI boundary (red) + GEDI footprints (blue points)
- Console shows: Number of footprints + height statistics

### 4. Export Data
In **Tasks** tab (right panel), click **"Run"** on:
- `GEDI_footprints_vector` (CSV) ← **Use this for ML**
- `GEDI_footprints_geojson` (GeoJSON) ← For GIS visualization

### 5. Download from Google Drive
Folder: `carbon_estimator_gedi/`
File: `gedi_footprints_2019-04-18_2024-12-31.csv`

### 6. Convert CSV to GeoJSON
```bash
python scripts/convert_gedi_csv_to_geojson.py \
  --input data/gedi/gedi_footprints_2019-04-18_2024-12-31.csv \
  --output data/gedi/gedi_points.geojson
```

### 7. Run GEDI-Calibrated Carbon Estimation
```bash
python scripts/plantation_carbon_estimation.py \
  --gedi-points data/gedi/gedi_points.geojson \
  --use-gedi \
  --output results/carbon_gedi_calibrated.json
```

---

## Expected Results

| AOI Size | GEDI Footprints | Suitable for Training? |
|----------|----------------|------------------------|
| 3 ha (your plantation) | 0-2 | ❌ No |
| 5 km × 5 km (25 km²) | 50-200 | ✅ Yes |
| 10 km × 10 km (100 km²) | 200-800 | ✅ Yes (better) |

---

## Uncertainty Improvement

| Method | Height Error | Carbon Uncertainty |
|--------|-------------|-------------------|
| SAR-only (current) | ±28% | **±68.5%** |
| GEDI-calibrated | ±10-15% | **±30-40%** |

**Improvement: ~50% reduction in uncertainty!**

---

## Files Overview

| File | Purpose |
|------|---------|
| `gee/03_extract_gedi.js` | GEE Code Editor script (updated with your AOI) |
| `gee/GEDI_EXTRACTION_GUIDE.md` | Detailed instructions + troubleshooting |
| `scripts/convert_gedi_csv_to_geojson.py` | CSV → GeoJSON converter |
| `scripts/fetch_gedi_data.py` | Python API method (didn't work, AOI too small) |
| `src/gedi_calibration.py` | Random Forest calibration module (ready to use) |

---

## Default AOI Configuration

Script is pre-configured for Kerala, India:
```javascript
// 5 km × 5 km region (centered on 76.8127°E, 9.4660°N)
aoi: ee.Geometry.Rectangle([76.79, 9.44, 76.84, 9.49])
```

To expand (uncomment in script):
```javascript
// 10 km × 10 km region
aoi: ee.Geometry.Rectangle([76.75, 9.40, 76.85, 9.50])
```

---

## Calibration Strategy

1. **Regional training**: Use GEDI from 5-25 km² region
2. **Feature extraction**: Sample Sentinel-2/1 at GEDI locations
3. **Model training**: Random Forest (Height = f(NDVI, EVI, VV, VH, texture))
4. **Wall-to-wall prediction**: Apply model to entire region (10m resolution)
5. **Plantation extraction**: Clip height map to 3 ha boundaries

**Result**: GEDI-calibrated height map for your plantation!

---

## Key Insight

**GEDI is NOT wall-to-wall coverage** - it's sparse reference data for calibration.
- ❌ Don't interpolate GEDI directly
- ✅ Use GEDI to train Sentinel-based height model
- ✅ Apply model for continuous 10m height map

---

## Need Help?

- **No GEDI footprints?** → Expand AOI to at least 5 km × 5 km
- **Export failed?** → Check Google Drive storage space
- **Unrealistic heights?** → Check quality filters in script
- **CSV parsing error?** → Use converter script provided

For detailed guide: `gee/GEDI_EXTRACTION_GUIDE.md`
