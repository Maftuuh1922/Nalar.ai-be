import os
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from typing import Dict, Any
import uuid

from app.api.deps import get_current_user
from app.models.user import User
from app.services.storm_service import setup_storm_runner
from app.services.docx_exporter import markdown_to_docx

router = APIRouter(prefix="/storm", tags=["storm"])

# In-memory status tracker for simplicity (in prod, use DB/Redis)
storm_tasks: Dict[str, Dict[str, Any]] = {}

def run_storm_task(task_id: str, topic: str):
    """Background task to run STORM and generate the report."""
    storm_tasks[task_id]["status"] = "running"
    try:
        output_dir = f"./storm_output/{task_id}"
        runner = setup_storm_runner(output_dir=output_dir)
        
        # Run STORM algorithm (this may take several minutes)
        runner.run(
            topic=topic,
            do_research=True,
            do_generate_outline=True,
            do_generate_article=True,
            do_polish_article=True,
        )
        
        runner.post_run()
        runner.summary()
        
        # Look for the generated markdown article
        article_path = os.path.join(output_dir, "storm_gen_article_polished.txt")
        if not os.path.exists(article_path):
            article_path = os.path.join(output_dir, "storm_gen_article.txt")
            
        if os.path.exists(article_path):
            with open(article_path, "r") as f:
                content = f.read()
            
            # Export to Docx
            docx_path = os.path.join(output_dir, f"{topic.replace(' ', '_')}_Report.docx")
            markdown_to_docx(content, docx_path)
            
            storm_tasks[task_id]["status"] = "completed"
            storm_tasks[task_id]["result_text"] = content
            storm_tasks[task_id]["docx_path"] = docx_path
        else:
            storm_tasks[task_id]["status"] = "failed"
            storm_tasks[task_id]["error"] = "Article generation failed. Output not found."
            
    except Exception as e:
        storm_tasks[task_id]["status"] = "failed"
        storm_tasks[task_id]["error"] = str(e)


@router.post("/generate")
async def generate_storm_report(
    topic: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """Start a new STORM report generation task."""
    task_id = str(uuid.uuid4())
    storm_tasks[task_id] = {
        "status": "pending",
        "topic": topic,
        "result_text": None,
        "docx_path": None,
        "error": None
    }
    
    background_tasks.add_task(run_storm_task, task_id, topic)
    return {"task_id": task_id, "status": "pending", "message": "STORM report generation started."}

@router.get("/status/{task_id}")
async def get_storm_status(task_id: str, current_user: User = Depends(get_current_user)):
    """Check the status of a STORM report generation task."""
    if task_id not in storm_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task_info = storm_tasks[task_id].copy()
    # Don't send the full path over the API
    if "docx_path" in task_info:
        task_info["has_docx"] = bool(task_info["docx_path"])
        del task_info["docx_path"]
        
    return task_info

@router.get("/download/{task_id}")
async def download_storm_docx(task_id: str, current_user: User = Depends(get_current_user)):
    """Download the generated DOCX report."""
    if task_id not in storm_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
        
    task_info = storm_tasks[task_id]
    if task_info["status"] != "completed" or not task_info.get("docx_path"):
        raise HTTPException(status_code=400, detail="Report is not ready or failed.")
        
    if not os.path.exists(task_info["docx_path"]):
        raise HTTPException(status_code=404, detail="DOCX file not found on server.")
        
    return FileResponse(
        path=task_info["docx_path"],
        filename=os.path.basename(task_info["docx_path"]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
