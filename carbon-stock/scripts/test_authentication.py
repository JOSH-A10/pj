"""
Quick Authentication Test Script
Tests both GEE and Planet authentication
"""

def test_gee():
    """Test Google Earth Engine authentication"""
    print("\n🔍 Testing Google Earth Engine...")
    try:
        import ee
        
        # Try to initialize with project from environment
        import os
        project = os.environ.get('EARTHENGINE_PROJECT')
        
        if project:
            ee.Initialize(project=project)
            print(f"  ✓ Initialized with project: {project}")
        else:
            # Try without project (may work if already authenticated)
            try:
                ee.Initialize()
                print("  ✓ Initialized (no project specified)")
            except Exception as e:
                print(f"  ✗ Initialization failed: {e}")
                print("\n  💡 Solution:")
                print("     Set EARTHENGINE_PROJECT environment variable:")
                print("     $env:EARTHENGINE_PROJECT='your-project-id'")
                return False
        
        # Test query
        image = ee.Image('COPERNICUS/S2_SR_HARMONIZED/20230101T103319_20230101T103321_T32TQQ')
        info = image.getInfo()
        print("  ✓ Successfully queried Sentinel-2 image")
        print("  ✓ GEE authentication: OK")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        print("\n  💡 Solution:")
        print("     1. Run: earthengine authenticate")
        print("     2. Set: $env:EARTHENGINE_PROJECT='your-project-id'")
        return False


def test_planet():
    """Test Planet Labs authentication"""
    print("\n🔍 Testing Planet Labs...")
    try:
        from planet import Auth, Session
        
        # Try to get auth from environment
        auth = Auth.from_env()
        
        if not auth:
            print("  ✗ No API key found in environment")
            print("\n  💡 Solution:")
            print("     Set PL_API_KEY environment variable:")
            print("     $env:PL_API_KEY='your-api-key'")
            return False
        
        print("  ✓ API key found in environment")
        
        # Create session
        session = Session(auth=auth)
        print("  ✓ Session created successfully")
        print("  ✓ Planet authentication: OK")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {e}")
        print("\n  💡 Solution:")
        print("     1. Get API key from: https://www.planet.com/account/")
        print("     2. Set: $env:PL_API_KEY='your-api-key'")
        return False


def main():
    print("=" * 70)
    print("AUTHENTICATION TEST")
    print("=" * 70)
    
    gee_ok = test_gee()
    planet_ok = test_planet()
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"\n  Google Earth Engine: {'✓ OK' if gee_ok else '✗ Failed'}")
    print(f"  Planet Labs:         {'✓ OK' if planet_ok else '✗ Failed'}")
    
    if gee_ok and planet_ok:
        print("\n  🎉 All authentication successful!")
        print("  You can now run the carbon estimation pipeline.")
    elif gee_ok:
        print("\n  ⚠️  GEE OK, but Planet needs setup")
        print("  You can still run Sentinel-1/2 and GEDI extraction (Stages 1-4)")
    elif planet_ok:
        print("\n  ⚠️  Planet OK, but GEE needs setup")
        print("  You need GEE for Sentinel-1/2 and GEDI data (Stages 1-3)")
    else:
        print("\n  ❌ Both services need authentication")
        print("  See AUTHENTICATION.md for setup instructions")
    
    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
