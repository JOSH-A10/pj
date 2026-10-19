import boto3
import json
from botocore import UNSIGNED
from botocore.config import Config
import pyproj

s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))

# Your AOI coordinates (Oregon) - WGS84
target_bounds = [-122.297879, 44.279232, -122.172223, 44.297909]
print(f"Target AOI (WGS84): Lon [{target_bounds[0]:.4f}, {target_bounds[2]:.4f}], Lat [{target_bounds[1]:.4f}, {target_bounds[3]:.4f}]")
print()

# Check Oregon datasets
datasets = [
    'OR_McKenzieRiver_1_2021',
    'OR_NRCSUSGS_1_2019',
    'OR_NRCSUSGS_2_2019', 
    'OR_NRCSUSGS_3_2019',
    'OR_NRCSUSGS_4_2019',
    'OR_NRCSUSGS_5_2019',
    'OR_OLCMetro_2019'
]

print("Checking which dataset covers your AOI...")
print()

# Web Mercator to WGS84 transformer
transformer = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

for ds in datasets:
    try:
        ept_key = f'{ds}/ept.json'
        obj = s3.get_object(Bucket='usgs-lidar-public', Key=ept_key)
        metadata = json.loads(obj['Body'].read())
        bounds = metadata.get('bounds', [])
        srs = metadata.get('srs', {}).get('wkt', '')
        
        if len(bounds) == 6:  # [minx, miny, minz, maxx, maxy, maxz]
            minx, miny, _, maxx, maxy, _ = bounds
            
            # Try to detect CRS and convert to WGS84
            if 'PROJCS' in srs or minx < -180:  # Projected coordinates
                # Assume Web Mercator (EPSG:3857) for USGS data
                minx_wgs, miny_wgs = transformer.transform(minx, miny)
                maxx_wgs, maxy_wgs = transformer.transform(maxx, maxy)
            else:  # Already in geographic coordinates
                minx_wgs, miny_wgs, maxx_wgs, maxy_wgs = minx, miny, maxx, maxy
            
            # Check if AOI intersects dataset bounds
            intersects = not (target_bounds[2] < minx_wgs or target_bounds[0] > maxx_wgs or
                            target_bounds[3] < miny_wgs or target_bounds[1] > maxy_wgs)
            
            status = "✓ COVERS AOI" if intersects else "  (no overlap)"
            print(f"{ds}:")
            print(f"  Bounds: Lon [{minx_wgs:.4f}, {maxx_wgs:.4f}], Lat [{miny_wgs:.4f}, {maxy_wgs:.4f}] {status}")
        else:
            print(f"{ds}: bounds format unexpected")
            
    except Exception as e:
        print(f"{ds}: {str(e)[:100]}")

print()
print("Recommendation: Use the dataset marked with ✓")
