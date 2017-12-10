#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Hierarchical RNN encoders."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from models.pytorch.encoders.rnn_utils import _init_hidden
from models.pytorch.encoders.cnn import CNNEncoder
from utils.io.variable import var2np


class HierarchicalRNNEncoder(nn.Module):
    """Hierarchical RNN encoder.
    Args:
        input_size (int): the dimension of input features
        rnn_type (string): lstm or gru or rnn
        bidirectional (bool): if True, use the bidirectional encoder
        num_units (int): the number of units in each layer
        num_proj (int): the number of nodes in the projection layer
        num_layers (int): the number of layers in the main task
        num_layers_sub (int): the number of layers in the sub task
        dropout (float): the probability to drop nodes
        parameter_init (float): the range of uniform distribution to
            initialize weight parameters (>= 0)
        use_cuda (bool, optional): if True, use GPUs
        batch_first (bool, optional): if True, batch-major computation will be
            performed
        merge_bidirectional (bool, optional): if True, sum bidirectional outputs
        num_stack (int, optional): the number of frames to stack
        splice (int, optional): frames to splice. Default is 1 frame.
        conv_channels (list, optional):
        conv_kernel_sizes (list, optional):
        conv_strides (list, optional):
        poolings (list, optional):
        batch_norm (bool, optional):
    """

    def __init__(self,
                 input_size,
                 rnn_type,
                 bidirectional,
                 num_units,
                 num_proj,
                 num_layers,
                 num_layers_sub,
                 dropout,
                 parameter_init,
                 use_cuda=False,
                 batch_first=False,
                 merge_bidirectional=False,
                 num_stack=1,
                 splice=1,
                 conv_channels=[],
                 conv_kernel_sizes=[],
                 conv_strides=[],
                 poolings=[],
                 batch_norm=False):

        super(HierarchicalRNNEncoder, self).__init__()

        if num_layers_sub < 1 or num_layers < num_layers_sub:
            raise ValueError(
                'Set num_layers_sub between 1 to num_layers.')

        self.rnn_type = rnn_type
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1
        self.num_units = num_units
        self.num_proj = num_proj
        self.num_layers = num_layers
        self.num_layers_sub = num_layers_sub
        self.use_cuda = use_cuda
        self.batch_first = batch_first
        self.merge_bidirectional = merge_bidirectional

        # Setting for CNNs before RNNs
        if len(conv_channels) > 0 and len(conv_channels) == len(conv_kernel_sizes) and len(conv_kernel_sizes) == len(conv_strides):
            self.conv = CNNEncoder(
                input_size,
                num_stack=num_stack,
                splice=splice,
                conv_channels=conv_channels,
                conv_kernel_sizes=conv_kernel_sizes,
                conv_strides=conv_strides,
                poolings=poolings,
                dropout=dropout,
                parameter_init=parameter_init,
                use_cuda=use_cuda,
                batch_norm=batch_norm)
            input_size = self.conv.output_size
        else:
            input_size = input_size * splice * num_stack
            self.conv = None

        # NOTE: dropout is applied except the last layer
        self.rnns = []
        for i_layer in range(num_layers):
            if rnn_type == 'lstm':
                rnn = nn.LSTM(
                    input_size if i_layer == 0 else num_units * self.num_directions,
                    hidden_size=num_units,
                    num_layers=1,
                    bias=True,
                    batch_first=batch_first,
                    dropout=dropout,
                    bidirectional=bidirectional)
            elif rnn_type == 'gru':
                rnn = nn.GRU(
                    input_size if i_layer == 0 else num_units * self.num_directions,
                    hidden_size=num_units,
                    num_layers=1,
                    bias=True,
                    batch_first=batch_first,
                    dropout=dropout,
                    bidirectional=bidirectional)
            elif rnn_type == 'rnn':
                rnn = nn.RNN(
                    input_size if i_layer == 0 else num_units * self.num_directions,
                    hidden_size=num_units,
                    num_layers=1,
                    bias=True,
                    batch_first=batch_first,
                    dropout=dropout,
                    bidirectional=bidirectional)
            else:
                raise ValueError('rnn_type must be "lstm" or "gru" or "rnn".')

            setattr(self, rnn_type + '_l' + str(i_layer), rnn)

            if use_cuda:
                rnn = rnn.cuda()

            self.rnns.append(rnn)

    def forward(self, inputs, inputs_seq_len, volatile=True,
                mask_sequence=True):
        """Forward computation.
        Args:
            inputs: A tensor of size `[B, T, input_size]`
            inputs_seq_len (IntTensor or LongTensor): A tensor of size `[B]`
            volatile (bool, optional): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
            mask_sequence (bool, optional): if True, mask by sequence
                lenghts of inputs
        Returns:
            outputs:
                if batch_first is True, a tensor of size
                    `[B, T, num_units (* num_directions)]`
                else
                    `[T, B, num_units (* num_directions)]`
            final_state_fw: A tensor of size `[1, B, num_units]`
            outputs_sub:
                if batch_first is True, a tensor of size
                    `[B, T, num_units (* num_directions)]`
                else
                    `[T, B, num_units (* num_directions)]`
            final_state_fw_sub: A tensor of size `[1, B, num_units]`
            perm_indices ():
        """
        batch_size, max_time, input_size = inputs.size()

        # Initialize hidden states (and memory cells) per mini-batch
        h_0 = _init_hidden(batch_size=batch_size,
                           rnn_type=self.rnn_type,
                           num_units=self.num_units,
                           num_directions=self.num_directions,
                           num_layers=1,
                           use_cuda=self.use_cuda,
                           volatile=volatile)

        if mask_sequence:
            # Sort inputs by lengths in descending order
            inputs_seq_len, perm_indices = inputs_seq_len.sort(
                dim=0, descending=True)
            inputs = inputs[perm_indices]
        else:
            perm_indices = None

        # Path through CNN layers before RNN layers
        if self.conv is not None:
            inputs = self.conv(inputs)

        if not self.batch_first:
            # Reshape to the time-major
            inputs = inputs.transpose(0, 1).contiguous()

        if not isinstance(inputs_seq_len, list):
            inputs_seq_len = var2np(inputs_seq_len).tolist()

        if mask_sequence:
            # Pack encoder inputs
            inputs = pack_padded_sequence(
                inputs, inputs_seq_len, batch_first=self.batch_first)

        outputs = inputs
        for i_layer in range(self.num_layers):
            if self.rnn_type == 'lstm':
                outputs, (h_n, c_n) = self.rnns[i_layer](outputs, hx=h_0)
            else:
                outputs, h_n = self.rnns[i_layer](outputs, hx=h_0)

            if i_layer == self.num_layers_sub - 1:
                outputs_sub = outputs
                h_n_sub = h_n

        if mask_sequence:
            # Unpack encoder outputs
            outputs, unpacked_seq_len = pad_packed_sequence(
                outputs, batch_first=self.batch_first)
            outputs_sub, unpacked_seq_len_sub = pad_packed_sequence(
                outputs_sub, batch_first=self.batch_first)
            # TODO: update version for padding_value=0.0

            assert inputs_seq_len == unpacked_seq_len
            assert inputs_seq_len == unpacked_seq_len_sub

        # Sum bidirectional outputs
        if self.bidirectional and self.merge_bidirectional:
            outputs = outputs[:, :, :self.num_units] + \
                outputs[:, :, self.num_units:]
            outputs_sub = outputs_sub[:, :, :self.num_units] + \
                outputs_sub[:, :, self.num_units:]

        # Pick up the final state of the top layer (forward)
        if self.num_directions == 2:
            final_state_fw = h_n[-2:-1, :, :]
            final_state_fw_sub = h_n_sub[-2:-1, :, :]
        else:
            final_state_fw = h_n[-1, :, :].unsqueeze(dim=0)
            final_state_fw_sub = h_n_sub[-1, :, :].unsqueeze(dim=0)
        # NOTE: h_n: `[num_layers * num_directions, B, num_units]`
        #   h_n_sub: `[num_layers_sub * num_directions, B, num_units]`

        return outputs, final_state_fw, outputs_sub, final_state_fw_sub, perm_indices
