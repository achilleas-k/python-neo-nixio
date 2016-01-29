# Copyright (c) 2014, German Neuroinformatics Node (G-Node)
#                     Achilleas Koutsou <achilleas.k@gmail.com>
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted under the terms of the BSD License. See
# LICENSE file in the root of the Project.

from __future__ import absolute_import

from datetime import datetime
import numpy as np
import quantities as pq

from neo.io.baseio import BaseIO
from neo.core import Block, Segment, RecordingChannelGroup

try:
    import nix
except ImportError:
    raise ImportError("Failed to import NIX (NIXPY not found). "
                      "The NixIO requires the Python bindings for NIX.")


attribute_mappings = {"name": "name",
                      "description": "definition"}
container_mappings = {"segments": "groups",
                      "recordingchannelgroups": "sources"}


def calculate_timestamp(dt):
    return int((dt - datetime.fromtimestamp(0)).total_seconds())


class NixIO(BaseIO):
    """
    Class for reading and writing NIX files.
    """

    is_readable = False  # for now
    is_writable = True

    supported_objects = [Block, Segment, RecordingChannelGroup]
    readable_objects = []
    writeable_objects = [Block, Segment, RecordingChannelGroup]

    name = "NIX"
    extensions = ["h5"]
    mode = "file"

    def __init__(self, filename):
        """
        Initialise IO instance and NIX file.

        :param filename: full path to the file
        """
        BaseIO.__init__(self, filename=None)
        self.filename = filename
        if self.filename:
            self.nix_file = nix.File.open(self.filename, nix.FileMode.Overwrite)

    def __del__(self):
        self.nix_file.close()

    def write_block(self, neo_block, cascade=True):
        """
        Convert ``neo_block`` to the NIX equivalent and write it to the file.
        If ``cascade`` is True, write all the block's child objects as well.

        :param neo_block: Neo block to be written
        :param cascade: save all child objects (default: True)
        """
        nix_name = neo_block.name
        nix_type = "neo.block"
        nix_definition = neo_block.description
        nix_block = self.nix_file.create_block(nix_name, nix_type)
        nix_block.definition = nix_definition
        # TODO: Handle timestamps in a Python2 compatible way
        if neo_block.rec_datetime:
            # Truncating timestamp to seconds
            nix_block.force_created_at(
                    calculate_timestamp(neo_block.rec_datetime))
        if neo_block.file_datetime:
            block_metadata = self._get_or_init_metadata(nix_block)
            # Truncating timestamp to seconds
            block_metadata.create_property(
                    "file_datetime",
                    nix.Value(
                            calculate_timestamp(neo_block.file_datetime)))
        if neo_block.file_origin:
            block_metadata = self._get_or_init_metadata(nix_block)
            block_metadata.create_property("file_origin",
                                           nix.Value(neo_block.file_origin))
        if cascade:
            for segment in neo_block.segments:
                self.add_segment(segment, nix_block)
            for rcg in neo_block.recordingchannelgroups:
                self.add_recordingchannelgroup(rcg, nix_block)

    def write_all_blocks(self, neo_blocks, cascade=True):
        """
        Convert all ``neo_blocks`` to the NIX equivalent and write them to the
        file. If ``cascade`` is True, write all child objects as well.

        :param neo_blocks: list (or iterable) containing Neo blocks
        :param cascade: save all child objects (default: True)
        """
        for nb in neo_blocks:
            self.write_block(nb, cascade)

    def write_segment(self, segment, parent_block):
        """
        Write the provided ``segment`` Neo object to the NIX file.
        Neo ``segment`` objects are converted to ``Group`` objects in NIX.
        The ``parent_block`` must be a Neo Block object, which is used to
        find the equivalent NIX ``Block`` in the file where the NIX ``Group``
        will be added.

        :param segment: Neo Segment to be written
        :param parent_block: The parent neo block of the provided Segment
        :return: The newly created NIX Group
        """
        parent_name = parent_block.name
        if parent_name in self.nix_file.blocks:
            nix_block = self.nix_file.blocks[parent_name]
            return self.add_segment(segment, nix_block)
        else:
            raise LookupError(
                    "Parent Block with name '{}' for Segment with "
                    "name '{}' does not exist in file '{}'.".format(
                            parent_block.name, segment.name, self.filename))

    def add_segment(self, segment, parent_block):
        """
        Write the provided ``segment`` to the NIX file as a child of
        parent_block after converting to a ``Group`` object.

        :param segment: Neo segment to be written
        :param parent_block: The parent NIX Block
        :return: The newly created NIX Group
        """
        nix_name = segment.name
        nix_type = "neo.segment"
        nix_definition = segment.description
        nix_group = parent_block.create_group(nix_name, nix_type)
        nix_group.definition = nix_definition
        if segment.rec_datetime:
            # Truncating timestamp to seconds
            nix_group.force_created_at(calculate_timestamp(segment.rec_datetime))
        if segment.file_datetime:
            group_metadata = self._get_or_init_metadata(nix_group)
            # Truncating timestamp to seconds
            group_metadata .create_property(
                    "file_datetime",
                    nix.Value(calculate_timestamp(segment.file_datetime)))
        if segment.file_origin:
            group_metadata = self._get_or_init_metadata(nix_group)
            group_metadata.create_property("file_origin",
                                           nix.Value(segment.file_origin))
        return nix_group

    def write_recordingchannelgroup(self, rcg, parent_block):
        """
        Write the provided ``rcg`` (RecordingChannelGroup) Neo object to the
        NIX file. Neo ``RecordingChannelGroup`` objects are converted to
        ``Source`` objects in NIX. The ``parent_block`` must be a Neo Block
        object, which is used to find the equivalent NIX ``Block`` in the file
         where the NIX ``Source`` will be added.

        :param rcg: Neo RecordingChannelGroup to be written
        :param parent_block: The parent neo block of the provided
            RecordingChannelGroup
        :return: The newly created NIX Source
        """
        parent_name = parent_block.name
        if parent_name in self.nix_file.blocks:
            nix_block = self.nix_file.blocks[parent_name]
            return self.add_recordingchannelgroup(rcg, nix_block)
        else:
            raise LookupError(
                    "Parent Block with name '{}' for RecordingChannelGroup "
                    "with name '{}' does not exist in file '{}'.".format(
                            parent_block.name, rcg.name, self.filename))

    def add_recordingchannelgroup(self, rcg, parent_block):
        """
        Write the provided ``rcg`` (RecordingChannelGroup) to the NIX file as
        a child of ``parent_block`` after converting to a ``Source`` object.

        :param rcg: The Neo RecordingChannelGroup to be written
        :param parent_block: The parent NIX Block
        :return: The newly created NIX Source.
        """
        nix_name = rcg.name
        nix_type = "neo.recordingchannelgroup"
        nix_definition = rcg.description
        nix_source = parent_block.create_source(nix_name, nix_type)
        nix_source.definition = nix_definition
        if rcg.file_origin:
            source_metadata = self._get_or_init_metadata(nix_source)
            source_metadata.create_property("file_origin",
                                            nix.Value(rcg.file_origin))
        if hasattr(rcg, "coordinates"):
            source_metadata = self._get_or_init_metadata(nix_source)
            nix_coordinates = NixIO._copy_coordinates(rcg.coordinates)
            source_metadata.create_property("coordinates",
                                            nix_coordinates)
        return nix_source

    def add_analogsignal(self, anasig, parent_group, parent_block):
        """
        Write the provided ``anasig`` (AnalogSignal) to the NIX file as a child
        of ``parent_group`` after converting to a ``DataArray`` object.

        :param anasig: The Neo AnalogSignal to be written
        :param parent_group: The parent NIX Group
        :param parent_block: The parent NIX Block under which the DataArray is
            created
        :return: The newly created NIX DataArray.
        """
        nix_name = anasig.name
        nix_type = "neo.analogsignal"
        nix_definition = anasig.description
        nix_data_array = parent_block.create_data_array(nix_name, nix_type)
        parent_group.data_arrays.append(nix_data_array)
        nix_data_array.definition = nix_definition
        if anasig.file_origin:
            darray_metadata = self._get_or_init_metadata(nix_data_array)
            darray_metadata.create_property("file_origin",
                                            nix.Value(anasig.file_origin))

        # data
        data = NixIO._convert_signal_data(anasig)
        data.unit = str(anasig.units)
        nix_data_array.append(data)

        # dimensions
        time_units = str(anasig.sampling_period.units)
        offset = anasig.t_start.rescale(time_units).item()
        sampling_interval = anasig.sampling_period.item()

        timedim = nix_data_array.append_sampled_dimension(sampling_interval)
        timedim.unit = time_units
        timedim.label = "time"
        timedim.offset = offset
        chandim = nix_data_array.append_set_dimension()
        return nix_data_array

    def add_irregularlysampledsignal(self, irsig, parent_group, parent_block):
        """
        Write the provided ``irsig`` (IrregularlySampledSignal) to the NIX file
        as a child of ``parent_group`` after converting to a ``DataArray``
        object.

        :param irsig: The Neo IrregularlySampledSignal to be written
        :param parent_group: The parent NIX Group
        :param parent_block: The parent NIX Block under which the DataArray is
            created
        :return: The newly created NIX DataArray.
        """
        nix_name = irsig.name
        nix_type = "neo.irregularlysampledsignal"
        nix_definition = irsig.description
        nix_data_array = parent_block.create_data_array(nix_name, nix_type)
        parent_group.data_arrays.append(nix_data_array)
        nix_data_array.definition = nix_definition
        if irsig.file_origin:
            darray_metadata = self._get_or_init_metadata(nix_data_array)
            darray_metadata.create_property("file_origin",
                                            nix.Value(irsig.file_origin))

        # data
        data = NixIO._convert_signal_data(irsig)
        data.unit = str(irsig.units)
        nix_data_array.append(data)

        # dimensions
        times = irsig.times.magnitude.tolist()
        time_units = str(irsig.times.units)
        timedim = nix_data_array.append_range_dimension(times)
        timedim.unit = time_units
        timedim.label = "time"
        chandim = nix_data_array.append_set_dimension()
        return nix_data_array

    def add_epoch(self, ep, parent_group, parent_block):
        """
        Write the provided ``ep`` (Epoch) to the NIX file as a child of
        ``parent_group`` after converting to a ``MultiTag`` object.

        :param ep: The Neo Epoch to be written
        :param parent_group: The parent NIX Group
        :param parent_block: The parent NIX Block under which the DataArray is
            created
        :return: The newly created NIX MultiTag.
        """

    def add_event(self, ev, parent_group, parent_block):
        """
        Write the provided ``ev`` (Event) to the NIX file as a child of
        ``parent_group`` after converting to a ``MultiTag`` object.

        :param ev: The Neo Event to be written
        :param parent_group: The parent NIX Group
        :param parent_block: The parent NIX Block under which the DataArray is
            created
        :return: The newly created NIX MultiTag.
        """

    def add_spiketrain(self, sptr, parent_group, parent_block):
        """
        Write the provided ``sptr`` (SpikeTrain) to the NIX file as a child of
        ``parent_group`` after converting to a ``MultiTag`` object.

        :param sptr: The Neo SpikeTrain to be written
        :param parent_group: The parent NIX Group
        :param parent_block: The parent NIX Block under which the DataArray is
            created
        :return: The newly created NIX MultiTag.
        """

    def add_unit(self, ut, parent_block):
        """
        Write the provided ``ut`` (Unit) to the NIX file as a child of
        ``parent_block`` after converting to a ``Source`` object.

        :param ut: The Neo Unit to be written
        :param parent_block: The parent NIX Block under which the DataArray is
            created
        :return: The newly created NIX Source.
        """

    def _get_or_init_metadata(self, nix_obj):
        """
        Creates a metadata Section for the provided NIX object if it doesn't
        have one already. Returns the new or existing metadata section.

        :param nix_obj: The object to which the Section is attached.
        :return: The metadata section of the provided object.
        """
        # TODO: Metadata section tree should mirror neo object structure
        if nix_obj.metadata is None:
            nix_obj.metadata = self.nix_file.create_section(
                    nix_obj.name, nix_obj.type+".metadata")
        return nix_obj.metadata

    @staticmethod
    def _equals(neo_obj, nix_obj, cascade=True):
        """
        Returns ``True`` if the attributes of ``neo_obj`` match the attributes
        of the ``nix_obj``.

        :param neo_obj: a Neo object (block, segment, etc.)
        :param nix_obj: a NIX object to compare to (block, group, etc.)
        :param cascade: test all child objects for equivalence recursively
                        (default: True)
        :return: true if the attributes and child objects (if cascade=True)
         of the two objects, as defined in the object mapping, are equal.
        """
        if not NixIO._equals_attr(neo_obj, nix_obj):
            return False
        if cascade:
            return NixIO._equals_child_objects(neo_obj, nix_obj)
        else:
            return True

    @staticmethod
    def _equals_attr(neo_obj, nix_obj):
        for neo_attr_name, nix_attr_name in attribute_mappings.items():
            neo_attr = getattr(neo_obj, neo_attr_name, None)
            nix_attr = getattr(nix_obj, nix_attr_name, None)
            if neo_attr != nix_attr:
                return False

        if hasattr(neo_obj, "rec_datetime") and neo_obj.rec_datetime and\
                (neo_obj.rec_datetime !=
                 datetime.fromtimestamp(nix_obj.created_at)):
            return False

        if hasattr(neo_obj, "file_datetime") and neo_obj.file_datetime and\
                (neo_obj.file_datetime !=
                 datetime.fromtimestamp(nix_obj.metadata["file_datetime"])):
            return False

        if neo_obj.file_origin and\
                neo_obj.file_origin != nix_obj.metadata["file_origin"]:
            return False

        return True

    @staticmethod
    def _equals_child_objects(neo_obj, nix_obj):
        for neo_container_name, nix_container_name \
                in container_mappings.items():
            neo_container = getattr(neo_obj, neo_container_name, None)
            nix_container = getattr(nix_obj, nix_container_name, None)
            if not (neo_container or nix_container):
                # both are empty or undefined (None)
                continue
            if len(neo_container) != len(nix_container):
                return False
            for neo_child_obj, nix_child_obj in zip(neo_container,
                                                    nix_container):
                if not NixIO._equals(neo_child_obj, nix_child_obj):
                    return False
        else:
            return True

    @staticmethod
    def _copy_coordinates(neo_coords):
        nix_coords = nix.Value(0)
        return nix_coords

    @staticmethod
    def _convert_signal_data(signal):
        data = []
        for chan in signal:
            data.append(chan.magnitude)
        return np.array(data)
