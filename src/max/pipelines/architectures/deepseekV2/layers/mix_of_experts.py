# ===----------------------------------------------------------------------=== #
# Copyright (c) 2025, Modular Inc. All rights reserved.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions:
# https://llvm.org/LICENSE.txt
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===----------------------------------------------------------------------=== #

"""Mixture of Experts Layer."""

from max.dtype import DType
from max.graph import DeviceRef, TensorValue, Weight, ops
from max.nn.layer import Module
from max.nn.linear import LinearV2
from max.pipelines.architectures.deepseekV2.layers.moe_gate import MaxMoEGate


class MoE(Module):
    num_experts_per_tok: int
    """Number of experts to router each token to."""

    ep_size: int
    """Size of expert parallel group."""

    experts_per_rank: int
    """Number of experts per rank."""

    moe_intermediate_size: int
    """Hidden dimension size for MoE intermediate layer."""

    max_position_embeddings: int
    """Maximum sequence length."""

    n_shared_experts: int
    """Number of shared experts."""

    def __init__(
        self,
        device: DeviceRef,
        num_experts_per_tok: int = 6,
        ep_size: int = 1,
        experts_per_rank: int = 64,
        moe_intermediate_size: int = 1408,
        max_position_embeddings: int = 2048,
        n_shared_experts: int = 2,
        dtype: DType = DType.bfloat16,
    ):
        """
        Args:
            device: The device the experts are on.
            num_experts_per_tok: Number of experts to route each token to.
            ep_size: Size of expert parallel group.
            experts_per_rank: Number of experts per rank.
            moe_intermediate_size: Hidden dimension size for MoE intermediate layer.
            max_position_embeddings: Maximum sequence length.
        """
        super().__init__()
        self.num_experts_per_tok = num_experts_per_tok
        self.ep_size = ep_size
        self.experts_per_rank = experts_per_rank
        self.moe_intermediate_size = moe_intermediate_size
        self.max_position_embeddings = max_position_embeddings
        self.n_shared_experts = n_shared_experts
        self.gate = MaxMoEGate(device)

        # Initialize the weights for the MoE layer
        self.gate_proj, self.down_proj, self.up_proj = [], [], []

        # Routed experts weights
        for i in range(self.experts_per_rank):
            d = Weight(
                name=f"experts.{i}.down_proj.weight",
                shape=(
                    self.max_position_embeddings,
                    self.moe_intermediate_size,
                ),
                dtype=dtype,
                device=device,
            )
            setattr(self, f"experts.{i}.down_proj", d)
            self.down_proj.append(d)

            g = Weight(
                name=f"experts.{i}.gate_proj.weight",
                shape=(
                    self.moe_intermediate_size,
                    self.max_position_embeddings,
                ),
                dtype=dtype,
                device=device,
            )
            setattr(self, f"experts.{i}.gate_proj", g)
            self.gate_proj.append(g)

            u = Weight(
                name=f"experts.{i}.up_proj.weight",
                shape=(
                    self.moe_intermediate_size,
                    self.max_position_embeddings,
                ),
                dtype=dtype,
                device=device,
            )
            setattr(self, f"experts.{i}.up_proj", u)
            self.up_proj.append(u)

        # Shared experts weights
        self.shared_expert_up_proj = LinearV2(
            in_dim=self.max_position_embeddings,
            out_dim=self.moe_intermediate_size * self.n_shared_experts,
            dtype=dtype,
            device=device,
        )
        self.shared_expert_down_proj = LinearV2(
            in_dim=self.moe_intermediate_size * self.n_shared_experts,
            out_dim=self.max_position_embeddings,
            dtype=dtype,
            device=device,
        )
        self.shared_expert_gate_proj = LinearV2(
            in_dim=self.max_position_embeddings,
            out_dim=self.moe_intermediate_size * self.n_shared_experts,
            dtype=dtype,
            device=device,
        )

    def __call__(self, hidden_states: TensorValue):
        """Mixture of Experts Layer.

        Args:
            hidden_states: Input tensor of shape (batch_size, seq_length, hidden_size)

        Returns:
            Output tensor of shape (batch_size, seq_length, hidden_size)
        """
        identity = hidden_states
        # Get the topk experts per token and their weights
        topk_idx, topk_weight = self.gate(hidden_states)

        # Returns a list of weights for each expert
        # (n_routed_experts, h, w)
        down_proj = ops.stack(self.down_proj, axis=0)
        gate_proj = ops.stack(self.gate_proj, axis=0)
        up_proj = ops.stack(self.up_proj, axis=0)

        # Gather the weights for the topk experts for each token
        # (seq_len, k, h, w)
        topk_down_proj = ops.gather(down_proj, topk_idx, axis=0)
        topk_gate_proj = ops.gather(gate_proj, topk_idx, axis=0)
        topk_up_proj = ops.gather(up_proj, topk_idx, axis=0)

        # Unsqueeze the hidden states to match the shape of the topk weights
        # (seq_len, w) -> (seq_len, 1, w, 1)
        hidden_states = ops.unsqueeze(
            ops.unsqueeze(hidden_states[0], axis=1), axis=-1
        )

        # (seq_len, k, h, w) @ (seq_len, 1, w, 1) -> (seq_len, k, h, 1)
        up_projs = topk_up_proj @ hidden_states

        # (seq_len, k, h, w) @ (seq_len, 1, w, 1) -> (seq_len, k, h, 1)
        gate_projs = topk_gate_proj @ hidden_states

        # apply silu to gate_projs.
        gate_projs = ops.silu(gate_projs)

        # (seq_len, k, h, 1) * (seq_len, k, h, 1) -> (seq_len, k, h, 1)
        up_gate_projs = up_projs * gate_projs

        # (seq_len, k, w, h) @ (seq_len, k, h, 1) -> (seq_len, k, w)
        down_projs = ops.squeeze(topk_down_proj @ up_gate_projs, axis=-1).cast(
            topk_weight.dtype
        )
        topk_weight = ops.unsqueeze(topk_weight, axis=1)

        # (seq_len, 1, k) @ (seq_len, k, w) -> (seq_len, 1, w)
        summed_down_projs = (topk_weight @ down_projs).cast(identity.dtype)
        final_out = ops.squeeze(summed_down_projs, axis=1)

        # TODO(MODELS-396): Probably should be a MLPV2 layer
        shared_expert_out = self.shared_expert_down_proj(
            ops.silu(self.shared_expert_gate_proj(identity))
            * self.shared_expert_up_proj(identity)
        )

        return final_out + shared_expert_out
