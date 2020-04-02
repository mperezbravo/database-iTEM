from copy import copy
from functools import lru_cache
import os

import pandas as pd
import pycountry
import yaml

from item.common import paths
from item.remote import OpenKAPSARC, get_sdmx
from .scripts import T001
from .scripts.util.managers.dataframe import ColumnName


#: List of data processing Jupyter/IPython notebooks.
SCRIPTS = [
    'T000',
    'T001',
    'T002',
    'T003',
    'T004',
    'T005',
    'T006',
    'T007',
    'T008'
]

#:
MODULES = {
    1: T001
}

OUTPUT_PATH = paths['data'] / 'historical' / 'output'

#: Non-ISO names appearing in 1 or more data sets; see :meth:`iso_and_region`.
COUNTRY_NAME = {
    "Montenegro, Republic of": "Montenegro",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Korea": "Korea, Republic of",
    "Serbia, Republic of": "Serbia",
}


#: Map from ISO 3166 alpha-3 code to region name.
REGION = {}
# Populate the map from the regions.yaml file
with open(paths['data'] / 'model' / 'regions.yaml') as file:
    for region_name, info in yaml.safe_load(file).items():
        REGION.update({c: region_name for c in info['countries']})


with open(paths['data'] / 'historical' / 'sources.yaml') as f:
    #:
    SOURCES = yaml.safe_load(f)


def cache_results(id_str, df):
    """Write *df* to cache in two file formats.

    The files written are:

    - :file:`{id_str}_cleaned_PF.csv`, in long or 'programming-friendly'
      format.
    - :file:`{id_str}_cleaned_UF.csv`, in wide or 'user-friendly' format.
    """
    OUTPUT_PATH.mkdir(exist_ok=True)

    # Long format ('programming friendly view')
    path = OUTPUT_PATH / f'{id_str}_cleaned_PF.csv'
    df.to_csv(path, index=False)
    print(f'Write {path}')

    # Pivot to wide format ('user friendly view') and write to CSV
    columns = [ev.value for ev in ColumnName if ev != ColumnName.VALUE]
    path = OUTPUT_PATH / f'{id_str}_cleaned_UF.csv'
    df.set_index(columns) \
      .unstack(ColumnName.YEAR.value) \
      .reset_index()\
      .to_csv(path, index=False)

    print(f'Write {path}')


def fetch_source(id, use_cache=True):
    """Fetch data from source *id*.

    Parameters
    ----------
    use_cache : bool, optional
        If given, use a cached file. No check of cache validity is performed.
    """
    # Retrieve source information from sources.yaml
    id = source_str(id)
    source_info = copy(SOURCES[id])

    # Path for cached data. NB OpenKAPSARC does its own caching
    cache_path = paths['historical input'] / f'{id}.csv'

    if use_cache and cache_path.exists():
        return cache_path

    # Information for fetching the data
    fetch_info = source_info['fetch']

    remote_type = fetch_info.pop('type')
    if remote_type.lower() == 'sdmx':
        # Use SDMX to retrieve the data
        result = get_sdmx(**fetch_info)
    elif remote_type.lower() == 'openkapsarc':
        # Retrieve data using the OpenKAPSARC API
        ok_api = OpenKAPSARC(api_key=os.environ.get('OK_API_KEY', None))
        result = ok_api.table(**fetch_info)
    else:
        raise ValueError(remote_type)

    # Cache the results
    result.to_csv(cache_path, index=False)

    return cache_path


def input_file(id: int):
    """Return the path to a cached, raw input data file for data source *id*.

    CSV files are located in the 'historical input' data path. If more than
    one file has a name beginning with “T{id}”, the last sorted file is
    returned.
    """
    # List of all matching files
    all_files = sorted(paths['historical input']
                       .glob(f'{source_str(id)}*.csv'))

    # The last file has the most recent timestamp
    return all_files[-1]


def process(id):
    """Process a data set given its *id*.

    Performs the following common processing steps:

    1. Load the data from cache.
    2. Load a module defining dataset-specific processing steps. This module
       is in a file named e.g. :file:`T001.py`.
    3. Call the dataset's (optional) :meth:`check` method. This method receives
       the input data frame as an argument, and can make one or more assertions
       to ensure the data is in the expected format.
    4. Drop columns in the dataset's (optional) :data:`DROP_COLUMNS`
       :class:`list`.
    5. Call the dataset-specific (required) :meth:`process` method. This method
       receives the data frame from step (4), and performs any additional
       processing.
    6. Assign ISO 3166 alpha-3 codes and the iTEM region based on a column
       containing country names.
    7. Assign common dimensions from the dataset's (optional)
       :data:`COMMON_DIMS` :class:`dict`.
    8. Order columns.
    9. Output data to two files.

    """
    # Load the data from a common location, based on the dataset ID
    id_str = source_str(id)
    path = paths['data'] / 'historical' / 'input' / f'{id_str}_input.csv'
    df = pd.read_csv(path)

    # Get the module for this data set
    dataset_module = MODULES[1]

    try:
        # Check that the input data is of the form expected by process()
        dataset_module.check(df)
    except AttributeError:
        # Optional check() function does not exist
        print('No pre-processing checks to perform')
    except AssertionError as e:
        # An 'assert' statement in check() failed
        print(f'Input data is invalid: {e}')

    # Information about columns. If not defined, use defaults.
    columns = getattr(dataset_module, 'COLUMNS', dict(country_name='Country'))

    try:
        # List of column names to drop
        drop_cols = columns['drop']
    except KeyError:
        # No variable COLUMNS in dataset_module, or no key 'drop'
        print(f'No columns to drop for {id_str}')
    else:
        df.drop(columns=drop_cols, inplace=True)
        print('Drop {len(drop_cols)} extra column(s)')

    # Call the dataset-specific process() function; returns a modified df
    df = dataset_module.process(df)
    print(f'{len(df)} observations')

    # Assign ISO 3166 alpha-3 codes and iTEM regions from a country name column
    country_col = columns['country_name']
    # Use pandas.Series.apply() to apply the same function to each entry in
    # the column. Join these to the existing data frame as additional columns.
    df = pd.concat([df, df[country_col].apply(iso_and_region)], axis=1)

    # Values to assign across all observations: the dataset ID
    assign_values = {ColumnName.ID.value: id_str}

    # Handle any COMMON_DIMS, if defined
    for dim, value in getattr(dataset_module, 'COMMON_DIMS', {}).items():
        # Standard name for the column
        col_name = getattr(ColumnName, dim.upper()).value
        # Copy the value to be assigned
        assign_values[col_name] = value

    # - Assign the values.
    # - Order the columns in the standard order.
    df = df.assign(**assign_values) \
           .reindex(columns=[ev.value for ev in ColumnName])

    # Save the result to cache
    cache_results(id_str, df)

    # Return the data for use by other code
    return df


@lru_cache()
def iso_and_region(name):
    """Return (ISO 3166 alpha-3 code, iTEM region) for a country *name*."""
    # lru_cache() ensures this function call is as fast as a dictionary lookup
    # after the first time each country name is seen

    # Maybe map a known, non-standard value to a standard value
    name = COUNTRY_NAME.get(name, name)

    # Use pycountry's built-in, case-insensitive lookup on all fields including
    # name, official_name, and common_name
    alpha_3 = pycountry.countries.lookup(name).alpha_3

    # Look up the region, construct a Series, and return
    return pd.Series(
        [alpha_3, REGION.get(alpha_3, 'N/A')],
        index=[ColumnName.ISO_CODE.value, ColumnName.ITEM_REGION.value])


def source_str(id):
    """Return the canonical string name (e.g. 'T001') for a data source."""
    return f'T{id:03}' if isinstance(id, int) else id
