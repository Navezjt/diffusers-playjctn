from os import path
from torch import Tensor, load
from torch.nn import Module, Linear
from typing import OrderedDict
from .approx_decoder_ckpt import DecoderCkpt, approx_decoder_ckpt_filenames
import torch

class Decoder(Module):
  lin: Linear
  def __init__(self) -> None:
    super().__init__()
    self.lin = Linear(4, 3, True)
  
  def forward(self, input: Tensor) -> Tensor:
    output: Tensor = self.lin(input)
    return output

def get_approx_decoder(decoder_ckpt: DecoderCkpt, device: torch.device = torch.device('cpu')) -> Decoder:  
  approx_decoder_ckpt: str = path.join(path.dirname(__file__), approx_decoder_ckpt_filenames[decoder_ckpt])
  approx_state: OrderedDict[str, Tensor] = load(approx_decoder_ckpt, map_location=device, weights_only=True)
  approx_decoder = Decoder()
  approx_decoder.load_state_dict(approx_state)
  return approx_decoder.eval().to(device)