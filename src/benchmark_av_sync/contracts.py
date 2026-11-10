from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any


class ContractError(ValueError):
    """Raised when a persisted contract is invalid."""


def _finite_nonnegative(name: str, value: float) -> None:
    if not isfinite(value) or value < 0:
        raise ContractError(f"{name} must be finite and non-negative, got {value!r}")


@dataclass(slots=True)
class AlignmentToken:
    index: int
    text: str
    start: float
    end: float
    confidence: float = 1.0
    status: str = "valid"
    source: str = "forced_alignment"

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ContractError("token index must be non-negative")
        if not self.text:
            raise ContractError("token text must be non-empty")
        _finite_nonnegative("token start", self.start)
        _finite_nonnegative("token end", self.end)
        if self.end < self.start:
            raise ContractError("token end precedes start")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ContractError("token confidence must be in [0, 1]")

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def is_stable(self) -> bool:
        return self.status == "valid" and self.duration > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AlignmentToken":
        return cls(**value)


@dataclass(slots=True)
class VideoSegment:
    source_start: float
    source_end: float
    speed: float
    output_start: float
    review: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        _finite_nonnegative("segment source_start", self.source_start)
        _finite_nonnegative("segment source_end", self.source_end)
        _finite_nonnegative("segment output_start", self.output_start)
        if self.source_end <= self.source_start:
            raise ContractError("video segment must have positive duration")
        if not isfinite(self.speed) or self.speed <= 0:
            raise ContractError("video segment speed must be positive")

    @property
    def source_duration(self) -> float:
        return self.source_end - self.source_start

    @property
    def output_duration(self) -> float:
        return self.source_duration / self.speed

    @property
    def output_end(self) -> float:
        return self.output_start + self.output_duration

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_end"] = self.output_end
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VideoSegment":
        value = dict(value)
        value.pop("output_end", None)
        return cls(**value)


@dataclass(slots=True)
class AudioChunk:
    source_start: float
    source_end: float
    output_start: float
    fade_seconds: float = 0.03
    reason: str = ""
    continuity_group_id: str = ""
    cut_before_allowed: bool = False
    cut_after_allowed: bool = False

    def __post_init__(self) -> None:
        _finite_nonnegative("audio source_start", self.source_start)
        _finite_nonnegative("audio source_end", self.source_end)
        _finite_nonnegative("audio output_start", self.output_start)
        _finite_nonnegative("audio fade_seconds", self.fade_seconds)
        if self.source_end <= self.source_start:
            raise ContractError("audio chunk must have positive duration")
        if self.fade_seconds * 2 > self.duration:
            self.fade_seconds = self.duration / 2

    @property
    def duration(self) -> float:
        return self.source_end - self.source_start

    @property
    def output_end(self) -> float:
        return self.output_start + self.duration

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_end"] = self.output_end
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AudioChunk":
        value = dict(value)
        value.pop("output_end", None)
        return cls(**value)


@dataclass(slots=True)
class AlignmentPlan:
    pair_id: str
    generated_duration: float
    reference_duration: float
    trim_start: float
    trim_end: float
    output_duration: float
    video_segments: list[VideoSegment]
    audio_chunks: list[AudioChunk]
    status: str = "pass"
    reasons: list[str] = field(default_factory=list)
    residuals_ms: list[float] = field(default_factory=list)
    strategy: str = "dual-audio-alignment"
    output_fps: float = 30.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pair_id:
            raise ContractError("pair_id must be non-empty")
        for name in (
            "generated_duration",
            "reference_duration",
            "trim_start",
            "trim_end",
            "output_duration",
        ):
            _finite_nonnegative(name, getattr(self, name))
        if self.trim_end <= self.trim_start:
            raise ContractError("trim_end must be greater than trim_start")
        if self.trim_end > self.generated_duration + 1e-6:
            raise ContractError("trim_end exceeds generated duration")
        if self.status not in {"pass", "review", "fail"}:
            raise ContractError(f"invalid plan status: {self.status}")
        if not isfinite(self.output_fps) or self.output_fps <= 0:
            raise ContractError("output_fps must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "pair_id": self.pair_id,
            "generated_duration": self.generated_duration,
            "reference_duration": self.reference_duration,
            "trim_start": self.trim_start,
            "trim_end": self.trim_end,
            "output_duration": self.output_duration,
            "video_segments": [x.to_dict() for x in self.video_segments],
            "audio_chunks": [x.to_dict() for x in self.audio_chunks],
            "status": self.status,
            "reasons": list(self.reasons),
            "residuals_ms": list(self.residuals_ms),
            "strategy": self.strategy,
            "output_fps": self.output_fps,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AlignmentPlan":
        data = dict(value)
        data.pop("version", None)
        data["video_segments"] = [VideoSegment.from_dict(x) for x in data["video_segments"]]
        data["audio_chunks"] = [AudioChunk.from_dict(x) for x in data["audio_chunks"]]
        return cls(**data)


@dataclass(slots=True)
class SafeBoundary:
    index: int
    time: float
    pause_ms: float
    cut_allowed: bool
    review: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ContractError("boundary index must be non-negative")
        _finite_nonnegative("boundary time", self.time)
        _finite_nonnegative("boundary pause_ms", self.pause_ms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SafeBoundary":
        return cls(**value)


@dataclass(slots=True)
class BenchmarkGroup:
    group_id: str
    text: str
    token_start: int
    token_end: int
    source_start: float
    source_end: float
    continuity_group_id: str

    def __post_init__(self) -> None:
        if not self.group_id or not self.text or not self.continuity_group_id:
            raise ContractError("benchmark group identifiers and text must be non-empty")
        if self.token_start < 0 or self.token_end < self.token_start:
            raise ContractError("invalid benchmark group token range")
        _finite_nonnegative("benchmark group source_start", self.source_start)
        _finite_nonnegative("benchmark group source_end", self.source_end)
        if self.source_end <= self.source_start:
            raise ContractError("benchmark group must have positive duration")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BenchmarkGroup":
        return cls(**value)


@dataclass(slots=True)
class BenchmarkScript:
    pair_id: str
    text: str
    reference_duration: float
    speech_start: float
    speech_end: float
    tokens: list[AlignmentToken]
    boundaries: list[SafeBoundary]
    groups: list[BenchmarkGroup]
    status: str = "pass"
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pair_id or not self.text:
            raise ContractError("benchmark pair_id and text must be non-empty")
        for name in ("reference_duration", "speech_start", "speech_end"):
            _finite_nonnegative(name, getattr(self, name))
        if self.speech_end <= self.speech_start:
            raise ContractError("benchmark speech range must have positive duration")
        if self.speech_end > self.reference_duration + 1e-6:
            raise ContractError("benchmark speech range exceeds reference duration")
        if self.status not in {"pass", "review", "fail"}:
            raise ContractError(f"invalid benchmark status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pair_id": self.pair_id,
            "text": self.text,
            "reference_duration": self.reference_duration,
            "speech_start": self.speech_start,
            "speech_end": self.speech_end,
            "tokens": [item.to_dict() for item in self.tokens],
            "boundaries": [item.to_dict() for item in self.boundaries],
            "groups": [item.to_dict() for item in self.groups],
            "status": self.status,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BenchmarkScript":
        data = dict(value)
        data.pop("version", None)
        data["tokens"] = [AlignmentToken.from_dict(item) for item in data["tokens"]]
        data["boundaries"] = [SafeBoundary.from_dict(item) for item in data["boundaries"]]
        data["groups"] = [BenchmarkGroup.from_dict(item) for item in data["groups"]]
        return cls(**data)


@dataclass(slots=True)
class MouthFrame:
    time: float
    openness: float
    motion: float
    activity: float
    face_confidence: float
    active: bool

    def __post_init__(self) -> None:
        for name in ("time", "openness", "motion", "activity", "face_confidence"):
            _finite_nonnegative(name, getattr(self, name))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MouthFrame":
        return cls(**value)


@dataclass(slots=True)
class MouthTrack:
    pair_id: str
    duration: float
    fps: float
    frames: list[MouthFrame]
    speech_start: float
    speech_end: float
    active_segments: list[list[float]]
    low_motion_candidates: list[float]
    quality: float
    status: str = "pass"
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.pair_id:
            raise ContractError("mouth track pair_id must be non-empty")
        for name in ("duration", "fps", "speech_start", "speech_end", "quality"):
            _finite_nonnegative(name, getattr(self, name))
        if self.fps <= 0:
            raise ContractError("mouth track fps must be positive")
        if self.speech_end <= self.speech_start:
            raise ContractError("mouth speech range must have positive duration")
        if self.status not in {"pass", "review", "fail"}:
            raise ContractError(f"invalid mouth track status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pair_id": self.pair_id,
            "duration": self.duration,
            "fps": self.fps,
            "frames": [item.to_dict() for item in self.frames],
            "speech_start": self.speech_start,
            "speech_end": self.speech_end,
            "active_segments": [list(item) for item in self.active_segments],
            "low_motion_candidates": list(self.low_motion_candidates),
            "quality": self.quality,
            "status": self.status,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MouthTrack":
        data = dict(value)
        data.pop("version", None)
        data["frames"] = [MouthFrame.from_dict(item) for item in data["frames"]]
        return cls(**data)


@dataclass(slots=True)
class VerificationReport:
    pair_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
