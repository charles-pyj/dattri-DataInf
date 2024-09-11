"""This module implement the influence function."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Callable, Dict, List, Optional, Tuple

    from torch import Tensor
    from torch.utils.data import DataLoader

    from dattri.task import AttributionTask

import math
import warnings
from functools import partial

import torch
from torch.func import grad, vmap
from tqdm import tqdm

from dattri.algorithm.utils import _check_shuffle
from dattri.func.hessian import ihvp_arnoldi, ihvp_cg, ihvp_explicit, ihvp_lissa
from dattri.func.utils import flatten_params

from .base import BaseAttributor, BaseInnerProductAttributor


def _lissa_collate_fn(
    sampled_input: List[Tensor],
) -> Tuple[Tensor, List[Tuple[Tensor, ...]]]:
    """Collate function for LISSA.

    Args:
        sampled_input (List[Tensor]): The sampled input from the dataloader.

    Returns:
        Tuple[Tensor, List[Tuple[Tensor, ...]]]: The collated input for the LISSA.
    """
    return (
        sampled_input[0],
        tuple(sampled_input[i].float() for i in range(1, len(sampled_input))),
    )


SUPPORTED_IHVP_SOLVER = {
    "explicit": ihvp_explicit,
    "cg": ihvp_cg,
    "arnoldi": ihvp_arnoldi,
    "lissa": partial(ihvp_lissa, collate_fn=_lissa_collate_fn),
}


class IFAttributor(BaseAttributor):
    """Influence function attributor."""

    def __init__(
        self,
        target_func: Callable,
        params: dict,
        ihvp_solver: str = "explicit",
        ihvp_kwargs: Optional[Dict[str, Any]] = None,
        device: str = "cpu",
    ) -> None:
        """Influence function attributor.

        Args:
            target_func (Callable): The target function to be attributed.
                The function can be quite flexible in terms of what is calculate,
                but it should take the parameters and the dataloader as input.
                A typical example is as follows:
                ```python
                @flatten_func(model)
                def f(params, dataloader):
                    loss = nn.CrossEntropyLoss()
                    loss_val = 0
                    for image, label in dataloader:
                        yhat = torch.func.functional_call(model, params, image)
                        loss_val += loss(yhat, label)
                    return loss_val
                ```.
                This examples calculates the loss of the model on the dataloader.
            params (dict): The parameters of the target function. The key is the
                name of a parameter and the value is the parameter tensor.
                TODO: This should be changed to support a list of parameters or
                    paths for ensembling and memory efficiency.
            ihvp_solver (str): The solver for inverse hessian vector product
                calculation, currently we only support "explicit", "cg" and "arnoldi".
            ihvp_kwargs (Optional[Dict[str, Any]]): Keyword arguments for ihvp solver.
                calculation, currently we only support "explicit", "cg", "arnoldi",
                and "lissa".
            device (str): The device to run the attributor. Default is cpu.
        """
        self.target_func = target_func
        self.params = flatten_params(params)
        if ihvp_kwargs is None:
            ihvp_kwargs = {}
        self.ihvp_solver_name = ihvp_solver
        self.ihvp_solver = partial(SUPPORTED_IHVP_SOLVER[ihvp_solver], **ihvp_kwargs)
        self.device = device
        self.full_train_dataloader = None
        self.grad_func = vmap(grad(self.target_func), in_dims=(None, 1))

    def cache(self, full_train_dataloader: DataLoader) -> None:
        """Cache the dataset for inverse hessian calculation.

        Args:
            full_train_dataloader (DataLoader): The dataloader
                with full training samples for inverse hessian calculation.
        """
        self.full_train_dataloader = full_train_dataloader

    def attribute(
        self,
        train_dataloader: DataLoader,
        test_dataloader: DataLoader,
    ) -> torch.Tensor:
        """Calculate the influence of the training set on the test set.

        Args:
            train_dataloader (DataLoader): The dataloader for
                training samples to calculate the influence. It can be a subset
                of the full training set if `cache` is called before. A subset
                means that only a part of the training set's influence is calculated.
                The dataloader should not be shuffled.
            test_dataloader (DataLoader): The dataloader for
                test samples to calculate the influence. The dataloader should not\
                be shuffled.

        Returns:
            torch.Tensor: The influence of the training set on the test set, with
                the shape of (num_train_samples, num_test_samples).

        """
        super().attribute(train_dataloader, test_dataloader)
        if self.full_train_dataloader is None:
            self.full_train_dataloader = train_dataloader
            warnings.warn(
                "The full training data loader was NOT cached. \
                           Treating the train_dataloader as the full training \
                           data loader.",
                stacklevel=1,
            )

        _check_shuffle(train_dataloader)
        _check_shuffle(test_dataloader)

        tda_output = torch.zeros(
            size=(len(train_dataloader.sampler), len(test_dataloader.sampler)),
            device=self.device,
        )

        # TODO: sometimes the train dataloader could be swapped with the test dataloader
        # prepare a checkpoint-specific seed

        for train_batch_idx, train_batch_data_ in enumerate(
            tqdm(
                train_dataloader,
                desc="calculating gradient of training set...",
                leave=False,
            ),
        ):
            # get gradient of train
            train_batch_data = tuple(
                data.to(self.device).unsqueeze(0) for data in train_batch_data_
            )

            train_batch_grad = self.grad_func(self.params, train_batch_data)

            for test_batch_idx, test_batch_data_ in enumerate(
                tqdm(
                    test_dataloader,
                    desc="calculating gradient of training set...",
                    leave=False,
                ),
            ):
                # get gradient of test
                test_batch_data = tuple(
                    data.to(self.device).unsqueeze(0) for data in test_batch_data_
                )

                test_batch_grad = self.grad_func(self.params, test_batch_data)

                ihvp = 0
                # currently full-batch is considered
                # so only one iteration
                for full_data_ in self.full_train_dataloader:
                    # move to device
                    full_data = tuple(data.to(self.device) for data in full_data_)

                    if self.ihvp_solver_name == "lissa":
                        self.ihvp_func = self.ihvp_solver(
                            self.target_func,
                        )
                        ihvp += self.ihvp_func(
                            (self.params, *full_data),
                            test_batch_grad,
                            in_dims=(None,) + (0,) * len(full_data),
                        ).detach()
                    else:
                        self.ihvp_func = self.ihvp_solver(
                            partial(self.target_func, data_target_pair=full_data),
                        )
                        ihvp += self.ihvp_func((self.params,), test_batch_grad).detach()

                # results position based on batch info
                row_st = train_batch_idx * train_dataloader.batch_size
                row_ed = min(
                    (train_batch_idx + 1) * train_dataloader.batch_size,
                    len(train_dataloader.sampler),
                )

                col_st = test_batch_idx * test_dataloader.batch_size
                col_ed = min(
                    (test_batch_idx + 1) * test_dataloader.batch_size,
                    len(test_dataloader.sampler),
                )

                tda_output[row_st:row_ed, col_st:col_ed] += train_batch_grad @ ihvp.T

        return tda_output


class IFAttributorExplicit(BaseInnerProductAttributor):
    """The inner product attributor with explicit inverse hessian transformation."""

    def __init__(
        self,
        task: AttributionTask,
        device: Optional[str] = "cpu",
        regularization: float = 0.0,
    ) -> None:
        """Initialize the explicit inverse hessian attributor.

        Args:
            task (AttributionTask): The task to be attributed. The task should
                be an instance of `AttributionTask`.
            device (str): The device to run the attributor. Default is cpu.
            regularization (float): A float default to 0.0. Specifies the regularization
                term to be added to the Hessian matrix. This is useful when the Hessian
                matrix is singular or ill-conditioned. The regularization term is
                `regularization * I`, where `I` is the identity matrix directly added
                to the Hessian matrix.
        """
        super().__init__(task, device)
        self.transformation_kwargs = {
            "regularization": regularization,
        }

    def transform_test_rep(
        self,
        ckpt_idx: int,
        test_rep: torch.Tensor,
    ) -> torch.Tensor:
        """Calculate the transformation on the test rep through ihvp_explicit.

        Args:
            ckpt_idx (int): The index of the model parameters. This index
                is used for ensembling of different trained model.
            test_rep (torch.Tensor): The test representations to be transformed.
                Typically, it is a 2-d dimensional tensor with the shape of
                (batch_size, num_parameters).

        Returns:
            torch.Tensor: The transformation on the query. Normally it is a 2-d
                dimensional tensor with the shape of (batchsize, transformed_dimension).
        """
        from dattri.func.hessian import ihvp_explicit

        vector_product = 0
        model_params, _ = self.task.get_param(ckpt_idx)
        for full_data_ in self.full_train_dataloader:
            # move to device
            full_data = tuple(data.to(self.device) for data in full_data_)
            self.ihvp_func = ihvp_explicit(
                partial(self.task.get_loss_func(), data_target_pair=full_data),
                **self.transformation_kwargs,
            )
            vector_product += self.ihvp_func((model_params,), test_rep).detach()
        return vector_product


class IFAttributorCG(BaseInnerProductAttributor):
    """The inner product attributor with CG inverse hessian transformation."""

    def __init__(
        self,
        task: AttributionTask,
        device: Optional[str] = "cpu",
        max_iter: int = 10,
        tol: float = 1e-7,
        mode: str = "rev-rev",
        regularization: float = 0.0,
    ) -> None:
        """Initialize the CG inverse hessian attributor.

        Args:
            task (AttributionTask): The task to be attributed. The task should
                be an instance of `AttributionTask`.
            device (str): The device to run the attributor. Default is cpu.
            max_iter (int): An integer default 10. Specifies the maximum iteration
                to calculate the ihvp through Conjugate Gradient Descent.
            tol (float): A float default to 1e-7. Specifies the break condition that
                decide if the algorithm has converged. If the torch.norm of residual
                is less than tol, then the algorithm is truncated.
            mode (str): The auto diff mode, which can have one of the following values:
                - rev-rev: calculate the hessian with two reverse-mode auto-diff. It has
                        better compatibility while cost more memory.
                - rev-fwd: calculate the hessian with the composing of reverse-mode and
                        forward-mode. It's more memory-efficient but may not be
                        supported by some operator.
            regularization (float): A float default to 0.0. Specifies the regularization
                term to be added to the Hessian vector product, which is useful for the
                later inverse calculation if the Hessian matrix is singular or
                ill-conditioned. Specifically, the regularization term is
                `regularization * v`.
        """
        super().__init__(task, device)
        self.transformation_kwargs = {
            "max_iter": max_iter,
            "tol": tol,
            "mode": mode,
            "regularization": regularization,
        }

    def transform_test_rep(
        self,
        ckpt_idx: int,
        test_rep: torch.Tensor,
    ) -> torch.Tensor:
        """Calculate the transformation on the test rep through ihvp_cg.

        Args:
            ckpt_idx (int): The index of the model parameters. This index
                is used for ensembling of different trained model.
            test_rep (torch.Tensor): The test representations to be transformed.
                Typically, it is a 2-d dimensional tensor with the shape of
                (batch_size, num_parameters).

        Returns:
            torch.Tensor: The transformation on the query. Normally it is a 2-d
                dimensional tensor with the shape of (batchsize, transformed_dimension).
        """
        from dattri.func.hessian import ihvp_cg

        vector_product = 0
        model_params, _ = self.task.get_param(ckpt_idx)
        for full_data_ in self.full_train_dataloader:
            # move to device
            full_data = tuple(data.to(self.device) for data in full_data_)
            self.ihvp_func = ihvp_cg(
                partial(self.task.get_loss_func(), data_target_pair=full_data),
                **self.transformation_kwargs,
            )
            vector_product += self.ihvp_func((model_params,), test_rep).detach()
        return vector_product


class IFAttributorArnoldi(BaseInnerProductAttributor):
    """The inner product attributor with Arnoldi projection transformation."""

    def __init__(
        self,
        task: AttributionTask,
        device: Optional[str] = "cpu",
        precompute_data_ratio: float = 1.0,
        proj_dim: int = 100,
        max_iter: int = 100,
        norm_constant: float = 1.0,
        tol: float = 1e-7,
        regularization: float = 0.0,
        seed: int = 0,
    ) -> None:
        """Initialize the Arnoldi projection attributor.

        Args:
            task (AttributionTask): The task to be attributed. The task should
                be an instance of `AttributionTask`.
            device (str): The device to run the attributor. Default is cpu.
            precompute_data_ratio (float): A float defaulting to 1.0. Specifies the
                ratio of the full training data to be precomputed for the Arnoldi
                projection.
            proj_dim (int): Dimension after the projection. This corresponds to the
                number of top eigenvalues (top-k eigenvalues) to keep for the
                Hessian approximation.
            max_iter (int): An integer defaulting to 100. Specifies the maximum
                iteration to calculate the ihvp through Arnoldi Iteration.
            norm_constant (float): A float defaulting to 1.0. Specifies a constant
                value for the norm of each projection. In some situations (e.g.
                with a large number of parameters) it might be advisable to set
                norm_constant > 1 to avoid dividing projection components by a
                large normalization factor.
            tol (float): A float defaulting to 1e-7. Specifies the break condition
                that decides if the algorithm has converged. If the torch.norm of
                the current basis vector is less than tol, then the algorithm is
                truncated.
            regularization (float): A float defaulting to 0.0. Specifies the
                regularization term to be added to the Hessian vector product,
                which is useful for the later inverse calculation if the Hessian
                matrix is singular or ill-conditioned. Specifically, the
                regularization term is `regularization * v`.
            seed (int): Random seed used by the projector. Defaults to 0.
        """
        super().__init__(task, device)
        self.precompute_data_ratio = precompute_data_ratio
        self.proj_dim = proj_dim
        self.max_iter = max_iter
        self.norm_constant = norm_constant
        self.tol = tol
        self.regularization = regularization
        self.seed = seed

    def cache(
        self,
        full_train_dataloader: DataLoader,
    ) -> None:
        """Cache the dataset and pre-calculate the Arnoldi projector.

        Args:
            full_train_dataloader (DataLoader): The dataloader with full training data.
        """
        self.full_train_dataloader = full_train_dataloader
        self.arnoldi_projectors = []

        # Assuming that full_train_dataloader has only one batch
        iter_number = math.ceil(len(full_train_dataloader) * self.precompute_data_ratio)
        data_target_pair_list = []
        for _ in range(iter_number):
            data_target_pair_list.append(next(iter(full_train_dataloader)))  # noqa: PERF401

        # concatenate all data
        data_target_pair = data_target_pair_list[0]
        for batch_idx in range(1, len(data_target_pair_list)):
            for data_item_idx in range(len(data_target_pair_list[0])):
                data_target_pair[data_item_idx] = torch.cat(
                    (
                        data_target_pair[data_item_idx],
                        data_target_pair_list[batch_idx][data_item_idx],
                    ),
                    dim=0,
                )

        # to device (only once for all data)
        for data_item_idx in range(len(data_target_pair)):
            data_target_pair[data_item_idx] = data_target_pair[data_item_idx].to(
                self.device,
            )

        func = partial(self.task.get_loss_func(), data_target_pair=data_target_pair)

        from dattri.func.projection import arnoldi_project

        for i in range(len(self.task.get_checkpoints())):
            model_params, _ = self.task.get_param(i)
            self.arnoldi_projectors.append(
                arnoldi_project(
                    feature_dim=len(model_params),
                    func=func,
                    x=model_params,
                    proj_dim=self.proj_dim,
                    max_iter=self.max_iter,
                    norm_constant=self.norm_constant,
                    tol=self.tol,
                    regularization=self.regularization,
                    seed=self.seed,
                    device=self.device,
                ),
            )

    def transform_test_rep(
        self,
        ckpt_idx: int,
        test_rep: torch.Tensor,
    ) -> torch.Tensor:
        """Transform the test representations via Arnoldi projection.

        Args:
            ckpt_idx (int): The index of the model checkpoints. This index
                is used for ensembling different trained model checkpoints.
            test_rep (torch.Tensor): The test representations to be transformed.
                It is a 2-d dimensional tensor with the shape of
                (batch_size, num_params).

        Returns:
            torch.Tensor: The transformed test representations, a 2-d dimensional
                tensor with the shape of (batch_size, proj_dim).

        Raises:
            ValueError: If the Arnoldi projector has not been cached.
        """
        if not hasattr(self, "arnoldi_projectors"):
            error_msg = "The Arnoldi projector has not been cached.\
                         Please call cache() first."
            raise ValueError(error_msg)

        return self.arnoldi_projectors[ckpt_idx](test_rep).detach()


class IFAttributorLiSSA(BaseInnerProductAttributor):
    """The inner product attributor with LiSSA inverse hessian transformation."""

    def __init__(
        self,
        task: AttributionTask,
        device: Optional[str] = "cpu",
        batch_size: int = 1,
        num_repeat: int = 1,
        recursion_depth: int = 5000,
        damping: int = 0.0,
        scaling: int = 50.0,
        mode: str = "rev-rev",
    ) -> None:
        """Initialize the LiSSA inverse hessian attributor.

        Args:
            task (AttributionTask): The task to be attributed. The task should
                be an instance of `AttributionTask`.
            device (str): The device to run the attributor. Default is cpu.
            batch_size (int): An integer default to 1. Specifies the batch size used
                for LiSSA inner loop update.
            num_repeat (int): An integer default to 1. Specifies the number of samples
                of the hvp approximation to average on.
            recursion_depth (int): A integer default to 5000. Specifies the number of
                recursions used to estimate each IHVP sample.
            damping (int): Damping factor used for non-convexity in LiSSA IHVP
                calculation.
            scaling (int): Scaling factor used for convergence in LiSSA IHVP
                calculation.
            mode (str): The auto diff mode, which can have one of the following values:
                - rev-rev: calculate the hessian with two reverse-mode auto-diff. It has
                        better compatibility while cost more memory.
                - rev-fwd: calculate the hessian with the composing of reverse-mode and
                        forward-mode. It's more memory-efficient but may not be
                        supported by some operator.
        """
        super().__init__(task, device)
        self.transformation_kwargs = {
            "batch_size": batch_size,
            "num_repeat": num_repeat,
            "recursion_depth": recursion_depth,
            "damping": damping,
            "scaling": scaling,
            "mode": mode,
        }

    def transform_test_rep(
        self,
        ckpt_idx: int,
        test_rep: torch.Tensor,
    ) -> torch.Tensor:
        """Calculate the transformation on the test rep through ihvp_lissa.

        Args:
            ckpt_idx (int): The index of the model parameters. This index
                is used for ensembling of different trained model.
            test_rep (torch.Tensor): The test representations to be transformed.
                Typically, it is a 2-d dimensional tensor with the shape of
                (batch_size, num_parameters).

        Returns:
            torch.Tensor: The transformation on the query. Normally it is a 2-d
                dimensional tensor with the shape of (batchsize, transformed_dimension).
        """
        from dattri.func.hessian import ihvp_lissa

        vector_product = 0
        model_params, _ = self.task.get_param(ckpt_idx)
        for full_data_ in self.full_train_dataloader:
            # move to device
            full_data = tuple(data.to(self.device) for data in full_data_)
            self.ihvp_func = ihvp_lissa(
                self.task.get_loss_func(),
                collate_fn=IFAttributorLiSSA.lissa_collate_fn,
                **self.transformation_kwargs,
            )
            vector_product += self.ihvp_func(
                (model_params, *full_data),
                test_rep,
                in_dims=(None,) + (0,) * len(full_data),
            ).detach()
        return vector_product

    @staticmethod
    def lissa_collate_fn(
        sampled_input: List[Tensor],
    ) -> Tuple[Tensor, List[Tuple[Tensor, ...]]]:
        """Collate function for LISSA.

        Args:
            sampled_input (List[Tensor]): The sampled input from the dataloader.

        Returns:
            Tuple[Tensor, List[Tuple[Tensor, ...]]]: The collated input for the LISSA.
        """
        return (
            sampled_input[0],
            tuple(sampled_input[i].float() for i in range(1, len(sampled_input))),
        )


class IFAttributorDataInf(BaseInnerProductAttributor):
    """The attributor using DataInf."""

    def __init__(
        self,
        task: AttributionTask,
        device: Optional[str] = "cpu",
        regularization: float = 0.0,
    ) -> None:
        """Initialize the explicit inverse hessian attributor.

        Args:
            task (AttributionTask): The task to be attributed. The task should
                be an instance of `AttributionTask`.
            device (str): The device to run the attributor. Default is cpu.
            regularization (float): A float default to 0.0. Specifies the regularization
                term to be added to the Hessian vector product, which is useful for the
                later inverse calculation if the Hessian matrix is singular or
                ill-conditioned. Specifically, the regularization term is
                `regularization * v`.
        """
        super().__init__(task, device)
        self.transformation_kwargs = {
            "regularization": regularization,
        }

    def transform_test_rep(
        self,
        ckpt_idx: int,
        full_train_rep: torch.Tensor,
        test_rep: torch.Tensor,
    ) -> torch.Tensor:
        """Calculate the transformation on the query through ifvp_datainf.

        Args:
            ckpt_idx (int): The index of the model parameters. This index
                is used for ensembling of different trained model.
            test_rep (torch.Tensor): The test representations to be transformed.
                Typically, it is a 2-d dimensional tensor with the shape of
                (batch_size, num_parameters).

        Returns:
            torch.Tensor: The transformation on the query. Normally it is a 2-d
                dimensional tensor with the shape of (batchsize, transformed_dimension).
        """

        def _single_datainf(
            v: torch.Tensor,
            grad: torch.Tensor,
            regularization: float,
        ) -> torch.Tensor:
            """Intermediate DataInf value calculation of a single data point.

            Args:
                v (torch.Tensor): A tensor representing (batched) validation
                    set gradient,Normally of shape (num_valiation,parameter_size)
                grad (torch.Tensor): A tensor representing a single training
                    gradient, of shape (parameter_size,)
                regularization (float): A float or list of floats default
                    to 0.0.Specifies the regularization term to be added to
                    the Hessian matrix in each layer.

            Returns:
                A tensor corresponding to intermediate influence value. This value
                    will later to aggregated to obtain final influence.
            """
            #print(type(grad))
            grad = grad.unsqueeze(-1)
            coef = (v @ grad) / (regularization + torch.norm(grad) ** 2)
            return (v - coef @ grad.T) / regularization
        regularization = self.transformation_kwargs['regularization']
        query_split = self.get_layer_wise_grads(ckpt_idx, test_rep)
        train_grad_split = self.get_layer_wise_grads(ckpt_idx, full_train_rep)
        layer_cnt = len(train_grad_split)
        #print(f"Layer cnt: {layer_cnt}")
        #print(train_grad_split[0].shape)
        ifvps = []
        # Using test batch size, should be practically suitable.
        # Peak memory usage: test_batch_size * test_batch_size * max_num_parameter
        batch_size = test_rep.shape[0]
        for layer in range(layer_cnt):
            grad_layer = train_grad_split[layer]
            #print(type(grad_layer))
            #print(grad_layer.shape)
            length = grad_layer.shape[0]
            grad_batches = grad_layer.split(batch_size, dim=0)
            running_contributions = 0

            for batch in grad_batches:
                #print(batch.shape)
                reg = 0.1 if regularization is None else regularization
                contribution = torch.func.vmap(
                    lambda grad, layer=layer, reg=reg: _single_datainf(
                    query_split[layer],
                    grad,
                    reg,
                ),
                )(batch)
                running_contributions += contribution.sum(dim=0)
            running_contributions /= length
            ifvps.append(running_contributions)
        return torch.cat(ifvps, dim=1)

    def get_layer_wise_grads(self,
        index: int,
        query: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        """Split a gradient into layer-wise gradients.

        Args:
            index (int): The index of the model parameters. This index
                is used for ensembling of different trained model.
            query (torch.Tensor): Input gradient to split, could be train/test
                gradients. Normally of shape (batch_size,parameter)

        Returns:
            Tuple[torch.Tensor, ...]: The layer-wise splitted tensor, a tuple of shape
                (batch_size,layer0_size), (batch_size,layer1_size)...
        """
        model_params, param_layer_map = self.task.get_param(
            index, layer_split=True,
        )
        split_index = [0] * (param_layer_map[-1] + 1)
        for idx, layer_index in enumerate(param_layer_map):
            split_index[layer_index] += model_params[idx].shape[0]
        current_idx = 0
        query_split = []
        for i in range(len(split_index)):
            query_split.append(query[:, current_idx : split_index[i] + current_idx])
            current_idx += split_index[i]
        return query_split

    def attribute(
        self,
        train_dataloader: DataLoader,
        test_dataloader: DataLoader,
    ) -> torch.Tensor:
        """Calculate the influence of the training set on the test set.

        Args:
            train_dataloader (DataLoader): The dataloader for
                training samples to calculate the influence. It can be a subset
                of the full training set if `cache` is called before. A subset
                means that only a part of the training set's influence is calculated.
                The dataloader should not be shuffled.
            test_dataloader (DataLoader): The dataloader for
                test samples to calculate the influence. The dataloader should not
                be shuffled.

        Returns:
            torch.Tensor: The influence of the training set on the test set, with
                the shape of (num_train_samples, num_test_samples).

        """
        if self.full_train_dataloader is None:
            self.full_train_dataloader = train_dataloader
            warnings.warn(
                "The full training data loader was NOT cached. \
                           Treating the train_dataloader as the full training \
                           data loader.",
                stacklevel=1,
            )

        _check_shuffle(train_dataloader)
        _check_shuffle(test_dataloader)

        tda_output = torch.zeros(
            size=(len(train_dataloader.sampler), len(test_dataloader.sampler)),
            device=self.device,
        )

        # TODO: sometimes the train dataloader could be swapped with the test dataloader
        # prepare a checkpoint-specific seed

        for checkpoint_idx in range(len(self.task.get_checkpoints())):
            # set index to current one
            tda_output *= checkpoint_idx
            # calculate gradient with respect to full_data, do it only once
            full_data_grad = 0
            for full_data_ in self.full_train_dataloader:
                full_data = tuple(data.to(self.device).unsqueeze(0)
                             for data in full_data_)
                full_train_rep = self.generate_train_rep(
                    ckpt_idx=checkpoint_idx,
                    data=full_data,
                )

            for train_batch_idx, train_batch_data_ in enumerate(
                tqdm(
                    train_dataloader,
                    desc="calculating gradient of training set...",
                    leave=False,
                ),
            ):
                # move to device
                train_batch_data = tuple(
                    data.to(self.device).unsqueeze(0) for data in train_batch_data_
                )
                # get initial representations of train data
                train_batch_rep = self.generate_train_rep(
                    ckpt_idx=checkpoint_idx,
                    data=train_batch_data,
                )
                # transform the train representations
                train_batch_rep = self.transform_train_rep(
                    ckpt_idx=checkpoint_idx,
                    train_rep=train_batch_rep,
                )

                for test_batch_idx, test_batch_data_ in enumerate(
                    tqdm(
                        test_dataloader,
                        desc="calculating gradient of training set...",
                        leave=False,
                    ),
                ):
                    # move to device
                    test_batch_data = tuple(
                        data.to(self.device).unsqueeze(0) for data in test_batch_data_
                    )
                    # get initial representations of test data
                    test_batch_rep = self.generate_test_rep(
                        ckpt_idx=checkpoint_idx,
                        data=test_batch_data,
                    )
                    # transform the test representations
                    test_batch_rep = self.transform_test_rep(
                        ckpt_idx=checkpoint_idx,
                        test_rep=test_batch_rep,
                        full_train_rep=full_train_rep,
                    )

                    # results position based on batch info
                    row_st = train_batch_idx * train_dataloader.batch_size
                    row_ed = min(
                        (train_batch_idx + 1) * train_dataloader.batch_size,
                        len(train_dataloader.sampler),
                    )

                    col_st = test_batch_idx * test_dataloader.batch_size
                    col_ed = min(
                        (test_batch_idx + 1) * test_dataloader.batch_size,
                        len(test_dataloader.sampler),
                    )

                    tda_output[row_st:row_ed, col_st:col_ed] += (
                        train_batch_rep @ test_batch_rep.T
                    )

            tda_output /= checkpoint_idx + 1
             

        return tda_output

