from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
import time
import uuid

BusType = Literal['CAN', 'FlexRay', 'EthDoIP', 'EthSOMEIP', 'EthXCP']
Severity = Literal['info', 'warning', 'critical']


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class DataSource:
    id: str
    name: str
    type: BusType
    enabled: bool
    config: Dict[str, Any] = field(default_factory=dict)
    dbc_name: str = ''
    fibex_name: str = ''
    created_at_ms: int = field(default_factory=now_ms)
    updated_at_ms: int = field(default_factory=now_ms)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'enabled': bool(self.enabled),
            'config': dict(self.config or {}),
            'dbc_name': str(self.dbc_name or ''),
            'fibex_name': str(self.fibex_name or ''),
            'created_at_ms': int(self.created_at_ms or 0),
            'updated_at_ms': int(self.updated_at_ms or 0),
        }


@dataclass
class SignalRef:
    source_id: str
    message: str
    signal: str
    unit: Optional[str] = None

    def key(self) -> str:
        return f"{self.source_id}:{self.message}:{self.signal}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            'source_id': self.source_id,
            'message': self.message,
            'signal': self.signal,
            'unit': self.unit,
        }


CompareOp = Literal['lt', 'gt', 'eq', 'le', 'ge', 'ne', 'delta_abs', 'delta_pct', 'missing']


@dataclass
class RuleAction:
    kind: Literal['log_csv', 'emit_ws', 'beep']
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {'kind': self.kind, 'params': dict(self.params or {})}


@dataclass
class ComparisonRule:
    id: str
    name: str
    enabled: bool
    severity: Severity
    a: SignalRef
    op: CompareOp
    b_kind: Literal['signal', 'const']
    b: Optional[SignalRef] = None
    b_const: Optional[float] = None
    threshold: float = 0.0
    debounce_s: float = 2.0
    missing_timeout_s: float = 0.5
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    conditions_mode: Literal['and', 'or'] = 'and'
    actions: List[RuleAction] = field(default_factory=list)
    created_at_ms: int = field(default_factory=now_ms)
    updated_at_ms: int = field(default_factory=now_ms)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'enabled': bool(self.enabled),
            'severity': self.severity,
            'a': self.a.to_dict(),
            'op': self.op,
            'b_kind': self.b_kind,
            'b': self.b.to_dict() if self.b else None,
            'b_const': self.b_const,
            'threshold': float(self.threshold or 0.0),
            'debounce_s': float(self.debounce_s or 0.0),
            'missing_timeout_s': float(self.missing_timeout_s or 0.0),
            'conditions': list(self.conditions or []),
            'conditions_mode': self.conditions_mode,
            'actions': [x.to_dict() for x in (self.actions or [])],
            'created_at_ms': int(self.created_at_ms or 0),
            'updated_at_ms': int(self.updated_at_ms or 0),
        }


@dataclass
class Violation:
    id: str
    ts_ms: int
    rule_id: str
    rule_name: str
    severity: Severity
    description: str
    a: Dict[str, Any]
    b: Dict[str, Any]
    diff: Optional[float] = None
    threshold: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'ts_ms': int(self.ts_ms or 0),
            'rule_id': self.rule_id,
            'rule_name': self.rule_name,
            'severity': self.severity,
            'description': self.description,
            'a': dict(self.a or {}),
            'b': dict(self.b or {}),
            'diff': self.diff,
            'threshold': self.threshold,
        }
