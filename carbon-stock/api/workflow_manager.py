"""
Plantation Carbon Estimation - Pipeline Workflow Manager
=========================================================

Orchestrates sequential pipeline execution with proper delays and status tracking
"""

import asyncio
import json
import subprocess
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict
import logging

# Import species database from carbon estimation script
import sys
sys.path.append(str(Path(__file__).parent.parent / 'scripts'))

try:
    from plantation_carbon_estimation import SPECIES_DATABASE
except ImportError:
    # Fallback species database
    from dataclasses import dataclass as species_dataclass
    
    @species_dataclass
    class SpeciesData:
        common_name: str
        scientific_name: str
        wood_density: float
        allometry_a: float
        allometry_b: float
        carbon_fraction: float
        source: str
    
    SPECIES_DATABASE = {
        'teak': SpeciesData(
            common_name='Teak',
            scientific_name='Tectona grandis',
            wood_density=0.55,
            allometry_a=0.1428,
            allometry_b=2.343,
            carbon_fraction=0.47,
            source='Perez & Kanninen (2003)'
        )
    }

logger = logging.getLogger(__name__)


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PipelineStage(str, Enum):
    DATA_UPLOAD = "data_upload"
    DATA_VALIDATION = "data_validation"
    PREPROCESSING = "preprocessing"
    FOREST_ANALYSIS = "forest_analysis"
    CARBON_ESTIMATION = "carbon_estimation"
    REPORT_GENERATION = "report_generation"


@dataclass
class StageResult:
    stage: PipelineStage
    status: PipelineStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    output_files: List[str] = None
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if self.output_files is None:
            self.output_files = []


@dataclass
class WorkflowState:
    workflow_id: str
    status: PipelineStatus
    current_stage: Optional[PipelineStage]
    stages: Dict[str, StageResult]
    created_at: str
    updated_at: str
    input_params: Dict
    total_duration: Optional[float] = None
    
    def to_dict(self):
        data = asdict(self)
        # Convert enums to strings
        data['status'] = self.status.value
        if self.current_stage:
            data['current_stage'] = self.current_stage.value
        data['stages'] = {k: {**v, 'stage': v['stage'].value, 'status': v['status'].value} 
                          for k, v in data['stages'].items()}
        return data


class PipelineWorkflowManager:
    """
    Manages end-to-end pipeline execution with proper sequencing and delays
    """
    
    def __init__(self, base_dir: Path, python_exe: Path):
        self.base_dir = base_dir
        self.python_exe = python_exe
        self.workflows: Dict[str, WorkflowState] = {}
        self.state_file = base_dir / "outputs" / "workflow_states.json"
        self._load_states()
    
    def _load_states(self):
        """Load previous workflow states from disk"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    # Reconstruct workflow states (simplified version)
                    self.workflows = data
            except Exception as e:
                logger.warning(f"Could not load previous states: {e}")
    
    def _save_state(self, workflow_id: str):
        """Save workflow state to disk"""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump({wid: wf.to_dict() for wid, wf in self.workflows.items()}, 
                         f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
    
    def create_workflow(
        self,
        workflow_id: str,
        s2_file: str,
        s1_file: str,
        species: str,
        area_ha: float,
        tree_count: int,
        aoi_geojson: Optional[str] = None
    ) -> WorkflowState:
        """
        Create a new workflow instance
        """
        now = datetime.utcnow().isoformat()
        
        workflow = WorkflowState(
            workflow_id=workflow_id,
            status=PipelineStatus.PENDING,
            current_stage=None,
            stages={
                stage.value: StageResult(
                    stage=stage,
                    status=PipelineStatus.PENDING
                )
                for stage in PipelineStage
            },
            created_at=now,
            updated_at=now,
            input_params={
                's2_file': s2_file,
                's1_file': s1_file,
                'species': species,
                'area_ha': area_ha,
                'tree_count': tree_count,
                'aoi_geojson': aoi_geojson
            }
        )
        
        self.workflows[workflow_id] = workflow
        self._save_state(workflow_id)
        return workflow
    
    def get_workflow(self, workflow_id: str) -> Optional[WorkflowState]:
        """Get workflow state by ID"""
        return self.workflows.get(workflow_id)
    
    async def execute_workflow(self, workflow_id: str):
        """
        Execute complete workflow pipeline with proper sequencing
        """
        workflow = self.workflows.get(workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {workflow_id} not found")
        
        workflow.status = PipelineStatus.RUNNING
        workflow.updated_at = datetime.utcnow().isoformat()
        self._save_state(workflow_id)
        
        start_time = time.time()
        
        try:
            # Stage 1: Data validation
            await self._execute_stage(
                workflow_id,
                PipelineStage.DATA_VALIDATION,
                self._validate_data,
                delay_after=2.0
            )
            
            # Stage 2: Preprocessing
            await self._execute_stage(
                workflow_id,
                PipelineStage.PREPROCESSING,
                self._run_preprocessing,
                delay_after=3.0
            )
            
            # Stage 3: Forest analysis (honest pipeline)
            await self._execute_stage(
                workflow_id,
                PipelineStage.FOREST_ANALYSIS,
                self._run_forest_analysis,
                delay_after=2.0
            )
            
            # Stage 4: Carbon estimation
            await self._execute_stage(
                workflow_id,
                PipelineStage.CARBON_ESTIMATION,
                self._run_carbon_estimation,
                delay_after=2.0
            )
            
            # Stage 5: Report generation
            await self._execute_stage(
                workflow_id,
                PipelineStage.REPORT_GENERATION,
                self._generate_report,
                delay_after=1.0
            )
            
            # Workflow completed
            workflow.status = PipelineStatus.COMPLETED
            workflow.total_duration = time.time() - start_time
            workflow.updated_at = datetime.utcnow().isoformat()
            self._save_state(workflow_id)
            
            logger.info(f"Workflow {workflow_id} completed in {workflow.total_duration:.1f}s")
            
        except Exception as e:
            workflow.status = PipelineStatus.FAILED
            workflow.updated_at = datetime.utcnow().isoformat()
            self._save_state(workflow_id)
            logger.error(f"Workflow {workflow_id} failed: {e}")
            raise
    
    async def _execute_stage(
        self,
        workflow_id: str,
        stage: PipelineStage,
        func,
        delay_after: float = 0.0
    ):
        """
        Execute a single pipeline stage with timing and error handling
        """
        workflow = self.workflows[workflow_id]
        stage_result = workflow.stages[stage.value]
        
        workflow.current_stage = stage
        stage_result.status = PipelineStatus.RUNNING
        stage_result.started_at = datetime.utcnow().isoformat()
        workflow.updated_at = datetime.utcnow().isoformat()
        self._save_state(workflow_id)
        
        logger.info(f"[{workflow_id}] Starting stage: {stage.value}")
        
        start_time = time.time()
        
        try:
            # Execute stage function
            output_files = await func(workflow_id)
            
            # Success
            stage_result.status = PipelineStatus.COMPLETED
            stage_result.completed_at = datetime.utcnow().isoformat()
            stage_result.duration_seconds = time.time() - start_time
            stage_result.output_files = output_files or []
            
            logger.info(f"[{workflow_id}] Completed stage: {stage.value} "
                       f"in {stage_result.duration_seconds:.1f}s")
            
            # Delay before next stage (allow file system to settle)
            if delay_after > 0:
                logger.info(f"[{workflow_id}] Waiting {delay_after}s before next stage...")
                await asyncio.sleep(delay_after)
            
        except Exception as e:
            stage_result.status = PipelineStatus.FAILED
            stage_result.completed_at = datetime.utcnow().isoformat()
            stage_result.duration_seconds = time.time() - start_time
            stage_result.error_message = str(e)
            
            logger.error(f"[{workflow_id}] Stage {stage.value} failed: {e}")
            raise
        
        finally:
            workflow.updated_at = datetime.utcnow().isoformat()
            self._save_state(workflow_id)
    
    async def _validate_data(self, workflow_id: str) -> List[str]:
        """Validate input data files exist and are valid rasters"""
        workflow = self.workflows[workflow_id]
        params = workflow.input_params
        
        s2_path = Path(params['s2_file'])
        s1_path = Path(params['s1_file'])
        
        if not s2_path.exists():
            raise FileNotFoundError(f"Sentinel-2 file not found: {s2_path}")
        if not s1_path.exists():
            raise FileNotFoundError(f"Sentinel-1 file not found: {s1_path}")
        
        # Quick validation using rasterio
        import rasterio
        try:
            with rasterio.open(s2_path) as src:
                logger.info(f"S2: {src.count} bands, {src.shape}")
            with rasterio.open(s1_path) as src:
                logger.info(f"S1: {src.count} bands, {src.shape}")
        except Exception as e:
            raise ValueError(f"Invalid raster file: {e}")
        
        return [str(s2_path), str(s1_path)]
    
    async def _run_preprocessing(self, workflow_id: str) -> List[str]:
        """Run preprocessing if needed (currently data is pre-processed)"""
        workflow = self.workflows[workflow_id]
        params = workflow.input_params
        
        # For now, data is already preprocessed
        # In future, this could run:
        # - Band stacking
        # - Cloud masking
        # - Resampling
        # - Index calculation
        
        logger.info(f"[{workflow_id}] Preprocessing: Data already processed")
        
        return [
            str(Path(params['s2_file'])),
            str(Path(params['s1_file']))
        ]
    
    async def _run_forest_analysis(self, workflow_id: str) -> List[str]:
        """Run honest forest analysis pipeline"""
        workflow = self.workflows[workflow_id]
        params = workflow.input_params
        
        output_dir = self.base_dir / "outputs" / "honest_analysis" / workflow_id
        output_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            str(self.python_exe),
            str(self.base_dir / "scripts" / "honest_forest_analysis_pipeline.py"),
            "--s2", params['s2_file'],
            "--s1", params['s1_file'],
            "--output", str(output_dir)
        ]
        
        logger.info(f"[{workflow_id}] Running: {' '.join(cmd)}")
        
        # Run subprocess asynchronously
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.base_dir)
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise RuntimeError(f"Forest analysis failed: {stderr.decode()}")
        
        # Find output files
        output_files = [
            str(output_dir / "forest_mask.tif"),
            str(output_dir / "canopy_cover_pct.tif"),
            str(output_dir / "canopy_density_class.tif"),
            str(output_dir / "biomass_proxy_unitless.tif"),
            str(output_dir / "analysis_report.json")
        ]
        
        return [f for f in output_files if Path(f).exists()]
    
    async def _run_carbon_estimation(self, workflow_id: str) -> List[str]:
        """Run plantation carbon estimation"""
        workflow = self.workflows[workflow_id]
        params = workflow.input_params
        
        output_dir = self.base_dir / "outputs" / "carbon_estimation" / workflow_id
        output_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            str(self.python_exe),
            str(self.base_dir / "scripts" / "plantation_carbon_estimation.py"),
            "--s2", params['s2_file'],
            "--s1", params['s1_file'],
            "--species", params['species'],
            "--area", str(params['area_ha']),
            "--trees", str(params['tree_count']),
            "--output", str(output_dir)
        ]
        
        logger.info(f"[{workflow_id}] Running: {' '.join(cmd)}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.base_dir)
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise RuntimeError(f"Carbon estimation failed: {stderr.decode()}")
        
        result_file = output_dir / "carbon_estimation_results.json"
        return [str(result_file)] if result_file.exists() else []
    
    async def _generate_report(self, workflow_id: str) -> List[str]:
        """Generate comprehensive report combining all results"""
        workflow = self.workflows[workflow_id]
        params = workflow.input_params
        
        output_dir = self.base_dir / "outputs" / "reports" / workflow_id
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Load forest analysis results
        forest_analysis_file = (
            self.base_dir / "outputs" / "honest_analysis" / workflow_id / "analysis_report.json"
        )
        forest_analysis = {}
        if forest_analysis_file.exists():
            with open(forest_analysis_file) as f:
                forest_analysis = json.load(f)
        
        # Load carbon estimation results
        carbon_file = (
            self.base_dir / "outputs" / "carbon_estimation" / workflow_id / 
            "carbon_estimation_results.json"
        )
        carbon_results = {}
        if carbon_file.exists():
            with open(carbon_file) as f:
                carbon_results = json.load(f)
        
        # Create combined report
        report = {
            'workflow_id': workflow_id,
            'generated_at': datetime.utcnow().isoformat(),
            'input_parameters': params,
            'forest_analysis': forest_analysis,
            'carbon_estimation': carbon_results,
            'pipeline_execution': {
                'total_duration': workflow.total_duration,
                'stages': {
                    stage_name: {
                        'status': stage_data.status.value,
                        'duration': stage_data.duration_seconds,
                        'output_files': stage_data.output_files
                    }
                    for stage_name, stage_data in workflow.stages.items()
                }
            },
            'disclaimers': [
                'Carbon estimates have ±68-70% uncertainty',
                'NOT suitable for carbon stock monetization',
                'Height from SAR without LiDAR validation',
                'Generic allometry not site-calibrated',
                'Monitoring & analysis only - NOT for registry submission'
            ]
        }
        
        report_file = output_dir / "comprehensive_report.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"[{workflow_id}] Report generated: {report_file}")
        
        return [str(report_file)]
