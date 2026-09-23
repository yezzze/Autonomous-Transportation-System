"""Shared image preprocessing used by training and video inference."""

from PIL import Image


try:
    BILINEAR = Image.Resampling.BILINEAR
except AttributeError:  # Pillow < 9.1
    BILINEAR = Image.BILINEAR


class LetterboxResize:
    """Resize an image without cropping or changing its aspect ratio.

    The resized image is centered on a square canvas. For a 16:9 dashcam
    frame this keeps the complete horizontal field of view and adds padding
    above and below instead of discarding the left and right sides.
    """

    def __init__(self, size=224, fill=(114, 114, 114)):
        if isinstance(size, int):
            self.size = (size, size)
        else:
            self.size = tuple(size)
        self.fill = fill

    def __call__(self, image):
        image = image.convert("RGB")
        source_width, source_height = image.size
        target_width, target_height = self.size

        if source_width <= 0 or source_height <= 0:
            raise ValueError("image width and height must be positive")

        scale = min(target_width / source_width, target_height / source_height)
        resized_width = max(1, int(round(source_width * scale)))
        resized_height = max(1, int(round(source_height * scale)))
        resized = image.resize((resized_width, resized_height), BILINEAR)

        canvas = Image.new("RGB", self.size, self.fill)
        left = (target_width - resized_width) // 2
        top = (target_height - resized_height) // 2
        canvas.paste(resized, (left, top))
        return canvas
