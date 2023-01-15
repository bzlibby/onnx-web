from numpy import random
from PIL import Image, ImageChops, ImageFilter
from typing import Tuple

import numpy as np


def mask_filter_none(mask_image: Image, dims: Tuple[int, int], origin: Tuple[int, int], fill='white') -> Image:
    width, height = dims

    noise = Image.new('RGB', (width, height), fill)
    noise.paste(mask_image, origin)

    return noise


def mask_filter_gaussian(mask_image: Image, dims: Tuple[int, int], origin: Tuple[int, int], rounds=3) -> Image:
    '''
    Gaussian blur, source image centered on white canvas.
    '''
    noise = mask_filter_none(mask_image, dims, origin)

    for i in range(rounds):
        blur = noise.filter(ImageFilter.GaussianBlur(5))
        noise = ImageChops.screen(noise, blur)

    return noise


def noise_source_fill(source_image: Image, dims: Tuple[int, int], origin: Tuple[int, int], fill='white') -> Image:
    '''
    Identity transform, source image centered on white canvas.
    '''
    width, height = dims

    noise = Image.new('RGB', (width, height), fill)
    noise.paste(source_image, origin)

    return noise


def noise_source_gaussian(source_image: Image, dims: Tuple[int, int], origin: Tuple[int, int], rounds=3) -> Image:
    '''
    Gaussian blur, source image centered on white canvas.
    '''
    noise = noise_source_uniform(source_image, dims, origin)
    noise.paste(source_image, origin)

    for i in range(rounds):
        noise = noise.filter(ImageFilter.GaussianBlur(5))

    return noise


def noise_source_uniform(source_image: Image, dims: Tuple[int, int], origin: Tuple[int, int]) -> Image:
    width, height = dims
    size = width * height

    noise_r = random.uniform(0, 256, size=size)
    noise_g = random.uniform(0, 256, size=size)
    noise_b = random.uniform(0, 256, size=size)

    noise = Image.new('RGB', (width, height))

    for x in range(width):
        for y in range(height):
            i = x * y
            noise.putpixel((x, y), (
                int(noise_r[i]),
                int(noise_g[i]),
                int(noise_b[i])
            ))

    return noise


def noise_source_normal(source_image: Image, dims: Tuple[int, int], origin: Tuple[int, int]) -> Image:
    width, height = dims
    size = width * height

    noise_r = random.normal(128, 32, size=size)
    noise_g = random.normal(128, 32, size=size)
    noise_b = random.normal(128, 32, size=size)

    noise = Image.new('RGB', (width, height))

    for x in range(width):
        for y in range(height):
            i = x * y
            noise.putpixel((x, y), (
                int(noise_r[i]),
                int(noise_g[i]),
                int(noise_b[i])
            ))

    return noise


def noise_source_histogram(source_image: Image, dims: Tuple[int, int], origin: Tuple[int, int]) -> Image:
    r, g, b = source_image.split()
    width, height = dims
    size = width * height

    hist_r = r.histogram()
    hist_g = g.histogram()
    hist_b = b.histogram()

    noise_r = random.choice(256, p=np.divide(
        np.copy(hist_r), np.sum(hist_r)), size=size)
    noise_g = random.choice(256, p=np.divide(
        np.copy(hist_g), np.sum(hist_g)), size=size)
    noise_b = random.choice(256, p=np.divide(
        np.copy(hist_b), np.sum(hist_b)), size=size)

    noise = Image.new('RGB', (width, height))

    for x in range(width):
        for y in range(height):
            i = x * y
            noise.putpixel((x, y), (
                noise_r[i],
                noise_g[i],
                noise_b[i]
            ))

    return noise


# based on https://github.com/AUTOMATIC1111/stable-diffusion-webui/blob/master/scripts/outpainting_mk_2.py#L175-L232
def expand_image(
        source_image: Image,
        mask_image: Image,
        expand_by: Tuple[int, int, int, int],
        fill='white',
        noise_source=noise_source_histogram,
        mask_filter=mask_filter_gaussian,
):
    left, right, top, bottom = expand_by

    full_width = left + source_image.width + right
    full_height = top + source_image.height + bottom

    dims = (full_width, full_height)
    origin = (top, left)

    full_source = Image.new('RGB', dims, fill)
    full_source.paste(source_image, origin)

    full_mask = mask_filter(mask_image, dims, origin)
    full_noise = noise_source(source_image, dims, origin)
    full_source = Image.composite(full_noise, full_source, full_mask.convert('L'))

    return (full_source, full_mask, full_noise, (full_width, full_height))
