# -*- coding: utf-8 -*-
"""A/B 实验管理路由（ab_testing + ab_experiment 双后端）。"""
from fastapi import APIRouter, HTTPException, Depends
from .auth import verify_api_key

router = APIRouter(tags=["experiments"], dependencies=[Depends(verify_api_key)])

# ── ab_testing 后端 (前缀 /experiments) ──


@router.get("/experiments")
def list_experiments(status: str = ""):
    from src.host.ab_testing import get_ab_store
    return {"experiments": get_ab_store().list_experiments(status)}


@router.post("/experiments")
def create_experiment(body: dict):
    from src.host.ab_testing import get_ab_store
    exp_id = get_ab_store().create(
        name=body["name"],
        category=body.get("category", "general"),
        variants=body.get("variants"),
    )
    return {"experiment_id": exp_id}


@router.get("/experiments/{name}")
def get_experiment(name: str):
    from src.host.ab_testing import get_ab_store
    summary = get_ab_store().get_experiment_summary(name)
    if not summary:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return summary


@router.get("/experiments/{name}/analyze")
def analyze_experiment(name: str):
    """返回实验各变体统计（供其他节点聚合查询）。"""
    from src.host.ab_testing import get_ab_store
    ab = get_ab_store()
    variants = ab.analyze(name)
    best = ab.best_variant(name, metric="reply_received", min_samples=3)
    return {"experiment": name, "variants": variants, "best_variant": best or "control"}


@router.post("/experiments/{name}/record")
def record_experiment_event(name: str, body: dict):
    from src.host.ab_testing import get_ab_store
    ab = get_ab_store()
    ab.record(name, body["variant"], body["event_type"],
              device_id=body.get("device_id", ""),
              metadata=body.get("metadata"))
    return {"status": "recorded"}


@router.post("/experiments/{name}/end")
def end_experiment(name: str):
    from src.host.ab_testing import get_ab_store
    get_ab_store().end_experiment(name)
    return {"status": "ended"}


# ── ab_experiment 后端 (前缀 /ab) ──


@router.post("/ab/experiments")
def ab_create_experiment(body: dict):
    """Create a new A/B experiment."""
    name = body.get("name", "")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    variants = body.get("variants", [])
    if len(variants) < 2:
        raise HTTPException(status_code=400,
                            detail="At least 2 variants required")
    from src.behavior.ab_experiment import get_experiment_manager
    exp = get_experiment_manager().create_experiment(
        name=name,
        description=body.get("description", ""),
        variants=variants,
        device_ids=body.get("device_ids"),
    )
    return exp.to_dict()


@router.get("/ab/experiments")
def ab_list_experiments():
    """List all experiments."""
    from src.behavior.ab_experiment import get_experiment_manager
    return {"experiments": get_experiment_manager().list_experiments()}


@router.get("/ab/experiments/analyses/all")
def all_experiment_analyses():
    """Get analyses for all experiments."""
    from src.behavior.ab_experiment import get_experiment_manager
    return {"analyses": get_experiment_manager().get_all_analyses()}


@router.get("/ab/experiments/{exp_id}")
def ab_get_experiment(exp_id: str):
    """Get experiment details."""
    from src.behavior.ab_experiment import get_experiment_manager
    exp = get_experiment_manager().get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return exp.to_dict()


@router.get("/ab/experiments/{exp_id}/analysis")
def experiment_analysis(exp_id: str):
    """Get statistical analysis of an experiment."""
    from src.behavior.ab_experiment import get_experiment_manager
    analysis = get_experiment_manager().get_analysis(exp_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return analysis


@router.post("/ab/experiments/{exp_id}/complete")
def complete_experiment(exp_id: str):
    """Complete an experiment and determine winner."""
    from src.behavior.ab_experiment import get_experiment_manager
    result = get_experiment_manager().complete_experiment(exp_id)
    if not result:
        raise HTTPException(status_code=404, detail="Experiment not found")
    return result


@router.post("/ab/experiments/{exp_id}/pause")
def pause_experiment(exp_id: str):
    from src.behavior.ab_experiment import get_experiment_manager
    get_experiment_manager().pause_experiment(exp_id)
    return {"status": "paused"}


@router.post("/ab/experiments/{exp_id}/resume")
def resume_experiment(exp_id: str):
    from src.behavior.ab_experiment import get_experiment_manager
    get_experiment_manager().resume_experiment(exp_id)
    return {"status": "resumed"}


@router.delete("/ab/experiments/{exp_id}")
def delete_experiment(exp_id: str):
    from src.behavior.ab_experiment import get_experiment_manager
    get_experiment_manager().delete_experiment(exp_id)
    return {"status": "deleted"}
