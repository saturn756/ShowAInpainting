"""Safe runtime used for API and deployment smoke tests."""

from __future__ import annotations

from PIL import Image


class MockRuntime:
    ready = False
    cuda_available = False
    device = "mock"

    def generate(self, **kwargs) -> list[Image.Image]:
        raise RuntimeError("mock runtime cannot generate images")
