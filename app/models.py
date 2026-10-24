"""Public response schemas. These define the clean API and drive openapi.json."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class NetworkRef(BaseModel):
    code: str = Field(examples=["DT-001"])
    name: str = Field(examples=["Malviya Nagar DT 1"])
    node_id: str | None = Field(
        None,
        description="Path of codes from the zone down to this node; unique, unlike codes below circle level.",
        examples=["Z-01/C-01/D-01/SD-01/SS-01/F-001/DT-001"],
    )


class Network(BaseModel):
    zone: NetworkRef
    circle: NetworkRef
    division: NetworkRef
    subdivision: NetworkRef
    substation: NetworkRef
    feeder: NetworkRef
    dt: NetworkRef = Field(description="Distribution transformer the meter is connected to.")


class Location(BaseModel):
    lat: float
    lng: float


class DataQualityIssue(BaseModel):
    field: str = Field(examples=["network.circle.code"])
    issue: Literal["blank", "conflict"] | str
    original: str
    resolved_to: str


class Meter(BaseModel):
    meter_id: str = Field(examples=["J100000"])
    serial_no: str | None = Field(examples=["SE33962"])
    make: str | None = Field(examples=["HPL"])
    phase: str | None = Field(description="Known values: single, three.", examples=["single"])
    status: str | None = Field(description="Known values: installed, faulty, decommissioned.", examples=["installed"])
    install_type: str | None = Field(description="Known values: whole_current, ct_operated.", examples=["whole_current"])
    record_format: str | None = Field(
        description="Portal-internal record layout (legacy or v2). Exposed for traceability only.", examples=["legacy"]
    )
    dt_code: str | None = Field(examples=["DT-001"])
    location: Location | None
    network: Network
    data_quality: list[DataQualityIssue] = Field(
        default_factory=list, description="Corrections applied to this meter's network data, if any."
    )
    distance_km: float | None = Field(None, description="Only present on proximity searches.")


class MeterPage(BaseModel):
    data: list[Meter]
    page: int
    page_size: int
    total: int
    as_of: datetime = Field(description="When the underlying portal snapshot was taken.")


class MeterDetail(Meter):
    source: Literal["snapshot", "live"] = Field(
        description="snapshot: served from the bulk-export catalogue; live: fetched page-by-page because the export was unavailable."
    )
    as_of: datetime


class Reading(BaseModel):
    timestamp: datetime = Field(description="ISO 8601 with IST offset (+05:30).")
    kwh_register: float | None = Field(description="Cumulative active-energy register (kWh).")
    kvah_register: float | None = Field(description="Cumulative apparent-energy register (kVAh).")
    voltage_r: float | None = Field(description="R-phase voltage (V).")
    kwh_delta: float | None = Field(description="kWh consumed since the previous reading.")


class ReadingIssue(BaseModel):
    type: str = Field(examples=["duplicate_timestamp"])
    timestamp: datetime | None
    detail: str


class Peak(BaseModel):
    timestamp: datetime
    kwh: float


class ConsumptionSummary(BaseModel):
    readings: int
    first_reading_at: datetime | None
    last_reading_at: datetime | None
    consumption_kwh: float | None = Field(description="Sum of kwh_delta over the window.")
    avg_voltage_r: float | None
    min_voltage_r: float | None
    max_voltage_r: float | None
    peak_interval: Peak | None = Field(description="Interval with the highest kwh_delta.")


class Consumption(BaseModel):
    meter_id: str
    requested_from: date | None
    requested_to: date | None
    interval_minutes: int | None = Field(description="Typical spacing of readings: 30 (half-hourly) or 1440 (daily).")
    timezone: str = "+05:30"
    summary: ConsumptionSummary
    readings: list[Reading]
    data_quality: list[ReadingIssue]


class Transformer(BaseModel):
    code: str
    name: str
    aliases: list[str] = Field(description="Other names this DT appears under in portal data.")
    capacity_kva: int | None
    feeder_code: str
    node_id: str
    meter_count: int
    status_counts: dict[str, int]


class TransformerDetail(Transformer):
    meter_ids: list[str]


class TransformerList(BaseModel):
    data: list[Transformer]
    total: int
    as_of: datetime


class NetworkNode(BaseModel):
    id: str
    level: Literal["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]
    code: str
    name: str
    meter_count: int
    status_counts: dict[str, int]
    children: list[NetworkNode] = Field(default_factory=list)
    meter_ids: list[str] | None = Field(None, description="DT level only.")
    aliases: list[str] | None = Field(None, description="DT level only.")


class Hierarchy(BaseModel):
    data: list[NetworkNode]
    as_of: datetime


class SnapshotStatus(BaseModel):
    loaded: bool
    fetched_at: datetime | None
    age_s: int | None
    ttl_s: int
    stale: bool
    meter_count: int
    transformer_count: int
    repairs: dict[str, int] = Field(description="Count of network-data corrections by kind.")
    last_error: str | None


class ErrorBody(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str
    details: list | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
