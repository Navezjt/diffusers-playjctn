from functools import partial
from torch import Tensor, no_grad, cat
from typing import List, Callable
from typing_extensions import TypeAlias
from PIL import Image
from diffusers.models import AutoencoderKL
from .approx_decoder import Decoder
import torch

LatentsToBCHW: TypeAlias = Callable[[Tensor], Tensor]
LatentsToPils: TypeAlias = Callable[[Tensor], List[Image.Image]]

@no_grad()
def approx_latents_to_pils_wd15(decoder: Decoder, latents: Tensor) -> Tensor:
  _, _, height, _ = latents.shape
  flat_channels_last: Tensor = latents.flatten(-2).transpose(-2,-1)
  decoded: Tensor = decoder.forward(flat_channels_last)
  unflat: Tensor = decoded.unflatten(-2, (height, -1))
  images: Tensor = unflat.round().clamp(0, 255).to(dtype=torch.uint8).cpu().numpy()
  pil_images: List[Image.Image] = [Image.fromarray(image) for image in images]
  return pil_images

def make_approx_latents_to_pils_wd15(decoder: Decoder) -> LatentsToPils:
  return partial(approx_latents_to_pils_wd15, decoder)

@no_grad()
def latents_to_bchw(vae: AutoencoderKL, latents: Tensor) -> Tensor:
  latents: Tensor = 1 / 0.18215 * latents

  if vae.device.type == 'mps' and latents.size(0) > 1:
    # batched VAE decode seems to be broken in MPS on recent kulinseth master
    images: Tensor = cat([vae.decode(sample_latents.to(vae.dtype)).sample for sample_latents in latents.split(1)])
  else:
    images: Tensor = vae.decode(latents.to(vae.dtype)).sample

  images: Tensor = (images / 2 + 0.5).clamp(0, 1)
  return images

def make_latents_to_bchw(vae: AutoencoderKL) -> LatentsToPils:
  return partial(latents_to_bchw, vae)

def latents_to_pils(latents_to_bchw: LatentsToBCHW, latents: Tensor) -> List[Image.Image]:
  images: Tensor = latents_to_bchw(latents)

  # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
  images = images.cpu().permute(0, 2, 3, 1).float().numpy()
  images = (images * 255).round().astype("uint8")

  pil_images: List[Image.Image] = [Image.fromarray(image) for image in images]
  return pil_images

def make_latents_to_pils(latents_to_bchw: LatentsToBCHW) -> LatentsToPils:
  return partial(latents_to_pils, latents_to_bchw)