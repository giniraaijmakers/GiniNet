from pathlib import Path
import numpy as np
import os
import pickle
import json
import matplotlib.pyplot as plt
import uuid

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
    EnsureType,
    MeanEnsemble
)

from monai.data import Dataset, DataLoader, decollate_batch

from interactivenet.transforms.transforms import (
    Resamplingd, 
    EGDMapd, 
    BoudingBoxd, 
    NormalizeValuesd, 
    OriginalSize,
    TestTimeFlipping
)
from interactivenet.utils.visualize import ImagePlot
from interactivenet.utils.statistics import ResultPlot, CalculateScores
from interactivenet.utils.postprocessing import ApplyPostprocessing

import torch
import pytorch_lightning as pl

import mlflow.pytorch
from mlflow.utils.mlflow_tags import MLFLOW_PARENT_RUN_ID

class PredictModule(pl.LightningModule):
    def __init__(
        self, 
        data, 
        metadata, 
        model, 
        tta=False):
        super().__init__()
        self._model = mlflow.pytorch.load_model(model, map_location=torch.device('cuda'))
        self.data = data
        self.metadata = metadata
        self.tta = tta
        self.post_numpy = EnsureType("numpy", device="cpu")
        self.original_size = OriginalSize(metadata["Fingerprint"]["Anisotropic"])

    def forward(self, x):
        return self._model(x)

    def prepare_data(self):
        set_determinism(seed=0)

        test_transforms = Compose(
            [
                LoadImaged(keys=["image", "annotation"]),
                EnsureChannelFirstd(keys=["image", "annotation"]),
                Resamplingd(
                    keys=["image", "annotation"],
                    pixdim=self.metadata["Fingerprint"]["Target spacing"],
                ),
                BoudingBoxd(
                    keys=["image", "annotation"],
                    on="annotation",
                    relaxation=0.1,
                    divisiblepadd=self.metadata["Plans"]["divisible by"],
                ),
                NormalizeValuesd(
                    keys=["image"],
                    clipping=self.metadata["Fingerprint"]["Clipping"],
                    mean=self.metadata["Fingerprint"]["Intensity_mean"],
                    std=self.metadata["Fingerprint"]["Intensity_std"],
                ),
                EGDMapd(
                    keys=["annotation"],
                    image="image",
                    lamb=1,
                    iter=4,
                    logscale=True,
                    ct=self.metadata["Fingerprint"]["CT"],
                ),
                CastToTyped(keys=["image", "annotation"], dtype=(np.float32, np.float32)),
                ConcatItemsd(keys=["image", "annotation"], name="image"),
                ToTensord(keys=["image"]),
            ]
        )

        self.predict_ds = Dataset(
            data=self.data, transform=test_transforms,
        )

    def predict_dataloader(self):
        predict_loader = DataLoader(
            self.predict_ds, batch_size=1, shuffle=False,
            num_workers=4,
        )
        return predict_loader

    def predict_step(self, batch, batch_idx):
        image = batch["image"]
        if self.tta:
            flip = TestTimeFlipping()
            ensembling = MeanEnsemble()

            image = flip(image)
            output = self.forward(image)
            
            flip.back = True
            output = flip(output)
            output = ensembling(output)
            output = [self.post_numpy(output)]
        else:
            output = self.forward(image)
            output = [self.post_numpy(i) for i in decollate_batch(output)]


        meta = [self.post_numpy(i) for i in decollate_batch(batch["annotation_meta_dict"])]
        output = [self.original_size(output, meta) for output, meta in zip(output, meta)]

        return output, meta

def main():
    print('Not implemented yet')

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
        "-a",
        "--tta",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Do you want to use test time augmentations?"
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

    from interactivenet.utils.utils import read_metadata, read_data, read_types, read_nifti
    data = read_data(raw, test=True)
    raw_data = read_data(raw)
    raw_data = read_nifti(raw_data)

    metadata = Path(exp, args.task, "plans.json")
    metadata = read_metadata(metadata)

    from interactivenet.utils.mlflow import mlflow_get_runs
    runs, experiment_id = mlflow_get_runs(args.task)

    if args.classes:
        types = read_types(raw / "types.json")
    else:
        types = False

    to_discrete = AsDiscrete(to_onehot=2)
    to_discrete_argmax = AsDiscrete(argmax=True)
    for idx, run in runs.iterrows():
        if run["tags.Mode"] != "training":
            continue

        run_id = run["run_id"]
        fold = run["params.fold"]
        postprocessing = Path(run["artifact_uri"].split('file://')[-1], "postprocessing.json")
        postprocessing = read_metadata(postprocessing, error_message="postprocessing hasn't been run yet, please do this before predictions")
        if postprocessing["using_checkpoint"]:
            model = "runs:/" + run_id + "/model_checkpoint"
        else:
            model = "runs:/" + run_id + "/model"

        network = PredictModule(data, metadata, model, tta=args.tta)

        trainer = pl.Trainer(
            gpus=-1,
        )

        tmp_dir = Path(exp, str(uuid.uuid4()))
        tmp_dir.mkdir(parents=True, exist_ok=True)

        with mlflow.start_run(experiment_id=experiment_id, tags={MLFLOW_PARENT_RUN_ID: run_id}) as run:
            mlflow.set_tag('Mode', 'testing')
            outputs = trainer.predict(model=network)

            dices = {}
            hausdorff = {}
            surface = {}
            for weight, meta in outputs:
                weight, meta = weight[0], meta[0]
                name = Path(meta["filename_or_obj"]).name.split('.')[0]

                image = raw_data[name]["image"]
                label = raw_data[name]["masks"]

                output = to_discrete_argmax(weight)
                output = ApplyPostprocessing(output, postprocessing["postprocessing"])

                f = ImagePlot(image, label, additional_scans=[output[0]], CT=metadata["Fingerprint"]["CT"])
                mlflow.log_figure(f, f"images/{name}.png")

                label = to_discrete(label[None,:])
                output = to_discrete(output)

                dice, hausdorff_distance, surface_distance = CalculateScores(output, label)
                dices[name] = dice.item()
                hausdorff[name] = hausdorff_distance.item()
                surface[name] = surface_distance.item()

                if args.weights:
                    data_file = tmp_dir / f"{name}.npz"

                    np.savez(str(data_file), weights=weight)
                    mlflow.log_artifact(str(data_file), artifact_path="weights")
                    data_file.unlink()

                    data_file = tmp_dir / f"{name}.pkl"
                    with open(str(data_file), 'wb') as handle:
                        pickle.dump(meta, handle, protocol=pickle.HIGHEST_PROTOCOL)

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
            tmp_dir.rmdir()