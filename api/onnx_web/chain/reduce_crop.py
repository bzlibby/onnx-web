from logging import getLogger
from PIL import Image

from ..params import (
    ImageParams,
    Size,
    StageParams,
)
from ..utils import (
    ServerContext,
)

logger = getLogger(__name__)


def reduce_crop(
    ctx: ServerContext,
    _stage: StageParams,
    _params: ImageParams,
    source_image: Image.Image,
    *,
    origin: Size,
    size: Size,
    **kwargs,
) -> Image.Image:
    image = source_image.crop(
        (origin.width, origin.height, size.width, size.height))
    logger.info('created thumbnail with dimensions: %sx%s',
                image.width, image.height)
    return image