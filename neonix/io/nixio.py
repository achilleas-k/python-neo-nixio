# Copyright (c) 2014, German Neuroinformatics Node (G-Node)
#                     Achilleas Koutsou <achilleas.k@gmail.com>
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted under the terms of the BSD License. See
# LICENSE file in the root of the Project.

from __future__ import absolute_import, print_function

import sys
import time
from datetime import datetime
from collections import Iterable
import itertools
from six import string_types
from hashlib import md5
import warnings

import quantities as pq
import numpy as np

from neo.io.baseio import BaseIO
from neo.core import (Block, Segment, RecordingChannelGroup, AnalogSignal,
                      IrregularlySampledSignal, Epoch, Event, SpikeTrain, Unit)
from neo.io.tools import LazyList

try:
    import nixio
except ImportError:  # pragma: no cover
    raise ImportError("Failed to import NIX. "
                      "The NixIO requires the Python bindings for NIX.")


def calculate_timestamp(dt):
    return int(time.mktime(dt.timetuple()))


class NixIO(BaseIO):
    """
    Class for reading and writing NIX files.
    """

    is_readable = False  # for now
    is_writable = True

    supported_objects = [Block, Segment, RecordingChannelGroup,
                         AnalogSignal, IrregularlySampledSignal,
                         Epoch, Event, SpikeTrain, Unit]
    readable_objects = [Block]
    writeable_objects = [Block]

    name = "NIX"
    extensions = ["h5"]
    mode = "file"

    _container_map = {
        "segments": "groups",
        "analogsignals": "data_arrays",
        "irregularlysampledsignals": "data_arrays",
        "events": "multi_tags",
        "epochs": "multi_tags",
        "spiketrains": "multi_tags",
        "recordingchannelgroups": "sources",
        "units": "sources"
    }

    _read_blocks = 0

    def __init__(self, filename, mode="ro"):
        """
        Initialise IO instance and NIX file.

        :param filename: Full path to the file
        """
        BaseIO.__init__(self, filename)
        self.filename = filename
        if mode == "ro":
            filemode = nixio.FileMode.ReadOnly
        elif mode == "rw":
            filemode = nixio.FileMode.ReadWrite
        elif mode == "ow":
            filemode = nixio.FileMode.Overwrite
        else:
            ValueError("Invalid mode specified '{}'. "
                       "Valid modes: 'ro' (ReadOnly)', 'rw' (ReadWrite), "
                       "'ow' (Overwrite).".format(mode))
        self.nix_file = nixio.File.open(self.filename, filemode)
        self._object_map = dict()
        self._lazy_loaded = list()
        self._object_hashes = dict()
        self._read_blocks = 0

    def __del__(self):
        self.nix_file.close()

    def read_all_blocks(self, cascade=True, lazy=False):
        blocks = list()
        for blk in self.nix_file.blocks:
            blocks.append(self.read_block("/" + blk.name, cascade, lazy))
        return blocks

    def read_block(self, path="/", cascade=True, lazy=False):
        if path == "/":
            try:
                nix_block = self.nix_file.blocks[self._read_blocks]
                path += nix_block.name
                self._read_blocks += 1
            except KeyError:
                return None
        else:
            nix_block = self._get_object_at(path)
        neo_block = self._block_to_neo(nix_block)
        neo_block.path = path
        if cascade:
            self._read_cascade(nix_block, path, cascade, lazy)
        self._update_maps(neo_block, lazy)
        return neo_block

    def read_segment(self, path, cascade=True, lazy=False):
        nix_group = self._get_object_at(path)
        neo_segment = self._group_to_neo(nix_group)
        neo_segment.path = path
        if cascade:
            self._read_cascade(nix_group, path, cascade, lazy)
        self._update_maps(neo_segment, lazy)
        nix_parent = self._get_parent(path)
        neo_parent = self._get_mapped_object(nix_parent)
        neo_segment.block = neo_parent
        return neo_segment

    def read_recordingchannelgroup(self, path, cascade=True, lazy=False):
        nix_source = self._get_object_at(path)
        neo_rcg = self._source_rcg_to_neo(nix_source)
        neo_rcg.path = path
        if cascade:
            self._read_cascade(nix_source, path, cascade, lazy)
        self._update_maps(neo_rcg, lazy)
        nix_parent = self._get_parent(path)
        neo_parent = self._get_mapped_object(nix_parent)
        neo_rcg.block = neo_parent
        return neo_rcg

    def read_signal(self, path, lazy=False):
        nix_data_arrays = list()
        parent_group = self._get_parent(path)
        parent_container = parent_group.data_arrays
        signal_group_name = path.split("/")[-1]
        for idx in itertools.count():
            signal_name = "{}.{}".format(signal_group_name, idx)
            if signal_name in parent_container:
                nix_data_arrays.append(parent_container[signal_name])
            else:
                break
        # check metadata segment
        group_section = nix_data_arrays[0].metadata
        for da in nix_data_arrays:
            assert da.metadata == group_section,\
                "DataArray {} is not a member of signal group {}".format(
                    da.name, group_section.name
                )
        neo_signal = self._signal_da_to_neo(nix_data_arrays, lazy)
        neo_signal.path = path
        if self._find_lazy_loaded(neo_signal) is None:
            self._update_maps(neo_signal, lazy)
            nix_parent = self._get_parent(path)
            neo_parent = self._get_mapped_object(nix_parent)
            neo_signal.segment = neo_parent
        return neo_signal

    def read_analogsignal(self, path, cascade=True, lazy=False):
        return self.read_signal(path, lazy)

    def read_irregularlysampledsignal(self, path, cascade=True, lazy=False):
        return self.read_signal(path, lazy)

    def read_eest(self, path, lazy=False):
        nix_mtag = self._get_object_at(path)
        neo_eest = self._mtag_eest_to_neo(nix_mtag, lazy)
        neo_eest.path = path
        self._update_maps(neo_eest, lazy)
        nix_parent = self._get_parent(path)
        neo_parent = self._get_mapped_object(nix_parent)
        neo_eest.segment = neo_parent
        return neo_eest

    def read_epoch(self, path, cascade=True, lazy=False):
        return self.read_eest(path, lazy)

    def read_event(self, path, cascade=True, lazy=False):
        return self.read_eest(path, lazy)

    def read_spiketrain(self, path, cascade=True, lazy=False):
        return self.read_eest(path, lazy)

    def read_unit(self, path, cascade=True, lazy=False):
        nix_source = self._get_object_at(path)
        neo_unit = self._source_unit_to_neo(nix_source)
        neo_unit.path = path
        if cascade:
            self._read_cascade(nix_source, path, cascade, lazy)
        self._update_maps(neo_unit, lazy)
        nix_parent = self._get_parent(path)
        neo_parent = self._get_mapped_object(nix_parent)
        neo_unit.recordingchannelgroup = neo_parent
        return neo_unit

    def _block_to_neo(self, nix_block):
        neo_attrs = self._nix_attr_to_neo(nix_block)
        neo_block = Block(**neo_attrs)
        self._object_map[nix_block.id] = neo_block
        return neo_block

    def _group_to_neo(self, nix_group):
        neo_attrs = self._nix_attr_to_neo(nix_group)
        neo_segment = Segment(**neo_attrs)
        self._object_map[nix_group.id] = neo_segment
        return neo_segment

    def _source_rcg_to_neo(self, nix_source):
        neo_attrs = self._nix_attr_to_neo(nix_source)
        rec_channels = list(self._nix_attr_to_neo(c)
                            for c in nix_source.sources
                            if c.type == "neo.recordingchannel")
        neo_attrs["channel_names"] = np.array([c["name"] for c in rec_channels],
                                              dtype="S")
        neo_attrs["channel_indexes"] = np.array([c["index"]
                                                 for c in rec_channels])
        if "coordinates" in rec_channels[0]:
            coord_units = rec_channels[0]["coordinates.units"]
            coord_values = list(c["coordinates"] for c in rec_channels)
            neo_attrs["coordinates"] = pq.Quantity(coord_values, coord_units)
        rcg = RecordingChannelGroup(**neo_attrs)
        self._object_map[nix_source.id] = rcg
        return rcg

    def _source_unit_to_neo(self, nix_unit):
        neo_attrs = self._nix_attr_to_neo(nix_unit)
        neo_unit = Unit(**neo_attrs)
        self._object_map[nix_unit.id] = neo_unit
        return neo_unit

    def _signal_da_to_neo(self, nix_da_group, lazy):
        """
        Convert a group of NIX DataArrays to a Neo signal. This method expects
        a list of data arrays that all represent the same, multidimensional
        Neo Signal object.
        This returns either an AnalogSignal or IrregularlySampledSignal.

        :param nix_da_group: a list of NIX DataArray objects
        :return: a Neo Signal object
        """
        nix_da_group = sorted(nix_da_group, key=lambda d: d.name)
        neo_attrs = self._nix_attr_to_neo(nix_da_group[0])
        neo_attrs["name"] = nix_da_group[0].metadata.name
        neo_type = nix_da_group[0].type

        unit = nix_da_group[0].unit
        if lazy:
            signaldata = pq.Quantity(np.empty(0), unit)
            lazy_shape = (len(nix_da_group[0]), len(nix_da_group))
        else:
            signaldata = pq.Quantity(np.transpose(nix_da_group), unit)
            lazy_shape = None
        timedim = self._get_time_dimension(nix_da_group[0])
        if neo_type == "neo.analogsignal"\
                or isinstance(timedim, nixio.SampledDimension):
            if lazy:
                sampling_period = pq.Quantity(1, timedim.unit)
                t_start = pq.Quantity(0, timedim.unit)
            else:
                sampling_period = pq.Quantity(timedim.sampling_interval,
                                              timedim.unit)
                sampling_period = sampling_period.rescale("ms")
                t_start = pq.Quantity(timedim.offset, timedim.unit)
            neo_signal = AnalogSignal(
                signal=signaldata, sampling_period=sampling_period,
                t_start=t_start, **neo_attrs
            )
        elif neo_type == "neo.irregularlysampledsignal"\
                or isinstance(timedim, nixio.RangeDimension):
            if lazy:
                times = pq.Quantity(np.empty(0), timedim.unit)
            else:
                times = pq.Quantity(timedim.ticks, timedim.unit)
            neo_signal = IrregularlySampledSignal(
                signal=signaldata, times=times, **neo_attrs
            )
        else:
            return None
        for da in nix_da_group:
            self._object_map[da.id] = neo_signal
        if lazy_shape:
            neo_signal.lazy_shape = lazy_shape
        return neo_signal

    def _mtag_eest_to_neo(self, nix_mtag, lazy):
        neo_attrs = self._nix_attr_to_neo(nix_mtag)
        neo_type = nix_mtag.type

        time_unit = nix_mtag.positions.unit
        if lazy:
            times = pq.Quantity(np.empty(0), time_unit)
            lazy_shape = np.shape(nix_mtag.positions)
        else:
            times = pq.Quantity(nix_mtag.positions, time_unit)
            lazy_shape = None
        if neo_type == "neo.epoch":
            if lazy:
                durations = pq.Quantity(np.empty(0), nix_mtag.extents.unit)
                labels = np.empty(0, dtype='S')
            else:
                durations = pq.Quantity(nix_mtag.extents, nix_mtag.extents.unit)
                labels = np.array(nix_mtag.positions.dimensions[0].labels,
                                  dtype="S")
            eest = Epoch(times=times, durations=durations, labels=labels,
                         **neo_attrs)
        elif neo_type == "neo.event":
            if lazy:
                labels = np.empty(0, dtype='S')
            else:
                labels = np.array(nix_mtag.positions.dimensions[0].labels,
                                  dtype="S")
            eest = Event(times=times, labels=labels, **neo_attrs)
        elif neo_type == "neo.spiketrain":
            eest = SpikeTrain(times=times, **neo_attrs)
            if len(nix_mtag.features):
                wfda = nix_mtag.features[0].data
                wftime = self._get_time_dimension(wfda)
                if lazy:
                    eest.waveforms = pq.Quantity(np.empty((0, 0, 0)), wfda.unit)
                    eest.sampling_period = pq.Quantity(1, wftime.unit)
                    eest.left_sweep = pq.Quantity(0, wftime.unit)
                else:
                    eest.waveforms = pq.Quantity(wfda, wfda.unit)
                    eest.sampling_period = pq.Quantity(
                        wftime.sampling_interval, wftime.unit
                    )
                    eest.left_sweep = pq.Quantity(wfda.metadata["left_sweep"],
                                                  wftime.unit)
        else:
            return None
        self._object_map[nix_mtag.id] = eest
        if lazy_shape:
            eest.lazy_shape = lazy_shape
        return eest

    def _read_cascade(self, nix_obj, path, cascade, lazy):
        neo_obj = self._object_map[nix_obj.id]
        for neocontainer in getattr(neo_obj, "_child_containers", []):
            nixcontainer = self._container_map[neocontainer]
            if not hasattr(nix_obj, nixcontainer):
                continue
            neotype = neocontainer[:-1]
            chpaths = list(path + "/" + neocontainer + "/" + c.name
                           for c in getattr(nix_obj, nixcontainer)
                           if c.type == "neo." + neotype)
            if neocontainer in ("analogsignals",
                                "irregularlysampledsignals"):
                chpaths = self._group_signals(chpaths)
            if cascade != "lazy":
                read_func = getattr(self, "read_" + neotype)
                children = list(read_func(cp, cascade, lazy)
                                for cp in chpaths)
            else:
                children = LazyList(self, lazy, chpaths)
            setattr(neo_obj, neocontainer, children)

        if isinstance(neo_obj, RecordingChannelGroup):
            # set references to signals
            parent_block_path = "/" + path.split("/")[1]
            parent_block = self._get_object_at(parent_block_path)
            ref_das = self._get_referers(nix_obj, parent_block.data_arrays)
            ref_signals = self._get_mapped_objects(ref_das)
            # deduplicate by name
            ref_signals = list(dict((s.name, s) for s in ref_signals).values())
            for sig in ref_signals:
                if isinstance(sig, AnalogSignal):
                    neo_obj.analogsignals.append(sig)
                elif isinstance(sig, IrregularlySampledSignal):
                    neo_obj.irregularlysampledsignals.append(sig)
                sig.recordingchannelgroup = neo_obj

        elif isinstance(neo_obj, Unit):
            # set references to spiketrains
            parent_block_path = "/" + path.split("/")[1]
            parent_block = self._get_object_at(parent_block_path)
            ref_mtags = self._get_referers(nix_obj, parent_block.multi_tags)
            ref_sts = self._get_mapped_objects(ref_mtags)
            neo_obj.spiketrains.extend(ref_sts)

    def get(self, path, cascade, lazy):
        parts = path.split("/")
        if len(parts) > 2:
            neotype = parts[-2][:-1]
        else:
            neotype = "block"
        read_func = getattr(self, "read_" + neotype)
        return read_func(path, cascade, lazy)

    def load_lazy_object(self, obj):
        return self.get(obj.path, cascade=False, lazy=False)

    def load_lazy_cascade(self, path, lazy):
        """
        Loads the object at the location specified by the path and all children.
        Data is loaded is lazy is False.

        :param path: Location of object in file
        :param lazy: Do not load data if True
        :return: The loaded object
        """
        neoobj = self.get(path, cascade=True, lazy=lazy)
        return neoobj

    def write_all_blocks(self, neo_blocks):
        """
        Convert all ``neo_blocks`` to the NIX equivalent and write them to the
        file.

        :param neo_blocks: List (or iterable) containing Neo blocks
        :return: A list containing the new NIX Blocks
        """
        self.resolve_name_conflicts(neo_blocks)
        for bl in neo_blocks:
            self.write_block(bl)

    def write_block(self, bl, parent_path=""):
        """
        Convert ``bl`` to the NIX equivalent and write it to the file.

        :param bl: Neo block to be written
        :param parent_path: Unused for blocks
        :return: The new NIX Block
        """
        if not bl.name:
            self.resolve_name_conflicts([bl])
        self.resolve_name_conflicts(bl.segments)
        self.resolve_name_conflicts(bl.recordingchannelgroups)

        allsignals = list()
        alleests = list()
        for s in bl.segments:
            allsignals.extend(s.analogsignals)
            allsignals.extend(s.irregularlysampledsignals)
            alleests.extend(s.events)
            alleests.extend(s.epochs)
            alleests.extend(s.spiketrains)
        self.resolve_name_conflicts(allsignals)
        self.resolve_name_conflicts(alleests)

        attr = self._neo_attr_to_nix(bl)
        obj_path = "/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(bl)
        if old_hash is None:
            nix_block = self.nix_file.create_block(attr["name"], attr["type"])
        else:
            nix_block = self._get_object_at(obj_path)
        if old_hash != new_hash:
            nix_block.definition = attr["definition"]
            self._write_attr_annotations(nix_block, attr, obj_path)
            self._object_hashes[obj_path] = new_hash
        self._object_map[id(bl)] = nix_block
        for segment in bl.segments:
            self.write_segment(segment, obj_path)
        for rcg in bl.recordingchannelgroups:
            self.write_recordingchannelgroup(rcg, obj_path)

    def write_segment(self, seg, parent_path=""):
        """
        Convert the provided ``seg`` to a NIX Group and write it to the NIX
        file at the location defined by ``parent_path``.

        :param seg: Neo seg to be written
        :param parent_path: Path to the parent of the new Segment
        :return: The newly created NIX Group
        """
        parent_block = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(seg)
        obj_path = parent_path + "/segments/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(seg)
        if old_hash is None:
            nix_group = parent_block.create_group(attr["name"], attr["type"])
        else:
            nix_group = self._get_object_at(obj_path)
        if old_hash != new_hash:
            nix_group.definition = attr["definition"]
            self._write_attr_annotations(nix_group, attr, obj_path)
            self._object_hashes[obj_path] = new_hash
        self._object_map[id(seg)] = nix_group
        for anasig in seg.analogsignals:
            self.write_analogsignal(anasig, obj_path)
        for irsig in seg.irregularlysampledsignals:
            self.write_irregularlysampledsignal(irsig, obj_path)
        for ep in seg.epochs:
            self.write_epoch(ep, obj_path)
        for ev in seg.events:
            self.write_event(ev, obj_path)
        for sptr in seg.spiketrains:
            self.write_spiketrain(sptr, obj_path)

    def write_recordingchannelgroup(self, rcg, parent_path=""):
        """
        Convert the provided ``rcg`` (RecordingChannelGroup) to a NIX Source
        and write it to the NIX file at the location defined by ``parent_path``.

        :param rcg: The Neo RecordingChannelGroup to be written
        :param parent_path: Path to the parent of the new RCG
        :return: The newly created NIX Source
        """
        self.resolve_name_conflicts(rcg.units)

        parent_block = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(rcg)
        obj_path = parent_path + "/recordingchannelgroups/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(rcg)
        if old_hash is None:
            nix_source = parent_block.create_source(attr["name"], attr["type"])

            # add signal references
            for nix_asigs in self._get_mapped_objects(rcg.analogsignals):
                # One AnalogSignal maps to list of DataArrays
                for da in nix_asigs:
                    da.sources.append(nix_source)
            for nix_isigs in self._get_mapped_objects(
                    rcg.irregularlysampledsignals
            ):
                # One IrregularlySampledSignal maps to list of DataArrays
                for da in nix_isigs:
                    da.sources.append(nix_source)
        else:
            nix_source = self._get_object_at(obj_path)
        if old_hash != new_hash:
            nix_source.definition = attr["definition"]
            self._write_attr_annotations(nix_source, attr, obj_path)
            for idx, channel in enumerate(rcg.channel_indexes):
                # create child source objects to represent each channel
                if len(rcg.channel_names):
                    nix_chan_name = rcg.channel_names[idx]
                else:
                    nix_chan_name = "{}.RecordingChannel{}".format(
                        nix_source.name, idx
                    )
                nix_chan_type = "neo.recordingchannel"
                if old_hash is None:
                    nix_chan = nix_source.create_source(nix_chan_name,
                                                        nix_chan_type)
                else:
                    nix_chan = nix_source.sources[nix_chan_name]
                nix_chan.definition = nix_source.definition
                chan_obj_path = obj_path + "/recordingchannels/" + nix_chan_name
                chan_metadata = self._get_or_init_metadata(nix_chan,
                                                           chan_obj_path)
                chan_metadata["index"] = self._to_value(int(channel))
                if "file_origin" in attr:
                    chan_metadata["file_origin"] =\
                        self._to_value(attr["file_origin"])

                if hasattr(rcg, "coordinates"):
                    chan_coords = rcg.coordinates[idx]
                    coord_unit = str(chan_coords[0].dimensionality)
                    nix_coord_unit = self._to_value(coord_unit)
                    nix_coord_values = tuple(
                        self._to_value(c.rescale(coord_unit).magnitude.item())
                        for c in chan_coords
                    )
                    if "coordinates" in chan_metadata:
                        del chan_metadata["coordinates"]
                    chan_metadata.create_property("coordinates",
                                                  nix_coord_values)
                    chan_metadata["coordinates.units"] = nix_coord_unit
            self._object_hashes[obj_path] = new_hash
        self._object_map[id(rcg)] = nix_source
        for unit in rcg.units:
            self.write_unit(unit, obj_path)

    def write_analogsignal(self, anasig, parent_path=""):
        """
        Convert the provided ``anasig`` (AnalogSignal) to a list of NIX
        DataArray objects and write them to the NIX file at the location defined
        by ``parent_path``. All DataArray objects created from the same
        AnalogSignal have their metadata section point to the same object.

        :param anasig: The Neo AnalogSignal to be written
        :param parent_path: Path to the parent of the new AnalogSignal
        :return: A list containing the newly created NIX DataArrays
        """

        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_group = self._get_object_at(parent_path)
        parent_metadata = self._get_or_init_metadata(parent_group, parent_path)
        attr = self._neo_attr_to_nix(anasig)
        obj_path = parent_path + "/analogsignals/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(anasig)
        if old_hash is None:
            anasig_group_segment = parent_metadata.create_section(
                attr["name"], attr["type"]+".metadata"
            )
            new = True
        else:
            anasig_group_segment = parent_metadata.sections[attr["name"]]
            new = False
        nix_data_arrays = list()
        if old_hash != new_hash:
            if "file_origin" in attr:
                anasig_group_segment["file_origin"] =\
                    self._to_value(attr["file_origin"])
            if anasig.annotations:
                self._add_annotations(anasig.annotations, anasig_group_segment)

            # common properties
            data_units = self._get_units(anasig)
            # often sampling period is in 1/Hz or 1/kHz - simplifying to s
            time_units = self._get_units(anasig.sampling_period, True)
            # rescale after simplification
            offset = anasig.t_start.rescale(time_units).item()
            sampling_interval = anasig.sampling_period.rescale(time_units).item()

            for idx, sig in enumerate(anasig.transpose()):
                daname = "{}.{}".format(attr["name"], idx)
                if new:
                    nix_data_array = parent_block.create_data_array(
                        daname,
                        attr["type"],
                        data=sig.magnitude
                    )
                    parent_group.data_arrays.append(nix_data_array)
                else:
                    nix_data_array = parent_block.data_arrays[daname]
                nix_data_array.definition = attr["definition"]
                nix_data_array.unit = data_units

                timedim = nix_data_array.append_sampled_dimension(
                    sampling_interval
                )
                timedim.unit = time_units
                timedim.label = "time"
                timedim.offset = offset
                chandim = nix_data_array.append_set_dimension()
                # point metadata to common section
                nix_data_array.metadata = anasig_group_segment
                nix_data_arrays.append(nix_data_array)
                self._object_hashes[obj_path] = new_hash
        else:
            for idx, sig in enumerate(anasig.transpose()):
                daname = "{}.{}".format(attr["name"], idx)
                nix_data_array = parent_block.data_arrays[daname]
                nix_data_arrays.append(nix_data_array)
        self._object_map[id(anasig)] = nix_data_arrays

    def write_irregularlysampledsignal(self, irsig, parent_path=""):
        """
        Convert the provided ``irsig`` (IrregularlySampledSignal) to a list of
        NIX DataArray objects and write them to the NIX file at the location
        defined by ``parent_path``. All DataArray objects created from the same
        IrregularlySampledSignal have their metadata section point to the same
        object.

        :param irsig: The Neo IrregularlySampledSignal to be written
        :param parent_path: Path to the parent of the new
        :return: The newly created NIX DataArray
        """
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_group = self._get_object_at(parent_path)
        parent_metadata = self._get_or_init_metadata(parent_group, parent_path)
        attr = self._neo_attr_to_nix(irsig)
        obj_path = parent_path + "/irregularlysampledsignals/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(irsig)
        if old_hash is None:
            irsig_group_segment = parent_metadata.create_section(
                attr["name"], attr["type"]+".metadata"
            )
            new = True
        else:
            irsig_group_segment = parent_metadata.sections[attr["name"]]
            new = False
        nix_data_arrays = list()
        if old_hash != new_hash:
            if "file_origin" in attr:
                irsig_group_segment["file_origin"] =\
                    self._to_value(attr["file_origin"])
            if irsig.annotations:
                self._add_annotations(irsig.annotations, irsig_group_segment)

            # common properties
            data_units = self._get_units(irsig)
            time_units = self._get_units(irsig.times)
            times = irsig.times.magnitude.tolist()

            for idx, sig in enumerate(irsig.transpose()):
                daname = "{}.{}".format(attr["name"], idx)
                if new:
                    nix_data_array = parent_block.create_data_array(
                        daname,
                        attr["type"],
                        data=sig.magnitude
                    )
                    parent_group.data_arrays.append(nix_data_array)
                else:
                    nix_data_array = parent_block.data_arrays[daname]
                nix_data_array.definition = attr["definition"]
                nix_data_array.unit = data_units

                timedim = nix_data_array.append_range_dimension(times)
                timedim.unit = time_units
                timedim.label = "time"
                chandim = nix_data_array.append_set_dimension()
                # point metadata to common section
                nix_data_array.metadata = irsig_group_segment
                nix_data_arrays.append(nix_data_array)
            self._object_hashes[obj_path] = new_hash
        else:
            for idx, sig in enumerate(irsig.transpose()):
                daname = "{}.{}".format(attr["name"], idx)
                nix_data_array = parent_block.data_arrays[daname]
                nix_data_arrays.append(nix_data_array)
        self._object_map[id(irsig)] = nix_data_arrays

    def write_epoch(self, ep, parent_path=""):
        """
        Convert the provided ``ep`` (Epoch) to a NIX MultiTag and write it to
        the NIX file at the location defined by ``parent_path``.

        :param ep: The Neo Epoch to be written
        :param parent_path: Path to the parent of the new MultiTag
        :return: The newly created NIX MultiTag
        """
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_group = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(ep)
        obj_path = parent_path + "/epochs/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(ep)

        if old_hash != new_hash:
            # times -> positions
            times_da_name = attr["name"] + ".times"
            times = ep.times.magnitude
            time_units = self._get_units(ep.times)

            # durations -> extents
            dura_da_name = attr["name"] + ".durations"
            durations = ep.durations.magnitude
            duration_units = self._get_units(ep.durations)

            if old_hash:
                del parent_block.data_arrays[times_da_name]
                del parent_block.data_arrays[dura_da_name]

            times_da = parent_block.create_data_array(
                times_da_name, attr["type"]+".times", data=times
            )
            times_da.unit = time_units
            durations_da = parent_block.create_data_array(
                attr["name"]+".durations",
                attr["type"]+".durations",
                data=durations
            )
            durations_da.unit = duration_units

            if old_hash is None:
                nix_multi_tag = parent_block.create_multi_tag(
                    attr["name"], attr["type"], times_da
                )
                parent_group.multi_tags.append(nix_multi_tag)
            else:
                nix_multi_tag = parent_block.multi_tags[attr["name"]]
                nix_multi_tag.positions = times_da

            label_dim = nix_multi_tag.positions.append_set_dimension()
            label_dim.labels = ep.labels
            nix_multi_tag.extents = durations_da
            nix_multi_tag.definition = attr["definition"]
            object_path = parent_path + "/epochs/" + nix_multi_tag.name
            self._write_attr_annotations(nix_multi_tag, attr, object_path)

            group_signals = self._get_contained_signals(parent_group)
            if old_hash is None:
                nix_multi_tag.references.extend(group_signals)
            else:
                nix_multi_tag.references.extend(
                    [sig for sig in group_signals
                     if sig not in nix_multi_tag.references]
                )

            self._object_hashes[obj_path] = new_hash
        else:
            nix_multi_tag = parent_block.multi_tags[attr["name"]]
        self._object_map[id(ep)] = nix_multi_tag

    def write_event(self, ev, parent_path=""):
        """
        Convert the provided ``ev`` (Event) to a NIX MultiTag and write it to
        the NIX file at the location defined by ``parent_path``.

        :param ev: The Neo Event to be written
        :param parent_path: Path to the parent of the new MultiTag
        :return: The newly created NIX MultiTag
        """
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_group = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(ev)
        obj_path = parent_path + "/events/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(ev)
        if old_hash != new_hash:
            # times -> positions
            times_da_name = attr["name"] + ".times"
            times = ev.times.magnitude
            time_units = self._get_units(ev.times)

            if old_hash:
                del parent_block.data_arrays[times_da_name]

            times_da = parent_block.create_data_array(
                times_da_name, attr["type"]+".times", data=times
            )
            times_da.unit = time_units

            if old_hash is None:
                nix_multi_tag = parent_block.create_multi_tag(
                    attr["name"], attr["type"], times_da
                )
                parent_group.multi_tags.append(nix_multi_tag)
            else:
                nix_multi_tag = parent_block.multi_tags[attr["name"]]
                nix_multi_tag.positions = times_da
            nix_multi_tag.definition = attr["definition"]

            label_dim = nix_multi_tag.positions.append_set_dimension()
            label_dim.labels = ev.labels

            self._write_attr_annotations(nix_multi_tag, attr, obj_path)

            group_signals = self._get_contained_signals(parent_group)
            if old_hash is None:
                nix_multi_tag.references.extend(group_signals)
            else:
                nix_multi_tag.references.extend(
                    [sig for sig in group_signals
                     if sig not in nix_multi_tag.references]
                )

            self._object_hashes[obj_path] = new_hash
        else:
            nix_multi_tag = parent_block.multi_tags[attr["name"]]
        self._object_map[id(ev)] = nix_multi_tag

    def write_spiketrain(self, sptr, parent_path=""):
        """
        Convert the provided ``sptr`` (SpikeTrain) to a NIX MultiTag and write
        it to the NIX file at the location defined by ``parent_path``.

        :param sptr: The Neo SpikeTrain to be written
        :param parent_path: Path to the parent of the new MultiTag
        :return: The newly created NIX MultiTag
        """
        block_path = "/" + parent_path.split("/")[1]
        parent_block = self._get_object_at(block_path)
        parent_group = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(sptr)
        obj_path = parent_path + "/spiketrains/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(sptr)

        if old_hash != new_hash:
            # spike times
            times_da_name = attr["name"] + ".times"
            times = sptr.times.magnitude
            time_units = self._get_units(sptr.times)

            if old_hash:
                del parent_block.data_arrays[times_da_name]

            times_da = parent_block.create_data_array(
                times_da_name, attr["type"] + ".times", data=times
            )
            times_da.unit = time_units

            if old_hash is None:
                nix_multi_tag = parent_block.create_multi_tag(
                    attr["name"], attr["type"], times_da
                )
                parent_group.multi_tags.append(nix_multi_tag)
            else:
                nix_multi_tag = parent_block.multi_tags[attr["name"]]
                nix_multi_tag.positions = times_da
            nix_multi_tag.definition = attr["definition"]

            self._write_attr_annotations(nix_multi_tag, attr, obj_path)

            mtag_metadata = self._get_or_init_metadata(nix_multi_tag,
                                                       obj_path)
            if sptr.t_start:
                t_start = sptr.t_start.rescale(time_units).magnitude.item()
                mtag_metadata["t_start"] = self._to_value(t_start)
            # t_stop is not optional
            t_stop = sptr.t_stop.rescale(time_units).magnitude.item()
            mtag_metadata["t_stop"] = self._to_value(t_stop)

            # waveforms
            if sptr.waveforms is not None:
                wf_data = list(wf.magnitude for wf in
                               list(wfgroup for wfgroup in sptr.waveforms))
                wf_name = attr["name"] + ".waveforms"
                if old_hash:
                    del parent_block.data_arrays[wf_name]
                    del nix_multi_tag.features[0]

                waveforms_da = parent_block.create_data_array(wf_name,
                                                              "neo.waveforms",
                                                              data=wf_data)
                wf_unit = self._get_units(sptr.waveforms)
                waveforms_da.unit = wf_unit
                nix_multi_tag.create_feature(waveforms_da,
                                             nixio.LinkType.Indexed)
                time_units = self._get_units(sptr.sampling_period, True)
                sampling_interval =\
                    sptr.sampling_period.rescale(time_units).item()
                wf_spikedim = waveforms_da.append_set_dimension()
                wf_chandim = waveforms_da.append_set_dimension()
                wf_timedim = waveforms_da.append_sampled_dimension(
                    sampling_interval
                )
                wf_timedim.unit = time_units
                wf_timedim.label = "time"
                wf_path = obj_path + "/waveforms/" + waveforms_da.name
                if old_hash:
                    waveforms_da.metadata = mtag_metadata.sections[wf_name]
                else:
                    waveforms_da.metadata = self._get_or_init_metadata(
                        waveforms_da, wf_path
                    )
                if sptr.left_sweep:
                    left_sweep = sptr.left_sweep.rescale(time_units).\
                        magnitude.item()
                    waveforms_da.metadata["left_sweep"] =\
                        self._to_value(left_sweep)

            self._object_hashes[obj_path] = new_hash
        else:
            nix_multi_tag = parent_block.multi_tags[attr["name"]]
        self._object_map[id(sptr)] = nix_multi_tag

    def write_unit(self, ut, parent_path=""):
        """
        Convert the provided ``ut`` (Unit) to a NIX Source and write it to the
        NIX file at the parent RCG.

        :param ut: The Neo Unit to be written
        :param parent_path: Path to the parent of the new Source
        :return: The newly created NIX Source
        """
        parent_source = self._get_object_at(parent_path)
        attr = self._neo_attr_to_nix(ut)
        obj_path = parent_path + "/units/" + attr["name"]
        old_hash = self._object_hashes.get(obj_path)
        new_hash = self._hash_object(ut)
        if old_hash is None:
            nix_source = parent_source.create_source(attr["name"], attr["type"])
            for nix_st in self._get_mapped_objects(ut.spiketrains):
                nix_st.sources.append(parent_source)
                nix_st.sources.append(nix_source)
        else:
            nix_source = parent_source.sources[attr["name"]]
        if old_hash != new_hash:
            nix_source.definition = attr["definition"]
            self._write_attr_annotations(nix_source, attr, obj_path)
            # Make contained spike trains refer to parent rcg and new unit
            self._object_hashes[obj_path] = new_hash

        self._object_map[id(ut)] = nix_source

    def _get_or_init_metadata(self, nix_obj, path):
        """
        Creates a metadata Section for the provided NIX object if it doesn't
        have one already. Returns the new or existing metadata section.

        :param nix_obj: The object to which the Section is attached
        :param path: Path to nix_obj
        :return: The metadata section of the provided object
        """
        parent_parts = path.split("/")[:-2]
        parent_path = "/".join(parent_parts)
        if nix_obj.metadata is None:
            if len(parent_parts) == 0:  # nix_obj is root block
                parent_metadata = self.nix_file
            else:
                obj_parent = self._get_object_at(parent_path)
                parent_metadata = self._get_or_init_metadata(obj_parent,
                                                             parent_path)
            nix_obj.metadata = parent_metadata.create_section(
                    nix_obj.name, nix_obj.type+".metadata"
            )
        return nix_obj.metadata

    def _get_object_at(self, path):
        """
        Returns the object at the location defined by the path.
        ``path`` is a '/' delimited string. Each part of the string alternates
        between an object name and a container.

        Example path: /block_1/segments/segment_a/events/event_a1

        :param path: Path string
        :return: The object at the location defined by the path
        """
        if path == "":
            return self.nix_file
        parts = path.split("/")
        if parts[0]:
            ValueError("Invalid object path: {}".format(path))
        if len(parts) == 2:  # root block
            return self.nix_file.blocks[parts[1]]
        parent_obj = self._get_parent(path)
        parent_container = getattr(parent_obj, self._container_map[parts[-2]])
        return parent_container[parts[-1]]

    def _get_parent(self, path):
        parts = path.split("/")
        parent_path = "/".join(parts[:-2])
        parent_obj = self._get_object_at(parent_path)
        return parent_obj

    def _get_mapped_objects(self, object_list):
        return list(map(self._get_mapped_object, object_list))

    def _get_mapped_object(self, obj):
        # We could use paths here instead
        try:
            if hasattr(obj, "id"):
                return self._object_map[obj.id]
            else:
                return self._object_map[id(obj)]
        except KeyError:
            raise KeyError("Failed to find mapped object for {}. "
                           "Object not yet converted.".format(obj))

    def _write_attr_annotations(self, nix_object, attr, object_path):
        if "created_at" in attr:
            nix_object.force_created_at(calculate_timestamp(attr["created_at"]))
        if "file_datetime" in attr:
            metadata = self._get_or_init_metadata(nix_object, object_path)
            metadata["file_datetime"] = self._to_value(attr["file_datetime"])
        if "file_origin" in attr:
            metadata = self._get_or_init_metadata(nix_object, object_path)
            metadata["file_origin"] = self._to_value(attr["file_origin"])
        if "rec_datetime" in attr and attr["rec_datetime"]:
            metadata["rec_datetime"] = self._to_value(attr["rec_datetime"])
        if "annotations" in attr:
            metadata = self._get_or_init_metadata(nix_object, object_path)
            self._add_annotations(attr["annotations"], metadata)

    def _update_maps(self, obj, lazy):
        objidx = self._find_lazy_loaded(obj)
        if lazy and objidx is None:
            self._lazy_loaded.append(obj)
        elif not lazy and objidx is not None:
            self._lazy_loaded.pop(objidx)
        if not lazy:
            self._object_hashes[obj.path] = self._hash_object(obj)

    def _find_lazy_loaded(self, obj):
        """
        Finds the index of an object in the _lazy_loaded list by comparing the
        path attribute. Returns None if the object is not in the list.

        :param obj: The object to find
        :return: The index of the object in the _lazy_loaded list or None if it
        was not added
        """
        for idx, llobj in enumerate(self._lazy_loaded):
            if llobj.path == obj.path:
                return idx
        else:
            return None

    @staticmethod
    def resolve_name_conflicts(objects):
        """
        Given a list of neo objects, change their names such that no two objects
        share the same name. Objects with no name are renamed based on their
        type.

        :param objects: List of Neo objects
        """
        if not len(objects):
            return
        names = [obj.name for obj in objects]
        for idx, cn in enumerate(names):
            if not cn:
                neo_type = type(objects[idx]).__name__
                cn = "neo.{}".format(neo_type)
            else:
                names[idx] = ""
            if cn not in names:
                newname = cn
            else:
                suffix = 1
                newname = "{}-{}".format(cn, suffix)
                while newname in names:
                    suffix += 1
                    newname = "{}-{}".format(cn, suffix)
            names[idx] = newname
        for obj, n in zip(objects, names):
            obj.name = n

    @staticmethod
    def _neo_attr_to_nix(neo_obj):
        neo_type = type(neo_obj).__name__
        nix_attrs = dict()
        nix_attrs["name"] = neo_obj.name
        nix_attrs["type"] = "neo.{}".format(neo_type.lower())
        nix_attrs["definition"] = neo_obj.description
        if isinstance(neo_obj, (Block, Segment)):
            nix_attrs["rec_datetime"] = neo_obj.rec_datetime
            if neo_obj.rec_datetime:
                nix_attrs["created_at"] = neo_obj.rec_datetime
            if neo_obj.file_datetime:
                nix_attrs["file_datetime"] = neo_obj.file_datetime
        if neo_obj.file_origin:
            nix_attrs["file_origin"] = neo_obj.file_origin
        if neo_obj.annotations:
            nix_attrs["annotations"] = neo_obj.annotations
        return nix_attrs

    @classmethod
    def _add_annotations(cls, annotations, metadata):
        for k, v in annotations.items():
            v = cls._to_value(v)
            metadata[k] = v

    @staticmethod
    def _to_value(v):
        """
        Helper function for converting arbitrary variables to types compatible
        with nixio.Value().

        :param v: The value to be converted
        :return: a nixio.Value() object
        """
        if isinstance(v, pq.Quantity):
            # v = nixio.Value((v.magnitude.item(), str(v.dimensionality)))
            warnings.warn("Quantities in annotations are not currently "
                          "supported when writing to NIX.")
            return None
        elif isinstance(v, datetime):
            v = nixio.Value(calculate_timestamp(v))
        elif isinstance(v, string_types):
            v = nixio.Value(v)
        elif isinstance(v, bytes):
            v = nixio.Value(v.decode())
        elif isinstance(v, Iterable):
            vv = list()
            for item in v:
                if isinstance(v, Iterable):
                    warnings.warn("Multidimensional arrays and nested "
                                  "containers are not currently supported "
                                  "when writing to NIX.")
                    return None
                if type(item).__module__ == "numpy":
                    item = nixio.Value(item.item())
                else:
                    item = nixio.Value(item)
                vv.append(item)
            if not len(vv):
                vv = None
            v = vv
        elif type(v).__module__ == "numpy":
            v = nixio.Value(v.item())
        else:
            v = nixio.Value(v)
        return v

    @staticmethod
    def _get_contained_signals(obj):
        return list(
             da for da in obj.data_arrays
             if da.type in ["neo.analogsignal", "neo.irregularlysampledsignal"]
        )

    @staticmethod
    def _get_units(quantity, simplify=False):
        """
        Returns the units of a quantity value or array as a string, or None if
        it is dimensionless.

        :param quantity: Quantity scalar or array
        :param simplify: True/False Simplify units
        :return: Units of the quantity or None if dimensionless
        """
        units = quantity.units.dimensionality
        if simplify:
            units = units.simplified
        units = str(units)
        if units == "dimensionless":
            units = None
        return units

    @staticmethod
    def _nix_attr_to_neo(nix_obj):
        neo_attrs = dict()
        neo_attrs["name"] = nix_obj.name

        neo_attrs["description"] = nix_obj.definition
        if nix_obj.metadata:
            for prop in nix_obj.metadata.props:
                values = prop.values
                if len(values) == 1:
                    neo_attrs[prop.name] = values[0].value
                else:
                    neo_attrs[prop.name] = list(v.value for v in values)

        if isinstance(nix_obj, (nixio.Block, nixio.Group)):
            if "rec_datetime" not in neo_attrs:
                neo_attrs["rec_datetime"] = None

        #     neo_attrs["rec_datetime"] = datetime.fromtimestamp(
        #         nix_obj.created_at)
        if "file_datetime" in neo_attrs:
            neo_attrs["file_datetime"] = datetime.fromtimestamp(
                neo_attrs["file_datetime"]
            )
        return neo_attrs

    @staticmethod
    def _group_signals(paths):
        """
        Groups data arrays that were generated by the same Neo Signal object.

        :param paths: A list of paths (strings) of all the signals to be grouped
        :return: A list of paths (strings) of signal groups. The last part of
        each path is the common name of the signals in the group.
        """
        grouppaths = list(".".join(p.split(".")[:-1])
                          for p in paths)
        return list(set(grouppaths))

    @staticmethod
    def _get_referers(nix_obj, obj_list):
        ref_list = list()
        for ref in obj_list:
            if nix_obj.name in list(src.name for src in ref.sources):
                ref_list.append(ref)
        return ref_list

    @staticmethod
    def _get_time_dimension(obj):
        for dim in obj.dimensions:
            if hasattr(dim, "label") and dim.label == "time":
                return dim
        return None

    @staticmethod
    def _hash_object(obj):
        """
        Computes an MD5 hash of a Neo object based on its attribute values and
        data objects. Child objects are not counted.

        :param obj: A Neo object
        :return: MD5 sum
        """
        objhash = md5()

        def strupdate(a):
            objhash.update(str(a).encode())

        def dupdate(d):
            if isinstance(d, np.ndarray) and not d.flags["C_CONTIGUOUS"]:
                d = d.copy(order="C")
            objhash.update(d)

        # attributes
        strupdate(obj.name)
        strupdate(obj.description)
        strupdate(obj.file_origin)

        # annotations
        for k, v in sorted(obj.annotations.items()):
            strupdate(k)
            strupdate(v)

        # data objects and type-specific attributes
        if isinstance(obj, (Block, Segment)):
            strupdate(obj.rec_datetime)
            strupdate(obj.file_datetime)
        elif isinstance(obj, RecordingChannelGroup):
            for idx in obj.channel_indexes:
                strupdate(idx)
            for n in obj.channel_names:
                strupdate(n)
            if hasattr(obj, "coordinates"):
                for coord in obj.coordinates:
                    for c in coord:
                        strupdate(c)
        elif isinstance(obj, AnalogSignal):
            dupdate(obj)
            dupdate(obj.units)
            dupdate(obj.t_start)
            dupdate(obj.sampling_rate)
            dupdate(obj.t_stop)
        elif isinstance(obj, IrregularlySampledSignal):
            dupdate(obj)
            dupdate(obj.times)
            dupdate(obj.units)
        elif isinstance(obj, Event):
            dupdate(obj.times)
            for l in obj.labels:
                strupdate(l)
        elif isinstance(obj, Epoch):
            dupdate(obj.times)
            dupdate(obj.durations)
            for l in obj.labels:
                strupdate(l)
        elif isinstance(obj, SpikeTrain):
            dupdate(obj.times)
            dupdate(obj.units)
            dupdate(obj.t_stop)
            dupdate(obj.t_start)
            if obj.waveforms is not None:
                dupdate(obj.waveforms)
            dupdate(obj.sampling_rate)
            if obj.left_sweep:
                strupdate(obj.left_sweep)

        # type
        strupdate(type(obj).__name__)

        return objhash.hexdigest()
