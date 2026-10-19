"""
Automated Planet Labs Data Downloader

Downloads PlanetScope imagery for a given AOI automatically.
Uses Planet API to search and download 3m resolution imagery.

Usage:
    python scripts/download_planet_data.py --aoi data/raw/FUSED_AOI_2023_2024.tif
    python scripts/download_planet_data.py --bbox 76.811 9.465 76.813 9.467
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
from datetime import datetime
import json
import os
import rasterio
from rasterio.warp import transform_bounds
from planet import Auth, Session, DataClient, OrdersClient
from planet.specs import get_item_types
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    load_dotenv(env_path)
    logging.info(f"✓ Loaded API keys from {env_path}")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PlanetDataDownloader:
    """Automated Planet Labs data downloader"""
    
    def __init__(self, aoi_bbox: tuple, start_date: str, end_date: str, output_dir: Path):
        """
        Initialize downloader
        
        Args:
            aoi_bbox: (west, south, east, north) in WGS84
            start_date: Start date 'YYYY-MM-DD'
            end_date: End date 'YYYY-MM-DD'
            output_dir: Output directory
        """
        self.aoi_bbox = aoi_bbox
        self.start_date = start_date
        self.end_date = end_date
        self.output_dir = Path(output_dir) / "planet"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize Planet API
        try:
            self.auth = Auth.from_env()
            self.session = Session(auth=self.auth)
            self.data_client = DataClient(self.session)
            self.orders_client = OrdersClient(self.session)
            logger.info("✓ Planet Labs API initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Planet API: {e}")
            logger.error("Make sure PL_API_KEY environment variable is set")
            raise
        
        # Create GeoJSON AOI
        self.aoi_geojson = {
            "type": "Polygon",
            "coordinates": [[
                [aoi_bbox[0], aoi_bbox[1]],  # SW
                [aoi_bbox[2], aoi_bbox[1]],  # SE
                [aoi_bbox[2], aoi_bbox[3]],  # NE
                [aoi_bbox[0], aoi_bbox[3]],  # NW
                [aoi_bbox[0], aoi_bbox[1]]   # Close
            ]]
        }
    
    def search_images(self, max_cloud_cover=20, limit=100):
        """
        Search for PlanetScope images
        
        Args:
            max_cloud_cover: Maximum cloud cover percentage
            limit: Maximum number of results
        
        Returns:
            List of image items
        """
        logger.info("\n" + "="*70)
        logger.info("SEARCHING PLANET LABS IMAGERY")
        logger.info("="*70)
        logger.info(f"AOI: {self.aoi_bbox}")
        logger.info(f"Date range: {self.start_date} to {self.end_date}")
        logger.info(f"Max cloud cover: {max_cloud_cover}%")
        
        # Build search request
        search_filter = {
            "type": "AndFilter",
            "config": [
                {
                    "type": "GeometryFilter",
                    "field_name": "geometry",
                    "config": self.aoi_geojson
                },
                {
                    "type": "DateRangeFilter",
                    "field_name": "acquired",
                    "config": {
                        "gte": f"{self.start_date}T00:00:00Z",
                        "lte": f"{self.end_date}T23:59:59Z"
                    }
                },
                {
                    "type": "RangeFilter",
                    "field_name": "cloud_cover",
                    "config": {
                        "lte": max_cloud_cover / 100.0
                    }
                }
            ]
        }
        
        # Execute search
        logger.info("Searching Planet catalog...")
        results = []
        
        try:
            # Use the quick search endpoint
            search_request = {
                "item_types": ["PSScene"],
                "filter": search_filter
            }
            
            # Create search
            search_result = self.data_client.create_search(search_request)
            
            # Iterate through pages
            for item in self.data_client.run_search(search_result['id'], limit=limit):
                results.append(item)
                if len(results) >= limit:
                    break
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            logger.info("\nTrying alternative search method...")
            
            # Fallback: Use REST API directly
            try:
                import os
                api_key = os.getenv('PL_API_KEY')
                if not api_key:
                    raise ValueError("PL_API_KEY environment variable not set")
                
                headers = {'Authorization': f'api-key {api_key}'}
                search_url = 'https://api.planet.com/data/v1/quick-search'
                
                search_body = {
                    "item_types": ["PSScene"],
                    "filter": search_filter
                }
                
                response = requests.post(search_url, json=search_body, headers=headers)
                response.raise_for_status()
                
                data = response.json()
                results = data.get('features', [])
                logger.info(f"✓ Found {len(results)} images using REST API")
                
            except Exception as e2:
                logger.error(f"Alternative search also failed: {e2}")
                return []
        
        logger.info(f"✓ Found {len(results)} images")
        
        if len(results) == 0:
            logger.warning("No images found matching criteria")
            return []
        
        # Sort by cloud cover and date
        results_sorted = sorted(results, key=lambda x: (x['properties']['cloud_cover'], x['properties']['acquired']))
        
        # Display top results
        logger.info(f"\nTop 10 images by quality:")
        for i, item in enumerate(results_sorted[:10]):
            props = item['properties']
            logger.info(f"  {i+1}. ID: {item['id']}")
            logger.info(f"     Date: {props['acquired'][:10]}")
            logger.info(f"     Cloud: {props['cloud_cover']*100:.1f}%")
            logger.info(f"     Resolution: {props.get('gsd', 'N/A')}m")
        
        return results_sorted
    
    def download_best_image(self, max_cloud_cover=10):
        """
        Search and download single best image (simplified version)
        
        Args:
            max_cloud_cover: Maximum cloud cover percentage
        """
        # Search
        items = self.search_images(max_cloud_cover=max_cloud_cover, limit=50)
        
        if not items:
            logger.warning("No suitable images found")
            logger.info("\nTips:")
            logger.info("  - Try increasing --max-cloud-cover (e.g., 20)")
            logger.info("  - Expand date range")
            logger.info("  - Check if AOI is over water/ocean")
            return
        
        # Get best item info
        best_item = items[0]
        
        logger.info("\n" + "="*70)
        logger.info("BEST IMAGE FOUND")
        logger.info("="*70)
        logger.info(f"ID: {best_item['id']}")
        logger.info(f"Date: {best_item['properties']['acquired'][:10]}")
        logger.info(f"Cloud cover: {best_item['properties']['cloud_cover']*100:.1f}%")
        logger.info(f"Resolution: {best_item['properties'].get('gsd', 'N/A')}m")
        logger.info(f"Instrument: {best_item['properties'].get('instrument', 'N/A')}")
        
        logger.info("\n" + "="*70)
        logger.info("PLANET IMAGERY DOWNLOAD INFO")
        logger.info("="*70)
        logger.info("\nPlanet imagery download requires Orders API v2.")
        logger.info("To download this image:")
        logger.info(f"\n1. Item ID: {best_item['id']}")
        logger.info(f"2. Item Type: PSScene")
        logger.info(f"3. Product Bundle: analytic_sr_udm2")
        logger.info(f"4. AOI Bounds: {self.aoi_bbox}")
        logger.info("\nYou can:")
        logger.info("  a) Use Planet Explorer: https://www.planet.com/explorer/")
        logger.info("  b) Use Planet CLI: planet orders create")
        logger.info("  c) Use Orders API directly")
        
        # Save search results
        # Save search results
        search_results = {
            'best_item': {
                'item_id': best_item['id'],
                'acquired': best_item['properties']['acquired'],
                'cloud_cover': best_item['properties']['cloud_cover'],
                'gsd_m': best_item['properties'].get('gsd'),
                'instrument': best_item['properties'].get('instrument'),
                'satellite_id': best_item['properties'].get('satellite_id')
            },
            'aoi_bbox': self.aoi_bbox,
            'search_date': datetime.now().isoformat(),
            'total_results': len(items),
            'all_items': [
                {
                    'id': item['id'],
                    'acquired': item['properties']['acquired'][:10],
                    'cloud_cover': item['properties']['cloud_cover']
                }
                for item in items[:10]  # Save top 10
            ]
        }
        
        metadata_path = self.output_dir / 'planet_search_results.json'
        with open(metadata_path, 'w') as f:
            json.dump(search_results, f, indent=2)
        
        logger.info(f"\n✓ Search results saved to {metadata_path}")
        logger.info("\nNote: Planet imagery is optional - only needed for tree detection (Stage 6)")
        logger.info("Sentinel-2 (10m) and Sentinel-1 (10m) from GEE are sufficient for carbon estimation")


def extract_bbox_from_raster(raster_path: str) -> tuple:
    """Extract WGS84 bbox from raster file"""
    with rasterio.open(raster_path) as src:
        bounds = src.bounds
        native_crs = src.crs
        
        if native_crs.to_epsg() != 4326:
            wgs84_bounds = transform_bounds(
                native_crs,
                'EPSG:4326',
                bounds.left,
                bounds.bottom,
                bounds.right,
                bounds.top
            )
            return wgs84_bounds
        else:
            return (bounds.left, bounds.bottom, bounds.right, bounds.top)


def main_sync(args):
    """Synchronous main function"""
    # Get bbox
    if args.aoi:
        logger.info(f"Extracting AOI from raster: {args.aoi}")
        bbox = extract_bbox_from_raster(args.aoi)
        logger.info(f"Extracted bbox: {bbox}")
    elif args.bbox:
        bbox = tuple(args.bbox)
    else:
        logger.error("Must provide either --aoi or --bbox")
        sys.exit(1)
    
    # Create downloader
    downloader = PlanetDataDownloader(
        aoi_bbox=bbox,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=Path(args.output)
    )
    
    # Search and display results
    downloader.download_best_image(max_cloud_cover=args.max_cloud_cover)
    
    logger.info("\n✓ Planet search completed")


def main():
    parser = argparse.ArgumentParser(
        description="Automatically download Planet Labs imagery"
    )
    parser.add_argument(
        '--aoi',
        type=str,
        help='Path to AOI raster file'
    )
    parser.add_argument(
        '--bbox',
        nargs=4,
        type=float,
        metavar=('WEST', 'SOUTH', 'EAST', 'NORTH'),
        help='Bounding box coordinates in WGS84'
    )
    parser.add_argument(
        '--start-date',
        type=str,
        default='2023-01-01',
        help='Start date (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--end-date',
        type=str,
        default='2023-12-31',
        help='End date (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--max-cloud-cover',
        type=int,
        default=10,
        help='Maximum cloud cover percentage (default: 10)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='data/raw',
        help='Output directory'
    )
    
    args = parser.parse_args()
    
    # Run synchronous main
    main_sync(args)


if __name__ == "__main__":
    main()
