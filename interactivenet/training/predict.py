from pathlib import Path
import numpy as np
import os
import pickle
import json
import matplotlib.pyplot as plt

from monai.utils import set_determinism
from monai.transforms import (
    AsDiscrete,
    Compose,
    ToTensord,
    Compose,
    LoadImaged,
    ConcatItemsd,
    EnsureChannelFirstd,
    NormalizeIntensityd,
    DivisiblePadd,
    CastToTyped,
)

from monai.data import Dataset, DataLoader, decollate_batch
from monai.metrics import compute_meandice, compute_average_surface_distance, compute_hausdorff_distance

from interactivenet.transforms.transforms import Resamplingd, EGDMapd, BoudingBoxd, NormalizeValuesd
from interactivenet.utils.visualize import ImagePlot
from interactivenet.utils.statistics import ResultPlot

import torch
import pytorch_lightning as pl

import mlflow.pytorch
from mlflow.utils.mlflow_tags import MLFLOW_PARENT_RUN_ID

class Net(pl.LightningModule):
    def __init__(self, data, metadata, model):
        super().__init__()
        self._model = mlflow.pytorch.load_model(model, map_location=torch.device('cuda'))
        self.data = data
        self.metadata = metadata
        self.post_pred = AsDiscrete(argmax=True, to_onehot=2)
        self.post_label = AsDiscrete(to_onehot=2)
        self.batch_size = 1

    def forward(self, x):
        return self._model(x)

    def prepare_data(self):
        set_determinism(seed=0)

        test_transforms = Compose(
            [
                LoadImaged(keys=["image", "annotation", "mask"]),
                EnsureChannelFirstd(keys=["image", "annotation", "mask"]),
                Resamplingd(
                    keys=["image", "annotation", "mask"],
                    pixdim=metadata["Fingerprint"]["Target spacing"],
                ),
                BoudingBoxd(
                    keys=["image", "annotation", "mask"],
                    on="mask",
                    relaxation=0.1,
                    divisiblepadd=metadata["Plans"]["divisible by"],
                ),
                NormalizeValuesd(
                    keys=["image"],
                    clipping=metadata["Fingerprint"]["Clipping"],
                    mean=metadata["Fingerprint"]["Intensity_mean"],
                    std=metadata["Fingerprint"]["Intensity_std"],
                ),
                EGDMapd(
                    keys=["annotation"],
                    image="image",
                    lamb=1,
                    iter=4,
                    logscale=True,
                    ct=metadata["Fingerprint"]["CT"],
                ),
                CastToTyped(keys=["image", "annotation", "mask"], dtype=(np.float32, np.float32, np.uint8)),
                ConcatItemsd(keys=["image", "annotation"], name="image"),
                ToTensord(keys=["image", "mask"]),
            ]
        )

        self.predict_ds = Dataset(
            data=self.data, transform=test_transforms,
        )

    def predict_dataloader(self):
        predict_loader = DataLoader(
            self.predict_ds, batch_size=self.batch_size, shuffle=False,
            num_workers=4,
        )
        return predict_loader

    def predict_step(self, batch, batch_idx):
        images, labels = batch["image"], batch["mask"]
        outputs = self.forward(images)
        weights = [i for i in decollate_batch(outputs)]
        outputs = [self.post_pred(i) for i in decollate_batch(outputs)]
        labels = [self.post_label(i) for i in decollate_batch(labels)]

        return images, labels, outputs, weights, batch["mask_meta_dict"]

if __name__=="__main__":
    import argparse
    import os

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
        "-c",
        "--classes",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Do you want to splits classes"
    )
    parser.add_argument(
        "-w",
        "--weights",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Do you want to save weights as .npy in order to ensembling?"
    )

    args = parser.parse_args()
    exp = os.environ["interactiveseg_processed"]
    raw = Path(os.environ["interactiveseg_raw"], args.task)

    results = Path(os.environ["interactiveseg_results"], args.task)
    results.mkdir(parents=True, exist_ok=True)

    images = sorted([x for x in (raw / "imagesTs").glob('**/*') if x.is_file()])
    masks = sorted([x for x in (raw / "labelsTs").glob('**/*') if x.is_file()])
    annotations = sorted([x for x in (raw / "interactionTs").glob('**/*') if x.is_file()])

    data = [
        {"image": img_path, "mask": mask_path, "annotation": annot_path}
        for img_path, mask_path, annot_path in zip(images, masks, annotations)
    ]

    metadata = Path(exp, args.task, "plans.json")
    if metadata.is_file():
        with open(metadata) as f:
            metadata = json.load(f)
    else:
        raise KeyError("metadata not found")

    experiment_id = mlflow.get_experiment_by_name(args.task)
    if experiment_id == None:
        raise ValueError("Experiments not found, please first train models")
    else: experiment_id = experiment_id.experiment_id

    runs = mlflow.search_runs(experiment_id)

    if args.classes:
        types = raw / "types.json"
        if types.is_file():
            with open(types) as f:
                types = json.load(f)
                types = {v: key for key, value in types.items() for v in value}
        else:
            raise KeyError("types file not found")
    else:
        types = False
        unseen = [False] * len(runs)

    for idx, run in runs.iterrows():
        if run["tags.Mode"] != "training":
            continue

        run_id = run["run_id"]
        fold = run["params.fold"]
        model = "runs:/" + run_id + "/model"
        network = Net(data, metadata, model)

        trainer = pl.Trainer(
            gpus=-1,
        )

        with mlflow.start_run(experiment_id=experiment_id, tags={MLFLOW_PARENT_RUN_ID: run_id}) as run:
            mlflow.set_tag('Mode', 'testing')
            outputs = trainer.predict(model=network)
            dices = {}
            hausdorff = {}
            surface = {}
            for image, label, output, weights, meta in outputs:
                name = Path(meta["filename_or_obj"][0]).name.split('.')[0]

                dice = compute_meandice(output[0][None,:], label[0][None,:], include_background=False)
                dices[name] = dice.item()

                hausdorff_distance = compute_hausdorff_distance(output[0][None,:], label[0][None,:], include_background=False)
                hausdorff[name] = hausdorff_distance.item()

                surface_distance = compute_average_surface_distance(output[0][None,:], label[0][None,:], include_background=False)
                surface[name] = surface_distance.item()

                f = ImagePlot(image[0][:1].numpy(), label[0].numpy(), [output[0][1:].numpy()], CT=metadata["Fingerprint"]["CT"])
                mlflow.log_figure(f, f"images/{name}.png")

                if args.weights:
                    tmp_dir = Path(exp, "tmp")
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    data_file = tmp_dir / f"{name}.npz"

                    image = image[0].detach().cpu().numpy()
                    label = label[0].detach().cpu().numpy()
                    weights = weights[0].detach().cpu().numpy()

                    np.savez(str(data_file), image=image, label=label, weights=weights)
                    mlflow.log_artifact(str(data_file), artifact_path="weights")
                    
                    data_file.unlink()

            mlflow.log_metric("Mean dice", np.mean(list(dices.values())))
            mlflow.log_metric("Std dice", np.std(list(dices.values())))

            f = ResultPlot(dices, "Dice", types)
            plt.close("all")
            mlflow.log_figure(f, f"dice.png")
            mlflow.log_dict(dices, "dice.json")

            f = ResultPlot(hausdorff, "Hausdorff Distance", types)
            plt.close("all")
            mlflow.log_figure(f, f"hausdorff_distance.png")
            mlflow.log_dict(hausdorff, "hausdorff_distance.json")

            f = ResultPlot(surface, "Surface Distance", types)
            plt.close("all")
            mlflow.log_figure(f, f"surface_distance.png")
            mlflow.log_dict(surface, "surface_distance.json")
