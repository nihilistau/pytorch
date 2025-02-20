# Copyright (c) Meta Platforms, Inc. and affiliates
from typing import List, Optional, Sequence, Tuple

import torch

from torch.distributed._tensor.placement_types import (
    DTensorSpec,
    Placement,
    Replicate,
    Shard,
    _Partial,
)
from torch.distributed._tensor.op_schema import OpSchema, OutputSharding
from torch.distributed._tensor.ops.utils import register_prop_rule
from torch.distributed._tensor.ops.common_rules import pointwise_rule

aten = torch.ops.aten  # pyre-ignore


@register_prop_rule(  # pyre-ignore
    [
        aten._foreach_neg.default,
        aten._foreach_reciprocal.default,
        aten._foreach_sqrt.default,
    ]
)
def _prop__foreach_unaop(op_schema: OpSchema) -> OutputSharding:
    self = op_schema.args_schema[0]
    assert isinstance(self, list) and all(
        [isinstance(s, DTensorSpec) for s in self]
    )
    # FIXME(@mrshenli): for sqrt, this is only mathematically correct for
    # Replicate and Shard tensor.
    return OutputSharding(output_spec=self)


@register_prop_rule(  # pyre-ignore
    [
        aten._foreach_add.List,
        aten._foreach_div.List,
        aten._foreach_mul.List,
    ]
)
def _prop__foreach_binop_list(op_schema: OpSchema) -> OutputSharding:
    self, other = op_schema.args_schema[:2]
    scalar = (
        None if len(op_schema.args_schema) < 3 else op_schema.args_schema[2]
    )
    assert isinstance(self, list) and all(
        [isinstance(s, DTensorSpec) for s in self]
    ), f"Expect a List[DTensorSpec] but got {self}"
    assert isinstance(other, list) and all(
        [isinstance(o, DTensorSpec) for o in other]
    ), f"Expect a List[DTensorSpec] but got {other}"
    assert len(self) == len(other), (
        "Two tensor lists must match in length, "
        f"but got {len(self)} and {len(other)}"
    )

    if any([s != o for s, o in zip(self, other)]):
        # If DTensorSpec for the two operand do not match, suggest using
        # self's DTensorSpec. This will trigger allreduce if other is partial
        # and self is replicated.
        return OutputSharding(
            output_spec=None,
            schema_suggestions=[
                OpSchema(
                    func_schema=op_schema.func_schema,
                    args_schema=(self, self, scalar)
                    if scalar
                    else (self, self),
                    kwargs_schema=op_schema.kwargs_schema,
                    is_inplace=op_schema.is_inplace,
                    is_out_variant=op_schema.is_out_variant,
                )
            ],
        )
    else:
        return OutputSharding(output_spec=self)


@register_prop_rule(  # pyre-ignore
    [
        aten._foreach_add.Scalar,
        aten._foreach_div.Scalar,
        aten._foreach_mul.Scalar,
        aten._foreach_sub.Scalar,
    ]
)
def _prop__foreach_binop_scalar(op_schema: OpSchema) -> OutputSharding:
    self, scalar = op_schema.args_schema
    assert isinstance(self, list) and all(
        [isinstance(s, DTensorSpec) for s in self]
    )
    assert not isinstance(scalar, list)
    return OutputSharding(output_spec=self)


@register_prop_rule(  # pyre-ignore
    [
        aten._foreach_addcdiv.Scalar,
        aten._foreach_addcmul.Scalar,
    ]
)
def _prop__foreach_addcop_scalar(op_schema: OpSchema):
    self, tensor1, tensor2 = op_schema.args_schema[:3]
    scalar = (
        None if len(op_schema.args_schema) < 4 else op_schema.args_schema[3]
    )
    assert isinstance(self, list) and all(
        [isinstance(s, DTensorSpec) for s in self]
    )
    assert isinstance(tensor1, list) and all(
        [isinstance(s, DTensorSpec) for s in self]
    )
    assert isinstance(tensor2, list) and all(
        [isinstance(s, DTensorSpec) for s in self]
    )
    if any([s != t1 or s != t2 for s, t1, t2 in zip(self, tensor1, tensor2)]):
        # If DTensorSpec for the two operand do not match, suggest using
        # self's DTensorSpec. This will trigger allreduce if other is partial
        # and self is replicated.
        return OutputSharding(
            output_spec=None,
            schema_suggestions=[
                OpSchema(
                    func_schema=op_schema.func_schema,
                    args_schema=(self, self, self, scalar)
                    if scalar
                    else (self, self, self),
                    kwargs_schema=op_schema.kwargs_schema,
                    is_inplace=op_schema.is_inplace,
                    is_out_variant=op_schema.is_out_variant,
                )
            ],
        )
    else:
        return OutputSharding(output_spec=self)


@register_prop_rule([aten._foreach_pow.ScalarAndTensor])  # pyre-ignore
def _prop__foreach_pow_scalar_and_tensor(op_schema: OpSchema):
    scala, exponent = op_schema.args_schema
    assert isinstance(exponent, list) and all(
        [isinstance(s, DTensorSpec) for s in exponent]
    )
    return OutputSharding(output_spec=exponent)


@register_prop_rule([aten._fused_adam.default])  # pyre-ignore
def _prop__fused_adam(op_schema: OpSchema):
    NT = 5
    tesnor_list_args: Tuple[List[DTensorSpec]] = op_schema.args_schema[:NT]  # type: ignore[assignment]

    assert all([isinstance(schema, list) for schema in tesnor_list_args])
    assert all(
        [
            isinstance(s, DTensorSpec)
            for schema in tesnor_list_args
            for s in schema
        ]
    )

    tensor_schemas: Tuple[List[DTensorSpec]] = [  # type: ignore[assignment]
        schema for schema in tesnor_list_args if len(schema)
    ]

    assert all([len(s) == len(tensor_schemas[0]) for s in tensor_schemas]), (
        "expect the same number of gradients and states, but got "
        f"{[len(s) for s in tensor_schemas]}."
    )

    if any([any([t != ts[0] for t in ts]) for ts in zip(*tensor_schemas)]):
        new_schemas: Tuple[List[DTensorSpec]] = tuple(  # type: ignore[assignment]
            op_schema.args_schema[0] if len(s) else s for s in tesnor_list_args
        )
        return OutputSharding(
            output_spec=None,
            schema_suggestions=[
                OpSchema(
                    func_schema=op_schema.func_schema,
                    args_schema=new_schemas + op_schema.args_schema[NT:],
                    kwargs_schema=op_schema.kwargs_schema,
                    is_inplace=op_schema.is_inplace,
                    is_out_variant=op_schema.is_out_variant,
                )
            ],
        )
    else:
        return OutputSharding(output_spec=(op_schema.args_schema[0],) * NT)  # type: ignore[arg-type]


@register_prop_rule(aten.native_layer_norm.default)  # pyre-ignore
def _prop_native_layer_norm(op_schema: OpSchema) -> OutputSharding:
    input, normalized_shape, weight, bias, eps = op_schema.args_schema
    assert isinstance(input, DTensorSpec)
    assert isinstance(weight, DTensorSpec)
    assert isinstance(bias, DTensorSpec)
    assert isinstance(normalized_shape, (tuple, list))
    assert all(isinstance(p, Replicate) for p in weight.placements)
    assert all(isinstance(p, Replicate) for p in bias.placements)
    # only the left-most (non-normalized) dimensions of the input can be sharded
    batch_ndim = len(input.shape) - len(normalized_shape)
    assert all(
        isinstance(p, Replicate)
        or (isinstance(p, Shard) and p.dim < batch_ndim,)
        for p in input.placements
    )
    stats_spec = DTensorSpec(
        mesh=weight.mesh,
        placements=input.placements,
    )
    return OutputSharding(output_spec=(input, stats_spec, stats_spec))


@register_prop_rule(aten.native_layer_norm_backward.default)  # pyre-ignore
def _prop_native_layer_norm_backward(op_schema: OpSchema) -> OutputSharding:
    (
        grad,
        input,
        normalized_shape,
        result1,
        result2,
        weight,
        bias,
        grad_input_mask,
    ) = op_schema.args_schema
    assert isinstance(grad, DTensorSpec)
    assert isinstance(weight, DTensorSpec)
    assert isinstance(bias, DTensorSpec)
    assert isinstance(grad_input_mask, (list, tuple))
    assert all(isinstance(s, Replicate) for s in weight.placements)
    assert all(isinstance(s, Replicate) for s in bias.placements)
    # ensure sharding on dim 0, which will trigger the "Partial" output on weight and bias grads
    assert any(
        isinstance(s, Shard) and s.dim == 0 for s in grad.placements
    ), f"Got {grad.placements}"
    weight_grad = DTensorSpec(
        mesh=weight.mesh,
        placements=[_Partial()] * weight.mesh.ndim,
    )
    bias_grad = DTensorSpec(
        mesh=bias.mesh,
        placements=[_Partial()] * bias.mesh.ndim,
    )
    return OutputSharding(
        # NOTE: type errors below are legit. This is because DTensor currently
        # doesn't support Optional return values. Need to be fixed in DTensor repo.
        output_spec=(
            grad if grad_input_mask[0] else None,
            weight_grad if grad_input_mask[1] else None,
            bias_grad if grad_input_mask[2] else None,
        ),
    )


def _refine_sharding(
    op_schema: OpSchema, active_dim: Optional[int]
) -> Sequence[Placement]:
    """
    Considers 2 first inputs of op_schema as having same shape,
    and returns suggested placement for a pointwise operation.
    """
    # consider the operating dimension as a singleton to prevent sharding on it
    # however, if active_dim is None, this means the input and output shapes are equal and
    # we'll apply exactly the pointwise rule.
    from torch.fx.passes.shape_prop import TensorMetadata

    args_schema = []
    for s in op_schema.args_schema[:2]:
        assert isinstance(s, DTensorSpec) and s.tensor_meta is not None
        args_schema.append(
            DTensorSpec(
                mesh=s.mesh,  # type: ignore[attr-defined]
                placements=s.placements,  # type: ignore[attr-defined]
                tensor_meta=TensorMetadata(
                    shape=torch.Size(
                        s.shape[0:active_dim] + (1,) + s.shape[active_dim + 1 :]
                    )
                    if active_dim is not None
                    else s.shape,
                    dtype=s.tensor_meta.dtype,
                    requires_grad=s.tensor_meta.requires_grad,
                    stride=s.tensor_meta.stride,
                    memory_format=s.tensor_meta.memory_format,
                    is_quantized=s.tensor_meta.is_quantized,
                    qparams=s.tensor_meta.qparams,
                ),
            )
        )

    op_schema = OpSchema(
        func_schema=op_schema.func_schema,
        args_schema=args_schema,  # type: ignore[arg-type]
        kwargs_schema={},
        is_inplace=op_schema.is_inplace,
        is_out_variant=op_schema.is_out_variant,
    )
    output_sharding = pointwise_rule(op_schema, linearity=False)
    if output_sharding.output_spec:
        assert isinstance(output_sharding.output_spec, DTensorSpec)
        return output_sharding.output_spec.placements
    else:
        assert output_sharding.schema_suggestions is not None
        out_schema = output_sharding.schema_suggestions[0].args_schema[0]
        assert isinstance(out_schema, DTensorSpec)
        return tuple(out_schema.placements)


@register_prop_rule(aten.slice_scatter.default)  # pyre-ignore
def prop_slice_scatter(op_schema: OpSchema) -> OutputSharding:
    # 1. number of dimensions in input and src need to match.
    # 2. number of elements on all non-dim need to match between input and src.
    # 3. numer of elements in src in dim need to match the slice size.
    # Given the above:
    # - We suggest for src to follow the sharding of input, except on the scatter dimension,
    #   where our best bet for now is to make them replicated as a fall-back.
    #   TODO: Ideally we'd like to make sure the output is re-sharded afterwards to keep input sharding.

    defaults = (None, None, 0, None, None, 1)
    input, src, dim, start, end, step = (
        op_schema.args_schema + defaults[len(op_schema.args_schema) :]
    )
    assert isinstance(input, DTensorSpec)
    assert isinstance(src, DTensorSpec)
    assert isinstance(dim, int)

    if dim < 0:
        dim += input.ndim

    # if the input shape and the output shape are the same on the operating dimension,
    # this is effectively a no-op, so we just propagate sharding as we would do for
    # pointwise, no exceptions.
    if input.shape[dim] == src.shape[dim]:
        assert start == 0
        assert end >= src.shape[dim]  # type: ignore[operator]
        dim = None

    # apply sharding refinement as implemented in pointwise_rule
    input_suggestion = list(_refine_sharding(op_schema, dim))
    # apply the exception -- disallow sharding on the operating dimension.
    for i, p in enumerate(input_suggestion):
        if isinstance(p, Shard) and p.dim == dim:
            input_suggestion[i] = Replicate()
    input_suggestion = tuple(input_suggestion)  # type: ignore[assignment]

    if input_suggestion == tuple(input.placements) and src.placements == tuple(
        input.placements
    ):
        # if our sharding is correct, the output sharding will be the same as the input.
        return OutputSharding(
            output_spec=DTensorSpec(
                mesh=input.mesh,
                placements=input.placements,
            )
        )
    else:
        # otherwise, return the suggestion.
        return OutputSharding(
            output_spec=None,
            schema_suggestions=[
                OpSchema(
                    func_schema=op_schema.func_schema,
                    args_schema=(
                        DTensorSpec(
                            mesh=input.mesh,
                            placements=input_suggestion,
                            tensor_meta=input.tensor_meta,
                        ),
                        DTensorSpec(
                            mesh=src.mesh,
                            placements=input_suggestion,
                            tensor_meta=src.tensor_meta,
                        ),
                    )
                    + op_schema.args_schema[2:],
                    kwargs_schema=op_schema.kwargs_schema,
                )
            ],
        )
