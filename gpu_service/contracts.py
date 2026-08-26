"""Stable GPU HTTP request and response contracts."""

from __future__ import annotations

from pydantic import BaseModel


class RoiBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class GenerateRequest(BaseModel):
    background_key: str
    reference_key: str
    mask_key: str
    roi: RoiBox | None = None
    ddim_steps: int = 50
    guidance_scale: float = 7.5
    seed: int = 42
    output_format: str = "jpeg"
    task_id: str | None = None
    user_public_key: str | None = None


class GenerateResponse(BaseModel):
    result_key: str
    result_url: str
    elapsed_seconds: float
    crypto_iv: str | None = None
    crypto_wk: str | None = None
