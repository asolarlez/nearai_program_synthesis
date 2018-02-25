import collections

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable

from program_synthesis.models import base
from program_synthesis.models import beam_search
from program_synthesis.models import prepare_spec
from program_synthesis.models.modules import attention
from program_synthesis.models.modules import karel as karel_modules
from program_synthesis.models.modules import karel_common
from program_synthesis.datasets.karel import karel_runtime

action_to_id = {
    '<s>': 0,
    '</s>': 1,
    'move': 2,
    'turnLeft': 3,
    'turnRight': 4,
    'putMarker': 5,
    'pickMarker': 6,
}
id_to_action = {
    0: '<s>',
    1: '</s>',
    2: 'move',
    3: 'turnLeft',
    4: 'turnRight',
    5: 'putMarker',
    6: 'pickMarker',
}


class TraceDecoderState(
        collections.namedtuple('TraceDecoderState', ['field', 'h', 'c']),
        beam_search.BeamSearchState):
    def select_for_beams(self, batch_size, indices):
        '''Return the hidden state necessary to continue the beams.

        batch size: int
        indices: 2 x batch size * beam size LongTensor
        '''
        # field: batch size (* beam size) x 15 x 18 x 18, numpy.ndarray
        selected = [
            self.field.reshape(
                batch_size, -1,
                *self.field.shape[1:])[tuple(indices.data.numpy())]
        ]
        for v in self.h, self.c:
            # before: 2 x batch size (* beam size) x num pairs x hidden
            # after:  2 x batch size x beam size x num pairs x hidden
            v = v.view(2, batch_size, -1, *v.shape[2:])
            # result: 2 x indices.shape[1] x num pairs x hidden
            selected.append(v[(slice(None), ) + tuple(indices.data.numpy())])
        return TraceDecoderState(*selected)


class TraceDecoder(nn.Module):
    def __init__(self, args):
        super(TraceDecoder, self).__init__()
        self._cuda = args.cuda

        if args.karel_trace_grid_enc == 'lgrl':
            self.grid_encoder = karel_common.LGRLGridEncoder(args)
        elif args.karel_trace_grid_enc == 'presnet':
            self.grid_encoder = karel_common.PResNetGridEncoder(args)
        elif args.karel_trace_grid_enc == 'none':
            self.grid_encoder = None
        else:
            raise ValueError(args.karel_trace_grid_enc)

        self.decoder = nn.LSTM(
            input_size=256 + (256 if self.grid_encoder else 0) + 512,
            hidden_size=256,
            num_layers=2)

        # Actions:
        # <s>, </s>, move, turn{Left,Right}, {pick,put}Marker
        num_actions = 7
        self.action_embed = nn.Embedding(num_actions, 256)
        self.out = nn.Linear(256, num_actions)

    def forward(self, io_embed, trace_grids, input_actions, output_actions,
                io_embed_indices):
        # io_embed: batch size x 512
        # trace_grids: PackedSequencePlus
        #   batch size x trace length x 15 x 18 x 18
        # input_actions: PackedSequencePlus
        #   batch size x trace length
        # output_actions: PackedSequencePlus
        #   batch size x trace length
        # io_embed_indices: len(input_actions)

        # PackedSequencePlus, batch size x sequence length x 256
        input_actions = input_actions.apply(self.action_embed)

        if self.grid_encoder:
            # PackedSequencePlus, batch size x sequence length x 256
            trace_grids = trace_grids.apply(self.grid_encoder)
            dec_input = input_actions.apply(
                    lambda d: torch.cat((d, trace_grids.ps.data,
                        io_embed[io_embed_indices]), dim=1))
        else:
            dec_input = input_actions.apply(
                lambda d: torch.cat((d, io_embed[io_embed_indices]), dim=1))

        dec_output, state = self.decoder(dec_input.ps)

        logits = self.out(dec_output.data)
        return logits, output_actions.ps.data

    def decode_token(self, token, state, memory, attentions=None):
        io_embed = memory.value

        # Advance the grids with the last action
        if self.grid_encoder:
            kr = karel_runtime.KarelRuntime()
            fields = state.field.copy()
            for field, action_id in zip(fields, token.data.cpu()):
                if action_id < 2:  # Ignore <s>, </s>
                    continue
                kr.init_from_array(field)
                getattr(kr, id_to_action[action_id])()
            fields_t = Variable(torch.from_numpy(fields.astype(np.float32)))
            if self._cuda:
                fields_t = fields_t.cuda()
            grid_embed = self.grid_encoder(fields_t)
        else:
            fields = state.field
            grid_embed = None

        # action_embed shape: batch size (* beam size) x 256
        action_embed = self.action_embed(token)

        # batch size (* beam size) x (256 + 256 + 512)
        dec_input = karel_modules.maybe_concat(
            (action_embed, grid_embed, io_embed), dim=1)
        dec_output, new_state = self.decoder(
            # 1 x batch size (* beam size) x hidden size
            dec_input.unsqueeze(0),
            (state.h, state.c))

        # Shape after squeezing: batch size (* beam size) x 256
        dec_output = dec_output.squeeze(0)
        logits = self.out(dec_output)

        return TraceDecoderState(fields, *new_state), logits

    def init_state(self, *args):
        return karel_modules.lstm_init(self._cuda, 2, 256, *args)


class TracePrediction(nn.Module):
    def __init__(self, args):
        super(TracePrediction, self).__init__()
        self.encoder = karel_modules.LGRLTaskEncoder(args)
        self.decoder = TraceDecoder(args)

    def encode(self, input_grids, output_grids):
        # input_grids: batch size x 15 x 18 x 18
        # output_grids: batch size x 15 x 18 x 18
        return self.encoder(input_grids, output_grids)

    def decode(self, *args):
        return self.decoder(*args)

    def decode_token(self, token, state, memory, attentions=None):
        return self.decoder.decode_token(token, state, memory, attentions)


class MultiEmbedding(nn.Module):
    def __init__(self, sizes, dims, combiner='sum'):
        super(MultiEmbedding, self).__init__()

        self.embeddings = nn.ModuleList(
            [nn.Embedding(size, dim) for size, dim in zip(sizes, dims)])
        if combiner == 'sum':
            self.combiner = self._sum
        elif combiner == 'cat':
            self.combiner = self._cat
        else:
            raise ValueError(combiner)

    def forward(self, inputs):
        return self.combiner([
            embedding(inputs[:, i])
            for i, embedding in enumerate(self.embeddings)
        ])

    def _sum(self, values):
        return torch.sum(torch.stack(values), dim=0)

    def _cat(self, values):
        return torch.cat(values, dim=1)


class IndividualTraceEncoder(nn.Module):
    def __init__(self, args):
        super(IndividualTraceEncoder, self).__init__()

        # Grid encoder
        if args.karel_trace_grid_enc == 'lgrl':
            self.grid_encoder = karel_common.LGRLGridEncoder(args)
        elif args.karel_trace_grid_enc == 'presnet':
            self.grid_encoder = karel_common.PResNetGridEncoder(args)
        elif args.karel_trace_grid_enc == 'none':
            self.grid_encoder = karel_common.none_fn
        else:
            raise ValueError(args.karel_trace_grid_enc)

        # Conditionals:
        #  front/left/rightIsClear, markersPresent
        if args.karel_trace_cond_enc == 'concat':
            self.cond_embed = MultiEmbedding([2] * 4, [256 / 4] * 4, 'cat')
        elif args.karel_trace_cond_enc == 'sum':
            self.cond_embed = MultiEmbedding([2] * 4, [256] * 4, 'sum')
        elif args.karel_trace_cond_enc == 'none':
            self.cond_embed = karel_common.none_fn
        else:
            raise ValueError(args.karel_trace_cond_enc)

        # Interleaved or together
        trace_enc_options = set(args.karel_trace_enc.split(':')[1:])
        interleave = karel_modules.set_pop(trace_enc_options, 'interleave')
        concat = karel_modules.set_pop(trace_enc_options, 'concat')
        assert interleave ^ concat
        assert not trace_enc_options
        self.interleave = interleave and not concat

        # Actions
        if args.karel_trace_action_enc == 'emb':
            self.action_embed = nn.Embedding(len(action_to_id), 512)
        elif args.karel_trace_action_enc == 'none':
            self.action_embed = karel_common.none_fn

        if self.interleave:
            assert self.action_embed  is not karel_common.none_fn
            enc_input_size = 512
            if self.grid_encoder is karel_common.none_fn:
                assert self.cond_embed is  not karel_common.none_fn
                self.cond_embed = nn.Sequential(
                        self.cond_embed,
                        nn.Linear(256, 512))

            if self.cond_embed is karel_common.none_fn:
                assert self.grid_encoder is  not karel_common.none_fn
                self.grid_encoder = nn.Sequential(
                        self.grid_encoder,
                        nn.Linear(256, 512))

        else:
            enc_input_size = 0
            if self.action_embed is not karel_common.none_fn:
                enc_input_size += 512
            if self.grid_encoder is not karel_common.none_fn:
                enc_input_size += 256
            if self.cond_embed is not karel_common.none_fn:
                enc_input_size += 256
            assert enc_input_size

        self.encoder = nn.LSTM(
            input_size=enc_input_size,
            hidden_size=256,
            num_layers=2,
            bidirectional=True)

    def forward(self, trace_grids, conds, actions, interleave):
        # 256 or none
        trace_grids = trace_grids.apply(self.grid_encoder)
        # 256 or none
        conds = conds.apply(self.cond_embed)
        # 512, always
        actions = actions.apply(self.action_embed)

        # Interleave them or not?
        if self.interleave:
            trace_grids = trace_grids.apply(
                    lambda _: karel_modules.maybe_concat(
                        (trace_grids.ps.data, conds.ps.data), dim=1))

            enc_input = prepare_spec.execute_interleave_psps(
                (trace_grids, actions), interleave)
        else:
            enc_input = trace_grids.apply(
                    lambda _: karel_modules.maybe_concat(
                        (trace_grids.ps.data, conds.ps.data, actions.ps.data),
                        dim=1))

        # output: PackedSequence, batch size x seq length x hidden (256 * 2)
        # state: 2 (layers) * 2 (directions) x batch x hidden size (256)
        output, state = self.encoder(enc_input.ps)

        return karel_modules.SequenceMemory(
            enc_input.with_new_ps(output), state)


class LatePoolingCodeDecoder(nn.Module):
    class Memory(
            collections.namedtuple('Memory', ('io', 'trace')),
            beam_search.BeamSearchMemory):
        def expand_by_beam(self, beam_size):
            io_exp = None if self.io is None else karel_modules.expand(
                self.io, beam_size)
            trace_exp = None if self.trace is None else self.trace.expand_by_beam(
                beam_size)
            return LatePoolingCodeDecoder.Memory(io_exp, trace_exp)

    # TODO: Deduplicate with LGRLRefineDecoderState.
    class State(
            collections.namedtuple('State',
                                   ['pairs_per_example', 'context', 'h', 'c']),
            beam_search.BeamSearchState):
        def select_for_beams(self, batch_size, indices):
            '''Return the hidden state necessary to continue the beams.

            batch size: int
            indices: 2 x batch size * beam size LongTensor
            '''
            selected = [
                None if self.context is None else self.context.view(
                    batch_size, -1,
                    *self.context.shape[1:])[indices.data.numpy()]
            ]
            for v in self.h, self.c:
                # before: 2 x batch size (* beam size) x num pairs x hidden
                # after:  2 x batch size x beam size x num pairs x hidden
                v = v.view(2, batch_size, -1, *v.shape[2:])
                # result: 2 x indices.shape[1] x num pairs x hidden
                selected.append(v[(slice(None), ) + tuple(indices.data.numpy(
                ))])
            return LatePoolingCodeDecoder.State(self.pairs_per_example,
                                                *selected)

        def truncate(self, k):
            return LatePoolingCodeDecoder.State(self.pairs_per_example,
                                                self.context[:k],
                                                self.h[:, :k], self.c[:, :k])

    def __init__(self, vocab_size, args):
        super(LatePoolingCodeDecoder, self).__init__()
        assert args.num_placeholders == 0
        self.args = args
        self._cuda = args.cuda

        trace_usage = args.karel_trace_usage.split(',')
        self.use_trace_memory = karel_modules.set_pop(trace_usage, 'memory')
        self.use_trace_state = karel_modules.set_pop(trace_usage, 'state')
        if karel_modules.set_pop(trace_usage, 'none'):
            self.use_trace_memory = False
            self.use_trace_state = False
        assert not trace_usage
        # Not yet implemented
        assert not self.use_trace_state

        if self.use_trace_memory:
            self.trace_attention = attention.SimpleSDPAttention(256, 512)

        self.use_io_embed = args.karel_io_enc != 'none'

        self.code_embed = nn.Embedding(vocab_size, 256)
        self.decoder = nn.LSTM(
            input_size=256 +  # last code token
            (512 if self.use_io_embed else 0) +  # io embedding
            (512 if self.use_trace_memory else 0),  # memory from trace
            hidden_size=256,
            num_layers=2)

        self.out = nn.Linear(256 + (512 if self.use_trace_memory else 0),
                             vocab_size)

    def prepare_memory(self, batch_size, pairs_per_example, io_embed,
                       trace_memory):
        if self.use_trace_memory and trace_memory is not None:
            # batch * num pairs x trace length x 512
            trace_memory, trace_lengths = trace_memory.mem.pad(
                batch_first=True)
            trace_mask = base.get_attn_mask(trace_lengths, self._cuda)
            # batch x num pairs x trace length x 512
            trace_memory = trace_memory.view(batch_size, pairs_per_example,
                                             *trace_memory.shape[1:])
            trace_mask = trace_mask.view(batch_size, pairs_per_example,
                                         *trace_mask.shape[1:])
            trace_memory = base.MaskedMemory(trace_memory, trace_mask)
        else:
            trace_memory = None

        return LatePoolingCodeDecoder.Memory(io_embed, trace_memory)

    def forward(self, batch_size, pairs_per_example, io_embed, trace_memory,
                input_code, output_code):
        # io_embed: batch x num pairs x 512 or None
        # trace_memory:
        #   SequenceMemory, containing:
        #     mem: PackedSequencePlus,
        #          batch size * num pairs x trace length x 512
        #     state: tuple containing two of
        #       2 (layers) * 2 (directions) x batch size * num pairs x 256
        #   or None
        # input_code: PackedSequencePlus, batch x seq length
        # output_code: PackedSequencePlus, batch x seq length

        # PackedSequencePlus, batch x seq length x 256
        input_code = input_code.apply(self.code_embed)
        state = self.init_state(batch_size, pairs_per_example)
        memory = self.prepare_memory(batch_size, pairs_per_example, io_embed,
                                     trace_memory)

        # batch x num pairs x 512
        io_embed_slice = io_embed = None
        if memory.io is not None:
            io_embed = io_embed_slice = memory.io[list(input_code.orig_to_sort)]
        # MaskedMemory, batch x num pairs x trace length x 512
        trace_memory = trace_memory_slice = None
        if memory.trace is not None:
            trace_memory = trace_memory_slice = memory.trace.apply(
                lambda t: t[list(input_code.orig_to_sort)])
        memory = LatePoolingCodeDecoder.Memory(io_embed_slice,
                trace_memory_slice)

        logits = []
        offset = 0
        last_bs = 0
        batch_order = input_code.orig_to_sort
        for i, bs in enumerate(input_code.ps.batch_sizes):
            # bs x 256
            dec_data_slice = input_code.ps.data[offset:offset + bs]
            # bs x num pairs x 256
            dec_data_slice = dec_data_slice.unsqueeze(1).expand(
                -1, pairs_per_example, -1)

            if bs < last_bs:
                if io_embed is not None:
                    io_embed_slice = io_embed[:bs]
                if trace_memory is not None:
                    trace_memory_slice = trace_memory.apply(lambda t: t[:bs])
                memory = LatePoolingCodeDecoder.Memory(
                    io_embed_slice, trace_memory_slice)
                batch_order = batch_order[:bs]
                state = state.truncate(bs)

            state, logits_for_t = self.compute_next_token_logits(
                state, memory, dec_data_slice)
            logits.append(logits_for_t)
            offset += bs
            last_bs = bs

        logits = torch.cat(logits, dim=0)
        labels = output_code.ps.data
        return logits, labels

    def decode_token(self, token, state, memory, attentions):
        pairs_per_example = state.pairs_per_example

        # token: LongTensor, batch (* beam)
        token_emb = self.code_embed(token)

        # TODO handle attentions arg
        return self.compute_next_token_logits(
            state, memory,
            token_emb.unsqueeze(1).expand(-1, pairs_per_example, -1))

    def compute_next_token_logits(self, state, memory, last_token_emb):
        # state:
        #   context: batch (* beam) x num pairs x hidden size
        #   h: 2 x batch (* beam) x num pairs x hidden size
        #   c: 2 x batch (* beam) x num pairs x hidden size
        # memory:
        #   io: batch (* beam) x num pairs x hidden size
        #   trace: batch (* beam) x num pairs x trace length x hidden size
        # last_token_emb: batch (* beam) x num pairs x hidden size
        pairs_per_example = state.pairs_per_example

        dec_input = karel_modules.maybe_concat(
            (last_token_emb, memory.io, state.context), dim=2)
        # batch (* beam) * num pairs x hidden size
        dec_input = dec_input.view(-1, dec_input.shape[-1])

        dec_output, new_state = self.decoder(
            # 1 x batch (* beam) * num pairs x hidden size
            dec_input.unsqueeze(0),
            # v before: 2 x batch (* beam) x num pairs x hidden
            # v after:  2 x batch (* beam) * num pairs x hidden
            (karel_modules.flatten(state.h, 1),
             karel_modules.flatten(state.c, 1)))
        new_state = (new_state[0].view_as(state.h),
                     new_state[1].view_as(state.c))
        dec_output = dec_output.squeeze(0)

        new_context = None
        if memory.trace:
            new_context, _ = self.trace_attention(
                dec_output,
                karel_modules.flatten(memory.trace.memory, 0),
                karel_modules.flatten(memory.trace.attn_mask, 0))

        # batch (* beam) * num pairs x hidden
        emb_for_logits = karel_modules.maybe_concat(
            (new_context, dec_output), dim=1)
        # batch (* beam) x hidden
        emb_for_logits, _ = emb_for_logits.view(
            -1, pairs_per_example, emb_for_logits.shape[-1]).max(dim=1)
        # batch (* beam) x vocab size
        logits = self.out(emb_for_logits)

        return LatePoolingCodeDecoder.State(
            state.pairs_per_example, None if new_context is None else
            new_context.view(-1, pairs_per_example, new_context.shape[-1]),
            *new_state), logits

    def init_state(self, batch_size, pairs_per_example):
        if self.use_trace_memory:
            context_size = (batch_size, pairs_per_example, 512)
            context = Variable(torch.zeros(*context_size))
            if self._cuda:
                context = context.cuda()
        else:
            context = None

        return LatePoolingCodeDecoder.State(
            pairs_per_example, context, *karel_modules.lstm_init(
                self._cuda, 2, 256, batch_size, pairs_per_example))


class CodeFromTraces(nn.Module):
    def __init__(self, vocab_size, args):
        super(CodeFromTraces, self).__init__()

        if args.karel_io_enc == 'lgrl':
            self.io_encoder = karel_modules.LGRLTaskEncoder(args)
        elif args.karel_io_enc == 'none':
            self.io_encoder = karel_common.none_fn
        else:
            raise ValueError(args.karel_io_enc)

        if args.karel_trace_enc.startswith('indiv'):
            self.trace_encoder = IndividualTraceEncoder(args)
        elif args.karel_trace_enc == 'none':
            self.trace_encoder = karel_common.none_fn
        else:
            raise ValueError(args.karel_trace_enc)

        if args.karel_code_dec.startswith('latepool'):
            self.decoder = LatePoolingCodeDecoder(vocab_size, args)
        else:
            raise ValueError(args.karel_code_dec)

    def encode(self, input_grids, output_grids, trace_grids, conds, actions,
               interleave):
        # batch size x num pairs x 512
        io_embed = self.io_encoder(input_grids, output_grids)

        # PackedSequencePlus, batch size * num pairs x length x 512
        trace_memory = self.trace_encoder(trace_grids, conds, actions,
                                          interleave)

        return io_embed, trace_memory

    def decode(self, *args):
        return self.decoder(*args)

    def decode_token(self, token, state, memory, attentions=None):
        return self.decoder.decode_token(token, state, memory, attentions)
