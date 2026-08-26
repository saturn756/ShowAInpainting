"""Model runtime contract exposed to the GPU API layer."""

from __future__ import annotations

from typing import Protocol

from PIL import Image


class InferenceRuntime(Protocol):
    ready: bool
    cuda_available: bool
    device: str

    def generate(
        self,
        *,
        pil_ref_image: Image.Image,
        pil_background_image: Image.Image,
        pil_mask_image: Image.Image,
        num_samples: int,
        num_inference_steps: int,
        guidance_scale: float,
        seed: int,
    ) -> list[Image.Image]:
        ...
