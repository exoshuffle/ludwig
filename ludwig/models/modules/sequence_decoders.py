# coding=utf-8
# Copyright (c) 2019 Uber Technologies, Inc.
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
# ==============================================================================
import logging
import numpy as np

import tensorflow.compat.v1 as tf
from tensorflow.keras.layers import Layer, Dense, Embedding
from tensorflow.keras.layers import GRUCell, SimpleRNNCell, LSTMCell

import tensorflow_addons as tfa
from tensorflow_addons.seq2seq import BasicDecoder
from tensorflow_addons.seq2seq.sampler import TrainingSampler
from tensorflow_addons.seq2seq import AttentionWrapper
from tensorflow_addons.seq2seq import LuongAttention
from tensorflow_addons.seq2seq import BahdanauAttention

from ludwig.utils.misc import get_from_registry
from ludwig.utils.tf_utils import sequence_length_3D, sequence_length_2D


# todo tf2 clean up
# from ludwig.models.modules.attention_modules import \
#     feed_forward_memory_attention
# from ludwig.models.modules.initializer_modules import get_initializer
# from ludwig.models.modules.recurrent_modules import recurrent_decoder

logger = logging.getLogger(__name__)

rnn_layers_registry = {
    'rnn': SimpleRNNCell,
    'gru': GRUCell,
    'lstm': LSTMCell
}


class SequenceGeneratorDecoder(Layer):
    def __init__(
            self,
            num_classes,
            cell_type='rnn',
            state_size=256,
            embedding_size=64,
            beam_width=1,
            num_layers=1,
            attention=None,
            tied_embeddings=None,
            initializer=None,
            regularize=True,
            is_timeseries=False,
            max_sequence_length=0,
            use_bias=True,
            weights_initializer='glorot_uniform',
            bias_initializer='zeros',
            weights_regularizer=None,
            bias_regularizer=None,
            activity_regularizer=None,
            **kwargs
    ):
        super(SequenceGeneratorDecoder, self).__init__()

        self.cell_type = cell_type
        self.state_size = state_size
        self.embedding_size = embedding_size
        self.beam_width = beam_width
        self.num_layers = num_layers
        self.attention = attention
        self.attention_mechanism = None
        self.tied_embeddings = tied_embeddings
        self.initializer = initializer
        self.regularize = regularize
        self.is_timeseries = is_timeseries
        self.num_classes = num_classes
        self.max_sequence_length = max_sequence_length
        self.state_size = state_size
        self.attention_mechanism = None

        if is_timeseries:
            self.vocab_size = 1
        else:
            self.vocab_size = self.num_classes

        self.decoder_embedding = Embedding(
            input_dim=self.num_classes + 1, # account for GO_SYMBOL
            output_dim=embedding_size)
        self.dense_layer = Dense(num_classes)
        self.decoder_rnncell = \
            get_from_registry(cell_type, rnn_layers_registry)(state_size)

        # Sampler
        self.sampler = tfa.seq2seq.sampler.TrainingSampler()

        print('setting up attention for', attention)
        if attention is not None:
            if attention == 'luong':
                self.attention_mechanism = LuongAttention(units=state_size)
            elif attention == 'bahdanau':
                self.attention_mechanism = BahdanauAttention(units=state_size)

            self.decoder_rnncell = AttentionWrapper(self.decoder_rnncell,
                                                self.attention_mechanism,
                                                attention_layer_size=state_size)

        self.decoder = tfa.seq2seq.BasicDecoder(self.decoder_rnncell,
                                                sampler=self.sampler,
                                                output_layer=self.dense_layer)

    def build_decoder_initial_state(self, batch_size, encoder_state, dtype):
        # todo tf2 attentinon_mechanism vs cell_type to determine method
        #      for initial state setup, attention meth
        if self.attention_mechanism is not None:
            decoder_initial_state = self.decoder_rnncell.get_initial_state(
                batch_size=batch_size,
                dtype=dtype)
            decoder_initial_state = decoder_initial_state.clone(
                cell_state=encoder_state)
        else:
            decoder_initial_state = encoder_state

        return decoder_initial_state

    def decoder_training(self, encoder_output,
            target=None, encoder_end_state=None):

        # ================ Setup ================
        GO_SYMBOL = self.vocab_size
        END_SYMBOL = 0
        batch_size = encoder_output.shape[0]
        encoder_sequence_length = sequence_length_3D(encoder_output)

        # Prepare target for decoding
        target_sequence_length = sequence_length_2D(target)
        start_tokens = tf.tile([GO_SYMBOL], [batch_size])
        end_tokens = tf.tile([END_SYMBOL], [batch_size])
        if self.is_timeseries:
            start_tokens = tf.cast(start_tokens, tf.float32)
            end_tokens = tf.cast(end_tokens, tf.float32)
        targets_with_go_and_eos = tf.concat([
            tf.expand_dims(start_tokens, 1),
            target,  # todo tf2: right now cast to tf.int32, fails if tf.int64
            tf.expand_dims(end_tokens, 1)], 1)
        target_sequence_length_with_eos = target_sequence_length + 1

        # Decoder Embeddings
        decoder_emb_inp = self.decoder_embedding(targets_with_go_and_eos)

        # Setting up decoder memory from encoder output and Zero State for AttentionWrapperState
        if self.attention_mechanism is not None:
            self.attention_mechanism.setup_memory(
                encoder_output,
                memory_sequence_length=encoder_sequence_length
            )

        decoder_initial_state = self.build_decoder_initial_state(
            batch_size,
            encoder_state=encoder_end_state,
            dtype=tf.float32
        )

        # BasicDecoderOutput
        outputs, final_state, final_sequence_lengths = self.decoder(
            decoder_emb_inp,
            initial_state=decoder_initial_state,
            sequence_length=target_sequence_length_with_eos
        )

        return outputs.rnn_output #, outputs, final_state, final_sequence_lengths

    def decoder_inference(self, encoder_output, encoder_end_state=None):

        # ================ Setup ================
        GO_SYMBOL = self.vocab_size
        END_SYMBOL = 0
        batch_size = encoder_output.shape[0]
        encoder_sequence_length = sequence_length_3D(encoder_output)

        # ================ predictions =================
        greedy_sampler = tfa.seq2seq.GreedyEmbeddingSampler()

        decoder_input = tf.expand_dims(
            [GO_SYMBOL] * batch_size, 1)

        decoder_emb_inp = self.decoder_embedding(decoder_input)

        decoder_instance = tfa.seq2seq.BasicDecoder(
            cell=self.decoder_rnncell, sampler=greedy_sampler,
            output_layer=self.dense_layer)

        if self.attention_mechanism is not None:
            self.attention_mechanism.setup_memory(
                encoder_output,
                memory_sequence_length=sequence_length_3D(encoder_output)
            )

        # Since we do not know the target sequence lengths in advance,
        # we use maximum_iterations to limit the translation lengths.
        # One heuristic is to decode up to two times the source sentence lengths.
        maximum_iterations = tf.round(tf.reduce_max(self.max_sequence_length) * 2)

        decoder_initial_state = self.build_decoder_initial_state(
            batch_size,
            encoder_state=encoder_end_state,
            dtype=tf.float32)

        start_tokens = tf.fill([batch_size], GO_SYMBOL)
        end_token = END_SYMBOL

        # initialize inference decoder
        decoder_embedding_matrix = self.decoder_embedding.variables[0]
        (first_finished, first_inputs,
         first_state) = decoder_instance.initialize(decoder_embedding_matrix,
                                                    start_tokens=start_tokens,
                                                    end_token=end_token,
                                                    initial_state=decoder_initial_state)

        inputs = first_inputs
        state = first_state
        predictions = np.empty((batch_size, 0), dtype=np.int32)
        for j in range(maximum_iterations):
            outputs, next_state, next_inputs, finished = decoder_instance.step(
                j, inputs, state)
            inputs = next_inputs
            state = next_state
            outputs = np.expand_dims(outputs.sample_id, axis=-1)
            predictions = np.append(predictions, outputs, axis=-1)

        logits = None # todo tf2 how to compute?
        probabilities = None # todo tf2 need logits

        return predictions, probabilities

############### Old code  todo tf2 remove
    #     self.embeddings_dec = Embedding(num_classes, embedding_size)
    #     self.decoder_cell = LSTMCell(state_size)
    #
    #     if attention_mechanism:
    #         if attention_mechanism == 'bahdanau':
    #             pass
    #         elif attention_mechanism == 'luong':
    #             self.attention_mechanism = tfa.seq2seq.LuongAttention(
    #                 state_size,
    #                 None,  # todo tf2: confirm on need
    #                 memory_sequence_length=max_sequence_length  # todo tf2: confirm inputs or output seq length
    #             )
    #         else:
    #             raise ValueError(
    #                 "Attention specificaiton '{}' is invalid.  Valid values are "
    #                 "'bahdanau' or 'luong'.".format(self.attention_mechanism))
    #
    #         self.decoder_cell = tfa.seq2seq.AttentionWrapper(
    #             self.decoder_cell,
    #             self.attention_mechanism
    #         )
    #
    #     self.sampler = tfa.seq2seq.sampler.TrainingSampler()
    #
    #     self.projection_layer = Dense(
    #         units=num_classes,
    #         use_bias=use_bias,
    #         kernel_initializer=weights_initializer,
    #         bias_initializer=bias_initializer,
    #         kernel_regularizer=weights_regularizer,
    #         bias_regularizer=bias_regularizer,
    #         activity_regularizer=activity_regularizer
    #     )
    #
    #     self.decoder = \
    #         tfa.seq2seq.basic_decoder.BasicDecoder(self.decoder_cell,
    #                                                 self.sampler,
    #                                                 output_layer=self.projection_layer)
    #
    # # todo tf2: remove if not needed
    # # def build_initial_state1(self, batch_size, encoder_state=None):
    # #     initial_state = self.decoder_cell.get_initial_state(
    # #         inputs=encoder_state,
    # #         batch_size=batch_size,
    # #         dtype=tf.float32
    # #     )
    # #     return initial_state
    #
    # def build_sequence_lengths(self, batch_size, max_sequence_length):
    #     return np.ones((batch_size,)).astype(np.int32) * max_sequence_length
    #
    # def build_initial_state(self, batch_size):
    #     initial_state = self.decoder_cell.get_initial_state(
    #         batch_size=batch_size, dtype=tf.float32
    #     )
    #     # todo tf2:  need to confirm this is correct construct
    #     zero_state = tf.zeros([batch_size, self.state_size])
    #     initial_state = initial_state.clone(cell_state=[zero_state, zero_state])
    #     return initial_state
#############old code

    def call(
            self,
            inputs,
            training=None,
            mask=None,
            target=None
    ):
        input = inputs['hidden']
        try:
            encoder_output_state = inputs['encoder_output_state']
        except KeyError:
            encoder_output_state = None

        if len(input.shape) != 3 and self.attention_mechanism is not None:
            raise ValueError(
                'Encoder outputs rank is {}, but should be 3 [batch x sequence x hidden] '
                'when attention mechanism is {}. '
                'If you are using a sequential encoder or combiner consider setting reduce_output to None '
                'and flatten to False if those parameters apply.'
                'Also make sure theat reduce_input of output feature is None,'.format(
                    len(input.shape), self.attention_name))
        if len(input.shape) != 2 and self.attention_mechanism is None:
            raise ValueError(
                'Encoder outputs rank is {}, but should be 2 [batch x hidden] '
                'when attention mechanism is {}. '
                'Consider setting reduce_input of output feature to a value different from None.'.format(
                    len(input.shape), self.attention_name))

        batch_size = input.shape[0]
        print(">>>>>>batch_size", batch_size)

        # Assume we have a final state
        encoder_end_state = encoder_output_state

        # in case we don't have a final state set to default value
        if self.cell_type in 'lstm' and encoder_end_state is None:
            encoder_end_state = [
                tf.zeros([batch_size, self.rnn_units], tf.float32),
                tf.zeros([batch_size, self.rnn_units], tf.float32)
            ]
        elif self.cell_type in {'rnn', 'gru'} and encoder_end_state is None:
            encoder_end_state = tf.zeros([batch_size, self.rnn_units], tf.float32)

        if training:
            return_tuple = self.decoder_training(
                input,
                target=target,
                encoder_end_state=encoder_end_state,
            )
        else:
            # todo tf2 clean-up code
            # return_tuple = self.decoder_inference(
            #     input,
            #     encoder_end_state=encoder_end_state
            # )
            return_tuple = {
                'encoder_output': input,
                'encoder_output_state': encoder_end_state
            }

        return return_tuple

        # if self.is_timeseries:
        #     vocab_size = 1
        # else:
        #     vocab_size = self.num_classes
        #
        # if not self.regularize:
        #     regularizer = None
        #
        # predictions_sequence, predictions_sequence_scores, \
        # predictions_sequence_length_with_eos, \
        # targets_sequence_length_with_eos, eval_logits, train_logits, \
        # class_weights, class_biases = recurrent_decoder(
        #     hidden,
        #     targets,
        #     output_feature['max_sequence_length'],
        #     vocab_size,
        #     cell_type=self.cell_type,
        #     state_size=self.state_size,
        #     embedding_size=self.embedding_size,
        #     beam_width=self.beam_width,
        #     num_layers=self.num_layers,
        #     attention_mechanism=self.attention_mechanism,
        #     is_timeseries=self.is_timeseries,
        #     embeddings=tied_embeddings_tensor,
        #     initializer=self.initializer,
        #     regularizer=regularizer
        # )
        #
        # probabilities_target_sequence = tf.nn.softmax(eval_logits)
        #
        # return predictions_sequence, predictions_sequence_scores, \
        #        predictions_sequence_length_with_eos, \
        #        probabilities_target_sequence, targets_sequence_length_with_eos, \
        #        eval_logits, train_logits, class_weights, class_biases


class SequenceTaggerDecoder(Layer):
    def __init__(
            self,
            num_classes,
            use_bias=True,
            weights_initializer='glorot_uniform',
            bias_initializer='zeros',
            weights_regularizer=None,
            bias_regularizer=None,
            activity_regularizer=None,
            attention=False,
            is_timeseries=False,
            **kwargs
    ):
        super(SequenceTaggerDecoder, self).__init__()
        self.attention = attention

        if is_timeseries:
            num_classes = 1

        self.decoder_layer = Dense(
            units=num_classes,
            use_bias=use_bias,
            kernel_initializer=weights_initializer,
            bias_initializer=bias_initializer,
            kernel_regularizer=weights_regularizer,
            bias_regularizer=bias_regularizer,
            activity_regularizer=activity_regularizer
        )

    def call(
            self,
            inputs,
            training=None,
            mask=None
    ):
        if len(inputs.shape) != 3:
            raise ValueError(
                'Decoder inputs rank is {}, but should be 3 [batch x sequence x hidden] '
                'when using a tagger sequential decoder. '
                'Consider setting reduce_output to null / None if a sequential encoder / combiner is used.'.format(
                    len(inputs.shape)))

        # hidden shape [batch_size, sequence_length, hidden_size]
        logits = self.decoder_layer(inputs)

        # TODO tf2 add feed forward attention

        # logits shape [batch_size, sequence_length, vocab_size]
        return logits


