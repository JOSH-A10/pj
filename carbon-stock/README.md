# Carbon Stock Estimation System

## 🌍 Overview

This project estimates forest carbon stock for research purposes and does not generate carbon credits.

This project estimates forest carbon stock using freely available remote sensing datasets. It does not generate carbon credits and is not intended for registry submission.

A **scientifically rigorous, reproducible, and auditable** system for estimating carbon stock (biomass and carbon density) from satellite imagery. This system integrates multi-sensor Earth observation data following international standards (IPCC, FAO, CEOS, ESA).

### Key Principle: **NO MAGIC NUMBERS**

Every parameter, scale factor, unit conversion, and empirical constant is:
- ✅ **Explicitly documented** with scientific source
- ✅ **Traceable** to peer-reviewed literature or international standards
- ✅ **Validated** against physical constraints
- ✅ **Unit-aware** with automatic conversion tracking

---

## 📊 System Architecture

### Multi-Stage Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 1: Sentinel-2 (Optical, 10m)                            │
│  ├─ L2A Surface Reflectance                                    │
│  ├─ SCL Cloud Masking                                          │
│  ├─ Vegetation Indices (NDVI, EVI, NDRE, GNDVI, SAVI, MSI,    │
│  │   NDWI)                                                     │
│  └─ Temporal Composites (Monthly Median)                       │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 2: Sentinel-1 (SAR, 10m)                                │
│  ├─ VV/VH Polarizations                                        │
│  ├─ Radiometric Terrain Correction (RTC)                       │
│  ├─ Incidence Angle Normalization                              │
│  ├─ Texture Metrics (GLCM: Entropy, Contrast, Variance)       │
│  └─ Backscatter Ratios & RVI                                   │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 3: GEDI LiDAR (Sparse, 25m footprints)                  │
│  ├─ L2A Canopy Height (RH98)                                   │
│  ├─ Quality Filtering (sensitivity > 0.98)                     │
│  └─ Reference Data for ML Training                             │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 4: Canopy Height Mapping (ML)                           │
│  ├─ U-Net / FCN Model                                          │
│  ├─ Input: S1 + S2 Features                                    │
│  ├─ Target: GEDI RH98                                          │
│  └─ Output: Wall-to-wall Height Map (10m)                      │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 5: Biomass & Carbon Stock                               │
│  ├─ Allometric Equations (Asner 2014, Chave 2014)             │
│  ├─ AGB (Mg/ha) = 0.54 × H^2.43                               │
│  ├─ Carbon (Mg C/ha) = AGB × 0.47                             │
│  └─ CO2e (Mg CO2e/ha) = Carbon × 3.6642                        │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 6: PlanetScope Tree Detection (3m)                      │
│  ├─ High-Resolution Canopy Segmentation                        │
│  ├─ Instance Separation (Watershed, Distance Transform)        │
│  ├─ Tree Count with Uncertainty                                │
│  └─ Cluster-Based Estimates (if individual trees unresolvable) │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 7: Cross-Sensor Consistency Check                       │
│  ├─ S2 Spectral ∩ S1 Structure ∩ Planet Canopy                │
│  ├─ Flag Disagreements                                         │
│  └─ Final Carbon Stock with Uncertainty Bounds                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📐 Scientific Standards

### Physical Constants

All values from **IPCC Guidelines for National Greenhouse Gas Inventories (2006, 2019 Refinement)**:

| Constant | Value | Source | Physical Meaning |
|----------|-------|--------|------------------|
| **C to CO₂ ratio** | 3.6642 | Molecular weights: CO₂ (44.01) / C (12.01) | Exact chemistry |
| **Carbon fraction** | 0.47 | IPCC 2006, Vol. 4, Table 4.3 | Default for tropical/subtropical forests |
| **Root-to-shoot ratio** | 0.24 | IPCC 2006, Vol. 4, Table 4.4 | Tropical forests (AGB > 125 Mg/ha) |

### Allometric Equations

#### Asner & Mascaro (2014) - **PRIMARY METHOD**
```
AGB (Mg/ha) = 0.54 × H^2.43
```
- **Source**: Asner & Mascaro, 2014, *Carbon Balance and Management*, DOI: [10.1186/1750-0680-9-3](https://doi.org/10.1186/1750-0680-9-3)
- **Applicability**: Pantropical forests
- **Height range**: 5-50 m
- **Uncertainty**: ±20%
- **Physical basis**: Power law reflects metabolic scaling theory

#### Chave et al. (2014) - **Alternative (requires DBH)**
```
AGB (kg/tree) = 0.0673 × (ρ × D² × H)^0.976
```
- **Source**: Chave et al., 2014, *Global Change Biology*, DOI: [10.1111/gcb.12629](https://doi.org/10.1111/gcb.12629)
- **Variables**: ρ = wood density (g/cm³), D = DBH (cm), H = height (m)
- **Uncertainty**: ±25%

### Vegetation Indices

All formulas explicitly documented in `src/indices.py`:

| Index | Formula | Source | Application |
|-------|---------|--------|-------------|
| **NDVI** | (NIR - Red) / (NIR + Red) | Rouse et al., 1974 | General vegetation vigor |
| **EVI** | 2.5 × ((NIR - Red) / (NIR + 6×Red - 7.5×Blue + 1)) | Huete et al., 2002 | High biomass, reduced saturation |
| **NDRE** | (NIR - RedEdge) / (NIR + RedEdge) | Gitelson & Merzlyak, 1994 | Chlorophyll, canopy closure |
| **SAVI** | ((NIR - Red) / (NIR + Red + 0.5)) × 1.5 | Huete, 1988 | Soil-adjusted |
| **NDWI** | (NIR - SWIR) / (NIR + SWIR) | Gao, 1996 | Water content |

---

## 🚀 Quick Start

### 1. Clone Repository
```bash
git clone <repository-url>
cd carbon-estimator
```

### 2. Install Dependencies
```bash
# Using conda (recommended)
conda create -n carbon python=3.10
conda activate carbon
conda install -c conda-forge gdal rasterio geopandas

# Install Python packages
pip install -r requirements.txt
```

### 3. Run Google Earth Engine Scripts

Open Google Earth Engine Code Editor and run scripts in order:

```javascript
// 1. Sentinel-2 extraction
gee/01_extract_sentinel2.js

// 2. Sentinel-1 extraction
gee/02_extract_sentinel1.js

// 3. GEDI canopy height
gee/03_extract_gedi.js
```

**Configure AOI in each script:**
```javascript
aoi: ee.Geometry.Rectangle([lon_min, lat_min, lon_max, lat_max])
```

### 4. Download Exported Data

From Google Drive folder `carbon_estimator_sentinel2`, `carbon_estimator_sentinel1`, `carbon_estimator_gedi`, download:
- `sentinel2_median_*.tif`
- `sentinel1_median_*.tif`
- `gedi_footprints_*.csv`

Place in `data/raw/`

### 5. Run Pipeline

```bash
python scripts/run_pipeline.py --config config.yaml
```

---

## 📁 Project Structure

```
carbon-estimator/
├── gee/                          # Google Earth Engine scripts
│   ├── 01_extract_sentinel2.js
│   ├── 02_extract_sentinel1.js
│   └── 03_extract_gedi.js
│
├── src/                          # Python source code
│   ├── config.py                 # ALL constants and standards
│   ├── io.py                     # Raster I/O with validation
│   ├── indices.py                # Vegetation indices (explicit formulas)
│   ├── biomass.py                # Allometric equations
│   ├── carbon.py                 # Carbon & CO2e conversion
│   ├── trees.py                  # Tree detection (PlanetScope)
│   └── timeseries.py             # Temporal analysis
│
├── scripts/                      # Execution scripts
│   └── run_pipeline.py           # Main pipeline orchestration
│
├── notebooks/                    # Validation & analysis
│   └── 01_explore.ipynb          # Data exploration & validation
│
├── data/
│   ├── raw/                      # GEE exports
│   ├── processed/                # Intermediate products
│   └── tables/                   # Tabular data (GEDI, validation)
│
├── outputs/
│   ├── maps/                     # Final carbon stock maps
│   └── reports/                  # Statistics & uncertainty
│
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

---

## 🔬 Validation

### Numerical Verification

All calculations are validated against:
1. **Unit consistency checks** (automatic in `io.py`)
2. **Physical range constraints** (e.g., NDVI ∈ [-1, 1])
3. **Cross-sensor agreement** (S2 ∩ S1 ∩ Planet)
4. **GEDI reference heights** (R² > 0.8 expected for ML model)

### Uncertainty Propagation

Two methods implemented:
1. **Error propagation**: √(σ_biomass² + σ_carbon_frac²)
2. **Monte Carlo**: 1000 iterations with parameter sampling

### Expected Accuracies

| Parameter | Expected Accuracy | Method |
|-----------|-------------------|--------|
| Canopy Height | RMSE < 3 m | Validation against GEDI |
| AGB | ±20-25% | Allometric equation uncertainty |
| Carbon Stock | ±25-30% | Propagated from AGB + carbon fraction |
| Tree Count | ±15-40% | Depends on crown size vs. resolution |

---

## 📊 Output Products

### 1. Carbon Stock Map
- **File**: `outputs/maps/carbon_stock.tif`
- **Bands**:
  - Band 1: Carbon (Mg C/ha)
  - Band 2: CO₂e (Mg CO₂e/ha)
  - Band 3: Uncertainty (Mg C/ha)
- **Resolution**: 10 m
- **CRS**: EPSG:4326 (or specified)

### 2. Statistics Report
- **File**: `outputs/reports/carbon_statistics.json`
- **Contents**:
  - Total carbon stock (Mg C)
  - Mean, median, std dev
  - Uncertainty bounds (5th, 95th percentile)
  - Area covered (ha)

### 3. Validation Metrics
- **File**: `outputs/reports/validation.json`
- **Contents**:
  - Height prediction R², RMSE
  - Cross-sensor agreement metrics
  - Quality flags per pixel

---

## 🛠️ Configuration

### Edit `src/config.py` for:

```python
# Temporal range
PIPELINE_CONFIG.start_date = "2023-01-01"
PIPELINE_CONFIG.end_date = "2024-12-31"

# Allometric equation selection
ALLOMETRIC_EQUATIONS['default'] = ALLOMETRIC_ASNER_2014_HEIGHT

# Carbon fraction (adjust for biome)
PHYSICAL_CONSTANTS.DEFAULT_CARBON_FRACTION = 0.47  # Tropical
```

### Command-line options

```bash
python scripts/run_pipeline.py \
  --aoi data/aoi.geojson \
  --start-date 2023-01-01 \
  --end-date 2023-12-31 \
  --biome tropical \
  --output outputs/
```

---

## 🔍 Known Limitations

### Physical Constraints

1. **GEDI coverage is sparse** (~25 m footprints, ~4% area coverage)
   - Solution: ML model interpolates between footprints
   - Validation: Cross-validation with withheld GEDI shots

2. **Tree counting requires 3 m resolution**
   - Individual trees detectable only if crowns > 3 m diameter
   - For smaller trees: cluster-based estimates with uncertainty

3. **Allometric equations have regional variation**
   - Asner 2014: Pantropical average (±20% uncertainty)
   - For local accuracy: calibrate with field data

4. **No seasonal phenology separation**
   - Monthly composites capture intra-annual variation
   - For deciduous forests: specify leaf-on period

### Technical Limitations

1. **Cloud cover**: Sentinel-2 limited in persistently cloudy regions
2. **Steep terrain**: SAR layover/shadow in mountains
3. **Young plantations**: Allometric equations calibrated for natural forests

---

## 📚 References

### International Standards
- IPCC, 2006. *Guidelines for National Greenhouse Gas Inventories*, Volume 4
- IPCC, 2019. *Refinement to the 2006 IPCC Guidelines*
- FAO, 2020. *Global Forest Resources Assessment*
- CEOS, 2021. *Analysis Ready Data for Land (CARD4L)*

### Allometric Equations
- Asner, G.P., Mascaro, J., 2014. *Carbon Balance and Management* 9:3
- Chave, J., et al., 2014. *Global Change Biology* 20(10):3177-3190

### Remote Sensing Methods
- Dubayah, R., et al., 2020. *Science of Remote Sensing* 2:100002 (GEDI)
- Kellndorfer, J., et al., 2019. *Remote Sensing* 11(18):2154 (SAR biomass)
- Potapov, P., et al., 2021. *Nature Geoscience* 14:221-227 (Global forest height)

### Vegetation Indices
- Rouse, J.W., et al., 1974. *NASA SP-351* (NDVI)
- Huete, A.R., et al., 2002. *Remote Sensing of Environment* 83:195-213 (EVI)
- Gao, B.C., 1996. *Remote Sensing of Environment* 58:257-266 (NDWI)

---

## 🤝 Contributing

### Before submitting changes:

1. **Document all constants** with sources
2. **Add unit tests** for new functions
3. **Validate against physical ranges**
4. **Update this README** with any new methods

### Code Style
```bash
# Format code
black src/ scripts/

# Check types
mypy src/

# Run tests
pytest tests/
```

---

## 📧 Contact & Support

For scientific questions about methods, consult the original papers cited in `src/config.py`.

For technical issues, open a GitHub issue with:
- Input data characteristics
- Error messages / logs
- Configuration used

---

## ⚖️ License

MIT License - See LICENSE file for details.

Data sources have their own licenses:
- Sentinel-1/2: Copernicus Open Access Hub (free and open)
- GEDI: NASA (public domain)
- PlanetScope: Commercial license required

---

## 🎯 Roadmap

### Phase 1: Core Pipeline (Current)
- ✅ GEE data extraction
- ✅ Python processing modules
- ✅ Validation framework
- ⏳ ML height model training

### Phase 2: Advanced Features
- [ ] Temporal change detection
- [ ] Uncertainty propagation (Monte Carlo)
- [ ] Multi-region batch processing
- [ ] Automated report generation

### Phase 3: Frontend (Future)
- [ ] Web-based visualization
- [ ] Interactive map interface
- [ ] API for carbon queries
- [ ] Automated monitoring dashboard

---

**⚠️ CRITICAL REMINDER**: If a value cannot be physically justified or traced to a standard, **it must not be used**.
