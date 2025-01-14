import warnings
from argparse import ArgumentParser
from logging import getLogger
from os import makedirs, path
from sys import exit
from typing import Any, Dict, List, Union

from jsonschema import ValidationError, validate
from onnx import load_model, save_model
from transformers import CLIPTokenizer

from ..constants import ONNX_MODEL, ONNX_WEIGHTS
from ..server.plugin import load_plugins
from ..utils import load_config
from .archive import convert_extract_archive
from .client import add_model_source, fetch_model
from .client.huggingface import HuggingfaceClient
from .correction.gfpgan import convert_correction_gfpgan
from .diffusion.control import convert_diffusion_control
from .diffusion.diffusion import convert_diffusion_diffusers
from .diffusion.diffusion_xl import convert_diffusion_diffusers_xl
from .diffusion.lora import blend_loras
from .diffusion.textual_inversion import blend_textual_inversions
from .upscaling.bsrgan import convert_upscaling_bsrgan
from .upscaling.resrgan import convert_upscale_resrgan
from .upscaling.swinir import convert_upscaling_swinir
from .utils import (
    DEFAULT_OPSET,
    ConversionContext,
    fix_diffusion_name,
    source_format,
    tuple_to_correction,
    tuple_to_diffusion,
    tuple_to_source,
    tuple_to_upscaling,
)

# suppress common but harmless warnings, https://github.com/ssube/onnx-web/issues/75
warnings.filterwarnings(
    "ignore", ".*The shape inference of prim::Constant type is missing.*"
)
warnings.filterwarnings("ignore", ".*Only steps=1 can be constant folded.*")
warnings.filterwarnings(
    "ignore",
    ".*Converting a tensor to a Python boolean might cause the trace to be incorrect.*",
)

logger = getLogger(__name__)

ModelDict = Dict[str, Union[float, int, str]]
Models = Dict[str, List[ModelDict]]

model_converters: Dict[str, Any] = {
    "archive": convert_extract_archive,
    "img2img": convert_diffusion_diffusers,
    "img2img-sdxl": convert_diffusion_diffusers_xl,
    "inpaint": convert_diffusion_diffusers,
    "txt2img": convert_diffusion_diffusers,
    "txt2img-sdxl": convert_diffusion_diffusers_xl,
}

# recommended models
base_models: Models = {
    "diffusion": [
        # v1.x
        (
            "stable-diffusion-onnx-v1-5",
            HuggingfaceClient.protocol + "runwayml/stable-diffusion-v1-5",
        ),
        (
            "stable-diffusion-onnx-v1-inpainting",
            HuggingfaceClient.protocol + "runwayml/stable-diffusion-inpainting",
        ),
        (
            "upscaling-stable-diffusion-x4",
            HuggingfaceClient.protocol + "stabilityai/stable-diffusion-x4-upscaler",
            True,
        ),
    ],
    "correction": [
        (
            "correction-gfpgan-v1-3",
            "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.3.pth",
            4,
        ),
        (
            "correction-codeformer",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/codeformer.pth",
            1,
        ),
    ],
    "upscaling": [
        (
            "upscaling-real-esrgan-x2-plus",
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
            2,
        ),
        (
            "upscaling-real-esrgan-x4-plus",
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
            4,
        ),
        (
            "upscaling-real-esrgan-x4-v3",
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
            4,
        ),
        {
            "model": "swinir",
            "name": "upscaling-swinir-classical-x4",
            "source": "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/001_classicalSR_DF2K_s64w8_SwinIR-M_x4.pth",
            "scale": 4,
        },
        {
            "model": "swinir",
            "name": "upscaling-swinir-real-large-x4",
            "source": "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/003_realSR_BSRGAN_DFOWMFC_s64w8_SwinIR-L_x4_GAN.pth",
            "scale": 4,
        },
        {
            "model": "bsrgan",
            "name": "upscaling-bsrgan-x4",
            "source": "https://github.com/cszn/KAIR/releases/download/v1.0/BSRGAN.pth",
            "scale": 4,
        },
        {
            "model": "bsrgan",
            "name": "upscaling-bsrgan-x2",
            "source": "https://github.com/cszn/KAIR/releases/download/v1.0/BSRGANx2.pth",
            "scale": 2,
        },
    ],
    # download only
    "sources": [
        # CodeFormer: no ONNX yet
        (
            "detection-resnet50-final",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/detection_Resnet50_Final.pth",
        ),
        (
            "detection-mobilenet025-final",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/detection_mobilenet0.25_Final.pth",
        ),
        (
            "detection-yolo-v5-l",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/yolov5l-face.pth",
        ),
        (
            "detection-yolo-v5-n",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/yolov5n-face.pth",
        ),
        (
            "parsing-bisenet",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/parsing_bisenet.pth",
        ),
        (
            "parsing-parsenet",
            "https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/parsing_parsenet.pth",
        ),
        # ControlNets: already converted
        {
            "dest": "control",
            "format": "onnx",
            "name": "canny",
            "source": "https://huggingface.co/ForserX/sd-controlnet-canny-onnx/resolve/main/model.onnx",
        },
        {
            "dest": "control",
            "format": "onnx",
            "name": "depth",
            "source": "https://huggingface.co/ForserX/sd-controlnet-depth-onnx/resolve/main/model.onnx",
        },
        {
            "dest": "control",
            "format": "onnx",
            "name": "hed",
            "source": "https://huggingface.co/ForserX/sd-controlnet-hed-onnx/resolve/main/model.onnx",
        },
        {
            "dest": "control",
            "format": "onnx",
            "name": "mlsd",
            "source": "https://huggingface.co/ForserX/sd-controlnet-mlsd-onnx/resolve/main/model.onnx",
        },
        {
            "dest": "control",
            "format": "onnx",
            "name": "normal",
            "source": "https://huggingface.co/ForserX/sd-controlnet-normal-onnx/resolve/main/model.onnx",
        },
        {
            "dest": "control",
            "format": "onnx",
            "name": "openpose",
            "source": "https://huggingface.co/ForserX/sd-controlnet-openpose-onnx/resolve/main/model.onnx",
        },
        {
            "dest": "control",
            "format": "onnx",
            "name": "seg",
            "source": "https://huggingface.co/ForserX/sd-controlnet-seg-onnx/resolve/main/model.onnx",
        },
    ],
}


def convert_model_source(conversion: ConversionContext, model):
    model_format = source_format(model)
    name = model["name"]
    source = model["source"]

    dest_path = None
    if "dest" in model:
        dest_path = path.join(conversion.model_path, model["dest"])

    dest = fetch_model(conversion, name, source, format=model_format, dest=dest_path)
    logger.info("finished downloading source: %s -> %s", source, dest)


def convert_model_network(conversion: ConversionContext, model):
    model_format = source_format(model)
    model_type = model["type"]
    name = model["name"]
    source = model["source"]

    if model_type == "control":
        dest = fetch_model(
            conversion,
            name,
            source,
            format=model_format,
        )

        convert_diffusion_control(
            conversion,
            model,
            dest,
            path.join(conversion.model_path, model_type, name),
        )
    else:
        model = model.get("model", None)
        dest = fetch_model(
            conversion,
            name,
            source,
            dest=path.join(conversion.model_path, model_type),
            format=model_format,
            embeds=(model_type == "inversion" and model == "concept"),
        )

    logger.info("finished downloading network: %s -> %s", source, dest)


def convert_model_diffusion(conversion: ConversionContext, model):
    # fix up entries with missing prefixes
    name = fix_diffusion_name(model["name"])
    if name != model["name"]:
        # update the model in-memory if the name changed
        model["name"] = name

    model_format = source_format(model)

    pipeline = model.get("pipeline", "txt2img")
    converter = model_converters.get(pipeline)
    converted, dest = converter(
        conversion,
        model,
        model_format,
    )

    # make sure blending only happens once, not every run
    if converted:
        # keep track of which models have been blended
        blend_models = {}

        inversion_dest = path.join(conversion.model_path, "inversion")
        lora_dest = path.join(conversion.model_path, "lora")

        for inversion in model.get("inversions", []):
            if "text_encoder" not in blend_models:
                blend_models["text_encoder"] = load_model(
                    path.join(
                        dest,
                        "text_encoder",
                        ONNX_MODEL,
                    )
                )

            if "tokenizer" not in blend_models:
                blend_models["tokenizer"] = CLIPTokenizer.from_pretrained(
                    dest,
                    subfolder="tokenizer",
                )

            inversion_name = inversion["name"]
            inversion_source = inversion["source"]
            inversion_format = inversion.get("format", None)
            inversion_source = fetch_model(
                conversion,
                inversion_name,
                inversion_source,
                dest=inversion_dest,
            )
            inversion_token = inversion.get("token", inversion_name)
            inversion_weight = inversion.get("weight", 1.0)

            blend_textual_inversions(
                conversion,
                blend_models["text_encoder"],
                blend_models["tokenizer"],
                [
                    (
                        inversion_source,
                        inversion_weight,
                        inversion_token,
                        inversion_format,
                    )
                ],
            )

        for lora in model.get("loras", []):
            if "text_encoder" not in blend_models:
                blend_models["text_encoder"] = load_model(
                    path.join(
                        dest,
                        "text_encoder",
                        ONNX_MODEL,
                    )
                )

            if "unet" not in blend_models:
                blend_models["unet"] = load_model(path.join(dest, "unet", ONNX_MODEL))

            # load models if not loaded yet
            lora_name = lora["name"]
            lora_source = lora["source"]
            lora_source = fetch_model(
                conversion,
                f"{name}-lora-{lora_name}",
                lora_source,
                dest=lora_dest,
            )
            lora_weight = lora.get("weight", 1.0)

            blend_loras(
                conversion,
                blend_models["text_encoder"],
                [(lora_source, lora_weight)],
                "text_encoder",
            )

            blend_loras(
                conversion,
                blend_models["unet"],
                [(lora_source, lora_weight)],
                "unet",
            )

        if "tokenizer" in blend_models:
            dest_path = path.join(dest, "tokenizer")
            logger.debug("saving blended tokenizer to %s", dest_path)
            blend_models["tokenizer"].save_pretrained(dest_path)

        for name in ["text_encoder", "unet"]:
            if name in blend_models:
                dest_path = path.join(dest, name, ONNX_MODEL)
                logger.debug("saving blended %s model to %s", name, dest_path)
                save_model(
                    blend_models[name],
                    dest_path,
                    save_as_external_data=True,
                    all_tensors_to_one_file=True,
                    location=ONNX_WEIGHTS,
                )


def convert_model_upscaling(conversion: ConversionContext, model):
    model_format = source_format(model)
    name = model["name"]

    source = fetch_model(conversion, name, model["source"], format=model_format)
    model_type = model.get("model", "resrgan")
    if model_type == "bsrgan":
        convert_upscaling_bsrgan(conversion, model, source)
    elif model_type == "resrgan":
        convert_upscale_resrgan(conversion, model, source)
    elif model_type == "swinir":
        convert_upscaling_swinir(conversion, model, source)
    else:
        logger.error("unknown upscaling model type %s for %s", model_type, name)
        raise ValueError(name)


def convert_model_correction(conversion: ConversionContext, model):
    model_format = source_format(model)
    name = model["name"]
    source = fetch_model(conversion, name, model["source"], format=model_format)
    model_type = model.get("model", "gfpgan")
    if model_type == "gfpgan":
        convert_correction_gfpgan(conversion, model, source)
    else:
        logger.error("unknown correction model type %s for %s", model_type, name)
        raise ValueError(name)


def convert_models(conversion: ConversionContext, args, models: Models):
    model_errors = []

    if args.sources and "sources" in models:
        for model in models.get("sources", []):
            model = tuple_to_source(model)
            name = model.get("name")

            if name in args.skip:
                logger.info("skipping source: %s", name)
            else:
                try:
                    convert_model_source(conversion, model)
                except Exception:
                    logger.exception("error fetching source %s", name)
                    model_errors.append(name)

    if args.networks and "networks" in models:
        for model in models.get("networks", []):
            name = model["name"]

            if name in args.skip:
                logger.info("skipping network: %s", name)
            else:
                try:
                    convert_model_network(conversion, model)
                except Exception:
                    logger.exception("error fetching network %s", name)
                    model_errors.append(name)

    if args.diffusion and "diffusion" in models:
        for model in models.get("diffusion", []):
            model = tuple_to_diffusion(model)
            name = model.get("name")

            if name in args.skip:
                logger.info("skipping model: %s", name)
            else:
                try:
                    convert_model_diffusion(conversion, model)
                except Exception:
                    logger.exception(
                        "error converting diffusion model %s",
                        name,
                    )
                    model_errors.append(name)

    if args.upscaling and "upscaling" in models:
        for model in models.get("upscaling", []):
            model = tuple_to_upscaling(model)
            name = model.get("name")

            if name in args.skip:
                logger.info("skipping model: %s", name)
            else:
                try:
                    convert_model_upscaling(conversion, model)
                except Exception:
                    logger.exception(
                        "error converting upscaling model %s",
                        name,
                    )
                    model_errors.append(name)

    if args.correction and "correction" in models:
        for model in models.get("correction", []):
            model = tuple_to_correction(model)
            name = model.get("name")

            if name in args.skip:
                logger.info("skipping model: %s", name)
            else:
                try:
                    convert_model_correction(conversion, model)
                except Exception:
                    logger.exception(
                        "error converting correction model %s",
                        name,
                    )
                    model_errors.append(name)

    if len(model_errors) > 0:
        logger.error("error while converting models: %s", model_errors)


def register_plugins(conversion: ConversionContext):
    logger.info("loading conversion plugins")
    exports = load_plugins(conversion)

    for proto, client in exports.clients:
        try:
            add_model_source(proto, client)
        except Exception:
            logger.exception("error loading client for protocol: %s", proto)

    # TODO: add converters


def main(args=None) -> int:
    parser = ArgumentParser(
        prog="onnx-web model converter", description="convert checkpoint models to ONNX"
    )

    # model groups
    parser.add_argument("--base", action="store_true", default=True)
    parser.add_argument("--networks", action="store_true", default=True)
    parser.add_argument("--sources", action="store_true", default=True)
    parser.add_argument("--correction", action="store_true", default=False)
    parser.add_argument("--diffusion", action="store_true", default=False)
    parser.add_argument("--upscaling", action="store_true", default=False)

    # extra models
    parser.add_argument("--extras", nargs="*", type=str, default=[])
    parser.add_argument("--prune", nargs="*", type=str, default=[])
    parser.add_argument("--skip", nargs="*", type=str, default=[])

    # export options
    parser.add_argument(
        "--half",
        action="store_true",
        default=False,
        help="Export models for half precision, smaller and faster on most GPUs.",
    )
    parser.add_argument(
        "--opset",
        default=DEFAULT_OPSET,
        type=int,
        help="The version of the ONNX operator set to use.",
    )
    parser.add_argument(
        "--token",
        type=str,
        help="HuggingFace token with read permissions for downloading models.",
    )

    args = parser.parse_args(args=args)
    logger.info("CLI arguments: %s", args)

    server = ConversionContext.from_environ()
    server.half = args.half or server.has_optimization("onnx-fp16")
    server.opset = args.opset
    server.token = args.token

    register_plugins(server)

    logger.info(
        "converting models into %s using %s", server.model_path, server.training_device
    )

    if not path.exists(server.model_path):
        logger.info("model path does not existing, creating: %s", server.model_path)
        makedirs(server.model_path)

    if args.base:
        logger.info("converting base models")
        convert_models(server, args, base_models)

    extras = []
    extras.extend(server.extra_models)
    extras.extend(args.extras)
    extras = list(set(extras))
    extras.sort()
    logger.debug("loading extra files: %s", extras)

    extra_schema = load_config("./schemas/extras.yaml")

    for file in extras:
        if file is not None and file != "":
            logger.info("loading extra models from %s", file)
            try:
                data = load_config(file)
                logger.debug("validating extras file %s", data)
                try:
                    validate(data, extra_schema)
                    logger.info("converting extra models")
                    convert_models(server, args, data)
                except ValidationError:
                    logger.exception("invalid data in extras file")
            except Exception:
                logger.exception("error converting extra models")

    return 0


if __name__ == "__main__":
    exit(main())
