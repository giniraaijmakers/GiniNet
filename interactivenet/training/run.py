from pathlib import Path
import numpy as np
import os
import json
import argparse

from monai.utils import set_determinism
from monai.transforms import (
    AsDiscrete,
    Compose,
    RandFlipd,
    RandScaleIntensityd,
    ConcatItemsd,
    ToTensord,
    SpatialPadd,
    RandGaussianNoised,
    RandGaussianSmoothd,
    CastToTyped,
    RandAdjustContrastd,
    RandZoomd,
    RandRotated,
)
from monai.networks.nets import DynUNet
from monai.metrics import DiceMetric
from monai.losses import DiceCELoss
from monai.data import Dataset, DataLoader, decollate_batch

from interactivenet.transforms.transforms import LoadPreprocessed
from interactivenet.networks.unet import UNet

import torch
import pytorch_lightning as pl
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint

import mlflow.pytorch

class Net(pl.LightningModule):
    def __init__(self, data, metadata, split=0):
        super().__init__()
        self._model = DynUNet(
            spatial_dims=3,
            in_channels=2,
            out_channels=2,
            kernel_size=metadata["Plans"]["kernels"],
            strides=metadata["Plans"]["strides"],
            upsample_kernel_size=metadata["Plans"]["strides"][1:],
            filters=[4, 8, 16, 32, 64, 128],
            norm_name= 'instance',
            act_name = 'leakyrelu',
            deep_supervision = True,
            deep_supr_num = metadata["Plans"]["deep supervision"]
        )
        self.data = data
        self.metadata = metadata
        self.split = split
        self.loss_function = DiceCELoss(to_onehot_y=True, softmax=True)
        self.dice_metric = DiceMetric(include_background=False, reduction="mean", get_not_nans=False)
        self.post_pred = AsDiscrete(argmax=True, to_onehot=2)
        self.post_label = AsDiscrete(to_onehot=2)
        self.best_val_dice = 0
        self.best_val_epoch = 0
        self.max_epochs = 500
        self.batch_size = 1
        self.supervision_weights = metadata["Plans"]["deep supervision weights"]

    def forward(self, x):
        return self._model(x)

    def prepare_data(self):
        set_determinism(seed=0)

        train_transforms = Compose(
            [
                LoadPreprocessed(keys=["npz", "metadata"], new_keys=["image", "annotation", "mask"]),
                RandRotated(
                    keys=["image", "annotation", "mask"],
                    range_x=180,
                    range_y=180,
                    mode=("bilinear", "bilinear", "nearest"),
                    align_corners=(True, True, None),
                    prob=0.2,
                ),
                RandZoomd(
                    keys=["image", "annotation", "mask"],
                    min_zoom=0.7,
                    max_zoom=1.4,
                    mode=("trilinear", "trilinear", "nearest"),
                    align_corners=(True, True, None),
                    prob=0.2,
                ),
                RandGaussianNoised(keys=["image"], std=0.01, prob=0.15),
                RandGaussianSmoothd(
                    keys=["image"],
                    sigma_x=(0.5, 1.5),
                    sigma_y=(0.5, 1.5),
                    sigma_z=(0.5, 1.5),
                    prob=0.2,
                ),
                RandScaleIntensityd(keys=["image"], factors=0.3, prob=0.15),
                RandAdjustContrastd(keys=["image"], gamma=(0.65, 1.5), prob=0.15),
                RandFlipd(keys=["image", "annotation", "mask"], spatial_axis=[0], prob=0.5),
                RandFlipd(keys=["image", "annotation", "mask"], spatial_axis=[1], prob=0.5),
                RandFlipd(keys=["image", "annotation", "mask"], spatial_axis=[2], prob=0.5),
                CastToTyped(keys=["image", "annotation", "mask"], dtype=(np.float32, np.float32, np.uint8)),
                ConcatItemsd(keys=["image", "annotation"], name="image"),
                ToTensord(keys=["image", "mask"]),
                ]
        )
        
        val_transforms = Compose(
            [
                LoadPreprocessed(keys=["npz", "metadata"], new_keys=["image", "annotation", "mask"]),
                CastToTyped(keys=["image", "annotation", "mask"], dtype=(np.float32, np.float32, np.uint8)),
                ConcatItemsd(keys=["image", "annotation"], name="image"),
                ToTensord(keys=["image", "mask"]),
            ]
        )

        split = self.metadata["Plans"]["splits"][self.split]
        train_data = [x for x in self.data if x['npz'].stem in split['train']]
        val_data = [x for x in self.data if x['npz'].stem in split['val']]

        self.train_ds = Dataset(
            data=train_data, transform=train_transforms,
        )
        self.val_ds = Dataset(
            data=val_data, transform=val_transforms,
        )

    def train_dataloader(self):
        train_loader = DataLoader(
            self.train_ds, batch_size=self.batch_size, shuffle=True,
            num_workers=4,
        )
        return train_loader

    def val_dataloader(self):
        val_loader = DataLoader(
            self.val_ds, batch_size=1, num_workers=4)
        return val_loader

    def configure_optimizers(self):
        self.optimizer = torch.optim.SGD(self._model.parameters(), lr=0.01, momentum=0.99, weight_decay=3e-5, nesterov=True)
        self.lr_scheduler = {
            'scheduler': torch.optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lambda epoch: (1 - epoch / self.max_epochs) ** 0.9),
            'name': 'lr_sched'
        }
        return [self.optimizer], [self.lr_scheduler]

    def _compute_loss(self, outputs, labels):
        if len(outputs.size()) - len(labels.size()) == 1:
            outputs = torch.unbind(outputs, dim=1)
            loss = sum([self.supervision_weights[i] * self.loss_function(output, labels) for i, output in enumerate(outputs)])
        else:
            loss = self.loss_function(outputs, labels)

        return loss

    def training_step(self, batch, batch_idx):
        images, labels = batch["image"], batch["mask"]
        outputs = self.forward(images)
        loss = self._compute_loss(outputs, labels)

        self.log("loss", loss, on_epoch=True, batch_size=self.batch_size)
        return {"loss": loss}

    def training_epoch_end(self, outputs):
        # Only required for logging it to mlflow for some reason.
        self.log("lr", self.lr_scheduler["scheduler"].get_last_lr()[0])

    def validation_step(self, batch, batch_idx):
        images, labels = batch["image"], batch["mask"]
        outputs = self.forward(images)
        loss = self._compute_loss(outputs, labels)

        outputs = [self.post_pred(i) for i in decollate_batch(outputs)]
        labels = [self.post_label(i) for i in decollate_batch(labels)]
        self.dice_metric(y_pred=outputs, y=labels)
        self.log("val_loss", loss, on_epoch=True, batch_size=1)
        return {"val_loss": loss, "val_number": len(outputs)}

    def validation_epoch_end(self, outputs):
        val_loss, num_items = 0, 0
        for output in outputs:
            val_loss += output["val_loss"].sum().item()
            num_items += output["val_number"]

        mean_val_dice = self.dice_metric.aggregate().item()
        self.dice_metric.reset()
        mean_val_loss = torch.tensor(val_loss / num_items)
        if mean_val_dice > self.best_val_dice:
            self.best_val_dice = mean_val_dice
            self.best_val_epoch = self.current_epoch
        print(
            f"current epoch: {self.current_epoch} "
            f"current mean dice: {mean_val_dice:.4f}"
            f"\nbest mean dice: {self.best_val_dice:.4f} "
            f"at epoch: {self.best_val_epoch}"
        )
        self.log("curent epoch", self.current_epoch, on_epoch=True)
        self.log("current mean dice", mean_val_dice, on_epoch=True)
        self.log("best mean dice", self.best_val_dice, on_epoch=True)
        self.log("at epoch", self.best_val_epoch, on_epoch=True)
        return mean_val_dice, mean_val_loss

if __name__=="__main__":
    parser = argparse.ArgumentParser(
             description="Preprocessing of "
         )
    parser.add_argument(
         "-t",
         "--task",
         nargs="?",
         default="Task710_STTMRI",
         help="Task name"
    )
    parser.add_argument(
         "-f",
         "--fold",
         nargs="?",
         default=0,
         type=int,
         help="which fold do you want to train?"
    )
    args = parser.parse_args()
    exp = Path(os.environ["interactiveseg_processed"], args.task)

    from interactivenet.utils.utils import read_processed, read_metadata
    data = read_processed(exp)
    metadata = read_metadata(exp / "plans.json")

    lr_logger = LearningRateMonitor(logging_interval="epoch")
    checkpoint_callback = ModelCheckpoint(
        monitor="val_loss",
        filename='{epoch:02d}-{val_loss:.2f}',
        mode='min'
    )

    from interactivenet.utils.mlflow import mlflow_get_id
    experiment_id = mlflow_get_id(args.task)

    mlflow.pytorch.autolog()

    with mlflow.start_run(experiment_id=experiment_id, run_name=args.fold) as run:
        mlflow.set_tag('Mode', 'training')
        mlflow.log_param("fold", args.fold)
        artifact_path = Path(mlflow.get_artifact_uri().split('file://')[-1])

        network = Net(data, metadata, split=args.fold)
        trainer = pl.Trainer(
            gpus=-1,
            max_epochs=500,
            num_sanity_val_steps=1,
            log_every_n_steps=50,
            check_val_every_n_epoch=1,
            callbacks=[lr_logger, checkpoint_callback],
            accumulate_grad_batches=4,
            default_root_dir=artifact_path
        )
        
        trainer.fit(network)
