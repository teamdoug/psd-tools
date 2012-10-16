# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals, division
import collections
import logging
import warnings

from psd_tools.utils import read_fmt, read_pascal_string, read_be_array
from psd_tools.exceptions import Error
from psd_tools.constants import Compression, Clipping, BlendMode

logger = logging.getLogger(__name__)

_LayerRecord = collections.namedtuple('_LayerRecord', [
    'top', 'left', 'bottom', 'right',
    'num_channels', 'channels',
    'blend_mode', 'opacity', 'cilpping', 'flags',
    'mask_data', 'blending_ranges', 'name',
    'tagged_blocks'
])

class LayerRecord(_LayerRecord):

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


Layers = collections.namedtuple('Layers', 'length, layer_count, layer_records, channel_image_data')
LayerAndMaskInfo = collections.namedtuple('LayerAndMaskData', 'layers global_mask_info tagged_blocks')
ChannelInfo = collections.namedtuple('ChannelInfo', 'id length')
MaskData = collections.namedtuple('MaskData', 'top left bottom right default_color flags real_flags real_background')
LayerBlendingRanges = collections.namedtuple('LayerBlendingRanges', 'composite_ranges channel_ranges')

class ChannelData(collections.namedtuple('ChannelData', 'compression data')):
    def __repr__(self):
        return "ChannelData(compression=%r, len(data)=%r" % (
            self.compression, len(self.data) if self.data is not None else None
        )

GlobalMaskInfo = collections.namedtuple('GlobalMaskInfo', 'overlay color_components opacity kind')

def read(fp, encoding):
    """
    Reads layers and masks information.
    """
    length = read_fmt("I", fp)[0]
    start_position = fp.tell()

    layers = _read_layers(fp, encoding)

    # XXX: are tagged blocks really after the layers?
    # XXX: does global mask reading really work?
    global_mask_info = _read_global_mask_info(fp)

    consumed_bytes = fp.tell() - start_position
    tagged_blocks = _read_layer_tagged_blocks(fp, length - consumed_bytes)

    consumed_bytes = fp.tell() - start_position
    fp.seek(length-consumed_bytes, 1)

    return LayerAndMaskInfo(layers, global_mask_info, tagged_blocks)

def _read_layers(fp, encoding):
    """
    Reads info about layers.
    """
    length = read_fmt("I", fp)[0]
    layer_count = read_fmt("h", fp)[0]

    layer_records = []
    for idx in range(abs(layer_count)):
        layer = _read_layer_record(fp, encoding)
        layer_records.append(layer)

    channel_image_data = []
    for layer in layer_records:

        data = _read_channel_image_data(fp, layer)
        channel_image_data.append(data)

    return Layers(length, layer_count, layer_records, channel_image_data)

def _read_layer_record(fp, encoding):
    """
    Reads single layer record.
    """
    top, left, bottom, right = read_fmt("4i", fp)
    num_channels = read_fmt("H", fp)[0]

    channel_info = []
    for channel_num in range(num_channels):
        info = ChannelInfo(*read_fmt("hI", fp))
        channel_info.append(info)

    sig = fp.read(4)
    if sig != b'8BIM':
        raise Error("Error parsing layer: invalid signature (%r)" % sig)

    blend_mode = fp.read(4).decode('ascii')
    if not BlendMode.is_known(blend_mode):
        warnings.warn("Unknown blend mode (%s)" % blend_mode)

    opacity, clipping, flags, extra_length = read_fmt("BBBxI", fp)

    if not Clipping.is_known(clipping):
        warnings.warn("Unknown clipping: %s" % clipping)

    start = fp.tell()
    mask_data = _read_layer_mask_data(fp)
    blending_ranges = _read_layer_blending_ranges(fp)
    name = read_pascal_string(fp, encoding, 1) # XXX: spec says padding should be 4?

    remaining_length = extra_length - (fp.tell()-start)
    tagged_blocks = _read_layer_tagged_blocks(fp, remaining_length)

    remaining_length = extra_length - (fp.tell()-start)
    fp.seek(remaining_length, 1) # skip the reminder

    return LayerRecord(
        top, left, bottom, right,
        num_channels, channel_info,
        blend_mode, opacity, clipping, flags,
        mask_data, blending_ranges, name,
        tagged_blocks
    )

def _read_layer_tagged_blocks(fp, remaining_length):
    """
    Reads a section of tagged blocks with additional layer information.
    """
    blocks = []
    start_pos = fp.tell()
    read_bytes = 0
    while read_bytes < remaining_length:
        block = _read_additional_layer_info_block(fp)
        if block is None:
            break
        blocks.append(block)
        read_bytes = fp.tell() - start_pos
    return blocks

def _read_additional_layer_info_block(fp):
    """
    Reads a tagged block with additional layer information.
    """
    sig = fp.read(4)
    if sig not in [b'8BIM', b'8B64']:
        fp.seek(-4, 1)
        return

    key = fp.read(4)
    length = read_fmt("I", fp)[0]
    data = fp.read(length)
    return key, data

def _read_layer_mask_data(fp):
    """ Reads layer mask or adjustment layer data. """
    size = read_fmt("I", fp)[0]
    if size not in [0, 20, 36]:
        warnings.warn("Invalid layer data size: %d" % size)
        fp.seek(size, 1)
        return

    if not size:
        return

    top, left, bottom, right, default_color, flags = read_fmt("4i 2B", fp)
    if size == 20:
        fp.seek(2, 1)
        real_flags, real_background = None, None
    else:
        real_flags, real_background = read_fmt("2B", fp)
        fp.seek(16, 1)

    return LayerMaskData(top, left, bottom, right, default_color, flags, real_flags, real_background)

def _read_layer_blending_ranges(fp):
    """ Reads layer blending data. """

    def read_channel_range():
        src_start, src_end, dest_start, dest_end = read_fmt("4H", fp)
        return (src_start, src_end), (dest_start, dest_end)

    composite_ranges = None
    channel_ranges = []
    length = read_fmt("I", fp)[0]

    if length:
        composite_ranges = read_channel_range()
        for x in range(length//8 - 1):
            channel_ranges.append(read_channel_range())

    return LayerBlendingRanges(composite_ranges, channel_ranges)

def _read_channel_image_data(fp, layer):
    """
    Reads image data for all channels in a layer.
    """
    w, h = layer.width(), layer.height()

    channel_data = []

    for channel in layer.channels:
        start_pos = fp.tell()
        compression = read_fmt("H", fp)[0]

        if compression == Compression.RAW:
            data = fp.read(w*h)
            channel_data.append(ChannelData(compression, data))

        elif compression == Compression.PACK_BITS:
            byte_counts = read_be_array("H", h, fp)
            data = fp.read(sum(byte_counts))
            channel_data.append(ChannelData(compression, data))

        elif Compression.is_known(compression):
            warnings.warn("This compression type (%d) is not yet supported." % compression)
        else:
            warnings.warn("Unknown compression type: %d" % compression)

        remaining_bytes = channel.length - (fp.tell() - start_pos) - 2
        if remaining_bytes > 0:
            fp.seek(remaining_bytes, 1)

    return channel_data


def _read_global_mask_info(fp):
    """
    Reads global layer mask info.
    """
    # XXX: Does it really work properly? What is it for?
    start_pos = fp.tell()
    length, overlay_color_space, c1, c2, c3, c4, opacity, kind = read_fmt("IH 4H HB", fp)
    filler_length = length - (fp.tell()-start_pos)
    if filler_length > 0:
        fp.seek(filler_length, 1)

    return GlobalMaskInfo(overlay_color_space, (c1, c2, c3, c4), opacity, kind)

def read_image_data(fp, header):
    """
    Reads merged image pixel data which is stored at the end of PSD file.
    """
    w, h = header.width, header.height
    compression = read_fmt("H", fp)[0]

    channel_byte_counts = []
    if compression == Compression.PACK_BITS:
        for ch in range(header.number_of_channels):
            channel_byte_counts.append(read_be_array("H", h, fp))

    channel_data = []
    for channel_id in range(header.number_of_channels):

        if compression == Compression.RAW:
            data = fp.read(w*h)
            channel_data.append(ChannelData(compression, data))

        elif compression == Compression.PACK_BITS:
            byte_counts = channel_byte_counts[channel_id]
            data = fp.read(sum(byte_counts))
            channel_data.append(ChannelData(compression, data))

        elif Compression.is_known(compression):
            warnings.warn("This compression type (%d) is not yet supported." % compression)
        else:
            warnings.warn("Unknown compression type: %d" % compression)

    return channel_data