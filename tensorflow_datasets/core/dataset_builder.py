# coding=utf-8
# Copyright 2018 The TensorFlow Datasets Authors.
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

"""DatasetBuilder base class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import collections
import datetime
import os
import enum

import six
import tensorflow as tf

from tensorflow_datasets.core import api_utils
from tensorflow_datasets.core import dataset_utils
from tensorflow_datasets.core import download
from tensorflow_datasets.core import file_format_adapter
from tensorflow_datasets.core import naming
from tensorflow_datasets.core import registered
from tensorflow_datasets.core import utils

import termcolor

__all__ = [
    "Split",
    "SplitFiles",
    "DatasetBuilder",
    "SplitGenerator",
    "GeneratorBasedDatasetBuilder",
]

DEFAULT_DATA_DIR = os.path.join("~", "tensorflow_datasets")


class Split(enum.Enum):
  """`Enum` for dataset splits.

  Datasets are typically split into different subsets to be used at various
  stages of training and evaluation. All datasets have at least the `TRAIN` and
  `TEST` splits.

  Note that for datasets without a `VALIDATION` split, you should use a fraction
  of the `TRAIN` data for evaluation as you iterate on your model so as not to
  overfit to the `TEST` data. You can do so by...

  TODO(rsepassi): update when as_dataset supports this.

  * `TRAIN`: the training data.
  * `VALIDATION`: the validation data. If present, this is typically used as
    evaluation data while iterating on a model (e.g. changing hyperparameters,
    model architecture, etc.).
  * `TEST`: the testing data. This is the data to report metrics on. Typically
    you do not want to use this during model iteration as you may overfit to it.
  """
  TRAIN = "train"
  VALIDATION = "validation"
  TEST = "test"


class SplitFiles(object):
  """Utility to produce filepaths and filepatterns for a Split."""

  def __init__(self, dataset_name, split, num_shards, data_dir,
               filetype_suffix=None):
    """Constructs a SplitFiles object.

    Args:
      dataset_name: `str`, name of the dataset. Typically `DatasetBuilder.name`.
      split: `tfds.Split`, which split of the dataset.
      num_shards: `int`, number of file shards for this split on disk.
      data_dir: `str`, directory containing the data files.
      filetype_suffix: `str`, if provided, will be added to the filenames before
        the sharding specification (e.g.
        "foo_dataset-train.csv-00000-of-00001").
    """
    self.dataset_name = dataset_name
    self.split = split
    self.num_shards = num_shards
    self.data_dir = data_dir
    self.filetype_suffix = filetype_suffix

  @property
  def filepaths(self):
    """Returns list of filepaths for this split."""
    return naming.filepaths_for_dataset_split(
        dataset_name=self.dataset_name,
        split=self.split,
        num_shards=self.num_shards,
        data_dir=self.data_dir,
        filetype_suffix=self.filetype_suffix)

  @property
  def filepattern(self):
    """Returns a Glob filepattern for this split."""
    return naming.filepattern_for_dataset_split(
        dataset_name=self.dataset_name,
        split=self.split,
        data_dir=self.data_dir,
        filetype_suffix=self.filetype_suffix)

  def exists(self):
    return file_format_adapter.do_files_exist(self.filepaths)


# TODO(afrozm): Replace by the metadata DatasetInfo
class DatasetInfo(collections.namedtuple("DatasetInfo", ["specs"])):
  """Structure defining the info of the dataset.

  DatasetInfo
  """


# TODO(rsepassi): Add info() property
@six.add_metaclass(registered.RegisteredDataset)
class DatasetBuilder(object):
  """Abstract base class for datasets.

  Typical usage:

  ```python
  mnist_builder = tfds.MNIST(data_dir="~/tfds_data")
  mnist_builder.download_and_prepare()
  train_dataset = mnist_builder.as_dataset(tfds.Split.TRAIN)
  assert isinstance(train_dataset, tf.data.Dataset)

  # And then the rest of your input pipeline
  train_dataset = train_dataset.repeat().shuffle(1024).batch(128).prefetch(4)
  features = train_dataset.make_one_shot_iterator().get_next()
  image, label = features['input'], features['target']
  ```
  """

  name = None  # Name of the dataset, filled by metaclass based on class name.
  SIZE = None  # Approximate size of dataset, if known, in GB.
  # TODO(pierrot): take size from DatasetInfo.

  @api_utils.disallow_positional_args
  def __init__(self, data_dir=None):
    """Construct a DatasetBuilder.

    Callers must pass arguments as keyword arguments.

    Args:
      data_dir (str): directory to read/write data.
        Optional, useful for testing.
    """
    self._data_dir_root = os.path.expanduser(data_dir or DEFAULT_DATA_DIR)
    # Get the last dataset if it exists (or None otherwise)
    self._data_dir = self._get_data_dir()

  @utils.memoized_property
  def info(self):
    """Return the dataset info object. See `DatasetInfo` for details."""
    return self._info()

  @api_utils.disallow_positional_args
  def download_and_prepare(self, cache_dir=None, dl_manager=None):
    """Downloads and prepares dataset for reading.

    Subclasses must override _download_and_prepare.

    Args:
      cache_dir (str): Cached directory where to extract the data. If None,
        a default tmp directory will be used.
      dl_manager (DownloadManager): DownloadManager to use. Only one of
        dl_manager and cache_dir can be set

    Raises:
      ValueError: If the user defines both cache_dir and dl_manager
    """
    # Both args are set
    if cache_dir and dl_manager is not None:
      raise ValueError("Only one of dl_manager and cache_dir can be defined.")
    # None are set. Use the data_dir as cache_dir
    if not cache_dir and dl_manager is None:
      cache_dir = os.path.join(self._data_dir_root, "tmp")

    # Create the download manager
    if cache_dir:
      dl_manager = download.DownloadManager(cache_dir=cache_dir)

    # If the dataset already exists (data_dir not empty) and that we do not
    # overwrite the dataset
    if (self._data_dir and
        dl_manager.mode == download.GenerateMode.REUSE_DATASET_IF_EXISTS):
      tf.logging.info("Reusing dataset %s (%s)", self.name, self._data_dir)
      return

    # Otherwise, create a new version in a new data_dir.
    curr_date = datetime.datetime.now()
    version_str = curr_date.strftime("v_%Y%m%d_%H%M")
    data_dir = self._get_data_dir(version=version_str)
    tf.logging.info("Generating dataset %s (%s)", self.name, data_dir)

    # Print is intentional: we want this to always go to stdout so user has
    # information needed to cancel download/preparation if needed.
    # This comes right before the progress bar.
    size_text = termcolor.colored("%s GB" % self.SIZE or "?", attrs=["bold"])
    termcolor.cprint("Downloading / extracting dataset %s (%s) to %s..." % (
        self.name, size_text, data_dir))

    # Wrap the Dataset generation in a .incomplete directory
    with file_format_adapter.incomplete_dir(data_dir) as data_dir_tmp:
      # TODO(epot): Data_dir should be an argument of download_and_prepare.
      # Modify this once a better split API exists.
      self._data_dir = data_dir_tmp
      self._download_and_prepare(dl_manager)
      self._data_dir = data_dir

  # TODO(rsepassi): Make it easy to further shard the TRAIN data (e.g. for
  # synthetic VALIDATION splits).
  @api_utils.disallow_positional_args
  def as_dataset(self, split, shuffle_files=None):
    """Constructs a `tf.data.Dataset`.

    Callers must pass arguments as keyword arguments.

    Subclasses must override _as_dataset.

    Args:
      split: `tfds.Split`, which subset of the data to read.
      shuffle_files: `bool` (optional), whether to shuffle the input files.
        Defaults to `True` if `split == tfds.Split.TRAIN` and `False` otherwise.

    Returns:
      `tf.data.Dataset`
    """
    if not self._data_dir:
      raise AssertionError(
          ("Dataset %s: could not find data in %s. Please make sure to call "
           "dataset_builder.download_and_prepare(), or pass download=True to "
           "tfds.load() before trying to access the tf.data.Dataset object."
          ) % (self.name, self._data_dir_root))
    return self._as_dataset(split=split, shuffle_files=shuffle_files)

  def numpy_iterator(self, **as_dataset_kwargs):
    """Generates numpy elements from the given `tfds.Split`.

    This generator can be useful for non-TensorFlow programs.

    Args:
      **as_dataset_kwargs: Keyword arguments passed on to
        `tfds.DatasetBuilder.as_dataset`.

    Returns:
      Generator yielding feature dictionaries
      `dict<str feature_name, numpy.array feature_val>`.
    """
    def iterate():
      dataset = self.as_dataset(**as_dataset_kwargs)
      dataset = dataset.prefetch(128)
      return dataset_utils.iterate_over_dataset(dataset)

    if tf.executing_eagerly():
      return iterate()
    else:
      with tf.Graph().as_default():
        return iterate()

  def _get_data_dir(self, version=None):
    """Return the data directory of one dataset version.

    Args:
      version (str): If specified, return the data_dir associated with the
        given version

    Returns:
      data_dir (str):
        If version is given, return the data_dir associated with this version.
        Otherwise, automatically extract the last version from the directory.
        If no previous version is found, return None.
    """
    data_root_dir = os.path.join(self._data_dir_root, self.name)
    if version is not None:
      return os.path.join(data_root_dir, version)

    # Get the most recent directory
    if tf.gfile.Exists(data_root_dir):
      version_dirnames = [
          f for f in sorted(tf.gfile.ListDirectory(data_root_dir))
          if ".incomplete" not in f
      ]
      if version_dirnames:
        return os.path.join(data_root_dir, version_dirnames[-1])

    # No directory found
    return None

  def _split_files(self, **kwargs):
    kwargs["dataset_name"] = self.name
    kwargs["data_dir"] = self._data_dir
    return SplitFiles(**kwargs)

  @abc.abstractmethod
  def _info(self):
    """Construct the DatasetInfo object. See `DatasetInfo` for details.

    Warning: This function is only called once and the result is cached for all
    following .info() calls.

    Returns:
      dataset_info (DatasetInfo): The dataset information
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _download_and_prepare(self, dl_manager):
    """Downloads and prepares dataset for reading.

    This is the internal implementation to overwritte called when user call
    `download_and_prepare`. It should download all required data and generate
    the pre-processed datasets files.

    Args:
      dl_manager (DownloadManager): `DownloadManager` used to download and cache
        data.
    """
    raise NotImplementedError

  @abc.abstractmethod
  def _as_dataset(self, split, shuffle_files=None):
    """Constructs a `tf.data.Dataset`.

    This is the internal implementation to overwritte called when user call
    `as_dataset`. It should read the pre-processed datasets files and generate
    the `tf.data.Dataset` object.

    Args:
      split (`tfds.Split`): which subset of the data to read.
      shuffle_files (bool): whether to shuffle the input files. Optional,
        defaults to `True` if `split == tfds.Split.TRAIN` and `False` otherwise.

    Returns:
      `tf.data.Dataset`
    """
    raise NotImplementedError


class SplitGenerator(collections.namedtuple("_SplitGenerator",
                                            ["generator_fn", "split_files"])):
  """Contains a generator to produce examples across splits.

  Args:
    generator_fn: function with no arguments yielding feature dictionaries.
    split_files: `list<SplitFiles>`, splits that the examples from
      `generator_fn` should be sharded across.
  """

  def output_files_exist(self):
    """Whether all the specified output files exist."""
    return all([split.exists() for split in self.split_files])

  @property
  def output_files(self):
    """Output files combined across `split_files`."""
    output_files = []
    for split in self.split_files:
      output_files.extend(split.filepaths)
    return output_files

  @property
  def splits(self):
    return [sf.split for sf in self.split_files]


class GeneratorBasedDatasetBuilder(DatasetBuilder):
  """Base class for datasets with data generation based on dict generators.

  `GeneratorBasedDatasetBuilder` is a convenience class that abstracts away much
  of the data writing and reading of `DatasetBuilder`. It expects subclasses to
  implement generators of feature dictionaries across the dataset splits
  (`_dataset_split_generators`) and to specify a file type
  (`_file_format_adapter`). See the method docstrings for details.

  Minimally, subclasses must override `_dataset_split_generators` and
  `_file_format_adapter`.

  `FileFormatAdapter`s are defined in
  `tensorflow_datasets.core.file_format_adapter` and specify constraints on the
  feature dictionaries yielded by example generators. See the class docstrings.
  """

  @api_utils.disallow_positional_args
  def __init__(self, **kwargs):
    """Builder constructor.

    Args:
      **kwargs: Constructor kwargs forwarded to DatasetBuilder
    """
    super(GeneratorBasedDatasetBuilder, self).__init__(**kwargs)

    # Load the format adapter (CSV, TF-Record,...)
    file_adapter_cls = file_format_adapter.TFRecordExampleAdapter
    file_specs = self.info.specs.get_specs()
    self._file_format_adapter = file_adapter_cls(file_specs)

  @abc.abstractmethod
  def _dataset_split_generators(self, dl_manager):
    """Specify feature dictionary generators and dataset splits.

    This function returns a list of `SplitGenerator`s.
    Each generator yields feature dictionaries (`dict<str feature_name,
    feature_value>`).  The examples yielded by each generator will be written to
    the specified `SplitFile`s.

    If a generator produces data exclusively for a single split, then the
    `SplitGenerator` should have only a single `SplitFile`.

    If a generator produces examples that should be sharded across multiple
    splits (this is the case if the underlying dataset does not have pre-defined
    data splits), then the `SplitGenerator` will have multiple `SplitFile`s
    (equal to the number of splits the examples should be sharded across). The
    proportion of the examples that will end up in each split is defined by the
    relative number of shards each `ShardFiles` object specifies. For example:

    ```
    def _dataset_split_generators(self):
      return [
          SplitGenerator(
              generator_fn=my_generator_fn,
              split_files=[
                  self._split_files(split=Split.TRAIN, num_shards=2),
                  self._split_files(split=Split.VALIDATION, num_shards=2),
                  self._split_files(split=Split.TEST, num_shards=2)
              ]
          )
      ]
    ```

    The examples from `my_generator_fn` would be split evenly across the 3
    `Split`s provided.

    Each `SplitFiles` can be constructed with the `_split_files` helper method
    (`self._split_files(split=Split.TRAIN, num_shards=10)`) which fills in
    common fields.

    For downloads and extractions, use the given `download_manager`.
    Note that the `DownloadManager` caches downloads, so it is fine to have each
    generator attempt to download the source data.

    Args:
      dl_manager (DownloadManager): Download manager to download the data

    Returns:
      `list<SplitGenerator`>.
    """
    raise NotImplementedError()

  def _download_and_prepare(self, dl_manager):
    if not tf.gfile.Exists(self._data_dir):
      tf.gfile.MakeDirs(self._data_dir)
    for split_generator in self._dataset_split_generators(dl_manager):
      if split_generator.output_files_exist():
        tf.logging.info("Skipping download_and_prepare for splits %s as all "
                        "files exist.", split_generator.splits)
        continue
      self._file_format_adapter.write_from_generator(
          split_generator.generator_fn, split_generator.output_files)

  def _as_dataset(self, split=Split.TRAIN, shuffle_files=None):
    tf_data = dataset_utils.build_dataset(
        filepattern=self._split_files(num_shards=None, split=split).filepattern,
        dataset_from_file_fn=self._file_format_adapter.dataset_from_filename,
        shuffle_files=(
            split == Split.TRAIN if shuffle_files is None else shuffle_files))
    tf_data = tf_data.map(self.info.specs.decode_sample)
    return tf_data

  def _split_files(self, **kwargs):
    kwargs["dataset_name"] = self.name
    kwargs["data_dir"] = self._data_dir
    kwargs["filetype_suffix"] = self._file_format_adapter.filetype_suffix
    return SplitFiles(**kwargs)
