# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import torch.multiprocessing as mp
from omegaconf.omegaconf import OmegaConf, open_dict
from pytorch_lightning import Trainer
from pytorch_lightning.plugins.environments import TorchElasticEnvironment
from nemo.collections.nlp.models.language_modeling.megatron_fused_retro import MegatronFusedRetrievalLoraModel

from nemo.collections.nlp.models.language_modeling.megatron_t5_adapter_model import MegatronT5LoraModel
from nemo.collections.nlp.parts.nlp_overrides import (
    GradScaler,
    MegatronHalfPrecisionPlugin,
    NLPDDPStrategy,
    NLPSaveRestoreConnector,
    PipelineMixedPrecisionPlugin,
)
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager

mp.set_start_method("spawn", force=True)

"""
This is the script to train an Adapter infused GPT Model for text generation.
A base GPT Model is required as a starting point. This script will then insert
Adapters into each Transformer layer and will train/update only these adapters
during training. The base GPT Model weights will remain frozen.

During training this script will only save the newly trained Adapter weights
in checkpoints. At the end of training a .nemo file of Adapter weights will 
be saved.

Usage:
    Assuming the base model is a 125m GPT Model, with TP=1, PP=1:
    a. run a training run for a base gpt nemo file:
        python megatron_gpt_adapter_tuning.py \
            "model.data.train_ds=[PATH TO TRAINING JSONL FILE]",
            "model.data.validation_ds=[PATH TO VALIDATION JSONL FILE]",
            model.language_model_path="PATH TO BASE GPT MODEL .nemo FILE"
            name="NAME OF TRAINING RUN"
            exp_manager.exp_dir="DIR TO SAVE CHECKPOINTS and .nemo FILE",
            trainer.max_epochs=2
"""


@hydra_runner(config_path="conf", config_name="retro_gpt_lora_tuning_config")
def main(cfg) -> None:
    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f'\n{OmegaConf.to_yaml(cfg)}')

    megatron_amp_o2 = cfg.model.get('megatron_amp_O2', False)
    with_distributed_adam = cfg.model.optim.get('name') == 'distributed_fused_adam'

    plugins = []
    strategy = NLPDDPStrategy(
        no_ddp_communication_hook=True,  # we don't use DDP for async grad allreduce
        gradient_as_bucket_view=False,
        find_unused_parameters=False,
    )
    if cfg.trainer.precision in [16, 'bf16']:
        scaler = None
        if cfg.trainer.precision == 16:
            scaler = GradScaler(
                init_scale=cfg.model.get('native_amp_init_scale', 2 ** 32),
                growth_interval=cfg.model.get('native_amp_growth_interval', 1000),
                hysteresis=cfg.model.get('hysteresis', 2),
            )
        if megatron_amp_o2 and not with_distributed_adam:
            plugins.append(MegatronHalfPrecisionPlugin(precision=cfg.trainer.precision, device='cuda', scaler=scaler))
        else:
            plugins.append(PipelineMixedPrecisionPlugin(precision=cfg.trainer.precision, device='cuda', scaler=scaler))

    if cfg.get('cluster_type', None) == 'BCP':
        plugins.append(TorchElasticEnvironment())

    trainer = Trainer(plugins=plugins, strategy=strategy, **cfg.trainer)
    exp_manager(trainer, cfg.exp_manager)

    with open_dict(cfg):
        cfg.model.precision = cfg.trainer.precision

    save_restore_connector = NLPSaveRestoreConnector()

    if os.path.isdir(cfg.get('restore_from_path')):
        save_restore_connector.model_extracted_dir = cfg.get('restore_from_path')
    frozen_model_cfg = MegatronFusedRetrievalLoraModel.restore_from(
        cfg.get('restore_from_path'), trainer=trainer, return_config=True, save_restore_connector=save_restore_connector,
    )

    frozen_model_cfg.tokenizer = cfg.model.tokenizer
    frozen_model_cfg.data = cfg.model.data
    frozen_model_cfg.adapter_tuning = cfg.model.adapter_tuning
    frozen_model_cfg.optim = cfg.model.optim
    frozen_model_cfg.restore_from_path = cfg.model.restore_from_path
    frozen_model_cfg.eval = cfg.model.eval
    frozen_model_cfg.add_position_embedding = cfg.model.add_position_embedding
    frozen_model_cfg.micro_batch_size = cfg.model.micro_batch_size
    frozen_model_cfg.precision = trainer.precision

    frozen_model_cfg.task_templates = cfg["model"]["task_templates"]


    if "shape_file" in frozen_model_cfg:
        frozen_model_cfg.pop("shape_file")

    print(frozen_model_cfg)
    model = MegatronFusedRetrievalLoraModel(frozen_model_cfg, trainer)
    trainer.fit(model)

if __name__ == '__main__':
    main()
