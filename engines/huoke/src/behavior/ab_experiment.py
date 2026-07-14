# -*- coding: utf-8 -*-
"""
A/B 策略实验框架 — 不同设备/账号使用不同养号策略，自动对比效果。

实验结构:
  Experiment
    ├─ Variant A (control): params = {like_prob: 0.20, duration: 30}
    ├─ Variant B (test):    params = {like_prob: 0.35, duration: 25}
    └─ Variant C (test):    params = {like_prob: 0.15, duration: 40}

每个 Variant 分配若干设备/账号，追踪指标后自动统计对比。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import data_dir

log = logging.getLogger(__name__)

_DATA_DIR = data_dir() / "experiments"


@dataclass
class VariantMetrics:
    """Accumulated metrics for one variant."""
    sessions: int = 0
    total_watched: int = 0
    total_liked: int = 0
    total_commented: int = 0
    total_followed: int = 0
    follow_success: int = 0
    follow_attempts: int = 0
    algo_scores: List[float] = field(default_factory=list)
    engagement_rates: List[float] = field(default_factory=list)

    @property
    def avg_algo_score(self) -> float:
        return sum(self.algo_scores) / max(len(self.algo_scores), 1)

    @property
    def avg_engagement(self) -> float:
        return sum(self.engagement_rates) / max(len(self.engagement_rates), 1)

    @property
    def like_rate(self) -> float:
        return self.total_liked / max(self.total_watched, 1)

    @property
    def follow_rate(self) -> float:
        return self.follow_success / max(self.follow_attempts, 1)

    def to_dict(self) -> dict:
        return {
            "sessions": self.sessions,
            "total_watched": self.total_watched,
            "total_liked": self.total_liked,
            "total_commented": self.total_commented,
            "total_followed": self.total_followed,
            "follow_success": self.follow_success,
            "follow_attempts": self.follow_attempts,
            "avg_algo_score": round(self.avg_algo_score, 4),
            "avg_engagement": round(self.avg_engagement, 4),
            "like_rate": round(self.like_rate, 4),
            "follow_rate": round(self.follow_rate, 4),
            "samples_algo": len(self.algo_scores),
            "samples_engagement": len(self.engagement_rates),
        }

    @staticmethod
    def from_dict(d: dict) -> VariantMetrics:
        vm = VariantMetrics()
        vm.sessions = d.get("sessions", 0)
        vm.total_watched = d.get("total_watched", 0)
        vm.total_liked = d.get("total_liked", 0)
        vm.total_commented = d.get("total_commented", 0)
        vm.total_followed = d.get("total_followed", 0)
        vm.follow_success = d.get("follow_success", 0)
        vm.follow_attempts = d.get("follow_attempts", 0)
        vm.algo_scores = d.get("algo_scores", [])
        vm.engagement_rates = d.get("engagement_rates", [])
        return vm


@dataclass
class Variant:
    """A single variant (arm) in an A/B experiment."""
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    devices: List[str] = field(default_factory=list)
    metrics: VariantMetrics = field(default_factory=VariantMetrics)
    is_control: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "params": self.params,
            "devices": self.devices,
            "metrics": self.metrics.to_dict(),
            "is_control": self.is_control,
        }

    @staticmethod
    def from_dict(d: dict) -> Variant:
        return Variant(
            name=d["name"],
            params=d.get("params", {}),
            devices=d.get("devices", []),
            metrics=VariantMetrics.from_dict(d.get("metrics", {})),
            is_control=d.get("is_control", False),
        )


@dataclass
class Experiment:
    """A/B experiment definition with variants and results."""
    experiment_id: str
    name: str
    description: str = ""
    variants: List[Variant] = field(default_factory=list)
    status: str = "active"  # active, paused, completed
    created_at: float = 0.0
    updated_at: float = 0.0
    winner: str = ""

    def get_variant_for_device(self, device_id: str) -> Optional[Variant]:
        for v in self.variants:
            if device_id in v.devices:
                return v
        return None

    def auto_assign(self, device_ids: List[str]):
        """Evenly distribute devices across variants."""
        n = len(self.variants)
        if n == 0:
            return
        for i, did in enumerate(device_ids):
            already = any(did in v.devices for v in self.variants)
            if not already:
                self.variants[i % n].devices.append(did)

    def get_analysis(self) -> dict:
        """Compare variants and determine winner."""
        results = {}
        best_variant = ""
        best_score = -1.0

        for v in self.variants:
            m = v.metrics
            composite = (m.avg_algo_score * 0.35
                         + m.avg_engagement * 100 * 0.30
                         + m.follow_rate * 0.20
                         + min(m.sessions / 10, 1.0) * 0.15)
            results[v.name] = {
                "composite_score": round(composite, 4),
                "is_control": v.is_control,
                "device_count": len(v.devices),
                **v.metrics.to_dict(),
            }
            if composite > best_score and m.sessions >= 3:
                best_score = composite
                best_variant = v.name

        control = next((v for v in self.variants if v.is_control), None)
        control_score = 0
        if control:
            cm = control.metrics
            control_score = (cm.avg_algo_score * 0.35
                             + cm.avg_engagement * 100 * 0.30
                             + cm.follow_rate * 0.20
                             + min(cm.sessions / 10, 1.0) * 0.15)

        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "status": self.status,
            "variants": results,
            "winner": best_variant,
            "winner_lift": (round(
                (best_score - control_score) / max(control_score, 0.001) * 100,
                1) if control_score > 0 else 0),
            "confidence": ("high" if all(
                v.metrics.sessions >= 5 for v in self.variants
            ) else "low"),
        }

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "description": self.description,
            "variants": [v.to_dict() for v in self.variants],
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "winner": self.winner,
        }

    @staticmethod
    def from_dict(d: dict) -> Experiment:
        return Experiment(
            experiment_id=d["experiment_id"],
            name=d["name"],
            description=d.get("description", ""),
            variants=[Variant.from_dict(v) for v in d.get("variants", [])],
            status=d.get("status", "active"),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            winner=d.get("winner", ""),
        )


class ExperimentManager:
    """Manages A/B experiments lifecycle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._experiments: Dict[str, Experiment] = {}
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def create_experiment(self, name: str, description: str,
                          variants: List[dict],
                          device_ids: Optional[List[str]] = None) -> Experiment:
        """
        Create a new A/B experiment.

        variants format: [
          {"name": "control", "params": {...}, "is_control": true},
          {"name": "high_like", "params": {"like_probability": 0.35}},
        ]
        """
        exp = Experiment(
            experiment_id=uuid.uuid4().hex[:12],
            name=name,
            description=description,
            created_at=time.time(),
            updated_at=time.time(),
        )
        for vd in variants:
            v = Variant(
                name=vd["name"],
                params=vd.get("params", {}),
                is_control=vd.get("is_control", False),
                devices=vd.get("devices", []),
            )
            exp.variants.append(v)

        if device_ids:
            exp.auto_assign(device_ids)

        with self._lock:
            self._experiments[exp.experiment_id] = exp
        self._save(exp)
        log.info("[A/B] 创建实验 '%s' (%d 变体, %d 设备)",
                 name, len(variants),
                 sum(len(v.devices) for v in exp.variants))
        return exp

    def get_experiment(self, exp_id: str) -> Optional[Experiment]:
        with self._lock:
            return self._experiments.get(exp_id)

    def list_experiments(self) -> List[dict]:
        with self._lock:
            return [e.to_dict() for e in self._experiments.values()]

    def get_device_params(self, device_id: str) -> Optional[dict]:
        """
        Get experiment-override params for a device.
        Returns the variant params if the device is in an active experiment.
        """
        with self._lock:
            for exp in self._experiments.values():
                if exp.status != "active":
                    continue
                variant = exp.get_variant_for_device(device_id)
                if variant:
                    return {
                        "_experiment": exp.name,
                        "_variant": variant.name,
                        **variant.params,
                    }
        return None

    def record_session(self, device_id: str, warmup_stats: dict,
                       algo_score: float = 0):
        """Record session results for the device's experiment variant."""
        with self._lock:
            for exp in self._experiments.values():
                if exp.status != "active":
                    continue
                variant = exp.get_variant_for_device(device_id)
                if not variant:
                    continue

                m = variant.metrics
                m.sessions += 1
                m.total_watched += warmup_stats.get("watched", 0)
                m.total_liked += warmup_stats.get("liked", 0)
                m.total_commented += warmup_stats.get("comments_posted", 0)
                m.total_followed += warmup_stats.get("followed", 0)
                if algo_score > 0:
                    m.algo_scores.append(algo_score)
                watched = warmup_stats.get("watched", 0)
                if watched > 0:
                    liked = warmup_stats.get("liked", 0)
                    commented = warmup_stats.get("comments_posted", 0)
                    m.engagement_rates.append(
                        (liked + commented) / watched)
                exp.updated_at = time.time()

                self._check_early_stopping(exp)
                self._save(exp)
                return

    def _check_early_stopping(self, exp: Experiment):
        """Auto-complete if a variant is clearly inferior."""
        control = next((v for v in exp.variants if v.is_control), None)
        if not control or control.metrics.sessions < 5:
            return
        cm = control.metrics
        ctrl_score = cm.avg_algo_score * 0.5 + cm.avg_engagement * 50
        if ctrl_score <= 0:
            return

        all_ready = all(v.metrics.sessions >= 5 for v in exp.variants)
        if not all_ready:
            return

        for v in exp.variants:
            if v.is_control:
                continue
            vm = v.metrics
            v_score = vm.avg_algo_score * 0.5 + vm.avg_engagement * 50
            if v_score < ctrl_score * 0.7 and vm.sessions >= 5:
                log.info("[A/B] 变体 '%s' 表现明显不佳 (%.2f vs %.2f)，"
                         "建议提前终止", v.name, v_score, ctrl_score)

    def record_follow_result(self, device_id: str,
                             success: bool, attempts: int = 1):
        """Record follow result for experiment tracking."""
        with self._lock:
            for exp in self._experiments.values():
                if exp.status != "active":
                    continue
                variant = exp.get_variant_for_device(device_id)
                if not variant:
                    continue
                variant.metrics.follow_attempts += attempts
                if success:
                    variant.metrics.follow_success += 1
                exp.updated_at = time.time()
                self._save(exp)
                return

    def get_analysis(self, exp_id: str) -> Optional[dict]:
        with self._lock:
            exp = self._experiments.get(exp_id)
        if not exp:
            return None
        return exp.get_analysis()

    def get_all_analyses(self) -> List[dict]:
        with self._lock:
            exps = list(self._experiments.values())
        return [e.get_analysis() for e in exps]

    def complete_experiment(self, exp_id: str) -> Optional[dict]:
        """Mark experiment as completed and determine winner."""
        with self._lock:
            exp = self._experiments.get(exp_id)
            if not exp:
                return None
            analysis = exp.get_analysis()
            exp.status = "completed"
            exp.winner = analysis.get("winner", "")
            exp.updated_at = time.time()
            self._save(exp)
        return analysis

    def pause_experiment(self, exp_id: str):
        with self._lock:
            exp = self._experiments.get(exp_id)
            if exp:
                exp.status = "paused"
                self._save(exp)

    def resume_experiment(self, exp_id: str):
        with self._lock:
            exp = self._experiments.get(exp_id)
            if exp and exp.status == "paused":
                exp.status = "active"
                self._save(exp)

    def delete_experiment(self, exp_id: str):
        with self._lock:
            self._experiments.pop(exp_id, None)
        path = _DATA_DIR / f"{exp_id}.json"
        if path.exists():
            path.unlink()

    def _save(self, exp: Experiment):
        try:
            path = _DATA_DIR / f"{exp.experiment_id}.json"
            path.write_text(
                json.dumps(exp.to_dict(), indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            log.debug("[A/B] 保存失败: %s", e)

    def _load_all(self):
        try:
            for f in _DATA_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    exp = Experiment.from_dict(data)
                    self._experiments[exp.experiment_id] = exp
                except Exception:
                    continue
            if self._experiments:
                log.info("[A/B] 已加载 %d 个实验", len(self._experiments))
        except Exception:
            pass


_manager: Optional[ExperimentManager] = None
_mgr_lock = threading.Lock()


def get_experiment_manager() -> ExperimentManager:
    global _manager
    if _manager is None:
        with _mgr_lock:
            if _manager is None:
                _manager = ExperimentManager()
    return _manager
