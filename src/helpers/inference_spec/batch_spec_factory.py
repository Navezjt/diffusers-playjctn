from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Protocol, Generator, List, Iterable, NamedTuple, Optional, Generic, TypeVar
from dataclasses import dataclass
import torch
from torch import FloatTensor, Generator as TorchGenerator
from ..get_seed import get_seed
from ..device import DeviceType
from itertools import islice
from ..iteration.chunk import chunk
from ..iteration.rle import run_length, RLEGeneric

T = TypeVar('T')

class AbstractSeedGenerator(Protocol):
  def generate() -> Generator[int]: ...

class RandomSeedSequence(AbstractSeedGenerator):
  def generate() -> Generator[int]:
    while True:
      yield get_seed()

class FixedSeedSequence(AbstractSeedGenerator):
  seeds: Iterable[int]
  def __init__(
    self,
    seeds: Iterable[int],
  ) -> None:
    super().__init__()
    self.seeds = seeds

  def generate(self) -> Generator[int]:
    return self.seeds.__iter__()
    # return (seed for seed in self.seeds)

class AbstractLatentsGenerator:
  generator: TorchGenerator
  device: DeviceType
  def __init__(
    self,
    batch_size: int,
    device: DeviceType = torch.device('cpu')
  ):
    self.batch_size = batch_size
    self.generator = TorchGenerator(device='cpu')
    self.device = device

  @abstractmethod
  def generate(self) -> Generator[FloatTensor]: ...

class SeedsTaken(NamedTuple):
  seed: int
  taken: int
  next_spec: Optional[AbstractSeedSpec]

@dataclass
class AbstractSeedSpec(ABC):
  # seed: int
  @abstractmethod
  def take(self, want: int) -> SeedsTaken: ...

class SequenceSeedSpec(AbstractSeedSpec):
  seeds: Iterable[int]
  def __init__(
    self,
    seeds: Iterable[int],
  ) -> None:
    super().__init__()
    self.seeds = seeds

  def take(self, want: int) -> SeedsTaken:

    assert want > 0 and self.remaining > 0
    next_spec: Optional[AbstractSeedSpec] = FiniteSeedSpec(
      seed=self.seed,
      taken=want,
      remaining=self.remaining-want,
    ) if want < self.remaining else None
    return SeedsTaken(seeds=self.seed, taken=want, next_spec=self)

class FiniteSeedSpec(AbstractSeedSpec):
  remaining: int
  def __init__(
    self,
    seed: int,
    remaining=1,
  ) -> None:
    super().__init__(seed)
    self.remaining = remaining

  def take(self, want: int) -> SeedsTaken:
    assert want > 0 and self.remaining > 0
    next_spec: Optional[AbstractSeedSpec] = FiniteSeedSpec(
      seed=self.seed,
      taken=want,
      remaining=self.remaining-want,
    ) if want < self.remaining else None
    return SeedsTaken(seeds=self.seed, taken=want, next_spec=next_spec)

class InfiniteSeedSpec(AbstractSeedSpec):
  def take(self, want: int) -> SeedsTaken:
    return SeedsTaken(seeds=self.seed, taken=want, next_spec=self)

AbstractSeedSpec.register(FiniteSeedSpec)
AbstractSeedSpec.register(InfiniteSeedSpec)

class MakeLatents(Protocol, Generic[T]):
  @staticmethod
  def __call__(spec: T, repeat: int = 1) -> FloatTensor: ...

class AreEqual(Protocol, Generic[T]):
  @staticmethod
  def __call__(left: T, right: T) -> bool: ...

@dataclass
class MakeLatentsFromSeedSpec:
  seed: int

class MakeLatentsFromSeed(MakeLatents[MakeLatentsFromSeedSpec]):
  @staticmethod
  def make(spec: MakeLatentsFromSeedSpec) -> FloatTensor:
    pass

class LatentsGenerator(Generic[T]):
  batch_size: int
  specs: Iterable[T]
  make_latents: MakeLatents[T]
  # are_equal: AreEqual[T]
  def __init__(
    self,
    batch_size: int,
    specs: Iterable[T],
    make_latents: MakeLatents[T],
    # are_equal: AreEqual[T]
  ) -> None:
    self.batch_size = batch_size
    self.specs = specs
    self.make_latents = make_latents
    # self.are_equal = are_equal

  def generate(self) -> Generator[FloatTensor]:
    for chnk in chunk(self.specs, self.batch_size):
      rle_specs: List[RLEGeneric[T]] = list(run_length.encode(chnk))
      latents: List[FloatTensor] = [
        self.make_latents(rle_spec.element, rle_spec.count) for rle_spec in rle_specs
      ]
      yield torch.cat(latents, dim=0)
      
    # iter = self.seed_specs.__iter__()
    # next_spec: Optional[AbstractSeedSpec] = None
    # seeds_acc: int = 0
    # latents_acc: List[FloatTensor] = []
    # while spec := next_spec or next(iter, None):
    #   seed, taken, next_spec = spec.take(self.batch_size)
    #   latents: FloatTensor = self.make_latents(seed).unsqueeze(0).expand(taken)
    #   seeds_acc += taken
    #   assert seeds_acc <= self.batch_size
    #   if seeds_acc == self.batch_size:
    #     if latents_acc:
    #       latents_acc.append(latents)
    #       yield torch.stack(latents_acc)
    #       latents_acc.clear()
    #     else:
    #       yield latents
    #     seeds_acc = 0
    #   else:
    #     latents_acc.append(latents)
    # if latents_acc:
    #   yield torch.stack(latents_acc)



class LatentSampleShape(NamedTuple):
  channels: int
  # dimensions of *latents*, not pixels
  height: int
  width: int

# class AbstractBatchSpecFactory(ABC):
#   def 

@dataclass
class BatchSpec:
  latents: FloatTensor
  seeds: List[int]

class AbstractBatchSpecFactory(ABC):
  batch_size: int
  generator: TorchGenerator
  device: DeviceType
  def __init__(
    self,
    batch_size: int,
    device: DeviceType = torch.device('cpu')
  ):
    self.batch_size = batch_size
    self.generator = TorchGenerator(device='cpu')

  @abstractmethod
  def generate() -> Generator[BatchSpec]: ...

class BasicBatchSpecFactory(AbstractBatchSpecFactory):
  def generate() -> Generator[BatchSpec]:
    pass

class BatchSpecFactory:
  def __init__(self) -> None:
    pass