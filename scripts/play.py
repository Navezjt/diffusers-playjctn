import os, fnmatch
from diffusers.models.autoencoder_kl import AutoencoderKLOutput

# monkey-patch _randn to use CPU random before k-diffusion uses it
from helpers.brownian_tree_mps_fix import reassuring_message
from helpers.cumsum_mps_fix import reassuring_message as reassuring_message_2
from helpers.device import DeviceLiteral, get_device_type
from helpers.diffusers_denoiser import DiffusersSDDenoiser, DiffusersSD2Denoiser
from helpers.batch_denoiser import Denoiser, BatchDenoiserFactory
from helpers.encode_img import EncodeImg, make_encode_img
from helpers.file_naming import get_sample_stem
from helpers.inference_spec.latent_maker_img_encode_strategy import ImgEncodeLatentMaker
from helpers.load_img import load_img
from helpers.log_intermediates import LogIntermediates, LogIntermediatesFactory, make_log_intermediates_factory
from helpers.sample_interpolation.interp_strategy import InterpStrategy, InterpProto
from helpers.sample_interpolation.slerp import slerp
from helpers.schedules import KarrasScheduleParams, KarrasScheduleTemplate, get_template_schedule
print(reassuring_message) # avoid "unused" import :P
print(reassuring_message_2)

import torch
from torch import Tensor, FloatTensor, BoolTensor, LongTensor, no_grad, zeros, tensor, arange, linspace, lerp
from diffusers.models import UNet2DConditionModel, AutoencoderKL
from diffusers.models.cross_attention import AttnProcessor2_0
from diffusers.utils.import_utils import is_xformers_available
from k_diffusion.sampling import BrownianTreeNoiseSampler, get_sigmas_karras, sample_dpmpp_2m

from helpers.attention.mode import AttentionMode
from helpers.attention.multi_head_attention.to_mha import to_mha
from helpers.attention.set_chunked_attn import make_set_chunked_attn
from helpers.attention.tap_attn import TapAttn, tap_attn_to_tap_module
from helpers.attention.replace_attn import replace_attn_to_tap_module
from helpers.tap.tap_module import TapModule
from helpers.schedule_params import get_alphas, get_alphas_cumprod, get_betas, quantize_to
from helpers.get_seed import get_seed
from helpers.latents_to_pils import LatentsToPils, LatentsToBCHW, make_latents_to_pils, make_latents_to_bchw, make_approx_latents_to_pils_wd15
from helpers.approx_decoder import Decoder, get_approx_decoder
from helpers.approx_decoder_ckpt import DecoderCkpt
from helpers.embed_text_types import Embed, EmbeddingAndMask
from helpers.embed_text import ClipCheckpoint, ClipImplementation, get_embedder
from helpers.model_db import get_model_needs, ModelNeeds, get_approx_decoder_ckpt
from helpers.inference_spec.sample_spec import SampleSpec
from helpers.inference_spec.latent_spec import SeedSpec, ImgEncodeSpec
from helpers.inference_spec.latents_shape import LatentsShape
from helpers.inference_spec.cond_spec import ConditionSpec, SingleCondition, MultiCond, WeightedPrompt, CFG, Prompt, BasicPrompt, InterPrompt
from helpers.inference_spec.execution_plan_batcher import ExecutionPlanBatcher, BatchSpecGeneric
from helpers.inference_spec.execution_plan import CondInterp, ExecutionPlan, make_execution_plan
from helpers.inference_spec.batch_latent_maker import BatchLatentMaker
from helpers.inference_spec.latent_maker import LatentMaker, MakeLatentsStrategy
from helpers.inference_spec.latent_maker_seed_strategy import SeedLatentMaker
from helpers.sample_interpolation.make_in_between import make_inbetween
from helpers.sample_interpolation.intersperse_linspace import intersperse_linspace
from itertools import chain, repeat, cycle, pairwise
from easing_functions import CubicEaseInOut

from typing import List, Generator, Iterable, Optional, Callable, Tuple, Dict
from PIL import Image
import time
import numpy as np
from einops import repeat as einops_repeat

half = True

# hakurei/waifu-diffusion
# can refer to both 1.3 and 1.4, depending on commit
# latest main points at 1.4
# latest fp16 points at 1.3
# when True: we make use of this mismatch, to deliberately select 1.3 by picking fp16 revision
wd_prefer_1_3 = True
# wd_prefer_1_3 = False

revision=None
torch_dtype=None
if half:
  revision='fp16'
  torch_dtype=torch.float16
device_type: DeviceLiteral = get_device_type()
device = torch.device(device_type)

model_name = (
  # 'CompVis/stable-diffusion-v1-3'
  'CompVis/stable-diffusion-v1-4'
  # 'hakurei/waifu-diffusion'
  # 'waifu-diffusion/wd-1-5-beta'
  # 'waifu-diffusion/wd-1-5-beta2'
  # 'runwayml/stable-diffusion-v1-5'
  # 'stabilityai/stable-diffusion-2'
  # 'stabilityai/stable-diffusion-2-1'
  # 'stabilityai/stable-diffusion-2-base'
  # 'stabilityai/stable-diffusion-2-1-base'
)

model_needs: ModelNeeds = get_model_needs(model_name, torch.float32 if torch_dtype is None else torch_dtype)

is_768 = model_needs.is_768
needs_vparam = model_needs.needs_vparam
if model_name == 'hakurei/waifu-diffusion' and wd_prefer_1_3:
  needs_penultimate_clip_hidden_state = False
else:
  needs_penultimate_clip_hidden_state = model_needs.needs_penultimate_clip_hidden_state
upcast_attention = model_needs.needs_upcast_attention

match model_name:
  # WD1.4fp32 = { model_name: 'hakurei/waifu-diffusion', revision=None }
  # WD1.4fp16 = no (official) distribution
  # WD1.3fp32 = { model_name: 'hakurei/waifu-diffusion', revision='a20c448ad20e797115c379fa2418c5ad64a4cd5c' }
  # WD1.3fp16 = { model_name: 'hakurei/waifu-diffusion', revision='fp16' }
  case 'hakurei/waifu-diffusion':
    if wd_prefer_1_3:
      if not half:
        # prefer final WD1.3 fp32 commit over default branch
        revision = 'a20c448ad20e797115c379fa2418c5ad64a4cd5c'
    else:
      # there's no (official WD1.3fp16), so download 32-bit weights either way (we can cast dtype afterward)
      revision = None
  # WD 1.5beta only has fp16 revisions for CompVis
  case 'waifu-diffusion/wd-1-5-beta' | 'waifu-diffusion/wd-1-5-beta2':
    revision = None
unet: UNet2DConditionModel = UNet2DConditionModel.from_pretrained(
  model_name,
  subfolder='unet',
  revision=revision,
  torch_dtype=torch_dtype,
  upcast_attention=upcast_attention,
).to(device).eval()

attn_mode = AttentionMode.ScaledDPAttn
match(attn_mode):
  case AttentionMode.Standard: pass
  case AttentionMode.Chunked:
    set_chunked_attn: TapAttn = make_set_chunked_attn(
      query_chunk_size = 1024,
      kv_chunk_size = None,
    )
    tap_module: TapModule = tap_attn_to_tap_module(set_chunked_attn)
    unet.apply(tap_module)
  case AttentionMode.TorchMultiheadAttention:
    tap_module: TapModule = replace_attn_to_tap_module(to_mha)
    unet.apply(tap_module)
  case AttentionMode.ScaledDPAttn:
    unet.set_attn_processor(AttnProcessor2_0())
  case AttentionMode.Xformers:
    assert is_xformers_available()
    unet.enable_xformers_memory_efficient_attention()

# sampling in higher-precision helps to converge more stably toward the "true" image (not necessarily better-looking though)
sampling_dtype: torch.dtype = torch.float32
# sampling_dtype: torch.dtype = torch_dtype
alphas_cumprod: Tensor = get_alphas_cumprod(get_alphas(get_betas(device=device))).to(dtype=sampling_dtype)
unet_k_wrapped = DiffusersSD2Denoiser(unet, alphas_cumprod, sampling_dtype) if needs_vparam else DiffusersSDDenoiser(unet, alphas_cumprod, sampling_dtype)
denoiser_factory = BatchDenoiserFactory(unet_k_wrapped)

vae_dtype = torch_dtype
vae_revision = revision
# you can make VAE 32-bit but it looks the same to me and would be slightly slower + more disk space
# vae_dtype: torch.dtype = torch.float32
# vae_revision=None

vae: AutoencoderKL = AutoencoderKL.from_pretrained(
  model_name,
  subfolder='vae',
  revision=vae_revision,
  torch_dtype=vae_dtype,
).to(device).eval()
latents_to_bchw: LatentsToBCHW = make_latents_to_bchw(vae)
latents_to_pils: LatentsToPils = make_latents_to_pils(latents_to_bchw)
encode_img: EncodeImg = make_encode_img(vae)

approx_decoder_ckpt: DecoderCkpt = get_approx_decoder_ckpt(model_name, wd_prefer_1_3)
approx_decoder: Decoder = get_approx_decoder(approx_decoder_ckpt, device)
approx_latents_to_pils: LatentsToPils = make_approx_latents_to_pils_wd15(approx_decoder)

clip_impl = ClipImplementation.HF
if model_name == 'hakurei/waifu-diffusion' and wd_prefer_1_3:
  clip_ckpt = ClipCheckpoint.OpenAI
  max_context_segments=1
else:
  clip_ckpt: ClipCheckpoint = model_needs.clip_ckpt
clip_subtract_hidden_state_layers = 1 if needs_penultimate_clip_hidden_state else 0
embed: Embed = get_embedder(
  impl=clip_impl,
  ckpt=clip_ckpt,
  subtract_hidden_state_layers=clip_subtract_hidden_state_layers,
  max_context_segments=model_needs.xattn_max_context_segments,
  device=device,
  torch_dtype=torch_dtype
)

schedule_template = KarrasScheduleTemplate.CudaMastering
schedule: KarrasScheduleParams = get_template_schedule(
  schedule_template,
  model_sigma_min=unet_k_wrapped.sigma_min,
  model_sigma_max=unet_k_wrapped.sigma_max,
  device=unet_k_wrapped.sigmas.device,
  dtype=unet_k_wrapped.sigmas.dtype,
)

steps, sigma_max, sigma_min, rho = schedule.steps, schedule.sigma_max, schedule.sigma_min, schedule.rho
sigmas: Tensor = get_sigmas_karras(
  n=steps,
  sigma_max=sigma_max,
  sigma_min=sigma_min,
  rho=rho,
  device=device,
).to(sampling_dtype)
sigmas_quantized = torch.cat([
  quantize_to(sigmas[:-1], unet_k_wrapped.sigmas),
  zeros((1), device=sigmas.device, dtype=sigmas.dtype)
])
print(f"sigmas (quantized):\n{', '.join(['%.4f' % s.item() for s in sigmas_quantized])}")

sample_path='out'
intermediates_path=f'{sample_path}/intermediates'
latents_path=f'{sample_path}/latents'
for path_ in [sample_path, intermediates_path, latents_path]:
  os.makedirs(path_, exist_ok=True)

approx_intermediate_decode = True
intermediate_latents_to_pils: LatentsToPils = approx_latents_to_pils if approx_intermediate_decode else latents_to_pils
make_log_intermediates: LogIntermediatesFactory = make_log_intermediates_factory(intermediate_latents_to_pils)
log_intermediates_enabled = False
save_latents_enabled = False

match(model_name):
  case 'hakurei/waifu-diffusion':
    if wd_prefer_1_3:
      height = 512
      width = 512**2//height
    else:
      # WD1.4 was trained on area=640**2 and no side longer than 768
      height = 768
      width = 640**2//height
  case 'waifu-diffusion/wd-1-5-beta' | 'waifu-diffusion/wd-1-5-beta2':
    # WD1.5 was trained on area=896**2 and no side longer than 1152
    sqrt_area=896
    # >1 = portrait
    # aspect_ratio = 1.357
    aspect_ratio = 1.15
    height = int(sqrt_area*aspect_ratio)
    width = sqrt_area**2//height
  case _:
    width = 768 if is_768 else 512
    height = width

latent_scale_factor = 8
latents_shape = LatentsShape(unet.in_channels, height // latent_scale_factor, width // latent_scale_factor)

# img_tensor: FloatTensor = load_img('/home/birch/badger-clean.png')
# img_tensor: FloatTensor = load_img('/home/birch/flandre2.png')

seed_latent_maker = SeedLatentMaker(latents_shape, dtype=torch.float32, device=device)
img_encode_latent_maker = ImgEncodeLatentMaker(seed_latent_maker)

latent_strategies: List[MakeLatentsStrategy] = [
  img_encode_latent_maker.make_latents,
  seed_latent_maker.make_latents,
]

latent_maker = LatentMaker(
  strategies=latent_strategies,
)

batch_latent_maker = BatchLatentMaker(
  latent_maker.make_latents,
)

# seeds_nominal: List[int] = [3524181318]
seeds_nominal: List[int] = [
  4097250441,
  245331461,
  679566949,
  1527468831,
  1659224482,
]
# cfg_scales_: Iterable[float] = (1.0, 1.75, 2.5, 5., 7.5, 10., 15., 20., 25., 30.,) #20.,)
# cfg_scales_: Iterable[float] = (7.5, 10., 12.5, 15., 17.5, 20., 22.5, 25., 27.5, 30.,) #20.,)
# cfg_scales_: Iterable[float] = (7.5, 30.,) #20.,)
cfg_scales_: Iterable[float] = (7.5,) #20.,)

center_denoise_outputs_: Tuple[bool, bool] = (False, True,) #20.,)

# max_batch_size = 8
max_batch_size = 10
# n_rand_seeds = max_batch_size
# n_rand_seeds = 1
n_rand_seeds = (max_batch_size)//len(cfg_scales_)

seeds: Iterable[int] = chain(
  # (2678555696,),
  # (get_seed() for _ in range(n_rand_seeds)),
  # (seed for _ in range(n_rand_seeds//2) for seed in repeat(get_seed(), 2)),
  # (seed for _ in range(len(seeds_nominal)) for seed in chain.from_iterable(repeat(seeds_nominal, len(cfg_scales_)))),
  (seed for seed in seeds_nominal for _ in range(len(cfg_scales_)*len(center_denoise_outputs_))),
)

uncond_prompt=BasicPrompt(text='')
# uncond_prompt=BasicPrompt(
#   text='lowres, bad anatomy, bad hands, missing fingers, extra fingers, blurry, mutation, deformed face, ugly, bad proportions, monster, cropped, worst quality, jpeg, bad posture, long body, long neck, jpeg artifacts, deleted, bad aesthetic, realistic, real life, instagram'
# )

conditions: Iterable[ConditionSpec] = cycle((SingleCondition(
  # cfg=CFG(scale=cfg_scale, uncond_prompt=uncond_prompt, mimic_scale=7.5, dynthresh_percentile=0.985),
  cfg=CFG(scale=cfg_scale, uncond_prompt=uncond_prompt, center_denoise_output=center_denoise_output),
  prompt=BasicPrompt(
    # text='flandre scarlet, carnelian, 1girl, blonde hair, blush, light smile, collared shirt, hair between eyes, hat bow, looking at viewer, medium hair, mob cap, upper body, puffy short sleeves, red bow, watercolor (medium), traditional media, red eyes, red vest, small breasts, upper body, white shirt, yellow ascot'
    text='masterpiece character portrait of shrine maiden, artgerm, ilya kuvshinov, tony pyykko, from side, looking at viewer, long black hair, upper body, 4k hdr, global illumination, lit from behind, oriental scenic, Pixiv featured, vaporwave',
  ),
  center_denoise_output=center_denoise_output,
) for center_denoise_output in center_denoise_outputs_ for cfg_scale in cfg_scales_))

sample_specs: Iterable[SampleSpec] = (SampleSpec(
  latent_spec=SeedSpec(seed),
  # latent_spec=ImgEncodeSpec(seed=seed, start_sigma=16., get_latents=lambda: encode_img(img_tensor, generator=torch.Generator(device=vae.device).manual_seed(seed))),
  cond_spec=cond,
) for seed, cond in zip(seeds, conditions))

batcher = ExecutionPlanBatcher[SampleSpec, ExecutionPlan](
  max_batch_size=max_batch_size,
  make_execution_plan=make_execution_plan,
)
batch_generator: Generator[BatchSpecGeneric[ExecutionPlan], None, None] = batcher.generate(sample_specs)

consistent_batch_size=None

sum_of_batch_times=0
initial_batch_time=0
sample_count=0
batch_count=0

with no_grad():
  batch_tic = time.perf_counter()
  for batch_ix, (plan, specs) in enumerate(batch_generator):
    # explicit type cast to help IDE infer type
    plan: ExecutionPlan = plan
    specs: List[SampleSpec] = specs

    batch_count += 1
    batch_sample_count = len(specs)
    seeds: List[Optional[int]] = list(map(lambda spec: spec.latent_spec.seed if isinstance(spec.latent_spec, SeedSpec) else None, specs))
    cfgs: List[Optional[float]] = list(map(lambda spec: None if spec.cond_spec.cfg is None else spec.cond_spec.cfg.scale, specs))
    if plan.start_sigma is not None:
      sigmas = sigmas[sigmas<plan.start_sigma]
      print(f"sigmas (truncated):\n{', '.join(['%.4f' % s.item() for s in sigmas])}")
    latents: FloatTensor = batch_latent_maker.make_latents(specs=map(lambda spec: spec.latent_spec, specs), start_sigma=sigmas[0])
    
    embedding_and_mask: EmbeddingAndMask = embed(plan.prompt_texts_ordered)
    embedding_norm, mask_norm = embedding_and_mask
    del embedding_and_mask

    if '' in plan.prompt_texts_ordered:
      null_prompt_ix: int = plan.prompt_texts_ordered.index('')
      # SD was trained loads on unmasked empty-string uncond, so undo uc mask on any empty-string uncond
      unmasked_clip_segment: BoolTensor = arange(mask_norm.size(1), device=device) < 77
      mask_norm[null_prompt_ix] = unmasked_clip_segment
      del unmasked_clip_segment

    embed_instance_ixs_flat: List[int] = [ix for sample_ixs in plan.prompt_text_instance_ixs for ix in sample_ixs]
    # denormalize
    embedding_denorm: FloatTensor = embedding_norm.index_select(0, tensor(embed_instance_ixs_flat, device=device))
    mask_denorm: BoolTensor = mask_norm.index_select(0, tensor(embed_instance_ixs_flat, device=device))

    # per sample: quantity of conditions upon which it should be denoised
    conds_per_prompt: List[int] = [len(sample_ixs) for sample_ixs in plan.prompt_text_instance_ixs]
    conds_per_prompt_tensor: LongTensor = tensor(conds_per_prompt, dtype=torch.long, device=device)
    if plan.cfg is None:
      uncond_ixs = None
      cfg_scales = None
      mimic_scales = None
      mimic_scales_arr = [None]*batch_sample_count
      dynthresh_percentile = None
    else:
      first_cond_ix_per_prompt: LongTensor = conds_per_prompt_tensor.roll(1).index_put(
        indices=[torch.zeros([1], dtype=torch.long, device=device)],
        values=torch.zeros([1], dtype=torch.long, device=device),
      ).cumsum(0)
      uncond_ixs: LongTensor = first_cond_ix_per_prompt + tensor(plan.cfg.uncond_instance_ixs, dtype=torch.long, device=device)
      cfg_scales: FloatTensor = tensor(plan.cfg.scales, dtype=sampling_dtype, device=device)
      if any(map(lambda scale: scale is not None, plan.cfg.mimic_scales)):
        mimic_scales_arr: List[Optional[float]] = plan.cfg.mimic_scales
        mimic_scales_defaulted: List[float] = [nominal if mimic is None else mimic for mimic, nominal in zip(plan.cfg.mimic_scales, plan.cfg.scales)]
        mimic_scales: FloatTensor = tensor(mimic_scales_defaulted, dtype=sampling_dtype, device=device)
      else:
        mimic_scales = None
        mimic_scales_arr = [None]*batch_sample_count
      dynthresh_percentile: Optional[float] = plan.cfg.dynthresh_percentile
    
    cond_weights: FloatTensor = tensor(plan.cond_weights, dtype=sampling_dtype, device=device)

    if any(plan.center_denoise_outputs):
      center_denoise_outputs: Optional[BoolTensor] = tensor(plan.center_denoise_outputs, dtype=torch.bool, device=device).reshape(-1, 1, 1, 1) if any(plan.center_denoise_outputs) else None
      center_configs: List[bool] = [
        any((
          cond_prompt.center_denoise_output for cond_prompt in spec.cond_spec.weighted_cond_prompts
        )) or (
          False if spec.cond_spec.cfg is None else spec.cond_spec.cfg.center_denoise_output
        ) for spec in specs
      ]
    else:
      center_denoise_outputs = None
      center_configs: List[None] = [None]*batch_sample_count
    del specs

    cond_interps_flat: List[Optional[CondInterp]] = [cond_interp for cond_interps in plan.cond_interps for cond_interp in cond_interps]
    for cond_ix, (prompt_text_instance_ix, cond_interp) in enumerate(zip(embed_instance_ixs_flat, cond_interps_flat)):
      if cond_interp is None: continue
      match cond_interp.interp_strategy:
        case InterpStrategy.Slerp:
          interp: InterpProto = slerp
        case InterpStrategy.Lerp:
          interp: InterpProto = lerp
        case _:
          raise Exception(f"Never heard of a '{cond_interp.interp_strategy}' InterpStrategy")
      start: FloatTensor = embedding_denorm[cond_ix]
      end: FloatTensor = embedding_norm[cond_interp.prompt_text_instance_ix]
      embedding_denorm[cond_ix] = interp(
        start.float(),
        end.float(),
        cond_interp.interp_quotient,
      ).to(embedding_denorm.dtype)
      mask_denorm[cond_ix] |= mask_norm[cond_interp.prompt_text_instance_ix]
      del start, end
    del embedding_norm, plan, mask_norm
    
    match(attn_mode):
      # xformers attn_bias is only implemented for Triton + A100 GPU
      # https://github.com/facebookresearch/xformers/issues/576
      # chunked attention *can* be made to support masks, but I didn't implement it yet
      case AttentionMode.Xformers | AttentionMode.Chunked | AttentionMode.ScaledDPAttn:
        mask_denorm = None

    denoiser: Denoiser = denoiser_factory(
      cross_attention_conds=embedding_denorm,
      cross_attention_mask=mask_denorm,
      conds_per_prompt=conds_per_prompt_tensor,
      cond_weights=cond_weights,
      uncond_ixs=uncond_ixs,
      cfg_scales=cfg_scales,
      mimic_scales=mimic_scales,
      dynthresh_percentile=dynthresh_percentile,
      center_denoise_outputs=center_denoise_outputs,
    )
    del embedding_denorm, mask_denorm, conds_per_prompt_tensor, cond_weights, uncond_ixs, cfg_scales, mimic_scales, center_denoise_outputs

    noise_sampler = BrownianTreeNoiseSampler(
      latents,
      # rather than using the sigma_min,max vars we already have:
      # refer to sigmas array, which can be truncated if we are doing img2img
      # we grab *penultimate* sigma_min, because final sigma is always 0
      sigma_min=sigmas[-2],
      sigma_max=sigmas[0],
      # there's no requirement that the noise sampler's seed be coupled to the init noise seed;
      # I'm just re-using it because it's a convenient arbitrary number
      seed=seeds[0],
    )
  
    base_count = len(fnmatch.filter(os.listdir(sample_path), '*.png'))
    sample_stems: List[str] = [get_sample_stem(
      base_count=base_count,
      ix_in_batch=ix,
      seed=seed,
      cfg=cfg,
      mimic=mimic,
      dynthresh_percentile=dynthresh_percentile,
      center_denoise_output=center,
      half=half,
      model_name=model_name,
    ) for ix, (seed, cfg, mimic, center) in enumerate(zip(
      seeds,
      cfgs,
      mimic_scales_arr,
      center_configs,
    ))]

    if log_intermediates_enabled:
      intermediates_paths: List[str] = [f'{intermediates_path}/{stem}' for stem in sample_stems]
      for intermediates_path in intermediates_paths:
        os.makedirs(intermediates_path, exist_ok=True)
      callback: LogIntermediates = make_log_intermediates(intermediates_paths)
    else:
      callback = None

    tic = time.perf_counter()
    latents: Tensor = sample_dpmpp_2m(
      denoiser,
      latents,
      sigmas,
      # noise_sampler=noise_sampler, # you can only pass noise sampler to ancestral samplers
      callback=callback,
    ).to(vae_dtype)
    del denoiser
    if device.type == 'cuda':
      torch.cuda.empty_cache()
    
    if save_latents_enabled:
      for stem, sample_latents in zip(sample_stems, latents):
        torch.save(sample_latents, os.path.join(latents_path, f"{stem}.pt"))

    pil_images: List[Image.Image] = latents_to_pils(latents)
    del latents

    sample_time=time.perf_counter()-tic
    sum_of_batch_times += sample_time
    sample_count += batch_sample_count
    if batch_ix == 0:
      # account for first sample separately because warmup can be an outlier
      initial_batch_time = sample_time
      consistent_batch_size = batch_sample_count
    else:
      consistent_batch_size = consistent_batch_size if batch_sample_count == consistent_batch_size else None

    for stem, image in zip(sample_stems, pil_images):
      image.save(os.path.join(sample_path, f"{stem}.png"))
    del pil_images
    if device.type == 'cuda':
      torch.cuda.empty_cache()

total_time=time.perf_counter()-batch_tic

perf_message = f'in total, generated {batch_count} batches'
if consistent_batch_size is not None:
  perf_message += f' of {consistent_batch_size} images'
perf_message += ' in (secs):\n'

perf_message += f'Embed + Unet + sampling + VAE + RGB-to-PIL + PIL-to-disk:\n  {total_time:.2f}'
perf_message += f' (avg {total_time/sample_count:.2f}/sample)'
if consistent_batch_size and batch_count>1:
  perf_message += f' (avg {total_time/batch_count:.2f}/batch)'
perf_message += '\n'

perf_message += f'Unet + sampling + VAE + RGB-to-PIL:\n  {sum_of_batch_times:.2f}'
perf_message += f' (avg {sum_of_batch_times/sample_count:.2f}/sample)'
if consistent_batch_size and batch_count>1:
  perf_message += f' (avg {sum_of_batch_times/batch_count:.2f}/batch)'
perf_message += '\n'

if batch_count>1:
  excl_warmup_time=sum_of_batch_times-initial_batch_time
  perf_message += f'Unet + sampling + VAE + RGB-to-PIL (excl. warmup batch):\n  {excl_warmup_time:.2f}'
  if consistent_batch_size:
    perf_message += f' (avg {excl_warmup_time/sample_count:.2f}/sample)'
  if batch_count>2:
    perf_message += f' (avg {excl_warmup_time/batch_count:.2f}/batch)'

print(perf_message)