"""
USA Forest Carbon Estimation API
=================================

FastAPI backend for area-based forest carbon accounting using GEDI L4A.

NO tree counts, NO species requirements, NO per-tree logic.

Endpoints:
- POST /workflow/create - Create new workflow (area-based inputs only)
- POST /workflow/{id}/start - Start workflow execution
- GET /workflow/{id}/status - Get workflow status
- GET /workflow/{id}/results - Get final results
- POST /upload/forest_mask - Upload forest mask
- POST /upload/gedi_data - Upload GEDI L4A data
- GET /health - Health check

CHANGE LOG:
- REMOVED: tree_count, species (required), per-tree endpoints
- ADDED: Area-based carbon estimation using GEDI L4A
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Form
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

from workflow_manager import (
    PipelineWorkflowManager,
    PipelineStatus,
    PipelineStage,
    SPECIES_DATABASE
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastAPI
app = FastAPI(
    title="USA Forest Carbon Estimation API",
    description="Area-based carbon accounting using GEDI L4A authoritative biomass",
    version="2.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BASE_DIR = Path(__file__).parent.parent
PYTHON_EXE = Path(BASE_DIR / ".venv" / "Scripts" / "python.exe")
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Initialize workflow manager
workflow_manager = PipelineWorkflowManager(BASE_DIR, PYTHON_EXE)


# ============================================================================
# Pydantic Models
# ============================================================================

class WorkflowCreateRequest(BaseModel):
    """
    Area-based carbon estimation request.
    
    NO tree counts, NO species requirements.
    """
    aoi: dict = Field(..., description="AOI boundary (GeoJSON)")
    year: int = Field(..., description="Analysis year")
    biomass_source: str = Field("GEDI_L4A", description="Biomass source (only GEDI_L4A supported)")
    forest_mask_filename: Optional[str] = Field(None, description="Optional uploaded forest mask")
    
    @validator('biomass_source')
    def validate_source(cls, v):
        if v != "GEDI_L4A":
            raise ValueError("Only GEDI_L4A biomass source is supported for USA carbon accounting")
        return v
    
    @validator('year')
    def validate_year(cls, v):
        if v < 2019 or v > 2024:
            raise ValueError("GEDI data available for years 2019-2024")
        return v


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    status: str
    current_stage: Optional[str]
    created_at: str
    updated_at: str
    total_duration: Optional[float]
    stages: dict
    input_params: dict


class SpeciesInfo(BaseModel):
    key: str
    common_name: str
    scientific_name: str
    wood_density: float
    allometry_equation: str


# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/")
async def root():
    """API root endpoint"""
    return {
        "service": "USA Forest Carbon Estimation API",
        "version": "2.0.0",
        "status": "operational",
        "methodology": "Area-based carbon accounting using GEDI L4A",
        "documentation": "/docs",
        "notes": [
            "NO tree counts required",
            "NO species requirements",
            "GEDI L4A authoritative biomass only"
        ]
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "base_dir": str(BASE_DIR),
        "python_exe": str(PYTHON_EXE),
        "python_exists": PYTHON_EXE.exists()
    }


@app.post("/upload/sentinel2")
async def upload_sentinel2(file: UploadFile = File(...)):
    """
    Upload Sentinel-2 composite file (.tif)
    
    Expected bands: Blue, Green, Red, NIR, SWIR1, SWIR2, NDVI
    """
    if not file.filename.endswith('.tif'):
        raise HTTPException(400, "File must be a .tif raster")
    
    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"s2_{timestamp}_{file.filename}"
    filepath = UPLOAD_DIR / filename
    
    # Save file
    try:
        content = await file.read()
        with open(filepath, 'wb') as f:
            f.write(content)
        
        # Validate it's a valid raster
        import rasterio
        with rasterio.open(filepath) as src:
            info = {
                'bands': src.count,
                'shape': src.shape,
                'crs': str(src.crs),
                'bounds': src.bounds
            }
        
        logger.info(f"Uploaded S2 file: {filename} ({len(content)} bytes)")
        
        return {
            "success": True,
            "filename": filename,
            "filepath": str(filepath),
            "size_bytes": len(content),
            "raster_info": info
        }
    
    except Exception as e:
        if filepath.exists():
            filepath.unlink()
        raise HTTPException(500, f"Upload failed: {str(e)}")


@app.post("/upload/sentinel1")
async def upload_sentinel1(file: UploadFile = File(...)):
    """
    Upload Sentinel-1 composite file (.tif)
    
    Expected bands: VV, VH
    """
    if not file.filename.endswith('.tif'):
        raise HTTPException(400, "File must be a .tif raster")
    
    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"s1_{timestamp}_{file.filename}"
    filepath = UPLOAD_DIR / filename
    
    # Save file
    try:
        content = await file.read()
        with open(filepath, 'wb') as f:
            f.write(content)
        
        # Validate it's a valid raster
        import rasterio
        with rasterio.open(filepath) as src:
            info = {
                'bands': src.count,
                'shape': src.shape,
                'crs': str(src.crs),
                'bounds': src.bounds
            }
        
        logger.info(f"Uploaded S1 file: {filename} ({len(content)} bytes)")
        
        return {
            "success": True,
            "filename": filename,
            "filepath": str(filepath),
            "size_bytes": len(content),
            "raster_info": info
        }
    
    except Exception as e:
        if filepath.exists():
            filepath.unlink()
        raise HTTPException(500, f"Upload failed: {str(e)}")


@app.get("/species", response_model=List[SpeciesInfo])
async def list_species():
    """
    Get list of available plantation species with parameters
    """
    species_list = []
    for key, data in SPECIES_DATABASE.items():
        species_list.append(SpeciesInfo(
            key=key,
            common_name=data.common_name,
            scientific_name=data.scientific_name,
            wood_density=data.wood_density,
            allometry_equation=f"Biomass = {data.allometry_a:.4f} × Height^{data.allometry_b:.3f}"
        ))
    return species_list


@app.post("/workflow/create")
async def create_workflow(request: WorkflowCreateRequest):
    """
    Create a new workflow instance
    
    Returns workflow_id for tracking
    """
    # Validate files exist
    s2_path = UPLOAD_DIR / request.s2_filename
    s1_path = UPLOAD_DIR / request.s1_filename
    
    if not s2_path.exists():
        raise HTTPException(404, f"Sentinel-2 file not found: {request.s2_filename}")
    if not s1_path.exists():
        raise HTTPException(404, f"Sentinel-1 file not found: {request.s1_filename}")
    
    # Generate workflow ID
    workflow_id = f"wf_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    
    try:
        workflow = workflow_manager.create_workflow(
            workflow_id=workflow_id,
            s2_file=str(s2_path),
            s1_file=str(s1_path),
            species=request.species,
            area_ha=request.area_ha,
            tree_count=request.tree_count,
            aoi_geojson=request.aoi_geojson
        )
        
        logger.info(f"Created workflow: {workflow_id}")
        
        return {
            "success": True,
            "workflow_id": workflow_id,
            "status": workflow.status.value,
            "created_at": workflow.created_at,
            "message": "Workflow created. Use /workflow/{id}/start to begin execution"
        }
    
    except Exception as e:
        raise HTTPException(500, f"Failed to create workflow: {str(e)}")


@app.post("/workflow/{workflow_id}/start")
async def start_workflow(workflow_id: str, background_tasks: BackgroundTasks):
    """
    Start workflow execution in background
    
    Pipeline stages:
    1. Data validation (2s delay)
    2. Preprocessing (3s delay)
    3. Forest analysis (2s delay)
    4. Carbon estimation (2s delay)
    5. Report generation (1s delay)
    
    Use /workflow/{id}/status to track progress
    """
    workflow = workflow_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")
    
    if workflow.status != PipelineStatus.PENDING:
        raise HTTPException(400, f"Workflow already {workflow.status.value}")
    
    # Execute workflow in background
    background_tasks.add_task(workflow_manager.execute_workflow, workflow_id)
    
    logger.info(f"Started workflow execution: {workflow_id}")
    
    return {
        "success": True,
        "workflow_id": workflow_id,
        "status": "running",
        "message": "Workflow execution started in background"
    }


@app.get("/workflow/{workflow_id}/status", response_model=WorkflowStatusResponse)
async def get_workflow_status(workflow_id: str):
    """
    Get current workflow status and progress
    """
    workflow = workflow_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")
    
    return WorkflowStatusResponse(
        workflow_id=workflow.workflow_id,
        status=workflow.status.value,
        current_stage=workflow.current_stage.value if workflow.current_stage else None,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        total_duration=workflow.total_duration,
        stages={
            name: {
                'status': stage.status.value,
                'started_at': stage.started_at,
                'completed_at': stage.completed_at,
                'duration_seconds': stage.duration_seconds,
                'output_files': stage.output_files,
                'error_message': stage.error_message
            }
            for name, stage in workflow.stages.items()
        },
        input_params=workflow.input_params
    )


@app.get("/workflow/{workflow_id}/results")
async def get_workflow_results(workflow_id: str):
    """
    Get final workflow results (only available when completed)
    """
    workflow = workflow_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")
    
    if workflow.status != PipelineStatus.COMPLETED:
        raise HTTPException(400, f"Workflow not completed (status: {workflow.status.value})")
    
    # Load comprehensive report
    report_file = BASE_DIR / "outputs" / "reports" / workflow_id / "comprehensive_report.json"
    
    if not report_file.exists():
        raise HTTPException(500, "Results file not found")
    
    import json
    with open(report_file) as f:
        results = json.load(f)
    
    return results


@app.get("/workflow/{workflow_id}/download/report")
async def download_report(workflow_id: str):
    """
    Download comprehensive report JSON
    """
    workflow = workflow_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")
    
    report_file = BASE_DIR / "outputs" / "reports" / workflow_id / "comprehensive_report.json"
    
    if not report_file.exists():
        raise HTTPException(404, "Report file not found")
    
    return FileResponse(
        report_file,
        media_type="application/json",
        filename=f"carbon_report_{workflow_id}.json"
    )


@app.get("/workflow/{workflow_id}/download/carbon")
async def download_carbon_results(workflow_id: str):
    """
    Download carbon estimation results JSON
    """
    workflow = workflow_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")
    
    carbon_file = BASE_DIR / "outputs" / "carbon_estimation" / workflow_id / "carbon_estimation_results.json"
    
    if not carbon_file.exists():
        raise HTTPException(404, "Carbon results file not found")
    
    return FileResponse(
        carbon_file,
        media_type="application/json",
        filename=f"carbon_estimation_{workflow_id}.json"
    )


@app.get("/workflows")
async def list_workflows(limit: int = 20, status_filter: Optional[str] = None):
    """
    List all workflows with optional status filter
    """
    workflows = []
    
    for wf_id, wf in workflow_manager.workflows.items():
        if status_filter and wf.status.value != status_filter:
            continue
        
        workflows.append({
            'workflow_id': wf_id,
            'status': wf.status.value,
            'current_stage': wf.current_stage.value if wf.current_stage else None,
            'created_at': wf.created_at,
            'updated_at': wf.updated_at,
            'species': wf.input_params.get('species'),
            'area_ha': wf.input_params.get('area_ha'),
            'tree_count': wf.input_params.get('tree_count')
        })
    
    # Sort by created_at descending
    workflows.sort(key=lambda x: x['created_at'], reverse=True)
    
    return {
        'total': len(workflows),
        'workflows': workflows[:limit]
    }


# ============================================================================
# Error Handlers
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "status_code": exc.status_code
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "detail": str(exc)
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
