import os
from copy import deepcopy
from typing import Dict, List, Tuple

import cv2
import numpy as np
import onnxruntime as ort
import pooch
from jsonschema import validate
from PIL import Image
from PIL.Image import Image as PILImage

from .base import BaseSession


def get_preprocess_shape(oldh: int, oldw: int, long_side_length: int):
    scale = long_side_length * 1.0 / max(oldh, oldw)
    newh, neww = oldh * scale, oldw * scale
    neww = int(neww + 0.5)
    newh = int(newh + 0.5)

    return (newh, neww)


def apply_coords(coords: np.ndarray, original_size, target_length):
    old_h, old_w = original_size
    new_h, new_w = get_preprocess_shape(
        original_size[0], original_size[1], target_length
    )

    coords = deepcopy(coords).astype(float)
    coords[..., 0] = coords[..., 0] * (new_w / old_w)
    coords[..., 1] = coords[..., 1] * (new_h / old_h)

    return coords


def get_input_points(prompt):
    points = []
    labels = []

    for mark in prompt:
        if mark["type"] == "point":
            points.append(mark["data"])
            labels.append(mark["label"])
        elif mark["type"] == "rectangle":
            points.append([mark["data"][0], mark["data"][1]])
            points.append([mark["data"][2], mark["data"][3]])
            labels.append(2)
            labels.append(3)

    points, labels = np.array(points), np.array(labels)
    return points, labels


def transform_masks(masks, original_size, transform_matrix):
    output_masks = []

    for batch in range(masks.shape[0]):
        batch_masks = []
        for mask_id in range(masks.shape[1]):
            mask = masks[batch, mask_id]
            mask = cv2.warpAffine(
                mask,
                transform_matrix[:2],
                (original_size[1], original_size[0]),
                flags=cv2.INTER_LINEAR,
            )
            batch_masks.append(mask)
        output_masks.append(batch_masks)

    return np.array(output_masks)


class SamSession(BaseSession):
    """
    This class represents a session for the Sam model.

    Args:
        model_name (str): The name of the model.
        sess_opts (ort.SessionOptions): The session options.
        *args: Variable length argument list.
        **kwargs: Arbitrary keyword arguments.
    """

    def __init__(
        self,
        model_name: str,
        sess_opts: ort.SessionOptions,
        providers=None,
        *args,
        **kwargs,
    ):
        """
        Initialize a new SamSession with the given model name and session options.

        Args:
            model_name (str): The name of the model.
            sess_opts (ort.SessionOptions): The session options.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        self.model_name = model_name

        valid_providers = []
        available_providers = ort.get_available_providers()

        for provider in providers or []:
            if provider in available_providers:
                valid_providers.append(provider)
        else:
            valid_providers.extend(available_providers)

        paths = self.__class__.download_models(*args, **kwargs)
        self.encoder = ort.InferenceSession(
            str(paths[0]),
            providers=valid_providers,
            sess_options=sess_opts,
        )
        self.decoder = ort.InferenceSession(
            str(paths[1]),
            providers=valid_providers,
            sess_options=sess_opts,
        )

    def predict(
        self,
        img: PILImage,
        *args,
        **kwargs,
    ) -> List[PILImage]:
        """
        Predict masks for an input image.

        This function takes an image as input and performs various preprocessing steps on the image. It then runs the image through an encoder to obtain an image embedding. The function also takes input labels and points as additional arguments. It concatenates the input points and labels with padding and transforms them. It creates an empty mask input and an indicator for no mask. The function then passes the image embedding, point coordinates, point labels, mask input, and has mask input to a decoder. The decoder generates masks based on the input and returns them as a list of images.

        Parameters:
            img (PILImage): The input image.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.

        Returns:
            List[PILImage]: A list of masks generated by the decoder.
        """
        prompt = kwargs.get("sam_prompt", "{}")
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "label": {"type": "integer"},
                    "data": {
                        "type": "array",
                        "items": {"type": "number"},
                    },
                },
            },
        }

        validate(instance=prompt, schema=schema)

        target_size = 1024
        input_size = (684, 1024)
        encoder_input_name = self.encoder.get_inputs()[0].name

        img = img.convert("RGB")
        cv_image = np.array(img)
        original_size = cv_image.shape[:2]

        scale_x = input_size[1] / cv_image.shape[1]
        scale_y = input_size[0] / cv_image.shape[0]
        scale = min(scale_x, scale_y)

        transform_matrix = np.array(
            [
                [scale, 0, 0],
                [0, scale, 0],
                [0, 0, 1],
            ]
        )

        cv_image = cv2.warpAffine(
            cv_image,
            transform_matrix[:2],
            (input_size[1], input_size[0]),
            flags=cv2.INTER_LINEAR,
        )

        ## encoder

        encoder_inputs = {
            encoder_input_name: cv_image.astype(np.float32),
        }

        encoder_output = self.encoder.run(None, encoder_inputs)
        image_embedding = encoder_output[0]

        embedding = {
            "image_embedding": image_embedding,
            "original_size": original_size,
            "transform_matrix": transform_matrix,
        }

        ## decoder

        input_points, input_labels = get_input_points(prompt)
        onnx_coord = np.concatenate([input_points, np.array([[0.0, 0.0]])], axis=0)[
            None, :, :
        ]
        onnx_label = np.concatenate([input_labels, np.array([-1])], axis=0)[
            None, :
        ].astype(np.float32)
        onnx_coord = apply_coords(onnx_coord, input_size, target_size).astype(
            np.float32
        )

        onnx_coord = np.concatenate(
            [
                onnx_coord,
                np.ones((1, onnx_coord.shape[1], 1), dtype=np.float32),
            ],
            axis=2,
        )
        onnx_coord = np.matmul(onnx_coord, transform_matrix.T)
        onnx_coord = onnx_coord[:, :, :2].astype(np.float32)

        onnx_mask_input = np.zeros((1, 1, 256, 256), dtype=np.float32)
        onnx_has_mask_input = np.zeros(1, dtype=np.float32)

        decoder_inputs = {
            "image_embeddings": image_embedding,
            "point_coords": onnx_coord,
            "point_labels": onnx_label,
            "mask_input": onnx_mask_input,
            "has_mask_input": onnx_has_mask_input,
            "orig_im_size": np.array(input_size, dtype=np.float32),
        }

        masks, _, _ = self.decoder.run(None, decoder_inputs)
        inv_transform_matrix = np.linalg.inv(transform_matrix)
        masks = transform_masks(masks, original_size, inv_transform_matrix)

        mask = np.zeros((masks.shape[2], masks.shape[3], 3), dtype=np.uint8)
        for m in masks[0, :, :, :]:
            mask[m > 0.0] = [255, 255, 255]

        return [Image.fromarray(mask).convert("L")]

    @classmethod
    def download_models(cls, *args, **kwargs):
        """
        Class method to download ONNX model files.

        This method is responsible for downloading two ONNX model files from specified URLs and saving them locally. The downloaded files are saved with the naming convention 'name_encoder.onnx' and 'name_decoder.onnx', where 'name' is the value returned by the 'name' method.

        Parameters:
            cls: The class object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            tuple: A tuple containing the file paths of the downloaded encoder and decoder models.
        """
        model_name = kwargs.get("sam_model", "sam_vit_b_01ec64")
        quant = kwargs.get("sam_quant", False)

        fname_encoder = f"{model_name}.encoder.onnx"
        fname_decoder = f"{model_name}.decoder.onnx"

        if quant:
            fname_encoder = f"{model_name}.encoder.quant.onnx"
            fname_decoder = f"{model_name}.decoder.quant.onnx"

        pooch.retrieve(
            f"https://github.com/danielgatis/rembg/releases/download/v0.0.0/{fname_encoder}",
            None,
            fname=fname_encoder,
            path=cls.u2net_home(*args, **kwargs),
            progressbar=True,
        )

        pooch.retrieve(
            f"https://github.com/danielgatis/rembg/releases/download/v0.0.0/{fname_decoder}",
            None,
            fname=fname_decoder,
            path=cls.u2net_home(*args, **kwargs),
            progressbar=True,
        )

        if fname_encoder == "sam_vit_h_4b8939.encoder.onnx" and not os.path.exists(
            os.path.join(
                cls.u2net_home(*args, **kwargs), "sam_vit_h_4b8939.encoder_data.bin"
            )
        ):
            content = bytearray()

            for i in range(1, 4):
                pooch.retrieve(
                    f"https://github.com/danielgatis/rembg/releases/download/v0.0.0/sam_vit_h_4b8939.encoder_data.{i}.bin",
                    None,
                    fname=f"sam_vit_h_4b8939.encoder_data.{i}.bin",
                    path=cls.u2net_home(*args, **kwargs),
                    progressbar=True,
                )

                fbin = os.path.join(
                    cls.u2net_home(*args, **kwargs),
                    f"sam_vit_h_4b8939.encoder_data.{i}.bin",
                )
                content.extend(open(fbin, "rb").read())
                os.remove(fbin)

            with open(
                os.path.join(
                    cls.u2net_home(*args, **kwargs),
                    "sam_vit_h_4b8939.encoder_data.bin",
                ),
                "wb",
            ) as fp:
                fp.write(content)

        return (
            os.path.join(cls.u2net_home(*args, **kwargs), fname_encoder),
            os.path.join(cls.u2net_home(*args, **kwargs), fname_decoder),
        )

    @classmethod
    def name(cls, *args, **kwargs):
        """
        Class method to return a string value.

        This method returns the string value 'sam'.

        Parameters:
            cls: The class object.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            str: The string value 'sam'.
        """
        return "sam"
