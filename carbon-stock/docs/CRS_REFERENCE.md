# CRS Reference Guide for LiDAR Processing

## Quick Reference

### When to Use Each CRS

| Use Case | Recommended CRS | EPSG Code | Notes |
|----------|----------------|-----------|-------|
| Input AOI | WGS84 Geographic | `EPSG:4326` | Lat/lon coordinates |
| LAZ Processing | Auto-detect | Varies | Use native projected CRS |
| California (West) | WGS84 UTM 10N | `EPSG:32610` | Most common |
| California (East) | WGS84 UTM 11N | `EPSG:32611` | Sierra Nevada region |
| Oregon/Washington | WGS84 UTM 10N | `EPSG:32610` | Pacific Northwest |
| Arizona | WGS84 UTM 12N | `EPSG:32612` | Southwest |
| Texas | WGS84 UTM 13-15N | `EPSG:32613-32615` | Varies by longitude |

## Understanding Coordinate Systems

### Geographic vs Projected

**Geographic (EPSG:4326)**
- **Units**: Degrees (latitude/longitude)
- **Range**: Latitude [-90, 90], Longitude [-180, 180]
- **Use**: Input AOI, global datasets
- **Don't Use**: LAZ processing (distance calculations inaccurate)

**Projected (UTM)**
- **Units**: Meters (easting/northing)
- **Range**: Varies by zone (typically 100,000 - 900,000)
- **Use**: LAZ processing, raster outputs, area calculations
- **Benefit**: Preserves distances and areas

## UTM Zone Calculation

### Formula
```python
utm_zone = int((longitude + 180) / 6) + 1
```

### Examples

| Location | Longitude | Zone Calculation | UTM Zone | EPSG Code |
|----------|-----------|------------------|----------|-----------|
| San Francisco, CA | -122.4° | `(-122.4 + 180) / 6 + 1` = 10.6 → 10 | 10N | EPSG:32610 |
| Los Angeles, CA | -118.2° | `(-118.2 + 180) / 6 + 1` = 11.3 → 11 | 11N | EPSG:32611 |
| Phoenix, AZ | -112.1° | `(-112.1 + 180) / 6 + 1` = 12.3 → 12 | 12N | EPSG:32612 |
| Denver, CO | -104.9° | `(-104.9 + 180) / 6 + 1` = 13.5 → 13 | 13N | EPSG:32613 |
| Houston, TX | -95.4° | `(-95.4 + 180) / 6 + 1` = 15.1 → 15 | 15N | EPSG:32615 |

### Hemisphere

- **Northern Hemisphere** (lat ≥ 0): EPSG code = `32600 + zone`
- **Southern Hemisphere** (lat < 0): EPSG code = `32700 + zone`

## US State UTM Zones

### Western States

| State | Primary Zones | EPSG Codes |
|-------|---------------|------------|
| California | 10N, 11N | EPSG:32610, EPSG:32611 |
| Oregon | 10N | EPSG:32610 |
| Washington | 10N, 11N | EPSG:32610, EPSG:32611 |
| Nevada | 11N | EPSG:32611 |
| Arizona | 12N | EPSG:32612 |
| New Mexico | 12N, 13N | EPSG:32612, EPSG:32613 |
| Utah | 12N | EPSG:32612 |
| Idaho | 11N, 12N | EPSG:32611, EPSG:32612 |

### Central States

| State | Primary Zones | EPSG Codes |
|-------|---------------|------------|
| Colorado | 13N | EPSG:32613 |
| Wyoming | 12N, 13N | EPSG:32612, EPSG:32613 |
| Montana | 12N, 13N | EPSG:32612, EPSG:32613 |
| Texas | 13N, 14N, 15N | EPSG:32613-32615 |
| Oklahoma | 14N, 15N | EPSG:32614, EPSG:32615 |
| Kansas | 14N, 15N | EPSG:32614, EPSG:32615 |

### Eastern States

| State | Primary Zones | EPSG Codes |
|-------|---------------|------------|
| Florida | 16N, 17N | EPSG:32616, EPSG:32617 |
| Georgia | 16N, 17N | EPSG:32616, EPSG:32617 |
| New York | 18N | EPSG:32618 |
| Pennsylvania | 17N, 18N | EPSG:32617, EPSG:32618 |
| North Carolina | 17N, 18N | EPSG:32617, EPSG:32618 |

## State Plane vs UTM

### State Plane Coordinate System (SPCS)

- **Purpose**: High-accuracy surveying within states
- **Units**: Feet (US Survey or International) or meters
- **Coverage**: State-specific zones
- **EPSG**: Varies (e.g., California Zone 1: EPSG:2225)

### When to Use State Plane
- Local government projects requiring survey-grade accuracy
- Legal boundary descriptions
- Engineering projects within single state

### When to Use UTM
- **Regional analysis** spanning multiple states
- **Scientific research** (standard global system)
- **LiDAR processing** (USGS 3DEP uses UTM)
- **International projects**

## CRS Detection Logic

### Pipeline Priority

```
1. USER OVERRIDE (--lidar-crs)
   └── Use explicitly specified CRS
   
2. LAZ HEADER CRS
   └── Read from .laz/.las file metadata
   
3. AUTO-DETECTION
   ├── If |x| < 180 and |y| < 90
   │   └── Return EPSG:4326 (Geographic)
   └── Else (Projected coordinates)
       ├── Get AOI centroid → (longitude, latitude)
       ├── Calculate UTM zone
       ├── Determine hemisphere (N/S)
       └── Return EPSG:326XX or EPSG:327XX
```

### Auto-Detection Examples

**Example 1: Geographic Coordinates**
```
LAZ bounds: (-124.5, 40.5, -124.0, 41.0)
Center: (-124.25, 40.75)
Check: |-124.25| < 180? NO → |40.75| < 90? YES → Ambiguous
→ Fallback: Compute UTM from AOI centroid
```

**Example 2: Projected Coordinates (California)**
```
LAZ bounds: (450000, 4500000, 460000, 4510000)
Center: (455000, 4505000)
Check: |455000| < 180? NO → Projected coordinates detected
AOI centroid: (-122.5°, 37.8°)
UTM zone: int((-122.5 + 180) / 6) + 1 = 10
Latitude: 37.8° → Northern hemisphere
EPSG: 32600 + 10 = 32610
→ Return: EPSG:32610 (WGS84 UTM 10N)
```

## Common CRS Transformations

### AOI Workflow

```python
import geopandas as gpd

# 1. Load AOI (any CRS)
aoi = gpd.read_file('aoi.geojson')  # CRS: EPSG:4326

# 2. Reproject to LAZ native CRS
aoi_utm = aoi.to_crs('EPSG:32610')  # WGS84 UTM 10N

# 3. Extract bounds for PDAL
bounds = aoi_utm.total_bounds
# Output: (minx, miny, maxx, maxy) in meters

# 4. Get polygon WKT for cropping
crop_polygon = aoi_utm.geometry.unary_union.wkt
# Output: "POLYGON ((x1 y1, x2 y2, ...))"
```

### Coordinate Conversion

```python
from pyproj import Transformer

# WGS84 to UTM 10N
transformer = Transformer.from_crs('EPSG:4326', 'EPSG:32610', always_xy=True)
x_utm, y_utm = transformer.transform(-122.5, 37.8)
# Output: (545000.0, 4185000.0) - UTM coordinates in meters

# UTM 10N to WGS84
transformer_inv = Transformer.from_crs('EPSG:32610', 'EPSG:4326', always_xy=True)
lon, lat = transformer_inv.transform(545000.0, 4185000.0)
# Output: (-122.5, 37.8) - Geographic coordinates
```

## Validation Rules

### Valid EPSG Codes

✅ **Accepted**:
- `EPSG:4326` (WGS84 Geographic)
- `EPSG:32601` to `EPSG:32660` (WGS84 UTM Northern Hemisphere)
- `EPSG:32701` to `EPSG:32760` (WGS84 UTM Southern Hemisphere)
- `EPSG:3857` (Web Mercator) - **NOT RECOMMENDED for LiDAR**

❌ **Rejected**:
- Invalid UTM zones (zone < 1 or zone > 60)
- Invalid EPSG codes (not in 32601-32660 or 32701-32760 range)
- `EPSG:3857` for LAZ processing (Web Mercator causes distortion)

### Coordinate Range Checks

```python
# Geographic (EPSG:4326)
assert -180 <= longitude <= 180
assert -90 <= latitude <= 90

# Projected (UTM)
# No strict bounds, but typical ranges:
# Easting: 100,000 - 900,000 meters
# Northing: 0 - 10,000,000 meters (varies by hemisphere)
```

## Troubleshooting CRS Issues

### Issue: "Cannot determine CRS from LAZ tiles"

**Symptoms**:
- LAZ files have no CRS metadata
- Auto-detection fails
- Pipeline aborts

**Solutions**:

1. **Find correct UTM zone**:
   - Use online tool: https://mangomap.com/robertyoung/maps/69585/what-utm-zone-am-i-in-
   - Or calculate manually: `utm_zone = int((longitude + 180) / 6) + 1`

2. **Override CRS**:
   ```powershell
   python scripts\run_pipeline.py --lidar-crs EPSG:32610
   ```

3. **Check LAZ metadata**:
   ```powershell
   pdal info input.laz --metadata | grep -i "srs"
   ```

### Issue: "AOI does not intersect with LAZ tile bounds"

**Symptoms**:
- AOI and LAZ bounds shown in different coordinate systems
- Reprojection failed
- No overlap between geometries

**Solutions**:

1. **Verify AOI CRS**:
   ```python
   import geopandas as gpd
   aoi = gpd.read_file('aoi.geojson')
   print(aoi.crs)  # Should be EPSG:4326
   ```

2. **Check coordinate order**:
   - GeoJSON: `[longitude, latitude]` (correct)
   - Some formats: `[latitude, longitude]` (wrong!)

3. **Visualize in QGIS**:
   - Load AOI and LAZ extent layers
   - Verify they overlap visually

### Issue: Wrong UTM zone detected

**Symptoms**:
- Output appears shifted by ~600 km
- Raster coordinates don't match LAZ bounds
- Processing succeeds but results wrong

**Solutions**:

1. **Check AOI centroid**:
   ```python
   import geopandas as gpd
   aoi = gpd.read_file('aoi.geojson')
   aoi_wgs84 = aoi.to_crs('EPSG:4326')
   centroid = aoi_wgs84.geometry.centroid.iloc[0]
   print(f"Centroid: {centroid.x}, {centroid.y}")
   ```

2. **Override with correct zone**:
   - Zone boundary issue (near zone edges)
   - Use `--lidar-crs` to force correct zone

## Additional Resources

### Online Tools
- **UTM Zone Finder**: https://mangomap.com/robertyoung/maps/69585/
- **EPSG.io**: https://epsg.io/ (CRS database)
- **Proj.org**: https://proj.org/ (projection library)

### Python Libraries
- **pyproj**: CRS transformations
- **geopandas**: Spatial data operations
- **rasterio**: Raster CRS management
- **laspy**: LAZ/LAS CRS reading

### Standards
- **EPSG Registry**: https://epsg.org/
- **OGC WKT2**: CRS definition standard
- **ISO 19111**: Geographic information standard

---

**Last Updated**: 2024  
**Pipeline Version**: 2.0 (Auto-detection enabled)
