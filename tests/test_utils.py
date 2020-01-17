import shutil
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from pywps import configuration

from finch.processes.utils import (
    get_bccaqv2_opendap_datasets,
    netcdf_to_csv,
    zip_files,
    is_opendap_url,
)
import pytest
from unittest import mock


@mock.patch("finch.processes.utils.TDSCatalog")
def test_get_opendap_datasets_bccaqv2(mock_tdscatalog):
    names = [
        "tasmin_day_BCCAQv2+ANUSPLIN300_CNRM-CM5_historical+rcp85_r1i1p1_19500101-21001231.nc",
        "tasmin_day_BCCAQv2+ANUSPLIN300_CNRM-CM5_historical+rcp45_r1i1p1_19500101-21001231.nc",
        "tasmin_day_BCCAQv2+ANUSPLIN300_CanESM2_historical+rcp45_r1i1p1_19500101-21001231.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_CanESM2_historical+rcp45_r1i1p1_19500101-21001231.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_NorESM1-M_historical+rcp26_r1i1p1_19500101-21001231.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp85_r1i1p1_19500101-21001231.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp45_r1i1p1_19500101-21001231.",
    ]
    catalog_url = configuration.get_config_value("finch", "bccaqv2_url")
    variable = "tasmin"
    rcp = "rcp45"

    mock_catalog = mock.MagicMock()
    mock_tdscatalog.return_value = mock_catalog

    def make_dataset(name):
        dataset = mock.MagicMock()
        dataset.access_urls = {"OPENDAP": "url"}
        dataset.name = name
        return dataset

    mock_catalog.datasets = {name: make_dataset(name) for name in names}

    urls = get_bccaqv2_opendap_datasets(catalog_url, variable, rcp)
    assert len(urls) == 2


def test_netcdf_to_csv_to_zip():
    here = Path(__file__).parent
    folder = here / "data" / "bccaqv2_single_cell"
    output_folder = here / "tmp" / "tasmin_csvs"
    shutil.rmtree(output_folder, ignore_errors=True)

    netcdf_files = list(sorted(folder.glob("tasmin*.nc")))
    # only take a small subset of files that have all the calendar types
    netcdf_files = netcdf_files[:5] + netcdf_files[40:50]
    csv_files, metadata = netcdf_to_csv(netcdf_files, output_folder, "file_prefix")

    output_zip = output_folder / "output.zip"
    files = csv_files + [metadata]
    zip_files(output_zip, files)

    with zipfile.ZipFile(output_zip) as z:
        n_calendar_types = 4
        n_files = len(netcdf_files)
        data_filenames = [n for n in z.namelist() if "metadata" not in n]
        metadata_filenames = [n for n in z.namelist() if "metadata" in n]

        assert len(z.namelist()) == n_files + n_calendar_types
        assert len(metadata_filenames) == n_files
        for filename in data_filenames:
            csv_lines = z.read(filename).decode().split("\n")[1:-1]
            n_lines = len(csv_lines)
            n_columns = len(csv_lines[0].split(",")) - 3

            if "proleptic_gregorian" in filename:
                assert n_lines == 366
                assert n_columns == 2
            elif "365_day" in filename:
                assert n_lines == 365
                assert n_columns == 9
            elif "360_day" in filename:
                assert n_lines == 360
                assert n_columns == 3
            elif "standard" in filename:
                assert n_lines == 366
                assert n_columns == 1
            else:
                assert False, "Unknown calendar type"


def test_netcdf_to_csv_bad_hours():
    here = Path(__file__).parent
    folder = here / "data" / "bccaqv2_single_cell"
    output_folder = here / "tmp" / "tasmin_csvs"
    shutil.rmtree(output_folder, ignore_errors=True)

    # these files contain an hour somewhere at 0 (midnight) it should be 12h
    bad_hours = [
        "pr_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp26_r1i1p1_19500101-21001231_sub.nc",
        "pr_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp45_r1i1p1_19500101-21001231_sub.nc",
        "pr_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp85_r1i1p1_19500101-21001231_sub.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp26_r1i1p1_19500101-21001231_sub.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp45_r1i1p1_19500101-21001231_sub.nc",
        "tasmax_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp85_r1i1p1_19500101-21001231_sub.nc",
        "tasmin_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp26_r1i1p1_19500101-21001231_sub.nc",
        "tasmin_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp45_r1i1p1_19500101-21001231_sub.nc",
        "tasmin_day_BCCAQv2+ANUSPLIN300_NorESM1-ME_historical+rcp85_r1i1p1_19500101-21001231_sub.nc",
    ]
    netcdf_files = [folder / bad for bad in bad_hours]

    csv_files, _ = netcdf_to_csv(netcdf_files, output_folder, "file_prefix")

    for csv in csv_files:
        df = pd.read_csv(csv, parse_dates=["time"])
        assert np.all(df.time.dt.hour == 12)


def test_is_opendap_url():
    # This test uses online requests, but the links should be pretty stable.
    # In case the link are no longer available, we should change the url.
    # This is better than skipping this test in CI.

    url = (
        "https://boreas.ouranos.ca/twitcher/ows/proxy/thredds/dodsC/"
        "birdhouse/nrcan/nrcan_canada_daily_v2/tasmin/nrcan_canada_daily_tasmin_2017.nc"
    )
    assert is_opendap_url(url)

    url = url.replace("dodsC", "fileServer")
    assert not is_opendap_url(url)

    # no Content-Description header
    url = "http://test.opendap.org/opendap/netcdf/examples/tos_O1_2001-2002.nc"
    assert is_opendap_url(url)

    url = "invalid_schema://something"
    assert not is_opendap_url(url)

    url = "https://www.example.com"
    assert not is_opendap_url(url)

    url = "/missing_schema"
    assert not is_opendap_url(url)
