from typing import Union, List

from pathlib import Path
from tqdm import tqdm
import numpy as np
import pandas as pd

from nilearn.image import load_img, resample_to_img
from nilearn.masking import intersect_masks

import templateflow
from bids import BIDSLayout

template = "MNI152NLin2009cAsym"
qulaity_control_standards = {
    "mean_fd": 0.55,
    "scrubbing_fd": 0.2,
    "proportion_kept": 0.5,
    "anatomical_dice": 0.99,
    "functional_dice": 0.89,
}


def get_reference_mask(
    analysis_level: str,
    subjects: List[str],
    task: List[str],
    fmriprep_bids_layout: BIDSLayout,
) -> dict:
    """
    Find the correct target mask for dice coefficient.

    Parameters
    ----------

    analysis_level : {"group", "participant"}
        BIDS app analyisi level.

    subjects :
        Participant IDs in a BIDS dataset.

    task :
        Task name in a BIDS dataset.

    fmriprep_bids_layout :
        BIDS layout of a fMRIPrep derivative.

    Returns
    -------

    Dict
        Reference brain masks for anatomical and functional scans.
    """
    template_mask = templateflow.api.get(
        [template], desc="brain", suffix="mask", resolution="01"
    )
    reference_masks = {"anat": template_mask}
    if analysis_level == "group" and len(subjects) > 1:
        print("Create dataset level functional brain mask")
        # create a group level mask
        func_filter = {
            "subject": subjects,
            "task": task,
            "space": template,
            "desc": ["brain"],
            "suffix": ["mask"],
            "extension": "nii.gz",
            "datatype": "func",
        }
        func_masks = fmriprep_bids_layout.get(
            **func_filter, return_type="file"
        )
        reference_masks["func"] = intersect_masks(func_masks, threshold=0.5)
    else:
        reference_masks["func"] = template_mask
    return reference_masks


def calculate_functional_metrics(
    subjects: List[str],
    task: List[str],
    fmriprep_bids_layout: BIDSLayout,
    reference_masks: dict,
) -> pd.DataFrame:
    """
    Calculate functional scan quality metrics:
        mean framewise displacement of original scan
        mean framewise displacement after scrubbing
        proportion of scan remained after scrubbing
        dice coefficient.

    The default scrubbing criteria is set to 0.2 mm.

    Parameters
    ----------
    subjects :
        Participant IDs in a BIDS dataset.

    task :
        Task name in a BIDS dataset.

    fmriprep_bids_layout :
        BIDS layout of a fMRIPrep derivative.

    reference_masks :
        Reference brain masks for anatomical and functional scans.

    Returns
    -------
    pandas.DataFrame
        Functional scan quality metrics
    """
    metrics = {}

    confounds_filter = {
        "subject": subjects,
        "task": task,
        "desc": "confounds",
        "extension": "tsv",
    }

    confounds = fmriprep_bids_layout.get(
        **confounds_filter, return_type="file"
    )

    print("Motion...")
    for confound_file in tqdm(confounds):
        # compute fds score
        framewise_displacements = pd.read_csv(confound_file, sep="\t")[
            "framewise_displacement"
        ].to_numpy()
        timeseries_length = len(framewise_displacements)
        fds_mean_raw = np.nanmean(framewise_displacements)
        kept_volumes = (
            framewise_displacements < qulaity_control_standards["scrubbing_fd"]
        )
        fds_mean_scrub = np.nanmean(framewise_displacements[kept_volumes])
        proportion_kept = sum(kept_volumes) / timeseries_length
        identifier = Path(confound_file).name.split("_desc-confounds")[0]

        metrics[identifier] = {
            "mean_fd_raw": fds_mean_raw,
            "mean_fd_scrubbed": fds_mean_scrub,
            "proportion_kept": proportion_kept,
        }

    func_filter = {
        "subject": subjects,
        "task": task,
        "space": template,
        "desc": ["brain"],
        "suffix": ["mask"],
        "extension": "nii.gz",
        "datatype": "func",
    }
    func_images = fmriprep_bids_layout.get(**func_filter, return_type="file")
    print("Functional dice...")
    for func_file in tqdm(func_images):
        identifier = Path(func_file).name.split(f"_space-{template}")[0]
        functional_dice = _dice_coefficient(func_file, reference_masks["func"])
        if identifier in metrics:
            metrics[identifier].update({"functional_dice": functional_dice})
        else:
            metrics[identifier] = {"functional_dice": functional_dice}
    metrics = pd.DataFrame(metrics).T
    return metrics.sort_index()


def calculate_anat_metrics(
    subjects: List[str],
    fmriprep_bids_layout: BIDSLayout,
    reference_masks: dict,
) -> pd.DataFrame:
    """
    Calculate the anatomical dice score.

    Parameters
    ----------
    subjects :
        Participant IDs in a BIDS dataset.

    fmriprep_bids_layout :
        BIDS layout of a fMRIPrep derivative.

    reference_masks :
        Reference brain masks for anatomical and functional scans.

    Returns
    -------
    pandas.DataFrame
        Anatomical scan dice score scan quality metrics.
    """
    print("Calculate the anatomical dice score.")
    metrics = {}
    for sub in tqdm(subjects):
        anat_filter = {
            "subject": sub,
            "space": template,
            "desc": ["brain"],
            "suffix": ["mask"],
            "extension": "nii.gz",
            "datatype": "anat",
        }
        anat_image = fmriprep_bids_layout.get(
            **anat_filter, return_type="file"
        )
        # dice
        anat_dice = _dice_coefficient(anat_image[0], reference_masks["anat"])
        metrics[sub] = {
            "anatomical_dice": anat_dice,
        }
    metrics = pd.DataFrame(metrics).T
    metrics["pass_qc"] = (
        metrics["anatomical_dice"]
        > qulaity_control_standards["anatomical_dice"]
    )
    return metrics.sort_index()


def quality_accessments(
    functional_metrics: pd.DataFrame, anatomical_metrics: pd.DataFrame
) -> pd.DataFrame:
    """
    Automatic quality accessments.
    Currently the criteria are a set of preset. Consider allow user providing
    there own.

    Parameters
    ----------

    functional_metrics:
        Functional scan metrics with fMRIPrep file identifier as index.

    anatomical_metrics:
        Anatomical scan metrics with fMRIPrep file identifier as index.

    Returns
    -------
    pandas.DataFrame
        All metric for a set of functional scans and pass / fail assessment.
    """
    keep_fd = (
        functional_metrics["mean_fd_raw"]
        < qulaity_control_standards["mean_fd"]
    )
    keep_proportion = (
        functional_metrics["proportion_kept"]
        > qulaity_control_standards["proportion_kept"]
    )
    keep_func = (
        functional_metrics["functional_dice"]
        > qulaity_control_standards["functional_dice"]
    )
    functional_metrics["pass_func_qc"] = keep_fd * keep_proportion * keep_func

    # get the anatomical pass / fail
    pass_anat_qc = {}
    for id in functional_metrics.index:
        sub = id.split("sub-")[-1].split("_")[0]
        pass_anat_qc[id] = {
            "anatomical_dice": anatomical_metrics.loc[sub, "anatomical_dice"],
            "pass_anat_qc": anatomical_metrics.loc[sub, "pass_qc"],
        }
    anat_qc = pd.DataFrame(pass_anat_qc).T
    metrics = pd.concat((functional_metrics, anat_qc), axis=1)
    metrics["pass_all_qc"] = metrics["pass_func_qc"] * metrics["pass_anat_qc"]
    print(
        f"{int(metrics['pass_all_qc'].sum())} out of {metrics.shape[0]} "
        "functional scans passed automatic QC."
    )
    return metrics


def parse_scan_information(metrics: pd.DataFrame) -> pd.DataFrame:
    """
    Parse the identifier into BIDS entities: subject, session, task, run.
    If session and run are not present, the information will not be parsed.

    Parameters
    ----------

    metrics:
        Quality assessment output with identifier as index.

    Returns
    -------
    pandas.DataFrame
        Quality assessment with BIDS entity separated.
    """
    metrics.index.name = "identifier"
    examplar = metrics.index[0].split("_")
    headers = [e.split("-")[0] for e in examplar]
    identifiers = pd.DataFrame(
        metrics.index.tolist(), index=metrics.index, columns=["identifier"]
    )
    identifiers[headers] = identifiers["identifier"].str.split(
        "_", expand=True
    )
    identifiers = identifiers.drop("identifier", axis=1)
    for h in headers:
        identifiers[h] = identifiers[h].str.replace(f"{h}-", "")
    identifiers = identifiers.rename(columns={"sub": "participant_id"})
    metrics = pd.concat((identifiers, metrics), axis=1)
    return metrics


def _dice_coefficient(
    processed_img: Union[str, Path], template_mask: Union[str, Path]
) -> np.array:
    """Compute the Sørensen-dice coefficient between two n-d volumes."""
    # make sure the inputs are 3d
    processed_img = load_img(processed_img)
    template_mask = load_img(template_mask)

    # resample template to processed image
    if (template_mask.affine != processed_img.affine).any():
        template_mask = resample_to_img(
            template_mask, processed_img, interpolation="nearest"
        )

    # check space, resample target to source space
    processed_img = processed_img.get_fdata().astype(bool)
    template_mask = template_mask.get_fdata().astype(bool)
    intersection = np.sum(np.logical_and(processed_img, template_mask))
    total_elements = np.sum(processed_img) + np.sum(template_mask)
    return 2 * intersection / total_elements