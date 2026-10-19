"""
Extract Area of Interest (AOI) coordinates from an existing raster file.

This script reads a GeoTIFF and outputs coordinates in formats ready to use
in Google Earth Engine Code Editor and Planet API.

Usage:
    python scripts/extract_aoi_from_raster.py data/raw/your_file.tif
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import rasterio
from rasterio.warp import transform_bounds
import json


def extract_aoi_from_raster(raster_path: str):
    """
    Extract bounding box coordinates from a raster file.
    
    Args:
        raster_path: Path to the raster file (.tif, .tiff, etc.)
    
    Returns:
        dict: AOI information including bounds, CRS, and code snippets
    """
    raster_path = Path(raster_path)
    
    if not raster_path.exists():
        raise FileNotFoundError(f"Raster file not found: {raster_path}")
    
    print(f"\n{'='*70}")
    print(f"EXTRACTING AOI FROM: {raster_path.name}")
    print(f"{'='*70}\n")
    
    with rasterio.open(raster_path) as src:
        # Get bounds in native CRS
        bounds = src.bounds  # (left, bottom, right, top)
        native_crs = src.crs
        
        print(f"📍 Raster Information:")
        print(f"   Native CRS: {native_crs}")
        print(f"   Shape: {src.width} x {src.height} pixels")
        print(f"   Resolution: {src.res[0]:.2f} x {src.res[1]:.2f} units")
        print(f"   Native Bounds:")
        print(f"      Left:   {bounds.left:.6f}")
        print(f"      Bottom: {bounds.bottom:.6f}")
        print(f"      Right:  {bounds.right:.6f}")
        print(f"      Top:    {bounds.top:.6f}")
        
        # Transform to WGS84 (EPSG:4326) for GEE and Planet
        if native_crs.to_epsg() != 4326:
            wgs84_bounds = transform_bounds(
                native_crs, 
                'EPSG:4326', 
                bounds.left, 
                bounds.bottom, 
                bounds.right, 
                bounds.top
            )
            lon_min, lat_min, lon_max, lat_max = wgs84_bounds
            print(f"\n🌍 WGS84 (EPSG:4326) Bounds:")
        else:
            lon_min, lat_min, lon_max, lat_max = bounds.left, bounds.bottom, bounds.right, bounds.top
            print(f"\n🌍 Bounds (already in WGS84):")
        
        print(f"      West:  {lon_min:.6f}°")
        print(f"      South: {lat_min:.6f}°")
        print(f"      East:  {lon_max:.6f}°")
        print(f"      North: {lat_max:.6f}°")
        
        # Calculate center and area
        center_lon = (lon_min + lon_max) / 2
        center_lat = (lat_min + lat_max) / 2
        
        # Approximate area (degrees to km at equator: 1° ≈ 111 km)
        width_km = (lon_max - lon_min) * 111 * abs(center_lat / 90)
        height_km = (lat_max - lat_min) * 111
        area_km2 = width_km * height_km
        
        print(f"\n📐 Area Information:")
        print(f"   Center: ({center_lon:.6f}, {center_lat:.6f})")
        print(f"   Approximate size: {width_km:.2f} km × {height_km:.2f} km")
        print(f"   Approximate area: {area_km2:.2f} km²")
        
        # Generate code snippets
        print(f"\n{'='*70}")
        print("📋 COPY-PASTE CODE SNIPPETS")
        print(f"{'='*70}\n")
        
        # GEE JavaScript
        print("🟢 Google Earth Engine Code Editor (JavaScript):")
        print("─" * 70)
        gee_code = f"""// Area of Interest from {raster_path.name}
var aoi = ee.Geometry.Rectangle([
  {lon_min:.6f}, {lat_min:.6f},  // [west, south]
  {lon_max:.6f}, {lat_max:.6f}   // [east, north]
]);

// Or as a bounding box
var bbox = [{lon_min:.6f}, {lat_min:.6f}, {lon_max:.6f}, {lat_max:.6f}];

// Center point
var center = ee.Geometry.Point([{center_lon:.6f}, {center_lat:.6f}]);

// Visualize AOI
Map.centerObject(aoi, 12);
Map.addLayer(aoi, {{color: 'red'}}, 'AOI');

print('AOI area (km²):', aoi.area().divide(1e6));"""
        print(gee_code)
        
        # Python GEE
        print("\n🐍 Python (Google Earth Engine):")
        print("─" * 70)
        python_gee_code = f"""import ee
ee.Initialize(project='heroic-bliss-472519-f7')

# Area of Interest from {raster_path.name}
aoi = ee.Geometry.Rectangle([
    {lon_min:.6f}, {lat_min:.6f},  # [west, south]
    {lon_max:.6f}, {lat_max:.6f}   # [east, north]
])

# Or as bbox list
bbox = [{lon_min:.6f}, {lat_min:.6f}, {lon_max:.6f}, {lat_max:.6f}]"""
        print(python_gee_code)
        
        # Planet API
        print("\n🌎 Python (Planet Labs API):")
        print("─" * 70)
        planet_code = f"""from planet import Auth, DataClient
from datetime import datetime

auth = Auth.from_env()
client = DataClient(auth)

# Area of Interest from {raster_path.name}
aoi = {{
    "type": "Polygon",
    "coordinates": [[
        [{lon_min:.6f}, {lat_min:.6f}],  # SW
        [{lon_max:.6f}, {lat_min:.6f}],  # SE
        [{lon_max:.6f}, {lat_max:.6f}],  # NE
        [{lon_min:.6f}, {lat_max:.6f}],  # NW
        [{lon_min:.6f}, {lat_min:.6f}]   # Close polygon
    ]]
}}

# Search for images
search_request = {{
    "item_types": ["PSScene"],
    "filter": {{
        "type": "AndFilter",
        "config": [
            {{"type": "GeometryFilter", "field_name": "geometry", "config": aoi}},
            {{"type": "DateRangeFilter", "field_name": "acquired", 
              "config": {{"gte": "2023-01-01T00:00:00Z", "lte": "2023-12-31T23:59:59Z"}}}}
        ]
    }}
}}

results = client.quick_search(search_request)"""
        print(planet_code)
        
        # GeoJSON
        print("\n📄 GeoJSON (for upload to GEE Assets or other tools):")
        print("─" * 70)
        geojson = {
            "type": "Feature",
            "properties": {
                "name": raster_path.stem,
                "source": str(raster_path),
                "area_km2": round(area_km2, 2)
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon_min, lat_min],
                    [lon_max, lat_min],
                    [lon_max, lat_max],
                    [lon_min, lat_max],
                    [lon_min, lat_min]
                ]]
            }
        }
        print(json.dumps(geojson, indent=2))
        
        # Save GeoJSON
        geojson_path = raster_path.parent / f"{raster_path.stem}_aoi.geojson"
        with open(geojson_path, 'w') as f:
            json.dump(geojson, f, indent=2)
        print(f"\n💾 Saved to: {geojson_path}")
        
        print(f"\n{'='*70}")
        print("✅ NEXT STEPS")
        print(f"{'='*70}\n")
        print("1. Copy the GEE JavaScript code above")
        print("   → Paste into https://code.earthengine.google.com/")
        print("   → Use 'aoi' variable in your Sentinel-2/1/GEDI extraction scripts")
        print()
        print("2. The GeoJSON file can be:")
        print("   → Uploaded to GEE Assets tab")
        print("   → Used in QGIS/ArcGIS")
        print("   → Passed to other geospatial tools")
        print()
        print("3. For Planet API, use the Python code above to search for imagery")
        print()
        
        return {
            'bounds_wgs84': (lon_min, lat_min, lon_max, lat_max),
            'center': (center_lon, center_lat),
            'area_km2': area_km2,
            'native_crs': str(native_crs),
            'geojson_path': str(geojson_path)
        }


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("❌ Error: No raster file provided")
        print()
        print("Usage:")
        print("  python scripts/extract_aoi_from_raster.py <path_to_raster.tif>")
        print()
        print("Example:")
        print("  python scripts/extract_aoi_from_raster.py data/raw/fused_2023_2024.tif")
        print("  python scripts/extract_aoi_from_raster.py data/raw/study_area.tif")
        sys.exit(1)
    
    raster_path = sys.argv[1]
    
    try:
        result = extract_aoi_from_raster(raster_path)
        print(f"✅ Successfully extracted AOI from {Path(raster_path).name}")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
