from typing import TypeVar, Protocol, Generic, Iterable, Tuple, List, Generator, TypeAlias
from torch import FloatTensor
from torch import cat
from ..iteration.rle import run_length, RLEGeneric

SampleSpec = TypeVar('SampleSpec')

LatentBatcherOutput: TypeAlias = FloatTensor

class MakeLatents(Protocol, Generic[SampleSpec]):
  @staticmethod
  def __call__(spec: SampleSpec) -> FloatTensor: ...

class LatentBatcher(Generic[SampleSpec]):
  make_latents: MakeLatents[SampleSpec]
  def __init__(
    self,
    make_latents: MakeLatents[SampleSpec],
  ) -> None:
    self.make_latents = make_latents

  def generate(
    self,
    spec_chunks: Iterable[Tuple[SampleSpec, ...]],
  ) -> Generator[LatentBatcherOutput, None, None]:
    for chnk in spec_chunks:
      rle_specs: List[RLEGeneric[SampleSpec]] = list(run_length.encode(chnk))
      latents: List[FloatTensor] = [
        self.make_latents(rle_spec.element).expand(rle_spec.count, -1, -1, -1) for rle_spec in rle_specs
      ]
      yield cat(latents, dim=0)