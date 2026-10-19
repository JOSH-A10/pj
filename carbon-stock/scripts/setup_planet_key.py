"""
Setup Planet Labs API Key

Helper script to configure your Planet Labs API key in .env file.
Run this once to store your API key permanently.

Usage:
    python scripts/setup_planet_key.py
    python scripts/setup_planet_key.py YOUR_API_KEY_HERE
"""

import sys
from pathlib import Path

def setup_api_key(api_key=None):
    """Setup Planet API key in .env file"""
    
    env_file = Path(__file__).parent.parent / '.env'
    
    # If no key provided, prompt user
    if not api_key:
        print("\n" + "="*70)
        print("PLANET LABS API KEY SETUP")
        print("="*70)
        print("\nGet your API key at: https://www.planet.com/account/#/user-settings")
        print("(You need a Planet Labs account)")
        api_key = input("\nEnter your Planet API key: ").strip()
    
    if not api_key or api_key == 'your_planet_api_key_here':
        print("\n❌ Invalid API key provided")
        sys.exit(1)
    
    # Read existing .env or create template
    if env_file.exists():
        with open(env_file, 'r') as f:
            lines = f.readlines()
    else:
        lines = [
            "# Carbon Estimator Configuration\n",
            "# DO NOT COMMIT THIS FILE TO GIT (already in .gitignore)\n",
            "\n",
            "# Planet Labs API Key\n",
            "PL_API_KEY=your_planet_api_key_here\n",
            "\n",
            "# Google Earth Engine Project\n",
            "GEE_PROJECT=heroic-bliss-472519-f7\n"
        ]
    
    # Update PL_API_KEY line
    updated = False
    for i, line in enumerate(lines):
        if line.startswith('PL_API_KEY='):
            lines[i] = f'PL_API_KEY={api_key}\n'
            updated = True
            break
    
    if not updated:
        lines.append(f'\nPL_API_KEY={api_key}\n')
    
    # Write back
    with open(env_file, 'w') as f:
        f.writelines(lines)
    
    print(f"\n✓ API key saved to {env_file}")
    print("\n✓ You can now run Planet data downloads without setting environment variables:")
    print("  python scripts/download_planet_data.py --aoi data/raw/FUSED_AOI_2023_2024.tif")
    print("\n" + "="*70)


if __name__ == '__main__':
    if len(sys.argv) > 1:
        setup_api_key(sys.argv[1])
    else:
        setup_api_key()
