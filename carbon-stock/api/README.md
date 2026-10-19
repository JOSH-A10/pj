# Plantation Carbon Estimation API

FastAPI backend with workflow orchestration for plantation carbon monitoring.

## Features

- **Data Upload**: Sentinel-1 and Sentinel-2 file uploads with validation
- **Workflow Management**: Create and track multi-stage pipeline execution
- **Sequential Processing**: Proper delays between stages for file system stability
- **Status Tracking**: Real-time progress monitoring for each pipeline stage
- **Results Retrieval**: Download comprehensive reports and carbon estimates

## API Endpoints

### Health & Info
- `GET /` - API root
- `GET /health` - Health check
- `GET /species` - List available plantation species

### Data Upload
- `POST /upload/sentinel2` - Upload Sentinel-2 composite (.tif)
- `POST /upload/sentinel1` - Upload Sentinel-1 composite (.tif)

### Workflow Management
- `POST /workflow/create` - Create new workflow
- `POST /workflow/{id}/start` - Start workflow execution (background)
- `GET /workflow/{id}/status` - Get workflow status and progress
- `GET /workflow/{id}/results` - Get final results (when completed)
- `GET /workflows` - List all workflows

### Results Download
- `GET /workflow/{id}/download/report` - Download comprehensive report
- `GET /workflow/{id}/download/carbon` - Download carbon estimation results

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Or install in existing environment
pip install fastapi uvicorn[standard] python-multipart pydantic rasterio numpy
```

## Running the Server

```bash
# Development mode (auto-reload)
python api/main.py

# Or with uvicorn directly
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Server will start at: http://localhost:8000

API documentation: http://localhost:8000/docs

## Workflow Pipeline Stages

1. **Data Validation** (2s delay)
   - Verify files exist
   - Validate raster format
   - Check band structure

2. **Preprocessing** (3s delay)
   - Data already preprocessed
   - Future: cloud masking, resampling

3. **Forest Analysis** (2s delay)
   - Runs `honest_forest_analysis_pipeline.py`
   - Outputs: forest mask, canopy cover, density, biomass proxy

4. **Carbon Estimation** (2s delay)
   - Runs `plantation_carbon_estimation.py`
   - Estimates height from SAR
   - Applies species-specific allometry
   - Calculates carbon stock and CO₂e

5. **Report Generation** (1s delay)
   - Combines all results
   - Generates comprehensive JSON report
   - Includes disclaimers and limitations

**Total execution time**: ~20-40 seconds (depending on data size)

## Usage Example

### 1. Upload Data Files

```bash
curl -X POST "http://localhost:8000/upload/sentinel2" \
  -F "file=@data/processed/sentinel2_composite.tif"

curl -X POST "http://localhost:8000/upload/sentinel1" \
  -F "file=@data/processed/sentinel1_composite.tif"
```

### 2. Create Workflow

```bash
curl -X POST "http://localhost:8000/workflow/create" \
  -H "Content-Type: application/json" \
  -d '{
    "species": "teak",
    "area_ha": 3.0,
    "tree_count": 300,
    "s2_filename": "s2_20260130_120000_sentinel2_composite.tif",
    "s1_filename": "s1_20260130_120000_sentinel1_composite.tif"
  }'
```

Response:
```json
{
  "success": true,
  "workflow_id": "wf_20260130_120000_abc12345",
  "status": "pending",
  "created_at": "2026-01-30T12:00:00.000000",
  "message": "Workflow created. Use /workflow/{id}/start to begin execution"
}
```

### 3. Start Workflow

```bash
curl -X POST "http://localhost:8000/workflow/wf_20260130_120000_abc12345/start"
```

### 4. Check Status

```bash
curl "http://localhost:8000/workflow/wf_20260130_120000_abc12345/status"
```

Response:
```json
{
  "workflow_id": "wf_20260130_120000_abc12345",
  "status": "running",
  "current_stage": "carbon_estimation",
  "created_at": "2026-01-30T12:00:00.000000",
  "updated_at": "2026-01-30T12:00:25.000000",
  "total_duration": null,
  "stages": {
    "data_validation": {
      "status": "completed",
      "started_at": "2026-01-30T12:00:05.000000",
      "completed_at": "2026-01-30T12:00:07.000000",
      "duration_seconds": 2.1,
      "output_files": ["..."],
      "error_message": null
    },
    "carbon_estimation": {
      "status": "running",
      "started_at": "2026-01-30T12:00:25.000000",
      "completed_at": null,
      "duration_seconds": null,
      "output_files": [],
      "error_message": null
    }
  }
}
```

### 5. Get Results (When Completed)

```bash
curl "http://localhost:8000/workflow/wf_20260130_120000_abc12345/results"
```

### 6. Download Report

```bash
curl -O "http://localhost:8000/workflow/wf_20260130_120000_abc12345/download/report"
```

## Available Species

| Key | Common Name | Scientific Name | Wood Density (g/cm³) |
|-----|-------------|-----------------|----------------------|
| teak | Teak | *Tectona grandis* | 0.55 |
| eucalyptus | Eucalyptus | *Eucalyptus spp.* | 0.65 |
| mahogany | Mahogany | *Swietenia macrophylla* | 0.55 |
| rubber | Rubber | *Hevea brasiliensis* | 0.56 |
| pine | Pine | *Pinus spp.* | 0.48 |
| acacia | Acacia | *Acacia mangium* | 0.52 |

## Important Limitations

All carbon estimates include these disclaimers:

- **±68-70% uncertainty** - NOT suitable for carbon stock monetization
- Height from SAR (no LiDAR validation)
- Generic allometry (not site-calibrated)
- Wood density from literature (not field-measured)
- Does NOT include: soil carbon, roots, understory
- **Monitoring & analysis only** - NOT for registry submission

## Project Structure

```
api/
├── main.py                 # FastAPI application
├── workflow_manager.py     # Pipeline orchestration
├── requirements.txt        # API dependencies
└── README.md              # This file

data/
└── uploads/               # Uploaded files storage

outputs/
├── honest_analysis/       # Forest analysis results
│   └── {workflow_id}/
├── carbon_estimation/     # Carbon estimation results
│   └── {workflow_id}/
├── reports/              # Comprehensive reports
│   └── {workflow_id}/
└── workflow_states.json  # Workflow state persistence
```

## Error Handling

All endpoints return standardized error responses:

```json
{
  "success": false,
  "error": "Error message",
  "status_code": 400
}
```

Common status codes:
- `200` - Success
- `400` - Bad request (validation error)
- `404` - Resource not found
- `500` - Internal server error

## Development

```bash
# Run with auto-reload
uvicorn api.main:app --reload

# Run with specific host/port
uvicorn api.main:app --host 0.0.0.0 --port 8080

# Enable debug logging
uvicorn api.main:app --reload --log-level debug
```

## Testing

```bash
# Test health endpoint
curl http://localhost:8000/health

# List species
curl http://localhost:8000/species

# List workflows
curl http://localhost:8000/workflows
```

## Future Enhancements

- [ ] WebSocket support for real-time progress updates
- [ ] Batch workflow processing
- [ ] Visualization endpoints (PNG/JPEG map outputs)
- [ ] Time series analysis for multiple dates
- [ ] User authentication and authorization
- [ ] Database integration (PostgreSQL + PostGIS)
- [ ] Celery task queue for long-running jobs
- [ ] Docker containerization
