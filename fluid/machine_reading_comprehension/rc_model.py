#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserve.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import paddle.fluid.layers as layers
import paddle.fluid as fluid
import numpy as np


def dropout(input, args):
    if args.drop_rate:
        return layers.dropout(
            input,
            dropout_prob=args.drop_rate,
            seed=args.random_seed,
            is_test=False)
    else:
        return input


def bi_lstm_encoder(input_seq, gate_size, para_name, args):
    # A bi-directional lstm encoder implementation.
    # Linear transformation part for input gate, output gate, forget gate
    # and cell activation vectors need be done outside of dynamic_lstm.
    # So the output size is 4 times of gate_size.

    input_forward_proj = layers.fc(
        input=input_seq,
        param_attr=fluid.ParamAttr(name=para_name + '_fw_gate_w'),
        size=gate_size * 4,
        act=None,
        bias_attr=False)
    input_reversed_proj = layers.fc(
        input=input_seq,
        param_attr=fluid.ParamAttr(name=para_name + '_bw_gate_w'),
        size=gate_size * 4,
        act=None,
        bias_attr=False)
    forward, _ = layers.dynamic_lstm(
        input=input_forward_proj,
        size=gate_size * 4,
        use_peepholes=False,
        param_attr=fluid.ParamAttr(name=para_name + '_fw_lstm_w'),
        bias_attr=fluid.ParamAttr(name=para_name + '_fw_lstm_b'))
    reversed, _ = layers.dynamic_lstm(
        input=input_reversed_proj,
        param_attr=fluid.ParamAttr(name=para_name + '_bw_lstm_w'),
        bias_attr=fluid.ParamAttr(name=para_name + '_bw_lstm_b'),
        size=gate_size * 4,
        is_reverse=True,
        use_peepholes=False)

    encoder_out = layers.concat(input=[forward, reversed], axis=1)
    return encoder_out


def encoder(input_name, para_name, shape, hidden_size, args):
    input_ids = layers.data(
        name=input_name, shape=[1], dtype='int64', lod_level=1)
    input_embedding = layers.embedding(
        input=input_ids,
        size=shape,
        dtype='float32',
        is_sparse=True,
        param_attr=fluid.ParamAttr(name='embedding_para'))

    encoder_out = bi_lstm_encoder(
        input_seq=input_embedding,
        gate_size=hidden_size,
        para_name=para_name,
        args=args)
    return dropout(encoder_out, args)


def attn_flow(q_enc, p_enc, p_ids_name, args):
    tag = p_ids_name + "::"
    drnn = layers.DynamicRNN()
    with drnn.block():
        h_cur = drnn.step_input(p_enc)
        u_all = drnn.static_input(q_enc)
        h_expd = layers.sequence_expand(x=h_cur, y=u_all)
        s_t_mul = layers.elementwise_mul(x=u_all, y=h_expd, axis=0)
        s_t_sum = layers.reduce_sum(input=s_t_mul, dim=1, keep_dim=True)
        s_t_re = layers.reshape(s_t_sum, shape=[-1, 0])
        s_t = layers.sequence_softmax(input=s_t_re)
        u_expr = layers.elementwise_mul(x=u_all, y=s_t, axis=0)
        u_expr = layers.sequence_pool(input=u_expr, pool_type='sum')

        b_t = layers.sequence_pool(input=s_t_sum, pool_type='max')
        drnn.output(u_expr, b_t)
    U_expr, b = drnn()
    b_norm = layers.sequence_softmax(input=b)
    h_expr = layers.elementwise_mul(x=p_enc, y=b_norm, axis=0)
    h_expr = layers.sequence_pool(input=h_expr, pool_type='sum')

    H_expr = layers.sequence_expand(x=h_expr, y=p_enc)
    H_expr = layers.lod_reset(x=H_expr, y=p_enc)
    h_u = layers.elementwise_mul(x=p_enc, y=U_expr, axis=0)
    h_h = layers.elementwise_mul(x=p_enc, y=H_expr, axis=0)

    g = layers.concat(input=[p_enc, U_expr, h_u, h_h], axis=1)
    return dropout(g, args)


def lstm_step(x_t, hidden_t_prev, cell_t_prev, size, para_name, args):
    def linear(inputs, para_name, args):
        return layers.fc(input=inputs,
                         size=size,
                         param_attr=fluid.ParamAttr(name=para_name + '_w'),
                         bias_attr=fluid.ParamAttr(name=para_name + '_b'))

    input_cat = layers.concat([hidden_t_prev, x_t], axis=1)
    forget_gate = layers.sigmoid(x=linear(input_cat, para_name + '_lstm_f',
                                          args))
    input_gate = layers.sigmoid(x=linear(input_cat, para_name + '_lstm_i',
                                         args))
    output_gate = layers.sigmoid(x=linear(input_cat, para_name + '_lstm_o',
                                          args))
    cell_tilde = layers.tanh(x=linear(input_cat, para_name + '_lstm_c', args))

    cell_t = layers.sums(input=[
        layers.elementwise_mul(
            x=forget_gate, y=cell_t_prev), layers.elementwise_mul(
                x=input_gate, y=cell_tilde)
    ])

    hidden_t = layers.elementwise_mul(x=output_gate, y=layers.tanh(x=cell_t))

    return hidden_t, cell_t


#point network
def point_network_decoder(p_vec, q_vec, hidden_size, args):
    tag = 'pn_decoder:'
    init_random = fluid.initializer.Normal(loc=0.0, scale=1.0)

    random_attn = layers.create_parameter(
        shape=[1, hidden_size],
        dtype='float32',
        default_initializer=init_random)
    random_attn = layers.fc(
        input=random_attn,
        size=hidden_size,
        act=None,
        param_attr=fluid.ParamAttr(name=tag + 'random_attn_fc_w'),
        bias_attr=fluid.ParamAttr(name=tag + 'random_attn_fc_b'))
    random_attn = layers.reshape(random_attn, shape=[-1])
    U = layers.fc(input=q_vec,
                  param_attr=fluid.ParamAttr(name=tag + 'q_vec_fc_w'),
                  bias_attr=False,
                  size=hidden_size,
                  act=None) + random_attn
    U = layers.tanh(U)

    logits = layers.fc(input=U,
                       param_attr=fluid.ParamAttr(name=tag + 'logits_fc_w'),
                       bias_attr=fluid.ParamAttr(name=tag + 'logits_fc_b'),
                       size=1,
                       act=None)
    scores = layers.sequence_softmax(input=logits)
    pooled_vec = layers.elementwise_mul(x=q_vec, y=scores, axis=0)
    pooled_vec = layers.sequence_pool(input=pooled_vec, pool_type='sum')

    init_state = layers.fc(
        input=pooled_vec,
        param_attr=fluid.ParamAttr(name=tag + 'init_state_fc_w'),
        bias_attr=fluid.ParamAttr(name=tag + 'init_state_fc_b'),
        size=hidden_size,
        act=None)

    def custom_dynamic_rnn(p_vec, init_state, hidden_size, para_name, args):
        tag = para_name + "custom_dynamic_rnn:"

        def static_rnn(step,
                       p_vec=p_vec,
                       init_state=None,
                       para_name='',
                       args=args):
            tag = para_name + "static_rnn:"
            ctx = layers.fc(
                input=p_vec,
                param_attr=fluid.ParamAttr(name=tag + 'context_fc_w'),
                bias_attr=fluid.ParamAttr(name=tag + 'context_fc_b'),
                size=hidden_size,
                act=None)

            beta = []
            c_prev = init_state
            m_prev = init_state
            for i in range(step):
                m_prev0 = layers.fc(
                    input=m_prev,
                    size=hidden_size,
                    act=None,
                    param_attr=fluid.ParamAttr(name=tag + 'm_prev0_fc_w'),
                    bias_attr=fluid.ParamAttr(name=tag + 'm_prev0_fc_b'))
                m_prev1 = layers.sequence_expand(x=m_prev0, y=ctx)

                Fk = ctx + m_prev1
                Fk = layers.tanh(Fk)
                logits = layers.fc(
                    input=Fk,
                    size=1,
                    act=None,
                    param_attr=fluid.ParamAttr(name=tag + 'logits_fc_w'),
                    bias_attr=fluid.ParamAttr(name=tag + 'logits_fc_b'))

                scores = layers.sequence_softmax(input=logits)
                attn_ctx = layers.elementwise_mul(x=p_vec, y=scores, axis=0)
                attn_ctx = layers.sequence_pool(input=attn_ctx, pool_type='sum')

                hidden_t, cell_t = lstm_step(
                    attn_ctx,
                    hidden_t_prev=m_prev,
                    cell_t_prev=c_prev,
                    size=hidden_size,
                    para_name=tag,
                    args=args)
                m_prev = hidden_t
                c_prev = cell_t
                beta.append(scores)
            return beta

        return static_rnn(
            2, p_vec=p_vec, init_state=init_state, para_name=para_name)

    fw_outputs = custom_dynamic_rnn(p_vec, init_state, hidden_size, tag + "fw:",
                                    args)
    bw_outputs = custom_dynamic_rnn(p_vec, init_state, hidden_size, tag + "bw:",
                                    args)

    start_prob = layers.elementwise_add(
        x=fw_outputs[0], y=bw_outputs[1], axis=0) / 2
    end_prob = layers.elementwise_add(
        x=fw_outputs[1], y=bw_outputs[0], axis=0) / 2

    return start_prob, end_prob


def fusion(g, args):
    m = bi_lstm_encoder(
        input_seq=g, gate_size=args.hidden_size, para_name='fusion', args=args)
    return dropout(m, args)


def rc_model(hidden_size, vocab, args):
    emb_shape = [vocab.size(), vocab.embed_dim]
    # stage 1:encode 
    p_ids_names = []
    q_ids_names = []
    ms = []
    gs = []
    qs = []
    for i in range(args.doc_num):
        p_ids_name = "pids_%d" % i
        p_ids_names.append(p_ids_name)
        p_enc_i = encoder(p_ids_name, 'p_enc', emb_shape, hidden_size, args)

        q_ids_name = "qids_%d" % i
        q_ids_names.append(q_ids_name)
        q_enc_i = encoder(q_ids_name, 'q_enc', emb_shape, hidden_size, args)

        # stage 2:match
        g_i = attn_flow(q_enc_i, p_enc_i, p_ids_name, args)
        # stage 3:fusion
        m_i = fusion(g_i, args)
        ms.append(m_i)
        gs.append(g_i)
        qs.append(q_enc_i)
    m = layers.sequence_concat(input=ms)
    g = layers.sequence_concat(input=gs)
    q_vec = layers.sequence_concat(input=qs)

    # stage 4:decode 
    start_probs, end_probs = point_network_decoder(
        p_vec=m, q_vec=q_vec, hidden_size=hidden_size, args=args)

    start_labels = layers.data(
        name="start_lables", shape=[1], dtype='float32', lod_level=1)
    end_labels = layers.data(
        name="end_lables", shape=[1], dtype='float32', lod_level=1)

    cost0 = layers.sequence_pool(
        layers.cross_entropy(
            input=start_probs, label=start_labels, soft_label=True),
        'sum')
    cost1 = layers.sequence_pool(
        layers.cross_entropy(
            input=end_probs, label=end_labels, soft_label=True),
        'sum')

    cost0 = layers.mean(cost0)
    cost1 = layers.mean(cost1)
    cost = cost0 + cost1
    cost.persistable = True

    feeding_list = q_ids_names + ["start_lables", "end_lables"] + p_ids_names
    return cost, start_probs, end_probs, feeding_list
