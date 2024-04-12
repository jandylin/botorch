#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
References

.. [Feng2020HDCPS]
    Q. Feng, B. Latham, H. Mao and E. Backshy. High-Dimensional Contextual Policy
    Search with Unknown Context Rewards using Bayesian Optimization.
    Advances in Neural Information Processing Systems 33, NeurIPS 2020.
"""

import warnings
from typing import List, Optional

import torch
from botorch.models.multitask import MultiTaskGP
from botorch.models.transforms.input import InputTransform
from botorch.models.transforms.outcome import OutcomeTransform
from gpytorch.constraints import Interval
from gpytorch.distributions.multivariate_normal import MultivariateNormal
from gpytorch.kernels.rbf_kernel import RBFKernel
from linear_operator.operators import LinearOperator
from torch import Tensor
from torch.nn import ModuleList


class LCEMGP(MultiTaskGP):
    r"""The Multi-Task GP with the latent context embedding multioutput (LCE-M)
    kernel. See [Feng2020HDCPS]_ for a reference on the model and its use in Bayesian
    optimization.

    """

    def __init__(
        self,
        train_X: Tensor,
        train_Y: Tensor,
        task_feature: int,
        train_Yvar: Optional[Tensor] = None,
        context_cat_feature: Optional[Tensor] = None,
        context_emb_feature: Optional[Tensor] = None,
        embs_dim_list: Optional[List[int]] = None,
        output_tasks: Optional[List[int]] = None,
        all_tasks: Optional[List[int]] = None,
        input_transform: Optional[InputTransform] = None,
        outcome_transform: Optional[OutcomeTransform] = None,
    ) -> None:
        r"""
        Args:
            train_X: (n x d) X training data.
            train_Y: (n x 1) Y training data.
            task_feature: Column index of train_X to get context indices.
            train_Yvar: An optional (n x 1) tensor of observed variances of each
                training Y. If None, we infer the noise. Note that the inferred noise
                is common across all tasks.
            context_cat_feature: (n_contexts x k) one-hot encoded context
                features. Rows are ordered by context indices, where k is the
                number of categorical variables. If None, task indices will
                be used and k = 1.
            context_emb_feature: (n_contexts x m) pre-given continuous
                embedding features. Rows are ordered by context indices.
            embs_dim_list: Embedding dimension for each categorical variable.
                The length equals k. If None, the embedding dimension is set to 1
                for each categorical variable.
            output_tasks: A list of task indices for which to compute model
                outputs for. If omitted, return outputs for all task indices.
            all_tasks: By default, multi-task GPs infer the list of all tasks from
                the task features in `train_X`. This is an experimental feature that
                enables creation of multi-task GPs with tasks that don't appear in the
                training data. Note that when a task is not observed, the corresponding
                task covariance will heavily depend on random initialization and may
                behave unexpectedly.
        """
        super().__init__(
            train_X=train_X,
            train_Y=train_Y,
            task_feature=task_feature,
            train_Yvar=train_Yvar,
            output_tasks=output_tasks,
            all_tasks=all_tasks,
            input_transform=input_transform,
            outcome_transform=outcome_transform,
        )
        self.device = train_X.device
        if all_tasks is None:
            all_tasks = train_X[:, task_feature].unique()
            self.all_tasks = all_tasks.to(dtype=torch.long).tolist()
        else:
            all_tasks = torch.tensor(all_tasks, dtype=torch.long)
            self.all_tasks = all_tasks
        self.all_tasks.sort()  # These are the context indices.

        if context_cat_feature is None:
            context_cat_feature = all_tasks.unsqueeze(-1).to(device=self.device)
        self.context_cat_feature = context_cat_feature  # row indices = context indices
        self.context_emb_feature = context_emb_feature

        #  construct emb_dims based on categorical features
        if embs_dim_list is None:
            #  set embedding_dim = 1 for each categorical variable
            embs_dim_list = [1 for _i in range(context_cat_feature.size(1))]
        n_embs = sum(embs_dim_list)
        self.emb_dims = [
            (len(context_cat_feature[:, i].unique()), embs_dim_list[i])
            for i in range(context_cat_feature.size(1))
        ]
        # contruct embedding layer: need to handle multiple categorical features
        self.emb_layers = ModuleList(
            [
                torch.nn.Embedding(num_embeddings=x, embedding_dim=y, max_norm=1.0)
                for x, y in self.emb_dims
            ]
        )
        self.task_covar_module = RBFKernel(
            ard_num_dims=n_embs,
            lengthscale_constraint=Interval(
                0.0, 2.0, transform=None, initial_value=1.0
            ),
        )
        self.to(train_X)

    def _eval_context_covar(self) -> LinearOperator:
        """Obtain the context covariance matrix, a linear operator
        with shape (num_contexts x num_contexts).

        This first generates the embedding features for all contexts,
        then evaluates the task covariance matrix with those embeddings
        to get the task covariance matrix.
        """
        all_embs = self._task_embeddings()
        return self.task_covar_module(all_embs)

    def _task_embeddings(self) -> Tensor:
        """Generate embedding features for all contexts."""
        embeddings = [
            emb_layer(
                self.context_cat_feature[:, i].to(
                    dtype=torch.long, device=self.device
                )  # pyre-ignore
            )
            for i, emb_layer in enumerate(self.emb_layers)
        ]
        embeddings = torch.cat(embeddings, dim=1)

        # add given embeddings if any
        if self.context_emb_feature is not None:
            embeddings = torch.cat(
                [embeddings, self.context_emb_feature.to(self.device)],
                dim=1,  # pyre-ignore
            )
        return embeddings

    def task_covar_matrix(self, task_idcs: Tensor) -> Tensor:
        r"""Compute the task covariance matrix for a given tensor of
        task / context indices.

        Args:
            task_idcs: Task index tensor of shape (n x 1) or (b x n x 1).

        Returns:
            Task covariance matrix of shape (b x n x n).
        """
        # This is a tensor of shape (num_tasks x num_tasks).
        covar_matrix = self._eval_context_covar().to_dense()
        # Here, we index into the base covar matrix to extract
        # the rows & columns corresponding to the task indices.
        # First indexing operation picks the rows for each index in
        # task indices (results in b x n x num_tasks). We then transpose
        # to make the picked rows into columns (b x num_tasks x n), and
        # pick the rows again to result in the final covariance matrix.
        # The result is a symmetric tensor of shape (b x n x n).
        # An alternative implementation could pick the columns directly
        # by moving the transpose operation into the index of gather,
        # however, this does not seem to make any noticeable difference.
        base_idx = task_idcs.squeeze(-1)
        expanded_idx = task_idcs.expand(
            *([-1] * (task_idcs.dim() - 1)), task_idcs.shape[-2]
        )
        return (
            covar_matrix[base_idx].transpose(-1, -2).gather(index=expanded_idx, dim=-2)
        )

    def forward(self, x: Tensor) -> MultivariateNormal:
        if self.training:
            x = self.transform_inputs(x)
        x_basic, task_idcs = self._split_inputs(x)
        # Compute base mean and covariance
        mean_x = self.mean_module(x_basic)
        covar_x = self.covar_module(x_basic)
        # Compute task covariances
        covar_i = self.task_covar_matrix(task_idcs)
        covar = covar_x.mul(covar_i)
        return MultivariateNormal(mean_x, covar)


class FixedNoiseLCEMGP(LCEMGP):
    r"""The Multi-Task GP the latent context embedding multioutput
    (LCE-M) kernel, with known observation noise.

    DEPRECATED: Please use `LCEMGP` with `train_Yvar` instead.
    Will be removed in a future release (~v0.11).
    """

    def __init__(
        self,
        train_X: Tensor,
        train_Y: Tensor,
        train_Yvar: Tensor,
        task_feature: int,
        context_cat_feature: Optional[Tensor] = None,
        context_emb_feature: Optional[Tensor] = None,
        embs_dim_list: Optional[List[int]] = None,
        output_tasks: Optional[List[int]] = None,
    ) -> None:
        r"""
        Args:
            train_X: (n x d) X training data.
            train_Y: (n x 1) Y training data.
            train_Yvar: (n x 1) Observed variances of each training Y.
            task_feature: Column index of train_X to get context indices.
            context_cat_feature: (n_contexts x k) one-hot encoded context
                features. Rows are ordered by context indices, where k is the
                number of categorical variables. If None, task indices will
                be used and k = 1.
            context_emb_feature: (n_contexts x m) pre-given continuous
                embedding features. Rows are ordered by context indices.
            embs_dim_list: Embedding dimension for each categorical variable.
                The length equals to k. If None, the embedding dimension is set to
                1 for each categorical variable.
            output_tasks: A list of task indices for which to compute model
                outputs for. If omitted, return outputs for all task indices.

        """
        warnings.warn(
            "`FixedNoiseLCEMGP` has been deprecated and will be removed in a "
            "future release. Please use the `LCEMGP` model instead. "
            "When `train_Yvar` is specified, `LCEMGP` behaves the same "
            "as the `FixedNoiseLCEMGP`.",
            DeprecationWarning,
            stacklevel=2,
        )

        super().__init__(
            train_X=train_X,
            train_Y=train_Y,
            task_feature=task_feature,
            train_Yvar=train_Yvar,
            context_cat_feature=context_cat_feature,
            context_emb_feature=context_emb_feature,
            embs_dim_list=embs_dim_list,
            output_tasks=output_tasks,
        )
