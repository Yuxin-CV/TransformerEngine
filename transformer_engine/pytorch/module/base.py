# Copyright (c) 2022-2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.

"""Base modules and utilities for TransformerEngine PyTorch API"""
import os
import pickle
import warnings
from abc import ABC, abstractmethod
from typing import Union, Optional, Tuple, Dict, Any, List
from functools import partial
from contextlib import contextmanager

import numpy as np
import torch
import torch.nn.functional as F
from torch.nn.parameter import Parameter

import transformer_engine_extensions as tex
from ..fp8 import (
    is_fp8_enabled,
    is_fp8_calibration,
    get_fp8_recipe,
    get_fp8_group,
    get_default_fp8_recipe,
    get_fp8_te_dtype,
    is_first_fp8_module,
    new_fp8_context_id,
    get_fp8_context_id,
    set_fp8_context_id,
    add_amax_to_global_buffer,
    copy_amax_from_global_buffer,
    global_amax_reduction,
    setup_amax_forward_global_reduce_func,
    amax_and_scale_update,
    get_global_fp8_buffer,
    set_global_fp8_buffer,
    set_amax_buffer_key_deletion,
    delete_key_from_amax_buffer,
    copy_forward_fp8_meta_tensors_for_recompute,
    get_old_fp8_meta_tensors_for_recompute,
    restore_fp8_meta_tensors,
    get_amax_reduce_handle_fwd,
)
from ..distributed import (
    gather_along_first_dim,
    is_fp8_activation_recompute_enabled,
    in_fp8_activation_recompute_phase,
)
from ..cpp_extensions import (
    fp8_cast_transpose_fused,
    fp8_cast_transpose_bgrad_fused,
    cast_to_fp8,
)
from ..constants import dist_group_type

_2X_ACC_FPROP = False
_2X_ACC_DGRAD = True
_2X_ACC_WGRAD = True
_cublas_workspace = None
_ub_communicators = None
_NUM_MAX_UB_STREAMS = 3
_amax_reduce_handle_bwd = None


def get_cublas_workspace_size_bytes() -> None:
    """Return 32 MiB if using hopper, 4 MiB for all other architectures."""
    if torch.cuda.get_device_properties(torch.cuda.current_device()).major >= 9:
        return 33_554_432
    return 4_194_304


def get_workspace() -> torch.Tensor:
    """Returns workspace for cublas."""
    global _cublas_workspace
    if _cublas_workspace is None:
        _cublas_workspace = torch.empty(
            get_cublas_workspace_size_bytes(), dtype=torch.uint8, device="cuda"
        )
    return _cublas_workspace

@contextmanager
def _prepare_backward(
    fp8: bool,
    fp8_meta: Dict[str, Any],
    tp_group: dist_group_type,
    tp_size: int,
    name: str = ""
) -> None:
    """Checks and prep for BWD."""
    if fp8:
        global _amax_reduce_handle_bwd
        if _amax_reduce_handle_bwd is not None:
            _amax_reduce_handle_bwd.wait()
            _amax_reduce_handle_bwd = None

        # Update amax and scale; Skip all setup for global amax reduction
        if not fp8_meta["recipe"].reduce_amax:
            amax_and_scale_update(fp8_meta, False)
        else:
            # From previous iteration
            copy_amax_from_global_buffer(fp8_meta, forward=False)
            amax_and_scale_update(fp8_meta, False)
            set_amax_buffer_key_deletion(fp8_meta, forward=False)

            # Get new backward key.
            fp8_meta["autocast_id_bwd"] = fp8_meta["autocast_id_fwd_stack"].pop(0)

            add_amax_to_global_buffer(fp8_meta, forward=False)

    with torch.cuda.nvtx.range(name + " backward"):
        yield

    if fp8 and fp8_meta["recipe"].reduce_amax:
        if fp8_meta["first_module"]:
            _amax_reduce_handle_bwd = global_amax_reduction(
                fp8_meta,
                tp_group,
                tp_size,
                forward=False
            )
            delete_key_from_amax_buffer(forward=False)


def initialize_ub(
    shape: list,
    tp_size: int,
    use_fp8: bool = False,
    ub_cfgs: Optional[dict] = None
) -> None:
    """Initialize communicators for TP comm overlap using userbuffers."""
    global _ub_communicators
    assert _ub_communicators is None, "UB communicators are already initialized."
    _ub_communicators = {}
    rank_id = torch.distributed.get_rank()

    # Increase the workspace by the number of maximum concurrent streams
    global _cublas_workspace
    _cublas_workspace = get_workspace().repeat(_NUM_MAX_UB_STREAMS)

    # Default buffer precision: AllGather buffers use fp8 when using fp8 recipe
    fp8_buf = [
        "qkv_fprop", "qkv_dgrad", "proj_dgrad", "fc1_fprop", "fc1_dgrad", "fc2_dgrad"
    ]
    # Default overlap methods for layers
    methods = {
        "ring_exchange":["qkv_fprop", "fc1_fprop", "proj_dgrad", "fc2_dgrad"],
        "pipeline":["proj_fprop", "fc2_fprop"],
        "bulk":["qkv_dgrad", "qkv_wgrad", "fc1_dgrad", "fc1_wgrad"],
    }

    def get_method(name):
        for method, names in methods.items():
            if name in names:
                return method
        raise KeyError(f"Given layer name {name} does not exist.")

    def add_ub(
        name: str,
        method: str,
        num_sm: int = 16,
        cga_size: int = 2,
        set_sm_margin: int = 0,
        num_splits: int = 4,
        aggregate: int = 0,
    ) -> None:
        dtype = torch.uint8 if (use_fp8 and name in fp8_buf) else torch.bfloat16
        sample_buffer = torch.empty(shape, dtype=dtype, device='cuda')
        if method == 'ring_exchange':
            ub_obj = tex.UbufP2PCommOverlap(
                    sample_buffer,          # Sample userbuffer
                    rank_id,                # Rank id
                    tp_size,                # TP size
                    aggregate,              # Aggregate 2X GEMM chunks
                    _NUM_MAX_UB_STREAMS,    # Max concurrent GEMM streams
                )
        else:
            ub_obj = tex.UbufCommOverlap(
                    sample_buffer,          # Sample userbuffer
                    rank_id,                # Rank id
                    tp_size,                # TP size
                    num_sm,                 # Number of communication SMs
                    cga_size,               # CGA cluster size
                    num_splits,             # Number of communication splits
                    set_sm_margin,          # Set SM margin
                    _NUM_MAX_UB_STREAMS,    # Max concurrent GEMM streams
                )
        _ub_communicators[name] = ub_obj

    for name in (methods["ring_exchange"]+methods["pipeline"]+methods["bulk"]):
        if ub_cfgs is not None and name in ub_cfgs:
            ub_cfg = ub_cfgs[name]
            method = ub_cfg["method"] if "method" in ub_cfg else get_method(name)
            num_sm = ub_cfg["num_sm"] if "num_sm" in ub_cfg else 16
            cga_size = ub_cfg["cga_size"] if "cga_size" in ub_cfg else 2
            num_splits = ub_cfg["num_splits"] if "num_splits" in ub_cfg else 0
            set_sm_margin = ub_cfg["set_sm_margin"] if "set_sm_margin" in ub_cfg else 0
            aggregate = ub_cfg["aggregate"] if "aggregate" in ub_cfg else 0
            add_ub(
                name,
                method,
                num_sm,
                cga_size,
                set_sm_margin,
                num_splits,
                aggregate
            )
        else:
            method = get_method(name)
            if method == "pipeline":
                add_ub(name, method)
            else:
                add_ub(name, method, num_splits=0)


def get_ub(name: str):
    """Get userbuffer communicator corresponding to give key."""
    global _ub_communicators
    assert _ub_communicators is not None, "UB manager is not initialized."
    assert name in _ub_communicators, f"UB for {name} is not registered."
    return _ub_communicators[name]


class _NoopCat(torch.autograd.Function):
    """This class is a no-op replacement for `torch.cat`."""

    @staticmethod
    def forward(ctx,
                full_param_buffer: torch.Tensor,
                *params_split: Tuple[torch.Tensor, ...],
    ) -> torch.Tensor:
        assert not full_param_buffer.requires_grad, "Buffers should not require gradient"
        assert (
            full_param_buffer.shape[0] % len(params_split) == 0
        ), "Dimensions not compatible for concatenation"

        param_temp = full_param_buffer.new()
        param_temp.set_(full_param_buffer.storage(),
                        full_param_buffer.storage_offset(),
                        full_param_buffer.size(),
                        full_param_buffer.stride())
        param_temp.requires_grad = True

        ctx.save_for_backward(full_param_buffer, *params_split)
        return param_temp

    @staticmethod
    def backward(ctx, grad_output: torch.Tensor) -> Tuple[Union[torch.Tensor, None], ...]:
        full_param_buffer, *params_split = ctx.saved_tensors

        split_size = full_param_buffer.shape[0] // len(params_split)
        grads = []

        for i, _ in enumerate(params_split):
            grads.append(grad_output[i * split_size : (i+1) * split_size])

        return None, *grads


class TransformerEngineBaseModule(torch.nn.Module, ABC):
    """Base TE module."""

    def __init__(self) -> None:
        super().__init__()
        assert torch.cuda.is_available(), "TransformerEngine needs CUDA."
        self.fp8_initialized = False
        self.fp8 = False
        self.fp8_calibration = False
        self.fp8_meta = {}
        self.fp8_meta["fp8_group"] = None
        self.fp8_meta["recipe"] = get_default_fp8_recipe()
        self.fp8_meta_tensors_initialized = False
        self.tp_group = None
        self.tp_size = 1
        self.sequence_parallel = False
        self.fp8_weight_shapes = []
        self.fp8_meta["autocast_id_fwd_stack"] = []
        self.fp8_meta["async_amax_reduction"] = bool(
            int(os.getenv("NVTE_ASYNC_AMAX_REDUCTION", "0"))
        )

    def set_meta_tensor(self, fwd: bool) -> None:
        """Init scales and amaxes for fwd | bwd."""
        fp8_meta_tensor_key = "scaling_fwd" if fwd else "scaling_bwd"

        if self.fp8_meta_tensors_initialized:
            # Handle changed amax history size.
            curr_len = self.fp8_meta[fp8_meta_tensor_key].amax_history.shape[0]
            need_len = self.fp8_meta["recipe"].amax_history_len
            if need_len < curr_len:
                self.fp8_meta[fp8_meta_tensor_key].amax_history = (
                    self.fp8_meta[fp8_meta_tensor_key]
                    .amax_history[: self.fp8_meta["recipe"].amax_history_len].clone()
                )
            elif need_len > curr_len:
                extra_rows = need_len - curr_len
                self.fp8_meta[fp8_meta_tensor_key].amax_history = F.pad(
                    self.fp8_meta[fp8_meta_tensor_key].amax_history, pad=(0, 0, 0, extra_rows)
                )
            return

        # Max. number of fp8 tensors per GEMM = 3 (input, weight, output) for fwd and
        # 2 (grad_output and grad_input) for bwd
        num_fp8_tensors = (
            self.fp8_meta["num_gemms"] * 3 if fwd else self.fp8_meta["num_gemms"] * 2
        )

        self.fp8_meta[fp8_meta_tensor_key] = tex.FP8TensorMeta()
        self.fp8_meta[fp8_meta_tensor_key].scale = torch.ones(
            num_fp8_tensors, dtype=torch.float32, device="cuda"
        )
        self.fp8_meta[fp8_meta_tensor_key].scale_inv = torch.ones(
            num_fp8_tensors, dtype=torch.float32, device="cuda"
        )
        self.fp8_meta[fp8_meta_tensor_key].amax_history = torch.zeros(
            self.fp8_meta["recipe"].amax_history_len,
            num_fp8_tensors,
            dtype=torch.float32,
            device="cuda",
        )

        # Needed for calculation of scale inverses to
        # preserve scale_inv when caching FP8 weights
        if fwd:
            # [True, False, True]: -> [input, weight, output]
            self.fp8_meta[fp8_meta_tensor_key + "_non_weight_mask"] = torch.BoolTensor(
                [True, False, True] * self.fp8_meta["num_gemms"]
            ).cuda()
        else:
            # [True, True]: -> [grad_output, grad_input]
            self.fp8_meta[fp8_meta_tensor_key + "_non_weight_mask"] = torch.BoolTensor(
                [True, True] * self.fp8_meta["num_gemms"]
            ).cuda()

    def init_fp8_meta_tensors(self) -> None:
        """Init scales and amaxes."""
        self.set_meta_tensor(True)
        self.set_meta_tensor(False)
        self.fp8_meta_tensors_initialized = True

    def get_extra_state(self) -> torch.Tensor:
        """Save before checkpointing."""
        state = None
        if self.fp8 or self.fp8_calibration:
            state = {}
            state["scale_fwd"] = self.fp8_meta["scaling_fwd"].scale
            state["scale_inv_fwd"] = self.fp8_meta["scaling_fwd"].scale_inv
            state["amax_history_fwd"] = self.fp8_meta["scaling_fwd"].amax_history
            state["scale_bwd"] = self.fp8_meta["scaling_bwd"].scale
            state["scale_inv_bwd"] = self.fp8_meta["scaling_bwd"].scale_inv
            state["amax_history_bwd"] = self.fp8_meta["scaling_bwd"].amax_history
            state["global_fp8_buffer"] = get_global_fp8_buffer()

            # Store other pickelable values.
            extra = {}
            for k, v in self.fp8_meta.items():
                if isinstance(v, (bool, int, float, str)):
                    extra[k] = v
            state["extra_fp8_variables"] = extra

        state_serialized = pickle.dumps(state)
        state_tensor = torch.tensor(np.frombuffer(state_serialized, dtype=np.uint8))

        return state_tensor

    def set_extra_state(self, state: torch.Tensor) -> None:
        """Load previous state."""
        if state is None:
            return

        # Maintain backward compatibility with v0.2.0 and older.
        if isinstance(state, list):
            warnings.warn(
                "This checkpoint format is deprecated and will be"
                "removed in a future release of Transformer Engine"
            )

            # Retrieve checkpointed items.
            scale_fwd = state[0]
            amax_history_fwd = state[1]
            scale_bwd = state[2]
            amax_history_bwd = state[3]
            self.fp8_meta["recipe"].amax_history_len = amax_history_fwd.shape[0]
            self.fp8_meta["num_gemms"] = (
                amax_history_fwd.shape[1] // 2
            )  # Two FWD tensors per GEMM

            # Initialize before loading
            self.init_fp8_meta_tensors()
            self.fp8_meta["scaling_fwd"].scale.copy_(scale_fwd)
            self.fp8_meta["scaling_fwd"].amax_history.copy_(amax_history_fwd)
            self.fp8_meta["scaling_bwd"].scale.copy_(scale_bwd)
            self.fp8_meta["scaling_bwd"].amax_history.copy_(amax_history_bwd)

            # Restore global FP8 buffer state.
            set_global_fp8_buffer(state[4])
            self.fp8_meta["update_amax_and_scale_fwd"] = state[5]
            self.fp8_meta["global_fp8_buffer_pos_fwd"] = state[6]
            self.fp8_meta["global_fp8_buffer_pos_bwd"] = state[7]
            self.fp8_meta["autocast_id_fwd"] = state[8]
            self.fp8_meta["autocast_id_bwd"] = state[9]
            return

        if isinstance(state, torch.Tensor):
            state = pickle.loads(state.detach().cpu().numpy().tobytes())
            if state is None:
                return

        # Restore global FP8 buffer states.
        set_global_fp8_buffer(state["global_fp8_buffer"])
        # Load extra items.
        self.fp8_meta.update(state["extra_fp8_variables"])
        self.fp8_meta["recipe"].amax_history_len = state["amax_history_fwd"].shape[0]
        if "global_fp8_buffer_pos_fwd_recompute" in self.fp8_meta:
            del self.fp8_meta["global_fp8_buffer_pos_fwd_recompute"]

        # Initialize before loading.
        self.init_fp8_meta_tensors()
        self.fp8_meta["scaling_fwd"].scale.copy_(state["scale_fwd"])
        self.fp8_meta["scaling_fwd"].amax_history.copy_(state["amax_history_fwd"])
        self.fp8_meta["scaling_bwd"].scale.copy_(state["scale_bwd"])
        self.fp8_meta["scaling_bwd"].amax_history.copy_(state["amax_history_bwd"])

        # Backwards compatibility: compute scale inv if it wasn't saved in the extra state.
        if "scale_inv_fwd" not in state or "scale_inv_bwd" not in state:
            assert (
                "scale_inv_fwd" not in state and "scale_inv_bwd" not in state
            ), "Invalid state, began saving scale_inv_fwd and scale_inv_bwd at the same time"
            self.fp8_meta["scaling_fwd"].scale_inv.copy_(1.0/state["scale_fwd"])
            self.fp8_meta["scaling_bwd"].scale_inv.copy_(1.0/state["scale_bwd"])
        else:
            self.fp8_meta["scaling_fwd"].scale_inv.copy_(state["scale_inv_fwd"])
            self.fp8_meta["scaling_bwd"].scale_inv.copy_(state["scale_inv_bwd"])


    def set_activation_dtype(self, inp: torch.Tensor) -> None:
        """Get activation data type for AMP."""
        # Native AMP (`torch.autocast`) gets highest priority
        if torch.is_autocast_enabled():
            self.activation_dtype = torch.get_autocast_gpu_dtype()
            return

        # All checks after this have already been performed once, thus skip
        # We assume that user doesn't change input types across iterations
        if hasattr(self, "activation_dtype"):
            return

        assert all(
            (
                (inp.dtype == param.dtype) if param is not None else True
                for param in self.parameters()
            )
        ), (
            "Data type for activations and weights must "
            "match when outside of autocasted region"
        )
        assert all(
            (
                (inp.dtype == buf.dtype) if buf is not None else True
                for buf in self.buffers()
            )
        ), (
            "Data type for activations and buffers must "
            "match when outside of autocasted region"
        )
        self.activation_dtype = inp.dtype

    def set_fp8_weights(self) -> None:
        """Initializes FP8 weights for the module as class attributes. These
        are not parameters or buffers since we do not want functions such as
        `.to(dtype)` or `.to(device)` to effect them. These also do not need
        to be checkpointed. During `init` phase of the module, the attribute
        `fp8_weight_shapes` must be populated with the tensor shapes for FP8
        weights. This function will iterate over those shapes and initialize
        respective attributed named `weight1_fp8`, `weight2_fp8`, ...
        """
        if not self.fp8:
            return

        for i, shape in enumerate(self.fp8_weight_shapes, start=1):
            weight_cast_attr = f"weight{i}_fp8"
            weight_transpose_attr = f"weight{i}_t_fp8"

            if (
                hasattr(self, weight_cast_attr)
                and getattr(self, weight_cast_attr).shape == shape
            ):
                return

            setattr(
                self,
                weight_cast_attr,
                torch.empty(
                    shape,
                    device=torch.cuda.current_device(),
                    dtype=torch.uint8,
                ),
            )
            setattr(
                self,
                weight_transpose_attr,
                torch.empty(
                    shape[1],
                    shape[0],
                    device=torch.cuda.current_device(),
                    dtype=torch.uint8,
                ),
            )

    def set_tensor_parallel_group(self, tp_group: Union[dist_group_type, None]) -> None:
        """Set TP group."""
        self.tp_group = tp_group
        self.tp_group_initialized = True

    # This routine is shared across FP8 and FP8_calibration paths so should not actually
    # assume FP8 execution.
    def fp8_init(self, num_gemms: int = 1) -> None:
        """Initialize fp8 related metadata and tensors during fprop."""
        self.fp8 = is_fp8_enabled()
        self.fp8_calibration = is_fp8_calibration()

        if self.fp8 or self.fp8_calibration:
            # FP8 init has already been run and recipe is the same, don't do anything.
            if self.fp8_initialized and get_fp8_recipe() == self.fp8_meta["recipe"]:
                return

            # Set FP8, recipe, and other FP8 metadata
            self.fp8_meta["recipe"] = get_fp8_recipe()
            self.fp8_meta["num_gemms"] = num_gemms
            self.fp8_meta["fp8_group"] = get_fp8_group()

            # Set FP8_MAX per tensor according to recipe
            self.fp8_meta["fp8_max_fwd"] = self.fp8_meta["recipe"].fp8_format.value.max_fwd
            self.fp8_meta["fp8_max_bwd"] = self.fp8_meta["recipe"].fp8_format.value.max_bwd

            # Allocate scales and amaxes
            self.init_fp8_meta_tensors()
            self.fp8_initialized = True
        else:
            # If fp8 isn't enabled, turn off and return.
            self.fp8_initialized = False
            return

    @contextmanager
    def prepare_forward(
        self,
        inp: torch.Tensor,
        is_first_microbatch: Union[bool, None],
        num_gemms: int = 1,
    ) -> None:
        """Checks and prep for FWD.
        The context manager is needed because there isn't a way for a module to know
        if it's the last FP8 module in the forward autocast. It is useful
        to setup the forward aggregated amax reduction for every module
        just in case. The autocast exit will pick up the most recent one.
        """

        # Activation recomputation is used and this is the second forward phase.
        if self.fp8 and in_fp8_activation_recompute_phase():
            get_old_fp8_meta_tensors_for_recompute(self.fp8_meta)
        else:
            assert inp.is_cuda, "TransformerEngine needs CUDA."

            if self.tp_size > 1:
                assert self.tp_group_initialized, "TP group not initialized."

            self.set_activation_dtype(inp)
            self.fp8_init(num_gemms=num_gemms)
            self.set_fp8_weights()

            update_weight_scale_inv = is_first_microbatch is None or is_first_microbatch
            if self.fp8 and self.sequence_parallel:
                assert self.fp8_meta["recipe"].reduce_amax, \
                "Amax reduction across tensor parallel group is " \
                "necessary when using sequence parallelism with FP8."

            # Previous iteration was grad_enabled
            if self.fp8_meta.get("update_amax_and_scale_fwd", False):
                if self.fp8_meta["recipe"].reduce_amax:
                    copy_amax_from_global_buffer(self.fp8_meta, forward=True)
                    amax_and_scale_update(
                        self.fp8_meta, True, update_weight_scale_inv=update_weight_scale_inv
                    )
                    set_amax_buffer_key_deletion(self.fp8_meta, forward=True)
                else:
                    amax_and_scale_update(
                        self.fp8_meta, True, update_weight_scale_inv=update_weight_scale_inv
                    )

            if self.fp8 and self.training:
                # Setup for amax reduction
                if self.fp8_meta["recipe"].reduce_amax:
                    self.fp8_meta["first_module"] = is_first_fp8_module()
                    if self.fp8_meta["first_module"]:
                        # Wait for the prior AMAX reduction to finish
                        amax_reduce_handle_fwd = get_amax_reduce_handle_fwd()
                        if amax_reduce_handle_fwd is not None:
                            amax_reduce_handle_fwd.wait()
                        self.fp8_meta["autocast_id_fwd"] = new_fp8_context_id()
                        set_fp8_context_id(self.fp8_meta["autocast_id_fwd"])
                    else:
                        self.fp8_meta["autocast_id_fwd"] = get_fp8_context_id()
                    self.fp8_meta["autocast_id_fwd_stack"].append(
                        self.fp8_meta["autocast_id_fwd"]
                    )
                    add_amax_to_global_buffer(self.fp8_meta, forward=True)
                self.fp8_meta["update_amax_and_scale_fwd"] = True
            else:
                self.fp8_meta["update_amax_and_scale_fwd"] = False

            # Activation recomputation is used and this is the first forward phase.
            if (
                self.fp8
                and self.training
                and is_fp8_activation_recompute_enabled()
                and not in_fp8_activation_recompute_phase()
            ):
                copy_forward_fp8_meta_tensors_for_recompute(self.fp8_meta)

        with torch.cuda.nvtx.range(self.__class__.__name__ + " forward"):
            yield inp.contiguous()

        if self.fp8 and in_fp8_activation_recompute_phase():
            restore_fp8_meta_tensors(self.fp8_meta)
            return

        if self.fp8 and self.training and self.fp8_meta["recipe"].reduce_amax:
            set_fp8_context_id(self.fp8_meta["autocast_id_fwd"])
            reduce_func = partial(
                global_amax_reduction,
                self.fp8_meta,
                self.tp_group,
                self.tp_size,
                forward=True
            )
            setup_amax_forward_global_reduce_func(reduce_func)

    def set_nccl_overlap_warning_if_tp(self) -> None:
        """When using TP, the NCCL communication needs to be scheduled
        before the GEMM for there to be a guaranteed overlap. From the
        host side in TE, the comm calls are always launched first, but
        to ensure that the GEMM isn't scheduled first, the environment
        variable `CUDA_DEVICE_MAX_CONNECTIONS` needs to be set to 1 to
        force a single channel.
        """
        if self.tp_size == 1:
            return
        num_cuda_work_queues = int(os.getenv("CUDA_DEVICE_MAX_CONNECTIONS", "0"))
        if num_cuda_work_queues != 1:
            warnings.warn(
                "To guarantee overlapping TP and SP collectives with the backward"
                "GEMMs, set environment variable CUDA_DEVICE_MAX_CONNECTIONS = 1"
            )

    @staticmethod
    def grad_output_preprocess(
        ctx, grad_output: torch.Tensor, row_parallel_mode: bool
    ) -> Tuple[Union[torch.Tensor, None], ...]:
        """Utility function for backward.
        Returns tuple in order (all optional/None based on training precion/recipe):
            R1: gathered `grad_output` in higher precision.
            R2: gathered `grad_output` in FP8.
            R3: R2 transposed.
            R4: bias gradient on R1.

        """
        grad_output = grad_output.contiguous()
        grad_output_mat = grad_output.view((-1, grad_output.shape[-1]))
        gather_grad_output = row_parallel_mode and ctx.sequence_parallel

        # No-FP8 case: bgrad is fused with wgrad for this case.
        if not ctx.fp8:
            if gather_grad_output:
                if not ctx.ub_split_ag:
                    grad_output_mat, _ = gather_along_first_dim(
                        grad_output_mat, ctx.tp_group
                    )
                else:
                    ctx.ub_obj_gradout.copy_input_to_ubuf(grad_output, True)
                    grad_output_mat = ctx.ub_obj_gradout.get_ubuf_output(1)
            return grad_output_mat, None, None, None

        fp8_dtype_backward = get_fp8_te_dtype(
            ctx.fp8_meta["recipe"], fprop_tensor=False
        )

        # FP8 case with non-FP8 wgrad
        if (
            gather_grad_output
            and ctx.fp8_meta["recipe"].override_linear_precision.wgrad
        ):
            assert (
                not ctx.ub_split_ag
            ), "override_linear_precision.wgrad not supported with ub_split_ag"
            grad_output_mat, _ = gather_along_first_dim(grad_output_mat, ctx.tp_group)
        # FP8 case with gather: unfused bgrad, cast, transpose for efficient gather
        elif gather_grad_output:
            if ctx.use_bias:
                grad_bias = grad_output_mat.sum(dim=0)
            else:
                grad_bias = None
            if ctx.ub_split_ag:
                grad_output_c = ctx.ub_obj_gradout.get_ubuf_output(0)
            else:
                grad_output_c = torch.empty_like(grad_output_mat, dtype=torch.uint8)
            cast_to_fp8(
                grad_output_mat,
                ctx.fp8_meta["scaling_bwd"],
                tex.FP8BwdTensors.GRAD_OUTPUT1,
                fp8_dtype_backward,
                out=grad_output_c,
            )
            if not ctx.ub_split_ag:
                grad_output_c, _ = gather_along_first_dim(grad_output_c, ctx.tp_group)
                grad_output_t = tex.fp8_transpose(grad_output_c, fp8_dtype_backward)
            else:
                grad_output_c = ctx.ub_obj_gradout.get_ubuf_output(1)
                grad_output_t = None

            return grad_output_mat, grad_output_c, grad_output_t, grad_bias

        # FP8 case without gather: cast, transpose, bgrad fused
        if ctx.use_bias:
            grad_bias, grad_output_c, grad_output_t = fp8_cast_transpose_bgrad_fused(
                grad_output_mat,
                ctx.fp8_meta["scaling_bwd"],
                tex.FP8BwdTensors.GRAD_OUTPUT1,
                fp8_dtype_backward,
            )
        else:
            if not ctx.fp8_meta["recipe"].override_linear_precision.wgrad:
                grad_output_c, grad_output_t = fp8_cast_transpose_fused(
                    grad_output_mat,
                    ctx.fp8_meta["scaling_bwd"],
                    tex.FP8BwdTensors.GRAD_OUTPUT1,
                    fp8_dtype_backward,
                )
            else:
                grad_output_t = None
                grad_output_c = cast_to_fp8(
                    grad_output_mat,
                    ctx.fp8_meta["scaling_bwd"],
                    tex.FP8BwdTensors.GRAD_OUTPUT1,
                    fp8_dtype_backward,
                )
            grad_bias = None

        return grad_output_mat, grad_output_c, grad_output_t, grad_bias

    def noop_cat(self, buffer_name: str, pnames: List[str]) -> torch.Tensor:
        """No-op replacement of `torch.cat`. The buffer and split parameters must occupy
           the same memory region. If this is not the case, then the split parameters
           are concatenated and the buffer is overwritten. The parameters' memory is then
           re-assigned to point to the buffer to avoid subsequent concatenations.
        """

        assert hasattr(self, buffer_name), f"No buffer named {buffer_name}"
        full_param_buffer = getattr(self, buffer_name)
        split_size = full_param_buffer.shape[0] // len(pnames)
        params = [getattr(self, name) for name in pnames]
        for i, p in enumerate(params):
            if p.data.data_ptr() != full_param_buffer[i*split_size : (i+1)*split_size].data_ptr():
                with torch.no_grad():
                    setattr(self, buffer_name, torch.cat(params))
                    for j, pname in enumerate(pnames):
                        full_param_buffer = getattr(self, buffer_name)
                        setattr(self, pname,
                                Parameter(full_param_buffer[j*split_size : (j+1)*split_size]))
                break

        return _NoopCat.apply(getattr(self, buffer_name), *[getattr(self, name) for name in pnames])

    @abstractmethod
    def forward(self):
        """Needs override."""