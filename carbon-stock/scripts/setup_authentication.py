"""
Authentication Setup Script
Authenticates Google Earth Engine and Planet Labs API
"""

import os
import sys

def authenticate_gee():
    """
    Authenticate Google Earth Engine.
    This will open a browser window for authentication.
    """
    print("=" * 80)
    print("GOOGLE EARTH ENGINE AUTHENTICATION")
    print("=" * 80)
    
    try:
        import ee
        print("\n✓ Earth Engine API loaded")
        
        # Try to initialize (check if already authenticated)
        try:
            ee.Initialize()
            print("✓ Already authenticated with Google Earth Engine")
            
            # Test with a simple query
            test = ee.Image('COPERNICUS/S2_SR/20230101T103319_20230101T103321_T32TQQ')
            info = test.getInfo()
            print("✓ GEE authentication verified - test query successful")
            return True
        except Exception:
            print("\n→ Not yet authenticated. Starting authentication process...")
            print("\nThis will open a web browser. Please:")
            print("  1. Sign in with your Google account")
            print("  2. Authorize Earth Engine access")
            print("  3. Copy the authorization code")
            print("  4. Paste it back in the terminal\n")
            
            # Start authentication
            ee.Authenticate()
            
            # Try to initialize again
            ee.Initialize()
            print("\n✓ Google Earth Engine authenticated successfully!")
            return True
            
    except Exception as e:
        print(f"\n✗ Error authenticating GEE: {e}")
        print("\nManual authentication:")
        print("  1. Run: earthengine authenticate")
        print("  2. Or run Python: import ee; ee.Authenticate()")
        return False


def authenticate_planet():
    """
    Set up Planet Labs API authentication.
    Requires API key from Planet account.
    """
    print("\n" + "=" * 80)
    print("PLANET LABS AUTHENTICATION")
    print("=" * 80)
    
    try:
        from planet import Auth
        
        # Check if already authenticated
        auth = Auth.from_env()
        if auth:
            print("\n✓ Planet API key found in environment")
            
            # Test authentication
            try:
                from planet import Session
                client = Session(auth=auth)
                print("✓ Planet authentication verified")
                return True
            except Exception as e:
                print(f"✗ Planet authentication test failed: {e}")
        else:
            print("\n→ No Planet API key found in environment")
            
        print("\n" + "-" * 80)
        print("To authenticate Planet Labs:")
        print("-" * 80)
        print("\n1. Get your API key:")
        print("   - Go to: https://www.planet.com/account/")
        print("   - Sign in to your Planet account")
        print("   - Navigate to 'My Settings' > 'API Keys'")
        print("   - Copy your API key")
        
        print("\n2. Set the API key as environment variable:")
        print("\n   On Windows (PowerShell):")
        print("   $env:PL_API_KEY='your-api-key-here'")
        
        print("\n   Or permanently (System Environment Variables):")
        print("   - Search 'Environment Variables' in Windows")
        print("   - Add new variable: PL_API_KEY = your-api-key-here")
        
        print("\n3. Alternatively, create .env file in project root:")
        print("   PL_API_KEY=your-api-key-here")
        
        print("\n4. Test with:")
        print("   planet auth init --email your@email.com --password your-password")
        print("   Or: planet auth init --key your-api-key")
        
        print("\n" + "-" * 80)
        
        # Ask if user wants to set API key now
        print("\n→ Would you like to set your Planet API key now? (y/n)")
        response = input().strip().lower()
        
        if response == 'y':
            print("\nPaste your Planet API key:")
            api_key = input().strip()
            
            if api_key:
                os.environ['PL_API_KEY'] = api_key
                print("\n✓ API key set for this session")
                print("  To make it permanent, add to System Environment Variables")
                
                # Test authentication
                try:
                    from planet import Session
                    auth = Auth.from_key(api_key)
                    client = Session(auth=auth)
                    print("✓ Planet authentication successful!")
                    return True
                except Exception as e:
                    print(f"✗ Authentication test failed: {e}")
                    return False
            else:
                print("✗ No API key provided")
                return False
        else:
            print("\n→ Skipping Planet authentication for now")
            print("  You can set it up later using the instructions above")
            return False
            
    except ImportError:
        print("\n✗ Planet SDK not installed")
        print("  Install with: pip install planet")
        return False
    except Exception as e:
        print(f"\n✗ Error with Planet authentication: {e}")
        return False


def verify_authentication():
    """
    Verify both GEE and Planet authentication with test queries
    """
    print("\n" + "=" * 80)
    print("VERIFICATION")
    print("=" * 80)
    
    results = {
        'gee': False,
        'planet': False
    }
    
    # Test GEE
    try:
        import ee
        ee.Initialize()
        
        # Simple test query
        image = ee.Image('COPERNICUS/S2_SR/20230101T103319_20230101T103321_T32TQQ')
        info = image.getInfo()
        
        print("\n✓ GEE: Successfully queried Sentinel-2 image")
        results['gee'] = True
    except Exception as e:
        print(f"\n✗ GEE: Verification failed - {e}")
    
    # Test Planet
    try:
        from planet import Auth, Session
        
        auth = Auth.from_env()
        if auth:
            client = Session(auth=auth)
            print("✓ Planet: Authentication verified")
            results['planet'] = True
        else:
            print("✗ Planet: No API key found")
    except Exception as e:
        print(f"✗ Planet: Verification failed - {e}")
    
    return results


def main():
    """Main authentication workflow"""
    print("\n" + "=" * 80)
    print("CARBON ESTIMATOR - AUTHENTICATION SETUP")
    print("=" * 80)
    print("\nThis script will help you authenticate:")
    print("  1. Google Earth Engine (GEE) - for Sentinel-1/2 and GEDI data")
    print("  2. Planet Labs API - for PlanetScope 3m imagery")
    print("\n" + "=" * 80 + "\n")
    
    # Authenticate GEE
    gee_success = authenticate_gee()
    
    # Authenticate Planet
    planet_success = authenticate_planet()
    
    # Verify
    print("\n" + "=" * 80)
    results = verify_authentication()
    
    # Summary
    print("\n" + "=" * 80)
    print("AUTHENTICATION SUMMARY")
    print("=" * 80)
    print(f"\n  Google Earth Engine: {'✓ Authenticated' if results['gee'] else '✗ Not authenticated'}")
    print(f"  Planet Labs:         {'✓ Authenticated' if results['planet'] else '✗ Not authenticated'}")
    
    if results['gee'] and results['planet']:
        print("\n✓ All authentication complete! You're ready to run the pipeline.")
    elif results['gee']:
        print("\n⚠ GEE authenticated, but Planet needs setup.")
        print("  You can still run Sentinel-1/2 and GEDI extraction.")
        print("  Set up Planet later for high-resolution analysis (Stage 6).")
    else:
        print("\n⚠ Please complete authentication to use the system.")
    
    print("\n" + "=" * 80 + "\n")


if __name__ == "__main__":
    main()
