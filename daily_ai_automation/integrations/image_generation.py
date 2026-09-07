"""Greeting image generation via Google Gemini (PRD section 18).

The prompt sent here is built from a generic scene description plus, at most, a
first name. Nothing else about the recipient reaches the image service - PRD
section 18 is explicit that highly personal information must not go into image
prompts, and an image API is the one integration whose output we cannot inspect
before a human sees it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..supervisor.retry import TerminalError

logger = logging.getLogger(__name__)

#: Characters allowed through into a filename built from a recipient name.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class ImageGenerationError(RuntimeError):
    """The image service returned nothing usable."""


@dataclass
class GeneratedImage:
    data: bytes
    mime_type: str
    path: Path | None = None

    @property
    def extension(self) -> str:
        return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
            self.mime_type, ".png"
        )


class ImageClient:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.5-flash-image",
        aspect_ratio: str = "1:1",
        image_size: str = "1K",
    ) -> None:
        if not api_key:
            raise TerminalError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
            )
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.aspect_ratio = aspect_ratio
        self.image_size = image_size

    def generate(self, prompt: str) -> GeneratedImage:
        """Generate one image. Raises ImageGenerationError if none comes back."""
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(
                        aspect_ratio=self.aspect_ratio,
                        image_size=self.image_size,
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - translated below
            message = str(exc)
            if "API key" in message or "PERMISSION_DENIED" in message:
                raise TerminalError(
                    f"Gemini rejected the API key or the request: {message}"
                ) from exc
            raise

        image = _first_image(response)
        if image is None:
            raise ImageGenerationError(
                "Gemini returned no image. This is usually a safety filter "
                f"rejecting the prompt. Finish reason: {_finish_reason(response)}"
            )
        return image

    def generate_and_save(self, prompt: str, destination: Path) -> GeneratedImage:
        image = self.generate(prompt)
        destination.parent.mkdir(parents=True, exist_ok=True)
        path = destination.with_suffix(image.extension)
        path.write_bytes(image.data)
        image.path = path
        logger.debug("Saved generated image to %s (%d bytes)", path, len(image.data))
        return image


def _first_image(response) -> GeneratedImage | None:  # noqa: ANN001 - SDK response
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            blob = getattr(part, "inline_data", None)
            if blob is not None and getattr(blob, "data", None):
                return GeneratedImage(
                    data=blob.data,
                    mime_type=getattr(blob, "mime_type", "image/png") or "image/png",
                )
    return None


def _finish_reason(response) -> str:  # noqa: ANN001 - SDK response
    for candidate in getattr(response, "candidates", None) or []:
        reason = getattr(candidate, "finish_reason", None)
        if reason:
            return str(reason)
    return "unknown"


def safe_filename(*parts: str) -> str:
    """Build a filesystem-safe stem from arbitrary text."""
    joined = "_".join(part for part in parts if part)
    cleaned = _UNSAFE_FILENAME.sub("_", joined).strip("_")
    return cleaned[:80] or "image"
