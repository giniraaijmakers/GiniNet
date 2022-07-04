from typing import List, Tuple, Dict, Sequence, Optional, Callable, Union
from pathlib import Path, PosixPath
import json
import pickle

import numpy as np
import os

from monai.data import Dataset as _MonaiDataset
from monai.transforms import (
    Compose,
    LoadImaged,
    EnsureChannelFirstd,
    NormalizeIntensityd,
    DivisiblePadd,
)

from interactivenet.transforms.transforms import Resamplingd, EGDMapd, BoudingBoxd, Visualized

class Preprocessing(_MonaiDataset):
    def __init__(
        self, 
        images: List[PosixPath],
        masks: List[PosixPath],
        annotations: List[PosixPath],
        median_shape: Tuple[float],
        target_spacing: Tuple[float],
        save_location: PosixPath,
        relax_bbox: Union[float, Tuple[float]] = 0.1,
        divisble_using: Union[int, Tuple[int]] = (16, 16, 8)
    ) -> None:
        print("Initializing Preprocessing")

        self.save_location = save_location
        self.relax_bbox = relax_bbox
        self.divisble_using = divisble_using
        self.data = [
            {"image": img_path, "mask": mask_path, "annotation": annot_path}
            for img_path, mask_path, annot_path in zip(images, masks, annotations)
        ]

        self.transforms = Compose(
            [
                LoadImaged(keys=["image", "annotation", "mask"]),
                EnsureChannelFirstd(keys=["image", "annotation", "mask"]),
                Visualized(
                    keys=["image", "annotation", "mask"],
                    save=save_location / 'verbose' / 'raw',
                    annotation=True
                ),
                Resamplingd(
                    keys=["image", "annotation", "mask"],
                    pixdim=target_spacing,
                ),
                BoudingBoxd(
                    keys=["image", "annotation", "mask"],
                    on="mask",
                    relaxation=self.relax_bbox,
                    divisiblepadd=self.divisble_using,
                ),
                NormalizeIntensityd(
                    keys=["image"],
                    nonzero=False,
                    channel_wise=False,
                ),
                Visualized(
                    keys=["image", "annotation", "mask"],
                    save=save_location / 'verbose' / 'new',
                    annotation=True,
                ),
                EGDMapd(
                    keys=["annotation"],
                    image="image",
                    lamb=1,
                    iter=4,
                ),
                DivisiblePadd(
                    keys=["image", "annotation", "mask"],
                    k=self.divisble_using
                ),
                Visualized(
                    keys=["image", "annotation", "mask"],
                    save=save_location / 'verbose' / 'EGD',
                    distancemap=True,
                ),
                ]
        )
        self.create_directories()
        super().__init__(self.data, self.transforms)

    def __call__(self) -> None:
        print("\nPreprocessing:\n")
        metainfo = {}
        for i, item in enumerate(self.data):
            name = item["mask"].with_suffix('').stem
            print(f"File: {name}")
            item = self.__getitem__(i)

            metainfo[name] = self.create_metainfo(item)

            self.save_sample(item, name)
            print("")
    
        with open(self.input_folder / "metadata.pkl", 'wb') as handle:
            pickle.dump(metainfo, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def create_directories(self) -> None:
        self.input_folder = self.save_location / "network_input"
        self.input_folder.mkdir(parents=True, exist_ok=True)

    def create_metainfo(self, item:Dict[str, Union[np.ndarray, dict]]) -> dict:
        metadata = item["image_meta_dict"]
        return {
            "filename_or_obj" : metadata["filename_or_obj"],
            "org_size" : metadata["org_dim"],
            "org_spacing" : metadata["org_spacing"],
            "new_size" : metadata["new_dim"],
            "new_spacing" : metadata["new_spacing"],
            "ratio_size": metadata["new_dim"] / metadata["org_dim"],
            "ratio_spacing": metadata["new_spacing"] / metadata["org_spacing"],
            "resample_flag" : metadata["resample_flag"],
            "anisotrophy_flag" : metadata["anisotrophy_flag"],
            "bbox_location" : metadata["bbox"],
            "bbox_size" : metadata["bbox_shape"],
            "bbox_relaxation" : metadata["bbox_relaxation"],
            "bbox_ratio": metadata["bbox_shape"] / metadata["new_dim"],
            "final_size" : metadata["final_bbox"],
            "final_size" : metadata["final_bbox_shape"],
        }

    def save_sample(self, item:Dict[str, Union[np.ndarray, dict]], name:str) -> None:
        keys = list(item.keys())
        objects = [False if x.endswith("_meta_dict") else True for x in keys]
        objects = sum(objects)

        np.savez(self.input_folder / name, **{key : item[key] for key in keys[:objects]})

        pickle_data = {
            key : item[key] for key in keys[objects:]
        }

        with open(self.input_folder.parent / "network_input" / (name + ".pkl"), 'wb') as handle:
            pickle.dump(pickle_data, handle, protocol=pickle.HIGHEST_PROTOCOL)

if __name__=="__main__":
    from interactivenet.preprocessing.fingerprinting import FingerPrint

    import os
    exp = os.environ["interactiveseg_raw"]
    task = "Task001_Lipo"
    images = [x for x in Path(exp, task, "imagesTr").glob('**/*') if x.is_file()]
    masks = [x for x in Path(exp, task, "labelsTr").glob('**/*') if x.is_file()]
    annotations = [x for x in Path(exp, task, "interactionTr").glob('**/*') if x.is_file()]

    processed = os.environ["interactiveseg_processed"]
    save_location = Path(processed, task)
    save_location.mkdir(parents=True, exist_ok=True)

    results = FingerPrint(sorted(images), sorted(masks), sorted(annotations), save_location)
    results()
    prepro = Preprocessing(sorted(images), sorted(masks), sorted(annotations), results.dim, results.target_spacing, save_location, results.relax_bbox, results.divisible_by)
    prepro()