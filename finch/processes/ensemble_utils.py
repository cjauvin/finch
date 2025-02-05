from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, cast
import warnings

from parse import parse
from pywps import ComplexInput, FORMATS, Process
from pywps import configuration
from pywps.app.exceptions import ProcessError
from siphon.catalog import TDSCatalog
import xarray as xr
from xclim import ensembles
from xclim.core.indicator import Indicator
from xclim.core.calendar import percentile_doy, doy_to_days_since, days_since_to_doy

from .constants import (
    ALL_24_MODELS,
    BCCAQV2_MODELS,
    PCIC_12,
    PCIC_12_MODELS_REALIZATIONS,
    bccaq_variables,
    xclim_netcdf_variables,
)
from .subset import finch_subset_bbox, finch_subset_gridpoint, finch_subset_shape
from .utils import (
    PywpsInput,
    RequestInputs,
    compute_indices,
    dataset_to_dataframe,
    dataset_to_netcdf,
    format_metadata,
    log_file_path,
    single_input_or_none,
    write_log,
    zip_files,
)
from .wps_base import make_nc_input


@dataclass
class Bccaqv2File:
    variable: str
    frequency: str
    driving_model_id: str
    driving_experiment_id: str
    driving_realization: str
    driving_initialization_method: str
    driving_physics_version: str
    date_start: Optional[str] = None
    date_end: Optional[str] = None

    @classmethod
    def from_filename(cls, filename):
        pattern = "_".join(
            [
                "{variable}",
                "{frequency}",
                "BCCAQv2+ANUSPLIN300",
                "{driving_model_id}",
                "{driving_experiment_id}",
                "r{driving_realization}i{driving_initialization_method}p{driving_physics_version}",
                "{date_start}-{date_end}.nc",
            ]
        )
        try:
            return cls(**parse(pattern, filename).named)
        except AttributeError:
            return


def _tas(tasmin: xr.Dataset, tasmax: xr.Dataset) -> xr.Dataset:
    """Compute daily mean temperature, and set attributes in the output Dataset."""

    tas = (tasmin["tasmin"] + tasmax["tasmax"]) / 2
    tas_ds = tas.to_dataset(name="tas")
    tas_ds.attrs = tasmin.attrs
    tas_ds["tas"].attrs = tasmin["tasmin"].attrs
    tas_ds["tas"].attrs["long_name"] = "Daily Mean Near-Surface Air Temperature"
    tas_ds["tas"].attrs["cell_methods"] = "time: mean within days"
    return tas_ds


def _percentile_doy_tn10(tasmin: xr.Dataset):
    return percentile_doy(tasmin.tasmin, per=10).sel(percentiles=10, drop=True).to_dataset(name="tn10")


def _percentile_doy_tn90(tasmin: xr.Dataset):
    return percentile_doy(tasmin.tasmin, per=90).sel(percentiles=90, drop=True).to_dataset(name="tn90")


def _percentile_doy_tx90(tasmax: xr.Dataset):
    return percentile_doy(tasmax.tasmax, per=90).sel(percentiles=90, drop=True).to_dataset(name="tx90")


def _percentile_doy_t10(tas: xr.Dataset):
    return percentile_doy(tas.tas, per=10).sel(percentiles=10, drop=True).to_dataset(name="t10")


def _percentile_doy_t90(tas: xr.Dataset):
    return percentile_doy(tas.tas, per=90).sel(percentiles=90, drop=True).to_dataset(name="t90")


variable_computations = {
    "tas": {"inputs": ["tasmin", "tasmax"], "function": _tas},
    "tn10": {"inputs": ["tasmin"], "function": _percentile_doy_tn10},
    "tn90": {"inputs": ["tasmin"], "function": _percentile_doy_tn90},
    "tx90": {"inputs": ["tasmax"], "function": _percentile_doy_tx90},
    "t10": {"inputs": ["tas"], "function": _percentile_doy_t10},
    "t90": {"inputs": ["tas"], "function": _percentile_doy_t90},
}

accepted_variables = bccaq_variables.union(variable_computations)
not_implemented_variables = xclim_netcdf_variables - accepted_variables


class ParsingMethod(Enum):
    # parse the filename directly (faster and simpler, more likely to fail)
    filename = 1
    # parse each Data Attribute Structure (DAS) by appending .das to the url
    # One request for each dataset, so lots of small requests to the Thredds server
    opendap_das = 2
    # open the dataset using xarray and look at the file attributes
    # safer, but slower and lots of small requests are made to the Thredds server
    xarray = 3


def get_bccaqv2_local_files_datasets(
    catalog_url,
    variables: List[str] = None,
    rcp: str = None,
    method: ParsingMethod = ParsingMethod.filename,
    models=None,
) -> List[str]:
    """Get a list of filenames corresponding to variable and rcp on a local filesystem."""

    urls = []
    for file in Path(catalog_url).glob("*.nc"):
        if _bccaqv2_filter(
            method, file.name, str(file), variables=variables, rcp=rcp, models=models
        ):
            urls.append(str(file))
    return urls


def get_bccaqv2_opendap_datasets(
    catalog_url,
    variables: List[str] = None,
    rcp: str = None,
    method: ParsingMethod = ParsingMethod.filename,
    models=None,
) -> List[str]:
    """Get a list of urls corresponding to variable and rcp on a Thredds server.

    We assume that the files are named in a certain way on the Thredds server.

    This is the case for pavics.ouranos.ca/thredds
    For more general use cases, see the `xarray` and `requests` methods below."""

    catalog = TDSCatalog(catalog_url)

    urls = []
    for dataset in catalog.datasets.values():
        opendap_url = dataset.access_urls["OPENDAP"]
        if _bccaqv2_filter(
            method,
            dataset.name,
            opendap_url,
            variables=variables,
            rcp=rcp,
            models=models,
        ):
            urls.append(opendap_url)
    return urls


def _bccaqv2_filter(
    method: ParsingMethod,
    filename,
    url,
    variables: List[str] = None,
    rcp: str = None,
    models=None,
):
    """Parse metadata and filter BCCAQV2 datasets"""

    if models is None or [m.lower() for m in models] == [ALL_24_MODELS.lower()]:
        models = BCCAQV2_MODELS

    models = [m.lower() for m in models]

    if method == ParsingMethod.filename:
        parsed = Bccaqv2File.from_filename(filename)
        if parsed is None:
            return False

        if variables and parsed.variable not in variables:
            return False
        if rcp and rcp not in parsed.driving_experiment_id:
            return False

        if models == [PCIC_12.lower()]:
            for model, realization in PCIC_12_MODELS_REALIZATIONS:
                model_ok = model.lower() == parsed.driving_model_id.lower()
                r_ok = realization[1:] == parsed.driving_realization
                if model_ok and r_ok:
                    return True
            return False

        model_ok = parsed.driving_model_id.lower() in models
        r_ok = parsed.driving_realization == "1"
        return model_ok and r_ok

    elif method == ParsingMethod.opendap_das:

        raise NotImplementedError("todo: filter models and runs")

        # re_experiment = re.compile(r'String driving_experiment_id "(.+)"')
        # lines = requests.get(url + ".das").content.decode().split("\n")
        # variable_ok = variable_ok or any(
        #     line.startswith(f"    {variable} {{") for line in lines
        # )
        # if not rcp_ok:
        #     for line in lines:
        #         match = re_experiment.search(line)
        #         if match and rcp in match.group(1).split(","):
        #             rcp_ok = True

    elif method == ParsingMethod.xarray:

        raise NotImplementedError("todo: filter models and runs")

        # import xarray as xr

        # ds = xr.open_dataset(url, decode_times=False)
        # rcps = [
        #     r
        #     for r in ds.attrs.get("driving_experiment_id", "").split(",")
        #     if "rcp" in r
        # ]
        # variable_ok = variable_ok or variable in ds.data_vars
        # rcp_ok = rcp_ok or rcp in rcps


def get_datasets(
    dataset_name: Optional[str],
    workdir: str,
    variables: Optional[List[str]] = None,
    rcp=None,
    models: Optional[List[str]] = None,
) -> List[PywpsInput]:

    dataset_functions = {"bccaqv2": _get_bccaqv2_inputs}

    if dataset_name is None:
        dataset_name = configuration.get_config_value("finch", "default_dataset")
    dataset_name = cast(str, dataset_name)
    return dataset_functions[dataset_name](
        workdir=workdir, variables=variables, rcp=rcp, models=models
    )


def _get_bccaqv2_inputs(
    workdir: str,
    variables: Optional[List[str]] = None,
    rcp=None,
    models=None,
) -> List[PywpsInput]:
    """Adds a 'resource' input list with bccaqv2 urls to WPS inputs."""
    catalog_url = configuration.get_config_value("finch", "dataset_bccaqv2")

    inputs = []

    def _make_bccaqv2_resource_input():
        return ComplexInput(
            "resource",
            "NetCDF resource",
            max_occurs=1000,
            supported_formats=[FORMATS.NETCDF, FORMATS.DODS],
        )

    if catalog_url.startswith("http"):
        for url in get_bccaqv2_opendap_datasets(
            catalog_url, variables=variables, rcp=rcp, models=models
        ):
            resource = _make_bccaqv2_resource_input()
            resource.url = url
            resource.workdir = workdir
            inputs.append(resource)
    else:
        for file in get_bccaqv2_local_files_datasets(
            catalog_url, variables=variables, rcp=rcp, models=models
        ):
            resource = _make_bccaqv2_resource_input()
            resource.file = file
            resource.workdir = workdir
            inputs.append(resource)

    return inputs


def _formatted_coordinate(value) -> Optional[str]:
    """Returns the first float value.

    The value can be a comma separated list of floats or a single float
    """
    if not value:
        return
    try:
        value = value.split(",")[0]
    except AttributeError:
        pass
    return f"{float(value):.3f}"


def make_output_filename(process: Process, inputs: List[PywpsInput], rcp=None):
    """Returns a filename for the process's output, depending on its inputs.

    The rcp part of the filename can be overriden.
    """
    if rcp is None:
        rcp = single_input_or_none(inputs, "rcp")
    lat = _formatted_coordinate(single_input_or_none(inputs, "lat"))
    lon = _formatted_coordinate(single_input_or_none(inputs, "lon"))
    lat0 = _formatted_coordinate(single_input_or_none(inputs, "lat0"))
    lon0 = _formatted_coordinate(single_input_or_none(inputs, "lon0"))
    lat1 = _formatted_coordinate(single_input_or_none(inputs, "lat1"))
    lon1 = _formatted_coordinate(single_input_or_none(inputs, "lon1"))

    output_parts = [process.identifier]

    if lat and lon:
        output_parts.append(f"{float(lat):.3f}")
        output_parts.append(f"{float(lon):.3f}")
    elif lat0 and lon0:
        output_parts.append(f"{float(lat0):.3f}")
        output_parts.append(f"{float(lon0):.3f}")

    if lat1 and lon1:
        output_parts.append(f"{float(lat1):.3f}")
        output_parts.append(f"{float(lon1):.3f}")

    if rcp:
        output_parts.append(rcp)

    return "_".join(output_parts)


def uses_accepted_netcdf_variables(indicator: Indicator) -> bool:
    """Returns True if this indicator uses  netcdf variables in `accepted_variables`."""
    return not any(p in not_implemented_variables for p in indicator.parameters)


def make_indicator_inputs(
    indicator: Indicator, wps_inputs: RequestInputs, files_list: List[Path]
) -> List[RequestInputs]:
    """From a list of files, make a list of inputs used to call the given xclim indicator."""

    arguments = set(indicator.parameters)

    required_netcdf_args = accepted_variables.intersection(arguments)

    input_list = []

    if len(required_netcdf_args) == 1:
        variable_name = list(required_netcdf_args)[0]
        for path in files_list:
            inputs = deepcopy(wps_inputs)
            inputs[variable_name] = deque([make_nc_input(variable_name)])
            inputs[variable_name][0].file = str(path)
            input_list.append(inputs)
    else:
        for group in make_file_groups(files_list):
            inputs = deepcopy(wps_inputs)
            for variable_name, path in group.items():
                if variable_name not in required_netcdf_args:
                    continue
                inputs[variable_name] = deque([make_nc_input(variable_name)])
                inputs[variable_name][0].file = str(path)
            input_list.append(inputs)

    return input_list


def make_file_groups(files_list: List[Path]) -> List[Dict[str, Path]]:
    """Groups files by filenames, changing only the netcdf variable name."""
    groups = []
    filenames = {f.name: f for f in files_list}

    for file in files_list:
        if file.name not in filenames:
            continue
        group = {}
        for variable in accepted_variables:
            if file.name.startswith(f"{variable}_"):
                for other_var in accepted_variables.difference([variable]):
                    other_filename = file.name.replace(variable, other_var, 1)
                    if other_filename in filenames:
                        group[other_var] = filenames[other_filename]
                        del filenames[other_filename]

                group[variable] = file
                del filenames[file.name]
                groups.append(group)
                break

    return groups


def make_ensemble(files: List[Path], percentiles: List[int]) -> None:
    ensemble = ensembles.create_ensemble(files)
    # make sure we have data starting in 1950
    ensemble = ensemble.sel(time=(ensemble.time.dt.year >= 1950))

    # If data is in day of year, percentiles won't make sense.
    # Convert to "days since" (base will be the time coordinate)
    for v in ensemble.data_vars:
        if ensemble[v].attrs.get('is_dayofyear', 0) == 1:
            ensemble[v] = doy_to_days_since(ensemble[v])

    ensemble_percentiles = ensembles.ensemble_percentiles(ensemble, values=percentiles)

    # Doy data converted previously is converted back.
    for v in ensemble_percentiles.data_vars:
        if ensemble_percentiles[v].attrs.get('units', '').startswith('days after'):
            ensemble_percentiles[v] = days_since_to_doy(ensemble_percentiles[v])

    if "realization" in ensemble_percentiles.coords:
        # realization coordinate will probably be removed in xclim
        # directly in the near future so this line will not be necessary
        ensemble_percentiles = ensemble_percentiles.drop_vars("realization")

    # Depending on the datasets, I've found that writing the netcdf could hang
    # if the dataset was not loaded explicitely previously... Not sure why.
    # The datasets should be pretty small when computing the ensembles, so this is
    # a best effort at working around what looks like a bug in either xclim or xarray.
    # The xarray documentation mentions: 'this method can be necessary when working
    # with many file objects on disk.'
    ensemble_percentiles.load()

    return ensemble_percentiles


def compute_intermediate_variables(
    files_list: List[Path], required_variable_names: Iterable[str], workdir: Path
) -> List[Path]:
    """Compute netcdf datasets from a list of required variable names and existing files."""

    output_files_list = []

    file_groups = make_file_groups(files_list)

    for group in file_groups:
        # add file paths that are required without any computation
        for variable, path in group.items():
            if variable in required_variable_names:
                output_files_list.append(path)

        first_variable = list(group)[0]
        output_basename = group[first_variable].name.split("_", 1)[1]

        # compute other required variables
        variables_to_compute = set(required_variable_names) - set(group)

        # add intermediate files to compute (ex: tas is needed for tn10)
        for variable in list(variables_to_compute):
            for input_name in variable_computations[variable]["inputs"]:
                if input_name in variable_computations:
                    variables_to_compute.add(input_name)

        while variables_to_compute:
            for variable in list(variables_to_compute):
                input_names = variable_computations[variable]["inputs"]
                if all(i in group for i in input_names):
                    inputs = [xr.open_dataset(group[name]) for name in input_names]

                    output = variable_computations[variable]["function"](*inputs)
                    output_file = Path(workdir) / f"{variable}_{output_basename}"
                    dataset_to_netcdf(output, output_file)

                    variables_to_compute.remove(variable)
                    group[variable] = output_file
                    if variable in required_variable_names:
                        output_files_list.append(output_file)
                    break
            else:
                raise RuntimeError(
                    f"Cant compute intermediate variables {variables_to_compute}"
                )

    return output_files_list


def get_sub_inputs(variables):
    """From a list of dataset variables, get the source variable names to compute them."""

    output_variables = list(variables)
    while any(v in variable_computations for v in output_variables):
        new_output_variables = []
        for variable in output_variables:
            if variable in variable_computations:
                new_output_variables += variable_computations[variable]["inputs"]
            else:
                new_output_variables.append(variable)
        output_variables = new_output_variables
    return output_variables


def ensemble_common_handler(process: Process, request, response, subset_function):
    assert subset_function in [
        finch_subset_bbox,
        finch_subset_gridpoint,
        finch_subset_shape,
    ]

    xci_inputs = process.xci_inputs_identifiers
    request_inputs_not_datasets = {
        k: v for k, v in request.inputs.items() if k in xci_inputs
    }
    dataset_input_names = accepted_variables.intersection(xci_inputs)
    source_variable_names = bccaq_variables.intersection(
        get_sub_inputs(dataset_input_names)
    )

    convert_to_csv = request.inputs["output_format"][0].data == "csv"
    if not convert_to_csv:
        del process.status_percentage_steps["convert_to_csv"]
    percentiles_string = request.inputs["ensemble_percentiles"][0].data
    ensemble_percentiles = [int(p.strip()) for p in percentiles_string.split(",")]

    rcps = [r.data.strip() for r in request.inputs["rcp"]]
    write_log(process, f"Processing started ({len(rcps)} rcps)", process_step="start")
    models = [m.data.strip() for m in request.inputs["models"]]
    dataset_name = single_input_or_none(request.inputs, "dataset")

    base_work_dir = Path(process.workdir)
    ensembles = []
    for rcp in rcps:
        # Ensure no file name conflicts (i.e. if the rcp doesn't appear in the base filename)
        work_dir = base_work_dir / rcp
        work_dir.mkdir(exist_ok=True)
        process.set_workdir(str(work_dir))

        write_log(process, f"Fetching datasets for rcp={rcp}")
        output_filename = make_output_filename(process, request.inputs, rcp=rcp)
        netcdf_inputs = get_datasets(
            dataset_name,
            workdir=process.workdir,
            variables=list(source_variable_names),
            rcp=rcp,
            models=models,
        )

        write_log(process, f"Running subset rcp={rcp}", process_step="subset")

        subsetted_files = subset_function(
            process, netcdf_inputs=netcdf_inputs, request_inputs=request.inputs
        )

        if not subsetted_files:
            message = "No data was produced when subsetting using the provided bounds."
            raise ProcessError(message)

        subsetted_intermediate_files = compute_intermediate_variables(
            subsetted_files, dataset_input_names, process.workdir
        )

        write_log(process, f"Computing indices rcp={rcp}", process_step="compute_indices")

        input_groups = make_indicator_inputs(
            process.xci, request_inputs_not_datasets, subsetted_intermediate_files
        )
        n_groups = len(input_groups)

        indices_files = []

        warnings.filterwarnings("ignore", category=FutureWarning)
        warnings.filterwarnings("ignore", category=UserWarning)

        for n, inputs in enumerate(input_groups):
            write_log(
                process,
                f"Computing indices for file {n + 1} of {n_groups}, rcp={rcp}",
                subtask_percentage=n * 100 // n_groups,
            )
            output_ds = compute_indices(process, process.xci, inputs)

            output_name = f"{output_filename}_{process.identifier}_{n}.nc"
            for variable in accepted_variables:
                if variable in inputs:
                    input_name = Path(inputs.get(variable)[0].file).name
                    output_name = input_name.replace(variable, process.identifier)

            output_path = Path(process.workdir) / output_name
            dataset_to_netcdf(output_ds, output_path)
            indices_files.append(output_path)

        warnings.filterwarnings("default", category=FutureWarning)
        warnings.filterwarnings("default", category=UserWarning)

        output_basename = Path(process.workdir) / (output_filename + "_ensemble")
        ensemble = make_ensemble(indices_files, ensemble_percentiles)
        ensemble.attrs['source_datasets'] = '\n'.join([dsinp.url for dsinp in netcdf_inputs])
        ensembles.append(ensemble)

    process.set_workdir(str(base_work_dir))

    if len(rcps) > 1:
        ensemble = xr.concat(ensembles, dim=xr.DataArray(rcps, dims=('rcp',), name='rcp'))
    else:
        ensemble = ensembles[0]

    if convert_to_csv:
        ensemble_csv = output_basename.with_suffix(".csv")
        df = dataset_to_dataframe(ensemble)
        df = df.reset_index().set_index(["lat", "lon", "time"])
        if "region" in df.columns:
            df.drop(columns="region", inplace=True)

        df.dropna().to_csv(ensemble_csv)

        metadata = format_metadata(ensemble)
        metadata_file = output_basename.parent / f"{ensemble_csv.stem}_metadata.txt"
        metadata_file.write_text(metadata)

        ensemble_output = Path(process.workdir) / (output_filename + ".zip")
        zip_files(ensemble_output, [metadata_file, ensemble_csv])
    else:
        ensemble_output = output_basename.with_suffix(".nc")
        dataset_to_netcdf(ensemble, ensemble_output)

    response.outputs["output"].file = ensemble_output
    response.outputs["output_log"].file = str(log_file_path(process))

    write_log(process, "Processing finished successfully", process_step="done")
    return response
